"""
execute 노드 — sub-agent 순차/병렬 하이브리드 실행
"""
import asyncio
import json

from langchain_core.messages import SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.graph import MessagesState
from loguru import logger

from .state import read_state_meta, read_state_meta_json
from ..tools import MCPToolWrapper


# role → SubAgentTool name 매핑
ROLE_TO_TOOL_NAME = {
    "metric": "collect_metrics",
    "log": "collect_logs",
    "resource": "check_resources",
    "network": "investigate_network",
}


def build_target_constraint(targets: list[dict], profile: str) -> str:
    """확정된 타겟만 조회하도록 강제하는 제약 문자열 생성"""
    if not targets:
        return ""

    lines = [
        "## ⚠️ MANDATORY TARGET CONSTRAINT (절대 준수)",
        "아래 확정된 리소스만 조회할 것. 다른 리소스는 절대 조회 금지.",
        ""
    ]
    type_labels = {
        "cluster": "EKS 클러스터", "instance": "EC2 인스턴스",
        "db": "RDS 인스턴스", "function": "Lambda 함수",
    }
    for t in targets:
        t_type = t.get("type", "unknown")
        t_name = t.get("name", "unknown")
        pod_filter = t.get("pod_filter")
        label = type_labels.get(t_type, t_type)
        lines.append(f"- {label}: `{t_name}`")
        if pod_filter:
            lines.append(f"  → pod 필터: `{pod_filter}` (이 pod의 메트릭/로그만 조회)")
        if t_type == "cluster":
            lines.append(f'  → PromQL: cluster="{t_name}" 라벨 필터 필수 사용')

    lines.extend([
        "",
        f"AWS profile: {profile}",
        "위 리소스 외의 다른 클러스터/인스턴스/DB의 메트릭·로그·상태를 조회하면 안 됩니다.",
    ])
    return "\n".join(lines)


def _build_tool_by_role(main_tools: list[BaseTool]) -> dict[str, BaseTool]:
    """main_tools에서 role → SubAgentTool 매핑을 구성"""
    tool_by_role: dict[str, BaseTool] = {}
    for t in main_tools:
        for role, tool_name in ROLE_TO_TOOL_NAME.items():
            if t.name == tool_name:
                tool_by_role[role] = t
    return tool_by_role


def _setup_guardrails(targets: list[dict], all_tools: list[BaseTool]):
    """타겟 클러스터 가드레일 설정"""
    cluster_names = [
        t["name"] for t in targets
        if t.get("type") == "cluster" and t.get("name")
    ]
    logger.info(
        f"[Execute] 가드레일 설정: allowed_clusters={cluster_names}, "
        f"targets={json.dumps(targets, ensure_ascii=False)}"
    )
    for tool in all_tools:
        if isinstance(tool, MCPToolWrapper):
            tool.allowed_clusters = cluster_names if cluster_names else None


def _resolve_profile(analyzed: dict, resolved: dict, user_msg: str, profile_resolver) -> str:
    """프로필 결정 로직"""
    if resolved:
        return resolved.get("profile", "default")
    account_ref = analyzed.get("account_ref")
    if account_ref:
        return profile_resolver.resolve(account_ref)
    return profile_resolver.resolve(user_msg) if user_msg else profile_resolver.default_profile


