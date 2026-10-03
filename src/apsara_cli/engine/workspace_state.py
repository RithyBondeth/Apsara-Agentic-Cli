"""Content fingerprints for verification freshness and recovery conflicts."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

IGNORED = {".git", ".apsara", ".apsara-cli", ".venv", "venv", "node_modules", "target",
           "dist", "build", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".next"}


def ignored_directory(name: str) -> bool:
    return name in IGNORED or name.startswith((".venv-", ".venv_", "venv-", "venv_"))


def path_state(path: Path) -> dict[str, str | None]:
    if path.is_symlink():
        return {"kind": "symlink", "digest": hashlib.sha256(os.fsencode(os.readlink(path))).hexdigest()}
    if path.is_file():
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return {"kind": "file", "digest": digest.hexdigest()}
    return {"kind": "directory" if path.is_dir() else "missing", "digest": None}


def fingerprint(workspace: Path) -> dict[str, dict[str, str | None]]:
    root = workspace.resolve()
    result = {}
    for directory, subdirs, files in os.walk(root, followlinks=False):
        base = Path(directory)
        subdirs[:] = sorted(name for name in subdirs if not ignored_directory(name))
        for name in [*files, *subdirs]:
            path = base / name
            result[str(path.relative_to(root))] = path_state(path)
    return result


def changed_paths(before: dict, after: dict) -> list[str]:
    return sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
