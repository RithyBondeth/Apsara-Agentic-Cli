"""Tests for the five new agent tools."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from pathlib import Path
from apsara_cli.engine.tools import (
    agent_runtime_context,
    read_file_lines,
    create_directory,
    delete_file,
    move_file,
    glob_search,
    undo_last_checkpoint,
    undo_turn_checkpoint,
)


def _ctx(tmp: Path, confirm: bool = True):
    return agent_runtime_context(
        workspace_root=tmp,
        confirmation_callback=lambda action, payload: confirm,
    )


# ── read_file_lines ───────────────────────────────────────────────────────────

def test_read_file_lines_basic(tmp_path):
    (tmp_path / "f.py").write_text("line1\nline2\nline3\nline4\nline5\n")
    with _ctx(tmp_path):
        result = read_file_lines("f.py", 2, 4)
    assert "line2" in result
    assert "line3" in result
    assert "line4" in result
    assert "line1" not in result
    assert "line5" not in result


def test_read_file_lines_includes_numbers(tmp_path):
    (tmp_path / "f.py").write_text("a\nb\nc\n")
    with _ctx(tmp_path):
        result = read_file_lines("f.py", 2, 2)
    assert "2:" in result
    assert "b" in result


def test_read_file_lines_clamps_to_eof(tmp_path):
    (tmp_path / "f.py").write_text("a\nb\nc\n")
    with _ctx(tmp_path):
        result = read_file_lines("f.py", 2, 999)
    assert "b" in result
    assert "c" in result
    assert "Error" not in result


def test_read_file_lines_bad_start(tmp_path):
    (tmp_path / "f.py").write_text("a\n")
    with _ctx(tmp_path):
        result = read_file_lines("f.py", 0, 1)
    assert "Error" in result


def test_read_file_lines_start_beyond_file(tmp_path):
    (tmp_path / "f.py").write_text("a\nb\n")
    with _ctx(tmp_path):
        result = read_file_lines("f.py", 10, 20)
    assert "Error" in result


def test_read_file_lines_outside_workspace(tmp_path):
    with _ctx(tmp_path):
        result = read_file_lines("/etc/passwd", 1, 5)
    assert "Error" in result


# ── create_directory ──────────────────────────────────────────────────────────

def test_create_directory_simple(tmp_path):
    with _ctx(tmp_path):
        result = create_directory("newdir")
    assert "Created" in result
    assert (tmp_path / "newdir").is_dir()


def test_create_directory_nested(tmp_path):
    with _ctx(tmp_path):
        result = create_directory("a/b/c")
    assert "Created" in result
    assert (tmp_path / "a" / "b" / "c").is_dir()


def test_create_directory_rejected(tmp_path):
    with _ctx(tmp_path, confirm=False):
        result = create_directory("blocked")
    assert "not approved" in result
    assert not (tmp_path / "blocked").exists()


def test_create_directory_already_exists(tmp_path):
    (tmp_path / "existing").mkdir()
    with _ctx(tmp_path):
        result = create_directory("existing")
    assert "already exists" in result
    assert (tmp_path / "existing").is_dir()


def test_create_directory_outside_workspace(tmp_path):
    with _ctx(tmp_path):
        result = create_directory("/tmp/evil")
    assert "Error" in result


def test_agent_checkpoint_restore_requires_approval(tmp_path, monkeypatch):
    from apsara_cli.engine import checkpoints

    called = False

    def restore(*_args, **_kwargs):
        nonlocal called
        called = True
        return {"id": "x", "label": "x", "restored": [], "removed": []}

    monkeypatch.setattr(checkpoints, "restore_checkpoint", restore)
    with _ctx(tmp_path, confirm=False):
        result = undo_last_checkpoint()

    assert "not approved" in result
    assert called is False


def test_agent_turn_rollback_requires_approval(tmp_path, monkeypatch):
    from apsara_cli.engine import turn_checkpoints

    called = False

    def restore(*_args, **_kwargs):
        nonlocal called
        called = True
        return {"id": "x", "rollback": {}}

    monkeypatch.setattr(turn_checkpoints, "restore_turn_checkpoint", restore)
    with _ctx(tmp_path, confirm=False):
        result = undo_turn_checkpoint()

    assert "not approved" in result
    assert called is False


# ── delete_file ───────────────────────────────────────────────────────────────

def test_delete_file_approved(tmp_path):
    (tmp_path / "bye.txt").write_text("gone")
    with _ctx(tmp_path, confirm=True):
        result = delete_file("bye.txt")
    assert "Deleted" in result
    assert not (tmp_path / "bye.txt").exists()


def test_delete_file_rejected(tmp_path):
    (tmp_path / "keep.txt").write_text("keep me")
    with _ctx(tmp_path, confirm=False):
        result = delete_file("keep.txt")
    assert "Error" in result
    assert (tmp_path / "keep.txt").exists()


def test_delete_file_missing(tmp_path):
    with _ctx(tmp_path):
        result = delete_file("ghost.txt")
    assert "Error" in result


def test_delete_file_outside_workspace(tmp_path):
    with _ctx(tmp_path):
        result = delete_file("/etc/passwd")
    assert "Error" in result


def test_delete_file_directory_rejected(tmp_path):
    (tmp_path / "adir").mkdir()
    with _ctx(tmp_path):
        result = delete_file("adir")
    assert "Error" in result


# ── move_file ────────────────────────────────────────────────────────────────

def test_move_file_rename(tmp_path):
    (tmp_path / "old.txt").write_text("hello")
    with _ctx(tmp_path, confirm=True):
        result = move_file("old.txt", "new.txt")
    assert "Moved" in result
    assert not (tmp_path / "old.txt").exists()
    assert (tmp_path / "new.txt").read_text() == "hello"


def test_move_file_into_directory(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "file.txt").write_text("data")
    with _ctx(tmp_path, confirm=True):
        result = move_file("file.txt", "src")
    assert "Moved" in result
    assert (tmp_path / "src" / "file.txt").exists()


def test_move_file_creates_dest_parents(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    with _ctx(tmp_path, confirm=True):
        result = move_file("a.txt", "deep/nested/b.txt")
    assert "Moved" in result
    assert (tmp_path / "deep" / "nested" / "b.txt").exists()


def test_move_file_rejected(tmp_path):
    (tmp_path / "orig.txt").write_text("stay")
    with _ctx(tmp_path, confirm=False):
        result = move_file("orig.txt", "moved.txt")
    assert "Error" in result
    assert (tmp_path / "orig.txt").exists()


def test_move_file_outside_workspace(tmp_path):
    (tmp_path / "f.txt").write_text("x")
    with _ctx(tmp_path):
        result = move_file("f.txt", "/tmp/evil.txt")
    assert "Error" in result


# ── glob_search ───────────────────────────────────────────────────────────────

def test_glob_search_py_files(tmp_path):
    (tmp_path / "a.py").write_text("")
    (tmp_path / "b.py").write_text("")
    (tmp_path / "c.txt").write_text("")
    with _ctx(tmp_path):
        result = glob_search("*.py")
    assert "a.py" in result
    assert "b.py" in result
    assert "c.txt" not in result


def test_glob_search_recursive(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("")
    (tmp_path / "src" / "util.py").write_text("")
    (tmp_path / "README.md").write_text("")
    with _ctx(tmp_path):
        result = glob_search("**/*.py")
    assert "main.py" in result
    assert "util.py" in result
    assert "README.md" not in result


def test_glob_search_no_match(tmp_path):
    (tmp_path / "hello.py").write_text("")
    with _ctx(tmp_path):
        result = glob_search("*.rs")
    assert "No matches" in result


def test_glob_search_outside_workspace(tmp_path):
    with _ctx(tmp_path):
        result = glob_search("/etc/**/*.conf")
    assert "No matches" in result or "Error" in result
