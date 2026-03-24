---
name: pytest-testing
description: pytest 기반 테스트 작성 패턴. AAA 패턴, fixture, parametrize, FastAPI TestClient, 비동기 테스트, mock/patch를 다룹니다.
---

# pytest 테스트 작성 가이드

## 기본 구조 (AAA 패턴)
```python
def test_기능_시나리오():
    # Arrange - 테스트 데이터 준비
    input_data = {"key": "value"}
    # Act - 테스트 대상 실행
    result = target_function(input_data)
    # Assert - 결과 검증
    assert result.status == "success"
```

## Fixture 활용
```python
@pytest.fixture
def mock_bedrock_client():
    """Bedrock 클라이언트 mock fixture"""
    with patch("backend.src.bedrock_client.BedrockClient") as mock:
        client = AsyncMock()
        mock.return_value = client
        yield client
```

## FastAPI 엔드포인트 테스트
```python
from fastapi.testclient import TestClient
from backend.src.main import app

client = TestClient(app)

def test_health_check():
    response = client.get("/health")
    assert response.status_code == 200
```

## Parametrize
```python
@pytest.mark.parametrize("input_val,expected", [
    ("valid_input", True),
    ("", False),
    (None, False),
])
def test_validate_input(input_val, expected):
    assert validate(input_val) == expected
```

## 비동기 테스트
```python
@pytest.mark.asyncio
async def test_async_operation():
    result = await async_function()
    assert result is not None
```

## 네이밍 규칙
- `test_[대상]_[시나리오]_[기대결과]`
- 예: `test_create_user_with_invalid_email_raises_error`
