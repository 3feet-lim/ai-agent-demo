"""
LangChain + LangGraph 기반 Multi-Agent Bedrock 클라이언트

Main Agent (라우팅/종합/리포트) → Sub-Agents (데이터 수집)
- Metric Agent: Grafana MCP, CloudWatch 메트릭
- Log Agent: CloudWatch Logs MCP
- Resource Agent: AWS API MCP (리소스 상태)
- Network Agent: AWS API MCP (VPC, TGW, SG, NACL)
"""
import asyncio
import json
from loguru import logger
import re
import time
from collections import Counter
from datetime import datetime, timezone, timedelta
from typing import Optional, Any

from langchain_aws import ChatBedrock
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.graph import StateGraph, MessagesState, START, END
from langgraph.errors import GraphRecursionError
from pydantic import BaseModel, Field, create_model

from .config import get_settings
from .mcp_manager import get_mcp_manager, MCPContext, MCPTool
from .conversation_store import get_conversation_store
from .account_profile_resolver import AccountProfileResolver


# ── 공통 유틸리티 ──────────────────────────────────────────────

def create_pydantic_model_from_schema(name: str, schema: dict) -> type[BaseModel]:
    """MCP input_schema에서 Pydantic 모델 동적 생성"""
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))

    fields = {}
    for prop_name, prop_schema in properties.items():
        prop_type = prop_schema.get("type", "string")
        description = prop_schema.get("description", "")

        type_mapping = {
            "string": str, "integer": int, "number": float,
            "boolean": bool, "array": list, "object": dict,
        }
        python_type = type_mapping.get(prop_type, Any)

        if prop_name in required:
            fields[prop_name] = (python_type, Field(description=description))
        else:
            fields[prop_name] = (Optional[python_type], Field(default=None, description=description))

    if not fields:
        return create_model(f"{name}Input")
    return create_model(f"{name}Input", **fields)


# 알람 메시지에서 발생 시각을 추출하고 ±10분 범위를 계산
_ALARM_TIME_PATTERN = re.compile(
    r"발생\s*시간\s*[:：]\s*(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})\s*(UTC|KST)?",
    re.IGNORECASE,
)

# 시간 파라미터를 가진 도구와 해당 파라미터 이름 매핑
_TIME_PARAM_MAP = {
    "execute_log_insights_query": ("start_time", "end_time"),
    "query_prometheus": ("startTime", "endTime"),
}


def parse_alarm_time_window(message: str, margin_minutes: int = 10, max_age_minutes: int = 60):
    """
    사용자 메시지에서 알람 발생 시각을 추출하고 ±margin 범위를 반환.
    발생 시각이 현재로부터 max_age_minutes 이상 지났으면 None 반환.
    """
    m = _ALARM_TIME_PATTERN.search(message)
    if not m:
        return None

    date_str, time_str, tz_str = m.group(1), m.group(2), m.group(3)
    dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S")

    if tz_str and tz_str.upper() == "KST":
        dt = dt.replace(tzinfo=timezone(timedelta(hours=9)))
        dt = dt.astimezone(timezone.utc)
    else:
        dt = dt.replace(tzinfo=timezone.utc)

    now_utc = datetime.now(timezone.utc)
    age = now_utc - dt
    if age > timedelta(minutes=max_age_minutes):
        logger.info(
            f"[시간 강제 건너뜀] 알람 발생 시각이 {age.total_seconds() / 3600:.1f}시간 전 "
            f"(한도: {max_age_minutes}분). 최근 30분 분석으로 폴백합니다."
        )
        return None

    start = dt - timedelta(minutes=margin_minutes)
    end = dt + timedelta(minutes=margin_minutes)
    return (start.strftime("%Y-%m-%dT%H:%M:%SZ"), end.strftime("%Y-%m-%dT%H:%M:%SZ"))


def _get_current_time_info() -> str:
    """현재 시간 정보 생성"""
    now_utc = datetime.now(timezone.utc)
    now_kst = now_utc.astimezone(timezone(timedelta(hours=9)))
    return f"Current time - UTC: {now_utc.strftime('%Y-%m-%d %H:%M:%S')} / KST: {now_kst.strftime('%Y-%m-%d %H:%M:%S')}"


# ── MCP 도구 래퍼 ──────────────────────────────────────────────

