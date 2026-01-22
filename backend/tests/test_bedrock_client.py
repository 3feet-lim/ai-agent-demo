"""
bedrock_client.py 단위 테스트
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage


class TestBedrockAgent:
    """BedrockAgent 클래스 테스트"""

    @pytest.fixture
    def mock_chat_bedrock(self):
        """ChatBedrock 모킹"""
        with patch('src.bedrock_client.ChatBedrock') as mock:
            mock_instance = MagicMock()
            mock_instance.ainvoke = AsyncMock(
                return_value=AIMessage(content="테스트 응답입니다.")
            )
            mock.return_value = mock_instance
            yield mock_instance

    @pytest.fixture
    def mock_mcp_manager(self):
        """MCP Manager 모킹"""
        with patch('src.bedrock_client.get_mcp_manager') as mock:
            manager = MagicMock()
            manager.get_context = AsyncMock(return_value=MagicMock(
                system_prompt="테스트 시스템 프롬프트",
                tools=[],
                resources=[]
            ))
            mock.return_value = manager
            yield manager

    @pytest.mark.asyncio
    async def test_chat_returns_response(self, mock_chat_bedrock, mock_mcp_manager):
        """채팅 응답 반환 테스트"""
        from src.bedrock_client import BedrockAgent

        # 싱글톤 초기화
        import src.bedrock_client as bedrock_module
        bedrock_module._agent = None

        agent = BedrockAgent()
        response = await agent.chat("안녕하세요")

        assert response == "테스트 응답입니다."

    @pytest.mark.asyncio
    async def test_chat_with_history(self, mock_chat_bedrock, mock_mcp_manager):
        """히스토리가 있는 채팅 테스트"""
        from src.bedrock_client import BedrockAgent

        import src.bedrock_client as bedrock_module
        bedrock_module._agent = None

        agent = BedrockAgent()
        history = [
            {"role": "user", "content": "이전 질문"},
            {"role": "assistant", "content": "이전 답변"}
        ]
        response = await agent.chat("새 질문", history=history)

        assert response == "테스트 응답입니다."

    def test_convert_to_langchain_messages(self, mock_chat_bedrock):
        """메시지 변환 테스트"""
        from src.bedrock_client import BedrockAgent

        import src.bedrock_client as bedrock_module
        bedrock_module._agent = None

        agent = BedrockAgent()

        history = [
            {"role": "user", "content": "질문1"},
            {"role": "assistant", "content": "답변1"},
            {"role": "user", "content": "질문2"},
        ]
        system_prompt = "시스템 프롬프트"

        messages = agent._convert_to_langchain_messages(history, system_prompt)

        assert len(messages) == 4
        assert isinstance(messages[0], SystemMessage)
        assert messages[0].content == "시스템 프롬프트"
        assert isinstance(messages[1], HumanMessage)
        assert messages[1].content == "질문1"
        assert isinstance(messages[2], AIMessage)
        assert messages[2].content == "답변1"
        assert isinstance(messages[3], HumanMessage)
        assert messages[3].content == "질문2"

    def test_convert_to_langchain_messages_empty_history(self, mock_chat_bedrock):
        """빈 히스토리 변환 테스트"""
        from src.bedrock_client import BedrockAgent

        import src.bedrock_client as bedrock_module
        bedrock_module._agent = None

        agent = BedrockAgent()
        messages = agent._convert_to_langchain_messages([], "시스템")

        assert len(messages) == 1
        assert isinstance(messages[0], SystemMessage)

    def test_convert_to_langchain_messages_no_system_prompt(self, mock_chat_bedrock):
        """시스템 프롬프트 없는 변환 테스트"""
        from src.bedrock_client import BedrockAgent

        import src.bedrock_client as bedrock_module
        bedrock_module._agent = None

        agent = BedrockAgent()

        history = [{"role": "user", "content": "질문"}]
        messages = agent._convert_to_langchain_messages(history, "")

        # 빈 시스템 프롬프트는 추가되지 않음
        assert len(messages) == 1
        assert isinstance(messages[0], HumanMessage)


class TestGetBedrockAgent:
    """get_bedrock_agent 함수 테스트"""

    def test_returns_agent_instance(self):
        """BedrockAgent 인스턴스 반환 테스트"""
        with patch('src.bedrock_client.ChatBedrock'):
            import src.bedrock_client as bedrock_module
            bedrock_module._agent = None

            from src.bedrock_client import get_bedrock_agent, BedrockAgent
            agent = get_bedrock_agent()

            assert isinstance(agent, BedrockAgent)

    def test_singleton_pattern(self):
        """싱글톤 패턴 테스트"""
        with patch('src.bedrock_client.ChatBedrock'):
            import src.bedrock_client as bedrock_module
            bedrock_module._agent = None

            from src.bedrock_client import get_bedrock_agent

            agent1 = get_bedrock_agent()
            agent2 = get_bedrock_agent()

            assert agent1 is agent2
