"""
MCP 도구 래핑 및 Sub-Agent 관련 모듈

- MCPToolWrapper: MCP 도구를 LangChain BaseTool로 래핑
- SubAgentTool: Sub-agent를 Main Agent의 도구로 래핑
- classify_tool: MCP 도구를 sub-agent 역할별로 분류
- build_sub_agent_graph: Sub-agent용 ReAct 그래프 생성
- run_sub_agent: Sub-agent 실행
"""
import asyncio
import json
import re
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Optional, Any

from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.graph import StateGraph, MessagesState, START, END
from langgraph.errors import GraphRecursionError
from loguru import logger
from pydantic import BaseModel, Field, create_model

from .mcp_manager import MCPTool
from .time_utils import TIME_PARAM_MAP


# ── 공통 유틸리티 ──────────────────────────────────────────────

def create_pydantic_model_from_schema(name: str, schema: dict) -> type[BaseModel]:
    """MCP input_schema에서 Pydantic 모델 동적 생성

    MCP 서버 내부용 파라미터(ctx 등)는 LLM에 노출하지 않도록 필터링합니다.
    """
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))

    # MCP 서버 내부용 파라미터 필터링 (LLM이 임의 값을 넣어 오류 유발)
    _INTERNAL_PARAMS = {"ctx"}

    fields = {}
    for prop_name, prop_schema in properties.items():
        if prop_name in _INTERNAL_PARAMS:
            continue

        prop_type = prop_schema.get("type", "string")
        description = prop_schema.get("description", "")

        type_mapping = {
            "string": str, "integer": int, "number": float,
            "boolean": bool, "array": list, "object": dict,
        }
        python_type = type_mapping.get(prop_type, Any)

        if prop_name in required and prop_name not in _INTERNAL_PARAMS:
            fields[prop_name] = (python_type, Field(description=description))
        else:
            fields[prop_name] = (Optional[python_type], Field(default=None, description=description))

    if not fields:
        return create_model(f"{name}Input")
    return create_model(f"{name}Input", **fields)


# ── MCP 도구 래퍼 ──────────────────────────────────────────────

# MCP 도구명 → 비전문가용 한국어 설명
_MCP_TOOL_DISPLAY = {
    "query_prometheus": "Prometheus 메트릭 조회",
    "analyze_log_group": "로그 그룹 이상 패턴 분석",
    "execute_log_insights_query": "로그 검색 쿼리 실행",
    "describe_log_groups": "로그 그룹 목록 조회",
    "get_metric_data": "CloudWatch 메트릭 조회",
    "analyze_metric": "메트릭 추세 분석",
    "get_active_alarms": "활성 알람 조회",
    "get_alarm_history": "알람 이력 조회",
    "call_aws": "AWS API 호출",
    "list_prometheus_label_values": "Prometheus 라벨 값 조회",
    "list_prometheus_metric_names": "Prometheus 메트릭 목록 조회",
}