class MCPToolWrapper(BaseTool):
    """MCP 도구를 LangChain BaseTool로 래핑"""
    name: str
    description: str
    args_schema: type[BaseModel]
    mcp_tool: MCPTool
    mcp_manager: Any
    enforced_time_window: Optional[tuple[str, str]] = None
    resolved_profile: Optional[str] = None

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

    _CW_PROFILE_TOOLS = {"list_log_groups", "get_log_events", "start_live_tail",
                         "filter_log_events", "start_query", "get_query_results",
                         "get_metric_data", "list_metrics", "describe_alarms"}

    def _inject_profile(self, kwargs: dict):
        """resolved_profile을 MCP 도구 파라미터에 주입"""
        profile = self.resolved_profile
        if not profile:
            return
        server_name = self.mcp_tool.server_name if hasattr(self.mcp_tool, 'server_name') else ""
        if server_name == "cloudwatch":
            if not kwargs.get("profile_name"):
                kwargs["profile_name"] = profile
                logger.info(f"[Profile 주입] {self.name}: profile_name={profile}")
        elif server_name == "aws-api" and "cli_command" in kwargs:
            cmd = kwargs["cli_command"]
            if "--profile" not in cmd:
                kwargs["cli_command"] = f"{cmd} --profile {profile}"
                logger.info(f"[Profile 주입] {self.name}: --profile {profile}")

    _BLOCKED_AWS_COMMANDS = [
        "aws cloudwatch get-metric",
        "aws cloudwatch list-metrics",
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
                            f"메트릭 조회 → Grafana 도구, 로그 조회 → CloudWatch Logs 도구. "
                            f"차단된 명령어: {kwargs.get('cli_command', '')[:100]}"
                        )
                        logger.warning(f"[차단] call_aws 우회 시도: {cli_cmd[:100]}")
                        return redirect_msg

            if self.resolved_profile:
                self._inject_profile(kwargs)

            # 알람 시간 범위 강제 덮어쓰기
            if self.enforced_time_window:
                enforced_start, enforced_end = self.enforced_time_window
                param_names = _TIME_PARAM_MAP.get(self.name)
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

            logger.info(f"MCP tool {self.name} called with: {kwargs}")
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
            return enriched
        except Exception as e:
            logger.error(f"Tool execution error for {self.name}: {e}")
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
    # Metric Agent: Grafana MCP 도구만 (PromQL 기반 메트릭 조회)
    "metric": {
        "servers": {"grafana"},
        "tools": set(),
    },
    # Log Agent: CloudWatch MCP 도구만 (로그 조회)
    "log": {
        "servers": {"cloudwatch"},
        "tools": set(),
    },
    # Resource Agent: AWS API MCP (리소스 상태 조회)
    "resource": {
        "servers": {"aws-api"},
        "tools": set(),
    },
    # Network Agent: AWS API MCP (네트워크 전용, resource와 도구 공유)
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


# ── Sub-Agent 프롬프트 ──────────────────────────────────────────

def _build_metric_agent_prompt() -> str:
    """Metric Agent 전용 시스템 프롬프트"""
    return "\n".join([
        "You are a Metric Collection Agent. Collect metrics and return raw data ONLY.",
        "Do NOT write reports, analysis, or recommendations.",
        "Respond in Korean.",
        "",
        "## Tools",
        "- query_prometheus: PromQL 쿼리 실행 (Grafana 데이터소스)",
        "- list_prometheus_metric_names: 사용 가능한 메트릭명 탐색",
        "",
        "## Rules",
        "- query_prometheus에 PromQL을 직접 전달. 대시보드 탐색 금지.",
        "- 메트릭명 모르면 list_prometheus_metric_names로 먼저 탐색.",
        "- 최대 15회 도구 호출.",
        "",
        "## Output: bullet list로 '지표명: 값 at 시간' 형태만 반환.",
    ])


def _build_log_agent_prompt() -> str:
    """Log Agent 전용 시스템 프롬프트"""
    return "\n".join([
        "You are a Log Collection Agent. Collect logs and return raw data ONLY.",
        "Do NOT write reports, analysis, or recommendations.",
        "Respond in Korean.",
        "",
        "## Rules",
        "- 로그 그룹명을 추측하지 말 것. 반드시 describe_log_groups로 먼저 탐색.",
        "- 접두사 패턴: /aws/containerinsights/{cluster}/, /aws/eks/{cluster}/,",
        "  /aws/lambda/{fn}, /aws/rds/instance/{id}/",
        "- EKS Container Insights 하위: application, dataplane, host, performance, flowlogs",
        "- 최대 20회 도구 호출. 모든 관련 로그 그룹을 확인할 때까지 '로그 없음' 결론 금지.",
        "",
        "## Output: '로그 그룹: 이름' + '주요 에러: 메시지 — N회' 형태만 반환.",
    ])


