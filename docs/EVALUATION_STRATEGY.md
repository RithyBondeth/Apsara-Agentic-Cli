# Coding-agent evaluation strategy

Apsara separates deterministic CI scoring from optional live model evaluation.
CI never needs an API key; a live run writes replayable evidence that can be
scored again without spending tokens.

## Test pyramid

| Layer | Purpose | Target |
|---|---|---|
| Unit | Scoring, manifests, path boundaries, rollback conflicts | Every scoring and recovery branch |
| Integration | Multi-file turns, command snapshots, interrupted runs, CLI dispatch | All terminal states and mutation types |
| Live benchmark | End-to-end model behavior in disposable repositories | Python, TypeScript, Go, and Rust |

The core suite measures the properties that matter for a coding agent:

- 50 points: the repository's independent verification command passes;
- 15 points: the agent reaches a completed state;
- 15 points: changes stay within the allowed production files;
- 10 points: tool calls stay within the case budget;
- 10 points: provider-reported tokens stay within the case budget.

A case must pass verification, avoid unexpected edits, and score at least 80.
Editing tests therefore cannot turn a broken solution into a passing result.

## Running the suite

The fixtures intentionally start with failing tests. A live run uses provider
tokens and copies every fixture beneath `.apsara/benchmarks/`:

```bash
apsara eval evals/coding-core.json --live --model opencode/space-bunny-free --repeat 3
```

`--repeat` runs every case independently from a fresh fixture copy and therefore
multiplies provider usage. Each trial runs the verification commands twice
before and twice after the agent by default. A pass/fail or return-code change
between those checks marks the trial flaky even if its final check passes.
If the fixture baseline passes, is unavailable, or is flaky, the trial is
recorded as invalid and skipped before any provider request is made.

Live suites may set `agent_timeout` globally or per case. The timeout covers the
complete agent turn, including provider waits and tool calls, and defaults to
300 seconds. A timeout is saved as a failed trial instead of leaving a release
gate hanging indefinitely. The bundled core release suite uses 600 seconds to
accommodate the higher latency of the default free model.

Each run produces `results.json`, an aggregate `summary.json`, per-trial run
journals, turn checkpoints, and raw event streams. Re-score every saved trial
offline with:

```bash
apsara eval evals/coding-core.json --results .apsara/benchmarks/<run>/results.json
```

A missing language runtime is recorded as `unavailable`, never mistaken for a
passing verification. Use the JSON suite format to add larger repositories or
provider-specific token and tool-call budgets.

## Comparing context efficiency

```bash
apsara eval evals/pinned-repositories.json --live --compare --model opencode/space-bunny-free --repeat 3
```

The optimized profile starts with core tools and activates specialized schemas
on demand. The reference exposes every enabled tool immediately. Both profiles
retain the same skills, permissions, verification gates, output bounds, and
compaction policy. This comparison isolates schema discovery; it does not
recreate every behavior of an older release. Trials use independent fresh
workspaces and alternate profile order on successive repetitions.

Each profile gets `results.json` and `summary.json`; `comparison.json` reports
sample counts, pass rates, independent verified success, false completion,
unexpected edits, agent latency, and total provider tokens including critic
calls. Raw events record schema count/bytes, estimated input, and active skills.
Missing, interrupted, or partial provider usage is labeled unknown and excluded
from token averages. It earns no token-budget score. Quality can still be scored
from independent checks; unknown usage cannot support a savings claim. Repeated
samples report variance. Small suites are evidence for these tasks and do not
establish general coding ability.

The pinned suite uses boltons and more-itertools at full commit SHAs and injects
specific regressions before baseline checks. The latter spans two modules.
Independent standard-library harnesses pass on the original revisions and fail
after injection. These are repair tasks in real repositories, not upstream bug
claims. Setup paths stay within the disposable workspace, regressions must match
exactly once, and harness files are outside each case's allowed production edits.
Baseline and final checks repeat independently. Changing tests, verification
configuration, or unrelated source files fails constrained-edit scoring.

Cases may use a local fixture or a pinned real repository. Remote sources must
use HTTPS and a full 40-character commit SHA, so a benchmark cannot silently
move when a branch or tag changes:

```json
{
  "name": "real-repository-issue",
  "language": "python",
  "repository": {
    "url": "https://github.com/example/project.git",
    "ref": "0123456789abcdef0123456789abcdef01234567"
  },
  "instruction": "Resolve the pinned issue without editing tests.",
  "verify": [["python", "-m", "pytest", "-q"]],
  "allowed_changes": ["src/**/*.py"]
}
```

## Aggregate release gates

Benchmark suites can define deterministic thresholds:

```json
{
  "verification_repeats": 2,
  "thresholds": {
    "min_pass_rate": 0.8,
    "max_verification_flaky_trials": 0,
    "max_unstable_cases": 0,
    "max_unsafe_trials": 0
  }
}
```

The CLI exits non-zero when the aggregate misses a threshold. Override only the
pass-rate requirement with `--min-pass-rate 90`; flaky verification, mixed
pass/fail outcomes for one case, and edits outside `allowed_changes` remain
separate gates. The summary records score, tool-call, and token averages and
variances plus machine-readable failure categories.

## Release coverage targets

- 100% of transaction restoration and scoring decision branches have tests.
- All offline tests pass on Python 3.10–3.14, macOS and Ubuntu.
- Windows smoke packaging remains green.
- Before a release candidate, run at least three live core-suite trials with
  the default model and retain both `results.json` and `summary.json` as release
  evidence.

## Known gaps

The bundled offline fixtures remain synthetic and relatively small. The runner
supports pinned open-source checkouts, but release owners must select and
maintain those networked cases and perform quality review for solutions that
pass tests while degrading maintainability.
