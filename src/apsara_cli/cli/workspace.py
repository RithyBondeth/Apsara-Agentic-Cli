import argparse
import os
from pathlib import Path
from typing import Optional

from apsara_cli.cli.options import resolve_value, resolve_workspace
from apsara_cli.cli.session import get_sessions_dir, list_sessions
from apsara_cli.shared.ui import ConsoleUI, default_use_color

DEFAULT_CONFIG_TEMPLATE = """# Apsara Project Configuration
[defaults]
# workspace = "."
# model = "opencode/space-bunny-free"
# auto_approve = false

# Let the agent verify its own work. Without a test runner on the allowlist it
# can write code but never run it, so it cannot catch its own mistakes.
# @verify expands to the common test/build tools (pytest, npm, go, cargo,
# make, ...); @read and @git are also available. Every command still needs
# your explicit approval at run time; --auto-approve covers file mutations
# only.
# allow_bash = true
# allowed_commands = ["@verify", "@git"]
# bash_timeout = 120

[ui]
# welcome_title = "Apsara Agentic"

# ── MCP servers ──────────────────────────────────────────────────────────────
# Connect external tools through the Model Context Protocol. Each server is
# launched (stdio) or reached (http) on demand, and you approve it once before
# it runs. Check them any time with `apsara mcp`.
#
# Launched as a subprocess:
# [mcp_servers.filesystem]
# command = "npx"
# args = ["-y", "@modelcontextprotocol/server-filesystem", "."]
#
# Reached over HTTP — keep secrets in the environment, not in this file:
# [mcp_servers.internal]
# url = "https://mcp.example.com/v1"
# headers = { Authorization = "Bearer ${MCP_TOKEN}" }
"""

DEFAULT_INSTRUCTIONS_TEMPLATE = """# Team Coding Standards
- Use functional programming patterns where possible.
- Ensure all new functions have type hints.
- Use 'pytest' for testing.
- Follow PEP 8 style guidelines.
"""

async def init_workspace(args: argparse.Namespace, config: object) -> int:
    config_defaults = getattr(config, "defaults", None)
    use_color = bool(resolve_value(
        getattr(args, "color", None),
        getattr(config_defaults, "color", None),
        default_use_color(),
    ))
    ui = ConsoleUI(use_color=use_color, auto_approve=True)
    workspace_value = resolve_value(args.workspace, None, ".")
    workspace_root = resolve_workspace(str(workspace_value))
    
    ui.info(f"Initializing Apsara in {workspace_root}...")
    
    apsara_dir = workspace_root / ".apsara"
    apsara_dir.mkdir(parents=True, exist_ok=True)
    
    # Create config.toml
    config_file = apsara_dir / "config.toml"
    if not config_file.exists() or getattr(args, "force", False):
        config_file.write_text(DEFAULT_CONFIG_TEMPLATE, encoding="utf-8")
        ui.success(f"Created {config_file.relative_to(workspace_root)}")
    else:
        ui.info(f"Config already exists at {config_file.relative_to(workspace_root)}")

    # Create instructions.md
    inst_file = apsara_dir / "instructions.md"
    if not inst_file.exists():
        inst_file.write_text(DEFAULT_INSTRUCTIONS_TEMPLATE, encoding="utf-8")
        ui.success(f"Created {inst_file.relative_to(workspace_root)} (Edit this for team standards)")

    # Keep transient journals, snapshots, reports, and sessions out of Git.
    gitignore = workspace_root / ".gitignore"
    ignore_lines = [
        ".apsara/logs/", ".apsara/bugs/", ".apsara/runs/",
        ".apsara/checkpoints/", ".apsara/turns/", ".apsara/benchmarks/",
        ".apsara/reports/", ".apsara/tool-results/", ".apsara-cli/",
    ]
    if gitignore.exists():
        content = gitignore.read_text(encoding="utf-8")
        missing = [line for line in ignore_lines if line not in content.splitlines()]
        if missing:
            with gitignore.open("a", encoding="utf-8") as f:
                f.write("\n# Apsara artifacts\n" + "\n".join(missing) + "\n")
            ui.success("Updated .gitignore with Apsara entries.")
    else:
        gitignore.write_text("# Apsara artifacts\n" + "\n".join(ignore_lines) + "\n", encoding="utf-8")
        ui.success("Created .gitignore with Apsara entries.")

    if not getattr(args, "no_chat", False):
        from apsara_cli.cli.parser import dispatch_command
        # Reload config for the chat loop
        from apsara_cli.config.cli_config import load_cli_config
        new_config = load_cli_config(str(config_file), str(workspace_root))
        chat_args = argparse.Namespace(**{**vars(args), "command": "chat"})
        return await dispatch_command(chat_args, new_config)
    
    return 0

def print_sessions(args: argparse.Namespace, config: object) -> int:
    workspace_value = resolve_value(args.workspace, None, ".")
    workspace_root = resolve_workspace(str(workspace_value))
    sessions = list_sessions(workspace_root)
    if not sessions:
        print(f"No sessions found in {get_sessions_dir(workspace_root)}")
        return 0

    print(f"\nSessions in {workspace_root}:")
    for session_path in sessions:
        print(f"  - {session_path.stem}")
    return 0
