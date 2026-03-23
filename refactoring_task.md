# 시스템 프롬프트 & Sub-Agent 실행 로직 리팩토링 계획

## 현재 아키텍처 요약

```
사용자 메시지
  → extract_node (리소스 식별자 추출 - LLM 호출 1회)
  → classify_node (질문 유형 분류 - LLM 호출 1회, 하드코딩 4종)
  → route_after_classify
    ├─ general → direct_answer_node → END
    └─ incident/status_list/status_summary → resolve_node (LLM + sub-agent로 검증)
        → route_after_resolve
          ├─ 전부 실패 → validation_fail → END
          └─ collect_setup → collect_agent ↔ collect_tools (루프)
              → report_setup → report_llm → END
```

### 현재 문제점
1. 분류 카테고리 하드코딩 (`incident`, `status_list`, `status_summary`, `general`)
2. extract + classify가 별도 LLM 호출 (토큰 낭비)
3. sub-agent 간 정보 전달 불가 (병렬만 가능, 순차 의존성 처리 못함)
4. 어떤 sub-agent를 호출할지의 판단 근거가 프롬프트에 약함
5. 프롬프트가 함수 내 하드코딩 (유지보수 어려움)
6. 원본 메시지가 중간에 변형될 가능성
7. bedrock_client.py 단일 파일 2100줄 — 가독성/유지보수 저하

---

## 목표 아키텍처

```
사용자 메시지
  → analyze_node (식별자 추출 + 의도 분류 + 필요 행동 판단 — LLM 1회)
  → route
    ├─ 일반 질문 → general_answer → END
    ├─ 리소스 조회 → resolve → plan → execute_steps → report → END
    └─ 장애 분석 → resolve → plan → execute_steps → report → END
        (resolve 실패 시 → validation_fail → END)
```

### 핵심 변경점
- extract + classify → `analyze_node` 1회 통합
- 분류 카테고리 동적화 (LLM이 의도와 필요 행동을 자유롭게 판단)
- `plan_node` 추가: 어떤 sub-agent를 어떤 순서로 실행할지 계획
- 순차/병렬 하이브리드 실행: step 간 컨텍스트 전달 가능
- 프롬프트 외부 파일 분리
- bedrock_client.py 모듈 분할
- 원본 메시지 불변성 보장

---

## 태스크 목록

> 각 태스크는 독립적으로 실행 가능하도록 설계됨.
> 의존성이 있는 경우 `depends_on`에 명시.

---

### Task 1: 프롬프트 외부 파일 분리 ✅ 완료

- **ID**: `task-1-prompts`
- **depends_on**: 없음
- **난이도**: ★★☆☆☆
- **영향 범위**: `backend/src/bedrock_client.py`

#### 목표
현재 `bedrock_client.py` 내에 하드코딩된 모든 프롬프트 빌더 함수를 `backend/src/prompts/` 디렉토리로 분리.

#### 작업 내용
1. `backend/src/prompts/` 디렉토리 생성
2. 다음 파일로 분리:
   - `__init__.py` — 모든 프롬프트 빌더를 re-export
   - `analyze.py` — `_build_extract_prompt()`, `_build_classify_prompt()` (Task 3에서 통합 예정)
   - `collect.py` — `_build_collect_prompt()`
   - `report.py` — `_build_report_prompt()`
   - `resolve.py` — `_build_resolve_prompt()`
   - `sub_agents.py` — `_build_metric_agent_prompt()`, `_build_log_agent_prompt()`, `_build_resource_agent_prompt()`, `_build_network_agent_prompt()`
   - `general.py` — `_build_general_prompt()`
   - `utils.py` — `_get_current_time_info()` 등 공통 유틸
3. `bedrock_client.py`에서 import 경로 변경
4. 기존 동작 변경 없음 (순수 리팩토링)

#### 검증
- 기존 테스트 통과 (있는 경우)
- 프롬프트 출력 내용이 분리 전과 동일

---

### Task 2: bedrock_client.py 모듈 분할 ✅ 완료

- **ID**: `task-2-split-modules`
- **depends_on**: `task-1-prompts`
- **난이도**: ★★★☆☆
- **영향 범위**: `backend/src/bedrock_client.py`, `backend/src/main.py`, `backend/src/webhook_handler.py`

#### 목표
2100줄짜리 단일 파일을 역할별 모듈로 분할.

#### 작업 내용
1. `backend/src/tools.py` — MCP 도구 래핑 관련:
   - `create_pydantic_model_from_schema()`
   - `MCPToolWrapper` 클래스
   - `create_mcp_tool()`
   - `classify_tool()`, `_TOOL_ROUTING`
   - `SubAgentTool` 클래스
   - `_build_sub_agent_graph()`, `_run_sub_agent()`
