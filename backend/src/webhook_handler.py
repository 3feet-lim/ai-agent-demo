"""
Prometheus Alertmanager Webhook → Agent 분석 → Slack 전송 핸들러
"""
import asyncio
import httpx
import re
import time
from loguru import logger
from typing import Optional

from .config import get_settings


# ── 알람 분석 큐 (동시 3개 처리, 누락 없음) ──────────────────

# 동시에 실행 가능한 알람 분석 최대 수
ANALYSIS_CONCURRENCY_LIMIT = 3

_alert_queue: asyncio.Queue | None = None
_worker_tasks: list[asyncio.Task] = []


def _get_alert_queue() -> asyncio.Queue:
    """알람 큐 싱글톤"""
    global _alert_queue
    if _alert_queue is None:
        _alert_queue = asyncio.Queue()
    return _alert_queue


async def _alert_worker(worker_id: int):
    """알람 분석 워커. 큐에서 작업을 꺼내 순차 처리."""
    # 순환 import 방지를 위해 함수 내부에서 import
    from .bedrock_client import get_bedrock_agent

    queue = _get_alert_queue()
    logger.info(f"[Queue] 워커 #{worker_id} 시작")

    while True:
        try:
            item = await queue.get()
            if item is None:
                # 종료 신호
                queue.task_done()
                break

            alert_message, payload = item
            logger.info(
                f"[Queue] 워커 #{worker_id} 분석 시작 "
                f"(대기 중: {queue.qsize()}건)"
            )

            try:
                agent = await get_bedrock_agent()
                analysis = await agent.chat(alert_message)
                logger.info(f"[Queue] 워커 #{worker_id} 분석 완료: {len(analysis)}자")

                # 분석 완료 후 캐시에 기록
                mark_alert_processed(payload)

                # Slack으로 전송
                await send_to_slack(analysis)

            except Exception as e:
                logger.error(f"[Queue] 워커 #{worker_id} 분석 실패: {e}")

            queue.task_done()

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[Queue] 워커 #{worker_id} 예상치 못한 에러: {e}")


def start_alert_workers():
    """알람 분석 워커 시작 (lifespan에서 호출)"""
    global _worker_tasks
    for i in range(ANALYSIS_CONCURRENCY_LIMIT):
        task = asyncio.create_task(_alert_worker(i))
        _worker_tasks.append(task)
    logger.info(f"[Queue] 알람 분석 워커 {ANALYSIS_CONCURRENCY_LIMIT}개 시작")


async def stop_alert_workers():
    """알람 분석 워커 종료 (lifespan에서 호출)"""
    queue = _get_alert_queue()
    # 종료 신호 전송
    for _ in _worker_tasks:
        await queue.put(None)
    # 워커 종료 대기
    for task in _worker_tasks:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    _worker_tasks.clear()
    logger.info("[Queue] 알람 분석 워커 종료")


# ── 중복 알람 억제 (deduplication) ──────────────────────────────

# {alert_key: last_processed_timestamp}
_alert_dedup_cache: dict[str, float] = {}
# 같은 알람을 다시 분석하지 않는 최소 간격 (초)
ALERT_DEDUP_INTERVAL = 3600  # 1시간


def _make_alert_key(alert: dict) -> str:
    """알람에서 고유 키 생성 (alertname + 주요 리소스 식별자)"""
    labels = alert.get("labels", {})
    parts = [labels.get("alertname", "unknown")]
    # 리소스 식별에 사용되는 주요 라벨들
    for key in ("dimension_InstanceId", "instance_id", "namespace",
                "pod", "function_name", "db_instance_identifier",
                "target_group_arn", "cluster_name"):
        if labels.get(key):
            parts.append(f"{key}={labels[key]}")
    # account + region 구분
    if labels.get("account_id"):
        parts.append(f"account={labels['account_id']}")
    if labels.get("region"):
        parts.append(f"region={labels['region']}")
    return "|".join(parts)


def is_duplicate_alert(payload: dict) -> bool:
    """중복 알람인지 확인. 중복이면 True 반환."""
    now = time.time()
    # 오래된 캐시 정리 (2배 간격 이상 지난 항목 제거)
    expired = [k for k, t in _alert_dedup_cache.items()
               if now - t > ALERT_DEDUP_INTERVAL * 2]
    for k in expired:
        del _alert_dedup_cache[k]

    alerts = payload.get("alerts", [])
    if not alerts:
        return False

    # 모든 알람이 중복인 경우에만 True
    all_duplicate = True
    for alert in alerts:
        key = _make_alert_key(alert)
        last_time = _alert_dedup_cache.get(key)
        if last_time and (now - last_time) < ALERT_DEDUP_INTERVAL:
            logger.info(
                f"[Webhook] 중복 알람 스킵: {key} "
                f"(마지막 분석: {int(now - last_time)}초 전, 간격: {ALERT_DEDUP_INTERVAL}초)"
            )
        else:
            all_duplicate = False

    return all_duplicate


def mark_alert_processed(payload: dict):
    """알람을 처리 완료로 기록"""
    now = time.time()
    for alert in payload.get("alerts", []):
        key = _make_alert_key(alert)
        _alert_dedup_cache[key] = now


