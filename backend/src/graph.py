"""
Main Agent용 LangGraph 워크플로우

analyze → route → resolve → plan → execute_steps → report 파이프라인과
direct_answer 분기를 포함하는 메인 그래프를 구성합니다.
"""
import asyncio
import json
import re
from typing import Optional, Any

from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.graph import StateGraph, MessagesState, START, END
from loguru import logger

from .tools import MCPToolWrapper
from .prompts import (
    build_analyze_prompt,
    build_plan_prompt,
    build_report_prompt,
    build_resolve_prompt,
    build_general_prompt,
)


# ── State 메타데이터 헬퍼 ──────────────────────────────────────

def read_state_meta(state: MessagesState, key: str) -> Optional[str]:
    """state에서 __{KEY}__:value 형태의 SystemMessage 메타데이터를 읽는 유틸.

    가장 마지막에 저장된 값을 반환합니다.
    찾지 못하면 None을 반환합니다.
    """
    prefix = f"__{key}__:"
    for m in reversed(state["messages"]):
        if isinstance(m, SystemMessage) and isinstance(m.content, str):
            if m.content.startswith(prefix):
                return m.content[len(prefix):]
    return None


def read_state_meta_json(state: MessagesState, key: str) -> Optional[dict]:
    """state에서 __{KEY}__:JSON 형태의 메타데이터를 파싱하여 dict로 반환.

    파싱 실패 시 None을 반환합니다.
    """
    raw = read_state_meta(state, key)
    if raw is None:
        return None
    try:
        result = json.loads(raw)
        return result if isinstance(result, dict) else None
    except json.JSONDecodeError:
        return None


