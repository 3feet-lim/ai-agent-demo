"""
MCP (Model Context Protocol) 관리자
Grafana MCP와 CloudWatch MCP 서버를 관리하고 도구를 제공합니다.
"""
import logging
from typing import Optional, Any
from dataclasses import dataclass, field

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

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
    """개별 MCP 서버 연결 관리 (SSE/HTTP 기반)"""

    def __init__(self, name: str, url: str):
        self.name = name
        self.url = url
        self._session: Optional[ClientSession] = None
        self._http_client: Optional[httpx.AsyncClient] = None
        self._streamable_context = None
        self._session_context = None
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> bool:
        """MCP 서버에 SSE/HTTP로 연결"""
        try:
            logger.info(f"Attempting to connect to MCP server: {self.name}")
            logger.info(f"URL: {self.url}")

            # HTTP 클라이언트 생성
            self._http_client = httpx.AsyncClient(timeout=30.0)

            # Streamable HTTP 클라이언트 컨텍스트 시작
            self._streamable_context = streamable_http_client(
                self.url,
                http_client=self._http_client
            )
            read, write, _ = await self._streamable_context.__aenter__()

            # 세션 컨텍스트 시작
            self._session_context = ClientSession(read, write)
            self._session = await self._session_context.__aenter__()

            # 세션 초기화
            await self._session.initialize()

            self._connected = True
            logger.info(f"Successfully connected to MCP server: {self.name}")
            return True

        except httpx.ConnectError as e:
            logger.error(f"Connection error for {self.name}: {e}")
            self._connected = False
            await self._cleanup_contexts()
            return False
        except Exception as e:
            logger.error(f"Failed to connect to MCP server {self.name}: {e}")
            self._connected = False
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

        if self._streamable_context:
            try:
                await self._streamable_context.__aexit__(None, None, None)
            except Exception as e:
                logger.debug(f"Error closing streamable context for {self.name}: {e}")
            self._streamable_context = None

        if self._http_client:
            try:
                await self._http_client.aclose()
            except Exception as e:
                logger.debug(f"Error closing HTTP client for {self.name}: {e}")
            self._http_client = None

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
        """설정에 따라 MCP 서버 구성 (SSE/HTTP 기반)"""
        settings = get_settings()

        # 디버깅: 설정 값 로그
        logger.info(f"Grafana URL from settings: {settings.grafana_url}")
        logger.info(f"Grafana API Key exists: {bool(settings.grafana_api_key)}")

        # Grafana MCP 서버 설정 (Docker 컨테이너로 실행 중)
        # Grafana MCP는 /sse 엔드포인트 사용
        if settings.grafana_url and settings.grafana_api_key:
            grafana_mcp_url = settings.grafana_mcp_url or "http://grafana-mcp:8000/sse"
            self._servers["grafana"] = MCPServerConnection(
                name="grafana",
                url=grafana_mcp_url
            )
            logger.info(f"Grafana MCP server configured at {grafana_mcp_url}")

        # CloudWatch MCP 서버 설정 (Docker 컨테이너로 실행 중)
        # AWS MCP 서버들은 /mcp 엔드포인트 사용 (Streamable HTTP)
        if settings.aws_region:
            cloudwatch_mcp_url = settings.cloudwatch_mcp_url or "http://cloudwatch-mcp:8000/mcp"
            self._servers["cloudwatch"] = MCPServerConnection(
                name="cloudwatch",
                url=cloudwatch_mcp_url
            )
            logger.info(f"CloudWatch MCP server configured at {cloudwatch_mcp_url}")

            # AWS API MCP 서버 설정 (Docker 컨테이너로 실행 중)
            aws_api_mcp_url = settings.aws_api_mcp_url or "http://aws-api-mcp:8000/mcp"
            self._servers["aws-api"] = MCPServerConnection(
                name="aws-api",
                url=aws_api_mcp_url
            )
            logger.info(f"AWS API MCP server configured at {aws_api_mcp_url}")

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
