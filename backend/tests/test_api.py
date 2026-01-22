"""
main.py FastAPI 엔드포인트 단위 테스트
"""
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient, ASGITransport


@pytest.fixture
def mock_dependencies():
    """모든 의존성 모킹"""
    with patch('src.main.get_conversation_store') as mock_store, \
         patch('src.main.get_mcp_manager') as mock_mcp, \
         patch('src.main.get_bedrock_agent') as mock_agent:

        # ConversationStore 모킹
        store = MagicMock()
        store.create_conversation = AsyncMock(return_value="test-conv-id")
        store.add_message = AsyncMock(return_value="test-msg-id")
        store.get_messages = AsyncMock(return_value=[])
        store.get_conversation = AsyncMock(return_value={
            "id": "test-conv-id",
            "title": "테스트 대화",
            "created_at": "2024-01-01T00:00:00",
            "updated_at": "2024-01-01T00:00:00",
            "messages": []
        })
        store.list_conversations = AsyncMock(return_value=[])
        store.delete_conversation = AsyncMock(return_value=True)
        mock_store.return_value = store

        # MCP Manager 모킹
        mcp = MagicMock()
        mcp.is_enabled = False
        mcp.disconnect = AsyncMock()
        mock_mcp.return_value = mcp

        # BedrockAgent 모킹
        agent = MagicMock()
        agent.chat = AsyncMock(return_value="AI 응답입니다.")
        mock_agent.return_value = agent

        yield {
            "store": store,
            "mcp": mcp,
            "agent": agent
        }


@pytest.fixture
def app(mock_dependencies):
    """테스트용 FastAPI 앱"""
    from src.main import app
    return app


class TestHealthEndpoint:
    """헬스체크 엔드포인트 테스트"""

    @pytest.mark.asyncio
    async def test_health_check(self, app):
        """헬스체크 성공 테스트"""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health")

        assert response.status_code == 200
        assert response.json() == {"status": "healthy"}


class TestStatusEndpoint:
    """상태 확인 엔드포인트 테스트"""

    @pytest.mark.asyncio
    async def test_get_status(self, app, mock_dependencies):
        """상태 확인 테스트"""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/status")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert "model" in data
        assert "region" in data
        assert "mcp_enabled" in data


class TestChatEndpoint:
    """채팅 엔드포인트 테스트"""

    @pytest.mark.asyncio
    async def test_chat_new_conversation(self, app, mock_dependencies):
        """새 대화 생성 테스트"""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/chat",
                json={"message": "안녕하세요"}
            )

        assert response.status_code == 200
        data = response.json()
        assert data["response"] == "AI 응답입니다."
        assert data["conversation_id"] == "test-conv-id"

    @pytest.mark.asyncio
    async def test_chat_existing_conversation(self, app, mock_dependencies):
        """기존 대화 이어가기 테스트"""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/chat",
                json={
                    "message": "질문입니다",
                    "conversation_id": "existing-conv-id"
                }
            )

        assert response.status_code == 200
        # store.create_conversation이 호출되지 않았는지 확인
        mock_dependencies["store"].create_conversation.assert_not_called()

    @pytest.mark.asyncio
    async def test_chat_saves_messages(self, app, mock_dependencies):
        """메시지 저장 테스트"""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post(
                "/api/chat",
                json={"message": "테스트 메시지"}
            )

        # 사용자 메시지와 AI 응답 모두 저장되었는지 확인
        assert mock_dependencies["store"].add_message.call_count == 2

    @pytest.mark.asyncio
    async def test_chat_error_handling(self, app, mock_dependencies):
        """에러 처리 테스트"""
        mock_dependencies["agent"].chat = AsyncMock(
            side_effect=Exception("테스트 에러")
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/chat",
                json={"message": "에러 발생"}
            )

        assert response.status_code == 500


class TestConversationsEndpoint:
    """대화 목록 엔드포인트 테스트"""

    @pytest.mark.asyncio
    async def test_list_conversations_empty(self, app, mock_dependencies):
        """빈 대화 목록 테스트"""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/conversations")

        assert response.status_code == 200
        assert response.json() == []

    @pytest.mark.asyncio
    async def test_list_conversations_with_data(self, app, mock_dependencies):
        """대화 목록 반환 테스트"""
        mock_dependencies["store"].list_conversations = AsyncMock(return_value=[
            {
                "id": "conv-1",
                "title": "대화 1",
                "updated_at": "2024-01-01T00:00:00",
                "preview": "첫 번째 메시지"
            },
            {
                "id": "conv-2",
                "title": "대화 2",
                "updated_at": "2024-01-02T00:00:00",
                "preview": "두 번째 메시지"
            }
        ])

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/conversations")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2


class TestConversationDetailEndpoint:
    """대화 상세 엔드포인트 테스트"""

    @pytest.mark.asyncio
    async def test_get_conversation(self, app, mock_dependencies):
        """대화 상세 조회 테스트"""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/conversations/test-conv-id")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == "test-conv-id"
        assert data["title"] == "테스트 대화"

    @pytest.mark.asyncio
    async def test_get_conversation_not_found(self, app, mock_dependencies):
        """존재하지 않는 대화 조회 테스트"""
        mock_dependencies["store"].get_conversation = AsyncMock(return_value=None)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/conversations/nonexistent")

        assert response.status_code == 404


class TestDeleteConversationEndpoint:
    """대화 삭제 엔드포인트 테스트"""

    @pytest.mark.asyncio
    async def test_delete_conversation(self, app, mock_dependencies):
        """대화 삭제 테스트"""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.delete("/api/conversations/test-conv-id")

        assert response.status_code == 200
        assert response.json() == {"status": "deleted"}

    @pytest.mark.asyncio
    async def test_delete_conversation_not_found(self, app, mock_dependencies):
        """존재하지 않는 대화 삭제 테스트"""
        mock_dependencies["store"].delete_conversation = AsyncMock(return_value=False)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.delete("/api/conversations/nonexistent")

        assert response.status_code == 404
