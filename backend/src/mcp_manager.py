"""
MCP (Model Context Protocol) 관리자
Grafana MCP와 CloudWatch MCP 서버를 관리하고 도구를 제공합니다.
"""
import asyncio
import logging
import os
from typing import Optional, Any
from dataclasses import dataclass, field
from contextlib import asynccontextmanager

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .config import get_settings

logger = logging.getLogger(__name__)


@dataclass
class MCPTool:
    """MCP 도구 정보"""
    name: str
    description: str
    input_schema: dict
    server_name: str


@dataclass
class MCPContext:
    """MCP에서 수집된 컨텍스트"""
    system_prompt: str = ""
    tools: list[MCPTool] = field(default_factory=list)
    resources: list[dict] = field(default_factory=list)


class MCPServerConnection:
    """개별 MCP 서버 연결 관리"""

    def __init__(self, name: str, command: str, args: list[str], env: dict[str, str] | None = None):
        self.name = name
        self.command = command
        self.args = args
        self.env = env or {}
        self._session: Optional[ClientSession] = None
        self._stdio_context = None
        self._session_context = None
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> bool:
        """MCP 서버에 연결"""
        try:
            # 환경변수 설정
            server_env = os.environ.copy()
            server_env.update(self.env)

            server_params = StdioServerParameters(
                command=self.command,
                args=self.args,
                env=server_env
            )

            logger.info(f"Attempting to connect to MCP server: {self.name}")
            logger.info(f"Command: {self.command} {' '.join(self.args)}")

            # stdio 클라이언트 컨텍스트 시작
            self._stdio_context = stdio_client(server_params)
            read, write = await self._stdio_context.__aenter__()

            # 세션 컨텍스트 시작
            self._session_context = ClientSession(read, write)
            self._session = await self._session_context.__aenter__()

            # 세션 초기화
            await self._session.initialize()

            self._connected = True
            logger.info(f"Successfully connected to MCP server: {self.name}")
            return True

        except FileNotFoundError as e:
            logger.error(f"MCP server command not found for {self.name}: {e}")
            self._connected = False
            return False
        except Exception as e:
            logger.error(f"Failed to connect to MCP server {self.name}: {e}")
            self._connected = False
            # 부분적으로 열린 컨텍스트 정리
            await self._cleanup_contexts()
            return False

    async def _cleanup_contexts(self):
        """컨텍스트 정리"""
        if self._session_context:
            try:
                await self._session_context.__aexit__(None, None, None)
            except Exception as e:
                logger.debug(f"Error closing session context for {self.name}: {e}")
            self._session_context = None
            self._session = None

        if self._stdio_context:
            try:
                await self._stdio_context.__aexit__(None, None, None)
            except Exception as e:
                logger.debug(f"Error closing stdio context for {self.name}: {e}")
            self._stdio_context = None

    async def disconnect(self):
        """MCP 서버 연결 해제"""
        await self._cleanup_contexts()
        self._connected = False
        logger.info(f"Disconnected from MCP server: {self.name}")

    async def list_tools(self) -> list[MCPTool]:
        """서버에서 제공하는 도구 목록 조회"""
        if not self._connected or not self._session:
            return []

        try:
            result = await self._session.list_tools()
            tools = []
            for tool in result.tools:
                tools.append(MCPTool(
                    name=tool.name,
                    description=tool.description or "",
                    input_schema=tool.inputSchema if hasattr(tool, 'inputSchema') else {},
                    server_name=self.name
                ))
            return tools
        except Exception as e:
            logger.error(f"Failed to list tools from {self.name}: {e}")
            return []

    async def call_tool(self, tool_name: str, arguments: dict) -> Any:
        """도구 실행"""
        if not self._connected or not self._session:
            raise RuntimeError(f"MCP server {self.name} not connected")

        try:
            result = await self._session.call_tool(tool_name, arguments)
            return result
        except Exception as e:
            logger.error(f"Failed to call tool {tool_name} on {self.name}: {e}")
            raise


