---
name: api-docs
description: REST API 문서 작성 가이드. 엔드포인트 문서 형식, 요청/응답 스키마, 에러 코드, 환경 변수 문서화 패턴을 제공합니다.
---

# API 문서 작성 가이드

## 엔드포인트 문서 형식
```markdown
### POST /conversations/{conversation_id}/messages

대화에 새 메시지를 전송합니다.

**요청 본문:**
| 필드 | 타입 | 필수 | 설명 |
|------|------|------|------|
| message | string | ✅ | 사용자 메시지 |

**응답 (200 OK):**
{"role": "assistant", "content": "AI 응답 내용"}

**에러 응답:**
- 404: 대화 세션을 찾을 수 없음
- 500: AI 모델 호출 실패
```

## 환경 변수 문서화
```markdown
| 변수명 | 설명 | 필수 | 기본값 |
|--------|------|------|--------|
| AWS_REGION | AWS 리전 | ✅ | - |
| BEDROCK_MODEL_ID | 모델 ID | ❌ | anthropic.claude-3-sonnet |
```

## 주의사항
- FastAPI 자동 문서(Swagger UI)와 일관성 유지
- 민감 정보는 절대 문서에 포함하지 않음
