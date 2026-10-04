"""Per-turn limits and honest accounting for provider usage."""

from __future__ import annotations

import os
from contextvars import ContextVar
from dataclasses import dataclass

from apsara_cli.engine.usage import normalize_usage


def _limit(name: str, default: int, ceiling: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return max(1, min(value, ceiling))


@dataclass
class TurnBudget:
    step_limit: int
    tool_call_limit: int = 50
    usage_limit: int = 100_000
    steps_used: int = 0
    tool_calls_used: int = 0
    reported_usage: int = 0
    estimated_usage: int = 0
    usage_complete: bool = True
    reused_checks: int = 0

    @classmethod
    def from_environment(cls, step_limit: int) -> "TurnBudget":
        return cls(
            step_limit=step_limit,
            tool_call_limit=_limit("APSARA_MAX_TOOL_CALLS", 50, 500),
            usage_limit=_limit("APSARA_MAX_TURN_TOKENS", 100_000, 10_000_000),
        )

    @property
    def remaining_usage(self) -> int:
        return max(0, self.usage_limit - self.reported_usage - self.estimated_usage)

    def observe_usage(self, usage: dict, *, completion_reserve: int | None = None) -> None:
        normalized = normalize_usage(usage)
        self.reported_usage += normalized["total_tokens"]
        self.estimated_usage += normalized["estimated_input_tokens"]
        if normalized["unreported_calls"] or normalized["interrupted_calls"]:
            self.usage_complete = False
            # Reserve the output ceiling for unknown calls; never present this
            # reservation as provider-reported token usage.
            from apsara_cli.engine.llm import DEFAULT_MAX_COMPLETION_TOKENS
            self.estimated_usage += completion_reserve or DEFAULT_MAX_COMPLETION_TOKENS

    def request_fits(self, estimated_input: int, completion_reserve: int) -> bool:
        return estimated_input + completion_reserve <= self.remaining_usage

    def as_dict(self) -> dict:
        return dict(vars(self))


_ACTIVE: ContextVar[TurnBudget | None] = ContextVar("apsara_turn_budget", default=None)


def current_budget() -> TurnBudget | None:
    return _ACTIVE.get()
