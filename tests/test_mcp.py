"""Tests for MCP client support.

The integration tests launch a real MCP server over stdio (an in-process
MCPServer script), so the transport, tool discovery, and call path are all
exercised for real rather than mocked.
"""
import asyncio
import os
import sys
import textwrap
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from apsara_cli.config.cli_config import load_cli_config, parse_mcp_servers
from apsara_cli.engine.mcp_client import (
    McpManager,
    McpServerConfig,
    _result_to_text,
    _tool_to_definition,
    describe_exception,
    is_mcp_tool_name,
    namespaced_tool_name,
    sanitize_name_part,
)
from apsara_cli.engine.tools import (
    execute_tool_async,
    get_agent_tools,
    mcp_manager_context,
)


ECHO_SERVER = textwrap.dedent(
    '''
    from mcp.server.mcpserver import MCPServer

    server = MCPServer("echo")

    @server.tool()
    def shout(text: str) -> str:
        """Uppercase the given text."""
        return text.upper()

    @server.tool()
    def boom() -> str:
        """Always fails."""
        raise RuntimeError("intentional failure")

    if __name__ == "__main__":
        server.run()
    '''
)


def _run(coro):
    return asyncio.run(coro)


# ── name mangling ─────────────────────────────────────────────────────────────

def test_namespaced_name_shape():
    assert namespaced_tool_name("github", "create_issue") == "mcp__github__create_issue"
    assert is_mcp_tool_name("mcp__github__create_issue")
    assert not is_mcp_tool_name("read_file")


def test_name_parts_are_sanitized():
    assert sanitize_name_part("my server!") == "my_server_"
    assert "/" not in namespaced_tool_name("a/b", "c d")


def test_long_names_are_truncated_to_api_limit():
    name = namespaced_tool_name("s" * 40, "t" * 40)
    assert len(name) <= 64


def test_mcp_names_cannot_collide_with_builtins():
    builtin_names = {t["function"]["name"] for t in get_agent_tools()}
    assert namespaced_tool_name("x", "read_file") not in builtin_names


# ── schema conversion ─────────────────────────────────────────────────────────

@dataclass
class _FakeTool:
    name: str
    description: str | None
    input_schema: dict | None


def test_tool_definition_conversion():
    tool = _FakeTool("create_issue", "Open an issue.", {"type": "object", "properties": {"title": {"type": "string"}}})
    adapted = _tool_to_definition("github", tool)
    assert adapted.local_name == "mcp__github__create_issue"
    assert adapted.remote_name == "create_issue"
    fn = adapted.definition["function"]
    assert fn["name"] == "mcp__github__create_issue"
    assert fn["description"].startswith("[github]")
    assert fn["parameters"]["properties"]["title"]["type"] == "string"


def test_tool_definition_tolerates_missing_schema():
    adapted = _tool_to_definition("s", _FakeTool("t", None, None))
    params = adapted.definition["function"]["parameters"]
    assert params["type"] == "object"
    assert params["properties"] == {}
    assert adapted.definition["function"]["description"]


# ── result flattening ─────────────────────────────────────────────────────────

@dataclass
class _Block:
    type: str
    text: str | None = None


@dataclass
class _Result:
    content: list
    structured_content: object = None
    is_error: bool = False


def test_result_joins_text_blocks():
    assert _result_to_text(_Result([_Block("text", "a"), _Block("text", "b")])) == "a\nb"


def test_result_marks_errors():
    out = _result_to_text(_Result([_Block("text", "no such repo")], is_error=True))
    assert out.startswith("Error from MCP tool:")
    assert "no such repo" in out


def test_result_falls_back_to_structured_content():
    out = _result_to_text(_Result([], structured_content={"count": 2}))
    assert "count" in out


def test_result_describes_non_text_blocks():
    assert "[image content omitted]" in _result_to_text(_Result([_Block("image")]))


def test_result_handles_empty():
    assert _result_to_text(_Result([])) == "(tool returned no content)"


def test_result_is_truncated():
    from apsara_cli.engine.mcp_client import MAX_TOOL_RESULT_CHARS

    out = _result_to_text(_Result([_Block("text", "x" * (MAX_TOOL_RESULT_CHARS + 500))]))
    assert "truncated" in out
    assert len(out) < MAX_TOOL_RESULT_CHARS + 200


# ── error rendering ───────────────────────────────────────────────────────────

class _FakeGroup(Exception):
    """Stands in for an ExceptionGroup, which is only a builtin on 3.11+."""

    def __init__(self, message, exceptions):
        super().__init__(message)
        self.exceptions = exceptions


def test_describe_exception_flattens_groups():
    group = _FakeGroup(
        "unhandled errors in a TaskGroup",
        [FileNotFoundError("No such file: server.py")],
    )
    described = describe_exception(group)
    assert "No such file: server.py" in described
    assert "TaskGroup" not in described


