---
name: code-documentation
description: 코드 문서화 스타일 가이드 (Docstring, TSDoc, README, API 문서 등)를 제공합니다.
inclusion: manual
---

# 코드 문서화 스킬 (Code Documentation Skill)

코드 문서화 시 참고하는 스타일 가이드와 패턴입니다.

## Python Docstring (Google 스타일)

### 함수/메서드
```python
async def send_message(
    conversation_id: str,
    message: str,
    model_id: str = "anthropic.claude-3-sonnet"
) -> dict:
    """대화에 메시지를 전송하고 AI 응답을 반환합니다.

    Args:
        conversation_id: 대화 세션 고유 ID
        message: 사용자 입력 메시지
        model_id: 사용할 Bedrock 모델 ID (기본값: anthropic.claude-3-sonnet)

    Returns:
        AI 응답이 포함된 딕셔너리. 예시:
            {"role": "assistant", "content": "응답 내용"}

    Raises:
        HTTPException: 대화 세션을 찾을 수 없는 경우 (404)
        HTTPException: Bedrock API 호출 실패 시 (500)
    """
```

### 클래스
```python
class ConversationStore:
    """대화 세션을 메모리에 저장하고 관리하는 클래스.

    인메모리(in-memory) 저장소로, 서버 재시작 시 데이터가 초기화됩니다.

    Attributes:
        conversations: 대화 ID를 키로 하는 대화 데이터 딕셔너리
        max_conversations: 최대 저장 가능한 대화 수
    """
```

### 모듈
```python
"""MCP(Model Context Protocol) 매니저 모듈.

MCP 서버와의 연결을 관리하고, 도구(tool) 호출을 처리합니다.
Bedrock 클라이언트와 연동하여 AI 모델이 외부 도구를 사용할 수 있게 합니다.
"""
```

## TypeScript/React 문서화 (TSDoc)

### 컴포넌트
```typescript
/**
 * 채팅 영역 컴포넌트
 *
 * 대화 메시지 목록을 표시하고 자동 스크롤을 지원합니다.
 * 마크다운 렌더링과 코드 하이라이팅을 포함합니다.
 *
 * @param props - 컴포넌트 속성
 * @param props.messages - 표시할 메시지 배열
 * @param props.isLoading - AI 응답 대기 중 여부
 * @param props.onRetry - 메시지 재전송 콜백
 */
```

### 인터페이스/타입
```typescript
/** 대화 메시지 타입 */
interface Message {
  /** 메시지 고유 ID */
  id: string;
  /** 발신자 역할 ("user" | "assistant") */
  role: "user" | "assistant";
  /** 메시지 본문 (마크다운 지원) */
  content: string;
  /** 메시지 생성 시각 (ISO 8601) */
  timestamp: string;
}
```

### 커스텀 훅(Hook)
```typescript
/**
 * 대화 세션을 관리하는 커스텀 훅
 *
 * @param conversationId - 대화 세션 ID (없으면 새 세션 생성)
 * @returns 대화 상태와 제어 함수들
 *
 * @example
 * ```tsx
 * const { messages, sendMessage, isLoading } = useConversation("conv-123");
 * ```
 */
```

## 인라인 주석 가이드

### 작성 원칙
- "왜(why)"를 설명하고, "무엇을(what)"은 코드가 스스로 설명하게 합니다
- 비즈니스 로직의 배경이나 제약 조건을 설명합니다
- 비직관적인 코드에 대한 이유를 명시합니다

### 태그 사용
```python
# TODO: 영구 저장소(DB)로 마이그레이션 필요
# FIXME: 동시 요청 시 race condition 발생 가능
# HACK: Bedrock API의 응답 형식 불일치를 임시 처리
# NOTE: MCP 서버 연결은 최초 요청 시 lazy 초기화됨
```

### 좋은 예 vs 나쁜 예
```python
# ❌ 나쁜 예: 코드를 그대로 반복
# 리스트를 순회한다
for item in items:

# ✅ 좋은 예: 이유를 설명
# 최신 메시지부터 처리해야 컨텍스트 윈도우 초과 시 오래된 메시지가 잘림
for item in reversed(items):
```

## README 작성 가이드

### 필수 섹션
1. 프로젝트 개요 및 주요 기능
2. 기술 스택
3. 설치 및 실행 방법 (Docker / 로컬)
4. 환경 변수 설정 (`.env.example` 참조)
5. API 엔드포인트 요약
6. 프로젝트 구조

### 환경 변수 문서화
```markdown
| 변수명 | 설명 | 필수 | 기본값 |
|--------|------|------|--------|
| AWS_REGION | AWS 리전 | ✅ | - |
| BEDROCK_MODEL_ID | 사용할 모델 ID | ❌ | anthropic.claude-3-sonnet |
```

## API 문서 작성 가이드

### 엔드포인트 문서 형식
```markdown
### POST /conversations/{conversation_id}/messages

대화에 새 메시지를 전송합니다.

**요청 본문(Request Body):**
| 필드 | 타입 | 필수 | 설명 |
|------|------|------|------|
| message | string | ✅ | 사용자 메시지 |

**응답 (200 OK):**
​```json
{
  "role": "assistant",
  "content": "AI 응답 내용"
}
​```

**에러 응답:**
- `404`: 대화 세션을 찾을 수 없음
- `500`: AI 모델 호출 실패
```

## 주의사항

- 기존 문서 스타일이 있으면 일관성을 유지합니다
- FastAPI 자동 문서(Swagger UI, `/docs`)와 충돌하지 않도록 합니다
- 민감 정보(API 키, 비밀번호)는 절대 문서에 포함하지 않습니다
- 코드 변경 없이 문서만 추가/수정하는 것을 기본으로 합니다
