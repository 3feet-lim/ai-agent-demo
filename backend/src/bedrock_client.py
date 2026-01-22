"""
LangChain + LangGraph 기반 Bedrock 클라이언트
MCP 도구를 실제로 호출하는 ReAct 에이전트 구현
"""
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Any

from langchain_aws import ChatBedrock
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.graph import StateGraph, MessagesState, START, END
from pydantic import BaseModel, Field, create_model

from .config import get_settings
from .mcp_manager import get_mcp_manager, MCPContext, MCPTool

logger = logging.getLogger(__name__)


def create_pydantic_model_from_schema(name: str, schema: dict) -> type[BaseModel]:
    """MCP input_schema에서 Pydantic 모델 동적 생성"""
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))

    fields = {}
    for prop_name, prop_schema in properties.items():
        prop_type = prop_schema.get("type", "string")
        description = prop_schema.get("description", "")

        # JSON Schema 타입을 Python 타입으로 매핑
        type_mapping = {
            "string": str,
            "integer": int,
            "number": float,
            "boolean": bool,
            "array": list,
            "object": dict,
        }
        python_type = type_mapping.get(prop_type, Any)

        # 필수 여부에 따라 기본값 설정
        if prop_name in required:
            fields[prop_name] = (python_type, Field(description=description))
        else:
            fields[prop_name] = (Optional[python_type], Field(default=None, description=description))

    # 빈 스키마인 경우 기본 모델 반환
    if not fields:
        return create_model(f"{name}Input")

    return create_model(f"{name}Input", **fields)


class MCPToolWrapper(BaseTool):
    """MCP 도구를 LangChain BaseTool로 래핑"""
    name: str
    description: str
    args_schema: type[BaseModel]
    mcp_tool: MCPTool
    mcp_manager: Any

    class Config:
        arbitrary_types_allowed = True

    async def _arun(self, **kwargs) -> str:
        """비동기 도구 실행"""
        try:
            logger.info(f"MCP tool {self.name} called with: {kwargs}")
            result = await self.mcp_manager.execute_tool(self.mcp_tool.name, kwargs)

            # MCP 결과를 문자열로 변환
            if hasattr(result, 'content'):
                contents = []
                for item in result.content:
                    if hasattr(item, 'text'):
                        contents.append(item.text)
                    else:
                        contents.append(str(item))
                return "\n".join(contents)
            return str(result)
        except Exception as e:
            logger.error(f"Tool execution error for {self.name}: {e}")
            return f"도구 실행 오류: {str(e)}"

    def _run(self, **kwargs) -> str:
        """동기 도구 실행 (사용하지 않음)"""
        raise NotImplementedError("Use async version")


def create_mcp_tool(mcp_tool: MCPTool, mcp_manager) -> BaseTool:
    """MCP 도구를 LangChain Tool로 변환"""
    # input_schema에서 Pydantic 모델 생성
    schema = mcp_tool.input_schema or {}
    args_model = create_pydantic_model_from_schema(mcp_tool.name, schema)

    return MCPToolWrapper(
        name=mcp_tool.name,
        description=mcp_tool.description or f"{mcp_tool.name} 도구",
        args_schema=args_model,
        mcp_tool=mcp_tool,
        mcp_manager=mcp_manager,
    )


