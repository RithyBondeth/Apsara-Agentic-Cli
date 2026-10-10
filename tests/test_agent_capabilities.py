"""Coverage for verification, isolation, hooks, critic, LSP, and real-repo evals."""

import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from apsara_cli.config import trust
from apsara_cli.engine import critic, evals, lsp
from apsara_cli.engine.hooks import run_hooks
from apsara_cli.engine.isolation import isolated_workspace
from apsara_cli.engine.tools import agent_runtime_context, verify_project
from apsara_cli.engine.verification import (
    detect_verification_commands,
    run_verification,
)


@pytest.fixture(autouse=True)
def isolated_trust_store(tmp_path, monkeypatch):
    monkeypatch.setattr(trust, "TRUST_PATH", tmp_path / "trust.json")


def _write_verification_config(workspace: Path, code: str) -> None:
    config = workspace / ".apsara" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    command = json.dumps([sys.executable, "-c", code])
    config.write_text(f"[verification]\ncommands = [{command}]\n", encoding="utf-8")


def _git(workspace: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(workspace), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def test_configured_verification_returns_structured_pass(tmp_path):
    _write_verification_config(tmp_path, "print('green')")

    commands = detect_verification_commands(tmp_path)
    report = run_verification(tmp_path)

    assert len(commands) == 1
    assert report.status == "passed"
    assert report.results[0].returncode == 0
    assert "green" in report.results[0].output


def test_verification_failure_is_not_reported_as_success(tmp_path):
    _write_verification_config(tmp_path, "raise SystemExit(3)")

    report = run_verification(tmp_path)

    assert report.status == "failed"
    assert report.results[0].returncode == 3


def test_plain_pyproject_does_not_imply_pytest(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'library-without-pytest'\nversion = '1.0.0'\n",
        encoding="utf-8",
    )

    assert detect_verification_commands(tmp_path) == []


def test_verify_project_requires_trust_and_then_runs(tmp_path):
    _write_verification_config(tmp_path, "print('ok')")
    with agent_runtime_context(workspace_root=tmp_path):
        assert "not approved" in verify_project()
    with agent_runtime_context(
        workspace_root=tmp_path,
        trust_callback=lambda _action, _payload: True,
    ):
        result = verify_project()
    assert result.startswith("Verification passed")
    assert '"status": "passed"' in result


@pytest.mark.skipif(not shutil.which("git"), reason="git unavailable")
def test_isolated_workspace_contains_current_diff_without_mutating_source(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    _git(workspace, "init", "-q")
    _git(workspace, "config", "user.email", "test@example.com")
    _git(workspace, "config", "user.name", "Test")
    tracked = workspace / "value.txt"
    tracked.write_text("old", encoding="utf-8")
    _git(workspace, "add", "value.txt")
    _git(workspace, "commit", "-qm", "base")
    tracked.write_text("new", encoding="utf-8")

    with isolated_workspace(workspace) as snapshot:
        assert (snapshot / "value.txt").read_text(encoding="utf-8") == "new"
        (snapshot / "generated.txt").write_text("isolated", encoding="utf-8")

    assert not (workspace / "generated.txt").exists()
    assert tracked.read_text(encoding="utf-8") == "new"


@pytest.mark.skipif(not shutil.which("git"), reason="git unavailable")
def test_isolated_workspace_rejects_external_symlink(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    _git(workspace, "init", "-q")
    _git(workspace, "config", "user.email", "test@example.com")
    _git(workspace, "config", "user.name", "Test")
    (workspace / "value.txt").write_text("safe", encoding="utf-8")
    (workspace / "escape").symlink_to(tmp_path / "outside")
    _git(workspace, "add", "value.txt", "escape")
    _git(workspace, "commit", "-qm", "base")

    with pytest.raises(RuntimeError, match="external symlink"):
        with isolated_workspace(workspace):
            pass


def test_hook_can_deny_a_tool_call(tmp_path):
    hooks = tmp_path / ".apsara" / "hooks.json"
    hooks.parent.mkdir(parents=True)
    hooks.write_text(json.dumps({
        "before_tool": [{
            "command": [
                sys.executable,
                "-c",
                "import json; print(json.dumps({'decision':'deny','reason':'policy'}))",
            ]
        }]
    }), encoding="utf-8")

    with agent_runtime_context(
        workspace_root=tmp_path,
        trust_callback=lambda _action, _payload: True,
    ):
        outcome = run_hooks("before_tool", {"tool": "write_to_file"}, tmp_path)

    assert outcome.allowed is False
    assert outcome.reason == "policy"


def test_invalid_hook_configuration_fails_closed(tmp_path):
    hooks = tmp_path / ".apsara" / "hooks.json"
    hooks.parent.mkdir(parents=True)
    hooks.write_text("{not-json", encoding="utf-8")

    outcome = run_hooks("session_start", {}, tmp_path)

    assert outcome.allowed is False
    assert "Invalid .apsara/hooks.json" in outcome.reason


def test_critic_call_has_no_tools_and_returns_review(tmp_path):
    mocked = AsyncMock(return_value=({"content": "APPROVED"}, {"total_tokens": 5}))
    with patch("apsara_cli.engine.llm.call_llm", mocked), \
         patch.object(critic, "_read_only_context", return_value="DIFF"):
        content, usage = asyncio.run(critic.request_critique(
            tmp_path, objective="fix it", focus="tests", model="test/model"
        ))

    assert content == "APPROVED"
    assert usage["total_tokens"] == 5
    assert mocked.await_args.kwargs["with_tools"] is False
    assert mocked.await_args.kwargs["max_completion_tokens"] == critic.CRITIC_MAX_COMPLETION_TOKENS


@pytest.mark.skipif(not shutil.which("git"), reason="git unavailable")
def test_critic_context_includes_changed_untracked_file(tmp_path):
    _git(tmp_path, "init", "-q")
    source = tmp_path / "new_module.py"
    source.write_text("VALUE = 42\n", encoding="utf-8")

    context = critic._read_only_context(tmp_path, ["new_module.py"])

    assert "NEW FILE new_module.py" in context
    assert "VALUE = 42" in context


def test_lsp_capability_is_optional(monkeypatch, tmp_path):
    source = tmp_path / "app.py"
    source.write_text("value = 1\n", encoding="utf-8")
    monkeypatch.setattr(lsp.shutil, "which", lambda _name: None)

    assert lsp.server_for_path(source) is None
    with pytest.raises(RuntimeError, match="No installed language server"):
        lsp.query_locations(tmp_path, source, line=1, column=1, operation="definition")


def test_lsp_locations_decode_file_uris_once_and_filter_outside_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "literal%20name.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text("VALUE = 2\n", encoding="utf-8")
    result = [
        {"uri": source.as_uri(), "range": {"start": {"line": 2, "character": 4}}},
        {"uri": outside.as_uri(), "range": {"start": {"line": 0, "character": 0}}},
    ]

    assert lsp._location_lines(result, workspace.resolve()) == [
        "literal%20name.py:3:5 (lsp)"
    ]


@pytest.mark.skipif(not shutil.which("git"), reason="git unavailable")
def test_real_repository_benchmark_materializes_pinned_commit_and_verifier(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", "module.py")
    _git(repo, "commit", "-qm", "base")
    commit = _git(repo, "rev-parse", "HEAD")
    suite = tmp_path / "suite.json"
    suite.write_text("{}", encoding="utf-8")
    target = tmp_path / "trial"

    evals._materialize_benchmark_case(
        {"repository": {"path": "repo", "ref": commit}}, suite, target
    )
    evals._prepare_benchmark_agent_workspace(
        target, [[sys.executable, "-m", "unittest", "-q"]]
    )

    assert (target / "module.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert _git(target, "rev-parse", "HEAD") == commit
    assert _git(target, "status", "--short") == ""
    config = (target / ".apsara" / "config.toml").read_text(encoding="utf-8")
    assert "unittest" in config


@pytest.mark.skipif(not shutil.which("git"), reason="git unavailable")
def test_synthetic_benchmark_gets_isolated_git_and_verification_config(tmp_path):
    workspace = tmp_path / "fixture"
    workspace.mkdir()
    (workspace / "app.py").write_text("VALUE = 1\n", encoding="utf-8")

    evals._prepare_benchmark_agent_workspace(
        workspace, [[sys.executable, "-m", "unittest", "-q"]]
    )

    assert _git(workspace, "status", "--short") == ""
    assert _git(workspace, "log", "-1", "--format=%s") == "benchmark baseline"
    exclude = (workspace / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert "target/" in exclude
    assert "__pycache__/" in exclude
    config = (workspace / ".apsara" / "config.toml").read_text(encoding="utf-8")
    assert "[verification]" in config
    assert "unittest" in config
