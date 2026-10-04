# Free-model coding validation — 2026-10-03

`opencode/space-bunny-free` works through Apsara with the existing OpenCode key.
The startup default now uses that free model; `--model bunny` selects it
explicitly. Paid requests and automatic fallback were disabled for these trials.
Explicit existing model settings remain authoritative.

The pilot does **not** pass the coding release gates. Seven real model-driven
coding trials passed their independent verification commands, but only one
completed within the benchmark's completion, tool, token, and edit constraints.
Six exhausted the agent's 25-step limit. This distinguishes a correct repair
from an efficient, reliably completed coding session.

## Evidence

- Default startup live probe: streaming and valid tool arguments passed; 448
  provider-reported tokens; `../.apsara/benchmarks/live-readiness/default-free-doctor.txt`.
- Live local MCP: the model discovered and called
  `mcp__validation__get_validation_nonce`, then returned the server-generated
  nonce. Only discovery and the read-only MCP tool were called. Evidence:
  `../.apsara/benchmarks/live-readiness/live-mcp-result.json`.
- Core results: `../.apsara/benchmarks/free-model-coding/20261003T133558Z-0e17cc/results.json`.
- Pinned comparison: `../.apsara/benchmarks/free-model-pinned/20261003T133753Z-839e2f/comparison.json`.
- Apsara regression suite: 486 passed. Eight targeted local MCP, cancellation,
  checkpoint, recovery, and undo checks also passed; those recovery checks do
  not establish the live model's ability to resume an interrupted coding task.

These are local artifacts, not published release attachments. The tested source
included uncommitted runtime and model-access changes based on main `a5480ea`.
Go and Cargo were unavailable; those two core cases were skipped and do not
count as successful coding trials. Python verifier commands used the Apsara
Python 3.12 virtualenv on PATH, rather than the system Python 3.9.

## Coding outcomes

One fresh trial per case/profile was run. Provider usage was complete for all
seven executed coding trials, including critic calls. Durations cover the agent
turn, not fixture setup or the independent checks after it.

| Task | Profile | Verification | Agent state | Tool calls / budget | Total tokens / budget | Seconds |
| --- | --- | --- | --- | --- | --- | --- |
| Python dedupe | Optimized | Passed | Completed verified | 8 / 12 | 23,262 / 50,000 | 35.5 |
| Node chunking | Optimized | Passed | Blocked | 30 / 12 | 157,689 / 50,000 | 210.5 |
| Python layered settings | Optimized | Passed | Blocked | 33 / 20 | 243,957 / 75,000 | 367.7 |
| boltons chunk contract | Optimized | Passed | Blocked | 33 / 18 | 342,116 / 75,000 | 182.5 |
| boltons chunk contract | Reference | Passed | Blocked | 35 / 18 | 339,935 / 75,000 | 351.0 |
| more-itertools chunk contract | Optimized | Passed | Blocked | 39 / 24 | 349,637 / 100,000 | 276.5 |
| more-itertools chunk contract | Reference | Passed | Blocked | 37 / 24 | 425,431 / 100,000 | 297.8 |

The core suite's full aggregate is 1/5 because it includes two unavailable
language cases. Among runnable core cases, 1/3 passed all scoring requirements.
Both pinned profiles scored 0/2. No false completion or flaky verification was
recorded. Reference boltons left `_apsara_chunk_probe.py` and
`_apsara_upstream_cmp.py`, outside the allowed edit paths; its constrained-edit
check failed. These were extra debug scripts, not modified verification tests.

Additional checks on the optimized repository repairs passed:

- boltons `tests/test_iterutils.py`: 50 tests. The repaired production source
  matches the pinned upstream revision.
- more-itertools `tests/test_recipes.py` and `tests/test_more.py`: 767 tests and
  21,202 subtests. The functional regression was repaired; extra comments remain
  in `recipes.py` relative to upstream.

The repository cases inject known regressions into pinned revisions. They do
not demonstrate discovery or resolution of unrelated upstream bugs.

## Token comparison limits

Pinned optimized trials averaged 345,876.5 total provider tokens and 229.524
seconds, versus 382,683 tokens and 324.405 seconds for reference. These are two
failed-budget trials per profile, with one sample per task and no repeated
quality-qualified successes. They do not establish dependable token savings or
general coding quality. Cached input is included in total token counts.

## Next work

1. Reduce repeated reads, revisions, and verification after task evidence is
   current; finish only after the requested work and applicable review gates
   are satisfied. Preserve the guards against false completion.
2. Enforce or clean up temporary debug files so constrained edits stay within
   the task's allowed production paths.
3. Repeat the available cases from fresh copies after those changes. Install
   Go and Rust locally before claiming full core language coverage.
4. If a paid comparison is useful, test direct DeepSeek access with the same
   cases and a small budget. No DeepSeek key or live DeepSeek evidence was
   available in this session. Refresh its legacy model presets and validate
   multi-turn thinking/tool-message handling before relying on that provider.

OpenCode currently lists Space Bunny as temporarily free:
https://opencode.ai/docs/zen/

DeepSeek's current Flash model is `deepseek-flash`, with tool-call support and
uncached-input prices of $0.15–$0.30 per million tokens, and output prices of
$0.60–$1.20 per million, depending on off-peak/peak hours. A hypothetical session
totaling 100,000 uncached input tokens and 10,000 output tokens costs about
$0.021–$0.042 at those rates. This is a token-cost illustration, not a prediction
of Apsara session usage or a minimum top-up amount. Check current official rates:
https://api-docs.deepseek.com/quick_start/pricing/
