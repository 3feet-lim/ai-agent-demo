"""
Prometheus Alertmanager Webhook → Agent 분석 → Slack 전송 핸들러
"""
import httpx
from loguru import logger
from typing import Optional

from .config import get_settings


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

    payload = {"text": text}
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
