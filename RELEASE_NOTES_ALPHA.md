# Apsara Agentic 0.1.0a2 — Alpha candidate

Version: `0.1.0a2`

Status: prepared locally; no release tag, GitHub release, or PyPI publication.

## Changes

- Space Bunny Free is the default OpenCode model, with actionable access errors
  and paid fallback disabled for the retained free-model validation.
- The colorful terminal UI follows the OpenCode layout: centered welcome,
  transcript cards, anchored composer, and optional sidebar. `--classic`
  preserves scrolling terminal chat.
- Current verification and review evidence guides the agent toward a final
  answer when the requested work is finished. Later edits invalidate evidence.
- Deterministic file reads, passing full checks, and approved critic results
  can be reused within a turn when source and policy fingerprints match.
  Explicit fresh checks execute again; external tools and shell commands do too.
  Earlier exchanges compact sooner as the turn allowance shrinks, while
  required context remains protected. Approvals for another project do not
  invalidate this project's evidence.
- `/budget`, the sidebar, and durable reports expose model steps, requested
  tool calls, provider totals, estimates, and reused results. Local limits
  preserve edits and block honestly when more work cannot fit.
- Single fenced JSON critic verdicts are accepted; material findings still
  require changes and missing review still blocks verified completion. Final
  reviews receive the actual objective and current verification evidence;
  agent-suggested extra cases are not accepted as additional requirements.
  Model review may still make scope or correctness errors.

## Validation

Current live and artifact evidence is documented in
`docs/COMPLETION_VALIDATION_2026-10-04.md`. Only OpenCode's free model is included
in live provider testing. Other provider adapters have offline coverage and
remain uncertified for live coding; see `docs/PROVIDER_VALIDATION.md`.

529 local tests and all 18 runtime CI jobs passed. Final free-model trials
passed 12/15 core tasks, 2/6 optimized pinned-repository tasks, and
1/6 reference tasks. Repeated completion remains unstable, so production
quality gates are not met. Live saved-session recovery, undo, and a local
read-only MCP round-trip passed; these are limited integration samples.

## Install the candidate

```bash
python3 -m pip install build
python3 -m build
pipx install ./dist/apsara_agentic-0.1.0a2-py3-none-any.whl
apsara doctor --no-live
apsara
```

## Alpha limits

The bundled tasks and injected regressions are limited samples, not a guarantee
for arbitrary repositories. Review generated changes and the final verification
report. Free-model availability depends on the provider. Turn token limits use
conservative estimates and are not a provider billing cap. Executed project
commands run with the user's permissions; isolated verification is not an OS
sandbox. Live testing of paid providers is outside this candidate's scope.

The previous release notes and original validation claims are preserved in
`docs/releases/0.1.0a1.md` as historical evidence.
