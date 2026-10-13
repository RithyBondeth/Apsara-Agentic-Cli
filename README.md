<div align="center">

# Apsara Agentic CLI

**A local, bring-your-own-key coding agent for your terminal.**

![Python](https://img.shields.io/badge/Python-3776AB?style=for-the-badge&logo=python&logoColor=white)
![OpenAI](https://img.shields.io/badge/OpenAI-412991?style=for-the-badge&logo=openai&logoColor=white)
![MCP](https://img.shields.io/badge/MCP-000000?style=for-the-badge&logo=modelcontextprotocol&logoColor=white)
![Pytest](https://img.shields.io/badge/Pytest-0A9EDC?style=for-the-badge&logo=pytest&logoColor=white)

</div>

---

## Overview

Apsara Agentic is a terminal coding agent. Its persona is **Makor**, shown as a pixel-art Makara mascot. It reads and edits code in a workspace you choose, runs allowlisted commands, and connects to external tools over the Model Context Protocol (MCP). It uses your own model-provider API key, and there is **no Apsara server in the middle**.

> **Status: Alpha (`0.1.0a2`).** The CLI is usable day to day, but interfaces may still change between releases.

## Features

- **Full-screen TUI and classic mode** with a welcome screen, Markdown transcript, details sidebar, colored diffs, and an optional animated Makor mascot.
- **Workspace-confined file tools.** Reading, searching, symbol lookup, LSP navigation, Git inspection, and exact-match editing, all scoped to the workspace root.
- **Approval-gated edits.** Every file change shows a diff preview first and creates a recoverable checkpoint (`/undo`, `/undo-turn`, `/checkpoints`).
- **Verification engine.** Detects Pytest, npm scripts, Go tests, and Cargo tests, then requires a passing full check (plus an independent critic review for multi-file changes) before a turn counts as `completed_verified`.
- **Opt-in command execution** with allowlist presets (`@verify`, `@read`, `@git`) and per-stage parsing of pipes and `&&` chains.
- **MCP client** for stdio and streamable-HTTP servers, with approval prompts for tools that change external state.
- **Skills and context budgeting.** Built-in `debug`, `test`, and `review` skills, lazy tool discovery, and per-turn step, tool-call, and token budgets.
- **Trust model.** Digest-based approval for repository-supplied plugins, MCP definitions, verification commands, and lifecycle hooks.
- **Local sessions, usage, and pricing.** JSON sessions, provider-reported token accounting, directional list-price costs, and `/report` run exports.
- **Evaluation harness.** `apsara eval` scores recorded or live runs across Python, TypeScript, Go, and Rust fixtures.

## Tech Stack

| Area | Technology |
| --- | --- |
| Language | Python 3.10 – 3.14 |
| Model routing | LiteLLM, OpenAI SDK (OpenCode Zen, Ollama, and other providers) |
| Tool protocol | Model Context Protocol (`mcp`) |
| Terminal UI | Rich, prompt_toolkit, Pygments |
| Configuration | pydantic-settings, python-dotenv, TOML |
| Code intelligence | Python AST, optional Tree-sitter language pack |
| Testing | Pytest, GitHub Actions CI |

## Getting Started

### Prerequisites

- Python 3.10 – 3.14
- `git` on your `PATH`
- [pipx](https://pipx.pypa.io/)
- An API key from a model provider, or a local [Ollama](https://ollama.com/) install

### Installation

`0.1.0a2` is a source candidate. Build and install it from this repository:

```bash
git clone https://github.com/RithyBondeth/Apsara-Agentic-Cli.git
cd Apsara-Agentic-Cli
python3 -m pip install build
python3 -m build
pipx install ./dist/apsara_agentic-0.1.0a2-py3-none-any.whl
```

Optional: parser-accurate symbols and diagnostics for JavaScript, TypeScript, Go, Rust, Java, Ruby, PHP, C#, C++, and C:

```bash
pipx inject apsara-agentic 'tree-sitter-language-pack>=1.0.0'
```

### Quickstart

```bash
cd /path/to/your/project
apsara
```

On the first request Apsara asks for your provider key inline. You can keep it for the session only, or store it in `~/.apsara/credentials.json` with owner-only permissions. Run `apsara init` to create a project `.apsara/config.toml`.

## Usage

| Command | Description |
| --- | --- |
| `apsara` | Open the full-screen interface |
| `apsara --classic` | Use scrolling terminal chat |
| `apsara run "<instruction>"` | Run a one-shot instruction, then exit |
| `apsara init` | Set up `.apsara/` in the current project |
| `apsara sessions` | List saved sessions for a workspace |
| `apsara mcp` | List configured MCP servers and verify they connect |
| `apsara trust` | Review or revoke approvals for workspace-defined code |
| `apsara doctor [--live]` | Check environment, config, tools, credentials, and model access |
| `apsara eval suite.json` | Score recorded runs against regression expectations |

Useful flags: `--model`, `--workspace`, `--read-only`, `--dry-run`, `--continue`, `--session <name>`, `--allow-bash --allowed-commands @verify,git`.

In chat, `/help` lists every slash command, including `/model`, `/diff`, `/undo`, `/undo-turn`, `/usage`, `/memory`, `/report`, and `/bug`.

### Configuration

Settings resolve in this order: **CLI flags → `.apsara/config.toml` → `~/.apsara/config.toml` → defaults.**

```toml
[defaults]
model = "opencode/space-bunny-free"
allow_bash = false
allowed_commands = ["@verify", "@git"]

[mcp_servers.filesystem]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "."]

[verification]
commands = [["python", "-m", "pytest", "-q"]]
```

## Project Structure

```text
Apsara-Agentic-Cli/
├── src/apsara_cli/
│   ├── cli/          # Entry point, TUI, chat loop, auth, MCP and trust commands
│   ├── config/       # Config loading, defaults, trust store
│   ├── engine/       # Agent runtime, tools, LLM routing, verification, checkpoints, skills
│   └── shared/       # UI helpers, events, types, Makor mascot assets
├── evals/            # Coding evaluation suites and multi-language fixtures
├── docs/             # Runtime, code-intelligence, evaluation, and validation docs
├── tests/            # Pytest suite
└── pyproject.toml
```

## Documentation

- [Agent runtime](docs/AGENT_RUNTIME.md)
- [Skills and context management](docs/SKILLS_AND_CONTEXT.md)
- [Code intelligence](docs/CODE_INTELLIGENCE.md)
- [Evaluation strategy](docs/EVALUATION_STRATEGY.md)
- [Tester quickstart](TESTER_QUICKSTART.md) · [Running the project](RUN_PROJECT.md) · [Releasing](RELEASING.md)

## Development

```bash
python -m pip install -e ".[dev]"
python -m pytest
```

## Related Repositories

- [Apsara-Agentic-Web](https://github.com/RithyBondeth/Apsara-Agentic-Web): the public website and install guide for Apsara Agentic

## Author

**Rithy Bondeth** — Full Stack Developer & AI Engineer

[![Portfolio](https://img.shields.io/badge/Portfolio-0A0A0A?style=for-the-badge&logo=vercel&logoColor=white)](https://bondeth.dev)
[![GitHub](https://img.shields.io/badge/GitHub-181717?style=for-the-badge&logo=github&logoColor=white)](https://github.com/RithyBondeth)
[![LinkedIn](https://img.shields.io/badge/LinkedIn-0A66C2?style=for-the-badge&logo=linkedin&logoColor=white)](https://www.linkedin.com/in/rithy-bondeth-484073282/)

## License

Distributed under the MIT License. See [`LICENSE`](LICENSE) for details.
