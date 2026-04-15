from __future__ import annotations

import inspect
from typing import Any

from core.agents import get_agent_manager
from system.mcp_runtime import get_mcp_manager
from system.tools import CONFIRM, SAFE, describe_tools, resolve_handler as resolve_builtin_handler


def _agent_manager():
    return get_agent_manager()


def _mcp_manager():
    return get_mcp_manager()


MCP_OPERATIONS: dict[str, dict[str, Any]] = {
    "list_servers": {"handler": _mcp_manager().list_servers, "risk": SAFE},
    "server_status": {"handler": _mcp_manager().server_status, "risk": SAFE},
    "list_server_tools": {"handler": _mcp_manager().list_server_tools, "risk": SAFE},
    "list_server_resources": {"handler": _mcp_manager().list_server_resources, "risk": SAFE},
    "call_tool": {"handler": _mcp_manager().call_tool, "risk": CONFIRM},
    "read_resource": {"handler": _mcp_manager().read_resource, "risk": SAFE},
    "reconnect_server": {"handler": _mcp_manager().reconnect_server, "risk": SAFE},
    "disable_server": {"handler": _mcp_manager().disable_server, "risk": CONFIRM},
}

AGENT_OPERATIONS: dict[str, dict[str, Any]] = {
    "start_agent": {"handler": _agent_manager().start_agent, "risk": SAFE},
    "list_agents": {"handler": _agent_manager().list_agents, "risk": SAFE},
    "agent_status": {"handler": _agent_manager().agent_status, "risk": SAFE},
    "cancel_agent": {"handler": _agent_manager().cancel_agent, "risk": CONFIRM},
    "agent_result": {"handler": _agent_manager().agent_result, "risk": SAFE},
    "clear_completed_agents": {"handler": _agent_manager().clear_completed_agents, "risk": CONFIRM},
}


def describe_capabilities() -> list[dict[str, Any]]:
    described = list(describe_tools())
    for tool, operations in (("mcp", MCP_OPERATIONS), ("agent", AGENT_OPERATIONS)):
        for operation, metadata in operations.items():
            handler = metadata["handler"]
            sig = inspect.signature(handler)
            params = [
                {"name": name, "type": str(param.annotation), "required": param.default == inspect.Parameter.empty}
                for name, param in sig.parameters.items()
            ]
            described.append(
                {
                    "tool": tool,
                    "operation": operation,
                    "risk": metadata["risk"],
                    "parameters": params,
                }
            )
    snapshot = _mcp_manager().state_snapshot()
    described.append({"tool": "mcp", "operation": "configured_servers", "risk": SAFE, "servers": snapshot.get("servers", [])})
    return described


def resolve_handler(tool: str, operation: str) -> tuple[Any, str] | tuple[None, None]:
    if tool == "mcp":
        data = MCP_OPERATIONS.get(operation)
        if not data:
            return None, None
        return data["handler"], data["risk"]
    if tool == "agent":
        data = AGENT_OPERATIONS.get(operation)
        if not data:
            return None, None
        return data["handler"], data["risk"]
    return resolve_builtin_handler(tool, operation)