2. `backend/src/graph.py` — LangGraph 워크플로우:
   - `_build_main_graph()` 메서드의 내용을 독립 함수로 추출
   - 각 노드 함수 (`extract_node`, `classify_node`, `resolve_node`, `collect_*`, `report_*`, `direct_answer_*`)
   - 라우팅 함수 (`route_after_classify`, `route_after_resolve`, `should_continue_collecting`)
3. `backend/src/bedrock_client.py` — 오케스트레이터만 남김:
   - `BedrockAgent` 클래스 (초기화, chat_stream, chat)
   - 히스토리/토큰 관리
   - 싱글톤
4. `backend/src/time_utils.py` — 시간 관련 유틸:
   - `parse_alarm_time_window()`
   - `_ALARM_TIME_PATTERN`, `_TIME_PARAM_MAP`
5. `main.py`, `webhook_handler.py`의 import 경로 수정

#### 검증
- 모든 import가 정상 동작
- `chat_stream`, `chat` 메서드가 기존과 동일하게 동작
- SSE 이벤트 구조 (`tool_start`, `tool_end`, `token`, `tool_trace`) 변경 없음

---

### Task 3: analyze_node 통합 (extract + classify 합치기) ✅ 완료

- **ID**: `task-3-analyze-node`
- **depends_on**: `task-1-prompts`, `task-2-split-modules`
- **난이도**: ★★★☆☆
- **영향 범위**: `backend/src/prompts/analyze.py`, `backend/src/graph.py`

#### 목표
현재 2회 LLM 호출(extract + classify)을 1회로 통합하고, 분류 카테고리를 동적화.

#### 작업 내용
1. `backend/src/prompts/analyze.py`에 새 프롬프트 `_build_analyze_prompt()` 작성:
   - 입력: 사용자 메시지, known_aliases
   - 출력 JSON 스키마:
     ```json
     {
       "intent": "사용자의 의도를 자연어로 설명",
       "category": "general | resource_lookup | incident_analysis | status_inquiry | ...",
       "identifiers": ["리소스 이름/ID"],
       "identifier_types": {"리소스명": "cluster|pod|instance|db|function|unknown"},
       "service_hint": "eks|ecs|ec2|rds|lambda|s3|alb|general",
       "account_ref": "계정 참조 문자열 또는 null",
       "regions": ["리전"],
       "time_range": "시간 범위 또는 null",
       "requires_validation": true/false,
       "requires_data_collection": true/false,
       "collection_types": ["metric", "log", "resource", "network"]
     }
     ```
   - `category`는 하드코딩 목록이 아닌 LLM 자유 판단. 단, 라우팅에 사용할 행동 플래그(`requires_validation`, `requires_data_collection`, `collection_types`)를 별도로 출력
2. `graph.py`에서 `extract_node` + `classify_node` → `analyze_node` 1개로 교체
3. `route_after_analyze` 라우팅 함수 작성:
   - `requires_data_collection == false && requires_validation == false` → `general_answer`
   - `requires_validation == true` → `resolve`
   - `requires_data_collection == true && requires_validation == false` → `plan` (검증 불필요한 전체 조회)
4. 기존 `_build_extract_prompt()`, `_build_classify_prompt()` 제거

#### 검증
- 기존 시나리오 테스트:
  - "안녕하세요" → general
  - "fault-injection-lab-cluster 장애 분석해줘" → incident_analysis, requires_validation=true
  - "EC2 인스턴스 목록 보여줘" → resource_lookup, requires_validation=false
  - "[FIRING] 알람 메시지" → incident_analysis, requires_validation=true

---

### Task 4: plan_node 추가 (실행 계획 수립) ✅ 완료

- **ID**: `task-4-plan-node`
- **depends_on**: `task-3-analyze-node`
- **난이도**: ★★★★☆
- **영향 범위**: `backend/src/prompts/plan.py`, `backend/src/graph.py`

#### 목표
어떤 sub-agent를 어떤 순서로 실행할지 LLM이 계획을 세우는 노드 추가.

#### 작업 내용
1. `backend/src/prompts/plan.py` 생성 — `_build_plan_prompt()`:
   - 입력: analyze 결과 + resolve 결과 (targets)
   - 출력 JSON 스키마:
     ```json
     {
       "steps": [
         {
           "step_id": 0,
           "agents": ["resource"],
           "purpose": "EKS 클러스터 기본 정보 및 노드그룹 상태 조회",
           "task_template": "...",
           "depends_on": null
         },
         {
           "step_id": 1,
           "agents": ["resource"],
           "purpose": "로그 그룹 이름 조회 (Container Insights)",
           "task_template": "...",
           "depends_on": 0
         },
         {
           "step_id": 2,
           "agents": ["log", "metric"],
           "purpose": "로그 수집 + 메트릭 수집 (병렬)",
           "task_template": "...",
           "depends_on": 1
         }
       ]
     }
     ```
   - 프롬프트 규칙:
     - `depends_on`이 null인 step들은 병렬 실행 가능
     - 같은 step 내의 agents는 병렬 실행
     - `depends_on`이 있으면 해당 step 완료 후 실행
     - analyze에서 판단한 `collection_types`에 해당하는 agent만 사용
     - 불필요한 agent는 계획에 포함하지 않음