def _extract_text_from_content(content) -> str:
    """LLM 응답의 content에서 텍스트를 추출하는 유틸.

    content가 str이면 그대로 반환.
    content가 list (content blocks 형태)이면 text 블록들을 결합하여 반환.
    예: [{'type': 'text', 'text': '...'}, ...] → 텍스트 결합
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return str(content)


def build_main_graph(
    main_llm,
    main_llm_with_tools,
    main_tools: list[BaseTool],
    profile_resolver,
    default_region: str,
    all_tools: list[BaseTool],
) -> Any:
    """Main Agent용 LangGraph 워크플로우 구성

    analyze → route
      ├─ general → direct_answer → END
      ├─ requires_validation → resolve → route
      │   ├─ validation_fail → END
      │   └─ plan → execute_steps → report → END
      └─ data_collection_only → plan → execute_steps → report → END

    Args:
        main_llm: 도구 없는 Main LLM (리포트/분류용)
        main_llm_with_tools: sub-agent 도구가 바인딩된 Main LLM
        main_tools: SubAgentTool 리스트
        profile_resolver: AccountProfileResolver 인스턴스
        default_region: 기본 AWS 리전
        all_tools: 모든 MCP 도구 리스트 (가드레일 설정용)
    """

    # ── Phase 0: 통합 분석 (의도 분류 + 식별자 추출 + 행동 판단) ──
    async def analyze_node(state: MessagesState) -> MessagesState:
        """사용자 메시지를 분석하여 의도, 식별자, 필요 행동을 한 번에 판단.

        기존 extract_node + classify_node를 통합. LLM 1회 호출.
        결과를 __ANALYZE_RESULT__ SystemMessage로 state에 저장.
        원본 메시지를 __ORIGINAL_MESSAGE__로 불변 저장.
        """
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
                SystemMessage(content=f"__ORIGINAL_MESSAGE__:"),
                SystemMessage(content=f"__ANALYZE_RESULT__:{json.dumps(empty_result)}"),
            ]}

        # 계정 alias 목록을 프롬프트에 주입
        known_aliases = profile_resolver.get_known_aliases()

        analyzed = {}
        try:
            analyze_prompt = build_analyze_prompt(known_aliases)
            response = await main_llm.ainvoke([
                SystemMessage(content=analyze_prompt),
                HumanMessage(content=user_msg),
            ])
            raw = _extract_text_from_content(response.content).strip()
            # ```json ... ``` 블록이 있으면 추출
            json_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', raw, re.DOTALL)
            if json_match:
                raw = json_match.group(1)
            analyzed = json.loads(raw)
            if not isinstance(analyzed, dict):
                analyzed = {}
        except Exception as e:
            logger.warning(f"[Analyze] LLM 분석 실패: {e}")

        # 필수 필드 기본값 보장
        analyzed.setdefault("intent", "")
        analyzed.setdefault("category", "general")
        analyzed.setdefault("identifiers", [])
        analyzed.setdefault("identifier_types", {})
        analyzed.setdefault("service_hint", "general")
        analyzed.setdefault("account_ref", None)
        analyzed.setdefault("regions", [])
        analyzed.setdefault("time_range", None)
        analyzed.setdefault("requires_validation", False)
        analyzed.setdefault("requires_data_collection", False)
        analyzed.setdefault("collection_types", [])

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


    # ── Phase 1: 리소스 해석 (resolve) — LLM이 sub-agent로 검증 ──
    async def resolve_node(state: MessagesState) -> MessagesState:
        """analyze 결과를 LLM + sub-agent로 검증하여 확정된 targets를 생성."""
        analyzed = read_state_meta_json(state, "ANALYZE_RESULT") or {}
        user_msg = read_state_meta(state, "ORIGINAL_MESSAGE") or ""

        identifiers = analyzed.get("identifiers", [])
        service_hint = analyzed.get("service_hint", "general")
        account_ref = analyzed.get("account_ref")
        regions = analyzed.get("regions", [])
        time_range = analyzed.get("time_range")

        # 프로필 결정
        if account_ref:
            profile = profile_resolver.resolve(account_ref)
        else:
            profile = profile_resolver.resolve(user_msg) if user_msg else profile_resolver.default_profile

        region = regions[0] if regions else default_region

        logger.info(
            f"[Resolve] identifiers={identifiers}, service_hint={service_hint}, "
            f"account_ref={account_ref}, profile={profile}, region={region}"
        )

        # 식별자가 없으면 → 전체 현황 조회 (검증 불필요)
        if not identifiers:
            logger.info("[Resolve] 식별자 없음 → 전체 현황 조회 모드")
            resolved = {
                "profile": profile,
                "targets": [],
                "failed": [],
                "service_hint": service_hint,
                "regions": regions,
                "time_range": time_range,
            }
            return {"messages": [
                SystemMessage(content=f"__LOCKED_TARGETS__:{json.dumps(resolved, ensure_ascii=False)}")
            ]}

        # LLM + sub-agent로 검증 실행
        resolve_prompt = build_resolve_prompt(analyzed, profile, region)

        resolve_messages = [
            SystemMessage(content=resolve_prompt),
            HumanMessage(content=f"## 사용자 원본 메시지\n{user_msg}\n\n위 식별자들을 검증해주세요."),
        ]

        try:
            result = await main_llm_with_tools.ainvoke(resolve_messages)

            tool_map = {tool.name: tool for tool in main_tools}
            loop_count = 0
            max_loops = 6

            while hasattr(result, 'tool_calls') and result.tool_calls and loop_count < max_loops:
                loop_count += 1
                tool_messages = []
                for tc in result.tool_calls:
                    tool_name = tc["name"]
                    tool_args = tc["args"]
                    tool_id = tc["id"]
                    logger.info(
                        f"[Resolve] Loop {loop_count} → tool_call: {tool_name}, "
                        f"args={json.dumps(tool_args, ensure_ascii=False)[:500]}"
                    )
                    matched = tool_map.get(tool_name)
                    if matched:
                        try:
                            tool_result = await matched.ainvoke(tool_args)
                            tool_result_str = str(tool_result)
                            logger.info(
                                f"[Resolve] Loop {loop_count} ← tool_result: {tool_name}, "
                                f"len={len(tool_result_str)}, preview={tool_result_str[:300]}"
                            )
                            tool_messages.append(ToolMessage(
                                content=tool_result_str, name=tool_name, tool_call_id=tool_id
                            ))
                        except Exception as e:
                            logger.error(f"[Resolve] Loop {loop_count} tool error: {tool_name}: {e}")
                            tool_messages.append(ToolMessage(
                                content=f"Error: {e}", name=tool_name, tool_call_id=tool_id
                            ))
                    else:
                        tool_messages.append(ToolMessage(
                            content="Tool not found.", name=tool_name, tool_call_id=tool_id
                        ))

                resolve_messages.append(result)
                resolve_messages.extend(tool_messages)
                result = await main_llm_with_tools.ainvoke(resolve_messages)

            # 최종 응답에서 JSON 파싱
            response_text = _extract_text_from_content(result.content)
            json_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', response_text, re.DOTALL)
            if json_match:
                resolve_result = json.loads(json_match.group(1))
            else:
                json_match2 = re.search(r'\{[^{}]*"targets"[^{}]*\}', response_text, re.DOTALL)
                if json_match2:
                    resolve_result = json.loads(json_match2.group(0))
                else:
                    logger.warning(f"[Resolve] LLM 응답에서 JSON 파싱 실패: {response_text[:200]}")
                    resolve_result = {"targets": [], "failed": []}

        except Exception as e:
            logger.error(f"[Resolve] LLM 검증 실패: {e}")
            identifier_types = analyzed.get("identifier_types", {})
            resolve_result = {
                "targets": [],
                "failed": [
                    {"name": ident, "type": identifier_types.get(ident, "unknown"), "detail": f"resolve 예외: {e}"}
                    for ident in identifiers
                ] if identifiers else [],
            }

        targets = resolve_result.get("targets", [])
        failed = resolve_result.get("failed", [])

        logger.info(
            f"[Resolve] LLM 최종 응답 파싱 결과: "
            f"targets={json.dumps(targets, ensure_ascii=False)}, "
            f"failed={json.dumps(failed, ensure_ascii=False)}"
        )

        # 식별자가 있었는데 targets가 비어있으면 → 강제 실패 처리 (할루시네이션 방지)
        if identifiers and not targets and not failed:
            identifier_types = analyzed.get("identifier_types", {})
            failed = [
                {"name": ident, "type": identifier_types.get(ident, "unknown"), "detail": "resolve에서 확인 실패"}
                for ident in identifiers
            ]
            logger.warning(f"[Resolve] 식별자 {len(identifiers)}건 있었으나 targets/failed 모두 비어있음 → 강제 실패 처리")

        logger.info(f"[Resolve] 확정 {len(targets)}건, 실패 {len(failed)}건")

        resolved = {
            "profile": profile,
            "targets": targets,
            "failed": failed,
            "service_hint": service_hint,
            "regions": regions,
            "time_range": time_range,
        }
        return {"messages": [
            SystemMessage(content=f"__LOCKED_TARGETS__:{json.dumps(resolved, ensure_ascii=False)}")
        ]}

    def route_after_resolve(state: MessagesState) -> str:
        """resolve 결과에 따라 plan 또는 validation_fail로 라우팅"""
        resolved = read_state_meta_json(state, "LOCKED_TARGETS")
        if not resolved:
            return "plan"
        targets = resolved.get("targets", [])
        failed = resolved.get("failed", [])
        if not targets and failed:
            return "direct_answer_validation_fail"
        return "plan"


    # ── Phase 1.5: 실행 계획 수립 (plan) — LLM이 sub-agent 실행 순서 결정 ──
    async def plan_node(state: MessagesState) -> MessagesState:
        """analyze/resolve 결과를 기반으로 sub-agent 실행 계획을 수립.

        LLM 1회 호출로 어떤 sub-agent를 어떤 순서로 실행할지 결정.
        결과를 __EXECUTION_PLAN__ SystemMessage로 state에 저장.
        """
        analyzed = read_state_meta_json(state, "ANALYZE_RESULT") or {}
        resolved = read_state_meta_json(state, "LOCKED_TARGETS")

        plan_prompt = build_plan_prompt(analyzed, resolved)

        try:
            response = await main_llm.ainvoke([
                SystemMessage(content=plan_prompt),
                HumanMessage(content="위 컨텍스트를 기반으로 실행 계획을 JSON으로 작성하세요."),
            ])
            raw = _extract_text_from_content(response.content).strip()
            # ```json ... ``` 블록이 있으면 추출
            json_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', raw, re.DOTALL)
            if json_match:
                raw = json_match.group(1)
            plan = json.loads(raw)
            if not isinstance(plan, dict) or "steps" not in plan:
                raise ValueError("plan에 steps 필드가 없음")
        except Exception as e:
            logger.warning(f"[Plan] LLM 계획 수립 실패, 기본 계획 사용: {e}")
            # 폴백: collection_types 기반으로 단일 step 기본 계획 생성
            collection_types = analyzed.get("collection_types", ["resource"])
            if not collection_types:
                collection_types = ["resource"]
            plan = {
                "steps": [{
                    "step_id": 0,
                    "agents": collection_types,
                    "purpose": "데이터 수집 (기본 계획)",
                    "task_template": analyzed.get("intent", "리소스 정보를 수집하세요."),
                    "depends_on": None,
                }]
            }

        steps = plan.get("steps", [])
        logger.info(
            f"[Plan] 실행 계획 수립 완료: {len(steps)}개 step, "
            f"agents={[s.get('agents', []) for s in steps]}"
        )

        return {"messages": [
            SystemMessage(content=f"__EXECUTION_PLAN__:{json.dumps(plan, ensure_ascii=False)}")
        ]}

    async def direct_answer_validation_fail_node(state: MessagesState) -> MessagesState:
        """resolve에서 모든 리소스가 존재하지 않을 때 응답 생성"""
        resolved = read_state_meta_json(state, "LOCKED_TARGETS") or {}
        failed_list = resolved.get("failed", [])

        type_labels = {
            "cluster": "EKS 클러스터", "instance": "EC2 인스턴스",
            "db": "RDS 인스턴스", "function": "Lambda 함수",
            "eks": "EKS 클러스터", "ec2": "EC2 인스턴스",
            "rds": "RDS 인스턴스", "lambda": "Lambda 함수",
        }
        lines = ["요청하신 리소스를 찾을 수 없습니다.\n"]
        for item in failed_list:
            label = type_labels.get(item.get("type", ""), item.get("type", "unknown"))
            lines.append(f"- {label}: `{item['name']}` — 존재하지 않음")
        lines.append("\n리소스 이름을 확인하고 다시 요청해주세요.")

        return {"messages": [
            AIMessage(content="\n".join(lines))
        ]}


    # ── Phase 2: 순차/병렬 하이브리드 실행기 ──
    # role → SubAgentTool name 매핑
    _ROLE_TO_TOOL_NAME = {
        "metric": "collect_metrics",
        "log": "collect_logs",
        "resource": "check_resources",
        "network": "investigate_network",
    }

    def _build_target_constraint(targets: list[dict], profile: str) -> str:
        """확정된 타겟만 조회하도록 강제하는 제약 문자열 생성"""
        if not targets:
            return ""

        lines = [
            "## ⚠️ MANDATORY TARGET CONSTRAINT (절대 준수)",
            "아래 확정된 리소스만 조회할 것. 다른 리소스는 절대 조회 금지.",
            ""
        ]
        type_labels = {
            "cluster": "EKS 클러스터",
            "instance": "EC2 인스턴스",
            "db": "RDS 인스턴스",
            "function": "Lambda 함수",
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

    async def execute_steps_node(state: MessagesState) -> MessagesState:
        """실행 계획(plan)의 steps를 순차 실행.

        각 step 내의 agents는 병렬 실행.
        이전 step의 결과를 다음 step의 context로 전달.
        결과는 ToolMessage 리스트로 반환하여 SSE 이벤트 호환성 유지.
        """
        # ── state에서 필요한 정보 읽기 (헬퍼 함수 사용) ──
        execution_plan = read_state_meta_json(state, "EXECUTION_PLAN") or {}
        analyzed = read_state_meta_json(state, "ANALYZE_RESULT") or {}
        resolved = read_state_meta_json(state, "LOCKED_TARGETS") or {}
        user_msg = read_state_meta(state, "ORIGINAL_MESSAGE") or ""

        # 타겟/프로필 정보 결정
        targets = resolved.get("targets", [])
        profile = resolved.get("profile", "default")
        service_hint = resolved.get("service_hint") or analyzed.get("service_hint", "general")
        regions = resolved.get("regions") or analyzed.get("regions", [])

        # resolve를 거치지 않은 경우 프로필 결정
        if not resolved:
            account_ref = analyzed.get("account_ref")
            if account_ref:
                profile = profile_resolver.resolve(account_ref)
            else:
                profile = profile_resolver.resolve(user_msg) if user_msg else profile_resolver.default_profile

        # 타겟 가드레일 세팅
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

        target_constraint = _build_target_constraint(targets, profile)

        # sub-agent 도구 맵 (role → SubAgentTool)
        tool_by_role: dict[str, BaseTool] = {}
        for t in main_tools:
            for role, tool_name in _ROLE_TO_TOOL_NAME.items():
                if t.name == tool_name:
                    tool_by_role[role] = t

        # ── step별 순차 실행 ──
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

        accumulated_context: dict[int, str] = {}  # step_id → 결과 텍스트
        all_tool_messages: list[ToolMessage] = []

        for step in steps:
            step_id = step.get("step_id", 0)
            agents = step.get("agents", [])
            purpose = step.get("purpose", "")
            task_template = step.get("task_template", "")
            depends_on = step.get("depends_on")

            # 이전 step 결과를 context로 구성
            prev_context = ""
            if depends_on is not None and depends_on in accumulated_context:
                prev_context = accumulated_context[depends_on]

            logger.info(
                f"[Execute] Step {step_id}: agents={agents}, purpose={purpose}, "
                f"depends_on={depends_on}, prev_context_len={len(prev_context)}"
            )

            # 같은 step 내의 agents 병렬 실행
            async def _run_agent(agent_role: str) -> tuple[str, str]:
                """단일 agent 실행. (tool_name, 결과 텍스트) 반환."""
                matched_tool = tool_by_role.get(agent_role)
                if not matched_tool:
                    logger.warning(f"[Execute] agent role '{agent_role}'에 해당하는 도구 없음")
                    return (
                        _ROLE_TO_TOOL_NAME.get(agent_role, f"unknown_{agent_role}"),
                        f"agent '{agent_role}'에 해당하는 도구를 찾을 수 없습니다.",
                    )

                # task 구성: 제약 조건 + 이전 결과 + task_template
                task_parts = []
                if target_constraint:
                    task_parts.append(target_constraint)
                if user_msg:
                    task_parts.append(f"## 사용자 원본 요청\n{user_msg}")
                if prev_context:
                    task_parts.append(f"## 이전 단계 수집 결과\n{prev_context}")
                task_parts.append(f"---\n{task_template}")

                full_task = "\n\n".join(task_parts)

                try:
                    result = await matched_tool.ainvoke({"task": full_task})
                    return (matched_tool.name, str(result))
                except Exception as e:
                    logger.error(f"[Execute] agent '{agent_role}' 실행 에러: {e}")
                    return (matched_tool.name, f"Sub-agent error: {str(e)}")

            # 병렬 실행
            results = await asyncio.gather(
                *[_run_agent(role) for role in agents],
                return_exceptions=True,
            )

            # 결과 수집
            step_results = []
            for i, res in enumerate(results):
                if isinstance(res, Exception):
                    agent_role = agents[i] if i < len(agents) else "unknown"
                    tool_name = _ROLE_TO_TOOL_NAME.get(agent_role, f"unknown_{agent_role}")
                    result_text = f"Sub-agent error: {str(res)}"
                    logger.error(f"[Execute] Step {step_id}, agent '{agent_role}' 예외: {res}")
                else:
                    tool_name, result_text = res

                step_results.append(result_text)

                # ToolMessage로 변환 (SSE 이벤트 호환)
                call_id = f"step{step_id}_{tool_name}_{i}"
                all_tool_messages.append(ToolMessage(
                    content=result_text,
                    name=tool_name,
                    tool_call_id=call_id,
                ))

            # 이 step의 결과를 축적
            accumulated_context[step_id] = "\n\n".join(step_results)

            logger.info(
                f"[Execute] Step {step_id} 완료: {len(agents)}개 agent, "
                f"결과 총 {len(accumulated_context[step_id])}자"
            )

        # 타겟 정보를 state에 저장 (report_setup_node에서 사용)
        target_info = {
            "targets": targets,
            "profile": profile,
            "service_hint": service_hint,
            "regions": regions,
            "time_range": resolved.get("time_range") or analyzed.get("time_range"),
        }

        return {"messages": [
            SystemMessage(content="__PHASE__:collect"),
            SystemMessage(content=f"__CURRENT_TARGETS__:{json.dumps(target_info, ensure_ascii=False)}"),
            *all_tool_messages,
        ]}


    # ── Phase 3: 리포트 생성 ──

    async def report_setup_node(state: MessagesState) -> MessagesState:
        """수집된 데이터를 정리하여 리포트 프롬프트를 구성.

        헬퍼 함수를 사용하여 analyze 결과, 타겟 정보, 원본 메시지를 읽고
        step별 수집 결과를 구조화하여 리포트 입력으로 구성합니다.
        """
        analyzed = read_state_meta_json(state, "ANALYZE_RESULT") or {}
        target_info = read_state_meta_json(state, "CURRENT_TARGETS") or {}
        execution_plan = read_state_meta_json(state, "EXECUTION_PLAN") or {}
        user_msg = read_state_meta(state, "ORIGINAL_MESSAGE") or ""

        category = analyzed.get("category", "general")
        intent = analyzed.get("intent", "")
        targets = target_info.get("targets", [])
        time_range = target_info.get("time_range") or analyzed.get("time_range")

        # analyze의 intent/category를 직접 전달하여 리포트 형식 자동 결정
        report_prompt = build_report_prompt(intent=intent, category=category)

        # step별 purpose 매핑 구성 (실행 계획에서 step_id → purpose)
        step_purposes: dict[int, str] = {}
        for step in execution_plan.get("steps", []):
            step_purposes[step.get("step_id", 0)] = step.get("purpose", "")

        # 수집된 ToolMessage를 step별로 그룹화
        # ToolMessage의 tool_call_id 형식: "step{N}_{tool_name}_{idx}"
        step_data: dict[int, list[str]] = {}
        failed_data: list[str] = []

        for m in state["messages"]:
            if not isinstance(m, ToolMessage):
                continue
            content = m.content if isinstance(m.content, str) else str(m.content)
            tool_name = m.name or "unknown"
            call_id = m.tool_call_id or ""

            # step_id 추출 시도
            step_id = 0
            step_match = re.match(r"step(\d+)_", call_id)
            if step_match:
                step_id = int(step_match.group(1))

            if content and len(content.strip()) >= 5 and not content.startswith("Sub-agent error:"):
                step_data.setdefault(step_id, []).append(
                    f"#### [{tool_name}]\n{content}"
                )
            else:
                failed_data.append(f"- {tool_name} (Step {step_id}): 수집 실패 또는 데이터 없음")

        # step별 데이터를 구조화된 섹션으로 구성
        data_sections: list[str] = []
        for step_id in sorted(step_data.keys()):
            purpose = step_purposes.get(step_id, f"Step {step_id}")
            section_header = f"### Step {step_id}: {purpose}"
            section_body = "\n\n".join(step_data[step_id])
            data_sections.append(f"{section_header}\n\n{section_body}")

        # 타겟 정보 문자열
        target_lines: list[str] = []
        if targets:
            for t in targets:
                t_type = t.get("type", "unknown")
                t_name = t.get("name", "unknown")
                target_lines.append(f"- {t_type}: `{t_name}`")

        # 리포트 입력 구성
        data_section = "\n\n".join(data_sections) if data_sections else "(수집된 데이터 없음)"
        failed_section = "\n".join(failed_data) if failed_data else "(없음)"
        target_section = "\n".join(target_lines) if target_lines else "(전체 조회)"

        report_input = "\n".join([
            "## 사용자 요청",
            user_msg,
            "",
            "## 분석 대상",
            target_section,
            "",
            f"## 시간 범위: {time_range or '미지정'}",
            "",
            "## 수집된 데이터 (step별 구조화)",
            data_section,
            "",
            "## 수집 실패 항목",
            failed_section,
        ])

        return {"messages": [
            SystemMessage(content=report_prompt),
            HumanMessage(content=report_input),
        ]}

    async def report_llm_node(state: MessagesState) -> MessagesState:
        """리포트 프롬프트를 LLM에 전달하여 최종 리포트 생성.

        report_setup_node가 구성한 SystemMessage + HumanMessage를
        그대로 LLM에 전달합니다.
        """
        # state의 마지막 SystemMessage + HumanMessage 쌍을 찾아 LLM에 전달
        report_system = None
        report_human = None
        for m in reversed(state["messages"]):
            if isinstance(m, HumanMessage) and report_human is None:
                report_human = m
            elif isinstance(m, SystemMessage) and report_human is not None and report_system is None:
                # 리포트 프롬프트인지 확인 (build_report_prompt 결과)
                if isinstance(m.content, str) and "report writer" in m.content.lower():
                    report_system = m
                    break

        if report_system and report_human:
            response = await main_llm.ainvoke([report_system, report_human])
        else:
            # 폴백: 마지막 메시지들로 리포트 생성 시도
            logger.warning("[Report] 리포트 프롬프트 쌍을 찾지 못함, 폴백 실행")
            response = await main_llm.ainvoke(state["messages"][-3:])

        return {"messages": [response]}


    # ── 일반 질문 직접 응답 ──

    async def direct_answer_node(state: MessagesState) -> MessagesState:
        """일반 질문에 대한 직접 응답 생성.

        대화 히스토리 컨텍스트가 필요하므로 HumanMessage 탐색을 유지합니다.
        """
        general_prompt = build_general_prompt()

        # 대화 히스토리에서 최근 메시지들을 컨텍스트로 활용
        context_messages = [SystemMessage(content=general_prompt)]
        for m in state["messages"]:
            if isinstance(m, HumanMessage):
                context_messages.append(m)
            elif isinstance(m, AIMessage):
                context_messages.append(m)

        # 최대 10개 메시지로 제한 (시스템 프롬프트 제외)
        if len(context_messages) > 11:
            context_messages = [context_messages[0]] + context_messages[-10:]

        response = await main_llm.ainvoke(context_messages)
        return {"messages": [response]}


    # ── 그래프 조립 ──────────────────────────────────────────

    graph = StateGraph(MessagesState)

    # 노드 등록
    graph.add_node("analyze", analyze_node)
    graph.add_node("resolve", resolve_node)
    graph.add_node("plan", plan_node)
    graph.add_node("execute_steps", execute_steps_node)
    graph.add_node("report_setup", report_setup_node)
    graph.add_node("report", report_llm_node)
    graph.add_node("direct_answer", direct_answer_node)
    graph.add_node("direct_answer_validation_fail", direct_answer_validation_fail_node)

    # 엣지 연결
    graph.add_edge(START, "analyze")

    graph.add_conditional_edges("analyze", route_after_analyze, {
        "resolve": "resolve",
        "plan": "plan",
        "direct_answer": "direct_answer",
    })

    graph.add_conditional_edges("resolve", route_after_resolve, {
        "plan": "plan",
        "direct_answer_validation_fail": "direct_answer_validation_fail",
    })

    graph.add_edge("plan", "execute_steps")
    graph.add_edge("execute_steps", "report_setup")
    graph.add_edge("report_setup", "report")
    graph.add_edge("report", END)
    graph.add_edge("direct_answer", END)
    graph.add_edge("direct_answer_validation_fail", END)

    return graph.compile()
