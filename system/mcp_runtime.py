from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from system.config import settings
from system.tools import CONFIRM, SAFE, ToolResult


LOGGER = logging.getLogger(__name__)


def _read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        LOGGER.exception("Failed to read JSON from %s", path)
        return default


@dataclass(slots=True)
class MCPServerDefinition:
    name: str
    command: list[str]
    cwd: str = ""
    env: dict[str, str] = field(default_factory=dict)
    enabled: bool = True


@dataclass(slots=True)
class MCPServerState:
    name: str
    enabled: bool
    connected: bool = False
    last_error: str = ""
    tools: list[dict[str, Any]] = field(default_factory=list)
    resources: list[dict[str, Any]] = field(default_factory=list)
    prompts: list[dict[str, Any]] = field(default_factory=list)
    last_call: dict[str, Any] = field(default_factory=dict)


class MCPError(RuntimeError):
    pass


class StdioMCPClient:
    def __init__(self, definition: MCPServerDefinition) -> None:
        self.definition = definition
        self._process: subprocess.Popen[str] | None = None
        self._lock = threading.RLock()
        self._request_id = 0
        self._initialized = False

    def start(self) -> None:
        with self._lock:
            if self._process and self._process.poll() is None:
                return
            env = os.environ.copy()
            env.update({key: str(value) for key, value in self.definition.env.items()})
            self._process = subprocess.Popen(
                self.definition.command,
                cwd=self.definition.cwd or None,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self._initialized = False

    def close(self) -> None:
        with self._lock:
            if self._process and self._process.poll() is None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=1)
                except Exception:
                    self._process.kill()
            if self._process:
                for handle_name in ("stdin", "stdout", "stderr"):
                    handle = getattr(self._process, handle_name, None)
                    if handle:
                        try:
                            handle.close()
                        except Exception:
                            pass
            self._process = None
            self._initialized = False

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _initialize(self) -> None:
        if self._initialized:
            return
        self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "clientInfo": {"name": "jarvis", "version": "1.0"},
                "capabilities": {},
            },
        )
        self.notify("notifications/initialized", {})
        self._initialized = True

    def _write_message(self, payload: dict[str, Any]) -> None:
        if not self._process or not self._process.stdin:
            raise MCPError("MCP process is not running.")
        encoded = json.dumps(payload).encode("utf-8")
        header = f"Content-Length: {len(encoded)}\r\n\r\n".encode("ascii")
        self._process.stdin.write(header + encoded)
        self._process.stdin.flush()

    def _read_exact(self, size: int) -> bytes:
        if not self._process or not self._process.stdout:
            raise MCPError("MCP process is not running.")
        body = self._process.stdout.read(size)
        if len(body) != size:
            raise MCPError("Unexpected EOF from MCP server.")
        return body

    def _read_message(self) -> dict[str, Any]:
        if not self._process or not self._process.stdout:
            raise MCPError("MCP process is not running.")
        headers: dict[str, str] = {}
        while True:
            line = self._process.stdout.readline()
            if not line:
                stderr_text = ""
                if self._process.stderr:
                    try:
                        stderr_text = self._process.stderr.read().decode("utf-8", errors="ignore").strip()
                    except Exception:
                        stderr_text = ""
                raise MCPError(stderr_text or "MCP server closed the connection.")
            if line in {b"\r\n", b"\n"}:
                break
            decoded = line.decode("ascii", errors="ignore").strip()
            if ":" in decoded:
                key, value = decoded.split(":", 1)
                headers[key.strip().lower()] = value.strip()
        length = int(headers.get("content-length", "0"))
        if length <= 0:
            raise MCPError("MCP response did not include a valid Content-Length header.")
        body = self._read_exact(length)
        return json.loads(body.decode("utf-8"))

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            self.start()
            if method != "initialize" and not self._initialized:
                self._initialize()
            request_id = self._next_id()
            self._write_message({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
            while True:
                message = self._read_message()
                if "id" not in message:
                    continue
                if message["id"] != request_id:
                    continue
                if "error" in message:
                    error = message["error"]
                    raise MCPError(str(error.get("message") if isinstance(error, dict) else error))
                return dict(message.get("result", {}))

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        with self._lock:
            self.start()
            if method != "notifications/initialized" and not self._initialized:
                self._initialize()
            self._write_message({"jsonrpc": "2.0", "method": method, "params": params or {}})


class MCPManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._clients: dict[str, StdioMCPClient] = {}
        self._states: dict[str, MCPServerState] = {}

    def _load_definitions(self) -> dict[str, MCPServerDefinition]:
        raw = dict(settings.mcp_servers)
        if settings.mcp_servers_path.exists():
            raw.update(_read_json(settings.mcp_servers_path, {}))
        definitions: dict[str, MCPServerDefinition] = {}
        for name, payload in raw.items():
            if not isinstance(payload, dict):
                continue
            command = payload.get("command")
            if isinstance(command, str):
                command = [command]
            if not isinstance(command, list) or not command:
                continue
            definitions[name] = MCPServerDefinition(
                name=name,
                command=[str(part) for part in command],
                cwd=str(payload.get("cwd", "")),
                env={str(key): str(value) for key, value in dict(payload.get("env", {})).items()},
                enabled=bool(payload.get("enabled", True)),
            )
        return definitions

    def _ensure_state(self, definition: MCPServerDefinition) -> MCPServerState:
        state = self._states.get(definition.name)
        if state is None:
            state = MCPServerState(name=definition.name, enabled=definition.enabled)
            self._states[definition.name] = state
        state.enabled = definition.enabled
        return state

    def configured_servers(self) -> dict[str, MCPServerDefinition]:
        return self._load_definitions()

    def server_names(self) -> list[str]:
        return sorted(self.configured_servers().keys())

    def resolve_server_name(self, name: str) -> str | None:
        normalized = name.strip().lower()
        if not normalized:
            return None
        names = self.server_names()
        exact = [candidate for candidate in names if candidate.lower() == normalized]
        if exact:
            return exact[0]
        partial = [candidate for candidate in names if normalized in candidate.lower()]
        if len(partial) == 1:
            return partial[0]
        return None

    def _connect(self, name: str) -> tuple[StdioMCPClient | None, MCPServerState]:
        definitions = self.configured_servers()
        if name not in definitions:
            raise MCPError(f"Unknown MCP server: {name}")
        definition = definitions[name]
        state = self._ensure_state(definition)
        if not definition.enabled:
            state.connected = False
            state.last_error = "Server is disabled."
            return None, state
        client = self._clients.get(name)
        if client is None:
            client = StdioMCPClient(definition)
            self._clients[name] = client
        try:
            client.start()
            state.connected = True
            state.last_error = ""
            return client, state
        except Exception as exc:
            state.connected = False
            state.last_error = str(exc)
            raise

    def list_servers(self) -> ToolResult:
        items = []
        for definition in self.configured_servers().values():
            state = self._ensure_state(definition)
            items.append(
                {
                    "name": definition.name,
                    "enabled": definition.enabled,
                    "connected": state.connected,
                    "last_error": state.last_error,
                }
            )
        return ToolResult(True, f"Configured {len(items)} MCP server(s).", {"source_kind": "mcp", "servers": items})

    def server_status(self, name: str) -> ToolResult:
        resolved = self.resolve_server_name(name)
        if not resolved:
            return ToolResult(False, f"I need the exact MCP server name for '{name}'.", {"reason": "clarification_needed"})
        definition = self.configured_servers()[resolved]
        state = self._ensure_state(definition)
        try:
            self._connect(resolved)
        except Exception:
            pass
        return ToolResult(
            True,
            f"MCP server {resolved} is {'connected' if state.connected else 'disconnected'}.",
            {"source_kind": "mcp", "server_name": resolved, "status": asdict(state)},
        )

    def _list_metadata(self, name: str, method: str, key: str) -> ToolResult:
        resolved = self.resolve_server_name(name)
        if not resolved:
            return ToolResult(False, f"I need the exact MCP server name for '{name}'.", {"reason": "clarification_needed"})
        client, state = self._connect(resolved)
        assert client is not None
        payload = client.request(method, {})
        items = list(payload.get(key, []))
        setattr(state, key, items)
        return ToolResult(
            True,
            f"Loaded {len(items)} {key} from {resolved}.",
            {"source_kind": "mcp", "server_name": resolved, key: items},
        )

    def list_server_tools(self, name: str) -> ToolResult:
        return self._list_metadata(name, "tools/list", "tools")

    def list_server_resources(self, name: str) -> ToolResult:
        return self._list_metadata(name, "resources/list", "resources")

    def call_tool(self, server_name: str, tool_name: str, arguments: dict[str, Any] | None = None) -> ToolResult:
        resolved = self.resolve_server_name(server_name)
        if not resolved:
            return ToolResult(False, f"I need the exact MCP server name for '{server_name}'.", {"reason": "clarification_needed"})
        client, state = self._connect(resolved)
        assert client is not None
        payload = client.request("tools/call", {"name": tool_name, "arguments": arguments or {}})
        state.last_call = {"type": "tool", "name": tool_name, "ok": True}
        risk = CONFIRM if self._tool_requires_confirmation(tool_name) else SAFE
        return ToolResult(
            True,
            f"MCP tool {tool_name} ran on {resolved}.",
            {"source_kind": "mcp", "server_name": resolved, "tool_name": tool_name, "risk": risk, "result": payload},
        )

    def read_resource(self, server_name: str, uri: str) -> ToolResult:
        resolved = self.resolve_server_name(server_name)
        if not resolved:
            return ToolResult(False, f"I need the exact MCP server name for '{server_name}'.", {"reason": "clarification_needed"})
        client, state = self._connect(resolved)
        assert client is not None
        payload = client.request("resources/read", {"uri": uri})
        state.last_call = {"type": "resource", "name": uri, "ok": True}
        return ToolResult(
            True,
            f"Read MCP resource {uri} from {resolved}.",
            {"source_kind": "mcp", "server_name": resolved, "uri": uri, "result": payload},
        )

    def reconnect_server(self, name: str) -> ToolResult:
        resolved = self.resolve_server_name(name)
        if not resolved:
            return ToolResult(False, f"I need the exact MCP server name for '{name}'.", {"reason": "clarification_needed"})
        client = self._clients.pop(resolved, None)
        if client:
            client.close()
        try:
            self._connect(resolved)
            return ToolResult(True, f"Reconnected MCP server {resolved}.", {"source_kind": "mcp", "server_name": resolved})
        except Exception as exc:
            return ToolResult(False, f"Failed to reconnect MCP server {resolved}: {exc}", {"source_kind": "mcp", "server_name": resolved})

    def disable_server(self, name: str) -> ToolResult:
        resolved = self.resolve_server_name(name)
        if not resolved:
            return ToolResult(False, f"I need the exact MCP server name for '{name}'.", {"reason": "clarification_needed"})
        definitions = _read_json(settings.mcp_servers_path, {})
        payload = dict(definitions.get(resolved, {}))
        payload["enabled"] = False
        definitions[resolved] = payload
        settings.mcp_servers_path.write_text(json.dumps(definitions, indent=2), encoding="utf-8")
        state = self._states.setdefault(resolved, MCPServerState(name=resolved, enabled=False))
        state.enabled = False
        state.connected = False
        state.last_error = "Server disabled by Jarvis."
        client = self._clients.pop(resolved, None)
        if client:
            client.close()
        return ToolResult(True, f"Disabled MCP server {resolved}.", {"source_kind": "mcp", "server_name": resolved})

    def _tool_requires_confirmation(self, tool_name: str) -> bool:
        lowered = tool_name.lower()
        return any(token in lowered for token in ("delete", "remove", "write", "update", "modify"))

    def state_snapshot(self) -> dict[str, Any]:
        definitions = self.configured_servers()
        snapshot = []
        for name, definition in definitions.items():
            state = self._ensure_state(definition)
            snapshot.append(asdict(state))
        return {"servers": snapshot}


_MCP_MANAGER = MCPManager()


def get_mcp_manager() -> MCPManager:
    return _MCP_MANAGER