class BedrockAgent:
    """
    LangChain + LangGraph 기반 Bedrock 에이전트

    MCP에서 제공하는 도구를 실제로 호출하는 ReAct 에이전트입니다.
    """

    def __init__(self):
        settings = get_settings()
        self.model_id = settings.bedrock_model_id
        self.region = settings.aws_region
        self._mcp_manager = None
        self._tools: list[BaseTool] = []
        self._llm = None
        self._graph = None

    async def _ensure_initialized(self):
        """비동기 초기화 보장"""
        if self._llm is not None:
            return

        settings = get_settings()

        # MCP 매니저에서 도구 가져오기
        self._mcp_manager = await get_mcp_manager()
        context = await self._mcp_manager.get_context()

        # MCP 도구를 LangChain 도구로 변환
        self._tools = []
        for mcp_tool in context.tools:
            try:
                lc_tool = create_mcp_tool(mcp_tool, self._mcp_manager)
                self._tools.append(lc_tool)
                logger.info(f"Registered tool: {mcp_tool.name}")
            except Exception as e:
                logger.warning(f"Failed to create tool {mcp_tool.name}: {e}")

        # LangChain ChatBedrock 모델 초기화
        self._llm = ChatBedrock(
            model_id=self.model_id,
            region_name=self.region,
            model_kwargs={
                "max_tokens": 4096,
                "temperature": 0.7,
            }
        )

        # 도구가 있으면 바인딩
        if self._tools:
            self._llm_with_tools = self._llm.bind_tools(self._tools)
            logger.info(f"LLM bound with {len(self._tools)} tools")
        else:
            self._llm_with_tools = self._llm
            logger.info("No tools available, using plain LLM")

        # LangGraph 워크플로우 생성
        self._graph = self._build_graph()

    def _build_graph(self) -> StateGraph:
        """LangGraph 워크플로우 구성 - ReAct 패턴"""

        async def agent_node(state: MessagesState) -> MessagesState:
            """에이전트 노드: LLM 호출 (도구 바인딩됨)"""
            messages = state["messages"]
            response = await self._llm_with_tools.ainvoke(messages)
            return {"messages": [response]}

        async def tools_node(state: MessagesState) -> MessagesState:
            """도구 노드: 도구 호출 실행"""
            messages = state["messages"]
            last_message = messages[-1]

            tool_messages = []

            # tool_calls가 있는지 확인
            if hasattr(last_message, 'tool_calls') and last_message.tool_calls:
                for tool_call in last_message.tool_calls:
                    tool_name = tool_call["name"]
                    tool_args = tool_call["args"]
                    tool_id = tool_call["id"]

                    logger.info(f"Executing tool: {tool_name} with args: {tool_args}")

                    # 도구 찾기 및 실행
                    result = "도구를 찾을 수 없습니다."
                    for tool in self._tools:
                        if tool.name == tool_name:
                            try:
                                result = await tool.ainvoke(tool_args)
                            except Exception as e:
                                result = f"도구 실행 오류: {str(e)}"
                                logger.error(f"Tool execution error: {e}")
                            break

                    tool_messages.append(ToolMessage(
                        content=str(result),
                        tool_call_id=tool_id
                    ))

            return {"messages": tool_messages}

        def should_continue(state: MessagesState) -> str:
            """도구 호출 여부 결정"""
            messages = state["messages"]
            last_message = messages[-1]

            # 도구 호출이 있으면 tools 노드로
            if hasattr(last_message, 'tool_calls') and last_message.tool_calls:
                return "tools"
            # 없으면 종료
            return END

        # 그래프 정의
        graph = StateGraph(MessagesState)
        graph.add_node("agent", agent_node)
        graph.add_node("tools", tools_node)

        graph.add_edge(START, "agent")
        graph.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
        graph.add_edge("tools", "agent")  # 도구 실행 후 다시 에이전트로

        return graph.compile()

    def _get_current_time_info(self) -> str:
        """현재 시간 정보 생성"""
        now_utc = datetime.now(timezone.utc)
        now_kst = now_utc.astimezone(timezone(timedelta(hours=9)))

        return f"""현재 시간 정보:
- UTC: {now_utc.strftime('%Y-%m-%d %H:%M:%S')}
- KST (한국 시간): {now_kst.strftime('%Y-%m-%d %H:%M:%S')}
"""

    def _build_system_prompt(self, mcp_context: MCPContext) -> str:
        """시스템 프롬프트 생성"""
        base_prompt = f"""당신은 도움이 되는 AI 어시스턴트입니다.
사용자의 질문에 정확하고 친절하게 답변해 주세요.
한국어로 대화합니다.

{self._get_current_time_info()}
"""

        # 도구가 있으면 사용 안내 추가
        if mcp_context.tools:
            tool_list = "\n".join([f"- {t.name}: {t.description}" for t in mcp_context.tools])
            base_prompt += f"""
사용 가능한 도구:
{tool_list}

중요: 메트릭이나 대시보드 정보를 요청받으면 반드시 도구를 사용하여 실제 데이터를 조회하세요.
추측하거나 가상의 데이터를 생성하지 마세요.
"""

        return base_prompt

    def _convert_to_langchain_messages(
        self,
        history: list[dict],
        system_prompt: str
    ) -> list:
        """대화 히스토리를 LangChain 메시지 형식으로 변환"""
        messages = []

        # 시스템 프롬프트 추가
        if system_prompt:
            messages.append(SystemMessage(content=system_prompt))

        # 대화 히스토리 변환
        for msg in history:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            if role == "user":
                messages.append(HumanMessage(content=content))
            elif role == "assistant":
                messages.append(AIMessage(content=content))

        return messages

    async def chat(
        self,
        message: str,
        history: Optional[list[dict]] = None
    ) -> str:
        """
        대화 처리

        Args:
            message: 사용자 메시지
            history: 이전 대화 히스토리 [{"role": "user"|"assistant", "content": "..."}]

        Returns:
            AI 응답 텍스트
        """
        # 비동기 초기화
        await self._ensure_initialized()

        history = history or []

        # MCP에서 컨텍스트 수집
        context: MCPContext = await self._mcp_manager.get_context()

        # 시스템 프롬프트 생성 (현재 시간 포함)
        system_prompt = self._build_system_prompt(context)

        # 히스토리에 현재 메시지 추가
        current_history = history + [{"role": "user", "content": message}]

        # LangChain 메시지로 변환
        messages = self._convert_to_langchain_messages(
            current_history,
            system_prompt
        )

        try:
            # LangGraph 워크플로우 실행
            result = await self._graph.ainvoke({"messages": messages})

            # 마지막 AI 메시지 추출
            ai_messages = [m for m in result["messages"] if isinstance(m, AIMessage)]
            if ai_messages:
                last_ai_msg = ai_messages[-1]
                # content가 문자열인지 확인
                if isinstance(last_ai_msg.content, str):
                    return last_ai_msg.content
                elif isinstance(last_ai_msg.content, list):
                    # 여러 컨텐츠 블록인 경우 텍스트만 추출
                    texts = [c.get("text", "") for c in last_ai_msg.content if isinstance(c, dict) and "text" in c]
                    return "\n".join(texts) if texts else str(last_ai_msg.content)
                return str(last_ai_msg.content)

            return "응답을 생성할 수 없습니다."

        except Exception as e:
            logger.error(f"Error during chat: {e}")
            raise


# 싱글톤 인스턴스
_agent: Optional[BedrockAgent] = None


async def get_bedrock_agent() -> BedrockAgent:
    """Bedrock 에이전트 싱글톤 반환 (비동기)"""
    global _agent
    if _agent is None:
        _agent = BedrockAgent()
    await _agent._ensure_initialized()
    return _agent
