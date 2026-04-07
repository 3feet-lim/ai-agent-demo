"""
direct_answer 노드 — 일반 질문 직접 응답 + 검증 실패 응답
"""
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from langgraph.graph import MessagesState
from loguru import logger

from .state import read_state_meta_json
from ..prompts import build_general_prompt


class DirectAnswerNode:
    """일반 질문에 대한 직접 응답 생성."""

    def __init__(self, llm):
        self._llm = llm

    async def __call__(self, state: MessagesState) -> MessagesState:
        general_prompt = build_general_prompt()

        context_messages = [SystemMessage(content=general_prompt)]
        for m in state["messages"]:
            if isinstance(m, HumanMessage):
                context_messages.append(m)
            elif isinstance(m, AIMessage):
                context_messages.append(m)

        # 최대 10개 메시지로 제한 (시스템 프롬프트 제외)
        if len(context_messages) > 11:
            context_messages = [context_messages[0]] + context_messages[-10:]

        response = await self._llm.ainvoke(context_messages)
        return {"messages": [response]}


class DirectAnswerValidationFailNode:
    """resolve에서 모든 리소스가 존재하지 않을 때 응답 생성."""

    _TYPE_LABELS = {
        "cluster": "EKS 클러스터", "instance": "EC2 인스턴스",
        "db": "RDS 인스턴스", "function": "Lambda 함수",
        "eks": "EKS 클러스터", "ec2": "EC2 인스턴스",
        "rds": "RDS 인스턴스", "lambda": "Lambda 함수",
    }

    async def __call__(self, state: MessagesState) -> MessagesState:
        resolved = read_state_meta_json(state, "LOCKED_TARGETS") or {}
        failed_list = resolved.get("failed", [])

        lines = ["요청하신 리소스를 찾을 수 없습니다.\n"]
        for item in failed_list:
            label = self._TYPE_LABELS.get(item.get("type", ""), item.get("type", "unknown"))
            lines.append(f"- {label}: `{item['name']}` — 존재하지 않음")
        lines.append("\n리소스 이름을 확인하고 다시 요청해주세요.")

        return {"messages": [AIMessage(content="\n".join(lines))]}
