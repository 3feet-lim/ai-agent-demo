"""
수집 결과 평가 프롬프트 (evaluate_node)

execute 결과를 평가하여 추가 조사가 필요한지 판단합니다.
무한 루프 방지를 위해 이전 수행 이력을 함께 제공합니다.
"""


def build_evaluate_prompt(
    user_msg: str,
    intent: str,
    category: str,
    targets: list[dict],
    collected_summary: str,
    failed_summary: str,
    executed_history: list[dict],
) -> str:
    """수집 결과 평가 프롬프트.

    LLM이 수집된 데이터의 충분성을 판단하고,
    부족하면 구체적 근거와 함께 추가 조사 계획을 반환합니다.

    Args:
        user_msg: 사용자 원본 메시지
        intent: analyze 결과의 intent
        category: analyze 결과의 category
        targets: 확정된 타겟 리소스 목록
        collected_summary: 수집된 데이터 요약 (step별)
        failed_summary: 수집 실패 항목 요약
        executed_history: 이전까지 수행된 모든 조사 이력
            [{"round": 1, "agents": ["resource", "log"], "purpose": "...", "findings": "..."}]
    """
    # 타겟 정보 문자열
    target_lines = []
    for t in targets:
        t_type = t.get("type", "unknown")
        t_name = t.get("name", "unknown")
        target_lines.append(f"- {t_type}: `{t_name}`")
    target_section = "\n".join(target_lines) if target_lines else "(전체 조회)"

    # 이전 수행 이력 문자열
    history_lines = []
    if executed_history:
        for h in executed_history:
            round_num = h.get("round", "?")
            agents = h.get("agents", [])
            purpose = h.get("purpose", "")
            findings = h.get("findings", "")
            history_lines.append(
                f"### Round {round_num}: {', '.join(agents)}\n"
                f"- 목적: {purpose}\n"
                f"- 결과 요약: {findings}"
            )
    history_section = "\n\n".join(history_lines) if history_lines else "(첫 번째 수집)"

    return "\n".join([
        "You are an investigation evaluator for infrastructure incident analysis.",
        "수집된 데이터가 사용자의 요청에 답하기에 충분한지 평가하라.",
        "Return ONLY a JSON object. No explanation, no markdown fence.",
        "",
        "## 사용자 요청",
        user_msg,
        "",
        f"## 분석 의도: {intent}",
        f"## 카테고리: {category}",
        "",
        "## 분석 대상",
        target_section,
        "",
        "## 현재까지 수집된 데이터",
        collected_summary,
        "",
        "## 수집 실패 항목",
        failed_summary if failed_summary else "(없음)",
        "",
        "## 이전 수행 이력 (이미 조사한 내용)",
        history_section,
        "",
        "## OUTPUT SCHEMA",
        "{",
        '  "sufficient": true/false,',
        '  "reasoning": "판단 근거를 한국어로 2~3문장으로 설명",',
        '  "additional_investigation": null 또는 {',
        '    "agents": ["추가로 호출할 agent 목록"],',
        '    "purpose": "추가 조사의 구체적 목적",',
        '    "task_hint": "sub-agent에게 전달할 구체적 지시사항",',
        '    "evidence": "추가 조사가 필요한 근거 (수집된 데이터에서 발견한 단서)"',
        "  }",
        "}",
        "",
        "## 판단 규칙",
        "",
        "### sufficient = true (추가 조사 불필요)인 경우:",
        "- 사용자 요청에 답할 수 있는 데이터가 충분히 수집된 경우",
        "- 리소스 목록 조회 등 단순 조회 요청이고 결과가 있는 경우",
        "- 장애 원인이 수집된 데이터에서 명확히 드러나는 경우",
        "- 모든 관련 영역을 이미 조사했고 추가로 볼 곳이 없는 경우",
        "- 수집 실패가 있더라도, 성공한 데이터만으로 답변 가능한 경우",
        "",
        "### sufficient = false (추가 조사 필요)인 경우:",
        "- 수집된 데이터에서 원인의 단서가 발견되었지만, 확인하려면 다른 영역 조사가 필요한 경우",
        "  예: 로그에서 'connection refused to 10.0.1.5' 발견 → network agent로 SG/NACL 조사 필요",
        "  예: 메트릭에서 OOM 확인 → 로그에서 구체적 에러 메시지 확인 필요",
        "- 핵심 데이터가 수집되지 않았고, 다른 경로로 수집 가능한 경우",
        "  예: CloudWatch 로그 수집 실패 → Container Insights 로그로 재시도 가능",
        "",
        "### sufficient = false가 되면 안 되는 경우 (무한 루프 방지):",
        "- '이전 수행 이력'에 이미 동일한 agent + 동일한 목적의 조사가 있는 경우",
        "  → 같은 조사를 반복하면 안 됨. 이 경우 sufficient = true로 판단.",
        "- additional_investigation의 agents가 이전 이력에서 이미 사용된 agent와 같고,",
        "  새로운 조사 대상(다른 로그 그룹, 다른 메트릭, 다른 네트워크 경로)이 없는 경우",
        "  → sufficient = true로 판단.",
        "- evidence가 수집된 데이터에서 구체적으로 인용할 수 없는 경우",
        "  → '혹시 모르니까 더 봐보자'는 근거가 아님. sufficient = true로 판단.",
        "",
        "## EXAMPLES",
        "",
        "### 충분한 경우",
        '{"sufficient": true, "reasoning": "EKS 클러스터 상태, Pod 메트릭, 로그를 모두 수집했고 OOM 원인이 메모리 limit 초과로 확인됨.", "additional_investigation": null}',
        "",
        "### 추가 조사 필요한 경우",
        '{"sufficient": false, "reasoning": "로그에서 connection timeout to 10.0.1.5:3306 에러가 반복 발견되었으나, 해당 IP의 보안 그룹과 네트워크 경로를 아직 확인하지 않았음.", "additional_investigation": {"agents": ["network"], "purpose": "10.0.1.5:3306 연결 실패의 네트워크 원인 조사", "task_hint": "10.0.1.5가 속한 서브넷의 보안 그룹, NACL, 라우팅 테이블을 확인하고 3306 포트 허용 여부를 조사하세요.", "evidence": "로그에서 connection timeout to 10.0.1.5:3306 에러가 14:20~14:25 사이 23회 발생"}}',
    ])
