"""
시간 관련 유틸리티

알람 메시지에서 발생 시각을 추출하고 시간 범위를 계산하는 기능
"""
import re
from datetime import datetime, timezone, timedelta
from loguru import logger


# 알람 메시지에서 발생 시각을 추출하는 정규식
# "발생 시간", "발생시간", "발생시각", "발생 시각" 모두 매칭
# 날짜-시간 구분자: 공백 또는 T 허용
ALARM_TIME_PATTERN = re.compile(
    r"발생\s*(?:시간|시각)\s*[:：]\s*(\d{4}-\d{2}-\d{2})[\sT](\d{2}:\d{2}:\d{2})\s*(UTC|KST)?",
    re.IGNORECASE,
)

# ISO 8601 형식의 startsAt / 일반 날짜-시간 패턴 (공백 또는 T 구분자)
_STARTS_AT_PATTERN = re.compile(
    r"(\d{4}-\d{2}-\d{2})[\sT](\d{2}:\d{2}:\d{2})(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?",
)

# "UTC: ... / KST: ..." 형식 패턴
_UTC_KST_PATTERN = re.compile(
    r"UTC\s*[:：]\s*(\d{4}-\d{2}-\d{2})[\sT](\d{2}:\d{2}:\d{2})"
    r"\s*/\s*"
    r"KST\s*[:：]\s*(\d{4}-\d{2}-\d{2})[\sT](\d{2}:\d{2}:\d{2})",
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


# KST 타임존 상수
_KST = timezone(timedelta(hours=9))


def extract_alert_starts_at(message: str) -> str | None:
    """알람 메시지에서 발생 시각을 추출하여 'UTC: ... / KST: ...' 형식으로 반환.

    매칭 우선순위:
      1. _UTC_KST_PATTERN — 이미 UTC/KST 쌍이 명시된 경우 그대로 사용
      2. ALARM_TIME_PATTERN — '발생 시간' / '발생시각' 레이블 뒤의 시각
      3. _STARTS_AT_PATTERN — ISO 8601 등 일반 날짜-시간 (폴백)

    타임존 미지정 시 UTC로 간주하여 KST 변환 후 반환합니다.

    Args:
        message: 알람 원문 메시지

    Returns:
        'UTC: YYYY-MM-DD HH:MM:SS / KST: YYYY-MM-DD HH:MM:SS' 형식 문자열.
        매칭 실패 시 None.
    """
    # 1) 이미 UTC/KST 쌍이 있는 경우
    m = _UTC_KST_PATTERN.search(message)
    if m:
        utc_str = f"{m.group(1)} {m.group(2)}"
        kst_str = f"{m.group(3)} {m.group(4)}"
        return f"UTC: {utc_str} / KST: {kst_str}"

    # 2) '발생 시간' / '발생시각' 레이블 매칭
    m = ALARM_TIME_PATTERN.search(message)
    if m:
        date_str, time_str, tz_str = m.group(1), m.group(2), m.group(3)
        return _format_utc_kst(date_str, time_str, tz_str)

    # 3) 일반 날짜-시간 폴백
    m = _STARTS_AT_PATTERN.search(message)
    if m:
        date_str, time_str = m.group(1), m.group(2)
        return _format_utc_kst(date_str, time_str, None)

    return None


def _format_utc_kst(date_str: str, time_str: str, tz_str: str | None) -> str:
    """날짜/시간/타임존 문자열을 'UTC: ... / KST: ...' 형식으로 변환.

    Args:
        date_str: 'YYYY-MM-DD' 형식 날짜
        time_str: 'HH:MM:SS' 형식 시간
        tz_str: 'UTC', 'KST', 또는 None (None이면 UTC로 간주)
    """
    dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S")

    if tz_str and tz_str.upper() == "KST":
        dt = dt.replace(tzinfo=_KST)
    else:
        # 타임존 미지정 또는 UTC → UTC로 간주
        dt = dt.replace(tzinfo=timezone.utc)

    utc_dt = dt.astimezone(timezone.utc)
    kst_dt = dt.astimezone(_KST)
    return (
        f"UTC: {utc_dt.strftime('%Y-%m-%d %H:%M:%S')} / "
        f"KST: {kst_dt.strftime('%Y-%m-%d %H:%M:%S')}"
    )
