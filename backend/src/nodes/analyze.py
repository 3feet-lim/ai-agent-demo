"""
analyze 노드 — 사용자 메시지 분석 (의도 분류 + 식별자 추출 + 행동 판단)
"""
import json
import re

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import MessagesState
from loguru import logger

from .state import read_state_meta_json, extract_text_from_content
from ..prompts import build_analyze_prompt
from ..time_utils import extract_alert_starts_at


class AnalyzeNode:
    """통합 분석 노드. LLM 1회 호출로 의도, 식별자, 필요 행동을 판단."""

    def __init__(self, llm, profile_resolver):
        self._llm = llm
        self._profile_resolver = profile_resolver

    async def __call__(self, state: MessagesState) -> MessagesState:
        user_msg = ""
        for m in reversed(state["messages"]):
            if isinstance(m, HumanMessage):
                user_msg = m.content if isinstance(m.content, str) else str(m.content)
                break

        if not user_msg:
            empty_result = {
                "intent": "", "category": "general",
                "identifiers": [], "identifier_types": {},
                "service_hint": "general", "account_ref": None,
                "regions": [], "time_range": None,
                "requires_validation": False,
                "requires_data_collection": False,
                "collection_types": [],
            }
            return {"messages": [
                SystemMessage(content="__ORIGINAL_MESSAGE__:"),
                SystemMessage(content=f"__ANALYZE_RESULT__:{json.dumps(empty_result)}"),
            ]}

        # 계정 alias 목록을 프롬프트에 주입
        known_aliases = self._profile_resolver.get_known_aliases()

        analyzed = {}
        try:
            analyze_prompt = build_analyze_prompt(known_aliases)
            response = await self._llm.ainvoke([
                SystemMessage(content=analyze_prompt),
                HumanMessage(content=user_msg),
            ])
            raw = extract_text_from_content(response.content).strip()
            json_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', raw, re.DOTALL)
            if json_match:
                raw = json_match.group(1)
            analyzed = json.loads(raw)
            if not isinstance(analyzed, dict):
                analyzed = {}
        except Exception as e:
            logger.warning(f"[Analyze] LLM 분석 실패: {e}")

        # 필수 필드 기본값 보장
        defaults = {
            "intent": "", "category": "general",
            "identifiers": [], "identifier_types": {},
            "service_hint": "general", "account_ref": None,
            "regions": [], "time_range": None,
            "requires_validation": False,
            "requires_data_collection": False,
            "collection_types": [],
        }
        for k, v in defaults.items():
            analyzed.setdefault(k, v)

        logger.info(
            f"[Analyze] intent={analyzed['intent']}, category={analyzed['category']}, "
            f"identifiers={analyzed['identifiers']}, "
            f"requires_validation={analyzed['requires_validation']}, "
            f"requires_data_collection={analyzed['requires_data_collection']}, "
            f"collection_types={analyzed['collection_types']}"
        )

        return {"messages": [
            SystemMessage(content=f"__ORIGINAL_MESSAGE__:{user_msg}"),
            SystemMessage(content=f"__ANALYZE_RESULT__:{json.dumps(analyzed, ensure_ascii=False)}"),
            SystemMessage(content=f"__EVENT_TIME__:{extract_alert_starts_at(user_msg) or ''}"),
        ]}


def route_after_analyze(state: MessagesState) -> str:
    """analyze 결과의 행동 플래그에 따라 다음 노드 결정."""
    analyzed = read_state_meta_json(state, "ANALYZE_RESULT")
    if not analyzed:
        return "direct_answer"
    if analyzed.get("requires_validation"):
        return "resolve"
    if analyzed.get("requires_data_collection"):
        return "plan"
    return "direct_answer"
