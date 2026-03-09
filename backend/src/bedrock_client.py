"""
LangChain + LangGraph 기반 Bedrock 클라이언트
MCP 도구를 실제로 호출하는 ReAct 에이전트 구현
"""
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


def create_pydantic_model_from_schema(name: str, schema: dict) -> type[BaseModel]:
    """MCP input_schema에서 Pydantic 모델 동적 생성"""
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))

    fields = {}
    for prop_name, prop_schema in properties.items():
        prop_type = prop_schema.get("type", "string")
        description = prop_schema.get("description", "")

        # JSON Schema 타입을 Python 타입으로 매핑
        type_mapping = {
            "string": str,
            "integer": int,
            "number": float,
            "boolean": bool,
            "array": list,
            "object": dict,
        }
        python_type = type_mapping.get(prop_type, Any)

        # 필수 여부에 따라 기본값 설정
        if prop_name in required:
            fields[prop_name] = (python_type, Field(description=description))
        else:
            fields[prop_name] = (Optional[python_type], Field(default=None, description=description))

    # 빈 스키마인 경우 기본 모델 반환
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
    # CloudWatch Logs 도구
    "execute_log_insights_query": ("start_time", "end_time"),
    # Grafana 도구
    "query_prometheus": ("startTime", "endTime"),
}


def parse_alarm_time_window(message: str, margin_minutes: int = 10):
    """
    사용자 메시지에서 알람 발생 시각을 추출하고 ±margin 범위를 반환.
    Returns: (start_utc_iso, end_utc_iso) 또는 None
    """
    m = _ALARM_TIME_PATTERN.search(message)
    if not m:
        return None

    date_str, time_str, tz_str = m.group(1), m.group(2), m.group(3)
    dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S")

    # KST면 UTC로 변환
    if tz_str and tz_str.upper() == "KST":
        dt = dt.replace(tzinfo=timezone(timedelta(hours=9)))
        dt = dt.astimezone(timezone.utc)
    else:
        dt = dt.replace(tzinfo=timezone.utc)

    start = dt - timedelta(minutes=margin_minutes)
    end = dt + timedelta(minutes=margin_minutes)
    return (start.strftime("%Y-%m-%dT%H:%M:%SZ"), end.strftime("%Y-%m-%dT%H:%M:%SZ"))


