# Completion validation — 2026-10-04

Candidate: `0.1.0a2`, unpublished. Live calls use OpenCode Space Bunny Free
only, with paid fallbacks disabled. The colorful CLI layout is retained.

## Implementation

1. Add bounded steps, tool calls, and token accounting with `/budget`, live
   sidebar counters, durable reports, and preserved edits on a budget stop.
2. Guide completion using the original objective and current verification and
   review evidence. Reuse covered file reads and current passing evidence;
   source/policy changes and explicit fresh checks invalidate reuse.
3. Support fenced JSON critic verdicts and budget an 8,192-token review response
   allowance. Material findings and missing verdicts still prevent approval.
4. Test the free provider only; distinguish offline adapter coverage from live
   coding certification in `PROVIDER_VALIDATION.md`.
5. Prepare a new wheel and source distribution with updated dependency security
   floors. Keep the original published release notes as historical evidence.

## First batch

Source digest `d49a5345c54c2499b7b9dac2a5bbba77ec29480e277cf17e53cb6491c6118b79`.
The source manifest was checked unchanged at the end of the batch. This batch
predates the larger critic allowance and the updated SDK dependencies.

Three fresh trials per core case yielded 12/15 passing trials: Python dedupe,
Node chunking, Go tags, and Rust ports passed 3/3 each. Layered settings was
blocked 3/3 despite passing independent tests. Review calls spent the entire
4,096-token output allowance on reasoning, leaving no verdict. The 100,000-token
turn allowance then prevented more calls. Nothing was falsely marked complete.
Zero flaky verification trials, unstable cases, or unsafe edits were recorded.

The configured aggregate threshold is 80%, which this batch met. Its 0/3
multi-file completion result is still a material limitation.

Local evidence:
`.apsara/benchmarks/completion-core/20261004T120115Z-f2c816/`.

## Candidate validation

Repeated-batch source digest:
`f18c2cf9e519f46f21eed2b6616b2e2de1c4970ebc03e9f024739360cd2b8677`.

The bounded-runtime core repeat and pinned comparison are recorded separately
from the subsequent adaptive-context iteration. Their retained results will replace this progress note before the
implementation is considered fully reviewed. No release has been published.

Local checks: 529 tests passed; updated installed dependencies had no known
vulnerabilities according to pip-audit. Wheel and sdist metadata checks passed,
as did clean installation, dependency consistency, and the pipx lifecycle.
These local checks ran on macOS/Python 3.12; cross-platform CI is separate.

## Live recovery

A controlled trial interrupted the agent immediately after its first edit,
preserved that change and the completed tool exchanges in a saved session,
then loaded the session and finished the original task with full verification.
Independent verification passed twice. The resumed turn made no further source
changes and ended as `completed`; the earlier turn remained `cancelled`.
Undoing both checkpoints restored the original fixture with no conflicts.

This trial used the preceding source digest
`0daaea4aab1da3442de115781ec5b514dd0e79cd8c39d00767465951aaa4ffd2`.
The only subsequent runtime change corrected accounting when a critic omits
usage totals; recovery paths were unchanged. That correction has offline
regression coverage. The recovery trial reported all its provider usage:
12,552 tokens before interruption and 13,517 in the resumed turn.

Local evidence:
`.apsara/benchmarks/live-recovery/20261004T123534Z/result.json`.

## Scope

Small bundled tasks and injected regressions are limited samples. Net token
savings require matched, repeated, quality-qualified comparison results; the
older pilot and updated candidate are not a controlled causal comparison.
Go 1.27.1 and Rust 1.99.0 were installed under a temporary validation directory,
without modifying shell profiles. No paid provider was called.

## Adaptive-context iteration

Source digest `e693a6573a66f8a21f5e360e42ac74be5755718174bc598541a2024161927e80`.
This adds earlier context trimming as the turn allowance shrinks, preserves
protected context through a fallback to the normal model limit, scopes trust
freshness to the current workspace, and supplies final reviews with runtime
verification evidence and the actual user objective.

The repeated core and pinned comparisons are running on this source. The wheel
will be rebuilt from the same source and its file hashes checked before handoff.
A prompt-only review-scope experiment was insufficient: the model still treated
a suggested unsupported input as a requirement. Those exploratory calls are
retained locally; the final runtime replaces agent-suggested final-review hints
with objective/verification evidence rather than relying on a prompt alone.
