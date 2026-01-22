"""
pytest 설정 및 공통 fixture
"""
import os
import sys
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

# 프로젝트 루트를 path에 추가
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


@pytest.fixture(scope="session")
def event_loop_policy():
    """이벤트 루프 정책 설정"""
    import asyncio
    return asyncio.DefaultEventLoopPolicy()


@pytest.fixture
def temp_db_path():
    """임시 데이터베이스 경로"""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        yield f.name
    # 테스트 후 삭제
    if os.path.exists(f.name):
        os.unlink(f.name)


@pytest.fixture
def mock_settings(temp_db_path):
    """테스트용 설정 모킹"""
    with patch('src.config.Settings') as mock:
        settings = MagicMock()
        settings.aws_region = "ap-northeast-2"
        settings.aws_access_key_id = "test-access-key"
        settings.aws_secret_access_key = "test-secret-key"
        settings.bedrock_model_id = "anthropic.claude-sonnet-4-5-v2:0"
        settings.log_level = "DEBUG"
        settings.conversation_db_path = temp_db_path
        settings.mcp_server_url = None
        mock.return_value = settings
        yield settings


@pytest.fixture
def mock_bedrock_response():
    """Bedrock API 응답 모킹"""
    return MagicMock(content="테스트 응답입니다.")


@pytest_asyncio.fixture
async def conversation_store(temp_db_path):
    """테스트용 대화 저장소"""
    from src.conversation_store import ConversationStore

    store = ConversationStore(db_path=temp_db_path)
    await store.initialize()
    yield store


@pytest.fixture
def mock_langchain_llm(mock_bedrock_response):
    """LangChain LLM 모킹"""
    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(return_value=mock_bedrock_response)
    return mock_llm
