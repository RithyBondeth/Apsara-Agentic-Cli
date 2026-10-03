"""Atomic, recoverable checkpoints spanning a complete agent turn."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_current_turn: ContextVar[str | None] = ContextVar("apsara_turn_checkpoint", default=None)


def _root(workspace: Path, turn_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", turn_id) or turn_id in {".", ".."}:
        raise ValueError(f"Unsafe turn checkpoint id: {turn_id!r}")
    return workspace.resolve() / ".apsara" / "turns" / turn_id


def _manifest_path(workspace: Path, turn_id: str) -> Path:
    return _root(workspace, turn_id) / "manifest.json"


def _read_manifest(workspace: Path, turn_id: str) -> dict[str, Any]:
    path = _manifest_path(workspace, turn_id)
    if not path.is_file():
        raise FileNotFoundError(f"No turn checkpoint '{turn_id}' exists")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Turn checkpoint '{turn_id}' is invalid")
    return payload


def _write_manifest(workspace: Path, turn_id: str, payload: dict[str, Any]) -> None:
    path = _manifest_path(workspace, turn_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def _digest(path: Path) -> str | None:
    if path.is_symlink():
        return hashlib.sha256(os.readlink(path).encode("utf-8", errors="replace")).hexdigest()
    if not path.is_file():
        return None
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _target(workspace: Path, relative: str) -> Path:
    relative_path = Path(relative)
    if relative_path.is_absolute() or not relative or ".." in relative_path.parts:
        raise ValueError(f"Unsafe checkpoint path: {relative!r}")
    target = workspace.resolve() / relative_path
    target.parent.resolve().relative_to(workspace.resolve())
    return target


def begin_turn_checkpoint(workspace: Path, turn_id: str, objective: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    _write_manifest(workspace, turn_id, {
        "id": turn_id,
        "objective": objective,
        "status": "active",
        "created_at": now,
        "updated_at": now,
        "files": [],
        "changes": [],
        "coverage": "built-in file tools",
        "workspace_baseline": None,
        "rollback": None,
    })


def activate_turn_checkpoint(turn_id: str) -> Token:
    return _current_turn.set(turn_id)


def deactivate_turn_checkpoint(token: Token) -> None:
    _current_turn.reset(token)


def capture_turn_paths(workspace: Path, paths: list[Path]) -> None:
    """Snapshot each path once, immediately before its first turn mutation."""
    turn_id = _current_turn.get()
    if not turn_id:
        return
    workspace = workspace.resolve()
    manifest = _read_manifest(workspace, turn_id)
    if manifest.get("status") != "active":
        return
    known = {str(item.get("path")) for item in manifest.get("files", [])}
    snapshots = _root(workspace, turn_id) / "files"
    for raw_path in paths:
        resolved = raw_path.resolve()
        try:
            relative = resolved.relative_to(workspace)
        except ValueError:
            continue
        relative_text = str(relative)
        if relative_text in known or relative.parts[:2] == (".apsara", "turns"):
            continue
        kind = "file" if resolved.is_file() else "directory" if resolved.is_dir() else "missing"
        entry = {"path": relative_text, "before": kind, "sha256": _digest(resolved)}
        manifest.setdefault("files", []).append(entry)
        known.add(relative_text)
        if kind == "file":
            destination = snapshots / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(resolved, destination)
    manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
    _write_manifest(workspace, turn_id, manifest)


def capture_turn_workspace(workspace: Path) -> None:
    """Snapshot the workspace once before a command that may mutate arbitrary files."""
    turn_id = _current_turn.get()
    if not turn_id:
        return
    workspace = workspace.resolve()
    manifest = _read_manifest(workspace, turn_id)
    if manifest.get("workspace_baseline") is not None:
        return
    from apsara_cli.engine.workspace_state import ignored_directory
    paths: list[Path] = []
    baseline_files: list[str] = []
    baseline_directories: list[str] = []
    total = 0
    try:
        configured_limit = int(os.environ.get("APSARA_TURN_SNAPSHOT_MAX_MB", "100"))
    except ValueError:
        configured_limit = 100
    limit = max(1, configured_limit) * 1024 * 1024
    partial = False
    for root, directories, files in os.walk(workspace):
        root_path = Path(root)
        directories[:] = [name for name in directories if not ignored_directory(name)]
        for directory in directories:
            if (root_path / directory).is_symlink():
                partial = True
            baseline_directories.append(str((root_path / directory).relative_to(workspace)))
        for filename in files:
            path = root_path / filename
            relative = str(path.relative_to(workspace))
            baseline_files.append(relative)
            if path.is_symlink():
                partial = True
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if total + size <= limit:
                paths.append(path)
                total += size
            else:
                partial = True
    manifest["workspace_baseline"] = {
        "files": baseline_files,
        "directories": baseline_directories,
        "bytes_captured": total,
    }
    manifest["coverage"] = "partial command snapshot" if partial else "complete command snapshot"
    _write_manifest(workspace, turn_id, manifest)
    capture_turn_paths(workspace, paths)


def _capture_command_creations(workspace: Path, manifest: dict[str, Any]) -> None:
    baseline = manifest.get("workspace_baseline")
    if not isinstance(baseline, dict):
        return
    known = {str(item.get("path")) for item in manifest.get("files", [])}
    baseline_files = set(baseline.get("files") or [])
    baseline_directories = set(baseline.get("directories") or [])
    from apsara_cli.engine.workspace_state import ignored_directory
    for root, directories, files in os.walk(workspace):
        root_path = Path(root)
        directories[:] = [name for name in directories if not ignored_directory(name)]
        for filename in files:
            relative = str((root_path / filename).relative_to(workspace))
            if relative not in baseline_files and relative not in known:
                manifest.setdefault("files", []).append({"path": relative, "before": "missing", "sha256": None})
                known.add(relative)
        for directory in directories:
            relative = str((root_path / directory).relative_to(workspace))
            if relative not in baseline_directories and relative not in known:
                manifest.setdefault("files", []).append({"path": relative, "before": "missing", "sha256": None})
                known.add(relative)


def _changes(workspace: Path, manifest: dict[str, Any]) -> list[dict[str, str]]:
    changes: list[dict[str, str]] = []
    for entry in manifest.get("files", []):
        relative = str(entry.get("path") or "")
        try:
            target = _target(workspace, relative)
        except ValueError:
            continue
        before = entry.get("before")
        after = (
            "symlink" if target.is_symlink()
            else "file" if target.is_file()
            else "directory" if target.is_dir()
            else "missing"
        )
        if before == "missing" and after != "missing":
            action = "created"
        elif before != "missing" and after == "missing":
            action = "deleted"
        elif before != after:
            action = "replaced"
        elif before == "file" and entry.get("sha256") != _digest(target):
            action = "modified"
        else:
            continue
        changes.append({"path": relative, "action": action})
    return changes


def finish_turn_checkpoint(workspace: Path, turn_id: str, status: str) -> dict[str, Any]:
    manifest = _read_manifest(workspace, turn_id)
    if manifest.get("status") == "rolled_back":
        return manifest
    _capture_command_creations(workspace.resolve(), manifest)
    manifest["status"] = status
    manifest["changes"] = _changes(workspace.resolve(), manifest)
    from apsara_cli.engine.workspace_state import path_state
    for entry in manifest.get("files", []):
        entry["after"] = path_state(_target(workspace.resolve(), str(entry["path"])))
    manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
    _write_manifest(workspace, turn_id, manifest)
    return manifest


def list_turn_checkpoints(workspace: Path) -> list[dict[str, Any]]:
    base = workspace.resolve() / ".apsara" / "turns"
    if not base.is_dir():
        return []
    results: list[dict[str, Any]] = []
    for path in sorted(base.glob("*/manifest.json"), reverse=True):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                results.append(payload)
        except (OSError, json.JSONDecodeError):
            continue
    return sorted(
        results,
        key=lambda item: str(item.get("created_at") or item.get("updated_at") or ""),
        reverse=True,
    )


def restore_turn_checkpoint(workspace: Path, turn_id: str | None = None, *, force: bool = False) -> dict[str, Any]:
    workspace = workspace.resolve()
    turns = list_turn_checkpoints(workspace)
    manifest = next((
        item for item in turns
        if (item.get("id") == turn_id if turn_id else item.get("status") != "rolled_back")
    ), None)
    if manifest is None:
        raise FileNotFoundError("No matching turn checkpoint exists")
    selected_id = str(manifest["id"])
    # New paths observed only now may belong to the user, not the interrupted turn.
    snapshots = _root(workspace, selected_id) / "files"
    restored: list[str] = []
    removed: list[str] = []
    conflicts: list[str] = []
    entries = list(manifest.get("files", []))
    entries.sort(
        key=lambda item: (
            (
                (workspace / str(item.get("path") or "")).is_dir()
                and not (workspace / str(item.get("path") or "")).is_symlink()
            ),
            -len(Path(str(item.get("path") or "")).parts),
        )
    )
    for entry in entries:
        relative = str(entry.get("path") or "")
        target = _target(workspace, relative)
        before = entry.get("before")
        from apsara_cli.engine.workspace_state import path_state
        current = path_state(target)
        original = {"kind": before, "digest": entry.get("sha256")}
        if current == original:
            continue
        if not force and (entry.get("after") is None or current != entry["after"]):
            conflicts.append(relative)
            continue
        if before == "file":
            if target.is_symlink():
                target.unlink()
            elif target.exists() and not target.is_file():
                conflicts.append(relative)
                continue
            source = snapshots / relative
            try:
                source.resolve().relative_to(snapshots.resolve())
            except ValueError:
                conflicts.append(relative)
                continue
            if not source.is_file() or source.is_symlink():
                conflicts.append(relative)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            restored.append(relative)
        elif before == "missing" and (target.exists() or target.is_symlink()):
            if target.is_file() or target.is_symlink():
                target.unlink()
                removed.append(relative)
            elif target.is_dir():
                try:
                    target.rmdir()
                    removed.append(relative)
                except OSError:
                    conflicts.append(relative)
        elif before == "directory":
            if target.is_symlink() or (target.exists() and not target.is_dir()):
                conflicts.append(relative)
            elif not target.exists():
                target.mkdir(parents=True, exist_ok=True)
                restored.append(relative)
    manifest["status"] = "rollback_conflicts" if conflicts else "rolled_back"
    manifest["rollback"] = {
        "at": datetime.now(timezone.utc).isoformat(),
        "restored": restored,
        "removed": removed,
        "conflicts": conflicts,
        "forced": force,
    }
    manifest["changes"] = _changes(workspace, manifest)
    manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
    _write_manifest(workspace, selected_id, manifest)
    return manifest


def format_turn_checkpoint(manifest: dict[str, Any]) -> str:
    changes = manifest.get("changes") or []
    counts: dict[str, int] = {}
    for change in changes:
        action = str(change.get("action") or "changed")
        counts[action] = counts.get(action, 0) + 1
    summary = ", ".join(f"{count} {action}" for action, count in sorted(counts.items()))
    coverage = str(manifest.get("coverage") or "built-in file tools")
    return f"{manifest.get('id')}  {manifest.get('status')}  {summary or 'no file changes'}  [{coverage}]"


def rollback_failed_turns_enabled() -> bool:
    return os.environ.get("APSARA_ROLLBACK_FAILED_TURNS", "").strip().lower() in {
        "1", "true", "yes", "on",
    }