def convert_markdown_to_slack_mrkdwn(text: str) -> str:
    """
    표준 마크다운을 Slack mrkdwn 포맷으로 변환.

    변환 규칙:
    - ## 헤더 → *헤더* (볼드)
    - **볼드** → *볼드*
    - [text](url) → <url|text>
    - 마크다운 테이블 → 정렬된 텍스트 리스트
    """
    lines = text.split("\n")
    result = []
    i = 0

    while i < len(lines):
        line = lines[i]

        # 마크다운 테이블 감지 및 변환
        if "|" in line and i + 1 < len(lines) and re.match(r"^\s*\|[\s\-:|]+\|", lines[i + 1]):
            # 헤더 행 파싱
            headers = [h.strip() for h in line.strip().strip("|").split("|")]
            i += 2  # 헤더 + 구분선 건너뜀

            # 데이터 행 파싱
            rows = []
            while i < len(lines) and "|" in lines[i] and lines[i].strip().startswith("|"):
                cols = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                rows.append(cols)
                i += 1

            # 테이블 → bullet list 변환
            for row in rows:
                parts = []
                for h, v in zip(headers, row):
                    if v:
                        parts.append(f"{h}: {v}")
                if parts:
                    result.append("• " + " | ".join(parts))
            continue

        # ## 헤더 → *헤더*
        header_match = re.match(r"^(#{1,6})\s+(.+)$", line)
        if header_match:
            result.append(f"\n*{header_match.group(2)}*")
            i += 1
            continue

        # **볼드** → *볼드*
        line = re.sub(r"\*\*(.+?)\*\*", r"*\1*", line)

        # [text](url) → <url|text>
        line = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"<\2|\1>", line)

        result.append(line)
        i += 1

    return "\n".join(result)


def format_alertmanager_payload(payload: dict) -> str:
    """
    Alertmanager webhook payload를 에이전트가 분석할 수 있는 텍스트로 변환.

    Alertmanager payload 구조:
    {
      "status": "firing" | "resolved",
      "alerts": [
        {
          "status": "firing",
          "labels": {"alertname": "...", "severity": "...", ...},
          "annotations": {"summary": "...", "description": "..."},
          "startsAt": "2024-01-01T00:00:00Z",
          "endsAt": "...",
          "generatorURL": "..."
        }
      ],
      "commonLabels": {...},
      "commonAnnotations": {...},
      "groupLabels": {...}
    }
    """
    alerts = payload.get("alerts", [])
    if not alerts:
        return "알람 데이터가 비어있습니다."

    parts = []
    status = payload.get("status", "unknown")
    parts.append(f"[Prometheus Alert - {status.upper()}]")
    parts.append("")

    for i, alert in enumerate(alerts, 1):
        labels = alert.get("labels", {})
        annotations = alert.get("annotations", {})

        alert_name = labels.get("alertname", "Unknown")
        severity = labels.get("severity", "unknown")
        starts_at = alert.get("startsAt", "")
        alert_status = alert.get("status", status)

        parts.append(f"### 알람 #{i}: {alert_name}")
        parts.append(f"- 상태: {alert_status}")
        parts.append(f"- 심각도: {severity}")
        parts.append(f"- 발생 시각: {starts_at}")

        # annotations에서 summary, description 추출
        if annotations.get("summary"):
            parts.append(f"- 요약: {annotations['summary']}")
        if annotations.get("description"):
            parts.append(f"- 상세: {annotations['description']}")

        # 주요 labels 출력 (alertname, severity 제외)
        extra_labels = {k: v for k, v in labels.items()
                        if k not in ("alertname", "severity")}
        if extra_labels:
            label_str = ", ".join(f"{k}={v}" for k, v in extra_labels.items())
            parts.append(f"- 라벨: {label_str}")

        parts.append("")

    parts.append("위 알람을 분석하고 장애 분석 리포트를 작성해주세요.")

    return "\n".join(parts)


async def send_to_slack(
    text: str,
    webhook_url: Optional[str] = None,
    channel: Optional[str] = None,
) -> bool:
    """
    Slack Incoming Webhook으로 메시지 전송.

    Args:
        text: 전송할 메시지 텍스트
        webhook_url: Slack webhook URL (없으면 설정에서 가져옴)
        channel: Slack 채널 (선택)

    Returns:
        전송 성공 여부
    """
    settings = get_settings()
    url = webhook_url or settings.slack_webhook_url

    if not url:
        logger.error("[Slack] webhook URL이 설정되지 않았습니다.")
        return False

    # 표준 마크다운 → Slack mrkdwn 변환
    slack_text = convert_markdown_to_slack_mrkdwn(text)

    payload = {"text": slack_text}
    if channel or settings.slack_channel:
        payload["channel"] = channel or settings.slack_channel

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code == 200:
                logger.info("[Slack] 메시지 전송 성공")
                return True
            else:
                logger.error(f"[Slack] 전송 실패: {resp.status_code} {resp.text}")
                return False
    except Exception as e:
        logger.error(f"[Slack] 전송 에러: {e}")
        return False