def _build_resource_agent_prompt() -> str:
    """Resource Agent 전용 시스템 프롬프트"""
    return "\n".join([
        "You are a Resource Status Agent. Check AWS resource status and return raw data ONLY.",
        "Do NOT write reports, analysis, or recommendations.",
        "Respond in Korean.",
        "",
        "## Key Commands (call_aws)",
        "EKS: describe-cluster, list-nodegroups, describe-nodegroup",
        "EC2: describe-instances, describe-instance-status",
        "RDS: describe-db-instances | ALB/NLB: describe-target-health",
        "Lambda: get-function | ASG: describe-auto-scaling-groups",
        "CloudTrail: lookup-events",
        "",
        "## Rules",
        "- 최대 15회 도구 호출.",
        "",
        "## Output: '리소스: 이름/ID — 상태: 값' 형태만 반환.",
    ])


def _build_network_agent_prompt() -> str:
    """Network Agent 전용 시스템 프롬프트"""
    return "\n".join([
        "You are a Network Troubleshooting Agent. Investigate connectivity and return raw findings ONLY.",
        "Do NOT write reports, analysis, or recommendations.",
        "Respond in Korean.",
        "",
        "## Investigation Order (call_aws)",
        "1. 경로 식별: VPC, 서브넷, 연결 방식 (Peering/TGW/VPN/DX/IGW)",
        "2. 라우팅: describe-route-tables, describe-transit-gateway-attachments, search-transit-gateway-routes",
        "3. 보안: describe-security-groups, describe-network-acls (양방향 확인)",
        "4. 리소스 상태: ENI, NAT-GW, IGW",
        "5. Flow Logs / CloudTrail: REJECT 엔트리, 최근 변경 이벤트",
        "",
        "## Rules",
        "- 최대 20회 도구 호출.",
        "",
        "## Output: '경로/라우팅/보안그룹: 상태 — 상세' 형태만 반환.",
    ])


# ── Sub-Agent 실행기 ──────────────────────────────────────────

def _build_sub_agent_graph(llm_with_tools, tools: list[BaseTool]) -> Any:
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

        # 도구 매핑 (이름 → 도구 객체)
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

        # 모든 도구 호출을 병렬 실행
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


