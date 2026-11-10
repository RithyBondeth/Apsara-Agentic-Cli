"""Tests for workspace-scoped tool execution."""
import sys
import os
import tempfile
import pytest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from apsara_cli.engine.tools import (
    agent_runtime_context,
    read_file,
    write_to_file,
    run_bash_command,
    search_files,
    replace_file_lines,
    _extract_command_names,
)


def _ctx(tmp: Path, bash: bool = False, allowed=None):
    return agent_runtime_context(
        workspace_root=tmp,
        enable_bash=bash,
        allowed_commands=allowed or {"ls", "cat", "find", "grep", "pwd", "wc", "head", "tail", "sed"},
        confirmation_callback=lambda action, payload: True,
    )


# ── read_file ─────────────────────────────────────────────────────────────────

def test_read_file_ok(tmp_path):
    (tmp_path / "hello.txt").write_text("hello world")
    with _ctx(tmp_path):
        result = read_file("hello.txt")
    assert result == "hello world"


def test_read_file_missing(tmp_path):
    with _ctx(tmp_path):
        result = read_file("nope.txt")
    assert result.startswith("Error")


def test_read_file_outside_workspace(tmp_path):
    with _ctx(tmp_path):
        result = read_file("/etc/passwd")
    assert "Error" in result


def test_read_file_size_limit(tmp_path):
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * 10)
    with agent_runtime_context(
        workspace_root=tmp_path,
        max_file_size_bytes=5,
        confirmation_callback=lambda a, p: True,
    ):
        result = read_file("big.bin")
    assert "Error" in result


# ── write_to_file ─────────────────────────────────────────────────────────────

def test_write_file_creates(tmp_path):
    with _ctx(tmp_path):
        result = write_to_file("new.txt", "content")
    assert "Successfully" in result
    assert (tmp_path / "new.txt").read_text() == "content"


def test_write_file_overwrites(tmp_path):
    (tmp_path / "f.txt").write_text("old")
    with _ctx(tmp_path):
        write_to_file("f.txt", "new")
    assert (tmp_path / "f.txt").read_text() == "new"


def test_write_file_rejected(tmp_path):
    with agent_runtime_context(
        workspace_root=tmp_path,
        confirmation_callback=lambda action, payload: False,
    ):
        result = write_to_file("blocked.txt", "data")
    assert "Error" in result
    assert not (tmp_path / "blocked.txt").exists()


def test_write_file_outside_workspace(tmp_path):
    with _ctx(tmp_path):
        result = write_to_file("/tmp/evil.txt", "bad")
    assert "Error" in result


# ── replace_file_lines ────────────────────────────────────────────────────────

def test_replace_file_lines_ok(tmp_path):
    (tmp_path / "code.py").write_text("line1\nline2\nline3\n")
    with _ctx(tmp_path):
        result = replace_file_lines("code.py", 2, 2, "replaced\n")
    assert "Successfully" in result
    assert (tmp_path / "code.py").read_text() == "line1\nreplaced\nline3\n"


def test_replace_file_lines_out_of_bounds(tmp_path):
    (tmp_path / "f.py").write_text("one\n")
    with _ctx(tmp_path):
        result = replace_file_lines("f.py", 5, 5, "x\n")
    assert "Error" in result


# ── run_bash_command ──────────────────────────────────────────────────────────

def test_bash_disabled(tmp_path):
    with _ctx(tmp_path, bash=False):
        result = run_bash_command("ls")
    assert "disabled" in result


def test_bash_simple_command(tmp_path):
    with _ctx(tmp_path, bash=True):
        result = run_bash_command("pwd")
    assert str(tmp_path) in result
    assert "EXIT CODE: 0" in result


def test_bash_pipe_allowed(tmp_path):
    (tmp_path / "a.txt").write_text("foo\nbar\nfoo\n")
    with _ctx(tmp_path, bash=True):
        result = run_bash_command("cat a.txt | grep foo")
    assert "EXIT CODE: 0" in result
    assert "foo" in result


