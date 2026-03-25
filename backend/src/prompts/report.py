"""
리포트 작성 프롬프트

Phase 3: 수집된 데이터를 기반으로 최종 리포트를 작성하는 단계.

analyze의 동적 카테고리(intent, category)에 맞게 리포트 형식을 결정하고,
step별 수집 결과를 구조화하여 리포트 입력을 구성합니다.
"""
from .utils import get_current_time_info


# ── 리포트 형식 레지스트리 (개방-폐쇄 원칙) ──────────────────

# 새 리포트 형식 추가 시 이 딕셔너리에 항목만 추가하면 됨
_REPORT_FORMAT_REGISTRY: dict[str, list[str]] = {
    "incident": [
        "",
        "## Report Format: 🔍 장애 분석 리포트",
        "",
        "### 리포트 최상단에 아래 요약 테이블을 반드시 포함할 것",
        "| 항목 | 내용 |",
        "|------|------|",
        "| 발생일시 | {current_time} |",
        "| 대상 계정 | {account_info} |",
        "| 대상 서비스그룹 | 퍼블릭 샌드박스 관리 시스템 |",
        "| 환경 구분 | 개발 환경 |",
        "| 업무 부서 | 클라우드플랫폼부 안진모 대리 |",
        "| MSP 담당자 | 클라우드플랫폼부 자체 관리 시스템 |",
        "| 대상 자원 정보 | {target_resources} |",
        "| 장애/이슈 현재 Status | (수집 데이터 기반 현재 상황 1~2문장 요약) |",
        "| 원인 분석 | (수집 데이터 기반 근본 원인 1~2문장 요약) |",
        "| 해결방안 가이드 | (수집 데이터 기반 해결 방안 1~2문장 요약) |",
        "",
        "### 상세 리포트 작성 규칙",
        "• 위 요약 테이블과 중복되는 내용은 상세 리포트에 포함하지 않는다.",
        "• 요약에서 다루지 못한 상세 분석만 추가 설명한다.",
        "• 상세 설명이 불필요하면 작성하지 않는다.",
        "",
        "### 상세 섹션 가이드 (필요한 경우에만 작성)",
        "• 메트릭 상세: 수집된 메트릭에서 이상 수치를 구체적으로 인용.",
        "• 로그 상세: 에러/경고 로그의 핵심 메시지를 인용. 타임스탬프 포함.",
        "• 조치 방안 상세: 긴급도별 분류 (🔴긴급 / 🟡권장 / 🟢참고).",
    ],
    "status_list": [
        "",
        "## Report Format: 📋 리소스 목록",
        "🕐 조회 시간 (KST/UTC) → 🎯 대상 (리전, 리소스 유형)",
        "",
        "## 중요: 개별 리소스를 모두 나열할 것",
        "• 수집된 데이터의 각 리소스를 빠짐없이 나열.",
        "• 컬럼 예시: 이름(Name), 인스턴스 ID, 타입, 상태, 프라이빗 IP, 퍼블릭 IP, AZ",
        "• 리소스 유형에 맞게 컬럼을 조정. (예: RDS면 엔진, 스토리지 등)",
        "• 요약/집계(총 N개, 타입 분포 등)는 하지 말 것. 개별 목록이 핵심.",
        "• 마지막에 총 개수만 한 줄로 표시.",
    ],
    "status_summary": [
        "",
        "## Report Format: 📊 인프라 현황 리포트",
        "🕐 조회 시간 (KST/UTC) → 🎯 대상 → 리소스 요약 → 주요 메트릭 → 특이사항",
        "",
        "### 섹션별 작성 가이드",
        "• 리소스 요약: 리소스 유형별 개수, 상태 분포를 테이블로.",
        "• 주요 메트릭: 핵심 지표(CPU, 메모리, 네트워크 등)를 수치와 함께.",
        "• 특이사항: 임계치 초과, 비정상 상태 등 주의가 필요한 항목.",
    ],
}

