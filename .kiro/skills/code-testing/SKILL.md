---
name: code-testing
description: pytest 기반 테스트 작성 패턴과 가이드를 제공합니다.
inclusion: manual
---

# 코드 테스트 스킬 (Code Testing Skill)

테스트 작성 시 참고하는 패턴과 가이드입니다.

## pytest 테스트 패턴

### 기본 구조 (AAA 패턴)
```python
def test_기능_시나리오():
    # Arrange - 테스트 데이터 준비
    input_data = {"key": "value"}

    # Act - 테스트 대상 실행
    result = target_function(input_data)

    # Assert - 결과 검증
    assert result.status == "success"
```

### Fixture 활용
```python
import pytest
from unittest.mock import AsyncMock, patch

@pytest.fixture
def mock_bedrock_client():
    """Bedrock 클라이언트 mock fixture"""
    with patch("backend.src.bedrock_client.BedrockClient") as mock:
        client = AsyncMock()
        mock.return_value = client
        yield client

@pytest.fixture
def sample_conversation():
    """테스트용 대화 데이터"""
    return {
        "conversation_id": "test-123",
        "messages": [
            {"role": "user", "content": "안녕하세요"}
        ]
    }
```

### FastAPI 엔드포인트 테스트
```python
from fastapi.testclient import TestClient
from backend.src.main import app

client = TestClient(app)

def test_health_check():
    response = client.get("/health")
    assert response.status_code == 200

def test_create_conversation():
    response = client.post("/conversations", json={"title": "테스트"})
    assert response.status_code == 201
    assert "conversation_id" in response.json()
```

### Parametrize 활용
```python
@pytest.mark.parametrize("input_val,expected", [
    ("valid_input", True),
    ("", False),
    (None, False),
    ("special!@#", False),
])
def test_validate_input(input_val, expected):
    assert validate(input_val) == expected
```

### 비동기 테스트
```python
import pytest

@pytest.mark.asyncio
async def test_async_operation():
    result = await async_function()
    assert result is not None
```

## 테스트 네이밍 규칙
- `test_[대상]_[시나리오]_[기대결과]`
- 예: `test_create_user_with_invalid_email_raises_error`
- 한국어 주석으로 테스트 의도 설명
