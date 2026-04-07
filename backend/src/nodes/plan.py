"""
plan 노드 — 규칙 기반 실행 계획 수립 (LLM 호출 없음)
"""
import json

from langchain_core.messages import SystemMessage
from langgraph.graph import MessagesState
from loguru import logger

from .state import read_state_meta_json


class PlanNode:
    """규칙 기반 실행 계획 수립 노드.

    collection_types와 category를 보고 결정론적으로 step 계획을 생성합니다.
    """

    async def __call__(self, state: MessagesState) -> MessagesState:
        analyzed = read_state_meta_json(state, "ANALYZE_RESULT") or {}
        resolved = read_state_meta_json(state, "LOCKED_TARGETS")

        intent = analyzed.get("intent", "리소스 정보를 수집하세요.")
        collection_types = analyzed.get("collection_types", ["resource"])
        if not collection_types:
            collection_types = ["resource"]

        # 타겟 정보
        targets = resolved.get("targets", []) if resolved else []
        target_names = ", ".join(t.get("name", "") for t in targets) or "전체"

        # 규칙 기반 계획 생성
        has_resource = "resource" in collection_types
        has_network = "network" in collection_types
        parallel_agents = [a for a in collection_types if a in ("log", "metric")]

        steps = []
        if has_resource and parallel_agents:
            steps.append({
                "step_id": 0,
                "agents": ["resource"],
                "purpose": f"{target_names} 리소스 상태 조회 및 관련 로그 그룹 탐색",
                "task_template": intent,
                "depends_on": None,
            })
            steps.append({
                "step_id": 1,
                "agents": parallel_agents,
                "purpose": " + ".join(
                    {"log": "로그 수집", "metric": "메트릭 수집"}.get(a, a)
                    for a in parallel_agents
                ) + " (병렬)",
                "task_template": f"이전 단계에서 확인된 리소스 정보를 사용하여 {intent}",
                "depends_on": 0,
            })
            if has_network:
                steps.append({
                    "step_id": 2,
                    "agents": ["network"],
                    "purpose": "네트워크 경로 및 보안 규칙 조사",
                    "task_template": f"이전 단계에서 확인된 정보를 기반으로 네트워크를 조사하세요. {intent}",
                    "depends_on": 0,
                })
        elif has_network and has_resource:
            steps.append({
                "step_id": 0,
                "agents": ["resource"],
                "purpose": f"{target_names} 리소스 및 VPC/서브넷 정보 조회",
                "task_template": intent,
                "depends_on": None,
            })
            steps.append({
                "step_id": 1,
                "agents": ["network"],
                "purpose": "네트워크 경로 및 보안 규칙 조사",
                "task_template": f"이전 단계에서 확인된 VPC 정보를 기반으로 조사하세요. {intent}",
                "depends_on": 0,
            })
        else:
            steps.append({
                "step_id": 0,
                "agents": collection_types,
                "purpose": intent,
                "task_template": intent,
                "depends_on": None,
            })

        plan = {"steps": steps}
        logger.info(
            f"[Plan] 규칙 기반 계획 수립: {len(steps)}개 step, "
            f"agents={[s.get('agents', []) for s in steps]}"
        )

        return {"messages": [
            SystemMessage(content=f"__EXECUTION_PLAN__:{json.dumps(plan, ensure_ascii=False)}")
        ]}