class ExecuteStepsNode:
    """실행 계획의 steps를 순차/병렬 하이브리드로 실행."""

    def __init__(self, main_tools: list[BaseTool], all_tools: list[BaseTool],
                 profile_resolver):
        self._main_tools = main_tools
        self._all_tools = all_tools
        self._profile_resolver = profile_resolver

    async def __call__(self, state: MessagesState) -> MessagesState:
        execution_plan = read_state_meta_json(state, "EXECUTION_PLAN") or {}
        analyzed = read_state_meta_json(state, "ANALYZE_RESULT") or {}
        resolved = read_state_meta_json(state, "LOCKED_TARGETS") or {}
        user_msg = read_state_meta(state, "ORIGINAL_MESSAGE") or ""

        targets = resolved.get("targets", [])
        profile = _resolve_profile(analyzed, resolved, user_msg, self._profile_resolver)
        service_hint = resolved.get("service_hint") or analyzed.get("service_hint", "general")
        regions = resolved.get("regions") or analyzed.get("regions", [])

        _setup_guardrails(targets, self._all_tools)
        target_constraint = build_target_constraint(targets, profile)
        tool_by_role = _build_tool_by_role(self._main_tools)

        steps = execution_plan.get("steps", [])
        if not steps:
            logger.warning("[Execute] 실행 계획이 비어있음, 기본 resource 수집 실행")
            steps = [{
                "step_id": 0,
                "agents": analyzed.get("collection_types", ["resource"]) or ["resource"],
                "purpose": "데이터 수집",
                "task_template": analyzed.get("intent", "리소스 정보를 수집하세요."),
                "depends_on": None,
            }]

        accumulated_context: dict[int, str] = {}
        all_tool_messages: list[ToolMessage] = []

        pending = list(steps)
        while pending:
            ready = [
                s for s in pending
                if s.get("depends_on") is None
                or s["depends_on"] in accumulated_context
            ]
            if not ready:
                logger.error(f"[Execute] 의존성 교착: 남은 step={[s.get('step_id') for s in pending]}")
                break
            for s in ready:
                pending.remove(s)

            async def _run_step_agent(step: dict, agent_role: str, idx: int) -> tuple[int, int, str, str]:
                step_id = step.get("step_id", 0)
                task_template = step.get("task_template", "")
                depends_on = step.get("depends_on")

                prev_context = ""
                if depends_on is not None and depends_on in accumulated_context:
                    prev_context = accumulated_context[depends_on]

                matched_tool = tool_by_role.get(agent_role)
                if not matched_tool:
                    logger.warning(f"[Execute] agent role '{agent_role}'에 해당하는 도구 없음")
                    return (step_id, idx,
                            ROLE_TO_TOOL_NAME.get(agent_role, f"unknown_{agent_role}"),
                            f"agent '{agent_role}'에 해당하는 도구를 찾을 수 없습니다.")

                task_parts = []
                if target_constraint:
                    task_parts.append(target_constraint)
                if user_msg:
                    task_parts.append(f"## 사용자 원본 요청\n{user_msg}")
                if prev_context:
                    task_parts.append(f"## 이전 단계 수집 결과\n{prev_context}")
                task_parts.append(f"---\n{task_template}")

                try:
                    result = await matched_tool.ainvoke({"task": "\n\n".join(task_parts)})
                    return (step_id, idx, matched_tool.name, str(result))
                except Exception as e:
                    logger.error(f"[Execute] agent '{agent_role}' 실행 에러: {e}")
                    return (step_id, idx, matched_tool.name, f"Sub-agent error: {str(e)}")

            tasks = []
            task_meta = []
            for step in ready:
                step_id = step.get("step_id", 0)
                agents = step.get("agents", [])
                logger.info(
                    f"[Execute] Step {step_id}: agents={agents}, "
                    f"purpose={step.get('purpose', '')}, depends_on={step.get('depends_on')}"
                )
                for idx, role in enumerate(agents):
                    tasks.append(_run_step_agent(step, role, idx))
                    task_meta.append((step, role, idx))

            results = await asyncio.gather(*tasks, return_exceptions=True)

            step_results_map: dict[int, list[str]] = {}
            for i, res in enumerate(results):
                step, role, idx = task_meta[i]
                step_id = step.get("step_id", 0)

                if isinstance(res, Exception):
                    tool_name = ROLE_TO_TOOL_NAME.get(role, f"unknown_{role}")
                    result_text = f"Sub-agent error: {str(res)}"
                    logger.error(f"[Execute] Step {step_id}, agent '{role}' 예외: {res}")
                else:
                    step_id, _, tool_name, result_text = res

                step_results_map.setdefault(step_id, []).append(result_text)
                call_id = f"step{step_id}_{tool_name}_{idx}"
                all_tool_messages.append(ToolMessage(
                    content=result_text, name=tool_name, tool_call_id=call_id,
                ))

            for step in ready:
                step_id = step.get("step_id", 0)
                accumulated_context[step_id] = "\n\n".join(step_results_map.get(step_id, []))
                logger.info(f"[Execute] Step {step_id} 완료: 결과 총 {len(accumulated_context[step_id])}자")

        target_info = {
            "targets": targets, "profile": profile,
            "service_hint": service_hint, "regions": regions,
            "time_range": resolved.get("time_range") or analyzed.get("time_range"),
        }

        return {"messages": [
            SystemMessage(content="__PHASE__:collect"),
            SystemMessage(content=f"__CURRENT_TARGETS__:{json.dumps(target_info, ensure_ascii=False)}"),
            *all_tool_messages,
        ]}


