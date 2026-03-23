"""
리소스 검증(resolve) 프롬프트

Phase 1.5: 추출된 식별자가 실제로 존재하는지 sub-agent를 통해 검증
"""


def build_resolve_prompt(extracted: dict, profile: str, region: str) -> str:
    """resolve 단계 LLM 프롬프트: 추출된 식별자를 실제 리소스와 대조"""
    identifiers = extracted.get("identifiers", [])
    identifier_types = extracted.get("identifier_types", {})
    service_hint = extracted.get("service_hint", "general")

    id_lines = []
    for ident in identifiers:
        id_type = identifier_types.get(ident, "unknown")
        id_lines.append(f"  - `{ident}` (추정 타입: {id_type})")

    return "\n".join([
        "You are a resource resolver. 추출된 식별자가 실제로 존재하는지 확인하라.",
        "sub-agent 도구를 사용하여 검증하고, 결과를 JSON으로 반환하라.",
        "Always respond in Korean.",
        "",
        f"AWS profile: {profile}",
        f"AWS region: {region}",
        f"service_hint: {service_hint}",
        "",
        "## 검증할 식별자:",
        "\n".join(id_lines) if id_lines else "  (없음 — 전체 현황 조회)",
        "",
        "## 검증 방법",
        "1. identifier_type이 'cluster'인 경우:",
        "   → check_resources에 'aws eks describe-cluster --name {이름}' 요청",
        "2. identifier_type이 'pod'인 경우:",
        "   → collect_metrics에 'kube_pod_info{pod=~\"{이름}.*\"} 쿼리로 cluster 라벨 확인' 요청",
        "   → cluster 라벨에서 클러스터명을 추출한 뒤, 해당 클러스터를 check_resources로 존재 확인",
        "3. identifier_type이 'instance'인 경우:",
        "   → check_resources에 'aws ec2 describe-instances --instance-ids {이름}' 요청",
        "4. identifier_type이 'db'인 경우:",
        "   → check_resources에 'aws rds describe-db-instances --db-instance-identifier {이름}' 요청",
        "5. identifier_type이 'unknown'이고 service_hint가 'eks'인 경우:",
        "   → 먼저 클러스터로 검증 시도. 실패하면 pod로 간주하여 2번 방법 시도.",
        "",
        "## 출력 형식",
        "검증이 끝나면 반드시 아래 JSON 형식으로만 응답하라. 설명 없이 JSON만.",
        "```json",
        '{',
        '  "targets": [',
        '    {"type": "cluster|instance|db|function", "name": "확정된 리소스명", "pod_filter": "pod명 또는 null"}',
        '  ],',
        '  "failed": [',
        '    {"name": "식별자", "type": "추정타입", "detail": "실패 사유"}',
        '  ]',
        '}',
        "```",
        "",
        "## 규칙",
        "- pod_filter: pod 이름으로 검색해서 클러스터를 찾은 경우, 해당 pod명을 pod_filter에 넣을 것.",
        "- 식별자가 없으면 targets와 failed 모두 빈 배열로 반환.",
        "- 검증 결과만 반환. 분석/리포트 작성 금지.",
    ])