def test_describe_exception_flattens_nested_groups():
    group = _FakeGroup("outer", [_FakeGroup("inner", [ValueError("root cause")])])
    assert "root cause" in describe_exception(group)


def test_describe_exception_deduplicates_identical_leaves():
    group = _FakeGroup("outer", [ValueError("same"), ValueError("same")])
    assert describe_exception(group) == "ValueError: same"


@pytest.mark.skipif(sys.version_info < (3, 11), reason="ExceptionGroup is 3.11+")
def test_describe_exception_handles_real_exception_group():
    group = ExceptionGroup("unhandled errors in a TaskGroup", [OSError("boom")])  # noqa: F821
    assert "boom" in describe_exception(group)


def test_describe_exception_plain():
    assert describe_exception(ValueError("bad")) == "ValueError: bad"


# ── config validation ─────────────────────────────────────────────────────────

def test_config_requires_a_transport():
    assert "needs either" in McpServerConfig(name="x").validate()


def test_config_rejects_both_transports():
    problem = McpServerConfig(name="x", command="echo", url="https://e.com").validate()
    assert "one transport" in problem


def test_config_rejects_non_http_url():
    assert "not http" in McpServerConfig(name="x", url="ftp://e.com").validate()


def test_config_reports_missing_command():
    problem = McpServerConfig(name="x", command="definitely-not-real-xyz").validate()
    assert "not found on PATH" in problem


def test_valid_stdio_config_passes():
    assert McpServerConfig(name="x", command=sys.executable, args=["-V"]).validate() is None


def test_trust_digest_changes_when_execution_environment_changes():
    original = McpServerConfig(
        name="x",
        command=sys.executable,
        env={"PYTHONPATH": "safe"},
    )
    changed = McpServerConfig(
        name="x",
        command=sys.executable,
        env={"PYTHONPATH": "attacker-controlled"},
    )

    assert original.trust_digest_source() != changed.trust_digest_source()


def test_trust_digest_changes_when_http_headers_change():
    original = McpServerConfig(
        name="x",
        url="https://mcp.example.com",
        headers={"Authorization": "Bearer original"},
    )
    changed = McpServerConfig(
        name="x",
        url="https://mcp.example.com",
        headers={"Authorization": "Bearer replacement"},
    )

    assert original.trust_digest_source() != changed.trust_digest_source()


# ── TOML parsing ──────────────────────────────────────────────────────────────

def _write_config(tmp_path: Path, body: str) -> Path:
    config_dir = tmp_path / ".apsara"
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_parses_stdio_and_http_servers(tmp_path):
    path = _write_config(
        tmp_path,
        """
        [mcp_servers.files]
        command = "npx"
        args = ["-y", "server-filesystem", "."]

        [mcp_servers.remote]
        url = "https://mcp.example.com/v1"
        headers = { Authorization = "Bearer abc" }
        timeout = 5.0
        """,
    )
    config = load_cli_config(str(path))
    by_name = {s.name: s for s in config.mcp_servers}
    assert by_name["files"].transport == "stdio"
    assert by_name["files"].args == ["-y", "server-filesystem", "."]
    assert by_name["remote"].transport == "http"
    assert by_name["remote"].headers["Authorization"] == "Bearer abc"
    assert by_name["remote"].timeout == 5.0
    assert config.mcp_errors == []


def test_env_vars_are_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("APSARA_TEST_TOKEN", "s3cret")
    path = _write_config(
        tmp_path,
        """
        [mcp_servers.remote]
        url = "https://mcp.example.com"
        headers = { Authorization = "Bearer ${APSARA_TEST_TOKEN}" }
        """,
    )
    config = load_cli_config(str(path))
    assert config.mcp_servers[0].headers["Authorization"] == "Bearer s3cret"


def test_http_connection_uses_configured_headers(monkeypatch):
    import mcp
    from mcp.client import streamable_http
    from mcp.shared import _httpx_utils

    seen = {}

    class FakeHttpClient:
        async def __aenter__(self):
            seen["http_entered"] = True
            return self

        async def __aexit__(self, *_exc_info):
            seen["http_exited"] = True

    class FakeClient:
        def __init__(self, transport):
            seen["client_transport"] = transport

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc_info):
            seen["client_exited"] = True

    def create_http_client(headers=None, **_kwargs):
        seen["headers"] = headers
        return FakeHttpClient()

    def create_transport(url, *, http_client):
        seen["url"] = url
        seen["http_client"] = http_client
        return "authenticated-transport"

    monkeypatch.setattr(mcp, "Client", FakeClient)
    monkeypatch.setattr(_httpx_utils, "create_mcp_http_client", create_http_client)
    monkeypatch.setattr(streamable_http, "streamable_http_client", create_transport)

    async def scenario():
        manager = McpManager([])
        manager._stack = AsyncExitStack()
        await manager._stack.__aenter__()
        client = await manager._open_client(
            McpServerConfig(
                name="remote",
                url="https://mcp.example.com",
                headers={"Authorization": "Bearer secret"},
            )
        )
        await manager.aclose()
        return client

    client = _run(scenario())

    assert isinstance(client, FakeClient)
    assert seen["headers"] == {"Authorization": "Bearer secret"}
    assert seen["url"] == "https://mcp.example.com"
    assert seen["client_transport"] == "authenticated-transport"
    assert seen["http_client"] is not None
    assert seen["http_entered"] is True
    assert seen["client_exited"] is True
    assert seen["http_exited"] is True


