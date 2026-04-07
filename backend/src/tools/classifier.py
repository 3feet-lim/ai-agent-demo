"""
MCP 도구 → sub-agent 역할 분류
"""
from ..mcp_manager import MCPTool


# MCP 서버별 도구 → sub-agent 매핑
_TOOL_ROUTING = {
    "metric": {
        "servers": {"grafana"},
        "tools": set(),
    },
    "log": {
        "servers": {"cloudwatch"},
        "tools": set(),
    },
    "resource": {
        "servers": {"aws-api"},
        "tools": set(),
    },
    "network": {
        "servers": set(),
        "tools": set(),
    },
}


def classify_tool(mcp_tool: MCPTool) -> list[str]:
    """MCP 도구가 어떤 sub-agent에 속하는지 분류. 서버 기반으로 1:1 매핑."""
    for role, config in _TOOL_ROUTING.items():
        if mcp_tool.server_name in config["servers"]:
            return [role]
        if mcp_tool.name in config["tools"]:
            return [role]
    # 분류 안 된 도구는 resource에 기본 배정
    return ["resource"]
