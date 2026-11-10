import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


SESSION_ROOT_DIR = ".apsara-cli"
SESSIONS_DIR = "sessions"


def sanitize_session_name(name: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-")
    if not sanitized:
        raise ValueError(
            "Session name must contain letters, numbers, dots, dashes, or underscores."
        )
    return sanitized


def get_sessions_dir(workspace_root: Path) -> Path:
    return workspace_root / SESSION_ROOT_DIR / SESSIONS_DIR


def get_session_path(workspace_root: Path, session_name: str) -> Path:
    return get_sessions_dir(workspace_root) / f"{sanitize_session_name(session_name)}.json"


def new_session_name(now: datetime | None = None) -> str:
    """Return a readable, collision-resistant name for a fresh conversation."""
    started = now or datetime.now().astimezone()
    return f"session-{started.strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:4]}"


def latest_session_name(workspace_root: Path) -> str | None:
    """Return the most recently updated saved session, if one exists."""
    candidates: list[tuple[int, str]] = []
    for path in list_sessions(workspace_root):
        try:
            modified = path.stat().st_mtime_ns
        except OSError:
            continue
        candidates.append((modified, path.stem))
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item[0], item[1]))[1]


def load_session_messages(workspace_root: Path, session_name: str) -> list[dict[str, Any]]:
    session_path = get_session_path(workspace_root, session_name)
    if not session_path.exists():
        return []

    payload = json.loads(session_path.read_text(encoding="utf-8"))
    messages = payload.get("messages", [])
    if not isinstance(messages, list):
        raise ValueError(f"Session file '{session_path}' is invalid.")
    return messages


def load_session_usage(workspace_root: Path, session_name: str) -> dict[str, Any]:
    """Load persisted usage; older session files transparently return empty usage."""
    session_path = get_session_path(workspace_root, session_name)
    if not session_path.exists():
        return {}
    payload = json.loads(session_path.read_text(encoding="utf-8"))
    usage = payload.get("usage", {})
    return usage if isinstance(usage, dict) else {}


def save_session_messages(
    workspace_root: Path,
    session_name: str,
    model: str,
    messages: list[dict[str, Any]],
    usage: dict[str, Any] | None = None,
) -> Path:
    session_path = get_session_path(workspace_root, session_name)
    session_path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "session_name": sanitize_session_name(session_name),
        "workspace_root": str(workspace_root),
        "model": model,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "messages": messages,
        "usage": usage or {},
    }
    session_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return session_path


def list_sessions(workspace_root: Path) -> list[Path]:
    sessions_dir = get_sessions_dir(workspace_root)
    if not sessions_dir.exists():
        return []
    return sorted(path for path in sessions_dir.glob("*.json") if path.is_file())