class MCPManager:
    """
    MCP 서버 관리자

    Grafana MCP와 CloudWatch MCP 서버를 관리하고 통합된 인터페이스를 제공합니다.
    """

    def __init__(self):
        self._servers: dict[str, MCPServerConnection] = {}
        self._tools_cache: dict[str, MCPTool] = {}
        self._initialized = False

    @property
    def is_enabled(self) -> bool:
        """MCP가 활성화되어 있는지 확인"""
        settings = get_settings()
        return bool(settings.grafana_url) or bool(settings.aws_region)

    def _setup_servers(self):
        """설정에 따라 MCP 서버 구성"""
        settings = get_settings()

        # 디버깅: 설정 값 로그
        logger.info(f"Grafana URL from settings: {settings.grafana_url}")
        logger.info(f"Grafana API Key exists: {bool(settings.grafana_api_key)}")

        # Grafana MCP 서버 설정
        if settings.grafana_url and settings.grafana_api_key:
            self._servers["grafana"] = MCPServerConnection(
                name="grafana",
                command="uvx",
                args=["mcp-grafana"],
                env={
                    "GRAFANA_URL": settings.grafana_url,
                    "GRAFANA_API_KEY": settings.grafana_api_key,
                }
            )
            logger.info("Grafana MCP server configured")

        # CloudWatch MCP 서버 설정
        if settings.aws_region:
            env = {"AWS_REGION": settings.aws_region}
            if settings.aws_access_key_id:
                env["AWS_ACCESS_KEY_ID"] = settings.aws_access_key_id
            if settings.aws_secret_access_key:
                env["AWS_SECRET_ACCESS_KEY"] = settings.aws_secret_access_key
            if settings.aws_profile:
                env["AWS_PROFILE"] = settings.aws_profile

            self._servers["cloudwatch"] = MCPServerConnection(
                name="cloudwatch",
                command="uvx",
                args=["--from", "awslabs-cloudwatch-mcp-server", "awslabs.cloudwatch-mcp-server"],
                env=env
            )
            logger.info("CloudWatch MCP server configured")

            # AWS API MCP 서버 설정 (EC2 인스턴스 조회 등 AWS CLI 명령 실행)
            self._servers["aws-api"] = MCPServerConnection(
                name="aws-api",
                command="uvx",
                args=["awslabs.aws-api-mcp-server"],
                env=env
            )
            logger.info("AWS API MCP server configured")

    async def connect(self) -> bool:
        """모든 MCP 서버에 연결"""
        if self._initialized:
            return True

        self._setup_servers()

        if not self._servers:
            logger.info("No MCP servers configured")
            return False

        connected_count = 0
        for name, server in self._servers.items():
            try:
                if await server.connect():
                    connected_count += 1
                    # 도구 목록 캐시
                    tools = await server.list_tools()
                    for tool in tools:
                        self._tools_cache[tool.name] = tool
                        logger.info(f"Registered tool: {tool.name} from {name}")
            except Exception as e:
                logger.error(f"Failed to connect to {name}: {e}")

        self._initialized = True
        return connected_count > 0

    async def disconnect(self):
        """모든 MCP 서버 연결 해제"""
        for server in self._servers.values():
            await server.disconnect()
        self._servers.clear()
        self._tools_cache.clear()
        self._initialized = False
        logger.info("All MCP servers disconnected")

    async def get_context(self) -> MCPContext:
        """
        MCP 서버들에서 컨텍스트 수집

        Returns:
            MCPContext: 시스템 프롬프트, 도구, 리소스 정보
        """
        tools = list(self._tools_cache.values())

        return MCPContext(
            system_prompt=self._build_system_prompt(tools),
            tools=tools,
            resources=[]
        )

    def _build_system_prompt(self, tools: list[MCPTool]) -> str:
        """도구 목록을 포함한 시스템 프롬프트 생성"""
        base_prompt = """당신은 도움이 되는 AI 어시스턴트입니다.
사용자의 질문에 정확하고 친절하게 답변해 주세요.
한국어로 대화합니다."""

        if not tools:
            return base_prompt

        tool_descriptions = []
        for tool in tools:
            tool_descriptions.append(f"- {tool.name}: {tool.description}")

        tools_section = "\n\n사용 가능한 도구:\n" + "\n".join(tool_descriptions)

        return base_prompt + tools_section

    def get_tools_for_langchain(self) -> list[dict]:
        """LangChain 형식의 도구 목록 반환"""
        tools = []
        for tool in self._tools_cache.values():
            tools.append({
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
            })
        return tools

    async def execute_tool(self, tool_name: str, arguments: dict) -> Any:
        """
        MCP 도구 실행

        Args:
            tool_name: 실행할 도구 이름
            arguments: 도구 인자

        Returns:
            도구 실행 결과
        """
        if tool_name not in self._tools_cache:
            raise ValueError(f"Unknown tool: {tool_name}")

        tool = self._tools_cache[tool_name]
        server = self._servers.get(tool.server_name)

        if not server or not server.is_connected:
            raise RuntimeError(f"MCP server {tool.server_name} not connected")

        logger.info(f"Executing MCP tool: {tool_name} on {tool.server_name}")
        return await server.call_tool(tool_name, arguments)

    def get_available_servers(self) -> list[str]:
        """연결된 MCP 서버 목록 반환"""
        return [name for name, server in self._servers.items() if server.is_connected]

    def get_available_tools(self) -> list[str]:
        """사용 가능한 도구 이름 목록 반환"""
        return list(self._tools_cache.keys())


# 싱글톤 인스턴스
_manager: Optional[MCPManager] = None


async def get_mcp_manager() -> MCPManager:
    """MCP 관리자 싱글톤 반환"""
    global _manager
    if _manager is None:
        _manager = MCPManager()
        await _manager.connect()
    return _manager


async def shutdown_mcp_manager():
    """MCP 관리자 종료"""
    global _manager
    if _manager:
        await _manager.disconnect()
        _manager = None
