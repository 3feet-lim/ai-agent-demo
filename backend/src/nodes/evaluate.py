"""
evaluate 노드 — 수집 결과 평가 + 재계획 판단
"""
import json
import re

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import MessagesState
from loguru import logger

from .state import read_state_meta, read_state_meta_json, extract_text_from_content
from ..prompts import build_evaluate_prompt


def _summarize_tool_messages(state: MessagesState) -> tuple[str, str, list[dict]]:
    """state의 ToolMessage들을 요약하여 (수집 요약, 실패 요약, step별 이력) 반환."""
    execution_plan = read_state_meta_json(state, "EXECUTION_PLAN") or {}
    step_purposes: dict[int, str] = {}
    for step in execution_plan.get("steps", []):
        step_purposes[step.get("step_id", 0)] = step.get("purpose", "")

    step_data: dict[int, list[str]] = {}
    step_agents: dict[int, set] = {}
    failed_items: list[str] = []

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

        step_agents.setdefault(step_id, set()).add(tool_name)

        if content and len(content.strip()) >= 5 and not content.startswith("Sub-agent error:"):
            truncated = content[:2000] if len(content) > 2000 else content
            step_data.setdefault(step_id, []).append(f"[{tool_name}] {truncated}")
        else:
            failed_items.append(f"- {tool_name} (Step {step_id}): 수집 실패 또는 데이터 없음")

    collected_parts: list[str] = []
    for step_id in sorted(step_data.keys()):
        purpose = step_purposes.get(step_id, f"Step {step_id}")
        body = "\n".join(step_data[step_id])
        collected_parts.append(f"### Step {step_id}: {purpose}\n{body}")
    collected_summary = "\n\n".join(collected_parts) if collected_parts else "(수집된 데이터 없음)"
    failed_summary = "\n".join(failed_items) if failed_items else ""

    history: list[dict] = []
    for step_id in sorted(set(list(step_data.keys()) + list(step_agents.keys()))):
        agents = list(step_agents.get(step_id, set()))
        purpose = step_purposes.get(step_id, "")
        findings = ""
        if step_id in step_data:
            finding_parts = [entry[:200] for entry in step_data[step_id]]
            findings = " | ".join(finding_parts)
        history.append({
            "round": step_id + 1,
            "agents": agents,
            "purpose": purpose,
            "findings": findings[:500] if findings else "데이터 없음",
        })

    return collected_summary, failed_summary, history


