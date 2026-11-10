"""Tests for the workspace-code trust gate.

The threat these cover: cloning a repository that ships .apsara/tools/*.py must
not be enough to execute that code.
"""
import sys
import os
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from apsara_cli.config import trust
from apsara_cli.engine import tools as tools_mod
from apsara_cli.engine.tools import agent_runtime_context, get_tool_registry


PLUGIN_SOURCE = '''
METADATA = {
    "name": "shout",
    "description": "Uppercase text.",
    "parameters": {"type": "object", "properties": {}},
}

def run(**kwargs):
    return "SHOUTED"
'''


@pytest.fixture(autouse=True)
def isolated_trust_store(tmp_path, monkeypatch):
    """Never touch the developer's real ~/.apsara/trust.json."""
    store = tmp_path / "trust-store.json"
    monkeypatch.setattr(trust, "TRUST_PATH", store)
    tools_mod._plugin_cache.clear()
    yield store
    tools_mod._plugin_cache.clear()


def _workspace_with_plugin(tmp_path: Path, source: str = PLUGIN_SOURCE) -> Path:
    workspace = tmp_path / "project"
    tools_dir = workspace / ".apsara" / "tools"
    tools_dir.mkdir(parents=True)
    (tools_dir / "shout.py").write_text(source)
    return workspace


# ── the gate ──────────────────────────────────────────────────────────────────

def test_plugin_not_loaded_without_trust_callback(tmp_path, capsys):
    """No one to ask means no execution — the clone-and-run case."""
    workspace = _workspace_with_plugin(tmp_path)
    with agent_runtime_context(workspace_root=workspace):
        registry = get_tool_registry()
    assert "shout" not in registry
    assert "Skipped untrusted local plugin" in capsys.readouterr().out


def test_plugin_not_loaded_when_user_declines(tmp_path):
    workspace = _workspace_with_plugin(tmp_path)
    with agent_runtime_context(
        workspace_root=workspace,
        trust_callback=lambda action, payload: False,
    ):
        registry = get_tool_registry()
    assert "shout" not in registry


def test_plugin_loads_when_user_approves(tmp_path):
    workspace = _workspace_with_plugin(tmp_path)
    with agent_runtime_context(
        workspace_root=workspace,
        trust_callback=lambda action, payload: True,
    ):
        registry = get_tool_registry()
    assert "shout" in registry
    assert registry["shout"]() == "SHOUTED"


def test_plugin_manifest_can_disable_and_describe_tool(tmp_path):
    workspace = _workspace_with_plugin(tmp_path)
    manifest = workspace / ".apsara" / "tools" / "shout.json"
    manifest.write_text('{"enabled": false, "permissions": []}', encoding="utf-8")
    with agent_runtime_context(workspace_root=workspace, trust_callback=lambda action, payload: True):
        assert "shout" not in get_tool_registry()

    manifest.write_text(
        '{"enabled": true, "description": "Manifest description", "permissions": ["read"]}',
        encoding="utf-8",
    )
    tools_mod._plugin_cache.clear()
    with agent_runtime_context(workspace_root=workspace, trust_callback=lambda action, payload: True):
        registry = get_tool_registry()
        definitions = tools_mod.get_agent_tools()
    assert "shout" in registry
    shout = next(item for item in definitions if item["function"]["name"] == "shout")
    assert shout["function"]["description"] == "Manifest description"


def test_auto_approve_does_not_imply_trust(tmp_path):
    """--auto-approve waives write confirmation, not code execution."""
    workspace = _workspace_with_plugin(tmp_path)
    with agent_runtime_context(
        workspace_root=workspace,
        confirmation_callback=None,
    ):
        registry = get_tool_registry()
    assert "shout" not in registry


def test_console_ui_auto_approve_does_not_approve_trust(monkeypatch):
    """--auto-approve sets approve_all; that must not cover code execution."""
    from apsara_cli.shared.ui import ConsoleUI

    ui = ConsoleUI(use_color=False, auto_approve=True)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    assert ui.confirm_action("write_to_file", {"path": "a.txt"}) is True
    assert ui.confirm_action(
        "trust_workspace_code", {"kind": "plugin", "display_path": ".apsara/tools/x.py"}
    ) is False


def test_console_ui_auto_approve_does_not_approve_commands_or_external_mutations(monkeypatch):
    from apsara_cli.shared.ui import ConsoleUI

    ui = ConsoleUI(use_color=False, auto_approve=True)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    assert ui.confirm_action("run_bash_command", {"command": "pytest"}) is False
    assert ui.confirm_action("start_process", {"command": "npm test"}) is False
    assert ui.confirm_action("mcp_tool_call", {"tool": "mcp__x__create"}) is False
    assert ui.confirm_action("future_external_action", {}) is False
    assert ui.confirm_action("write_to_file", {"path": "a.txt"}) is True


