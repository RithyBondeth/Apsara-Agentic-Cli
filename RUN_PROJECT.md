# Running Apsara Agentic from source

For installing the published package instead, see [README.md](README.md). This
guide is for working on the CLI itself.

## 1. Clone and enter the package

```bash
git clone https://github.com/RithyBondeth/Apsara-Agentic-Cli.git
cd Apsara-Agentic-Cli
```

## 2. Create a virtual environment

Python 3.10 through 3.14 is supported.

```bash
python3 -m venv .venv
source .venv/bin/activate
```

On Windows, activate with `.venv\Scripts\activate`.

## 3. Install in editable mode

```bash
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

This installs the runtime dependencies and the `apsara` command, pointed at your
working copy — edits take effect without reinstalling.

If you don't want the console script, the module form works too:

```bash
python -m apsara_cli.cli.parser --help
```

## 4. Start Apsara

```bash
cd /path/to/some/project
apsara
```

The full-screen interface opens with `opencode/space-bunny-free` selected. If its
key is missing, Apsara requests it inline on the first prompt. You can keep it
for the session or store it in `~/.apsara/credentials.json` (owner-only).
Each plain launch starts a fresh saved conversation. Use `apsara --continue` to
resume the latest one, or `apsara --session <name>` to open a named conversation.
Alternatively, export the provider key or put it in a workspace `.env`; an
explicitly exported variable always wins over stored credentials.
Use `apsara --classic` if you prefer scrolling terminal chat.

```env
OPENCODE_API_KEY=your_opencode_zen_api_key
AGENT_WORKSPACE_ROOT=.
AGENT_ENABLE_BASH_TOOL=false
AGENT_ALLOWED_COMMANDS=pwd,ls,find,rg,cat,sed,head,tail,wc
AGENT_MAX_FILE_SIZE_BYTES=1000000
```

Space Bunny Free passed Apsara's live tool-call probe. Its free availability is
temporary; select another supported model if the provider removes access.

## 5. Check the environment

```bash
apsara doctor
```

This validates the Python version, git and ripgrep availability, config
loading, workspace access, session storage, the tool list, MCP server status,
and whether credentials for the selected model are present. Add `--live` to make
a real model call as a final check (this spends tokens).

## 6. Optional project initialization

```bash
cd /path/to/some/project
apsara init      # sets up .apsara/ and opens chat
```

The bare command does not require initialization:

```bash
cd /path/to/some/project
apsara
```

The explicit compatibility form remains available for scripts:

```bash
apsara chat --workspace /path/to/some/project
apsara run "Summarize this codebase" --workspace /path/to/some/project
```

## 7. Run the tests

```bash
python -m pytest
```

The suite is fully offline — no network and no real model calls. MCP tests
launch a small in-process server over stdio, so they exercise the real
transport.

## 8. Try an MCP server

Add one to the project's `.apsara/config.toml`:

```toml
[mcp_servers.filesystem]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "."]
```

Then verify it connects:

```bash
apsara mcp
```

You'll be asked to approve the server the first time, since launching it runs
code that came from the project rather than from you.

## Troubleshooting

**`apsara: command not found`** — the virtual environment isn't active, or the
editable install didn't finish. Re-run step 3.

**`Python 3.10+ is required`** — check `python3 --version`. On macOS the system
`python3` is often 3.9; install a supported one (e.g. `brew install python@3.14`)
and create the venv with that interpreter.

**Old packaging tools break `pip install -e .`** —

```bash
python -m pip install --upgrade pip setuptools wheel
```

**A local plugin or MCP server won't load** — it needs approval. Run
`apsara` in that project once and approve it when prompted; the decision is
recorded in `~/.apsara/trust.json`.

## Related docs

- Project guide: [README.md](README.md)
- Release process: [RELEASING.md](RELEASING.md)
- Tester setup: [TESTER_QUICKSTART.md](TESTER_QUICKSTART.md)
- Alpha handoff: [ALPHA_TESTING.md](ALPHA_TESTING.md)
- CLI entry point: `src/apsara_cli/cli/parser.py`
