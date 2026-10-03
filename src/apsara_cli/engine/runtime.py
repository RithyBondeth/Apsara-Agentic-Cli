"""Durable, typed execution state for agent runs."""

import json
import hashlib
import re
import threading
from pathlib import Path
from time import time
from typing import Any, Optional

from apsara_cli.shared.types import AgentRun, AgentRunState, AgentStep, ToolResult


_SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|authorization|cookie|credential|password|secret|token)",
    re.IGNORECASE,
)
_SECRET_VALUE_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\b(?:gsk_|AIza|gh[pousr]_)[A-Za-z0-9_-]{12,}\b"),
    re.compile(
        r"(?i)(\b(?:authorization|proxy-authorization|cookie|set-cookie)\b\s*[:=]\s*)"
        r"[^\r\n]+"
    ),
    re.compile(
        r"(?i)(\b[A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)\b\s*[:=]\s*)"
        r"(?:[^\s,;]+|\"[^\"]*\"|'[^']*')"
    ),
)
_OMITTED_ARGUMENT_KEYS = {
    "content", "data", "new_content", "new_text", "old_content", "old_text",
    "patch", "replacement", "text",
}
_MAX_JOURNAL_TEXT = 2_000


def redact_text(value: str, *, max_length: Optional[int] = _MAX_JOURNAL_TEXT) -> str:
    """Remove common credential forms and optionally bound the result."""
    redacted = value
    for pattern in _SECRET_VALUE_PATTERNS:
        if pattern.groups:
            redacted = pattern.sub(lambda match: f"{match.group(1)}[REDACTED]", redacted)
        else:
            redacted = pattern.sub("[REDACTED]", redacted)
    if max_length is not None and len(redacted) > max_length:
        return redacted[:max_length] + f"… [TRUNCATED {len(redacted) - max_length} chars]"
    return redacted


def summarize_text(value: str) -> str:
    """Describe a text payload without persisting its contents."""
    digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"[OMITTED {len(value)} chars sha256:{digest}]"


def sanitize_value(value: Any, *, key: str = "") -> Any:
    """Return a JSON-safe journal value without credentials or source payloads."""
    if _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            str(item_key): sanitize_value(item, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_value(item) for item in value]
    if isinstance(value, str):
        if key.lower() in _OMITTED_ARGUMENT_KEYS:
            return summarize_text(value)
        return redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_text(str(value))


class RunJournal:
    """Append-only JSONL trace plus a latest-state snapshot per run."""

    def __init__(self, workspace: Path, run: AgentRun):
        self.run = run
        self.workspace = workspace.resolve()
        self.directory = workspace / ".apsara" / "runs" / run.run_id
        self.events_path = self.directory / "events.jsonl"
        self.state_path = self.directory / "state.json"
        self._lock = threading.Lock()
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            self._write_state()
            from apsara_cli.engine.turn_checkpoints import begin_turn_checkpoint
            begin_turn_checkpoint(self.workspace, run.run_id, run.objective)
        except OSError:
            pass

    def _write_state(self) -> None:
        self.state_path.write_text(
            json.dumps(sanitize_value(self.run.as_dict()), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def record(self, event_type: str, **data: Any) -> None:
        event = sanitize_value({"timestamp": time(), "type": event_type, **data})
        try:
            with self._lock:
                self.directory.mkdir(parents=True, exist_ok=True)
                with self.events_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(event, ensure_ascii=False) + "\n")
                self._write_state()
        except OSError:
            pass

    def transition(self, state: AgentRunState, error: Optional[str] = None) -> None:
        self.run.state = state
        self.run.error = error
        if state in {
            AgentRunState.BLOCKED,
            AgentRunState.COMPLETED,
            AgentRunState.COMPLETED_VERIFIED,
            AgentRunState.COMPLETED_UNVERIFIED,
            AgentRunState.FAILED,
            AgentRunState.CANCELLED,
        }:
            self.run.finished_at = time()
        self.record("state", state=state.value, error=error)
        if state in {
            AgentRunState.BLOCKED,
            AgentRunState.COMPLETED,
            AgentRunState.COMPLETED_VERIFIED,
            AgentRunState.COMPLETED_UNVERIFIED,
            AgentRunState.FAILED,
            AgentRunState.CANCELLED,
        }:
            try:
                from apsara_cli.engine.turn_checkpoints import (
                    finish_turn_checkpoint,
                    restore_turn_checkpoint,
                    rollback_failed_turns_enabled,
                )
                manifest = finish_turn_checkpoint(self.workspace, self.run.run_id, state.value)
                if (
                    state in {AgentRunState.BLOCKED, AgentRunState.FAILED}
                    and manifest.get("changes")
                    and rollback_failed_turns_enabled()
                ):
                    restored = restore_turn_checkpoint(self.workspace, self.run.run_id)
                    self.record("turn_rollback", rollback=restored.get("rollback"))
            except (OSError, ValueError, FileNotFoundError):
                pass

    def add_step(self, kind: str, title: str, detail: str = "") -> int:
        step = AgentStep(kind=kind, title=title, detail=detail)
        self.run.steps.append(step)
        index = len(self.run.steps) - 1
        self.record("step_added", index=index, step=step.__dict__)
        return index

    def update_step(self, index: int, status: str, detail: str = "") -> None:
        if not 0 <= index < len(self.run.steps):
            return
        step = self.run.steps[index]
        step.status = status
        if detail:
            step.detail = detail
        if status == "in_progress" and step.started_at is None:
            step.started_at = time()
        if status in {"completed", "failed", "blocked"}:
            step.finished_at = time()
        self.record("step_updated", index=index, step=step.__dict__)

    def tool_result(self, name: str, result: ToolResult, arguments: dict[str, Any], risk: str = "read") -> None:
        self.record(
            "tool_result",
            name=name,
            arguments=arguments,
            risk=risk,
            result={
                "ok": result.ok,
                "content_summary": summarize_text(result.content) if result.content else "",
                "error": redact_text(result.error) if result.error else None,
                "metadata": result.metadata,
            },
        )


def latest_run(workspace: Path) -> Optional[dict[str, Any]]:
    runs_dir = workspace / ".apsara" / "runs"
    if not runs_dir.is_dir():
        return None
    states = sorted(runs_dir.glob("*/state.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not states:
        return None
    try:
        return json.loads(states[0].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