def test_explicit_command_approval_cannot_enable_always_allow(monkeypatch):
    from apsara_cli.shared.ui import ConsoleUI

    ui = ConsoleUI(use_color=False, auto_approve=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    seen = {}

    def choose(**options):
        seen.update(options)
        return "approve"

    monkeypatch.setattr(ui, "prompt_confirmation_choice", choose)

    assert ui.confirm_action("run_bash_command", {"command": "pytest"}) is True
    assert seen["allow_always"] is False
    assert ui.approve_all is False


def test_approving_a_plugin_does_not_silence_write_prompts(monkeypatch):
    from apsara_cli.shared.ui import ConsoleUI

    ui = ConsoleUI(use_color=False, auto_approve=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(ui, "prompt_confirmation_choice", lambda **kw: "always")

    assert ui.confirm_action(
        "trust_workspace_code", {"kind": "plugin", "display_path": "x.py"}
    ) is True
    assert ui.approve_all is False, "trust approval must not enable approve-all"


def test_trust_prompt_describes_the_plugin(tmp_path):
    workspace = _workspace_with_plugin(tmp_path)
    seen = {}

    def capture(action, payload):
        seen["action"] = action
        seen["payload"] = payload
        return True

    with agent_runtime_context(workspace_root=workspace, trust_callback=capture):
        get_tool_registry()

    assert seen["action"] == "trust_workspace_code"
    payload = seen["payload"]
    assert payload["kind"] == "plugin"
    assert payload["display_path"] == str(Path(".apsara/tools/shout.py"))
    assert "METADATA" in payload["source_preview"]
    assert payload["digest"]


# ── persistence ───────────────────────────────────────────────────────────────

def test_approval_is_remembered_across_runs(tmp_path):
    workspace = _workspace_with_plugin(tmp_path)
    calls = []

    def counting_callback(action, payload):
        calls.append(payload["key"])
        return True

    with agent_runtime_context(workspace_root=workspace, trust_callback=counting_callback):
        get_tool_registry()
    assert len(calls) == 1

    # Fresh process would start with an empty cache; the store must carry it.
    tools_mod._plugin_cache.clear()
    with agent_runtime_context(workspace_root=workspace, trust_callback=counting_callback):
        registry = get_tool_registry()
    assert len(calls) == 1, "already-approved plugin must not re-prompt"
    assert "shout" in registry


def test_modified_plugin_is_re_prompted(tmp_path):
    """An approved file that changes must not stay approved."""
    workspace = _workspace_with_plugin(tmp_path)
    calls = []

    def counting_callback(action, payload):
        calls.append(payload["digest"])
        return True

    with agent_runtime_context(workspace_root=workspace, trust_callback=counting_callback):
        get_tool_registry()
    assert len(calls) == 1

    plugin = workspace / ".apsara" / "tools" / "shout.py"
    plugin.write_text(PLUGIN_SOURCE.replace("SHOUTED", "TAMPERED"))
    tools_mod._plugin_cache.clear()

    with agent_runtime_context(workspace_root=workspace, trust_callback=counting_callback):
        registry = get_tool_registry()
    assert len(calls) == 2, "changed content must re-prompt"
    assert calls[0] != calls[1]
    assert registry["shout"]() == "TAMPERED"


def test_trust_is_scoped_per_workspace(tmp_path):
    first = _workspace_with_plugin(tmp_path / "a")
    second = _workspace_with_plugin(tmp_path / "b")
    calls = []

    def counting_callback(action, payload):
        calls.append(payload["key"])
        return True

    for workspace in (first, second):
        tools_mod._plugin_cache.clear()
        with agent_runtime_context(workspace_root=workspace, trust_callback=counting_callback):
            get_tool_registry()

    assert len(calls) == 2, "approving one project must not approve another"


# ── caching ───────────────────────────────────────────────────────────────────

def test_unchanged_plugins_are_not_re_executed(tmp_path):
    """get_tool_registry runs on every tool call; it must not re-exec each time."""
    workspace = _workspace_with_plugin(
        tmp_path,
        PLUGIN_SOURCE + "\nimport builtins\n"
        "builtins._apsara_plugin_exec_count = "
        "getattr(builtins, '_apsara_plugin_exec_count', 0) + 1\n",
    )
    import builtins
    builtins._apsara_plugin_exec_count = 0

    with agent_runtime_context(
        workspace_root=workspace,
        trust_callback=lambda action, payload: True,
    ):
        for _ in range(5):
            get_tool_registry()

    assert builtins._apsara_plugin_exec_count == 1
    del builtins._apsara_plugin_exec_count


# ── store primitives ──────────────────────────────────────────────────────────

def test_store_round_trip(tmp_path, isolated_trust_store):
    workspace = tmp_path / "proj"
    digest = trust.digest_text("some code")
    assert not trust.is_trusted(workspace, "plugin:a.py", digest)
    trust.record_trust(workspace, "plugin:a.py", digest)
    assert trust.is_trusted(workspace, "plugin:a.py", digest)
    assert not trust.is_trusted(workspace, "plugin:a.py", trust.digest_text("other"))


def test_forget_workspace_clears_approvals(tmp_path):
    workspace = tmp_path / "proj"
    digest = trust.digest_text("code")
    trust.record_trust(workspace, "plugin:a.py", digest)
    trust.forget_workspace(workspace)
    assert not trust.is_trusted(workspace, "plugin:a.py", digest)


def test_corrupt_store_is_not_fatal(tmp_path, isolated_trust_store):
    isolated_trust_store.parent.mkdir(parents=True, exist_ok=True)
    isolated_trust_store.write_text("{not json")
    assert trust.load_trust() == {"workspaces": {}}
    assert not trust.is_trusted(tmp_path, "plugin:a.py", "abc")


def test_store_is_owner_only(tmp_path, isolated_trust_store):
    trust.record_trust(tmp_path, "plugin:a.py", trust.digest_text("x"))
    assert isolated_trust_store.stat().st_mode & 0o077 == 0


# ── apsara trust command ──────────────────────────────────────────────────────

class _Defaults:
    workspace = None


class _Config:
    defaults = _Defaults()
    theme = None
    path = "/tmp/none"
    exists = False


class _Args:
    def __init__(self, workspace=None, reset=False):
        self.workspace = workspace
        self.reset = reset
        self.color = False


def test_trust_command_lists_nothing_when_empty(tmp_path, capsys):
    from apsara_cli.cli.trust_cli import trust_command

    rc = trust_command(_Args(workspace=str(tmp_path)), _Config())
    out = capsys.readouterr().out
    assert rc == 0
    assert "No workspace code has been approved" in out


def test_trust_command_lists_approvals(tmp_path, capsys):
    from apsara_cli.cli.trust_cli import trust_command

    trust.record_trust(tmp_path, "plugin:.apsara/tools/lint.py", trust.digest_text("a"))
    trust.record_trust(tmp_path, "mcp:github", trust.digest_text("b"))

    rc = trust_command(_Args(workspace=str(tmp_path)), _Config())
    out = capsys.readouterr().out
    assert rc == 0
    assert ".apsara/tools/lint.py" in out
    assert "github" in out
    assert "plugin" in out and "mcp" in out


def test_trust_reset_revokes_everything(tmp_path, capsys):
    from apsara_cli.cli.trust_cli import trust_command

    digest = trust.digest_text("a")
    trust.record_trust(tmp_path, "plugin:x.py", digest)
    assert trust.is_trusted(tmp_path, "plugin:x.py", digest)

    rc = trust_command(_Args(workspace=str(tmp_path), reset=True), _Config())
    out = capsys.readouterr().out
    assert rc == 0
    assert "Revoked 1 approval" in out
    assert not trust.is_trusted(tmp_path, "plugin:x.py", digest)


def test_trust_reset_on_empty_workspace_is_not_an_error(tmp_path, capsys):
    from apsara_cli.cli.trust_cli import trust_command

    rc = trust_command(_Args(workspace=str(tmp_path), reset=True), _Config())
    assert rc == 0
    assert "No approvals recorded" in capsys.readouterr().out


def test_trust_is_scoped_to_the_named_workspace(tmp_path, capsys):
    from apsara_cli.cli.trust_cli import trust_command

    other = tmp_path / "other"
    other.mkdir()
    digest = trust.digest_text("a")
    trust.record_trust(tmp_path, "plugin:mine.py", digest)
    trust.record_trust(other, "plugin:theirs.py", digest)

    trust_command(_Args(workspace=str(tmp_path), reset=True), _Config())
    assert not trust.is_trusted(tmp_path, "plugin:mine.py", digest)
    assert trust.is_trusted(other, "plugin:theirs.py", digest), (
        "resetting one workspace must not touch another"
    )
