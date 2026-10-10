# Apsara Agentic 0.1.0a1 — Private Alpha

Version: `0.1.0a1`

- Release date: 2026-08-10
- Python package: `apsara-agentic==0.1.0a1`
- Release tag: `v0.1.0a1`

## Overview

This is the first private-alpha release of Apsara Agentic, a local,
workspace-scoped coding assistant. Running `apsara` opens the full-screen
terminal interface immediately with Big Pickle selected by default; no Apsara
account or separate Apsara chat service is required.

The release is intended for trusted developer friends, technical early
adopters, and small internal testing groups. It is not a production-stable
release.

## Highlights

### Consistent boxed terminal interface

- full-screen transcript, right-side detail panel, and rounded input composer
- matching boxed user and assistant responses
- internal tool activity hidden by default and available with `Ctrl+B` or
  `/details`
- clean response presentation without redundant completion or build lines
- compact author timestamps and responsive terminal sizing
- each bare `apsara` launch starts a fresh conversation; saved sessions resume
  only when explicitly requested

### Strong coding-agent runtime

- structured baseline, targeted, and full project verification
- stale verification is invalidated after every subsequent workspace mutation
- independent tool-free critic review request for multi-file changes
- trusted lifecycle hooks at session, tool, verification, and completion
  boundaries
- disposable Git-worktree verification with external-symlink protection
- optional LSP-backed definitions and references for Python, TypeScript,
  JavaScript, Go, Rust, C, and C++
- pinned real-repository benchmark support alongside bundled offline fixtures
- bounded live benchmark trials with repeatable `results.json` and
  `summary.json` evidence
- bounded retries for empty provider responses, which otherwise fail the turn
  instead of appearing as successful completion
- per-request provider deadlines with one safe retry, preventing a stalled
  model call from consuming an entire turn

### Safer local execution

- workspace path boundaries for built-in file tools
- recoverable edit and turn checkpoints with undo support
- digest-based approval for plugins, MCP servers, verification commands, and
  lifecycle hooks
- command allowlists, nested-interpreter validation, and redirection checks
- `--read-only`, `--dry-run`, scoped `--auto-approve`, and process-group cleanup
- privacy-safe bug bundles that omit content by default and redact recognizable
  credentials

### Code intelligence and workflow

- repository maps, symbol search, definitions, references, and diagnostics
- Python AST support by default and optional Tree-sitter language support
- Git status, diff, log, show, blame, and checkpoint tools
- project memory, session history, context trimming, and usage reporting
- optional MCP servers and locally approved tool plugins
- model picker and BYO-provider-key setup inside the terminal

### Packaging and compatibility

- Python 3.10 through 3.14
- tested wheel installation and pipx lifecycle
- Linux and macOS test matrices plus Windows smoke coverage
- offline doctor diagnostics and optional semantic-intelligence extra

## Installation

```bash
pipx install https://github.com/RithyBondeth/Apsara-Agentic-Cli/releases/download/v0.1.0a1/apsara_agentic-0.1.0a1-py3-none-any.whl
apsara
```

For optional Tree-sitter parsers:

```bash
pipx inject apsara-agentic 'tree-sitter-language-pack>=1.0.0'
```

This private alpha is distributed as the wheel attached to its GitHub
prerelease. PyPI publication can follow separately without changing the tested
release artifact.

## Release validation

- full offline test suite across supported Python versions
- wheel and source-distribution metadata validation
- clean virtual-environment installation and dependency audit
- pipx install, upgrade, execution, and uninstall lifecycle on Linux, macOS,
  and Windows
- three live trials per bundled coding benchmark case with Big Pickle

The retained live benchmark evidence passed 15/15 trials (100%): zero flaky
trials, zero unstable cases, and zero unsafe edits. One layered trial exceeded
its soft token budget while still scoring 90/100 and passing every correctness
and safety gate.

- [Full benchmark results](https://github.com/RithyBondeth/Apsara-Agentic-Cli/releases/download/v0.1.0a1/apsara_agentic-0.1.0a1-benchmark-results.json)
- [Benchmark summary](https://github.com/RithyBondeth/Apsara-Agentic-Cli/releases/download/v0.1.0a1/apsara_agentic-0.1.0a1-benchmark-summary.json)

## Known alpha limitations

- live model quality and latency depend on OpenCode Zen availability, the
  tester's API key, billing, and rate limits
- OpenCode warns against sending confidential code to Big Pickle
- isolated verification protects ordinary workspace state but is not an
  operating-system security sandbox
- LSP tools require the corresponding language server to be installed
- bundled benchmarks are intentionally small; larger pinned-repository cases
  still need to be curated
- users should review proposed changes and keep Git backups

## Rollback

Remove the alpha package with:

```bash
pipx uninstall apsara-agentic
```

If upgrading from another build, reinstall the exact alpha version:

```bash
pipx install --force https://github.com/RithyBondeth/Apsara-Agentic-Cli/releases/download/v0.1.0a1/apsara_agentic-0.1.0a1-py3-none-any.whl
```

## Feedback areas

- first-run key setup and startup clarity
- boxed UI consistency and terminal resizing
- response cleanliness and detail-panel usefulness
- edit approval, verification, critic, and undo flows
- provider latency, retry behavior, and actionable errors
- benchmark tasks that pass tests but reduce maintainability

## Related documentation

- [Tester quickstart](https://github.com/RithyBondeth/Apsara-Agentic-Cli/blob/v0.1.0a1/TESTER_QUICKSTART.md)
- [Alpha testing guide](https://github.com/RithyBondeth/Apsara-Agentic-Cli/blob/v0.1.0a1/ALPHA_TESTING.md)
- [Run guide](https://github.com/RithyBondeth/Apsara-Agentic-Cli/blob/v0.1.0a1/RUN_PROJECT.md)
- [Agent runtime](https://github.com/RithyBondeth/Apsara-Agentic-Cli/blob/v0.1.0a1/docs/AGENT_RUNTIME.md)
- [Evaluation strategy](https://github.com/RithyBondeth/Apsara-Agentic-Cli/blob/v0.1.0a1/docs/EVALUATION_STRATEGY.md)