def test_bash_disallowed_command(tmp_path):
    with _ctx(tmp_path, bash=True, allowed={"ls"}):
        result = run_bash_command("rm -rf .")
    assert "Error" in result
    assert "not allowed" in result


def test_bash_disallowed_pipe_component(tmp_path):
    # cat is allowed, but rm is not — whole command must be rejected
    with _ctx(tmp_path, bash=True, allowed={"cat", "grep"}):
        result = run_bash_command("cat file.txt | rm -rf .")
    assert "Error" in result


def test_bash_blocks_command_substitution(tmp_path):
    with _ctx(tmp_path, bash=True):
        result = run_bash_command("ls $(echo .)")
    assert "Error" in result
    result2 = run_bash_command("ls `pwd`")
    assert "Error" in result2


# ── _extract_command_names ────────────────────────────────────────────────────

def test_extract_simple():
    assert _extract_command_names("ls -la") == ["ls"]


def test_extract_quoted_arguments_and_chains():
    assert _extract_command_names("'/project with spaces/python' -c 'import time; time.sleep(1)'") == ['/project with spaces/python']
    assert _extract_command_names('echo "literal ; | &&" && rm file') == ['echo', 'rm']
    assert _extract_command_names(r'echo literal\;word | grep word') == ['echo', 'grep']
    assert _extract_command_names('python -m pip --version && echo done') == ['python', 'pip', 'echo']


def test_extract_pipe():
    assert _extract_command_names("cat file | grep foo") == ["cat", "grep"]


def test_extract_and():
    assert _extract_command_names("cd /tmp && ls") == ["cd", "ls"]


def test_extract_semicolon():
    assert _extract_command_names("pwd; ls") == ["pwd", "ls"]


def test_extract_or():
    assert _extract_command_names("ls missing || echo nope") == ["ls", "echo"]


# ── search_files ──────────────────────────────────────────────────────────────

def test_search_files_finds_match(tmp_path):
    (tmp_path / "src.py").write_text("def hello():\n    pass\n")
    with _ctx(tmp_path):
        result = search_files("def hello")
    assert "hello" in result


def test_search_files_no_match(tmp_path):
    (tmp_path / "empty.py").write_text("nothing here\n")
    with _ctx(tmp_path):
        result = search_files("zzznotfound")
    assert "No matches" in result or result.strip() == ""


@pytest.mark.parametrize('pattern,expected', [('def take|def chunked', ('def take','def chunked')), (r'take\(', ('take(',))])
def test_search_fallback_preserves_common_regex_behavior(monkeypatch,tmp_path,pattern,expected):
    from apsara_cli.engine import tools
    actual = tools.subprocess.run
    def without_rg(command, **kwargs):
        if command[0]=='rg':
            raise FileNotFoundError('rg not installed')
        return actual(command, **kwargs)
    monkeypatch.setattr(tools.subprocess,'run',without_rg)
    (tmp_path/'app.py').write_text('def take(n): pass\ndef chunked(n): pass\n')
    with agent_runtime_context(workspace_root=tmp_path):
        result=search_files(pattern)
    assert not result.startswith('Error')
    assert all(text in result for text in expected)


def test_search_fallback_does_not_search_agent_state_or_dependencies(monkeypatch,tmp_path):
    from apsara_cli.engine import tools
    actual=tools.subprocess.run
    def without_rg(command,**kwargs):
        if command[0]=='rg':raise FileNotFoundError('rg not installed')
        return actual(command,**kwargs)
    monkeypatch.setattr(tools.subprocess,'run',without_rg)
    for directory in ('.apsara','node_modules','.venv'):
        p=tmp_path/directory
        p.mkdir()
        (p/'private.txt').write_text('needle')
    (tmp_path/'app.py').write_text('needle')
    with agent_runtime_context(workspace_root=tmp_path):
        result=search_files('needle')
    assert 'app.py' in result and 'private.txt' not in result
