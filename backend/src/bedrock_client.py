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
from langgraph.errors import GraphRecursionError
from pydantic import BaseModel, Field, create_model

from .config import get_settings
from .mcp_manager import get_mcp_manager, MCPContext, MCPTool
from .conversation_store import get_conversation_store

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
            return f"Tool execution error: {str(e)}"

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
        description=mcp_tool.description or f"{mcp_tool.name} tool",
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
                    result = "Tool not found."
                    for tool in self._tools:
                        if tool.name == tool_name:
                            try:
                                result = await tool.ainvoke(tool_args)
                            except Exception as e:
                                result = f"Tool execution error: {str(e)}"
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
        utc_str = now_utc.strftime('%Y-%m-%d %H:%M:%S')
        kst_str = now_kst.strftime('%Y-%m-%d %H:%M:%S')
        return f"Current time - UTC: {utc_str} / KST: {kst_str}"

    def _build_system_prompt(self, mcp_context: MCPContext) -> str:
        """시스템 프롬프트 생성 (영어로 작성, 응답은 한국어 지시)"""
        time_info = self._get_current_time_info()

        lines = [
            "You are a helpful AI assistant.",
            "IMPORTANT: Always respond to the user in Korean (한국어).",
            "",
            time_info,
            "",
            "## Infrastructure Query Procedure",
            "",
            "When the user asks about infrastructure status, server performance,",
            "incidents, or resource usage, you MUST follow these steps in order:",
            "",
            "### Step 1: Grafana Metrics (Highest Priority)",
            "- First, use Grafana tools to query dashboard/panel metrics.",
            "- Check key indicators: CPU, memory, network, disk, request count, response time.",
            "- Identify time windows with anomalies or spikes.",
            "",
            "### Step 2: CloudWatch Logs (Detailed Analysis)",
            "- If anomalies are found in Grafana metrics, use CloudWatch MCP tools",
            "  to query logs for the relevant time window.",
            "- Specify appropriate log groups and filter patterns to search for error/warning logs.",
            "- Find the root cause of metric anomalies from the logs.",
            "",
            "### Step 3: AWS CLI (Additional Investigation)",
            "- If the above steps do not provide sufficient information,",
            "  use AWS CLI tools for further investigation.",
            "- Examples:",
            "  - EC2 instance status",
            "  - ECS/EKS service and task status",
            "  - RDS instance status and events",
            "  - ALB/NLB target group health checks",
            "  - Auto Scaling activity history",
            "",
            "### Response Guidelines",
            "- Summarize the current state by combining information from each step.",
            "- If anomalies are found, provide root cause analysis and recommended actions.",
            "- Present numerical data concretely and compare against normal ranges.",
            "- ALWAYS respond in Korean regardless of the language of tool outputs.",
        ]

        base_prompt = "\n".join(lines)

        # 도구가 있으면 사용 안내 추가
        if mcp_context.tools:
            tool_list = "\n".join(
                [f"- {t.name}: {t.description}" for t in mcp_context.tools]
            )
            base_prompt += (
                "\n\nAvailable tools:\n"
                + tool_list
                + "\n\nCRITICAL: When asked about metrics or dashboard information, "
                "you MUST use tools to query real data. "
                "Do NOT guess or fabricate data."
            )

        return base_prompt

    # 슬라이딩 윈도우 크기 (최근 N개 메시지는 원문 유지)
    WINDOW_SIZE = 20

    async def _summarize_messages(self, messages: list[dict]) -> str:
        """오래된 메시지들을 LLM으로 요약"""
        if not messages:
            return ""

        conversation_text = "\n".join(
            [f"{m['role']}: {m['content']}" for m in messages]
        )

        summary_prompt = [
            SystemMessage(content=(
                "You are a conversation summarizer. "
                "Summarize the following conversation concisely in Korean. "
                "Preserve key facts, decisions, tool results, and important context. "
                "Keep the summary under 500 words."
            )),
            HumanMessage(content=conversation_text),
        ]

        response = await self._llm.ainvoke(summary_prompt)

        if isinstance(response.content, str):
            return response.content
        return str(response.content)

    async def _build_hybrid_history(
        self,
        conversation_id: str,
        all_messages: list[dict],
    ) -> list[dict]:
        """하이브리드 메모리: 요약 + 슬라이딩 윈도우 결합"""
        total = len(all_messages)

        # 윈도우 이내면 전체 반환 (요약 불필요)
        if total <= self.WINDOW_SIZE:
            return all_messages

        # 윈도우 밖 메시지 = 요약 대상
        old_messages = all_messages[:-self.WINDOW_SIZE]
        recent_messages = all_messages[-self.WINDOW_SIZE:]

        # 기존 요약 확인
        store = await get_conversation_store()
        existing = await store.get_summary(conversation_id)

        old_count = len(old_messages)

        # 요약이 없거나 새로 요약할 메시지가 있으면 갱신
        if not existing or existing["summarized_until"] < old_count:
            # 기존 요약이 있으면 그 위에 추가 메시지만 요약
            if existing and existing["summarized_until"] > 0:
                new_portion = old_messages[existing["summarized_until"]:]
                combined_text = (
                    f"기존 요약:\n{existing['summary']}\n\n"
                    f"추가 대화:\n"
                    + "\n".join([f"{m['role']}: {m['content']}" for m in new_portion])
                )
                summary_messages = [
                    SystemMessage(content=(
                        "You are a conversation summarizer. "
                        "Merge the existing summary with the new conversation into one concise summary in Korean. "
                        "Preserve key facts, decisions, tool results, and important context. "
                        "Keep the summary under 500 words."
                    )),
                    HumanMessage(content=combined_text),
                ]
                response = await self._llm.ainvoke(summary_messages)
                summary = response.content if isinstance(response.content, str) else str(response.content)
            else:
                summary = await self._summarize_messages(old_messages)

            await store.save_summary(conversation_id, summary, old_count)
            logger.info(f"대화 요약 갱신: conversation_id={conversation_id}, summarized_until={old_count}")
        else:
            summary = existing["summary"]

        # 요약을 system 메시지로 앞에 붙이고 최근 메시지 결합
        hybrid = [
            {"role": "system", "content": f"[이전 대화 요약]\n{summary}"},
        ] + recent_messages

        return hybrid

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
            elif role == "system":
                # 하이브리드 메모리의 요약 메시지
                messages.append(SystemMessage(content=content))

        return messages

    async def chat(
        self,
        message: str,
        history: Optional[list[dict]] = None,
        conversation_id: Optional[str] = None,
    ) -> str:
        """
        대화 처리

        Args:
            message: 사용자 메시지
            history: 이전 대화 히스토리 [{"role": "user"|"assistant", "content": "..."}]
            conversation_id: 대화 ID (하이브리드 메모리 사용 시 필요)

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

        # 하이브리드 메모리 적용 (conversation_id가 있을 때)
        if conversation_id and len(current_history) > self.WINDOW_SIZE:
            current_history = await self._build_hybrid_history(
                conversation_id, current_history
            )

        # LangChain 메시지로 변환
        messages = self._convert_to_langchain_messages(
            current_history,
            system_prompt
        )

        try:
            # LangGraph 워크플로우 실행 (도구 호출 무한 반복 방지)
            result = await self._graph.ainvoke(
                {"messages": messages},
                {"recursion_limit": 30},
            )

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

        except GraphRecursionError:
            logger.warning("도구 호출 횟수 제한(recursion_limit=30)에 도달했습니다.")
            return (
                "죄송합니다. 요청을 처리하는 과정에서 도구 호출 횟수 제한에 도달했습니다. "
                "질문의 범위를 좁히거나, 더 구체적으로 질문해 주시면 더 나은 결과를 드릴 수 있습니다."
            )

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