2. `graph.py`에 `plan_node` 추가:
   - analyze 결과와 resolve 결과를 읽어 plan 프롬프트에 전달
   - LLM 응답에서 실행 계획 JSON 파싱
   - `__EXECUTION_PLAN__` SystemMessage로 state에 저장
3. 그래프 라우팅 수정:
   - resolve → plan → execute_steps (기존 collect 대체)

#### 검증
- EKS 장애 분석 시나리오: resource → resource(로그 그룹 조회) → log + metric 순서로 계획 수립 확인
- EC2 목록 조회 시나리오: resource 1개 step만 계획
- 일반 질문: plan_node 미실행 확인

---

### Task 5: 순차/병렬 하이브리드 실행기 구현 ✅ 완료

- **ID**: `task-5-hybrid-executor`
- **depends_on**: `task-4-plan-node`
- **난이도**: ★★★★★
- **영향 범위**: `backend/src/graph.py`, `backend/src/tools.py`

#### 목표
plan의 steps를 순서대로 실행하되, 같은 step 내의 agent들은 병렬 실행. step 간에는 이전 step의 결과를 다음 step의 context로 전달.

#### 작업 내용
1. `execute_steps_node` 구현 (기존 `collect_setup` + `collect_agent` + `collect_tools` 대체):
   ```python
   async def execute_steps_node(state: MessagesState) -> MessagesState:
       """
       실행 계획(plan)의 steps를 순차 실행.
       각 step 내의 agents는 병렬 실행.
       이전 step의 결과를 다음 step의 context로 전달.
       """
       plan = _read_plan_from_state(state)
       accumulated_context = {}  # step_id → 결과 텍스트
       
       for step in plan["steps"]:
           # depends_on 확인 → 이전 step 결과를 context에 포함
           prev_context = ""
           if step["depends_on"] is not None:
               prev_context = accumulated_context.get(step["depends_on"], "")
           
           # 같은 step 내의 agents 병렬 실행
           tasks = []
           for agent_role in step["agents"]:
               task_text = step["task_template"]
               if prev_context:
                   task_text = f"## 이전 단계 수집 결과\n{prev_context}\n\n---\n{task_text}"
               tasks.append(_run_sub_agent_by_role(agent_role, task_text))
           
           results = await asyncio.gather(*tasks)
           
           # 결과 축적
           accumulated_context[step["step_id"]] = "\n\n".join(results)
       
       # 모든 결과를 state에 저장
       return {"messages": [...]}
   ```
2. sub-agent 호출 시 target constraint + profile 주입 로직 유지
3. 기존 `collect_setup_node`, `collect_agent_node`, `collect_tools_node`, `should_continue_collecting` 제거
4. SSE 이벤트 (`tool_start`, `tool_end`) 발행 로직 유지:
   - 각 sub-agent 실행 시작/종료 시 이벤트 발행
   - chat_stream의 스트리밍 로직은 변경 없음

#### 검증
- EKS 장애 분석: step 0 (resource) → step 1 (resource: 로그 그룹 조회) → step 2 (log + metric 병렬) 순서 실행 확인
- step 1의 결과(로그 그룹 이름)가 step 2의 log agent task에 포함되는지 확인
- 단일 step 시나리오 (EC2 목록): 정상 동작 확인
- SSE 이벤트 정상 발행 확인

---

### Task 6: 원본 메시지 불변성 & Target Lock ✅ 완료

- **ID**: `task-6-immutability`
- **depends_on**: `task-2-split-modules`
- **난이도**: ★★☆☆☆
- **영향 범위**: `backend/src/graph.py`

#### 목표
원본 메시지와 확정된 targets가 파이프라인 중간에 변형되지 않도록 보장.

#### 작업 내용
1. `analyze_node`에서 `__ORIGINAL_MESSAGE__` SystemMessage를 최초 1회 저장
   - 이후 모든 노드에서 사용자 메시지가 필요할 때 이 값을 참조
   - HumanMessage를 직접 탐색하는 기존 패턴 제거
2. `resolve_node`에서 `__LOCKED_TARGETS__` SystemMessage 저장
   - 이후 collect/plan/report에서 이 값만 참조
   - targets를 수정하는 코드 경로 차단
