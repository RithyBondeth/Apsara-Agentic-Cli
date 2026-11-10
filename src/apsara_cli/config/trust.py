"""Approval store for workspace-supplied code.

Apsara can execute several definitions that come from the *workspace* rather
than from the user: local tool plugins, MCP servers, verification commands, and
lifecycle hooks. They run with the user's privileges, so merely opening a
cloned repository must not be enough to execute them.

Approvals are recorded per workspace and keyed by a digest of the code, so an
approved file that later changes is re-prompted rather than silently trusted.

Stored file shape (~/.apsara/trust.json):
    {
      "workspaces": {
        "/abs/path/to/project": {
          "plugin:.apsara/tools/lint.py": {
            "sha256": "ab12...",
            "approved_at": "2026-07-29T10:15:00Z"
          },
          "mcp:github": {"sha256": "cd34...", "approved_at": "..."}
        }
      }
    }
"""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

TRUST_PATH = Path.home() / ".apsara" / "trust.json"


def digest_text(text: str) -> str:
    """Stable content digest used as the approval key."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _workspace_key(workspace: Path) -> str:
    try:
        return str(Path(workspace).expanduser().resolve())
    except OSError:
        return str(workspace)


def load_trust(path: Optional[Path] = None) -> dict:
    """Return the parsed trust file, or an empty structure if missing/corrupt."""
    target = path or TRUST_PATH
    if not target.exists():
        return {"workspaces": {}}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return {"workspaces": {}}
    if not isinstance(data, dict) or not isinstance(data.get("workspaces"), dict):
        return {"workspaces": {}}
    return data


def _write_trust(data: dict, path: Optional[Path] = None) -> None:
    target = path or TRUST_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=2), encoding="utf-8")
    target.chmod(0o600)


def is_trusted(
    workspace: Path,
    key: str,
    digest: str,
    path: Optional[Path] = None,
) -> bool:
    """True only if this exact content was previously approved for this workspace."""
    entry = (
        load_trust(path)
        .get("workspaces", {})
        .get(_workspace_key(workspace), {})
        .get(key)
    )
    return isinstance(entry, dict) and entry.get("sha256") == digest


def record_trust(
    workspace: Path,
    key: str,
    digest: str,
    path: Optional[Path] = None,
) -> None:
    """Remember an approval so the user is not asked again for identical content."""
    data = load_trust(path)
    workspaces = data.setdefault("workspaces", {})
    entry = workspaces.setdefault(_workspace_key(workspace), {})
    entry[key] = {
        "sha256": digest,
        "approved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _write_trust(data, path)


def forget_workspace(workspace: Path, path: Optional[Path] = None) -> None:
    """Drop every approval for a workspace (used by `apsara trust --reset`)."""
    data = load_trust(path)
    data.get("workspaces", {}).pop(_workspace_key(workspace), None)
    _write_trust(data, path)


def list_trusted(workspace: Path, path: Optional[Path] = None) -> dict:
    """Return the recorded approvals for a workspace, newest format as stored."""
    return load_trust(path).get("workspaces", {}).get(_workspace_key(workspace), {})
