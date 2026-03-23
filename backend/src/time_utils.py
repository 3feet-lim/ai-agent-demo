"""
시간 관련 유틸리티

알람 메시지에서 발생 시각을 추출하고 시간 범위를 계산하는 기능
"""
import re
from datetime import datetime, timezone, timedelta
from loguru import logger


# 알람 메시지에서 발생 시각을 추출하는 정규식
ALARM_TIME_PATTERN = re.compile(
    r"발생\s*시간\s*[:：]\s*(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})\s*(UTC|KST)?",
    re.IGNORECASE,
)

# 시간 파라미터를 가진 도구와 해당 파라미터 이름 매핑
TIME_PARAM_MAP = {
    "execute_log_insights_query": ("start_time", "end_time"),
    "query_prometheus": ("startTime", "endTime"),
}


def parse_alarm_time_window(
    message: str,
    margin_minutes: int = 10,
    max_age_minutes: int = 60,
) -> tuple[str, str] | None:
    """
    사용자 메시지에서 알람 발생 시각을 추출하고 ±margin 범위를 반환.
    발생 시각이 현재로부터 max_age_minutes 이상 지났으면 None 반환.
    """
    m = ALARM_TIME_PATTERN.search(message)
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
