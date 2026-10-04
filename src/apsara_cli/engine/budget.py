"""Per-turn limits and honest accounting for provider usage."""

from __future__ import annotations

import os
from contextvars import ContextVar
from dataclasses import dataclass, field

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
    phase: str = "explore"
    phase_spent: dict[str, int] = field(default_factory=dict)
    review_attempts: int = 0
    implementation_closed: bool = False
    _review_attempts: dict[str, int] = field(default_factory=dict, repr=False)

    @property
    def phase_limits(self) -> dict[str, int]:
        # Small user-selected turns cannot meaningfully reserve a reasoning
        # review plus a final answer. Their overall limit still applies.
        if self.usage_limit < 32_768:
            return {p: self.usage_limit for p in ("explore", "implement", "verify", "review", "finish")}
        shares = {"explore": 20, "implement": 30, "verify": 10, "review": 30, "finish": 10}
        limits = {p: self.usage_limit * share // 100 for p, share in shares.items()}
        limits["finish"] += self.usage_limit - sum(limits.values())
        return limits

    def remaining_for(self, phase: str) -> int:
        if phase == "finish":
            # Only the executor's verified finishing stage can use otherwise
            # unused funds. Proposed edits/reviews are charged back to their
            # own phases before any tool executes.
            return self.remaining_usage
        return min(self.remaining_usage, max(0, self.phase_limits[phase] - self.phase_spent.get(phase, 0)))

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

    def observe_usage(self, usage: dict, *, completion_reserve: int | None = None, phase: str | None = None) -> None:
        normalized = normalize_usage(usage)
        before = self.reported_usage + self.estimated_usage
        self.reported_usage += normalized["total_tokens"]
        self.estimated_usage += normalized["estimated_input_tokens"]
        if normalized["unreported_calls"] or normalized["interrupted_calls"]:
            self.usage_complete = False
            # Reserve the output ceiling for unknown calls; never present this
            # reservation as provider-reported token usage.
            from apsara_cli.engine.llm import DEFAULT_MAX_COMPLETION_TOKENS
            unknown = max(normalized["unreported_calls"], normalized["interrupted_calls"])
            self.estimated_usage += unknown * (completion_reserve or DEFAULT_MAX_COMPLETION_TOKENS)
        active = phase or self.phase
        self.phase_spent[active] = self.phase_spent.get(active, 0) + self.reported_usage + self.estimated_usage - before

    def request_fits(self, estimated_input: int, completion_reserve: int, *, phase: str | None = None) -> bool:
        available = self.remaining_usage if phase is None else self.remaining_for(phase)
        return estimated_input + completion_reserve <= available

    def as_dict(self) -> dict:
        return {**{k: v for k, v in vars(self).items() if not k.startswith("_")},
                "phase_limits": self.phase_limits, "phase_available": self.remaining_for(self.phase)}


_ACTIVE: ContextVar[TurnBudget | None] = ContextVar("apsara_turn_budget", default=None)


def current_budget() -> TurnBudget | None:
    return _ACTIVE.get()
