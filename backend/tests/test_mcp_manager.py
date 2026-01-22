"""
mcp_manager.py 단위 테스트
"""
import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from src.mcp_manager import MCPManager, MCPContext, MCPTool, MCPServerConnection


class TestMCPTool:
    """MCPTool 데이터 클래스 테스트"""

    def test_create_tool(self):
        """도구 생성 테스트"""
        tool = MCPTool(
            name="test_tool",
            description="테스트 도구",
            input_schema={"type": "object"},
            server_name="test_server"
        )

        assert tool.name == "test_tool"
        assert tool.description == "테스트 도구"
        assert tool.server_name == "test_server"


class TestMCPContext:
    """MCPContext 데이터 클래스 테스트"""

    def test_default_values(self):
        """기본값 테스트"""
        context = MCPContext()

        assert context.system_prompt == ""
        assert context.tools == []
        assert context.resources == []

    def test_custom_values(self):
        """커스텀 값 테스트"""
        tool = MCPTool(
            name="tool1",
            description="desc",
            input_schema={},
            server_name="server1"
        )
        context = MCPContext(
            system_prompt="테스트 프롬프트",
            tools=[tool],
            resources=[{"uri": "resource1"}]
        )

        assert context.system_prompt == "테스트 프롬프트"
        assert len(context.tools) == 1
        assert len(context.resources) == 1


class TestMCPServerConnection:
    """MCPServerConnection 클래스 테스트"""

    def test_create_connection(self):
        """연결 객체 생성 테스트"""
        conn = MCPServerConnection(
            name="test",
            command="python",
            args=["-m", "test_module"],
            env={"TEST_VAR": "value"}
        )

        assert conn.name == "test"
        assert conn.command == "python"
        assert conn.args == ["-m", "test_module"]
        assert conn.env == {"TEST_VAR": "value"}
        assert conn.is_connected is False

    def test_default_env(self):
        """기본 환경변수 테스트"""
        conn = MCPServerConnection(
            name="test",
            command="python",
            args=[]
        )

        assert conn.env == {}


