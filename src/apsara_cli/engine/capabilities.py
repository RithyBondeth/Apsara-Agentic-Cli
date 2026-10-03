"""Per-turn tool activation and pinned skill instructions."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Iterator

CORE_TOOLS = frozenset({
    "read_file", "read_file_lines", "search_files", "glob_search", "repository_map",
    "edit_file", "write_to_file", "git_diff", "verify_project", "request_critic",
    "discover_tools", "list_skills", "read_skill", "read_skill_resource", "read_tool_result",
})
MAX_ACTIVE_TOOLS = 32
MAX_ACTIVE_SKILL_CHARS = 24_000


@dataclass
class CapabilityState:
    tools: set[str] = field(default_factory=set)
    skills: dict[str, str] = field(default_factory=dict)


_state: ContextVar[CapabilityState | None] = ContextVar("apsara_capabilities", default=None)


def current_capabilities() -> CapabilityState | None:
    return _state.get()


@contextmanager
def capability_context() -> Iterator[CapabilityState]:
    state = CapabilityState()
    token = _state.set(state)
    try:
        yield state
    finally:
        _state.reset(token)
