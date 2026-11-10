"""`apsara trust` — review and revoke approvals for workspace-supplied code.

Approving a plugin, MCP server, verification command, or hook is a security
decision, so it needs to be inspectable and reversible without hand-editing
JSON.
"""
from pathlib import Path
from typing import Any

from apsara_cli.config import trust as trust_store
from apsara_cli.shared.ui import ConsoleUI, Theme


def _workspace_root(args: Any, config: Any) -> Path:
    hint = getattr(args, "workspace", None) or getattr(config.defaults, "workspace", None)
    if hint:
        return Path(hint).expanduser().resolve()
    return Path.cwd().resolve()


def _describe_key(key: str) -> tuple[str, str]:
    """Split a stored key into (kind, subject) for display."""
    if key.startswith("plugin:"):
        return "plugin", key[len("plugin:"):]
    if key.startswith("mcp:"):
        return "mcp", key[len("mcp:"):]
    if key.startswith("hooks:"):
        return "hooks", key[len("hooks:"):]
    if key.startswith("verification:"):
        return "verification", key[len("verification:"):]
    return "other", key


def trust_command(args: Any, config: Any) -> int:
    theme = Theme()
    config_theme = getattr(config, "theme", None)
    if config_theme is not None:
        config_theme.apply_to(theme)
    use_color = getattr(args, "color", None)
    ui = ConsoleUI(use_color=True if use_color is None else use_color, theme=theme)

    workspace = _workspace_root(args, config)

    if getattr(args, "reset", False):
        existing = trust_store.list_trusted(workspace)
        if not existing:
            ui.info(f"No approvals recorded for {workspace}.")
            return 0
        trust_store.forget_workspace(workspace)
        ui.success(
            f"Revoked {len(existing)} approval"
            f"{'s' if len(existing) != 1 else ''} for {workspace}."
        )
        ui.print_line()
        ui.print_line(
            f"  {ui.dim('Executable workspace definitions will ask for approval again.')}"
        )
        return 0

    entries = trust_store.list_trusted(workspace)

    ui.print_line()
    ui.print_line(
        f"  {ui.badge('trust', '15', '48;2;70;85;115')}  {ui.dim(str(workspace))}"
    )
    ui.print_line()

    if not entries:
        ui.info("No workspace code has been approved here.")
        ui.print_line()
        ui.print_line(
            f"  {ui.dim('Plugins, MCP servers, verification commands, and hooks need approval')}"
        )
        ui.print_line(
            f"  {ui.dim('before they run. You will be prompted the first time.')}"
        )
        ui.print_line()
        return 0

    for key, entry in sorted(entries.items()):
        kind, subject = _describe_key(key)
        approved_at = entry.get("approved_at", "unknown") if isinstance(entry, dict) else "unknown"
        digest = entry.get("sha256", "") if isinstance(entry, dict) else ""

        colours = {
            "mcp": "38;2;130;170;250",
            "hooks": "38;2;230;170;90",
            "verification": "38;2;180;150;250",
        }
        colour = colours.get(kind, "38;2;120;200;150")
        ui.print_line(
            f"  {ui.style('◆', colour)} {ui.style(subject, '1', '38;2;220;225;240')}  "
            f"{ui.style(kind, colour)}"
        )
        ui.print_line(
            f"      {ui.dim(f'approved {approved_at}')}  {ui.dim(digest[:12])}"
        )

    ui.print_line()
    ui.print_line(
        f"  {ui.dim('Revoke everything for this workspace:')} "
        f"{ui.style('apsara trust --reset', '1', '38;2;180;210;255')}"
    )
    ui.print_line()
    return 0