async def _run_sub_agent(
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
    try:
        result = await graph.ainvoke(
            {"messages": messages},
            {"recursion_limit": recursion_limit},
        )
        # 도구 호출 수 = ToolMessage 개수
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
        logger.info(f"[Main→Sub] {self.name} 호출: {task[:200]}")
        start = time.monotonic()
        result, tool_count = await _run_sub_agent(
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
        return result

    def _run(self, task: str) -> str:
        raise NotImplementedError("Use async version")


# ── Main Agent (BedrockAgent) ──────────────────────────────────

def _build_main_agent_prompt(enforced_time_window: tuple[str, str] | None = None) -> str:
    """Main Agent 시스템 프롬프트 (통합)"""
    time_info = _get_current_time_info()

    lines = [
        "You are Olly, an AI assistant for infrastructure observability.",
        "Always respond in Korean (한국어).",
        "",
        time_info,
        "",
        "## Architecture",
        "You are the Main Agent. You do NOT call AWS/Grafana/CloudWatch tools directly.",
        "Delegate to sub-agents:",
        "• collect_metrics — Grafana PromQL 메트릭 수집",
        "• collect_logs — CloudWatch Logs 로그 수집",
        "• check_resources — AWS 리소스 상태 확인 (EC2, EKS, RDS, ALB 등)",
        "• investigate_network — 네트워크 연결 문제 조사 (VPC, TGW, SG, NACL)",
        "",
        "## Workflow",
        "1. 질문 분석 → 필요한 데이터 판단",
        "2. sub-agent 호출. 서로 다른 sub-agent는 한 턴에 병렬 호출.",
        "   같은 sub-agent를 2번 호출하지 말 것 — 하나의 task에 모든 요청을 담아라.",
        "3. 수집된 데이터에서 핵심만 추출하여 리포트 작성.",
        "",
        "Sub-agent 호출 시 task에 리소스 ID, 시간 범위, 리전, 계정 정보를 구체적으로 포함.",
        "인사/일반 지식/이미 수집된 데이터 관련 질문은 sub-agent 없이 직접 답변.",
        "",
        "## Sub-Agent Selection (원칙 기반)",
        "• 메트릭/성능/사용률 → collect_metrics",
        "• 에러/로그/이벤트 → collect_logs",
        "• 리소스 상태/구성/목록 → check_resources",
        "• 연결/통신/라우팅 문제 → investigate_network",
        "• 장애 분석 → 관련 sub-agent 복수 병렬 호출 (메트릭+로그+리소스)",
        "",
        "## Response Rules",
        "• 시간: 사용자 시간은 KST(UTC+9). 미지정 시 최근 30분.",
        "• 반환 금지: sub-agent 원문을 그대로 복사하지 말 것. 핵심만 요약.",
        "• 할루시네이션 금지: sub-agent 출력에 없는 데이터를 만들지 말 것.",
        "  - 확인된 사항 → '확인된 사항:', 분석 의견 → '분석 의견:', 불확실 → '확인 필요'",
        "• 최종 리포트는 3000자 이내로 간결하게.",
        "",
        "## Report Format",
        "상황에 맞게 아래 구조를 사용. 마크다운 테이블 대신 bullet list 사용.",
        "",
        "현황 조회: 📊 인프라 현황 리포트",
        "  🕐 조회 시간 (KST/UTC) → 🎯 대상 → 리소스 요약 → 주요 메트릭 → 특이사항",
        "",
        "장애 분석: 🔍 장애 분석 리포트",
        "  🕐 분석 시간 → 🎯 대상 → 📅 기간 → 현상 요약 → 메트릭 분석 → 로그 분석",
        "  → 원인 분석 → 조치 방안 (🔴긴급 / 🟡권장 / 🟢참고)",
    ]

    return "\n".join(lines)


class BedrockAgent:
    """
    Multi-Agent Bedrock 에이전트

    Main Agent가 Sub-Agent를 도구로 호출하는 구조.
    Main: 라우팅, 종합 분석, 리포트 작성
    Sub: 메트릭/로그/리소스/네트워크 데이터 수집
    """

    def __init__(self):
        settings = get_settings()
        self.main_model_id = settings.bedrock_model_id
        self.sub_model_id = settings.bedrock_sub_agent_model_id or settings.bedrock_model_id
        self.region = settings.aws_region
        self._mcp_manager = None
        self._all_tools: list[BaseTool] = []  # 모든 MCP 도구
        self._sub_agent_tools: dict[str, list[BaseTool]] = {}  # role → 도구 리스트
        self._sub_agent_graphs: dict[str, Any] = {}  # role → 컴파일된 그래프
        self._main_tools: list[BaseTool] = []  # Main Agent용 도구 (SubAgentTool)
        self._main_llm = None
        self._main_graph = None
        self._profile_resolver = AccountProfileResolver()
        self._initialized = False

    async def _ensure_initialized(self):
        """비동기 초기화"""
        if self._initialized:
            return

        settings = get_settings()
        self._mcp_manager = await get_mcp_manager()
        context = await self._mcp_manager.get_context()

        # MCP 도구를 LangChain 도구로 변환
        self._all_tools = []
        for mcp_tool in context.tools:
            try:
                lc_tool = create_mcp_tool(mcp_tool, self._mcp_manager)
                self._all_tools.append(lc_tool)
            except Exception as e:
                logger.warning(f"Failed to create tool {mcp_tool.name}: {e}")

        # 도구를 sub-agent별로 분류
        self._sub_agent_tools = {"metric": [], "log": [], "resource": [], "network": []}
        for lc_tool in self._all_tools:
            mcp_tool = lc_tool.mcp_tool if hasattr(lc_tool, 'mcp_tool') else None
            if mcp_tool:
                roles = classify_tool(mcp_tool)
                for role in roles:
                    if role in self._sub_agent_tools:
                        self._sub_agent_tools[role].append(lc_tool)

        # network agent는 resource agent와 같은 도구 공유
        if not self._sub_agent_tools["network"]:
            self._sub_agent_tools["network"] = list(self._sub_agent_tools["resource"])

        # Sub-agent LLM 초기화
        sub_llm = ChatBedrock(
            model_id=self.sub_model_id,
            region_name=self.region,
            model_kwargs={"max_tokens": 4096, "temperature": 0.3},
        )

        logger.info(f"[Multi-Agent] Main model: {self.main_model_id}")
        logger.info(f"[Multi-Agent] Sub model: {self.sub_model_id}")

        # Sub-agent 그래프 생성
        sub_configs = {
            "metric": (_build_metric_agent_prompt, 40),
            "log": (_build_log_agent_prompt, 40),
            "resource": (_build_resource_agent_prompt, 30),
            "network": (_build_network_agent_prompt, 40),
        }

        self._main_tools = []
        for role, (prompt_fn, rec_limit) in sub_configs.items():
            tools = self._sub_agent_tools.get(role, [])
            if tools:
                sub_llm_with_tools = sub_llm.bind_tools(tools)
                graph = _build_sub_agent_graph(sub_llm_with_tools, tools)
                self._sub_agent_graphs[role] = graph
                tool_names = [t.name for t in tools]
                logger.info(f"[Multi-Agent] {role} agent: {len(tools)} tools ({', '.join(tool_names[:5])}...)")
            else:
                graph = None
                logger.warning(f"[Multi-Agent] {role} agent: no tools available")
                continue

            # Sub-agent를 Main Agent의 도구로 래핑
            sub_tool = SubAgentTool(
                name=f"collect_{role}s" if role == "metric" else
                     f"collect_{role}s" if role == "log" else
                     f"check_{role}s" if role == "resource" else
                     f"investigate_{role}",
                description=self._get_sub_agent_description(role),
                graph=graph,
                system_prompt=prompt_fn(),
                recursion_limit=rec_limit,
            )
            self._main_tools.append(sub_tool)

        # Main Agent LLM 초기화
        self._main_llm = ChatBedrock(
            model_id=self.main_model_id,
            region_name=self.region,
            model_kwargs={"max_tokens": 4096, "temperature": 0.7},
        )

        if self._main_tools:
            self._main_llm_with_tools = self._main_llm.bind_tools(self._main_tools)
            logger.info(f"[Multi-Agent] Main agent bound with {len(self._main_tools)} sub-agent tools")
        else:
            self._main_llm_with_tools = self._main_llm
            logger.warning("[Multi-Agent] No sub-agent tools available")

        # Main Agent 그래프 생성
        self._main_graph = self._build_main_graph()
        self._initialized = True
        logger.info("[Multi-Agent] 초기화 완료")

    @staticmethod
    def _get_sub_agent_description(role: str) -> str:
        """Sub-agent 도구 설명"""
        descs = {
            "metric": (
                "메트릭 수집 에이전트. Grafana(PromQL)와 CloudWatch에서 메트릭을 조회합니다. "
                "task에 조회할 지표, 리소스, 시간 범위, 리전 등을 구체적으로 명시하세요."
            ),
            "log": (
                "로그 수집 에이전트. CloudWatch Logs에서 로그를 조회합니다. "
                "task에 서비스명, 로그 그룹 힌트, 검색 키워드, 시간 범위 등을 명시하세요."
            ),
            "resource": (
                "AWS 리소스 상태 확인 에이전트. EC2, EKS, RDS, ALB 등의 상태를 조회합니다. "
                "task에 리소스 ID, 서비스 유형, 리전, 확인할 항목을 명시하세요."
            ),
            "network": (
                "네트워크 문제 조사 에이전트. VPC, TGW, SG, NACL, 라우팅 등을 조사합니다. "
                "task에 소스/대상, VPC ID, 서브넷, 연결 방식 등을 명시하세요."
            ),
        }
        return descs.get(role, "Sub-agent")

    def _build_main_graph(self) -> Any:
        """Main Agent용 LangGraph 워크플로우"""

        async def agent_node(state: MessagesState) -> MessagesState:
            messages = state["messages"]
            response = await self._main_llm_with_tools.ainvoke(messages)
            return {"messages": [response]}

        async def tools_node(state: MessagesState) -> MessagesState:
            """Sub-agent 호출을 병렬 실행"""
            messages = state["messages"]
            last_message = messages[-1]
            if not (hasattr(last_message, 'tool_calls') and last_message.tool_calls):
                return {"messages": []}

            # 도구 매핑 (이름 → 도구 객체)
            tool_map = {tool.name: tool for tool in self._main_tools}

            async def _dispatch_one(tool_call):
                tool_name = tool_call["name"]
                tool_args = tool_call["args"]
                tool_id = tool_call["id"]
                logger.info(f"[Main] Dispatching to sub-agent: {tool_name}")
                matched = tool_map.get(tool_name)
                if not matched:
                    return ToolMessage(content="Sub-agent not found.", tool_call_id=tool_id)
                try:
                    result = await matched.ainvoke(tool_args)
                except Exception as e:
                    result = f"Sub-agent error: {str(e)}"
                    logger.error(f"[Main] Sub-agent error: {e}")
                return ToolMessage(content=str(result), tool_call_id=tool_id)

            # 모든 sub-agent 호출을 병렬 실행
            tool_messages = await asyncio.gather(
                *[_dispatch_one(tc) for tc in last_message.tool_calls]
            )
            return {"messages": list(tool_messages)}
            return {"messages": tool_messages}

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

    # ── 히스토리 / 토큰 관리 ──────────────────────────────────

    WINDOW_SIZE = 20
    MAX_CONTEXT_TOKENS = 150000
    CHARS_PER_TOKEN = 3

    def _estimate_tokens(self, text: str) -> int:
        return len(text) // self.CHARS_PER_TOKEN

    def _trim_messages_by_tokens(self, messages: list, max_tokens: int | None = None) -> list:
        """LangChain 메시지 리스트를 토큰 한도 내로 트리밍"""
        if not messages:
            return messages
        max_tokens = max_tokens or self.MAX_CONTEXT_TOKENS
        used = 0
        system_msgs = []
        other_msgs = []
        for msg in messages:
            content = msg.content if hasattr(msg, "content") else str(msg)
            if isinstance(content, list):
                content = " ".join(
                    str(b.get("text", "")) if isinstance(b, dict) else str(b)
                    for b in content
                )
            if isinstance(msg, SystemMessage):
                system_msgs.append(msg)
                used += self._estimate_tokens(str(content))
            else:
                other_msgs.append((msg, self._estimate_tokens(str(content))))
        if used >= max_tokens:
            logger.warning(f"[토큰 관리] 시스템 프롬프트만으로 {used:,} 토큰")
            return messages
        remaining = max_tokens - used
        kept = []
        for msg, tokens in reversed(other_msgs):
            if remaining - tokens < 0:
                break
            kept.insert(0, msg)
            remaining -= tokens
        trimmed_count = len(other_msgs) - len(kept)
        if trimmed_count > 0:
            logger.info(
                f"[토큰 관리] 히스토리 트리밍: {len(other_msgs)}개 → {len(kept)}개 "
                f"(제거 {trimmed_count}개)"
            )
        return system_msgs + kept

    async def _summarize_messages(self, messages: list[dict]) -> str:
        """오래된 메시지들을 LLM으로 요약"""
        if not messages:
            return ""
        conversation_text = "\n".join(
            [f"{m['role']}: {m['content']}" for m in messages]
        )
        summary_prompt = [
            SystemMessage(content=(
                "You are a conversation summarizer. "
                "Summarize the following conversation concisely in Korean. "
                "Preserve key facts, decisions, tool results, and important context. "
                "Keep the summary under 500 words."
            )),
            HumanMessage(content=conversation_text),
        ]
        response = await self._main_llm.ainvoke(summary_prompt)
        if isinstance(response.content, str):
            return response.content
        return str(response.content)

    async def _build_hybrid_history(
        self, conversation_id: str, all_messages: list[dict],
    ) -> list[dict]:
        """하이브리드 메모리: 요약 + 슬라이딩 윈도우"""
        total = len(all_messages)
        if total <= self.WINDOW_SIZE:
            return all_messages
        old_messages = all_messages[:-self.WINDOW_SIZE]
        recent_messages = all_messages[-self.WINDOW_SIZE:]
        store = await get_conversation_store()
        existing = await store.get_summary(conversation_id)
        old_count = len(old_messages)
        if not existing or existing["summarized_until"] < old_count:
            if existing and existing["summarized_until"] > 0:
                new_portion = old_messages[existing["summarized_until"]:]
                combined_text = (
                    f"기존 요약:\n{existing['summary']}\n\n추가 대화:\n"
                    + "\n".join([f"{m['role']}: {m['content']}" for m in new_portion])
                )
                summary_messages = [
                    SystemMessage(content=(
                        "You are a conversation summarizer. "
                        "Merge the existing summary with the new conversation. "
                        "Respond in Korean. Keep under 500 words."
                    )),
                    HumanMessage(content=combined_text),
                ]
                response = await self._main_llm.ainvoke(summary_messages)
                summary = response.content if isinstance(response.content, str) else str(response.content)
            else:
                summary = await self._summarize_messages(old_messages)
            await store.save_summary(conversation_id, summary, old_count)
            logger.info(f"대화 요약 갱신: conversation_id={conversation_id}")
        else:
            summary = existing["summary"]
        return [
            {"role": "system", "content": f"[이전 대화 요약]\n{summary}"},
        ] + recent_messages

    def _convert_to_langchain_messages(
        self, history: list[dict], system_prompt: str,
        images: Optional[list[str]] = None,
    ) -> list:
        """대화 히스토리를 LangChain 메시지 형식으로 변환"""
        messages = []
        if system_prompt:
            messages.append(SystemMessage(content=system_prompt))
        for i, msg in enumerate(history):
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "user":
                is_last_user = (i == len(history) - 1) and images
                if is_last_user:
                    content_blocks = []
                    for img_data in images:
                        if img_data.startswith("data:"):
                            header, b64 = img_data.split(",", 1)
                            media_type = header.split(":")[1].split(";")[0]
                        else:
                            b64 = img_data
                            media_type = "image/png"
                        content_blocks.append({
                            "type": "image_url",
                            "image_url": {"url": f"data:{media_type};base64,{b64}"},
                        })
                    content_blocks.append({"type": "text", "text": content})
                    messages.append(HumanMessage(content=content_blocks))
                else:
                    messages.append(HumanMessage(content=content))
            elif role == "assistant":
                messages.append(AIMessage(content=content))
            elif role == "system":
                messages.append(SystemMessage(content=content))
        return messages

    def _prepare_tools_for_request(
        self, message: str, time_window: Optional[tuple[str, str]],
    ):
        """요청별 도구 설정 (시간 강제, 프로필 주입)"""
        resolved_profile = self._profile_resolver.resolve(message)
        for tool in self._all_tools:
            if isinstance(tool, MCPToolWrapper):
                tool.enforced_time_window = time_window
                tool.resolved_profile = resolved_profile

    def _build_full_system_prompt(
        self, enforced_time_window: tuple[str, str] | None = None,
    ) -> str:
        """Main Agent 전체 시스템 프롬프트 조합"""
        base = _build_main_agent_prompt(enforced_time_window)
        # 알람 시간 범위 강제 표시
        if enforced_time_window:
            s_utc, e_utc = enforced_time_window
            s_dt = datetime.strptime(s_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            e_dt = datetime.strptime(e_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            kst = timezone(timedelta(hours=9))
            s_kst = s_dt.astimezone(kst).strftime("%Y-%m-%d %H:%M:%S")
            e_kst = e_dt.astimezone(kst).strftime("%Y-%m-%d %H:%M:%S")
            base += (
                f"\n\n## ENFORCED TIME RANGE\n"
                f"UTC: {s_utc} ~ {e_utc} / KST: {s_kst} ~ {e_kst}\n"
                f"Sub-agent 호출 시 이 시간 범위를 task에 포함. 리포트 분석 기간도 이 범위와 일치."
            )
        return base

    async def chat_stream(
        self, message: str, history: Optional[list[dict]] = None,
        conversation_id: Optional[str] = None,
        images: Optional[list[str]] = None,
    ):
        """스트리밍 대화 처리"""
        await self._ensure_initialized()
        history = history or []
        # 로그 prefix (conversation_id 앞 8자)
        cid = (conversation_id or "no-conv")[:8]

        time_window = parse_alarm_time_window(message)
        if time_window:
            tz_match = _ALARM_TIME_PATTERN.search(message)
            original_tz = tz_match.group(3) if tz_match and tz_match.group(3) else "UTC"
            original_time = f"{tz_match.group(1)} {tz_match.group(2)}" if tz_match else "?"
            logger.info(f"[{cid}] [시간 강제] 알람 시각: {original_time} {original_tz} → {time_window}")

        self._prepare_tools_for_request(message, time_window)

        system_prompt = self._build_full_system_prompt(enforced_time_window=time_window)
        current_history = history + [{"role": "user", "content": message}]

        if conversation_id and len(current_history) > self.WINDOW_SIZE:
            current_history = await self._build_hybrid_history(
                conversation_id, current_history
            )

        messages = self._convert_to_langchain_messages(
            current_history, system_prompt, images=images,
        )
        messages = self._trim_messages_by_tokens(messages)

        stream_start = time.monotonic()
        first_token_time = None
        tool_call_count = 0

        try:
            async for event in self._main_graph.astream_events(
                {"messages": messages},
                config={"recursion_limit": 15},
                version="v2",
            ):
                kind = event.get("event")

                if kind == "on_tool_start":
                    tool_call_count += 1
                    tool_name = event.get("name", "unknown")
                    logger.info(f"[{cid}] Sub-agent 호출 #{tool_call_count}: {tool_name} "
                                f"(경과: {time.monotonic() - stream_start:.1f}s)")
                    yield {"type": "tool_start", "name": tool_name,
                           "args": event.get("data", {}).get("input", {})}

                elif kind == "on_tool_end":
                    tool_name = event.get("name", "unknown")
                    output = event.get("data", {}).get("output", "")
                    if hasattr(output, "content"):
                        result_str = str(output.content)
                    else:
                        result_str = str(output) if output else ""
                    is_error = (
                        not result_str or len(result_str.strip()) < 5
                        or result_str.startswith("Sub-agent error:")
                    )
                    yield {"type": "tool_end", "name": tool_name,
                           "success": not is_error}

                elif kind == "on_chat_model_stream":
                    # TODO: sub-agent 내부 토큰 필터링 방법 확인 필요
                    # 현재는 모든 on_chat_model_stream 이벤트를 전달
                    chunk = event.get("data", {}).get("chunk")
                    if chunk and hasattr(chunk, "content"):
                        content = chunk.content
                        if isinstance(content, str) and content:
                            if first_token_time is None:
                                first_token_time = time.monotonic()
                                logger.info(f"[{cid}] 첫 토큰: {first_token_time - stream_start:.1f}s")
                            yield {"type": "token", "content": content}
                        elif isinstance(content, list):
                            for block in content:
                                if isinstance(block, dict) and block.get("type") == "text":
                                    text = block.get("text", "")
                                    if text:
                                        if first_token_time is None:
                                            first_token_time = time.monotonic()
                                        yield {"type": "token", "content": text}

            total_time = time.monotonic() - stream_start
            logger.info(f"[{cid}] 완료: {total_time:.1f}s, sub-agent 호출 {tool_call_count}회")

        except GraphRecursionError:
            logger.warning(f"[{cid}] Main agent 도구 호출 제한 도달")
            yield {
                "type": "token",
                "content": (
                    "\n\n---\n"
                    "⚠️ **Sub-agent 호출 횟수 제한에 도달하여 분석을 중단합니다.**\n\n"
                    "위 내용은 제한 도달 전까지 수집된 정보 기반입니다. "
                    "범위를 좁혀서 다시 질문해 주세요."
                ),
            }
        except Exception as e:
            logger.error(f"[{cid}] Error during chat_stream: {e}")
            raise

    async def chat(
        self, message: str, history: Optional[list[dict]] = None,
        conversation_id: Optional[str] = None,
    ) -> str:
        """비스트리밍 대화 처리 (webhook용)"""
        await self._ensure_initialized()
        history = history or []

        time_window = parse_alarm_time_window(message)
        self._prepare_tools_for_request(message, time_window)

        system_prompt = self._build_full_system_prompt(enforced_time_window=time_window)
        current_history = history + [{"role": "user", "content": message}]

        if conversation_id and len(current_history) > self.WINDOW_SIZE:
            current_history = await self._build_hybrid_history(
                conversation_id, current_history
            )

        messages = self._convert_to_langchain_messages(current_history, system_prompt)
        messages = self._trim_messages_by_tokens(messages)

        try:
            result = await self._main_graph.ainvoke(
                {"messages": messages}, {"recursion_limit": 15},
            )
            ai_messages = [m for m in result["messages"] if isinstance(m, AIMessage)]
            if ai_messages:
                last = ai_messages[-1]
                if isinstance(last.content, str):
                    return last.content
                elif isinstance(last.content, list):
                    texts = [c.get("text", "") for c in last.content
                             if isinstance(c, dict) and "text" in c]
                    return "\n".join(texts) if texts else str(last.content)
                return str(last.content)
            return "응답을 생성할 수 없습니다."

        except GraphRecursionError:
            logger.warning("Main agent 도구 호출 제한 도달 (chat)")
            try:
                ai_msgs = [m for m in result["messages"] if isinstance(m, AIMessage)]
                if ai_msgs and isinstance(ai_msgs[-1].content, str):
                    return ai_msgs[-1].content + "\n\n⚠️ Sub-agent 호출 제한 도달."
            except Exception:
                pass
            return "⚠️ Sub-agent 호출 제한에 도달하여 분석을 완료하지 못했습니다."

        except Exception as e:
            logger.error(f"Error during chat: {e}")
            raise


# ── 싱글톤 ──────────────────────────────────────────────────

_agent: Optional[BedrockAgent] = None


async def get_bedrock_agent() -> BedrockAgent:
    """Bedrock 에이전트 싱글톤 반환 (비동기)"""
    global _agent
    if _agent is None:
        _agent = BedrockAgent()
    await _agent._ensure_initialized()
    return _agent
