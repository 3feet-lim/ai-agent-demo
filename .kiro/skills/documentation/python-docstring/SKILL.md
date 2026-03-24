---
name: python-docstring
description: Python docstring 작성 가이드. Google 스타일 docstring으로 함수, 클래스, 모듈 문서화 패턴을 제공합니다.
---

# Python Docstring 가이드 (Google 스타일)

## 함수/메서드
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
        model_id: 사용할 Bedrock 모델 ID

    Returns:
        AI 응답이 포함된 딕셔너리

    Raises:
        HTTPException: 대화 세션을 찾을 수 없는 경우 (404)
    """
```

## 클래스
```python
class ConversationStore:
    """대화 세션을 메모리에 저장하고 관리하는 클래스.

    Attributes:
        conversations: 대화 ID를 키로 하는 대화 데이터 딕셔너리
        max_conversations: 최대 저장 가능한 대화 수
    """
```

## 모듈
```python
"""MCP(Model Context Protocol) 매니저 모듈.

MCP 서버와의 연결을 관리하고, 도구(tool) 호출을 처리합니다.
"""
```

## 인라인 주석
- "왜(why)"를 설명하고, "무엇을(what)"은 코드가 설명하게 함
- 태그: TODO, FIXME, HACK, NOTE