class ExecuteAdditionalNode:
    """evaluate에서 결정된 추가 조사를 실행."""

    def __init__(self, main_tools: list[BaseTool], all_tools: list[BaseTool],
                 profile_resolver):
        self._main_tools = main_tools
        self._all_tools = all_tools
        self._profile_resolver = profile_resolver

    async def __call__(self, state: MessagesState) -> MessagesState:
        additional_plan = read_state_meta_json(state, "ADDITIONAL_PLAN") or {}
        analyzed = read_state_meta_json(state, "ANALYZE_RESULT") or {}
        resolved = read_state_meta_json(state, "LOCKED_TARGETS") or {}
        user_msg = read_state_meta(state, "ORIGINAL_MESSAGE") or ""

        targets = resolved.get("targets", [])
        profile = _resolve_profile(analyzed, resolved, user_msg, self._profile_resolver)

        _setup_guardrails(targets, self._all_tools)
        target_constraint = build_target_constraint(targets, profile)
        tool_by_role = _build_tool_by_role(self._main_tools)

        # 이전 수집 결과를 context로 전달
        prev_context_parts: list[str] = []
        for m in state["messages"]:
            if isinstance(m, ToolMessage):
                content = m.content if isinstance(m.content, str) else str(m.content)
                if content and len(content.strip()) >= 5 and not content.startswith("Sub-agent error:"):
                    truncated = content[:3000] if len(content) > 3000 else content
                    prev_context_parts.append(f"[{m.name}] {truncated}")
        prev_context = "\n\n".join(prev_context_parts[-5:])

        steps = additional_plan.get("steps", [])
        all_tool_messages: list[ToolMessage] = []

        for step in steps:
            step_id = step.get("step_id", 0)
            agents = step.get("agents", [])
            task_template = step.get("task_template", "")

            logger.info(
                f"[Execute-Additional] Step {step_id}: agents={agents}, "
                f"purpose={step.get('purpose', '')}"
            )

            async def _run_additional_agent(agent_role: str, idx: int) -> tuple[str, str]:
                matched_tool = tool_by_role.get(agent_role)
                if not matched_tool:
                    return (
                        ROLE_TO_TOOL_NAME.get(agent_role, f"unknown_{agent_role}"),
                        f"agent '{agent_role}'에 해당하는 도구를 찾을 수 없습니다.",
                    )

                task_parts = []
                if target_constraint:
                    task_parts.append(target_constraint)
                if user_msg:
                    task_parts.append(f"## 사용자 원본 요청\n{user_msg}")
                if prev_context:
                    task_parts.append(f"## 이전 수집 결과 (참고용)\n{prev_context}")
                task_parts.append(f"---\n## 추가 조사 지시\n{task_template}")

                try:
                    result = await matched_tool.ainvoke({"task": "\n\n".join(task_parts)})
                    return (matched_tool.name, str(result))
                except Exception as e:
                    logger.error(f"[Execute-Additional] agent '{agent_role}' 에러: {e}")
                    return (matched_tool.name, f"Sub-agent error: {str(e)}")

            tasks = [_run_additional_agent(role, idx) for idx, role in enumerate(agents)]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for idx, res in enumerate(results):
                if isinstance(res, Exception):
                    tool_name = ROLE_TO_TOOL_NAME.get(agents[idx], f"unknown_{agents[idx]}")
                    result_text = f"Sub-agent error: {str(res)}"
                else:
                    tool_name, result_text = res

                call_id = f"step{step_id}_{tool_name}_{idx}"
                all_tool_messages.append(ToolMessage(
                    content=result_text, name=tool_name, tool_call_id=call_id,
                ))

        return {"messages": [
            SystemMessage(content="__PHASE__:additional_collect"),
            *all_tool_messages,
        ]}