class MCPToolWrapper(BaseTool):
    """MCP 도구를 LangChain BaseTool로 래핑"""
    name: str
    description: str
    args_schema: type[BaseModel]
    mcp_tool: MCPTool
    mcp_manager: Any
    # 알람 시간 범위 (설정 시 도구 호출의 시간 파라미터를 강제 덮어씀)
    enforced_time_window: Optional[tuple[str, str]] = None
    # 동적으로 결정된 AWS profile (계정별 자동 전환)
    resolved_profile: Optional[str] = None

    class Config:
        arbitrary_types_allowed = True

    @staticmethod
    def _enrich_with_stats(raw: str) -> str:
        """도구 결과에 통계 요약을 자동 추가 (LLM 카운팅 오류 방지)"""
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw

        summary_parts = []

        # 최상위 리스트인 경우
        if isinstance(data, list):
            summary_parts.append(f"[통계] 총 항목 수: {len(data)}")
            # 리스트 내 dict에서 상태 필드 자동 집계
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
                        breakdown = ", ".join(
                            f"{k}: {v}개" for k, v in counts.most_common()
                        )
                        summary_parts.append(f"[통계] {key}별 분포: {breakdown}")
                        break

        # dict 안에 리스트가 있는 경우 (예: {"Reservations": [...]})
        elif isinstance(data, dict):
            for key, val in data.items():
                if isinstance(val, list):
                    summary_parts.append(f"[통계] {key} 항목 수: {len(val)}")
                    # 중첩 리스트 (예: Reservations → Instances)
                    nested_items = []
                    for item in val:
                        if isinstance(item, dict):
                            for sub_key, sub_val in item.items():
                                if isinstance(sub_val, list):
                                    nested_items.extend(sub_val)
                    if nested_items:
                        summary_parts.append(
                            f"[통계] 중첩 항목 총 수: {len(nested_items)}"
                        )
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
                                    breakdown = ", ".join(
                                        f"{k}: {v}개" for k, v in counts.most_common()
                                    )
                                    summary_parts.append(
                                        f"[통계] {sk}별 분포: {breakdown}"
                                    )
                                    break

        if not summary_parts:
            return raw

        stats_header = "\n".join(summary_parts)
        return f"===STATS===\n{stats_header}\n===END STATS===\n\n{raw}"

    # CloudWatch MCP 도구 중 profile_name 파라미터를 지원하는 도구 패턴
    _CW_PROFILE_TOOLS = {"list_log_groups", "get_log_events", "start_live_tail",
                         "filter_log_events", "start_query", "get_query_results",
                         "get_metric_data", "list_metrics", "describe_alarms"}

    def _inject_profile(self, kwargs: dict):
        """
        resolved_profile을 MCP 도구 파라미터에 주입.
        - CloudWatch MCP 도구: profile_name 파라미터 추가
        - AWS API MCP (call_aws): CLI 명령에 --profile 플래그 추가
        """
        profile = self.resolved_profile
        if not profile:
            return

        server_name = self.mcp_tool.server_name if hasattr(self.mcp_tool, 'server_name') else ""

        # CloudWatch MCP: profile_name 파라미터 주입
        if server_name == "cloudwatch":
            # LLM이 이미 profile_name을 지정했으면 덮어쓰지 않음
            if not kwargs.get("profile_name"):
                kwargs["profile_name"] = profile
                logger.info(f"[Profile 주입] {self.name}: profile_name={profile}")

        # AWS API MCP: CLI 명령에 --profile 추가
        elif server_name == "aws-api" and "cli_command" in kwargs:
            cmd = kwargs["cli_command"]
            # 이미 --profile이 있으면 덮어쓰지 않음
            if "--profile" not in cmd:
                kwargs["cli_command"] = f"{cmd} --profile {profile}"
                logger.info(f"[Profile 주입] {self.name}: --profile {profile}")

    # call_aws에서 차단할 명령어 패턴 (Grafana 전용 도구가 있는 메트릭 조회)
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

            # AWS profile 자동 주입 (계정별 동적 전환)
            if self.resolved_profile:
                self._inject_profile(kwargs)

            # 알람 시간 범위가 설정되어 있으면 시간 파라미터 강제 덮어쓰기
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

                # call_aws CLI 명령어 안의 --start-time, --end-time 치환
                if self.name == "call_aws" and "cli_command" in kwargs:
                    cmd = kwargs["cli_command"]
                    # epoch 값 미리 계산
                    enforced_start_epoch = int(
                        datetime.strptime(enforced_start, "%Y-%m-%dT%H:%M:%SZ")
                        .replace(tzinfo=timezone.utc)
                        .timestamp() * 1000
                    )
                    enforced_end_epoch = int(
                        datetime.strptime(enforced_end, "%Y-%m-%dT%H:%M:%SZ")
                        .replace(tzinfo=timezone.utc)
                        .timestamp() * 1000
                    )
                    # epoch 형식 먼저 치환 (순서 중요: \S+가 epoch도 매칭하므로)
                    cmd = re.sub(
                        r"(--start-time\s+)\d{10,13}",
                        rf"\g<1>{enforced_start_epoch}",
                        cmd,
                    )
                    cmd = re.sub(
                        r"(--end-time\s+)\d{10,13}",
                        rf"\g<1>{enforced_end_epoch}",
                        cmd,
                    )
                    # ISO 형식 치환 (epoch이 아닌 나머지)
                    cmd = re.sub(
                        r"--start-time\s+(?!\d{10,13}\b)\S+",
                        f"--start-time {enforced_start}",
                        cmd,
                    )
                    cmd = re.sub(
                        r"--end-time\s+(?!\d{10,13}\b)\S+",
                        f"--end-time {enforced_end}",
                        cmd,
                    )
                    if cmd != kwargs["cli_command"]:
                        logger.info(f"[시간 강제] call_aws CLI 시간 치환 완료")
                    kwargs["cli_command"] = cmd

            logger.info(f"MCP tool {self.name} called with: {kwargs}")
            result = await self.mcp_manager.execute_tool(self.mcp_tool.name, kwargs)

            # MCP 결과를 문자열로 변환
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

            # 통계 요약 자동 추가
            return self._enrich_with_stats(raw)
        except Exception as e:
            logger.error(f"Tool execution error for {self.name}: {e}")
            return f"Tool execution error: {str(e)}"

    def _run(self, **kwargs) -> str:
        """동기 도구 실행 (사용하지 않음)"""
        raise NotImplementedError("Use async version")


def create_mcp_tool(mcp_tool: MCPTool, mcp_manager) -> BaseTool:
    """MCP 도구를 LangChain Tool로 변환"""
    # input_schema에서 Pydantic 모델 생성
    schema = mcp_tool.input_schema or {}
    args_model = create_pydantic_model_from_schema(mcp_tool.name, schema)

    return MCPToolWrapper(
        name=mcp_tool.name,
        description=mcp_tool.description or f"{mcp_tool.name} tool",
        args_schema=args_model,
        mcp_tool=mcp_tool,
        mcp_manager=mcp_manager,
    )


