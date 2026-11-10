"""MCP (Model Context Protocol) client.

Connects to the servers declared in `[mcp_servers]` and exposes their tools to
the agent in the same OpenAI function-calling shape as the built-in tools, so
the executor does not need to care where a tool came from.

Two transports are supported:

* **stdio** — the server is launched as a subprocess (`command` + `args`).
  This covers the majority of published servers.
* **streamable HTTP** — the server is reached over a URL, for hosted or
  internal servers.

Design notes:

* Tool names are namespaced `mcp__<server>__<tool>` so a server cannot shadow a
  built-in tool (or another server's tool) by naming collision.
* A server that fails to connect is recorded and skipped; it never takes the
  agent down with it.
* Connections are held open for the lifetime of the manager via an
  AsyncExitStack, so each tool call does not pay process-spawn cost.
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, Optional

TOOL_NAME_PREFIX = "mcp"
NAME_SEPARATOR = "__"
# OpenAI-compatible function names: letters, digits, underscore, dash, <= 64 chars.
_SAFE_NAME = re.compile(r"[^a-zA-Z0-9_-]")
MAX_TOOL_NAME_LENGTH = 64

DEFAULT_CONNECT_TIMEOUT = 30.0
DEFAULT_CALL_TIMEOUT = 120.0
MAX_TOOL_RESULT_CHARS = 100_000


def sanitize_name_part(value: str) -> str:
    """Make a server or tool name safe to embed in a function name."""
    return _SAFE_NAME.sub("_", value.strip())


def namespaced_tool_name(server: str, tool: str) -> str:
    """Build the agent-visible name for a remote tool."""
    name = f"{TOOL_NAME_PREFIX}{NAME_SEPARATOR}{sanitize_name_part(server)}{NAME_SEPARATOR}{sanitize_name_part(tool)}"
    if len(name) > MAX_TOOL_NAME_LENGTH:
        # Keep the prefix and server visible; truncating the tail still leaves a
        # unique-enough name because the remote name is looked up by mapping.
        name = name[:MAX_TOOL_NAME_LENGTH]
    return name


def is_mcp_tool_name(name: str) -> bool:
    return name.startswith(f"{TOOL_NAME_PREFIX}{NAME_SEPARATOR}")


@dataclass
class McpServerConfig:
    """One entry from the `[mcp_servers]` config table."""

    name: str
    command: Optional[str] = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: Optional[str] = None
    url: Optional[str] = None
    headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    timeout: float = DEFAULT_CONNECT_TIMEOUT

    @property
    def transport(self) -> str:
        return "http" if self.url else "stdio"

    def describe(self) -> str:
        if self.url:
            return self.url
        return " ".join([self.command or "", *self.args]).strip()

    def validate(self) -> Optional[str]:
        """Return an error message if this entry can't be used."""
        if self.url and self.command:
            return (
                f"MCP server '{self.name}' sets both 'command' and 'url'; "
                "use one transport per server."
            )
        if not self.url and not self.command:
            return f"MCP server '{self.name}' needs either 'command' (stdio) or 'url' (http)."
        if self.url and not re.match(r"^https?://", self.url):
            return f"MCP server '{self.name}' has a url that is not http(s): {self.url}"
        if self.command and shutil.which(self.command) is None and not self.command.startswith("/"):
            return (
                f"MCP server '{self.name}' command '{self.command}' was not found on PATH."
            )
        return None

    def trust_digest_source(self) -> str:
        """Stable text describing what would be executed, for the trust store."""
        return json.dumps(
            {
                "name": self.name,
                "command": self.command,
                "args": self.args,
                # Values matter here: PYTHONPATH, NODE_OPTIONS, and similar
                # variables can change which code a previously approved
                # command executes.  The resulting source is hashed before it
                # reaches the trust store; it is never displayed or persisted
                # in plaintext.
                "env": self.env,
                "cwd": self.cwd,
                "url": self.url,
                "headers": self.headers,
                "enabled": self.enabled,
                "timeout": self.timeout,
            },
            sort_keys=True,
        )


@dataclass
class McpTool:
    """A remote tool, adapted to the agent's tool interface."""

    server: str
    remote_name: str
    local_name: str
    definition: dict[str, Any]
    read_only: Optional[bool] = None


@dataclass
class McpServerStatus:
    name: str
    transport: str
    target: str
    connected: bool
    tool_count: int = 0
    error: Optional[str] = None


def _tool_to_definition(server: str, tool: Any) -> McpTool:
    """Convert an MCP Tool into an OpenAI function-calling definition."""
    local_name = namespaced_tool_name(server, tool.name)

    schema = getattr(tool, "input_schema", None) or {"type": "object", "properties": {}}
    if not isinstance(schema, dict):
        schema = {"type": "object", "properties": {}}
    # Some servers omit 'type'; the function-calling APIs expect an object schema.
    schema = {**schema}
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})

    description = (getattr(tool, "description", None) or f"{tool.name} (via {server})").strip()
    description = f"[{server}] {description}"
    annotations = getattr(tool, "annotations", None)
    read_only = None
    if annotations is not None:
        read_only = getattr(annotations, "read_only_hint", None)
        if read_only is None:
            read_only = getattr(annotations, "readOnlyHint", None)

    return McpTool(
        server=server,
        remote_name=tool.name,
        local_name=local_name,
        definition={
            "type": "function",
            "function": {
                "name": local_name,
                "description": description,
                "parameters": schema,
            },
        },
        read_only=read_only,
    )


