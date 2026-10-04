# Phase budgets and review recovery — 2026-10-04

The four requested improvements are implemented in the unpublished `0.1.0a2`
candidate: bounded review recovery, protected phase allowances, focused repeated
coding trials, and successful-work efficiency reporting. Only OpenCode Space
Bunny Free was called; paid fallbacks remained disabled. The colorful CLI is retained.

## Runtime changes

- For unchanged source and policy, reviews have at most two provider attempts.
  A missing structured verdict or timeout can trigger one recovery call with the
  complete evidence, original objective, verification, and prior unstructured
  concerns. Findings are never retried into approval. Exhausted recovery blocks
  honestly, and unknown/cancelled usage is reserved and counted.
- The 25-step, 50-tool, 100,000-token overall limits are unchanged. Nominal
  allocations are exploration 20,000, implementation 30,000, verification
  10,000, review 30,000, and finishing 10,000. Implementation can reclaim unused
  exploration funds while preserving checking/review/finishing reserves. Verification can use unused work
  funds without double-counting implementation headroom. Review and finishing
  remain protected. Verified finishing can use otherwise unused funds.
- Requests are charged by the proposed action, so optional review or late edits
  do not spend the final-answer reserve. When implementation closes, reserved
  verification can still run; it cannot fund more edits. Source/policy changes
  and material findings still require fresh checks and approval.
- The grep fallback uses extended patterns for common regex behavior and skips
  agent state/dependency directories. This fixes observed alternation and literal
  parenthesis search failures when ripgrep is absent; it does not promise full
  compatibility between every regex dialect or ignore-file rule.
- Comparisons separate successful repairs from failures and incomplete usage,
  match unique case/trial IDs, and include failed attempts in total cost per
  completion. Fewer than three matched successful pairs per case cannot be
  labeled a repeated sample. These results are not general causal claims.

## Tested source and offline checks

Live runtime commit `32ed04c`; SHA256 source digest
`1cdb0fa18bb90732beec301bca2563eba9028bd838fa403f7f73c4cfdec87a7a`. Both profiles loaded this runtime before the final
safety fix. Committed runtime file hashes match the retained manifest; the
working files were subsequently changed, so these trials do not certify the
final artifact. Python 3.12.14; exact SDK versions are retained in JSON.

Final artifact runtime `44a146d` has source digest
`337ce2dde3a22ed84502d8074b3561c88dc04dfecf1eee97c88856d760390a53`. Its additional safety fix clears an older approval
when a fresh review is unavailable. A regression test covers this path; the live
matrix was not repeated after that one-line fix.

**556 local tests and all 18 final-runtime CI jobs passed.** Coverage includes phase
handoffs, targeted-to-full checks, optional review accounting, preserved edits,
unknown usage, cancellation, repeated-review caps, material findings, search
fallback behavior, and quality-qualified comparison exclusions. Existing platform
CI covers Linux/macOS Python 3.10–3.14, Windows smoke and pipx checks, and package
and dependency auditing. The final wheel and source distribution passed metadata validation. The wheel
runtime matches the final source manifest, and clean installation passed dependency
and CLI checks. Isolated pipx lifecycle results and package checksums are retained
in [artifact evidence](validation/phase-recovery/artifact-checks.json).

## Focused free-model repeats

The focused suite selects the existing two failing core cases without changing
their instructions, soft efficiency limits, verification repetition, or acceptance
gates. The Node fixture is JavaScript `.mjs`; it is not TypeScript compiler coverage.

| Task | Passing trials |
| --- | --- |
| Python multi-file settings | 1/3 |
| Node chunking | 3/3 |

Result: **4/6**, aggregate acceptance **failed**;
1 unstable case, 0 flaky verification trials,
and 0 unexpected-production-edit trials. Usage was incomplete in
1 trial. Passing trial scores still permit missed soft
case efficiency targets; runtime completion checks remain required.

[All focused trial outcomes](validation/phase-recovery/focused-core.json).

## Pinned-repository comparison

The unchanged pinned suite ran three fresh trials per case in each profile,
counterbalancing optimized/reference order. Both profiles used the same runtime,
phase allowances, permissions, and total limits. These are injected regressions
in pinned repositories, not upstream issue resolutions.

| Metric | Optimized | Reference |
| --- | --- | --- |
| Passing trials | 0/6 | 1/6 |
| Completed, verified, constrained repairs | 0 | 1 |
| Completed repairs with complete usage | 0 | 1 |
| Mean tokens for measured successful work | unknown | 87,892 |
| All-trial tokens per completion, including failures | unknown | 333,220 |

Matched successful pairs with complete usage: **0**;
status `insufficient_repeated_samples`. Observed savings across those pairs:
not measurable.
With no matched successful pair, savings cannot be estimated. Unmatched successes, blocked
turns, unknown usage, and mixed-outcome means cannot establish dependable net
savings or general quality gains.

The optimized profile completed fewer trials than the earlier bounded batch
(1/6) and preceding full comparison (2/6). There is no demonstrated improvement
in larger-repository completion or token efficiency. Pinned aggregate acceptance was
**failed** under its unchanged 100% gate.

[All pinned outcomes](validation/phase-recovery/pinned.json) and
[successful-work comparison](validation/phase-recovery/comparison.json) include
case-level phase counters, review attempts, usage completeness, and constrained edits.

## Implementation probes and limits

The first live implementation probes exposed two boundary problems: optional
reviews charged the finishing reserve, and a targeted check could leave too
little rigid allowance for full verification. Those probes were interrupted
while the issues were fixed. Their [retained states](validation/phase-recovery/interrupted-implementation-probes.json)
are excluded from final acceptance and savings comparisons. Regression coverage
now tests both paths and prevents reused funds from being counted twice.

The preceding bounded phase batch passed 4/6 focused trials and 1/6 in each
pinned profile. It contained one matched successful pair, an insufficient sample
for dependable savings. Its source `4646193` and all outcomes remain in
`validation/phase-recovery/bounded-*.json`; neither earlier blocked trials nor
source changes are hidden by the final sample.

The preceding [full core validation](COMPLETION_VALIDATION_2026-10-04.md) remains
historical evidence on runtime `84ebde5`. This focused repeat does not recertify
Go/Rust or every provider. No paid provider was used, and no release was published.
Local token estimates are not provider billing guarantees. Oversized review
context blocks explicitly rather than approving truncated evidence.

Passing installation/unit checks alone does not establish production coding
reliability. Use these actual repeated outcomes and the unchanged completion
gates when deciding whether to invite alpha testers or release.