class BedrockAgent:
    """
    LangChain + LangGraph 기반 Bedrock 에이전트

    MCP에서 제공하는 도구를 실제로 호출하는 ReAct 에이전트입니다.
    """

    def __init__(self):
        settings = get_settings()
        self.model_id = settings.bedrock_model_id
        self.region = settings.aws_region
        self._mcp_manager = None
        self._tools: list[BaseTool] = []
        self._llm = None
        self._graph = None
        self._profile_resolver = AccountProfileResolver()

    async def _ensure_initialized(self):
        """비동기 초기화 보장"""
        if self._llm is not None:
            return

        settings = get_settings()

        # MCP 매니저에서 도구 가져오기
        self._mcp_manager = await get_mcp_manager()
        context = await self._mcp_manager.get_context()

        # MCP 도구를 LangChain 도구로 변환
        self._tools = []
        for mcp_tool in context.tools:
            try:
                lc_tool = create_mcp_tool(mcp_tool, self._mcp_manager)
                self._tools.append(lc_tool)
                logger.info(f"Registered tool: {mcp_tool.name}")
            except Exception as e:
                logger.warning(f"Failed to create tool {mcp_tool.name}: {e}")

        # LangChain ChatBedrock 모델 초기화
        self._llm = ChatBedrock(
            model_id=self.model_id,
            region_name=self.region,
            model_kwargs={
                "max_tokens": 4096,
                "temperature": 0.7,
            }
        )

        # 도구가 있으면 바인딩
        if self._tools:
            self._llm_with_tools = self._llm.bind_tools(self._tools)
            logger.info(f"LLM bound with {len(self._tools)} tools")
        else:
            self._llm_with_tools = self._llm
            logger.info("No tools available, using plain LLM")

        # LangGraph 워크플로우 생성
        self._graph = self._build_graph()

    def _build_graph(self) -> StateGraph:
        """LangGraph 워크플로우 구성 - ReAct 패턴"""

        async def agent_node(state: MessagesState) -> MessagesState:
            """에이전트 노드: LLM 호출 (도구 바인딩됨)"""
            messages = state["messages"]
            response = await self._llm_with_tools.ainvoke(messages)
            return {"messages": [response]}

        async def tools_node(state: MessagesState) -> MessagesState:
            """도구 노드: 도구 호출 실행"""
            messages = state["messages"]
            last_message = messages[-1]

            tool_messages = []

            # tool_calls가 있는지 확인
            if hasattr(last_message, 'tool_calls') and last_message.tool_calls:
                for tool_call in last_message.tool_calls:
                    tool_name = tool_call["name"]
                    tool_args = tool_call["args"]
                    tool_id = tool_call["id"]

                    logger.info(f"Executing tool: {tool_name} with args: {tool_args}")

                    # 도구 찾기 및 실행
                    result = "Tool not found."
                    for tool in self._tools:
                        if tool.name == tool_name:
                            try:
                                result = await tool.ainvoke(tool_args)
                            except Exception as e:
                                result = f"Tool execution error: {str(e)}"
                                logger.error(f"Tool execution error: {e}")
                            break

                    tool_messages.append(ToolMessage(
                        content=str(result),
                        tool_call_id=tool_id
                    ))

            return {"messages": tool_messages}

        def should_continue(state: MessagesState) -> str:
            """도구 호출 여부 결정"""
            messages = state["messages"]
            last_message = messages[-1]

            # 도구 호출이 있으면 tools 노드로
            if hasattr(last_message, 'tool_calls') and last_message.tool_calls:
                return "tools"
            # 없으면 종료
            return END

        # 그래프 정의
        graph = StateGraph(MessagesState)
        graph.add_node("agent", agent_node)
        graph.add_node("tools", tools_node)

        graph.add_edge(START, "agent")
        graph.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
        graph.add_edge("tools", "agent")  # 도구 실행 후 다시 에이전트로

        return graph.compile()

    def _get_current_time_info(self) -> str:
        """현재 시간 정보 생성"""
        now_utc = datetime.now(timezone.utc)
        now_kst = now_utc.astimezone(timezone(timedelta(hours=9)))
        utc_str = now_utc.strftime('%Y-%m-%d %H:%M:%S')
        kst_str = now_kst.strftime('%Y-%m-%d %H:%M:%S')
        return f"Current time - UTC: {utc_str} / KST: {kst_str}"

    def _build_system_prompt(self, mcp_context: MCPContext, enforced_time_window: tuple[str, str] | None = None) -> str:
        """시스템 프롬프트 생성 (영어로 작성, 응답은 한국어 지시)"""
        time_info = self._get_current_time_info()

        lines = [
            "You are Olly, an AI assistant for infrastructure observability.",
            "IMPORTANT: Always respond in Korean (한국어).",
            "",
            time_info,
            "",
            "## Tool Usage",
            "",
            "Answer DIRECTLY without tools for general knowledge, concepts, greetings,",
            "or follow-up questions about data already in this conversation.",
            "Use tools ONLY when the user asks about real-time infrastructure state,",
            "specific alarms/incidents, or actual metrics/logs/resource status.",
            "",
            "### MCP Tool Architecture",
            "All external data is accessed through MCP (Model Context Protocol) servers.",
            "You do NOT call APIs or run CLI commands directly. Every tool call goes through",
            "its respective MCP server. The available MCP servers are:",
            "- Grafana MCP: provides metric query tools (dashboards, panels, PromQL, etc.)",
            "- CloudWatch MCP: provides log query tools (log groups, log insights, etc.)",
            "- AWS API MCP: provides AWS resource query tools (EKS, ECS, EC2, RDS, ALB, etc.)",
            "",
            "### Tool Routing Priority (CRITICAL - ALWAYS follow this order)",
            "",
            "Each query type has a MANDATORY priority order. You MUST start with the",
            "highest-priority MCP tool. Only fall back to the next MCP tool if the previous",
            "one returns no data or errors. NEVER skip ahead in the priority chain.",
            "",
            "#### 📈 메트릭(Metrics) — Priority: Grafana MCP → CloudWatch MCP → AWS API MCP",
            "  1st: Grafana MCP (primary metrics source for CPU, memory, disk, network, latency, etc.)",
            "  2nd: CloudWatch MCP (fallback if Grafana MCP has no data for the requested metric)",
            "  3rd: AWS API MCP (last resort for CloudWatch metric-data queries)",
            "- You MUST try Grafana MCP FIRST. Do NOT skip to other MCP tools.",
            "- Only proceed to the next MCP tool after confirming the previous returned no useful data.",
            "",
            "#### 📋 로그(Logs) — Priority: CloudWatch MCP → AWS API MCP",
            "  1st: CloudWatch MCP (primary log source for error messages, stack traces, app logs)",
            "  2nd: AWS API MCP (fallback if CloudWatch MCP fails or is unavailable)",
            "- You MUST try CloudWatch MCP FIRST. Do NOT skip to AWS API MCP for log queries.",
            "",
            "#### 🔧 AWS 자원 조회(Resource Status) — Priority: AWS API MCP → CloudWatch MCP → Grafana MCP",
            "  1st: AWS API MCP (primary source for EKS/ECS, EC2, RDS, ALB, ASG, Lambda, etc.)",
            "  2nd: CloudWatch MCP (fallback for resource-related alarms/events)",
            "  3rd: Grafana MCP (fallback for resource health dashboards)",
            "- You MUST try AWS API MCP FIRST for any AWS resource status query.",
            "",
            "### Priority Enforcement Self-Check",
            "Before EVERY tool call, ask yourself:",
            "1. What type of data am I looking for? (metrics / logs / resource status)",
            "2. Am I using the HIGHEST-PRIORITY MCP tool for that type?",
            "3. Did the higher-priority MCP tool already fail or return no data?",
            "If you haven't tried the higher-priority MCP tool yet → STOP and use it first.",
            "",
            "### Situation-Based Selection",
            "| Situation | Start With | Then Check |",
            "|-----------|-----------|------------|",
            "| OOMKilled / memory | Grafana MCP (memory) → AWS API MCP (task/node) | CloudWatch MCP (logs) |",
            "| High CPU | Grafana MCP (CPU) | CloudWatch MCP → AWS API MCP (capacity) |",
            "| CrashLoopBackOff | CloudWatch MCP (error logs) → Grafana MCP (restarts) | AWS API MCP (events) |",
            "| 5xx / latency | Grafana MCP (error rate, latency) → CloudWatch MCP | AWS API MCP (target health) |",
            "| Node NotReady | AWS API MCP (node group, ASG) → Grafana MCP | CloudWatch MCP (system logs) |",
            "| Deployment failure | AWS API MCP (deploy status) → CloudWatch MCP | Grafana MCP (before/after) |",
            "| Disk pressure | Grafana MCP (disk usage) → AWS API MCP (EBS) | CloudWatch MCP |",
            "| General status | Grafana MCP (dashboards) → AWS API MCP (resource list) | CloudWatch MCP |",
            "",
            "- Extract alert name, severity, resource, timestamp from Prometheus alarms.",
            "- Cross-reference tools when possible. Skip remaining tools if answer is clear.",
            "- Max ~25 tool calls per turn. After 15+ calls without key info, stop and respond.",
            "",
            "## Response Rules",
            "",
            "### Timezone (CRITICAL)",
            "- Assume user times are KST (UTC+9). Convert: KST - 9h = UTC.",
            "- Tool time ranges are enforced by code at ±10 min from alarm time.",
            "- In the report, '분석 기간' MUST exactly match the enforced range (±10 min).",
            "  Example: alarm at 19:00:22 UTC → 분석 기간: 18:50~19:10 (UTC) / 03:50~04:10 (KST)",
            "- NEVER round, extend, or fabricate the analysis time range.",
            "- No specified time → use most recent 30 minutes.",
            "",
            "### Anti-Hallucination (CRITICAL)",
            "- State ONLY facts from tool output. Never fabricate data.",
            "- Tool error/empty result → report honestly, explain what you tried.",
            "- Confirmed data → '확인된 사항:', your interpretation → '분석 의견:'",
            "- No useful data → do NOT produce a fake report. List what you tried.",
            "- Uncertain → '확인 필요'. Never present guesses as facts.",
            "- Discard data outside the relevant time window. Never substitute older data.",
            "",
            "### Tool Output Interpretation",
            "- Read error messages carefully. Do not misinterpret.",
            "- 'Log group created on X' → logs AFTER X should exist.",
            "- 'No data found' → report as-is. Do NOT invent explanations.",
            "- Tool result contradicts your assumption → trust the tool.",
            "",
            "### Statistics (===STATS===)",
            "- ===STATS=== counts are code-computed and 100% accurate.",
            "- NEVER count items yourself. Copy numbers from ===STATS===.",
            "- Table row count MUST match ===STATS=== total.",
            "",
            "## Report Templates",
            "",
            "Use Template A for status/metrics queries, Template B for incidents/errors.",
            "Combine if needed. Always start with a blank line before the heading.",
            "",
            "### Template A: 인프라 현황",
            "```",
            "## 📊 인프라 현황 리포트",
            "",
            "**조회 시간**: YYYY-MM-DD HH:MM (KST) / HH:MM (UTC)",
            "**조회 대상**: (서비스/리소스명)",
            "",
            "### 리소스 요약",
            "| 구분 | 전체 | 정상 | 비정상 |",
            "|------|------|------|--------|",
            "",
            "### 주요 메트릭",
            "| 지표 | 현재값 | 정상 범위 | 상태 |",
            "|------|--------|-----------|------|",
            "",
            "### 특이사항",
            "- (이상 징후 또는 '특이사항 없음')",
            "```",
            "",
            "### Template B: 장애 분석",
            "```",
            "## 🔍 장애 분석 리포트",
            "",
            "**분석 시간**: YYYY-MM-DD HH:MM (KST)",
            "**대상 시스템**: (서비스명)",
            "**분석 기간**: (알람 시각 ±10분, KST/UTC 병기. 코드 강제 범위와 일치해야 함)",
            "",
            "### 현상 요약",
            "(1~2문장)",
            "",
            "### 메트릭 분석",
            "| 지표 | 정상 시 | 장애 시 | 변화율 |",
            "|------|---------|---------|--------|",
            "",
            "### 로그 분석",
            "- `(에러 메시지)` - N회 발생",
            "",
            "### 원인 분석",
            "(확인된 사항 기반 분석)",
            "",
            "### 조치 방안",
            "| 우선순위 | 조치 내용 |",
            "|----------|-----------|",
            "| 🔴 긴급 | (내용) |",
            "| 🟡 권장 | (내용) |",
            "```",
        ]

        base_prompt = "\n".join(lines)

        # 알람 시간 범위가 코드에서 강제되고 있으면 프롬프트에 명시
        if enforced_time_window:
            start_utc, end_utc = enforced_time_window
            # UTC → KST 변환
            start_dt = datetime.strptime(start_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            end_dt = datetime.strptime(end_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            kst = timezone(timedelta(hours=9))
            start_kst = start_dt.astimezone(kst).strftime("%Y-%m-%d %H:%M:%S")
            end_kst = end_dt.astimezone(kst).strftime("%Y-%m-%d %H:%M:%S")
            base_prompt += (
                f"\n\n## ENFORCED TIME RANGE (code-level, cannot be changed)\n"
                f"- UTC: {start_utc} ~ {end_utc}\n"
                f"- KST: {start_kst} ~ {end_kst}\n"
                f"- All tool calls are forced to this range. Your '분석 기간' MUST match this exactly.\n"
                f"- Do NOT write any other time range in the report."
            )

        # 도구 목록 추가
        if mcp_context.tools:
            tool_list = "\n".join(
                [f"- {t.name}: {t.description}" for t in mcp_context.tools]
            )
            base_prompt += "\n\nAvailable tools:\n" + tool_list

        return base_prompt

    # 슬라이딩 윈도우 크기 (최근 N개 메시지는 원문 유지)
    WINDOW_SIZE = 20

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

        response = await self._llm.ainvoke(summary_prompt)

        if isinstance(response.content, str):
            return response.content
        return str(response.content)

    async def _build_hybrid_history(
        self,
        conversation_id: str,
        all_messages: list[dict],
    ) -> list[dict]:
        """하이브리드 메모리: 요약 + 슬라이딩 윈도우 결합"""
        total = len(all_messages)

        # 윈도우 이내면 전체 반환 (요약 불필요)
        if total <= self.WINDOW_SIZE:
            return all_messages

        # 윈도우 밖 메시지 = 요약 대상
        old_messages = all_messages[:-self.WINDOW_SIZE]
        recent_messages = all_messages[-self.WINDOW_SIZE:]

        # 기존 요약 확인
        store = await get_conversation_store()
        existing = await store.get_summary(conversation_id)

        old_count = len(old_messages)

        # 요약이 없거나 새로 요약할 메시지가 있으면 갱신
        if not existing or existing["summarized_until"] < old_count:
            # 기존 요약이 있으면 그 위에 추가 메시지만 요약
            if existing and existing["summarized_until"] > 0:
                new_portion = old_messages[existing["summarized_until"]:]
                combined_text = (
                    f"기존 요약:\n{existing['summary']}\n\n"
                    f"추가 대화:\n"
                    + "\n".join([f"{m['role']}: {m['content']}" for m in new_portion])
                )
                summary_messages = [
                    SystemMessage(content=(
                        "You are a conversation summarizer. "
                        "Merge the existing summary with the new conversation into one concise summary in Korean. "
                        "Preserve key facts, decisions, tool results, and important context. "
                        "Keep the summary under 500 words."
                    )),
                    HumanMessage(content=combined_text),
                ]
                response = await self._llm.ainvoke(summary_messages)
                summary = response.content if isinstance(response.content, str) else str(response.content)
            else:
                summary = await self._summarize_messages(old_messages)

            await store.save_summary(conversation_id, summary, old_count)
            logger.info(f"대화 요약 갱신: conversation_id={conversation_id}, summarized_until={old_count}")
        else:
            summary = existing["summary"]

        # 요약을 system 메시지로 앞에 붙이고 최근 메시지 결합
        hybrid = [
            {"role": "system", "content": f"[이전 대화 요약]\n{summary}"},
        ] + recent_messages

        return hybrid

    def _convert_to_langchain_messages(
        self,
        history: list[dict],
        system_prompt: str,
        images: Optional[list[str]] = None,
    ) -> list:
        """대화 히스토리를 LangChain 메시지 형식으로 변환 (이미지 지원)"""
        messages = []

        # 시스템 프롬프트 추가
        if system_prompt:
            messages.append(SystemMessage(content=system_prompt))

        # 대화 히스토리 변환
        for i, msg in enumerate(history):
            role = msg.get("role", "user")
            content = msg.get("content", "")

            if role == "user":
                # 마지막 user 메시지에 이미지 첨부
                is_last_user = (i == len(history) - 1) and images
                if is_last_user:
                    # 멀티모달 content 구성
                    content_blocks = []
                    for img_data in images:
                        # base64 데이터에서 미디어 타입 추출
                        if img_data.startswith("data:"):
                            # "data:image/png;base64,..." 형식
                            header, b64 = img_data.split(",", 1)
                            media_type = header.split(":")[1].split(";")[0]
                        else:
                            b64 = img_data
                            media_type = "image/png"
                        content_blocks.append({
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{media_type};base64,{b64}",
                            },
                        })
                    content_blocks.append({
                        "type": "text",
                        "text": content,
                    })
                    messages.append(HumanMessage(content=content_blocks))
                else:
                    messages.append(HumanMessage(content=content))
            elif role == "assistant":
                messages.append(AIMessage(content=content))
            elif role == "system":
                messages.append(SystemMessage(content=content))

        return messages

    async def chat_stream(
        self,
        message: str,
        history: Optional[list[dict]] = None,
        conversation_id: Optional[str] = None,
        images: Optional[list[str]] = None,
    ):
        """
        스트리밍 대화 처리 (토큰/도구 호출 정보 AsyncGenerator)

        Yields:
            dict: {"type": "token", "content": "..."} 또는
                  {"type": "tool_start", "name": "...", "args": {...}} 또는
                  {"type": "tool_end", "name": "...", "success": bool}
        """
        await self._ensure_initialized()

        history = history or []

        # 알람 메시지에서 발생 시각 추출 → 도구 시간 범위 강제
        time_window = parse_alarm_time_window(message)
        if time_window:
            # 원본 타임존 정보도 로그에 포함
            tz_match = _ALARM_TIME_PATTERN.search(message)
            original_tz = tz_match.group(3) if tz_match and tz_match.group(3) else "UTC(기본값)"
            original_time = f"{tz_match.group(1)} {tz_match.group(2)}" if tz_match else "?"
            logger.info(f"[시간 강제] 알람 시각 감지: 원본={original_time} {original_tz} → UTC 범위={time_window[0]} ~ {time_window[1]}")
            for tool in self._tools:
                if isinstance(tool, MCPToolWrapper):
                    tool.enforced_time_window = time_window
        else:
            # 알람이 아닌 일반 질문이면 시간 강제 해제
            for tool in self._tools:
                if isinstance(tool, MCPToolWrapper):
                    tool.enforced_time_window = None

        # 메시지에서 계정 식별 → AWS profile 동적 결정
        resolved_profile = self._profile_resolver.resolve(message)
        for tool in self._tools:
            if isinstance(tool, MCPToolWrapper):
                tool.resolved_profile = resolved_profile

        context: MCPContext = await self._mcp_manager.get_context()
        system_prompt = self._build_system_prompt(context, enforced_time_window=time_window)

        current_history = history + [{"role": "user", "content": message}]

        if conversation_id and len(current_history) > self.WINDOW_SIZE:
            current_history = await self._build_hybrid_history(
                conversation_id, current_history
            )

        messages = self._convert_to_langchain_messages(
            current_history,
            system_prompt,
            images=images,
        )

        stream_start = time.monotonic()
        first_token_time = None
        tool_call_count = 0

        try:
            async for event in self._graph.astream_events(
                {"messages": messages},
                config={"recursion_limit": 30},
                version="v2",
            ):
                kind = event.get("event")

                # 도구 호출 시작
                if kind == "on_tool_start":
                    tool_call_count += 1
                    tool_name = event.get("name", "unknown")
                    tool_input = event.get("data", {}).get("input", {})
                    logger.info(f"[성능] 도구 호출 #{tool_call_count}: {tool_name} (경과: {time.monotonic() - stream_start:.1f}s)")
                    yield {
                        "type": "tool_start",
                        "name": tool_name,
                        "args": tool_input,
                    }

                # 도구 호출 완료
                elif kind == "on_tool_end":
                    tool_name = event.get("name", "unknown")
                    output = event.get("data", {}).get("output", "")
                    # 결과를 문자열로 변환
                    if hasattr(output, "content"):
                        result_str = str(output.content)
                    else:
                        result_str = str(output) if output else ""
                    # 에러/빈 결과 판별
                    is_error = (
                        not result_str
                        or "error" in result_str.lower()[:100]
                        or "not found" in result_str.lower()[:100]
                        or len(result_str.strip()) < 5
                    )
                    yield {
                        "type": "tool_end",
                        "name": tool_name,
                        "success": not is_error,
                    }

                # LLM 토큰 스트리밍
                elif kind == "on_chat_model_stream":
                    chunk = event.get("data", {}).get("chunk")
                    if chunk and hasattr(chunk, "content"):
                        content = chunk.content
                        if isinstance(content, str) and content:
                            if first_token_time is None:
                                first_token_time = time.monotonic()
                                logger.info(f"[성능] 첫 토큰 도착: {first_token_time - stream_start:.1f}s (도구 호출 {tool_call_count}회)")
                            yield {"type": "token", "content": content}
                        elif isinstance(content, list):
                            for block in content:
                                if isinstance(block, dict) and block.get("type") == "text":
                                    text = block.get("text", "")
                                    if text:
                                        if first_token_time is None:
                                            first_token_time = time.monotonic()
                                            logger.info(f"[성능] 첫 토큰 도착: {first_token_time - stream_start:.1f}s (도구 호출 {tool_call_count}회)")
                                        yield {"type": "token", "content": text}

            # 정상 완료 시 성능 로그
            total_time = time.monotonic() - stream_start
            logger.info(f"[성능] 스트리밍 완료: {total_time:.1f}s, 도구 호출 {tool_call_count}회")

        except GraphRecursionError:
            logger.warning("도구 호출 횟수 제한(recursion_limit=30)에 도달했습니다.")
            logger.info(f"[성능] 총 소요: {time.monotonic() - stream_start:.1f}s, 도구 호출 {tool_call_count}회 (제한 도달)")
            yield {
                "type": "token",
                "content": (
                    "\n\n---\n"
                    "⚠️ **도구 호출 횟수 제한에 도달하여 분석을 중단합니다.**\n\n"
                    "위 내용은 제한에 도달하기 전까지 수집된 정보를 기반으로 작성되었습니다. "
                    "누락된 정보가 있을 수 있으니, 추가 확인이 필요한 부분은 "
                    "질문의 범위를 좁혀서 다시 질문해 주세요."
                ),
            }

        except Exception as e:
            logger.error(f"Error during chat_stream: {e}")
            raise

    async def chat(
        self,
        message: str,
        history: Optional[list[dict]] = None,
        conversation_id: Optional[str] = None,
    ) -> str:
        """
        대화 처리

        Args:
            message: 사용자 메시지
            history: 이전 대화 히스토리 [{"role": "user"|"assistant", "content": "..."}]
            conversation_id: 대화 ID (하이브리드 메모리 사용 시 필요)

        Returns:
            AI 응답 텍스트
        """
        # 비동기 초기화
        await self._ensure_initialized()

        history = history or []

        # 알람 시간 범위 파싱
        time_window = parse_alarm_time_window(message)

        # 메시지에서 계정 식별 → AWS profile 동적 결정
        resolved_profile = self._profile_resolver.resolve(message)
        for tool in self._tools:
            if isinstance(tool, MCPToolWrapper):
                tool.enforced_time_window = time_window
                tool.resolved_profile = resolved_profile

        # MCP에서 컨텍스트 수집
        context: MCPContext = await self._mcp_manager.get_context()

        # 시스템 프롬프트 생성 (현재 시간 포함)
        system_prompt = self._build_system_prompt(context, enforced_time_window=time_window)

        # 히스토리에 현재 메시지 추가
        current_history = history + [{"role": "user", "content": message}]

        # 하이브리드 메모리 적용 (conversation_id가 있을 때)
        if conversation_id and len(current_history) > self.WINDOW_SIZE:
            current_history = await self._build_hybrid_history(
                conversation_id, current_history
            )

        # LangChain 메시지로 변환
        messages = self._convert_to_langchain_messages(
            current_history,
            system_prompt
        )

        try:
            # LangGraph 워크플로우 실행 (도구 호출 무한 반복 방지)
            result = await self._graph.ainvoke(
                {"messages": messages},
                {"recursion_limit": 30},
            )

            # 마지막 AI 메시지 추출
            ai_messages = [m for m in result["messages"] if isinstance(m, AIMessage)]
            if ai_messages:
                last_ai_msg = ai_messages[-1]
                # content가 문자열인지 확인
                if isinstance(last_ai_msg.content, str):
                    return last_ai_msg.content
                elif isinstance(last_ai_msg.content, list):
                    # 여러 컨텐츠 블록인 경우 텍스트만 추출
                    texts = [c.get("text", "") for c in last_ai_msg.content if isinstance(c, dict) and "text" in c]
                    return "\n".join(texts) if texts else str(last_ai_msg.content)
                return str(last_ai_msg.content)

            return "응답을 생성할 수 없습니다."

        except GraphRecursionError:
            logger.warning("도구 호출 횟수 제한(recursion_limit=30)에 도달했습니다.")
            # 제한 도달 전까지 생성된 AI 메시지가 있으면 활용
            try:
                ai_msgs = [m for m in result["messages"] if isinstance(m, AIMessage)]
                if ai_msgs:
                    partial = ai_msgs[-1].content
                    if isinstance(partial, str) and partial.strip():
                        return (
                            partial + "\n\n---\n"
                            "⚠️ **도구 호출 횟수 제한에 도달하여 분석을 중단합니다.**\n\n"
                            "위 내용은 제한에 도달하기 전까지 수집된 정보입니다. "
                            "추가 확인이 필요하면 질문의 범위를 좁혀서 다시 질문해 주세요."
                        )
            except Exception:
                pass
            return (
                "⚠️ 도구 호출 횟수 제한에 도달하여 분석을 완료하지 못했습니다.\n\n"
                "질문의 범위를 좁혀서 다시 질문해 주세요."
            )

        except Exception as e:
            logger.error(f"Error during chat: {e}")
            raise


# 싱글톤 인스턴스
_agent: Optional[BedrockAgent] = None


async def get_bedrock_agent() -> BedrockAgent:
    """Bedrock 에이전트 싱글톤 반환 (비동기)"""
    global _agent
    if _agent is None:
        _agent = BedrockAgent()
    await _agent._ensure_initialized()
    return _agent
