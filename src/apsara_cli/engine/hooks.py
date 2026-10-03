"""Trusted lifecycle hooks with JSON input and bounded output."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


HOOK_EVENTS = {
    "session_start", "before_tool", "after_tool", "before_verify",
    "after_verify", "turn_end",
}


@dataclass(frozen=True)
class HookOutcome:
    allowed: bool = True
    reason: str = ""
    output: str = ""


def _load_hooks(workspace: Path) -> tuple[Path | None, dict[str, Any], str]:
    path = workspace / ".apsara" / "hooks.json"
    if not path.exists():
        return None, {}, ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return path, {}, f"Invalid .apsara/hooks.json: {exc}"
    if not isinstance(payload, dict):
        return path, {}, "Invalid .apsara/hooks.json: top level must be an object"
    return path, payload, ""


def run_hooks(event: str, payload: dict[str, Any], workspace: Path) -> HookOutcome:
    if event not in HOOK_EVENTS:
        raise ValueError(f"Unsupported hook event: {event}")
    from apsara_cli.engine.tools import _read_only, request_workspace_trust

    if _read_only():
        return HookOutcome(True, "Hooks skipped in read-only mode")
    path, config, error = _load_hooks(workspace)
    if error:
        return HookOutcome(False, error)
    entries = config.get(event, [])
    if not entries:
        return HookOutcome()
    if not isinstance(entries, list):
        return HookOutcome(False, f"Invalid {event} hook configuration")

    from apsara_cli.config import trust
    try:
        source = path.read_text(encoding="utf-8") if path else ""
    except OSError as exc:
        return HookOutcome(False, f"Could not read .apsara/hooks.json: {exc}")
    if not request_workspace_trust(
        "hooks:.apsara/hooks.json",
        trust.digest_text(source),
        {
            "kind": "hooks",
            "display_path": ".apsara/hooks.json",
            "source_preview": source[:1200],
            "event": event,
        },
    ):
        return HookOutcome(False, "Workspace hooks were not approved")

    outputs: list[str] = []
    hook_input = json.dumps({"event": event, "payload": payload})
    for index, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            return HookOutcome(False, f"Invalid {event} hook #{index}")
        command = entry.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(item, str) for item in command):
            return HookOutcome(False, f"Hook {event} #{index} command must be a string array")
        try:
            timeout = max(1, min(int(entry.get("timeout", 30)), 300))
        except (TypeError, ValueError):
            return HookOutcome(False, f"Hook {event} #{index} timeout must be an integer")
        try:
            from apsara_cli.engine.cancellation import run_command
            from apsara_cli.engine.turn_checkpoints import capture_turn_workspace
            capture_turn_workspace(workspace)
            result = run_command(command, cwd=workspace, input_text=hook_input, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return HookOutcome(False, f"Hook {event} #{index} failed: {exc}")
        output = (result.stdout + result.stderr)[-4000:]
        outputs.append(output)
        if result.returncode != 0:
            return HookOutcome(False, f"Hook {event} #{index} exited {result.returncode}", output)
        try:
            decision = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            decision = {}
        if isinstance(decision, dict) and decision.get("decision") == "deny":
            return HookOutcome(False, str(decision.get("reason") or "Denied by hook"), output)
    return HookOutcome(True, output="\n".join(outputs)[-4000:])
