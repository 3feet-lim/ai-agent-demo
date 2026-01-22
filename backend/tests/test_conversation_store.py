"""
conversation_store.py 단위 테스트
"""
import pytest
import pytest_asyncio

from src.conversation_store import ConversationStore


class TestConversationStore:
    """ConversationStore 클래스 테스트"""

    @pytest.mark.asyncio
    async def test_initialize_creates_tables(self, temp_db_path):
        """초기화 시 테이블 생성 테스트"""
        store = ConversationStore(db_path=temp_db_path)
        await store.initialize()

        # 테이블이 존재하는지 확인
        import aiosqlite
        async with aiosqlite.connect(temp_db_path) as db:
            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            tables = await cursor.fetchall()
            table_names = [t[0] for t in tables]

            assert "conversations" in table_names
            assert "messages" in table_names

    @pytest.mark.asyncio
    async def test_create_conversation(self, conversation_store):
        """대화 생성 테스트"""
        conv_id = await conversation_store.create_conversation()

        assert conv_id is not None
        assert len(conv_id) == 36  # UUID 형식

    @pytest.mark.asyncio
    async def test_create_conversation_with_title(self, conversation_store):
        """제목이 있는 대화 생성 테스트"""
        conv_id = await conversation_store.create_conversation(title="테스트 대화")

        conversation = await conversation_store.get_conversation(conv_id)
        assert conversation["title"] == "테스트 대화"

    @pytest.mark.asyncio
    async def test_add_message(self, conversation_store):
        """메시지 추가 테스트"""
        conv_id = await conversation_store.create_conversation()
        msg_id = await conversation_store.add_message(conv_id, "user", "안녕하세요")

        assert msg_id is not None
        assert len(msg_id) == 36  # UUID 형식

    @pytest.mark.asyncio
    async def test_add_message_auto_title(self, conversation_store):
        """첫 메시지로 제목 자동 설정 테스트"""
        conv_id = await conversation_store.create_conversation()
        await conversation_store.add_message(conv_id, "user", "오늘 날씨가 어때?")

        conversation = await conversation_store.get_conversation(conv_id)
        assert conversation["title"] == "오늘 날씨가 어때?"

    @pytest.mark.asyncio
    async def test_add_message_long_title_truncated(self, conversation_store):
        """긴 메시지의 제목 잘림 테스트"""
        conv_id = await conversation_store.create_conversation()
        long_message = "A" * 100
        await conversation_store.add_message(conv_id, "user", long_message)

        conversation = await conversation_store.get_conversation(conv_id)
        assert len(conversation["title"]) == 53  # 50자 + "..."

    @pytest.mark.asyncio
    async def test_get_conversation(self, conversation_store):
        """대화 조회 테스트"""
        conv_id = await conversation_store.create_conversation()
        await conversation_store.add_message(conv_id, "user", "질문입니다")
        await conversation_store.add_message(conv_id, "assistant", "답변입니다")

        conversation = await conversation_store.get_conversation(conv_id)

        assert conversation["id"] == conv_id
        assert len(conversation["messages"]) == 2
        assert conversation["messages"][0]["role"] == "user"
        assert conversation["messages"][0]["content"] == "질문입니다"
        assert conversation["messages"][1]["role"] == "assistant"
        assert conversation["messages"][1]["content"] == "답변입니다"

    @pytest.mark.asyncio
    async def test_get_conversation_not_found(self, conversation_store):
        """존재하지 않는 대화 조회 테스트"""
        conversation = await conversation_store.get_conversation("nonexistent-id")
        assert conversation is None

    @pytest.mark.asyncio
    async def test_get_messages(self, conversation_store):
        """메시지 목록 조회 테스트 (LangChain 형식)"""
        conv_id = await conversation_store.create_conversation()
        await conversation_store.add_message(conv_id, "user", "Hello")
        await conversation_store.add_message(conv_id, "assistant", "Hi there!")

        messages = await conversation_store.get_messages(conv_id)

        assert len(messages) == 2
        assert messages[0] == {"role": "user", "content": "Hello"}
        assert messages[1] == {"role": "assistant", "content": "Hi there!"}

    @pytest.mark.asyncio
    async def test_list_conversations(self, conversation_store):
        """대화 목록 조회 테스트"""
        # 여러 대화 생성
        conv_id1 = await conversation_store.create_conversation()
        await conversation_store.add_message(conv_id1, "user", "첫 번째 대화")

        conv_id2 = await conversation_store.create_conversation()
        await conversation_store.add_message(conv_id2, "user", "두 번째 대화")

        conversations = await conversation_store.list_conversations()

        assert len(conversations) == 2
        # 최신순 정렬
        assert conversations[0]["id"] == conv_id2
        assert conversations[1]["id"] == conv_id1

    @pytest.mark.asyncio
    async def test_list_conversations_limit(self, conversation_store):
        """대화 목록 조회 제한 테스트"""
        # 3개 대화 생성
        for i in range(3):
            conv_id = await conversation_store.create_conversation()
            await conversation_store.add_message(conv_id, "user", f"대화 {i}")

        conversations = await conversation_store.list_conversations(limit=2)
        assert len(conversations) == 2

    @pytest.mark.asyncio
    async def test_delete_conversation(self, conversation_store):
        """대화 삭제 테스트"""
        conv_id = await conversation_store.create_conversation()
        await conversation_store.add_message(conv_id, "user", "삭제될 메시지")

        # 삭제
        result = await conversation_store.delete_conversation(conv_id)
        assert result is True

        # 삭제 확인
        conversation = await conversation_store.get_conversation(conv_id)
        assert conversation is None

    @pytest.mark.asyncio
    async def test_delete_conversation_not_found(self, conversation_store):
        """존재하지 않는 대화 삭제 테스트"""
        result = await conversation_store.delete_conversation("nonexistent-id")
        assert result is False

    @pytest.mark.asyncio
    async def test_message_order_preserved(self, conversation_store):
        """메시지 순서 보존 테스트"""
        conv_id = await conversation_store.create_conversation()

        messages_to_add = [
            ("user", "1번 메시지"),
            ("assistant", "2번 메시지"),
            ("user", "3번 메시지"),
            ("assistant", "4번 메시지"),
        ]

        for role, content in messages_to_add:
            await conversation_store.add_message(conv_id, role, content)

        messages = await conversation_store.get_messages(conv_id)

        assert len(messages) == 4
        for i, (role, content) in enumerate(messages_to_add):
            assert messages[i]["role"] == role
            assert messages[i]["content"] == content
