"""Privacy-safe diagnostic bundles for tester bug reports."""

from __future__ import annotations

import json
import os
import platform
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from apsara_cli import __version__
from apsara_cli.engine.runtime import redact_text, sanitize_value, summarize_text


_MAX_INCLUDED_CONTENT = 20_000
_MAX_LOG_BYTES = 200_000
_MAX_LOG_TEXT = 200_000
_LOG_HEADER = re.compile(r"^\[\d{2}:\d{2}:\d{2}\] \[[A-Z_]+\] ")


def _private_write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _safe_label(value: Any, fallback: str = "unknown") -> str:
    if not isinstance(value, str) or not value:
        return fallback
    return redact_text(value, max_length=200)


def _message_summary(message: Any, *, include_content: bool) -> dict[str, Any]:
    if not isinstance(message, dict):
        return {"role": "unknown", "content": summarize_text(str(message))}

    result: dict[str, Any] = {"role": _safe_label(message.get("role"))}
    for key in ("name", "tool_call_id"):
        if key in message:
            result[key] = _safe_label(message.get(key))

    content = message.get("content")
    if content is not None:
        raw_content = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        result["content"] = (
            redact_text(raw_content, max_length=_MAX_INCLUDED_CONTENT)
            if include_content
            else summarize_text(raw_content)
        )

    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        result["tool_calls"] = []
        for call in tool_calls:
            if not isinstance(call, dict):
                result["tool_calls"].append({"summary": summarize_text(str(call))})
                continue
            function = call.get("function") if isinstance(call.get("function"), dict) else {}
            arguments = function.get("arguments", "")
            raw_arguments = (
                arguments
                if isinstance(arguments, str)
                else json.dumps(arguments, ensure_ascii=False)
            )
            result["tool_calls"].append({
                "id": _safe_label(call.get("id")),
                "type": _safe_label(call.get("type"), "function"),
                "function": {
                    "name": _safe_label(function.get("name")),
                    "arguments": (
                        redact_text(raw_arguments, max_length=_MAX_INCLUDED_CONTENT)
                        if include_content
                        else summarize_text(raw_arguments)
                    ),
                },
            })
    return result


def _safe_options(options: Any) -> dict[str, Any]:
    allowed = (
        "stateless",
        "allow_bash",
        "allowed_commands",
        "max_file_size",
        "auto_approve",
        "use_color",
        "dry_run",
        "read_only",
        "bash_timeout",
    )
    result: dict[str, Any] = {}
    for key in allowed:
        if not hasattr(options, key):
            continue
        value = getattr(options, key)
        if isinstance(value, set):
            value = sorted(str(item) for item in value)
        result[key] = sanitize_value(value, key=key)
    return result


def _bounded_log_text(path: Path) -> tuple[str, int]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Diagnostic logs must be regular files")
    size = path.stat().st_size
    with path.open("rb") as handle:
        omitted = max(0, size - _MAX_LOG_BYTES)
        if omitted:
            handle.seek(-_MAX_LOG_BYTES, os.SEEK_END)
        data = handle.read(_MAX_LOG_BYTES)
    text = data.decode("utf-8", errors="replace")
    if omitted:
        _, separator, complete_tail = text.partition("\n")
        text = complete_tail if separator else text
        text = f"[OMITTED {omitted} leading log bytes]\n{text}"
    return text, omitted


def _sanitize_log(text: str, *, include_content: bool) -> str:
    """Keep log event headers while omitting tool payloads by default."""
    output: list[str] = []
    detail: list[str] = []

    def flush_detail() -> None:
        if not detail:
            return
        raw = "\n".join(detail)
        if include_content:
            cleaned = redact_text(raw, max_length=_MAX_INCLUDED_CONTENT)
            output.extend(f"  | {line}" for line in cleaned.splitlines())
        else:
            output.append(f"  | {summarize_text(raw)}")
        detail.clear()

    for line in text.splitlines():
        if line.startswith("  | "):
            detail.append(line[4:])
            continue
        if (
            line.startswith("--- Session started at ")
            or line.startswith("[OMITTED ")
            or _LOG_HEADER.match(line)
        ):
            flush_detail()
            output.append(redact_text(line, max_length=2_000))
            continue
        # Treat malformed or unstructured log lines as payload. This keeps the
        # default privacy promise even when a log is truncated mid-record.
        detail.append(line)
    flush_detail()
    return redact_text("\n".join(output) + "\n", max_length=_MAX_LOG_TEXT)


def _role_counts(history: Iterable[Any]) -> dict[str, int]:
    counts = Counter(
        _safe_label(message.get("role"))
        if isinstance(message, dict)
        else "unknown"
        for message in history
    )
    return dict(sorted(counts.items()))


def create_diagnostic_bundle(
    workspace: Path,
    current_model: str,
    history: list[dict[str, Any]],
    options: Any,
    log_file: Optional[Path],
    *,
    include_content: bool = False,
    generated_at: Optional[datetime] = None,
) -> Path:
    """Create a bounded bundle that never includes recognizable credentials."""
    created = generated_at or datetime.now(timezone.utc)
    stamp = created.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    bug_dir = workspace.resolve() / ".apsara" / "bugs" / f"bug_{stamp}"
    bug_dir.mkdir(parents=True, mode=0o700, exist_ok=False)
    try:
        bug_dir.chmod(0o700)
    except OSError:
        pass

    session_state = {
        "schema_version": 1,
        "privacy_mode": "content-included" if include_content else "metadata-only",
        "model": redact_text(current_model, max_length=200),
        "history": [
            _message_summary(message, include_content=include_content)
            for message in history
        ],
        "options": _safe_options(options),
    }
    state_path = bug_dir / "session_state.json"
    _private_write(
        state_path,
        json.dumps(session_state, ensure_ascii=False, indent=2) + "\n",
    )

    log_status: dict[str, Any] = {"included": False}
    if log_file is not None and log_file.exists():
        try:
            raw_log, omitted_bytes = _bounded_log_text(log_file)
            sanitized_log = _sanitize_log(raw_log, include_content=include_content)
            log_path = bug_dir / "session.log"
            _private_write(log_path, sanitized_log)
            log_status = {
                "included": True,
                "source_bytes": log_file.stat().st_size,
                "omitted_leading_bytes": omitted_bytes,
                "bundle_bytes": log_path.stat().st_size,
            }
        except (OSError, ValueError) as exc:
            log_status = {
                "included": False,
                "error": f"{type(exc).__name__}: log could not be sanitized",
            }

    files = sorted(path.name for path in bug_dir.iterdir() if path.is_file())
    manifest = {
        "schema_version": 1,
        "generated_at": created.astimezone(timezone.utc).isoformat(),
        "apsara_version": __version__,
        "python": platform.python_version(),
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "model": redact_text(current_model, max_length=200),
        "privacy_mode": session_state["privacy_mode"],
        "content_notice": (
            "Conversation and tool payloads were included after confirmation; "
            "recognizable credentials were still redacted."
            if include_content
            else "Conversation, source, and tool payloads were omitted."
        ),
        "message_count": len(history),
        "message_roles": _role_counts(history),
        "log": log_status,
        "files": [*files, "manifest.json"],
    }
    _private_write(
        bug_dir / "manifest.json",
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    )
    return bug_dir
