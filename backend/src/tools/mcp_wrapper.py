"""
MCPToolWrapper — MCP 도구를 LangChain BaseTool로 래핑

가드레일, 프로필 주입, 통계 보강, 응답 후처리를 포함합니다.
"""
import json
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Optional, Any

from langchain_core.tools import BaseTool
from loguru import logger
from pydantic import BaseModel

from ..mcp_manager import MCPTool
from ..time_utils import TIME_PARAM_MAP
from .schema_utils import _resolve_schema_type, create_pydantic_model_from_schema


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
    "list_prometheus_label_values": "Prometheus 라벨 값 조회",
    "list_prometheus_metric_names": "Prometheus 메트릭 목록 조회",
}

# call_aws의 "aws <service> <action>" → 비전문가용 설명
_AWS_CLI_DISPLAY = {
    "eks describe-cluster": "EKS 클러스터 정보 조회",
    "eks describe-nodegroup": "EKS 노드그룹 정보 조회",
    "eks list-nodegroups": "EKS 노드그룹 목록 조회",
    "eks list-clusters": "EKS 클러스터 목록 조회",
    "ec2 describe-instances": "EC2 인스턴스 정보 조회",
    "ec2 describe-security-groups": "보안 그룹 정보 조회",
    "ec2 describe-subnets": "서브넷 정보 조회",
    "ec2 describe-vpcs": "VPC 정보 조회",
    "elbv2 describe-target-health": "로드밸런서 대상 상태 확인",
    "elbv2 describe-load-balancers": "로드밸런서 정보 조회",
    "elbv2 describe-target-groups": "로드밸런서 대상 그룹 조회",
    "rds describe-db-instances": "RDS 데이터베이스 정보 조회",
    "rds describe-db-clusters": "RDS 클러스터 정보 조회",
    "autoscaling describe-auto-scaling-groups": "오토스케일링 그룹 조회",
    "cloudwatch describe-alarms": "CloudWatch 알람 조회",
    "s3 ls": "S3 버킷 목록 조회",
    "iam get-role": "IAM 역할 정보 조회",
    "sts get-caller-identity": "AWS 계정 정보 확인",
}


def _get_display_name(tool_name: str, kwargs: dict) -> str:
    """도구명과 파라미터로부터 비전문가용 설명 생성"""
    if tool_name == "call_aws":
        cli_cmd = str(kwargs.get("cli_command", ""))
        m = re.match(r"aws\s+(\S+)\s+(\S+)", cli_cmd)
        if m:
            key = f"{m.group(1)} {m.group(2)}"
            if key in _AWS_CLI_DISPLAY:
                return _AWS_CLI_DISPLAY[key]
            return f"AWS {m.group(1)} {m.group(2)}"
        return "AWS CLI 명령 실행"
    return _MCP_TOOL_DISPLAY.get(tool_name, tool_name)


