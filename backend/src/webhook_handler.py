"""
Prometheus Alertmanager Webhook → Agent 분석 → Slack 전송 핸들러
"""
import httpx
import re
from loguru import logger
from typing import Optional

from .config import get_settings


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