def test_disabled_servers_are_kept_but_flagged(tmp_path):
    path = _write_config(
        tmp_path,
        """
        [mcp_servers.off]
        command = "echo"
        enabled = false
        """,
    )
    config = load_cli_config(str(path))
    assert config.mcp_servers[0].enabled is False
    assert McpManager(config.mcp_servers).configs == []


def test_malformed_entry_is_reported_not_raised(tmp_path):
    path = _write_config(
        tmp_path,
        """
        [mcp_servers.good]
        command = "echo"

        [mcp_servers.bad]
        command = "echo"
        timeout = "soon"
        """,
    )
    config = load_cli_config(str(path))
    assert [s.name for s in config.mcp_servers] == ["good"]
    assert any("timeout" in e for e in config.mcp_errors)


def test_missing_section_is_fine(tmp_path):
    path = _write_config(tmp_path, "[defaults]\nmodel = 'gpt-4o'\n")
    config = load_cli_config(str(path))
    assert config.mcp_servers == []
    assert config.mcp_errors == []


def test_non_table_section_is_reported():
    servers, errors = parse_mcp_servers("nope")
    assert servers == []
    assert errors


# ── integration against a real server ─────────────────────────────────────────

@pytest.fixture
def echo_server(tmp_path):
    script = tmp_path / "echo_server.py"
    script.write_text(ECHO_SERVER, encoding="utf-8")
    return McpServerConfig(name="echo", command=sys.executable, args=[str(script)], timeout=30)


def test_connects_and_discovers_tools(echo_server):
    async def scenario():
        async with McpManager([echo_server]) as manager:
            return manager.statuses, manager.tool_names(), manager.connected_servers()

    statuses, names, servers = _run(scenario())
    assert statuses[0].connected
    assert statuses[0].tool_count == 2
    assert "mcp__echo__shout" in names
    assert servers == ["echo"]


def test_calls_a_remote_tool(echo_server):
    async def scenario():
        async with McpManager([echo_server]) as manager:
            return await manager.call("mcp__echo__shout", {"text": "hello"})

    assert _run(scenario()) == "HELLO"


def test_remote_tool_error_is_returned_not_raised(echo_server):
    async def scenario():
        async with McpManager([echo_server]) as manager:
            return await manager.call("mcp__echo__boom", {})

    assert _run(scenario()).startswith("Error")


def test_unknown_tool_is_reported(echo_server):
    async def scenario():
        async with McpManager([echo_server]) as manager:
            return await manager.call("mcp__echo__missing", {})

    assert "not available" in _run(scenario())


def test_one_broken_server_does_not_block_the_others(echo_server, tmp_path):
    broken = McpServerConfig(
        name="broken", command=sys.executable, args=[str(tmp_path / "missing.py")], timeout=20
    )

    async def scenario():
        async with McpManager([broken, echo_server]) as manager:
            return manager.statuses, manager.tool_names()

    statuses, names = _run(scenario())
    by_name = {s.name: s for s in statuses}
    assert not by_name["broken"].connected
    assert by_name["broken"].error
    assert by_name["echo"].connected
    assert "mcp__echo__shout" in names


def test_tools_appear_in_agent_tool_list(echo_server):
    async def scenario():
        async with McpManager([echo_server]) as manager:
            with mcp_manager_context(manager):
                return [t["function"]["name"] for t in get_agent_tools()]

    names = _run(scenario())
    assert "mcp__echo__shout" in names
    assert "read_file" in names, "built-ins must still be present"


def test_execute_tool_async_routes_to_mcp(echo_server):
    async def scenario():
        async with McpManager([echo_server]) as manager:
            with mcp_manager_context(manager):
                remote = await execute_tool_async("mcp__echo__shout", {"text": "hi"})
                unknown = await execute_tool_async("definitely_not_a_tool", {})
                return remote, unknown

    remote, unknown = _run(scenario())
    assert remote == "HI"
    assert "not found" in unknown


def test_no_manager_means_only_builtin_tools():
    names = [t["function"]["name"] for t in get_agent_tools()]
    assert not any(is_mcp_tool_name(n) for n in names)