class MCPToolWrapper(BaseTool):
    """MCP 도구를 LangChain BaseTool로 래핑"""
    name: str
    description: str
    args_schema: type[BaseModel]
    mcp_tool: MCPTool
    mcp_manager: Any
    enforced_time_window: Optional[tuple[str, str]] = None
    resolved_profile: Optional[str] = None
    # 타겟 클러스터 가드레일: 설정되면 PromQL에 해당 클러스터 필터가 없는 쿼리를 차단
    allowed_clusters: Optional[list[str]] = None
    # 개별 MCP 도구 호출 이벤트를 상위로 전파하기 위한 큐
    event_queue: Optional[Any] = None

    class Config:
        arbitrary_types_allowed = True

    @staticmethod
    def _enrich_with_stats(raw: str) -> str:
        """도구 결과에 통계 요약을 자동 추가"""
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw

        summary_parts = []

        if isinstance(data, list):
            summary_parts.append(f"[통계] 총 항목 수: {len(data)}")
            if data and isinstance(data[0], dict):
                for key in ("State", "state", "Status", "status",
                            "InstanceState", "instanceState"):
                    vals = []
                    for item in data:
                        val = item.get(key)
                        if val is None and isinstance(item.get("State"), dict):
                            val = item["State"].get("Name")
                        if val is not None:
                            vals.append(str(val))
                    if vals:
                        counts = Counter(vals)
                        breakdown = ", ".join(f"{k}: {v}개" for k, v in counts.most_common())
                        summary_parts.append(f"[통계] {key}별 분포: {breakdown}")
                        break
        elif isinstance(data, dict):
            for key, val in data.items():
                if isinstance(val, list):
                    summary_parts.append(f"[통계] {key} 항목 수: {len(val)}")
                    nested_items = []
                    for item in val:
                        if isinstance(item, dict):
                            for sub_key, sub_val in item.items():
                                if isinstance(sub_val, list):
                                    nested_items.extend(sub_val)
                    if nested_items:
                        summary_parts.append(f"[통계] 중첩 항목 총 수: {len(nested_items)}")
                        if nested_items and isinstance(nested_items[0], dict):
                            for sk in ("State", "state", "Status", "status"):
                                vals = []
                                for ni in nested_items:
                                    sv = ni.get(sk)
                                    if sv is None and isinstance(ni.get("State"), dict):
                                        sv = ni["State"].get("Name")
                                    if sv is not None:
                                        vals.append(str(sv))
                                if vals:
                                    counts = Counter(vals)
                                    breakdown = ", ".join(f"{k}: {v}개" for k, v in counts.most_common())
                                    summary_parts.append(f"[통계] {sk}별 분포: {breakdown}")
                                    break

        if not summary_parts:
            return raw
        stats_header = "\n".join(summary_parts)
        return f"===STATS===\n{stats_header}\n===END STATS===\n\n{raw}"

    @staticmethod
    def _strip_stored_bytes(raw: str) -> str:
        """describe_log_groups 응답에서 storedBytes 필드를 제거.

        AWS API가 실제 로그가 존재하는 로그 그룹에 대해서도
        storedBytes=0을 반환하는 경우가 있어, LLM이 이를 근거로
        '로그 없음'으로 잘못 판단하는 것을 방지한다.
        """
        try:
            data = json.loads(raw)
            changed = False
            if isinstance(data, dict):
                # {"logGroups": [...]} 형태
                for key, val in data.items():
                    if isinstance(val, list):
                        for item in val:
                            if isinstance(item, dict) and "storedBytes" in item:
                                del item["storedBytes"]
                                changed = True
                # 최상위에 storedBytes가 있는 경우
                if "storedBytes" in data:
                    del data["storedBytes"]
                    changed = True
            elif isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and "storedBytes" in item:
                        del item["storedBytes"]
                        changed = True
            if changed:
                logger.info("[storedBytes 제거] describe_log_groups 응답에서 storedBytes 필드 제거 완료")
                return json.dumps(data, ensure_ascii=False)
        except (json.JSONDecodeError, TypeError):
            # JSON 파싱 실패 시 정규식으로 제거
            stripped = re.sub(r',?\s*"storedBytes"\s*:\s*\d+', '', raw)
            if stripped != raw:
                logger.info("[storedBytes 제거] 정규식으로 storedBytes 필드 제거 완료")
                return stripped
        return raw

    def _coerce_list_params(self, kwargs: dict) -> dict:
        """MCP 스키마에서 array 타입인 파라미터를 LLM이 문자열로 보낸 경우 리스트로 변환.

        예: log_group_names='["/aws/eks/..."]' → ["/aws/eks/..."]
            log_group_names='/aws/eks/...' → ['/aws/eks/...']
        """
        original_schema = self.mcp_tool.input_schema or {}
        properties = original_schema.get("properties", {})
        for key, val in kwargs.items():
            if not isinstance(val, str):
                continue
            prop_schema = properties.get(key, {})
            if prop_schema.get("type") != "array":
                continue
            # JSON 배열 문자열 시도
            stripped = val.strip()
            if stripped.startswith("["):
                try:
                    parsed = json.loads(stripped)
                    if isinstance(parsed, list):
                        kwargs[key] = parsed
                        logger.info(f"[타입 변환] {self.name}.{key}: 문자열 → 리스트 ({len(parsed)}개)")
                        continue
                except (json.JSONDecodeError, TypeError):
                    pass
            # 단일 문자열 → 리스트 래핑
            kwargs[key] = [val]
            logger.info(f"[타입 변환] {self.name}.{key}: 단일 문자열 → 리스트 래핑")
        return kwargs

    _CW_PROFILE_TOOLS = {"list_log_groups", "get_log_events", "start_live_tail",
                         "filter_log_events", "start_query", "get_query_results",
                         "get_metric_data", "list_metrics", "describe_alarms"}

    def _inject_profile(self, kwargs: dict):
        """resolved_profile과 기본 region을 MCP 도구 파라미터에 주입"""
        profile = self.resolved_profile
        if not profile:
            return
        server_name = self.mcp_tool.server_name if hasattr(self.mcp_tool, 'server_name') else ""
        if server_name == "cloudwatch":
            if not kwargs.get("profile_name"):
                kwargs["profile_name"] = profile
                logger.info(f"[Profile 주입] {self.name}: profile_name={profile}")
            # CloudWatch 도구: region을 항상 기본 리전으로 강제
            if "region" in kwargs:
                from .config import get_settings
                default_region = get_settings().aws_region
                if kwargs["region"] != default_region:
                    logger.info(f"[Region 강제] {self.name}: {kwargs['region']} → {default_region}")
                    kwargs["region"] = default_region
        elif server_name == "aws-api" and "cli_command" in kwargs:
            cmd = kwargs["cli_command"]
            if "--profile" not in cmd:
                kwargs["cli_command"] = f"{cmd} --profile {profile}"
                logger.info(f"[Profile 주입] {self.name}: --profile {profile}")
            if "--region" not in cmd:
                from .config import get_settings
                kwargs["cli_command"] = f"{kwargs['cli_command']} --region {get_settings().aws_region}"
                logger.info(f"[Region 주입] {self.name}: --region {get_settings().aws_region}")

    _BLOCKED_AWS_COMMANDS = [
        "aws cloudwatch get-metric",
        "aws cloudwatch list-metrics",
        "aws logs filter-log-events",
        "aws logs get-log-events",
        "aws logs start-query",
        "aws logs get-query-results",
        "aws logs describe-log-groups",
        "aws logs describe-log-streams",
    ]

    async def _arun(self, **kwargs) -> str:
        """비동기 도구 실행"""
        try:
            # call_aws에서 전용 도구가 있는 명령어를 호출하면 리다이렉트 안내
            if self.name == "call_aws":
                cli_cmd = str(kwargs.get("cli_command", "")).lower()
                for blocked in self._BLOCKED_AWS_COMMANDS:
                    if blocked in cli_cmd:
                        redirect_msg = (
                            f"이 명령어는 call_aws 대신 전용 도구를 사용하세요. "
                            f"메트릭 조회 → Grafana query_prometheus 도구, "
                            f"로그 조회 → CloudWatch execute_log_insights_query / describe_log_groups 도구. "
                            f"차단된 명령어: {kwargs.get('cli_command', '')[:100]}"
                        )
                        logger.warning(f"[차단] call_aws 우회 시도: {cli_cmd[:100]}")
                        return redirect_msg

            if self.resolved_profile:
                self._inject_profile(kwargs)

            # ── 타겟 클러스터 가드레일 ──
            if self.allowed_clusters and self.name == "query_prometheus":
                expr = str(kwargs.get("expr", ""))
                has_allowed = any(c in expr for c in self.allowed_clusters)
                if not has_allowed:
                    blocked_msg = (
                        f"[타겟 가드레일] PromQL에 허용된 클러스터 필터가 없어 차단됨. "
                        f"허용 클러스터: {self.allowed_clusters}. "
                        f"쿼리에 dimension_ClusterName 또는 cluster 라벨로 "
                        f"해당 클러스터를 반드시 포함하세요. 차단된 expr: {expr[:150]}"
                    )
                    logger.warning(
                        f"[가드레일 차단] {self.name}: allowed_clusters={self.allowed_clusters}, "
                        f"expr={expr[:200]}"
                    )
                    return blocked_msg

            # 알람 시간 범위 강제 덮어쓰기
            if self.enforced_time_window:
                enforced_start, enforced_end = self.enforced_time_window
                param_names = TIME_PARAM_MAP.get(self.name)
                if param_names:
                    start_key, end_key = param_names
                    original_start = kwargs.get(start_key)
                    original_end = kwargs.get(end_key)
                    kwargs[start_key] = enforced_start
                    kwargs[end_key] = enforced_end
                    if original_start != enforced_start or original_end != enforced_end:
                        logger.info(
                            f"[시간 강제] {self.name}: "
                            f"{original_start}~{original_end} → {enforced_start}~{enforced_end}"
                        )

                if self.name == "call_aws" and "cli_command" in kwargs:
                    cmd = kwargs["cli_command"]
                    enforced_start_epoch = int(
                        datetime.strptime(enforced_start, "%Y-%m-%dT%H:%M:%SZ")
                        .replace(tzinfo=timezone.utc).timestamp() * 1000
                    )
                    enforced_end_epoch = int(
                        datetime.strptime(enforced_end, "%Y-%m-%dT%H:%M:%SZ")
                        .replace(tzinfo=timezone.utc).timestamp() * 1000
                    )
                    cmd = re.sub(r"(--start-time\s+)\d{10,13}", rf"\g<1>{enforced_start_epoch}", cmd)
                    cmd = re.sub(r"(--end-time\s+)\d{10,13}", rf"\g<1>{enforced_end_epoch}", cmd)
                    cmd = re.sub(r"--start-time\s+(?!\d{10,13}\b)\S+", f"--start-time {enforced_start}", cmd)
                    cmd = re.sub(r"--end-time\s+(?!\d{10,13}\b)\S+", f"--end-time {enforced_end}", cmd)
                    if cmd != kwargs["cli_command"]:
                        logger.info(f"[시간 강제] call_aws CLI 시간 치환 완료")
                    kwargs["cli_command"] = cmd

            # None 값 파라미터 제거 (MCP 서버가 타입 불일치로 거부)
            kwargs = {k: v for k, v in kwargs.items() if v is not None}

            # LLM이 list 타입 파라미터를 문자열로 보내는 경우 자동 변환
            # 예: log_group_names='["/aws/eks/..."]' → ["/aws/eks/..."]
            kwargs = self._coerce_list_params(kwargs)

            # ctx는 MCP 서버 내부용 — LLM이 넣은 값이든 자동 주입이든 제거
            # (ctx가 필요한 MCP 서버는 서버 측에서 자체 주입함)
            kwargs.pop("ctx", None)

            logger.info(f"MCP tool {self.name} called with: {kwargs}")
            # 개별 MCP 도구 실행 시작 이벤트 발행
            if self.event_queue:
                self.event_queue.put_nowait({
                    "type": "mcp_tool_start", "name": self.name,
                    "display": _MCP_TOOL_DISPLAY.get(self.name, self.name),
                })
            result = await self.mcp_manager.execute_tool(self.mcp_tool.name, kwargs)

            if hasattr(result, 'content'):
                contents = []
                for item in result.content:
                    if hasattr(item, 'text'):
                        contents.append(item.text)
                    else:
                        contents.append(str(item))
                raw = "\n".join(contents)
            else:
                raw = str(result)

            # MCP 응답 결과 로깅 (디버깅용, 앞부분만)
            logger.debug(f"MCP tool {self.name} result ({len(raw)}자): {raw[:500]}")

            # storedBytes는 AWS API가 부정확한 값(0)을 반환하는 경우가 있어
            # LLM이 이를 근거로 로그 없음 판단을 내리는 것을 방지
            if self.name == "describe_log_groups":
                raw = self._strip_stored_bytes(raw)

            enriched = self._enrich_with_stats(raw)

            MAX_TOOL_RESPONSE_CHARS = 30000
            if len(enriched) > MAX_TOOL_RESPONSE_CHARS:
                logger.warning(
                    f"[Truncation] {self.name} 응답 잘림: "
                    f"{len(enriched):,}자 → {MAX_TOOL_RESPONSE_CHARS:,}자"
                )
                enriched = (
                    enriched[:MAX_TOOL_RESPONSE_CHARS]
                    + "\n\n...[⚠️ 응답이 너무 길어 잘렸습니다. "
                    "필요 시 범위를 좁혀 다시 조회하세요.]"
                )
            # 개별 MCP 도구 실행 완료 이벤트 발행
            if self.event_queue:
                self.event_queue.put_nowait({
                    "type": "mcp_tool_end", "name": self.name, "success": True,
                    "display": _MCP_TOOL_DISPLAY.get(self.name, self.name),
                })
            return enriched
        except Exception as e:
            logger.error(f"Tool execution error for {self.name}: {e}")
            if self.event_queue:
                self.event_queue.put_nowait({
                    "type": "mcp_tool_end", "name": self.name, "success": False,
                    "display": _MCP_TOOL_DISPLAY.get(self.name, self.name),
                })
            return f"Tool execution error: {str(e)}"

    def _run(self, **kwargs) -> str:
        raise NotImplementedError("Use async version")