# intent/category 키워드 → report_type 매핑 규칙
# (키워드, report_type) 순서대로 매칭. 먼저 매칭되는 것이 우선.
_REPORT_TYPE_RULES: list[tuple[list[str], str]] = [
    # 장애/분석 관련
    (["incident", "장애", "분석", "troubleshoot", "debug", "error", "alarm", "alert", "firing"], "incident"),
    # 목록/리스트 관련
    (["list", "목록", "나열", "조회", "전체"], "status_list"),
    # 나머지는 status_summary (기본값)
]


def _resolve_report_type(intent: str, category: str) -> str:
    """analyze 결과의 intent와 category에서 report_type을 결정.

    하드코딩된 if/elif 체인 대신 규칙 기반 매핑을 사용합니다.
    """
    # intent와 category를 합쳐서 검색 대상으로 사용
    search_text = f"{intent} {category}".lower()

    for keywords, report_type in _REPORT_TYPE_RULES:
        for kw in keywords:
            if kw.lower() in search_text:
                return report_type

    return "status_summary"


def build_report_prompt(
    intent: str = "",
    category: str = "",
    report_type: str | None = None,
    account_info: str = "",
    target_resources: str = "",
) -> str:
    """Phase 3: 리포트 작성 프롬프트 (수집된 데이터 기반).

    analyze의 intent/category를 기반으로 리포트 형식을 자동 결정합니다.
    report_type을 직접 지정하면 해당 형식을 사용합니다.

    Args:
        intent: analyze 결과의 intent (사용자 의도 자연어 설명)
        category: analyze 결과의 category (동적 카테고리)
        report_type: 직접 지정 시 사용. None이면 intent/category에서 자동 결정.
        account_info: 대상 계정 정보 (Account ID / Account Name)
        target_resources: 대상 자원 정보 (name, arn 등)
    """
    time_info = get_current_time_info()

    # report_type 결정
    if report_type is None:
        report_type = _resolve_report_type(intent, category)

    base = [
        "You are Olly, an infrastructure report writer.",
        "Always respond in Korean (한국어).",
        "",
        time_info,
        "",
        f"## 사용자 의도: {intent}" if intent else "",
        "",
        "## Rules",
        "• sub-agent 원문을 그대로 복사하지 말 것. 핵심만 요약.",
        "• 🚨 할루시네이션 절대 금지:",
        "  - '수집된 데이터' 섹션에 포함된 내용만 리포트에 사용할 것.",
        "  - '수집 실패 항목'으로 표시된 데이터는 존재하지 않는 것으로 간주.",
        "    해당 영역은 '데이터 수집 실패'로 명시.",
        "  - 수집된 데이터에 특정 메트릭/로그/리소스 정보가 없으면",
        "    '해당 데이터 없음 — 수집되지 않았거나 조회 실패'로 표기.",
        "  - 데이터 없이 원인을 추측하거나 분석 의견을 작성하지 말 것.",
        "  - 확인된 사항 → '확인된 사항:', 불확실 → '확인 필요 (데이터 미수집)'",
        "• 유효 데이터가 전혀 없으면: 수집 실패 사실 + 재시도 안내만 작성.",
        "• 최종 리포트는 3000자 이내로 간결하게.",
        "• 데이터 형태에 따라 적절한 마크다운 포맷을 선택:",
        "  목록/리스트형 데이터는 테이블, 분석/설명은 bullet list.",
        "",
        "## 데이터 인용 규칙",
        "• 수치를 언급할 때는 반드시 출처 step을 명시. 예: '(Step 1: 리소스 조회 결과)'",
        "• 로그 메시지 인용 시 타임스탬프와 로그 그룹을 함께 표기.",
        "• 메트릭 인용 시 쿼리명 또는 지표명과 시간 범위를 표기.",
    ]

    # 빈 문자열 제거
    base = [line for line in base if line is not None]

    # 레지스트리에서 리포트 형식 추가
    format_lines = _REPORT_FORMAT_REGISTRY.get(report_type, [])
    # 플레이스홀더 치환
    replacements = {
        "{current_time}": time_info.split("현재 시각: ")[-1] if "현재 시각: " in time_info else time_info,
        "{account_info}": account_info or "(미확인)",
        "{target_resources}": target_resources or "(수집 데이터에서 확인)",
    }
    for line in format_lines:
        for k, v in replacements.items():
            line = line.replace(k, v)
        base.append(line)

    return "\n".join(base)
