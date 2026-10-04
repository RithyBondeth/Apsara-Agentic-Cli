# Completion validation — 2026-10-04

Candidate `0.1.0a2` remains unpublished. The implementation is stronger, but the
free model has not met the stability gates for a production release. Every live
call used OpenCode Space Bunny Free; paid fallbacks were disabled. The colorful
OpenCode-inspired terminal layout is retained.

## Implemented

1. **Verified completion:** retain the original objective and current source,
   verification, and review state to guide final answers. Reuse covered file
   reads and passing evidence only while source/policy fingerprints match.
   Explicit fresh checks discard older evidence; external changes invalidate it.
2. **Visible limits:** `/budget`, the sidebar, and saved reports show steps,
   requested tools, reported tokens, estimated usage, and reused results. Default
   limits are 25 steps, 50 tools, and 100,000 cumulative tokens. Budget stops
   preserve edits without claiming completion. Auxiliary reviews count too.
3. **Context and review:** compact older exchanges earlier to reserve headroom,
   preserving required context. Scope trust freshness to this workspace. Final
   reviews receive the original objective and current verification evidence;
   accept single fenced JSON verdicts and allow up to 8,192 review-output tokens.
   Material findings, unavailable verdicts, and stale checks still block approval.
4. **Real sessions:** repeated Python/Node/Go/Rust coding trials, pinned repository
   comparisons, saved-session recovery, undo, and a live local MCP round-trip.
5. **Candidate packaging:** update dependency security floors, install guides,
   and version metadata; prepare a wheel and source distribution without publishing.

## Final runtime results

Tested runtime commit `84ebde5`; source digest
`e693a6573a66f8a21f5e360e42ac74be5755718174bc598541a2024161927e80`.
The source manifest matched every runtime file throughout both final batches.
Three fresh trials per core case produced **12/15 passing trials**.

| Core task | Passing trials |
| --- | --- |
| Go tags | 3/3 |
| Python multi-file settings | 1/3 |
| Python dedupe | 3/3 |
| Rust ports | 3/3 |
| Node chunking | 2/3 |

Aggregate acceptance: **failed**. The core suite requires at least 80% passing
trials and zero unstable cases; it recorded 2 unstable cases,
0 flaky verification trials, and 0 unexpected-production-edit trials.
Provider usage was incomplete in 1 trial; missing totals are
reserved conservatively by the runtime rather than treated as zero.

The fixture named `typescript-safe-chunking` runs JavaScript `.mjs` with Node;
it does not establish TypeScript compiler coverage. A trial needs independent
verification, constrained edits, and a score of at least 80 to pass. Passing
trials can exceed a case's soft efficiency targets. Runtime completion and
conservative turn limits remain separate requirements.

The final pinned repository comparison passed **2/6 optimized trials** and
**1/6 reference trials**, below its 100% acceptance threshold. Boltons passed
2/3 optimized and 1/3 reference trials; more-itertools passed 0/3 in both.
All twelve trials reported usage. Neither profile recorded false completion or
unexpected production edits, but several blocked turns left failing verification
or no repair. Larger-task reliability remains unresolved.

Mean reported usage across successful and failed pinned trials was 81,734 tokens
optimized versus 94,081 reference. Those mixed-outcome averages are **not proof
of dependable net token savings** for completed work. This sample is too small
and unstable to claim general quality or efficiency gains.

Retained summaries: [final core](validation/0.1.0a2/adaptive-core.json),
[final pinned trials](validation/0.1.0a2/adaptive-pinned.json), and
[comparison](validation/0.1.0a2/adaptive-pinned-comparison.json).

## Iteration history

Earlier results remain visible rather than being replaced by the final sample.

| Runtime iteration | Core passes | Multi-file passes | Pinned optimized/reference |
| --- | --- | --- | --- |
| Initial bounded runtime (`d49a…`) | 12/15 | 0/3 | Not run |
| Expanded review/accounting (`f18c…`, `52772fb`) | 11/15 | 0/3 | 1/6, 0/6 |
| Adaptive context/objective evidence (`e693…`, `84ebde5`) | 12/15 | 1/3 | 2/6, 1/6 |

The first batch's reviews consumed their 4,096-token response allowance without
returning a verdict. The expanded allowance addressed that constraint, but did
not make the subsequent batch stable. A prompt-only scope experiment also failed
to prevent a suggested unsupported input from becoming a review requirement.
The final runtime supplies actual objective/verification evidence and replaces
agent-suggested final-review hints. Model-generated review can still be wrong;
these small batches do not isolate causal improvements.

Earlier [core](validation/0.1.0a2/initial-core.json),
[bounded core](validation/0.1.0a2/bounded-core.json), and
[bounded comparison](validation/0.1.0a2/bounded-pinned-comparison.json) preserve
exact source digests, dependencies, budgets, and every trial outcome.

## Recovery and MCP

A controlled live trial interrupted immediately after the first edit, preserved
changes and completed tool exchanges in a saved session, then loaded the session
and finished with full verification. Independent verification passed twice.
The resumed turn made no further edits and ended `completed`; the earlier turn
remained `cancelled`. Undo restored the original fixture without conflicts.
Reported usage was 12,552 tokens before interruption and 13,517 after resume.

That [recovery trial](validation/0.1.0a2/live-recovery.json) used the preceding
`0daaea4aab1da3442de115781ec5b514dd0e79cd8c39d00767465951aaa4ffd2`
runtime. Later changes affected accounting, context, review, and trust freshness;
session/cancellation/undo paths were unchanged and have current offline coverage.

The final runtime's [live MCP check](validation/0.1.0a2/live-mcp.json) passed:
the model discovered a local read-only stdio tool, called it, and returned its
exact nonce using two tool calls and 9,052 reported tokens. This validates that
local integration path, not arbitrary remote MCP services.

## Installation and platform checks

- 529 local tests passed on macOS/Python 3.12.14.
- All 18 CI jobs passed on runtime commit `84ebde5`: Linux/macOS Python
  3.10–3.14, Windows smoke coverage, platform pipx checks, and package/dependency audit.
- Updated installed dependencies had no known vulnerabilities reported by
  pip-audit. This is the audit result for those versions, not a future guarantee.
- Wheel/sdist metadata checks, clean installation, dependency consistency,
  version/help checks, and the isolated pipx install/init/upgrade/uninstall
  lifecycle passed. A no-key clean environment correctly reported missing
  provider credentials; no-live doctor was not an all-green credential check.
- Go 1.27.1 and Rust 1.99.0 were installed in a temporary validation directory;
  shell profiles and global toolchains were unchanged.

Artifact files and checksums are retained locally under
`.apsara/reports/candidate-0.1.0a2/`. Other provider adapters have offline coverage;
no paid provider was live-tested. See [provider scope](PROVIDER_VALIDATION.md).

The remaining production blocker is reliable completion on repeated larger
coding tasks. Do not lower review/verification gates or raise budgets merely to
turn these failures into passing scores. Review candidate edits and reports as
an alpha; no release tag, GitHub release, or PyPI publication was created.