def describe_exception(exc: BaseException, _depth: int = 0) -> str:
    """Render an exception for the user, flattening TaskGroup ExceptionGroups.

    anyio wraps transport failures in an ExceptionGroup whose own message
    ("unhandled errors in a TaskGroup") says nothing useful, so dig out the
    leaves — that's where "No such file or directory" actually lives.
    """
    nested = getattr(exc, "exceptions", None)
    if nested and _depth < 5:
        inner = [describe_exception(item, _depth + 1) for item in nested]
        unique = list(dict.fromkeys(part for part in inner if part))
        if unique:
            return "; ".join(unique)

    message = str(exc).strip()
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def _result_to_text(result: Any) -> str:
    """Flatten a CallToolResult into the plain string the executor expects."""
    parts: list[str] = []

    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
            continue
        # Non-text blocks (images, embedded resources) can't go into a text
        # transcript; describe them so the model knows something came back.
        block_type = getattr(block, "type", "unknown")
        parts.append(f"[{block_type} content omitted]")

    if not parts:
        structured = getattr(result, "structured_content", None)
        if structured is not None:
            try:
                parts.append(json.dumps(structured, indent=2, default=str))
            except (TypeError, ValueError):
                parts.append(str(structured))

    text = "\n".join(parts).strip() or "(tool returned no content)"

    if len(text) > MAX_TOOL_RESULT_CHARS:
        text = text[:MAX_TOOL_RESULT_CHARS] + "\n... [MCP result truncated]"

    if getattr(result, "is_error", False):
        return f"Error from MCP tool: {text}"
    return text


class McpManager:
    """Owns MCP connections and routes tool calls to the right server."""

    def __init__(self, configs: list[McpServerConfig]):
        self.configs = [c for c in configs if c.enabled]
        self._stack: Optional[AsyncExitStack] = None
        self._clients: dict[str, Any] = {}
        self._tools: dict[str, McpTool] = {}
        self.statuses: list[McpServerStatus] = []

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def __aenter__(self) -> "McpManager":
        await self.connect()
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.aclose()

    async def connect(self) -> list[McpServerStatus]:
        """Connect to every enabled server. Failures are recorded, not raised."""
        if not self.configs:
            return []

        self._stack = AsyncExitStack()
        await self._stack.__aenter__()

        for config in self.configs:
            status = McpServerStatus(
                name=config.name,
                transport=config.transport,
                target=config.describe(),
                connected=False,
            )

            problem = config.validate()
            if problem:
                status.error = problem
                self.statuses.append(status)
                continue

            try:
                client = await asyncio.wait_for(
                    self._open_client(config), timeout=config.timeout
                )
                listed = await asyncio.wait_for(
                    client.list_tools(), timeout=config.timeout
                )
            except asyncio.TimeoutError:
                status.error = f"timed out after {config.timeout:g}s"
                self.statuses.append(status)
                continue
            except Exception as exc:  # noqa: BLE001 - one server must not sink the rest
                status.error = describe_exception(exc)
                self.statuses.append(status)
                continue

            self._clients[config.name] = client
            for tool in listed.tools:
                adapted = _tool_to_definition(config.name, tool)
                if adapted.local_name in self._tools:
                    continue
                self._tools[adapted.local_name] = adapted

            status.connected = True
            status.tool_count = len(listed.tools)
            self.statuses.append(status)

        return self.statuses

    async def _open_client(self, config: McpServerConfig):
        from mcp import Client, StdioServerParameters
        from mcp.client.stdio import get_default_environment, stdio_client

        assert self._stack is not None

        if config.url:
            if config.headers:
                from mcp.client.streamable_http import streamable_http_client
                from mcp.shared._httpx_utils import create_mcp_http_client

                http_client = await self._stack.enter_async_context(
                    create_mcp_http_client(headers=config.headers)
                )
                client = Client(
                    streamable_http_client(config.url, http_client=http_client)
                )
            else:
                client = Client(config.url)
        else:
            params = StdioServerParameters(
                command=config.command or "",
                args=list(config.args),
                # Merge over the SDK's minimal safe environment so PATH/HOME survive.
                env={**get_default_environment(), **config.env},
                cwd=config.cwd,
            )
            client = Client(stdio_client(params))

        return await self._stack.enter_async_context(client)

    async def aclose(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except Exception:
                # Servers that die during teardown shouldn't mask the real result.
                pass
            self._stack = None
        self._clients.clear()
        self._tools.clear()

    # ── tool surface ─────────────────────────────────────────────────────────

    def tool_definitions(self) -> list[dict[str, Any]]:
        return [tool.definition for tool in self._tools.values()]

    def tool_names(self) -> list[str]:
        return list(self._tools)

    def connected_servers(self) -> list[str]:
        """Names of servers that connected and contributed at least one tool."""
        return sorted({tool.server for tool in self._tools.values()})

    def has_tool(self, name: str) -> bool:
        return name in self._tools

    def describe_tool(self, name: str) -> Optional[McpTool]:
        return self._tools.get(name)

    async def call(
        self,
        name: str,
        arguments: dict[str, Any],
        timeout: float = DEFAULT_CALL_TIMEOUT,
    ) -> str:
        """Invoke a namespaced MCP tool and return its result as text."""
        tool = self._tools.get(name)
        if tool is None:
            return f"Error: MCP tool '{name}' is not available."

        client = self._clients.get(tool.server)
        if client is None:
            return f"Error: MCP server '{tool.server}' is not connected."

        try:
            result = await asyncio.wait_for(
                client.call_tool(tool.remote_name, arguments), timeout=timeout
            )
        except asyncio.TimeoutError:
            return (
                f"Error: MCP tool '{name}' timed out after {timeout:g}s."
            )
        except Exception as exc:  # noqa: BLE001 - surface to the model, don't crash
            return f"Error calling MCP tool '{name}': {describe_exception(exc)}"

        return _result_to_text(result)
