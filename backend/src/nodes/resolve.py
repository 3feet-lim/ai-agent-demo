"""
resolve 노드 — 추출된 식별자를 실제 리소스와 대조하여 검증
"""
import json
import re

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.graph import MessagesState
from loguru import logger

from .state import read_state_meta, read_state_meta_json, extract_text_from_content
from ..prompts import build_resolve_prompt


class ResolveNode:
    """리소스 검증 노드. LLM + sub-agent 도구로 식별자 존재 여부를 확인."""

    def __init__(self, llm_with_tools, main_tools: list[BaseTool],
                 profile_resolver, default_region: str):
        self._llm_with_tools = llm_with_tools
        self._main_tools = main_tools
        self._profile_resolver = profile_resolver
        self._default_region = default_region

    async def __call__(self, state: MessagesState) -> MessagesState:
        analyzed = read_state_meta_json(state, "ANALYZE_RESULT") or {}
        user_msg = read_state_meta(state, "ORIGINAL_MESSAGE") or ""

        identifiers = analyzed.get("identifiers", [])
        service_hint = analyzed.get("service_hint", "general")
        account_ref = analyzed.get("account_ref")
        regions = analyzed.get("regions", [])
        time_range = analyzed.get("time_range")

        # 프로필 결정
        if account_ref:
            profile = self._profile_resolver.resolve(account_ref)
        else:
            profile = (self._profile_resolver.resolve(user_msg)
                       if user_msg else self._profile_resolver.default_profile)

        region = regions[0] if regions else self._default_region

        logger.info(
            f"[Resolve] identifiers={identifiers}, service_hint={service_hint}, "
            f"account_ref={account_ref}, profile={profile}, region={region}"
        )

        # 식별자가 없으면 → 전체 현황 조회 (검증 불필요)
        if not identifiers:
            logger.info("[Resolve] 식별자 없음 → 전체 현황 조회 모드")
            resolved = {
                "profile": profile, "targets": [], "failed": [],
                "service_hint": service_hint, "regions": regions,
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
            result = await self._llm_with_tools.ainvoke(resolve_messages)
            tool_map = {tool.name: tool for tool in self._main_tools}
            loop_count = 0
            max_loops = 3

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
                result = await self._llm_with_tools.ainvoke(resolve_messages)

            # 최종 응답에서 JSON 파싱
            response_text = extract_text_from_content(result.content)
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
                    {"name": ident, "type": identifier_types.get(ident, "unknown"),
                     "detail": f"resolve 예외: {e}"}
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
                {"name": ident, "type": identifier_types.get(ident, "unknown"),
                 "detail": "resolve에서 확인 실패"}
                for ident in identifiers
            ]
            logger.warning(
                f"[Resolve] 식별자 {len(identifiers)}건 있었으나 "
                f"targets/failed 모두 비어있음 → 강제 실패 처리"
            )

        logger.info(f"[Resolve] 확정 {len(targets)}건, 실패 {len(failed)}건")

        resolved = {
            "profile": profile, "targets": targets, "failed": failed,
            "service_hint": service_hint, "regions": regions,
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