def create_mcp_tool(mcp_tool: MCPTool, mcp_manager) -> BaseTool:
    """MCP 도구를 LangChain Tool로 변환"""
    schema = mcp_tool.input_schema or {}
    args_model = create_pydantic_model_from_schema(mcp_tool.name, schema)
    return MCPToolWrapper(
        name=mcp_tool.name,
        description=mcp_tool.description or f"{mcp_tool.name} tool",
        args_schema=args_model,
        mcp_tool=mcp_tool,
        mcp_manager=mcp_manager,
    )


# ── Sub-Agent 도구 분류 ──────────────────────────────────────────

# MCP 서버별 도구 → sub-agent 매핑
_TOOL_ROUTING = {
    "metric": {
        "servers": {"grafana"},
        "tools": set(),
    },
    "log": {
        "servers": {"cloudwatch"},
        "tools": set(),
    },
    "resource": {
        "servers": {"aws-api"},
        "tools": set(),
    },
    "network": {
        "servers": set(),
        "tools": set(),
    },
}


def classify_tool(mcp_tool: MCPTool) -> list[str]:
    """MCP 도구가 어떤 sub-agent에 속하는지 분류. 서버 기반으로 1:1 매핑."""
    for role, config in _TOOL_ROUTING.items():
        if mcp_tool.server_name in config["servers"]:
            return [role]
        if mcp_tool.name in config["tools"]:
            return [role]
    # 분류 안 된 도구는 resource에 기본 배정
    return ["resource"]


# ── Sub-Agent 실행기 ──────────────────────────────────────────

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


# ── Sub-Agent를 Main Agent의 도구로 래핑 ──────────────────────

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
        elapsed = time.monotonic() - start
        # ===STATS=== 블록 제거 (내부 메타데이터, 사용자에게 노출 불필요)
        result = re.sub(r'===STATS===.*?===END STATS===\s*', '', result, flags=re.DOTALL)
        # Sub-agent 응답이 너무 길면 truncate (Main Agent 컨텍스트 절약)
        max_len = 8000
        if len(result) > max_len:
            result = result[:max_len] + f"\n\n... (응답이 {len(result)}자로 길어 {max_len}자까지만 전달)"
        logger.info(
            f"[Main→Sub] {self.name} 완료: {elapsed:.1f}s, "
            f"MCP 도구 호출 {tool_count}회, 응답 {len(result)}자"
        )
        logger.debug(f"[Main→Sub] {self.name} 응답 미리보기: {result[:300]}")
        return result

    def _run(self, task: str) -> str:
        raise NotImplementedError("Use async version")
