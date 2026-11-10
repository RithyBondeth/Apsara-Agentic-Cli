"""Disposable workspace snapshots for running project checks away from source."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


_COPY_IGNORES = {
    ".git", ".apsara", ".apsara-cli", ".venv", "venv", "node_modules",
    "target", "dist", "build", "__pycache__",
}


def _copy_ignore(directory: str, names: list[str]) -> set[str]:
    ignored = {name for name in names if name in _COPY_IGNORES}
    root = Path(directory)
    ignored.update(name for name in names if (root / name).is_symlink())
    return ignored


def _validate_snapshot_links(workspace: Path) -> None:
    """Reject links that could let isolated checks reach outside the snapshot."""
    root = workspace.resolve()
    for candidate in workspace.rglob("*"):
        if not candidate.is_symlink():
            continue
        try:
            candidate.resolve(strict=False).relative_to(root)
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"Isolated workspace contains an external symlink: {candidate.relative_to(workspace)}"
            ) from exc


def _git_root(workspace: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    root = Path(result.stdout.strip()).resolve()
    return root if root == workspace.resolve() else None


def _copy_untracked(source: Path, target: Path) -> None:
    result = subprocess.run(
        ["git", "-C", str(source), "ls-files", "--others", "--exclude-standard", "-z"],
        capture_output=True,
        timeout=15,
        check=False,
    )
    if result.returncode != 0:
        return
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        relative = Path(raw.decode("utf-8", errors="surrogateescape"))
        if relative.is_absolute() or ".." in relative.parts:
            continue
        src = source / relative
        dest = target / relative
        if src.is_symlink() or not src.is_file():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)


def _apply_working_patch(source: Path, target: Path) -> None:
    patch = subprocess.run(
        ["git", "-C", str(source), "diff", "--binary", "HEAD", "--", "."],
        capture_output=True,
        timeout=30,
        check=False,
    )
    if patch.returncode != 0 or not patch.stdout:
        return
    applied = subprocess.run(
        ["git", "-C", str(target), "apply", "--whitespace=nowarn", "-"],
        input=patch.stdout,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if applied.returncode != 0:
        raise RuntimeError(applied.stderr.decode("utf-8", errors="replace").strip())


@contextmanager
def isolated_workspace(workspace: Path) -> Iterator[Path]:
    """Yield a disposable snapshot containing the workspace's current changes."""
    workspace = workspace.resolve()
    with tempfile.TemporaryDirectory(prefix="apsara-isolated-") as temp:
        target = Path(temp) / "workspace"
        git_root = _git_root(workspace)
        worktree_created = False
        try:
            if git_root is not None:
                created = subprocess.run(
                    ["git", "-C", str(workspace), "worktree", "add", "--detach", str(target), "HEAD"],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
                if created.returncode != 0:
                    raise RuntimeError(created.stderr.strip() or "git worktree creation failed")
                worktree_created = True
                _apply_working_patch(workspace, target)
                _copy_untracked(workspace, target)
                _validate_snapshot_links(target)
            else:
                shutil.copytree(
                    workspace,
                    target,
                    ignore=_copy_ignore,
                    symlinks=False,
                )
            yield target
        finally:
            if worktree_created:
                subprocess.run(
                    ["git", "-C", str(workspace), "worktree", "remove", "--force", str(target)],
                    capture_output=True,
                    timeout=30,
                    check=False,
                )
