# Apsara by Bondeth Alpha Testing Guide

This guide is for early testers of the local CLI.

Use this when you want someone to:

- install the project locally
- confirm the CLI works end to end
- try real coding-assistant workflows
- report bugs and rough edges clearly

## 1. Tester Goal

The goal of alpha testing is not to prove everything is perfect.

The goal is to answer:

- Can a new tester install it successfully?
- Can they start a session without getting stuck?
- Does Apsara feel useful for real coding tasks?
- Do edit approvals and patch review feel safe and understandable?
- What breaks first?

## 2. Best Test Audience

Start with 3 to 10 trusted testers who:

- are comfortable using the terminal
- can create an OpenCode Zen API key
- regularly work in local codebases
- are willing to share screenshots and error messages

## 3. Prerequisites

Each tester should have:

- Python 3.10 through 3.14
- a local terminal
- an OpenCode Zen API key
- a repo or sample project they can safely test on

The development default is Space Bunny Free. Its free availability is
promotional; check provider access and data handling before sending private
code. The older published alpha default is no longer suitable for these trials.

Optional but recommended:

- `rg` installed for better search tool behavior
- a preferred editor set in `$EDITOR` or `$VISUAL`

## 4. Quick Start For Testers

1. Clone and enter the project:

```bash
git clone https://github.com/RithyBondeth/Apsara-Agentic-Cli.git
cd Apsara-Agentic-Cli
```

1. Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

1. Install the CLI and development dependencies:

```bash
python3 -m pip install --upgrade pip
python3 -m pip install -e ".[dev]"
```

1. Configure a provider:

```bash
apsara login
```

1. Run the health check:

```bash
apsara doctor --workspace .
```

1. Initialize the project and start the CLI:

```bash
apsara init --workspace .
```

## 5. First-Test Script

Ask testers to try these in order.

### Test A: Basic Prompt

Run:

```text
Describe this project.
```

Expected result:

- Apsara starts normally
- the loading state appears
- the final answer is readable and organized

### Test B: Workspace Understanding

Run:

```text
Find the main CLI file and summarize what it does.
```

Expected result:

- Apsara searches the workspace
- the answer mentions the correct file
- the answer is clean without too much internal noise

### Test C: Proposed Code Change

Run:

```text
Suggest one small improvement to the CLI UI and implement it.
```

Expected result:

- Apsara proposes a file change
- a diff preview appears before approval
- `v` shows a larger terminal diff
- `e` opens the patch in the tester's editor
- `Enter` approves or `n` rejects cleanly

### Test D: Fresh Session Safety

Run:

```text
/clear
```

Then ask a new prompt.

Expected result:

- the prior conversation is cleared for the active session
- the new response does not depend on old chat context

### Test E: Hidden Internal Activity

Run:

```text
/details
```

Expected result:

- hidden tool or planning activity is visible on demand
- the default chat remains cleaner than the detail view

## 6. What Testers Should Watch For

Please ask testers to report:

- setup friction
- confusing output
- poor formatting in long answers
- approval prompts that feel unclear
- incorrect file changes
- missing or broken diff previews
- editor preview not opening
- rate limit or billing errors
- slow responses
- anything that feels unsafe or surprising

## 7. Known Alpha Notes

Testers should know:

- this is an alpha CLI, not a final public release
- editable installation failures should include the Python and pip versions used
- live model access depends on the tester's own API billing and rate limits
- live-provider tests are intentionally limited to avoid spending tester API credits

## 8. Feedback Template

Ask testers to send feedback in this format:

```text
Name:
OS + terminal:
Python version:
How I launched it:
What I asked Apsara to do:
What worked well:
What felt confusing:
Any error message:
Did edit review feel safe:
Diagnostic bundle path (review files before sharing):
Would I use it again:
Screenshot or terminal paste:
```

## 9. Alpha Release Checklist For Bondeth

Before sharing with testers, confirm:

- `.env` loading works
- `apsara doctor --workspace .` works
- `apsara chat --workspace .` works with the default `opencode/space-bunny-free` model
- the welcome screen renders nicely
- the loading animation appears while the agent is working
- assistant responses are readable
- code edits show a diff preview
- `v` shows a fuller diff
- `e` opens the patch in `$EDITOR` or `$VISUAL`
- `Enter` and `n` work in every approval prompt; `a` applies only to workspace
  file mutations
- `/details` works
- `/clear` works
- `/budget` and the sidebar show current limits, with honest estimates when
  provider usage is incomplete
- three fresh trials per core task meet the completion, constrained-edit,
  tool, token, and stability gates before claiming release-candidate readiness
- `/bug` creates a metadata-only bundle and clearly asks the tester to review
  its files before sharing
- `/bug --include-content` requires confirmation and still redacts recognizable
  credentials

## 10. Recommended Share Message

You can send testers this:

```text
I’m testing an early version of Apsara by Bondeth, a local coding assistant CLI. I’d love your help trying the setup, asking it a few coding questions, and testing one safe code edit workflow. Please send me any rough edges, confusing UX moments, screenshots, or errors you hit.
```

## 11. Related Docs

- Alpha release notes: [RELEASE_NOTES_ALPHA.md](RELEASE_NOTES_ALPHA.md)
- Tester quickstart: [TESTER_QUICKSTART.md](TESTER_QUICKSTART.md)
- Run guide: [RUN_PROJECT.md](RUN_PROJECT.md)
- Main project README: [README.md](README.md)
- CLI entrypoint: `src/apsara_cli/cli/parser.py`
