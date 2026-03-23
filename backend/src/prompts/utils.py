"""
프롬프트 공통 유틸리티
"""
from datetime import datetime, timezone, timedelta


def get_current_time_info() -> str:
    """현재 시간 정보 생성 (UTC / KST)"""
    now_utc = datetime.now(timezone.utc)
    now_kst = now_utc.astimezone(timezone(timedelta(hours=9)))
    return f"Current time - UTC: {now_utc.strftime('%Y-%m-%d %H:%M:%S')} / KST: {now_kst.strftime('%Y-%m-%d %H:%M:%S')}"