class MCPToolWrapper(BaseTool):
    """MCP 도구를 LangChain BaseTool로 래핑"""
    name: str
    description: str
    args_schema: type[BaseModel]
    mcp_tool: MCPTool
    mcp_manager: Any
    enforced_time_window: Optional[tuple[str, str]] = None
    resolved_profile: Optional[str] = None
    allowed_clusters: Optional[list[str]] = None
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
        """describe_log_groups 응답에서 storedBytes 필드를 제거."""
        try:
            data = json.loads(raw)
            changed = False
            if isinstance(data, dict):
                for key, val in data.items():
                    if isinstance(val, list):
                        for item in val:
                            if isinstance(item, dict) and "storedBytes" in item:
                                del item["storedBytes"]
                                changed = True
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
            stripped = re.sub(r',?\s*"storedBytes"\s*:\s*\d+', '', raw)
            if stripped != raw:
                logger.info("[storedBytes 제거] 정규식으로 storedBytes 필드 제거 완료")
                return stripped
        return raw

    def _coerce_list_params(self, kwargs: dict) -> dict:
        """array 타입 파라미터를 LLM이 문자열로 보낸 경우 리스트로 변환."""
        original_schema = self.mcp_tool.input_schema or {}
        properties = original_schema.get("properties", {})
        for key, val in kwargs.items():
            if not isinstance(val, str):
                continue
            prop_schema = properties.get(key, {})
            if _resolve_schema_type(prop_schema) != "array":
                continue
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
            kwargs[key] = [val]
            logger.info(f"[타입 변환] {self.name}.{key}: 단일 문자열 → 리스트 래핑")
        return kwargs

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
            if "region" in kwargs:
                from ..config import get_settings
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
                from ..config import get_settings
                kwargs["cli_command"] = f"{kwargs['cli_command']} --region {get_settings().aws_region}"
                logger.info(f"[Region 주입] {self.name}: --region {get_settings().aws_region}")

    _BLOCKED_AWS_COMMANDS = [
        "aws cloudwatch get-metric", "aws cloudwatch list-metrics",
        "aws logs filter-log-events", "aws logs get-log-events",
        "aws logs start-query", "aws logs get-query-results",
        "aws logs describe-log-streams",
    ]

    async def _arun(self, **kwargs) -> str:
        """비동기 도구 실행"""
        try:
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

            _LOG_QUERY_MAX_LIMIT = 100
            if self.name in ("execute_log_insights_query", "get_logs_insight_query_results"):
                raw_limit = kwargs.get("limit")
                if raw_limit is not None:
                    try:
                        int_limit = int(raw_limit)
                        if int_limit > _LOG_QUERY_MAX_LIMIT:
                            logger.info(f"[Limit 강제] {self.name}: limit {int_limit} → {_LOG_QUERY_MAX_LIMIT}")
                            kwargs["limit"] = str(_LOG_QUERY_MAX_LIMIT)
                    except (ValueError, TypeError):
                        pass
                else:
                    kwargs["limit"] = str(_LOG_QUERY_MAX_LIMIT)
                    logger.info(f"[Limit 강제] {self.name}: limit 미지정 → {_LOG_QUERY_MAX_LIMIT}")

            if self.allowed_clusters and self.name == "query_prometheus":
                expr = str(kwargs.get("expr", ""))
                has_allowed = any(c in expr for c in self.allowed_clusters)
                if not has_allowed and len(self.allowed_clusters) == 1:
                    cluster = self.allowed_clusters[0]
                    if "{" in expr:
                        injected = expr.replace("}", f', ClusterName="{cluster}"}}', 1)
                    else:
                        import re as _re
                        injected = _re.sub(
                            r'([a-zA-Z_:][a-zA-Z0-9_:]*)(?!\{)(?=\s*[\)\s,\[]|$)',
                            rf'\1{{ClusterName="{cluster}"}}',
                            expr, count=1,
                        )
                    if injected != expr:
                        logger.info(
                            f"[가드레일 자동주입] {self.name}: ClusterName=\"{cluster}\" 주입\n"
                            f"  before: {expr[:200]}\n  after:  {injected[:200]}"
                        )
                        kwargs["expr"] = injected
                    else:
                        logger.warning(f"[가드레일] ClusterName 자동주입 실패, 원본 쿼리 그대로 실행: {expr[:200]}")

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
                        logger.info(f"[시간 강제] {self.name}: {original_start}~{original_end} → {enforced_start}~{enforced_end}")

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

            kwargs = {k: v for k, v in kwargs.items() if v is not None}
            kwargs = self._coerce_list_params(kwargs)
            kwargs.pop("ctx", None)

            logger.info(f"MCP tool {self.name} called with: {kwargs}")
            if self.event_queue:
                self.event_queue.put_nowait({
                    "type": "mcp_tool_start", "name": self.name,
                    "display": _get_display_name(self.name, kwargs),
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

            server_name = self.mcp_tool.server_name if hasattr(self.mcp_tool, 'server_name') else ""
            if server_name == "cloudwatch":
                logger.info(f"MCP tool {self.name} result ({len(raw)}자): {raw[:500]}")
            else:
                logger.debug(f"MCP tool {self.name} result ({len(raw)}자): {raw[:500]}")

            if self.name == "describe_log_groups":
                raw = self._strip_stored_bytes(raw)

            enriched = self._enrich_with_stats(raw)

            MAX_TOOL_RESPONSE_CHARS = 15000
            if len(enriched) > MAX_TOOL_RESPONSE_CHARS:
                logger.warning(f"[Truncation] {self.name} 응답 잘림: {len(enriched):,}자 → {MAX_TOOL_RESPONSE_CHARS:,}자")
                enriched = (
                    enriched[:MAX_TOOL_RESPONSE_CHARS]
                    + "\n\n...[⚠️ 응답이 너무 길어 잘렸습니다. 필요 시 범위를 좁혀 다시 조회하세요.]"
                )
            if self.event_queue:
                self.event_queue.put_nowait({
                    "type": "mcp_tool_end", "name": self.name, "success": True,
                    "display": _get_display_name(self.name, kwargs),
                })
            return enriched
        except Exception as e:
            logger.error(f"Tool execution error for {self.name}: {e}")
            if self.event_queue:
                self.event_queue.put_nowait({
                    "type": "mcp_tool_end", "name": self.name, "success": False,
                    "display": _get_display_name(self.name, kwargs),
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