class TestMCPManager:
    """MCPManager 클래스 테스트"""

    @patch('src.mcp_manager.get_settings')
    def test_is_enabled_with_grafana(self, mock_settings):
        """Grafana 설정 시 활성화 테스트"""
        mock_settings.return_value = MagicMock(
            grafana_url="http://grafana.local",
            grafana_api_key="key",
            aws_region=None
        )

        manager = MCPManager()
        assert manager.is_enabled is True

    @patch('src.mcp_manager.get_settings')
    def test_is_enabled_with_aws(self, mock_settings):
        """AWS 설정 시 활성화 테스트"""
        mock_settings.return_value = MagicMock(
            grafana_url=None,
            aws_region="ap-northeast-2"
        )

        manager = MCPManager()
        assert manager.is_enabled is True

    @patch('src.mcp_manager.get_settings')
    def test_is_enabled_without_config(self, mock_settings):
        """설정 없을 때 비활성화 테스트"""
        mock_settings.return_value = MagicMock(
            grafana_url=None,
            aws_region=None
        )

        manager = MCPManager()
        assert manager.is_enabled is False

    @patch('src.mcp_manager.get_settings')
    def test_setup_servers_grafana(self, mock_settings):
        """Grafana 서버 설정 테스트"""
        mock_settings.return_value = MagicMock(
            grafana_url="http://grafana.local",
            grafana_api_key="test_key",
            aws_region=None,
            aws_access_key_id=None,
            aws_secret_access_key=None,
            aws_profile=None
        )

        manager = MCPManager()
        manager._setup_servers()

        assert "grafana" in manager._servers
        assert manager._servers["grafana"].env["GRAFANA_URL"] == "http://grafana.local"
        assert manager._servers["grafana"].env["GRAFANA_API_KEY"] == "test_key"

    @patch('src.mcp_manager.get_settings')
    def test_setup_servers_cloudwatch(self, mock_settings):
        """CloudWatch 서버 설정 테스트"""
        mock_settings.return_value = MagicMock(
            grafana_url=None,
            grafana_api_key=None,
            aws_region="ap-northeast-2",
            aws_access_key_id="test_key_id",
            aws_secret_access_key="test_secret",
            aws_profile=None
        )

        manager = MCPManager()
        manager._setup_servers()

        assert "cloudwatch" in manager._servers
        assert manager._servers["cloudwatch"].env["AWS_REGION"] == "ap-northeast-2"
        assert manager._servers["cloudwatch"].env["AWS_ACCESS_KEY_ID"] == "test_key_id"

    @pytest.mark.asyncio
    @patch('src.mcp_manager.get_settings')
    async def test_connect_no_servers(self, mock_settings):
        """서버 없을 때 연결 테스트"""
        mock_settings.return_value = MagicMock(
            grafana_url=None,
            grafana_api_key=None,
            aws_region=None,
            aws_access_key_id=None,
            aws_secret_access_key=None,
            aws_profile=None
        )

        manager = MCPManager()
        result = await manager.connect()

        assert result is False
        assert len(manager._servers) == 0

    @pytest.mark.asyncio
    @patch('src.mcp_manager.get_settings')
    async def test_disconnect(self, mock_settings):
        """연결 해제 테스트"""
        mock_settings.return_value = MagicMock(
            grafana_url=None,
            aws_region=None
        )

        manager = MCPManager()
        manager._initialized = True

        await manager.disconnect()

        assert manager._initialized is False
        assert len(manager._servers) == 0
        assert len(manager._tools_cache) == 0

    @pytest.mark.asyncio
    @patch('src.mcp_manager.get_settings')
    async def test_get_context_returns_default(self, mock_settings):
        """기본 컨텍스트 반환 테스트"""
        mock_settings.return_value = MagicMock(
            grafana_url=None,
            aws_region=None
        )

        manager = MCPManager()
        context = await manager.get_context()

        assert isinstance(context, MCPContext)
        assert "AI 어시스턴트" in context.system_prompt
        assert "한국어" in context.system_prompt

    @pytest.mark.asyncio
    @patch('src.mcp_manager.get_settings')
    async def test_get_context_with_tools(self, mock_settings):
        """도구가 있을 때 컨텍스트 테스트"""
        mock_settings.return_value = MagicMock(
            grafana_url=None,
            aws_region=None
        )

        manager = MCPManager()
        manager._tools_cache["test_tool"] = MCPTool(
            name="test_tool",
            description="테스트 도구입니다",
            input_schema={},
            server_name="test"
        )

        context = await manager.get_context()

        assert "test_tool" in context.system_prompt
        assert "테스트 도구입니다" in context.system_prompt

    @patch('src.mcp_manager.get_settings')
    def test_get_tools_for_langchain(self, mock_settings):
        """LangChain 형식 도구 반환 테스트"""
        mock_settings.return_value = MagicMock()

        manager = MCPManager()
        manager._tools_cache["tool1"] = MCPTool(
            name="tool1",
            description="설명1",
            input_schema={"type": "object"},
            server_name="server1"
        )

        tools = manager.get_tools_for_langchain()

        assert len(tools) == 1
        assert tools[0]["name"] == "tool1"
        assert tools[0]["description"] == "설명1"

    @pytest.mark.asyncio
    @patch('src.mcp_manager.get_settings')
    async def test_execute_tool_unknown_raises(self, mock_settings):
        """알 수 없는 도구 실행 시 예외 테스트"""
        mock_settings.return_value = MagicMock()

        manager = MCPManager()

        with pytest.raises(ValueError, match="Unknown tool"):
            await manager.execute_tool("unknown_tool", {})

    @patch('src.mcp_manager.get_settings')
    def test_get_available_servers(self, mock_settings):
        """연결된 서버 목록 테스트"""
        mock_settings.return_value = MagicMock()

        manager = MCPManager()

        mock_server = MagicMock()
        mock_server.is_connected = True
        manager._servers["test"] = mock_server

        servers = manager.get_available_servers()
        assert "test" in servers

    @patch('src.mcp_manager.get_settings')
    def test_get_available_tools(self, mock_settings):
        """사용 가능한 도구 목록 테스트"""
        mock_settings.return_value = MagicMock()

        manager = MCPManager()
        manager._tools_cache["tool1"] = MagicMock()
        manager._tools_cache["tool2"] = MagicMock()

        tools = manager.get_available_tools()
        assert "tool1" in tools
        assert "tool2" in tools


class TestGetMCPManager:
    """get_mcp_manager 함수 테스트"""

    @pytest.mark.asyncio
    @patch('src.mcp_manager.get_settings')
    async def test_returns_manager_instance(self, mock_settings):
        """MCPManager 인스턴스 반환 테스트"""
        mock_settings.return_value = MagicMock(
            grafana_url=None,
            aws_region=None
        )

        import src.mcp_manager as mcp_module
        mcp_module._manager = None

        from src.mcp_manager import get_mcp_manager
        manager = await get_mcp_manager()

        assert isinstance(manager, MCPManager)

    @pytest.mark.asyncio
    @patch('src.mcp_manager.get_settings')
    async def test_singleton_pattern(self, mock_settings):
        """싱글톤 패턴 테스트"""
        mock_settings.return_value = MagicMock(
            grafana_url=None,
            aws_region=None
        )

        import src.mcp_manager as mcp_module
        mcp_module._manager = None

        from src.mcp_manager import get_mcp_manager

        manager1 = await get_mcp_manager()
        manager2 = await get_mcp_manager()

        assert manager1 is manager2
