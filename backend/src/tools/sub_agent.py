"""
Sub-Agent 실행기 — ReAct 그래프 생성 + 실행 + Main Agent 도구 래핑
"""
import asyncio
import re
import time
from typing import Any

from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.graph import StateGraph, MessagesState, START, END
from langgraph.errors import GraphRecursionError
from loguru import logger


def build_sub_agent_graph(llm_with_tools, tools: list[BaseTool]) -> Any:
    """Sub-agent용 ReAct 그래프 생성"""

    async def agent_node(state: MessagesState) -> MessagesState:
        messages = state["messages"]
        response = await llm_with_tools.ainvoke(messages)
        return {"messages": [response]}

    async def tools_node(state: MessagesState) -> MessagesState:
        """MCP 도구를 병렬 실행"""
        messages = state["messages"]
        last_message = messages[-1]
        if not (hasattr(last_message, 'tool_calls') and last_message.tool_calls):
            return {"messages": []}

        tool_map = {tool.name: tool for tool in tools}

        async def _execute_one(tool_call):
            tool_name = tool_call["name"]
            tool_args = tool_call["args"]
            tool_id = tool_call["id"]
            logger.info(f"[Sub-Agent] Executing tool: {tool_name}")
            matched = tool_map.get(tool_name)
            if not matched:
                return ToolMessage(content="Tool not found.", tool_call_id=tool_id)
            try:
                result = await matched.ainvoke(tool_args)
            except Exception as e:
                result = f"Tool execution error: {str(e)}"
                logger.error(f"[Sub-Agent] Tool error: {e}")
            return ToolMessage(content=str(result), tool_call_id=tool_id)

        tool_messages = await asyncio.gather(
            *[_execute_one(tc) for tc in last_message.tool_calls]
        )
        return {"messages": list(tool_messages)}

    def should_continue(state: MessagesState) -> str:
        messages = state["messages"]
        last_message = messages[-1]
        if hasattr(last_message, 'tool_calls') and last_message.tool_calls:
            return "tools"
        return END

    graph = StateGraph(MessagesState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tools_node)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")
    return graph.compile()


async def run_sub_agent(
    graph,
    system_prompt: str,
    task: str,
    recursion_limit: int = 20,
) -> tuple[str, int]:
    """Sub-agent를 실행하고 (최종 응답, 도구 호출 수) 튜플을 반환"""
    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=task),
    ]
    tool_call_count = 0
    result = None
    try:
        result = await graph.ainvoke(
            {"messages": messages},
            {"recursion_limit": recursion_limit},
        )
        for m in result["messages"]:
            if isinstance(m, ToolMessage):
                tool_call_count += 1
        ai_messages = [m for m in result["messages"] if isinstance(m, AIMessage)]
        if ai_messages:
            last = ai_messages[-1]
            if isinstance(last.content, str):
                return last.content, tool_call_count
            elif isinstance(last.content, list):
                texts = [c.get("text", "") for c in last.content if isinstance(c, dict) and "text" in c]
                return ("\n".join(texts) if texts else str(last.content)), tool_call_count
            return str(last.content), tool_call_count
        return "Sub-agent가 응답을 생성하지 못했습니다.", tool_call_count
    except GraphRecursionError:
        logger.warning(f"[Sub-Agent] 도구 호출 횟수 제한 도달 (limit={recursion_limit})")
        if result:
            for m in result["messages"]:
                if isinstance(m, ToolMessage):
                    tool_call_count += 1
            try:
                ai_msgs = [m for m in result["messages"] if isinstance(m, AIMessage)]
                if ai_msgs and isinstance(ai_msgs[-1].content, str) and ai_msgs[-1].content.strip():
                    return ai_msgs[-1].content + "\n\n⚠️ 도구 호출 제한에 도달하여 수집을 중단했습니다.", tool_call_count
            except Exception:
                pass
        return "⚠️ 도구 호출 제한에 도달하여 수집을 중단했습니다.", tool_call_count
    except Exception as e:
        logger.error(f"[Sub-Agent] 예상치 못한 에러: {type(e).__name__}: {e}")
        return f"Sub-agent 실행 에러: {str(e)}", tool_call_count


class SubAgentTool(BaseTool):
    """Sub-agent를 Main Agent가 호출할 수 있는 도구로 래핑"""
    name: str
    description: str
    graph: Any
    system_prompt: str
    recursion_limit: int = 20

    class Config:
        arbitrary_types_allowed = True

    async def _arun(self, task: str) -> str:
        """Sub-agent 실행"""
        logger.info(f"[Main→Sub] {self.name} 호출: {task[:500]}")
        start = time.monotonic()
        result, tool_count = await run_sub_agent(
            self.graph, self.system_prompt, task, self.recursion_limit
        )
        # ===STATS=== 블록 제거
        result = re.sub(r'===STATS===.*?===END STATS===\s*', '', result, flags=re.DOTALL)
        max_len = 8000
        if len(result) > max_len:
            result = result[:max_len] + f"\n\n... (응답이 {len(result)}자로 길어 {max_len}자까지만 전달)"
        elapsed = time.monotonic() - start
        logger.info(
            f"[Main→Sub] {self.name} 완료: {elapsed:.1f}s, "
            f"MCP 도구 호출 {tool_count}회, 응답 {len(result)}자"
        )
        logger.debug(f"[Main→Sub] {self.name} 응답 미리보기: {result[:300]}")
        return result

    def _run(self, task: str) -> str:
        raise NotImplementedError("Use async version")