class EvaluateNode:
    """수집 결과를 평가하여 추가 조사 필요 여부를 판단."""

    def __init__(self, llm):
        self._llm = llm

    async def __call__(self, state: MessagesState) -> MessagesState:
        analyzed = read_state_meta_json(state, "ANALYZE_RESULT") or {}
        resolved = read_state_meta_json(state, "LOCKED_TARGETS") or {}
        user_msg = read_state_meta(state, "ORIGINAL_MESSAGE") or ""

        intent = analyzed.get("intent", "")
        category = analyzed.get("category", "general")
        targets = resolved.get("targets", [])

        # 일반 조회는 평가 없이 바로 report
        non_incident_categories = {"resource_lookup", "status_inquiry", "general"}
        if category in non_incident_categories and not any(
            kw in intent for kw in ["장애", "분석", "에러", "알람", "alert", "incident", "troubleshoot"]
        ):
            logger.info(f"[Evaluate] category={category} → 평가 스킵, 바로 report")
            return {"messages": [
                SystemMessage(content="__EVALUATE_RESULT__:sufficient"),
            ]}

        # 이전 evaluate 이력 읽기
        prev_history_raw = read_state_meta(state, "INVESTIGATION_HISTORY")
        prev_history: list[dict] = []
        if prev_history_raw:
            try:
                prev_history = json.loads(prev_history_raw)
            except json.JSONDecodeError:
                pass

        collected_summary, failed_summary, current_history = _summarize_tool_messages(state)
        all_history = prev_history + current_history

        evaluate_prompt = build_evaluate_prompt(
            user_msg=user_msg, intent=intent, category=category,
            targets=targets, collected_summary=collected_summary,
            failed_summary=failed_summary, executed_history=all_history,
        )

        try:
            response = await self._llm.ainvoke([
                SystemMessage(content=evaluate_prompt),
                HumanMessage(content="위 수집 결과를 평가해주세요."),
            ])
            raw = extract_text_from_content(response.content).strip()
            json_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', raw, re.DOTALL)
            if json_match:
                raw = json_match.group(1)
            eval_result = json.loads(raw)
        except Exception as e:
            logger.warning(f"[Evaluate] LLM 평가 실패: {e}, 바로 report 진행")
            return {"messages": [
                SystemMessage(content="__EVALUATE_RESULT__:sufficient"),
            ]}

        sufficient = eval_result.get("sufficient", True)
        reasoning = eval_result.get("reasoning", "")
        additional = eval_result.get("additional_investigation")

        logger.info(f"[Evaluate] sufficient={sufficient}, reasoning={reasoning[:200]}")

        if sufficient or not additional:
            return {"messages": [
                SystemMessage(content="__EVALUATE_RESULT__:sufficient"),
                SystemMessage(content=f"__INVESTIGATION_HISTORY__:{json.dumps(all_history, ensure_ascii=False)}"),
            ]}

        # ── 추가 조사 필요: 유효성 검증 ──
        new_agents = additional.get("agents", [])
        new_purpose = additional.get("purpose", "")
        new_task_hint = additional.get("task_hint", "")
        evidence = additional.get("evidence", "")

        if not evidence or len(evidence.strip()) < 10:
            logger.info("[Evaluate] evidence 부족 → 추가 조사 거부, report 진행")
            return {"messages": [
                SystemMessage(content="__EVALUATE_RESULT__:sufficient"),
                SystemMessage(content=f"__INVESTIGATION_HISTORY__:{json.dumps(all_history, ensure_ascii=False)}"),
            ]}

        prev_agent_purposes = set()
        for h in all_history:
            for agent in h.get("agents", []):
                prev_agent_purposes.add(agent)

        all_agents_used = all(a in prev_agent_purposes for a in new_agents)
        if all_agents_used and len(all_history) >= 2:
            logger.info(
                f"[Evaluate] 모든 agent({new_agents})가 이미 사용됨 + 2회 이상 수행 → 추가 조사 거부"
            )
            return {"messages": [
                SystemMessage(content="__EVALUATE_RESULT__:sufficient"),
                SystemMessage(content=f"__INVESTIGATION_HISTORY__:{json.dumps(all_history, ensure_ascii=False)}"),
            ]}

        logger.info(
            f"[Evaluate] 추가 조사 결정: agents={new_agents}, "
            f"purpose={new_purpose}, evidence={evidence[:200]}"
        )

        existing_plan = read_state_meta_json(state, "EXECUTION_PLAN") or {}
        existing_steps = existing_plan.get("steps", [])
        next_step_id = max((s.get("step_id", 0) for s in existing_steps), default=-1) + 1

        additional_plan = {
            "steps": [{
                "step_id": next_step_id,
                "agents": new_agents,
                "purpose": new_purpose,
                "task_template": new_task_hint if new_task_hint else new_purpose,
                "depends_on": None,
            }]
        }

        return {"messages": [
            SystemMessage(content="__EVALUATE_RESULT__:replan"),
            SystemMessage(content=f"__ADDITIONAL_PLAN__:{json.dumps(additional_plan, ensure_ascii=False)}"),
            SystemMessage(content=f"__INVESTIGATION_HISTORY__:{json.dumps(all_history, ensure_ascii=False)}"),
        ]}


def route_after_evaluate(state: MessagesState) -> str:
    """evaluate 결과에 따라 report 또는 추가 execute로 라우팅"""
    eval_result = read_state_meta(state, "EVALUATE_RESULT")
    if eval_result == "replan":
        return "execute_additional"
    return "report_setup"