3. state에서 메타데이터를 읽는 헬퍼 함수 작성:
   ```python
   def read_state_meta(state: MessagesState, key: str) -> Optional[str]:
       """state에서 __KEY__:value 형태의 메타데이터를 읽는 유틸"""
       prefix = f"__{key}__:"
       for m in reversed(state["messages"]):
           if isinstance(m, SystemMessage) and isinstance(m.content, str):
               if m.content.startswith(prefix):
                   return m.content[len(prefix):]
       return None
   ```
4. 기존 `__EXTRACT_RESULT__`, `__QUERY_TYPE__`, `__RESOLVED_TARGETS__` 등의 키를 통일된 네이밍으로 정리

#### 검증
- 파이프라인 전체에서 원본 메시지가 변형되지 않는지 로그로 확인
- resolve 이후 targets가 변경되지 않는지 확인

---

### Task 7: 리포트 프롬프트 개선 ✅ 완료

- **ID**: `task-7-report-prompt`
- **depends_on**: `task-1-prompts`, `task-5-hybrid-executor`
- **난이도**: ★★☆☆☆
- **영향 범위**: `backend/src/prompts/report.py`, `backend/src/graph.py`

#### 목표
리포트 프롬프트를 analyze의 동적 카테고리에 맞게 개선. 수집된 데이터 구조 변경(step별 결과)에 대응.

#### 작업 내용
1. `report.py`의 `_build_report_prompt()` 수정:
   - 기존 `report_type` 파라미터 (incident/status_list/status_summary) 대신 analyze 결과의 `intent`와 `category`를 활용
   - step별 수집 결과를 구조화하여 리포트 입력에 포함
2. `report_setup_node` 수정:
   - execute_steps의 결과를 step별로 구분하여 리포트 입력 구성
   - 각 step의 purpose를 섹션 헤더로 사용
3. 할루시네이션 방지 규칙 유지 및 강화

#### 검증
- 장애 분석 리포트 형식 정상 출력
- 리소스 목록 리포트 형식 정상 출력
- 수집 실패 시 "데이터 없음" 명시 확인

---

### Task 8: collect 프롬프트 제거 및 정리 ✅ 완료

- **ID**: `task-8-cleanup`
- **depends_on**: `task-5-hybrid-executor`, `task-7-report-prompt`
- **난이도**: ★☆☆☆☆
- **영향 범위**: `backend/src/prompts/`, `backend/src/graph.py`

#### 목표
하이브리드 실행기 도입으로 불필요해진 기존 collect 프롬프트 및 노드 정리.

#### 작업 내용
1. `backend/src/prompts/collect.py` 제거 (plan + execute_steps로 대체됨)
2. 기존 `_build_collect_prompt()` 참조 제거
3. 기존 `collect_setup_node`, `collect_agent_node`, `collect_tools_node` 코드 제거
4. 사용하지 않는 import 정리
5. 기존 `_CLASSIFY_TYPES` 딕셔너리 제거

#### 검증
- 전체 파이프라인 정상 동작
- 불필요한 코드/파일 없음 확인

---

## 실행 순서 (권장)

```
Task 1 (프롬프트 분리)
  └→ Task 2 (모듈 분할)
       ├→ Task 6 (불변성 보장) — 독립 실행 가능
       └→ Task 3 (analyze 통합)
            └→ Task 4 (plan 노드)
                 └→ Task 5 (하이브리드 실행기)
                      └→ Task 7 (리포트 개선)
                           └→ Task 8 (정리)
```

## 파일 구조 (최종)

```
backend/src/
├── __init__.py
├── bedrock_client.py      # BedrockAgent 클래스 (오케스트레이터, chat_stream, chat)
├── graph.py               # LangGraph 워크플로우 정의, 노드 함수, 라우팅
├── tools.py               # MCPToolWrapper, SubAgentTool, classify_tool, sub-agent 그래프
├── time_utils.py           # 시간 관련 유틸 (parse_alarm_time_window 등)
├── prompts/
│   ├── __init__.py         # re-export
│   ├── analyze.py          # analyze_node 프롬프트 (extract + classify 통합)
│   ├── plan.py             # plan_node 프롬프트 (실행 계획 수립)
│   ├── resolve.py          # resolve_node 프롬프트 (리소스 검증)
│   ├── report.py           # report 프롬프트 (리포트 작성)
│   ├── sub_agents.py       # sub-agent 프롬프트 (metric, log, resource, network)
│   ├── general.py          # 일반 질문 프롬프트
│   └── utils.py            # 공통 유틸 (_get_current_time_info 등)
├── config.py
├── mcp_manager.py
├── main.py
├── conversation_store.py
├── account_profile_resolver.py
└── webhook_handler.py
```
