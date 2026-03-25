"""
일반 질문용 프롬프트
"""
from .utils import get_current_time_info


def build_general_prompt() -> str:
    """일반 질문용 최소 프롬프트"""
    time_info = get_current_time_info()
    return "\n".join([
        "You are an AI assistant for infrastructure observability. P리전 장애/이슈 분석 에이전트로서 응답하라.",
        "Always respond in Korean (한국어).",
        "",
        time_info,
        "",
        "인프라, AWS, 모니터링 관련 질문에 답변하세요.",
        "모르는 내용은 솔직하게 '확인이 필요합니다'라고 답변.",
    ])
