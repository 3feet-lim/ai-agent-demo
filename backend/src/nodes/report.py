"""
report 노드 — 수집 데이터 정리 + LLM 리포트 생성
"""
import json
import re

from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage
from langgraph.graph import MessagesState
from loguru import logger

from .state import read_state_meta, read_state_meta_json
from ..prompts import build_report_prompt


class ReportSetupNode:
    """수집된 데이터를 정리하여 리포트 프롬프트를 구성."""

    def __init__(self, profile_resolver):
        self._profile_resolver = profile_resolver

    async def __call__(self, state: MessagesState) -> MessagesState:
        analyzed = read_state_meta_json(state, "ANALYZE_RESULT") or {}
        target_info = read_state_meta_json(state, "CURRENT_TARGETS") or {}
        execution_plan = read_state_meta_json(state, "EXECUTION_PLAN") or {}
        user_msg = read_state_meta(state, "ORIGINAL_MESSAGE") or ""

        category = analyzed.get("category", "general")
        intent = analyzed.get("intent", "")
        targets = target_info.get("targets", [])
        time_range = target_info.get("time_range") or analyzed.get("time_range")
        event_time = read_state_meta(state, "EVENT_TIME") or ""

        # account 정보 조회
        profile_name = target_info.get("profile", "")
        account = (self._profile_resolver.find_by_profile(profile_name)
                   if profile_name else None)
        if not account:
            account = self._profile_resolver.resolve_account(user_msg)
        account_info = f"{account.account_id} / {account.alias}" if account else ""

        # 대상 자원 정보 문자열
        target_resource_parts = []
        for t in targets:
            t_name = t.get("name", "")
            t_arn = t.get("arn", "")
            if t_arn:
                target_resource_parts.append(f"{t_name} (`{t_arn}`)")
            elif t_name:
                target_resource_parts.append(t_name)
        target_resources = ", ".join(target_resource_parts)

        report_prompt = build_report_prompt(
            intent=intent, category=category,
            account_info=account_info, target_resources=target_resources,
            event_time=event_time or time_range or "",
        )

        # step별 purpose 매핑
        step_purposes: dict[int, str] = {}
        for step in execution_plan.get("steps", []):
            step_purposes[step.get("step_id", 0)] = step.get("purpose", "")

        # 수집된 ToolMessage를 step별로 그룹화
        step_data: dict[int, list[str]] = {}
        failed_data: list[str] = []

        for m in state["messages"]:
            if not isinstance(m, ToolMessage):
                continue
            content = m.content if isinstance(m.content, str) else str(m.content)
            tool_name = m.name or "unknown"
            call_id = m.tool_call_id or ""

            step_id = 0
            step_match = re.match(r"step(\d+)_", call_id)
            if step_match:
                step_id = int(step_match.group(1))

            if content and len(content.strip()) >= 5 and not content.startswith("Sub-agent error:"):
                step_data.setdefault(step_id, []).append(f"#### [{tool_name}]\n{content}")
            else:
                failed_data.append(f"- {tool_name} (Step {step_id}): 수집 실패 또는 데이터 없음")

        data_sections: list[str] = []
        for step_id in sorted(step_data.keys()):
            purpose = step_purposes.get(step_id, f"Step {step_id}")
            section_header = f"### Step {step_id}: {purpose}"
            section_body = "\n\n".join(step_data[step_id])
            data_sections.append(f"{section_header}\n\n{section_body}")

        target_lines: list[str] = []
        if targets:
            for t in targets:
                target_lines.append(f"- {t.get('type', 'unknown')}: `{t.get('name', 'unknown')}`")

        data_section = "\n\n".join(data_sections) if data_sections else "(수집된 데이터 없음)"
        failed_section = "\n".join(failed_data) if failed_data else "(없음)"
        target_section = "\n".join(target_lines) if target_lines else "(전체 조회)"

        report_input = "\n".join([
            "## 사용자 요청", user_msg, "",
            "## 분석 대상", target_section, "",
            f"## 시간 범위: {time_range or '미지정'}", "",
            "## 수집된 데이터 (step별 구조화)", data_section, "",
            "## 수집 실패 항목", failed_section,
        ])

        return {"messages": [
            SystemMessage(content=report_prompt),
            HumanMessage(content=report_input),
        ]}


class ReportLLMNode:
    """리포트 프롬프트를 LLM에 전달하여 최종 리포트 생성."""

    def __init__(self, llm):
        self._llm = llm

    async def __call__(self, state: MessagesState) -> MessagesState:
        report_system = None
        report_human = None
        for m in reversed(state["messages"]):
            if isinstance(m, HumanMessage) and report_human is None:
                report_human = m
            elif isinstance(m, SystemMessage) and report_human is not None and report_system is None:
                if isinstance(m.content, str) and "report writer" in m.content.lower():
                    report_system = m
                    break

        if report_system and report_human:
            response = await self._llm.ainvoke([report_system, report_human])
        else:
            logger.warning("[Report] 리포트 프롬프트 쌍을 찾지 못함, 폴백 실행")
            response = await self._llm.ainvoke(state["messages"][-3:])

        return {"messages": [response]}
