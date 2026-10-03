"""Bound model-facing output and compact complete exchanges before requests."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

MAX_RESULT_CHARS = 6_000
_RESULT_ID = re.compile(r"^[a-f0-9]{64}$")
_COMPACTION_PREFIX = "Apsara task state after context compaction:\n"


class ContextBudgetError(ValueError):
    pass


def _result_directory(workspace: Path) -> Path:
    workspace = workspace.resolve()
    directory = workspace / ".apsara" / "tool-results"
    for candidate in (directory.parent, directory):
        if candidate.is_symlink():
            raise ValueError("Tool result storage cannot use symlinks.")
    directory.resolve().relative_to(workspace)
    return directory


def bound_tool_result(workspace: Path, text: str, *, limit: int = MAX_RESULT_CHARS) -> str:
    if len(text) <= limit:
        return text
    identifier = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
    try:
        directory = _result_directory(workspace)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / f"{identifier}.txt"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError:
            if path.is_symlink() or not path.is_file():
                raise ValueError("Invalid tool result artifact.")
        else:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(text)
        notice = f"\n[Output shortened from {len(text)} characters. Full result: {identifier}. Use read_tool_result to retrieve sections.]\n"
    except (OSError, ValueError):
        notice = "\n[Output shortened; full result could not be stored. Repeat the source tool with a narrower query.]\n"
    remaining = max(0, limit - len(notice))
    head = remaining * 2 // 3
    tail = remaining - head
    return text[:head] + notice + (text[-tail:] if tail else "")


def read_result(workspace: Path, identifier: str, start_line: int = 1, line_count: int = 80,
                *, char_offset: int | None = None) -> str:
    if not _RESULT_ID.fullmatch(identifier):
        raise ValueError("Invalid result identifier.")
    if start_line < 1 or not 1 <= line_count <= 200:
        raise ValueError("start_line must be positive and line_count must be 1–200.")
    if char_offset is not None and char_offset < 0:
        raise ValueError("char_offset must be non-negative.")
    path = _result_directory(workspace) / f"{identifier}.txt"
    if path.is_symlink():
        raise ValueError("Tool result artifacts cannot use symlinks.")
    lines: list[str] = []
    total = 0
    with path.open(encoding="utf-8") as stream:
        if char_offset is not None:
            remaining = char_offset
            while remaining:
                skipped = stream.read(min(remaining, 8192))
                if not skipped:
                    return "No characters in the requested range."
                remaining -= len(skipped)
            excerpt = stream.read(MAX_RESULT_CHARS - 150)
            if not excerpt:
                return "No characters in the requested range."
            return f"Characters from offset {char_offset}:\n{excerpt}\n[Next character offset: {char_offset + len(excerpt)}]"
        for number, line in enumerate(stream, 1):
            if number < start_line:
                continue
            if number >= start_line + line_count:
                break
            excerpt = f"{number}: {line.rstrip()}\n"
            allowance = MAX_RESULT_CHARS - total - 100
            if len(excerpt) > allowance:
                lines.append(excerpt[:max(0, allowance)] + "\n[Section shortened; use char_offset pagination for long lines.]\n")
                break
            lines.append(excerpt)
            total += len(excerpt)
    return "".join(lines) or "No lines in the requested range."


@dataclass
class PreparedContext:
    messages: list[dict]
    tokens: int
    dropped_messages: int


def build_task_state(messages: list[dict], *, objective: str, changed_files: list[str],
                     baseline_attempted: bool, verification_passed: bool, critic_received: bool) -> str:
    """Carry compact evidence references and notes forward through repeated trims."""
    actions: list[dict] = []
    notes: list[str] = []
    requests: list[str] = []
    for message in messages:
        content = str(message.get("content") or "")
        if message.get("role") == "system" and content.startswith(_COMPACTION_PREFIX):
            try:
                previous = json.loads(content[len(_COMPACTION_PREFIX):])
                if isinstance(previous, dict):
                    actions.extend(item for item in previous.get("recent_actions", []) if isinstance(item, dict))
                    notes.extend(item for item in previous.get("earlier_notes", []) if isinstance(item, str))
                    requests.extend(item for item in previous.get("earlier_requests", []) if isinstance(item, str))
            except (ValueError, TypeError):
                pass
        if message.get("role") == "tool":
            identifier = re.search(r"Full result: ([a-f0-9]{64})", content)
            action = {"tool": message.get("name"), "result": content[:180]}
            if identifier:
                action["result_identifier"] = identifier[1]
            actions.append(action)
        if message.get("role") == "assistant" and content.strip():
            notes.append(content[:240])
        if message.get("role") == "user" and content.strip() and content != objective:
            requests.append(content[:480])
    # Preserve chronology but avoid repeatedly adding the same retained exchange.
    unique_actions = {json.dumps(action, sort_keys=True): action for action in actions}
    unique_notes = list(dict.fromkeys(notes))
    return json.dumps({
        "objective": objective[:4000], "changed_files": changed_files,
        "baseline_attempted": baseline_attempted,
        "latest_full_verification_passed": verification_passed,
        "critic_received": critic_received,
        "recent_actions": list(unique_actions.values())[-8:],
        "earlier_notes": unique_notes[-6:],
        "earlier_requests": list(dict.fromkeys(requests))[-4:],
    }, ensure_ascii=False)


def _exchanges(messages: list[dict]) -> list[list[dict]]:
    """An assistant tool dispatch and all its results are one indivisible group."""
    groups: list[list[dict]] = []
    for message in messages:
        if (message.get("role") == "tool" and groups
                and groups[-1][0].get("role") == "assistant" and groups[-1][0].get("tool_calls")):
            groups[-1].append(message)
        else:
            groups.append([message])
    return groups


def prepare_context(
    messages: list[dict], *, budget: int, estimate: Callable[[list[dict]], int],
    task_state: str, workspace: Path,
) -> PreparedContext:
    """Keep policy, latest user request and latest exchange; drop older groups.

    Compaction is deterministic and makes no auxiliary model calls. When the
    irreducible context cannot fit, fail before sending an oversized request.
    """
    bounded = [dict(message) for message in messages]
    for message in bounded:
        if message.get("role") == "tool" and isinstance(message.get("content"), str):
            message["content"] = bound_tool_result(workspace, message["content"])
    tokens = estimate(bounded)
    if tokens <= budget:
        return PreparedContext(bounded, tokens, 0)
    system = [message for message in bounded if message.get("role") == "system"
              and not str(message.get("content", "")).startswith(_COMPACTION_PREFIX)]
    conversation = [message for message in bounded if message.get("role") != "system"]
    groups = _exchanges(conversation)
    last_user = next((index for index in range(len(groups) - 1, -1, -1)
                      if groups[index][0].get("role") == "user"), None)
    protected = {len(groups) - 1}
    if last_user is not None:
        protected.add(last_user)
    removed: set[int] = set()
    summary = {"role": "system", "content": _COMPACTION_PREFIX + task_state}
    dropped = 0
    for index, group in enumerate(groups):
        if index in protected:
            continue
        removed.add(index)
        dropped += len(group)
        candidate = system + [summary] + [message for position, exchange in enumerate(groups)
                                         if position not in removed for message in exchange]
        tokens = estimate(candidate)
        if tokens <= budget:
            return PreparedContext(candidate, tokens, dropped)
    raise ContextBudgetError(
        f"Required instructions, active tools, skills and latest exchange exceed the {budget}-token "
        "input budget. Narrow the request or select a model with a larger context window."
    )
