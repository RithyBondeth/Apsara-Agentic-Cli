# Retained alpha candidate evidence

These summaries record all completed trial outcomes, including blocked turns.
Only OpenCode Space Bunny Free was used, with paid fallbacks disabled.

- `initial-core.json`: first bounded-runtime batch, source digest `d49a...`.
- `bounded-core.json` and `bounded-pinned*.json`: subsequent iteration,
  source digest `f18c...`, commit `52772fb`.
- `adaptive-core.json` and `adaptive-pinned*.json`: final runtime iteration,
  source digest `e693...`, commit `84ebde5`.
- `live-recovery.json`: interruption, saved-session resume, verification,
  and undo on the preceding `0daa...` runtime.
- `live-mcp.json`: final runtime's read-only local MCP tool round-trip.

Full source digests, budgets, dependency versions, provider usage completeness,
trial states, changed paths, and verification results are retained in JSON.
No credentials, private source contents, or conversation transcripts are included.
The Node chunking fixture is named `typescript-safe-chunking` in the suite but
runs JavaScript `.mjs` with Node; it does not validate the TypeScript compiler.

Core trial passes require independent verification, constrained edits, and a
score of at least 80. A passing trial can exceed a case's soft tool/token target;
it cannot bypass the runtime's completion checks. Core aggregate acceptance
also requires at least 80% passing trials and no unstable cases, flaky
verification, or unexpected production edits. Pinned comparisons require 100%
passing trials. Comparison token/latency means include failures and must not be
presented as successful-work efficiency gains.

See [the interpretation and limitations](../../COMPLETION_VALIDATION_2026-10-04.md).
