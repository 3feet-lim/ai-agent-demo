"""
실행 계획 수립 프롬프트 (plan_node)

analyze 결과와 resolve 결과를 기반으로
어떤 sub-agent를 어떤 순서로 실행할지 LLM이 계획을 세웁니다.
"""


def build_plan_prompt(
    analyze_result: dict,
    resolve_result: dict | None = None,
) -> str:
    """실행 계획 수립 프롬프트.

    LLM이 sub-agent 실행 순서를 결정하는 JSON 계획을 생성하도록 유도합니다.
    depends_on이 null인 step들은 병렬 실행 가능하고,
    같은 step 내의 agents도 병렬 실행됩니다.

    Args:
        analyze_result: analyze_node의 출력 (intent, category, collection_types 등)
        resolve_result: resolve_node의 출력 (targets, profile 등). 없으면 None.
    """
    # analyze 결과에서 핵심 정보 추출
    intent = analyze_result.get("intent", "")
    category = analyze_result.get("category", "")
    collection_types = analyze_result.get("collection_types", [])
    service_hint = analyze_result.get("service_hint", "general")
    identifiers = analyze_result.get("identifiers", [])
    time_range = analyze_result.get("time_range")

    # resolve 결과에서 타겟 정보 추출
    targets = []
    profile = "default"
    regions = []
    if resolve_result:
        targets = resolve_result.get("targets", [])
        profile = resolve_result.get("profile", "default")
        regions = resolve_result.get("regions", [])

    # 컨텍스트 섹션 구성
    context_lines = [
        "## CONTEXT",
        f"- intent: {intent}",
        f"- category: {category}",
        f"- service_hint: {service_hint}",
        f"- collection_types: {collection_types}",
        f"- profile: {profile}",
    ]
    if identifiers:
        context_lines.append(f"- identifiers: {identifiers}")
    if regions:
        context_lines.append(f"- regions: {regions}")
    if time_range:
        context_lines.append(f"- time_range: {time_range}")

    if targets:
        context_lines.append("")
        context_lines.append("## RESOLVED TARGETS (확정된 리소스)")
        for t in targets:
            t_type = t.get("type", "unknown")
            t_name = t.get("name", "unknown")
            pod_filter = t.get("pod_filter")
            line = f"- {t_type}: {t_name}"
            if pod_filter:
                line += f" (pod_filter: {pod_filter})"
            context_lines.append(line)

    context_section = "\n".join(context_lines)

    return "\n".join([
        "You are an execution planner for infrastructure analysis.",
        "Based on the context below, create a step-by-step execution plan.",
        "Return ONLY a JSON object. No explanation, no markdown fence.",
        "",
        context_section,
        "",
        "## AVAILABLE AGENTS",
        '- "resource": AWS 리소스 상태 확인 (EC2, EKS, RDS, ALB, Lambda 등). call_aws 사용.',
        '- "metric": Grafana PromQL 메트릭 수집. query_prometheus 사용.',
        '- "log": CloudWatch Logs 로그 수집. filter_log_events, start_query 등 사용.',
        '- "network": 네트워크 문제 조사 (VPC, TGW, SG, NACL). call_aws 사용.',
        "",
        "## OUTPUT SCHEMA",
        "{",
        '  "steps": [',
        "    {",
        '      "step_id": 0,',
        '      "agents": ["resource"],',
        '      "purpose": "이 단계의 목적을 한국어로 설명",',
        '      "task_template": "sub-agent에게 전달할 구체적인 작업 지시 (한국어)",',
        '      "depends_on": null',
        "    },",
        "    {",
        '      "step_id": 1,',
        '      "agents": ["log", "metric"],',
        '      "purpose": "로그 + 메트릭 병렬 수집",',
        '      "task_template": "...",',
        '      "depends_on": 0',
        "    }",
        "  ]",
        "}",
        "",
        "## PLANNING RULES",
        "",
        "### 의존성 규칙",
        "- depends_on이 null인 step들은 서로 독립적이므로 병렬 실행 가능.",
        "- depends_on이 있으면 해당 step 완료 후에만 실행.",
        "- 같은 step 내의 agents는 병렬 실행됨.",
        "",
        "### 순서 결정 기준",
        "- 로그/메트릭 수집 전에 관련 리소스 정보를 먼저 조회해야 한다.",
        "  resource agent로 대상 리소스의 상세 정보와 관련 리소스(로그 그룹, 노드그룹 등)를 먼저 확인.",
        "  그 결과를 다음 step의 log/metric agent에 전달하여 정확한 리소스명으로 조회하도록 한다.",
        "  예: EKS 장애 → resource(클러스터 상태 + 로그 그룹 조회) → log + metric 병렬.",
        "- 🚨 로그 그룹 조회 시 prefix를 추측하지 말 것.",
        "  describe-log-groups 호출 시 log_group_name_prefix에 특정 경로 패턴을 넣지 말고,",
        "  클러스터/리소스 이름을 키워드로 검색하거나 prefix 없이 전체 조회 후 필터링할 것.",
        "  task_template 예시: 'describe-log-groups를 호출하여 {리소스명}과 관련된 로그 그룹을 찾으세요. prefix를 추측하지 말고 넓은 범위로 조회하세요.'",
        "- 리소스 ID를 이미 알고 있는 경우: 바로 해당 agent 호출.",
        "- 네트워크 문제: resource(VPC/서브넷 정보) → network(경로 조사) 순서.",
        "",
        "### agent 선택 기준",
        "- collection_types에 포함된 agent만 사용. 불필요한 agent는 계획에 포함하지 말 것.",
        "- 장애 분석(incident_analysis): 보통 resource → log + metric 순서.",
        "- 리소스 목록 조회(resource_lookup): resource 1개 step만.",
        "- 메트릭 조회: metric 1개 step만.",
        "- 네트워크 문제: resource → network 순서.",
        "",
        "### task_template 작성 규칙",
        "- 조회할 리소스 이름/ID를 구체적으로 명시.",
        "- 시간 범위가 있으면 포함.",
        "- 리전, 프로필 정보 포함.",
        "- 이전 step의 결과가 필요한 경우 '이전 단계에서 확인된 {정보}를 사용하여' 형태로 작성.",
        "  (실행 시 이전 step의 실제 결과가 주입됨)",
        "",
        "### 효율성 규칙",
        "- step 수를 최소화. 불필요한 분할 금지.",
        "- 의존성이 없는 agent들은 같은 step에 묶어 병렬 실행.",
        "- 최대 4개 step까지만 허용.",
        "",
        "## EXAMPLES",
        "",
        "### EKS 클러스터 장애 분석",
        '{"steps": [',
        '  {"step_id": 0, "agents": ["resource"], "purpose": "EKS 클러스터 상태 조회 및 관련 로그 그룹 탐색", '
        '"task_template": "EKS 클러스터 my-cluster의 상태를 확인하세요. '
        'describe-cluster, list-nodegroups, describe-nodegroup을 호출하세요. '
        '그리고 describe-log-groups를 호출하여 my-cluster와 관련된 로그 그룹을 찾으세요. '
        'prefix를 추측하지 말고 log_group_name_prefix 없이 호출하거나 클러스터 이름만으로 조회하세요. '
        'profile: default, region: ap-northeast-2", "depends_on": null},',
        '  {"step_id": 1, "agents": ["log", "metric"], "purpose": "로그 수집 + 메트릭 수집 (병렬)", '
        '"task_template": "이전 단계에서 확인된 정확한 로그 그룹 이름을 사용하여 최근 30분간 에러 로그를 검색하세요. '
        '로그 그룹 이름을 추측하지 말고 이전 단계 결과에서 확인된 이름만 사용하세요. '
        '메트릭은 CPU, 메모리, Pod 재시작 횟수를 조회하세요. cluster=my-cluster", "depends_on": 0}',
        "]}",
        "",
        "### EC2 인스턴스 목록 조회",
        '{"steps": [',
        '  {"step_id": 0, "agents": ["resource"], "purpose": "EC2 인스턴스 전체 목록 조회", '
        '"task_template": "EC2 인스턴스 전체 목록을 조회하세요. '
        '각 인스턴스의 이름, ID, 타입, 상태, IP, AZ를 개별 나열하세요. '
        'profile: default, region: ap-northeast-2", "depends_on": null}',
        "]}",
        "",
        "### VPC 간 통신 문제",
        '{"steps": [',
        '  {"step_id": 0, "agents": ["resource"], "purpose": "VPC 및 서브넷 기본 정보 조회", '
        '"task_template": "vpc-abc와 vpc-def의 상세 정보를 조회하세요. '
        '서브넷, 라우팅 테이블, 피어링/TGW 연결 상태를 확인하세요.", "depends_on": null},',
        '  {"step_id": 1, "agents": ["network"], "purpose": "네트워크 경로 및 보안 규칙 조사", '
        '"task_template": "이전 단계에서 확인된 VPC 정보를 기반으로 '
        '라우팅, 보안 그룹, NACL을 조사하세요.", "depends_on": 0}',
        "]}",
    ])
