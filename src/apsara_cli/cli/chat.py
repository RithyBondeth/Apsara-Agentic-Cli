from contextlib import asynccontextmanager
from getpass import getpass
from io import StringIO
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from apsara_cli.shared.types import ResolvedOptions
    from apsara_cli.shared.ui import ConsoleUI

from apsara_cli.shared.events import print_event
from apsara_cli.cli.history import (
    input_token_budget,
    model_context_window,
    trim_history_for_request,
    update_history_from_event,
)
from apsara_cli.cli.input import get_input_async
from apsara_cli.cli.options import resolve_runtime_options
from apsara_cli.cli.session import (
    get_session_path,
    list_sessions,
    load_session_messages,
    load_session_usage,
    sanitize_session_name,
    save_session_messages,
)
from apsara_cli.shared.text import summarize_history  # noqa: F401 (kept for potential external use)
from apsara_cli.shared.ui import ConsoleUI
from apsara_cli.shared.ui import Theme, DEFAULT_THEME
from apsara_cli.engine.tools import agent_runtime_context, get_agent_tools
from apsara_cli.engine.models import (
    MODELS,
    format_context_window,
    is_key_available,
    lookup_model,
    model_availability,
    model_lifecycle,
    model_price_label,
    providers_in_order,
    resolve_model_id,
)
from apsara_cli.cli.model_picker import pick_model


@asynccontextmanager
async def mcp_session(config: object, options: "ResolvedOptions", ui: "ConsoleUI"):
    """Connect the configured MCP servers for the life of a session.

    Servers are launched once per session rather than per turn, and each one is
    gated by the same workspace-trust prompt as local plugins: a config file
    that ships with a cloned repo must not silently start subprocesses.
    """
    from apsara_cli.engine.mcp_client import McpManager
    from apsara_cli.engine.tools import mcp_manager_context, request_workspace_trust
    from apsara_cli.config import trust as trust_store

    for message in getattr(config, "mcp_errors", []) or []:
        ui.warning(message)

    configured = list(getattr(config, "mcp_servers", []) or [])
    if not configured:
        yield None
        return

    approved = []
    with agent_runtime_context(
        workspace_root=options.workspace_root,
        trust_callback=ui.confirm_action,
    ):
        for server in configured:
            if not server.enabled:
                continue
            digest = trust_store.digest_text(server.trust_digest_source())
            if request_workspace_trust(
                f"mcp:{server.name}",
                digest,
                {
                    "kind": "mcp",
                    "server": server.name,
                    "display_path": server.describe(),
                    "command_preview": server.describe(),
                },
            ):
                approved.append(server)
            else:
                ui.warning(f"Skipped untrusted MCP server '{server.name}'.")

    if not approved:
        yield None
        return

    manager = McpManager(approved)
    try:
        statuses = await manager.connect()
        for status in statuses:
            if status.connected:
                ui.success(
                    f"MCP '{status.name}' connected — {status.tool_count} tool"
                    f"{'s' if status.tool_count != 1 else ''}."
                )
            else:
                ui.warning(f"MCP '{status.name}' unavailable: {status.error}")

        with mcp_manager_context(manager):
            yield manager
    finally:
        await manager.aclose()


def _switch_model(raw_name: str, current_model: str, options: "ResolvedOptions", ui: "ConsoleUI") -> str:
    """
    Resolve ``raw_name`` (a model id or alias) and switch to it, prompting
    for a missing API key when needed. Returns the resolved model id
    (unchanged from ``current_model`` if the switch didn't happen).
    """
    # Resolve alias → canonical model_id
    resolved = resolve_model_id(raw_name)
    entry = lookup_model(raw_name)

    if entry:
        selectable, health_message = model_availability(entry)
        if not selectable:
            ui.error(health_message)
            return current_model
        if health_message:
            ui.warning(health_message)
        ctx = format_context_window(entry.context_window)
        has_key = is_key_available(entry)
        ui.print_line()
        ui.print_line(
            f"  {ui.style('◆', '38;2;100;150;220')} "
            f"{ui.style(entry.display_name, '1', '38;2;220;225;240')}  "
            f"{ui.dim(entry.model_id)}  {ui.dim(ctx + ' ctx')}"
        )
        ui.print_line(f"  {ui.dim(model_price_label(entry.model_id))}")
        if entry.tier == "paid" and resolved != current_model:
            ui.warning(
                f"{entry.display_name} is a paid model. Requests may be billed by "
                f"{entry.provider.capitalize()} at its current rates."
            )
            ui.print_line(
                f"  {ui.badge('y  switch', '17', '48;2;80;170;140')}  "
                f"{ui.badge('n  cancel', '17', '48;2;120;100;80')}"
            )
            if ui.read_single_key() not in {"y", "Y"}:
                ui.info("Model switch cancelled — continuing with the current model.")
                return current_model
        if entry.tier == "local":
            ui.print_line(f"  {ui.style('✓ local model — no API key required', '38;2;120;200;150')}")
        elif has_key:
            ui.print_line(f"  {ui.style(f'✓ {entry.env_var} is set', '38;2;120;200;150')}")
        else:
            # ── Prompt for missing API key ─────────────────────────────
            ui.print_line(
                f"  {ui.style('✗', '38;2;220;120;100')} "
                f"{ui.style(entry.env_var, '1', '38;2;255;220;140')} "
                f"{ui.style('is not set', '38;2;220;120;100')}"
            )
            ui.print_line()
            ui.print_line(
                f"  {ui.style('?', '38;2;247;200;100')} "
                f"Enter your {ui.style(entry.env_var, '1', '38;2;255;220;140')} "
                f"{ui.dim('(hidden — press Enter to skip)')}"
            )
            try:
                raw_key = getpass("  → ")
            except (EOFError, KeyboardInterrupt):
                raw_key = ""

            if raw_key.strip():
                os.environ[entry.env_var] = raw_key.strip()
                ui.success(f"{entry.env_var} active for this session.")
                ui.print_line()
                ui.print_line(
                    f"  Save to .env?  "
                    f"{ui.badge('y  save', '17', '48;2;80;170;140')}  "
                    f"{ui.badge('n  session only', '17', '48;2;120;100;80')}"
                )
                save_choice = ui.read_single_key()
                if save_choice in {"y", "Y", "\r", "\n", ""}:
                    try:
                        saved_path = _save_api_key_to_env(
                            options.workspace_root, entry.env_var, raw_key.strip()
                        )
                        ui.success(f"Saved to {saved_path}")
                    except Exception as exc:
                        ui.error(f"Could not write .env: {exc}")
                else:
                    ui.info("Key active for this session only — not saved to disk.")
            else:
                ui.warning(
                    f"No key entered — switching anyway. "
                    f"Add {entry.env_var} to your .env to make it permanent."
                )
        ui.print_line()
    else:
        # Unknown models may be billable, so interactive switches require the
        # same explicit consent as known paid entries.
        ui.warning(
            f"'{raw_name}' is not in the built-in registry. Its pricing is unknown "
            "and the provider may bill requests."
        )
        ui.print_line(
            f"  {ui.badge('y  switch', '17', '48;2;80;170;140')}  "
            f"{ui.badge('n  cancel', '17', '48;2;120;100;80')}"
        )
        if resolved != current_model and ui.read_single_key() not in {"y", "Y"}:
            ui.info("Model switch cancelled — continuing with the current model.")
            return current_model

    if resolved != current_model:
        ui.info(f"Switched to {ui.style(resolved, '1', '38;2;188;218;255')}")
    return resolved


def build_status_line(options: "ResolvedOptions", current_model: str, session_label: str) -> str:
    """
    Mode · model · session · hint — the single source of truth for the
    bottom status line, shared by the classic REPL toolbar and the TUI
    status bar.
    """
    entry = lookup_model(current_model)
    model_name = entry.display_name if entry else current_model.split("/")[-1]
    if options.dry_run:
        mode = "dry-run"
    elif options.read_only:
        mode = "read-only"
    else:
        mode = "chat"
    return f"{mode} · {model_name} · {session_label}  —  /help commands · esc+enter newline"


def turn_mode_word(options: "ResolvedOptions") -> str:
    """The bold mode word shown in mode lines and turn footers."""
    if options.dry_run:
        return "Dry-run"
    if options.read_only:
        return "Read-only"
    return "Build"


def mode_line_parts(options: "ResolvedOptions", current_model: str) -> tuple[str, str, str]:
    """(mode, model display name, provider) for the input-box mode line."""
    entry = lookup_model(current_model)
    model_name = entry.display_name if entry else current_model.split("/")[-1]
    provider = (
        "OpenCode Zen" if entry and entry.provider == "opencode"
        else entry.provider.capitalize() if entry else ""
    )
    return turn_mode_word(options), model_name, provider


def build_mode_line(ui: "ConsoleUI", options: "ResolvedOptions", current_model: str) -> str:
    """
    Mode, model and provider inside the composer, with subdued metadata.
    """
    mode, model_name, provider = mode_line_parts(options, current_model)
    mode_color = {
        "Dry-run": "38;2;247;200;100",
        "Read-only": "38;2;240;170;90",
    }.get(mode, "38;2;96;150;250")

    parts = [
        ui.style(mode, "1", mode_color),
        ui.dim("·"),
        ui.style(model_name, "1", "38;2;225;230;242"),
    ]
    if provider:
        parts.append(ui.dim(provider))
    return " ".join(parts)


def build_scrolling_composer(
    ui: "ConsoleUI", options: "ResolvedOptions", current_model: str, columns: int
) -> tuple[str, str, str]:
    """Compact inline composer with Apsara's blue, violet and green accents."""
    from rich.text import Text

    width = max(1, min(columns - 4, 84))
    pad = "  " if columns >= 5 else ""
    border = ui.theme.border
    continuation = pad + ui.style("▌", ui.theme.accent) + " "
    prompt = "\n" + pad + ui.style("─" * width, border) + "\n" + continuation
    mode = Text.from_ansi(build_mode_line(ui, options, current_model))
    mode.truncate(max(1, width - 2), overflow="ellipsis")
    rendered = StringIO()
    from rich.console import Console
    Console(file=rendered, force_terminal=ui.use_color,
            color_system="truecolor" if ui.use_color else None, width=max(1, width - 2),
            height=1, legacy_windows=False).print(mode, end="", soft_wrap=True)
    footer = pad + ui.style("▌", "38;2;190;150;250") + " " + rendered.getvalue()
    return prompt, footer, continuation


def _load_stored_keys() -> None:
    """Load stored API keys from ~/.apsara/credentials.json into os.environ."""
    import os
    from apsara_cli.cli.auth import get_provider_key
    from apsara_cli.engine.models import providers_in_order as _providers, provider_env_var
    for provider in _providers():
        env_var = provider_env_var(provider)
        if env_var and not os.environ.get(env_var):
            stored = get_provider_key(provider)
            if stored:
                os.environ[env_var] = stored


def _save_api_key_to_env(workspace_root: Path, key_name: str, key_value: str) -> Path:
    """Write or update KEY=value in workspace_root/.env (creates the file if absent)."""
    import re as _re
    env_path = workspace_root / ".env"
    if env_path.exists():
        content = env_path.read_text(encoding="utf-8")
        pattern = _re.compile(rf"^{_re.escape(key_name)}\s*=.*$", _re.MULTILINE)
        if pattern.search(content):
            new_content = pattern.sub(f"{key_name}={key_value}", content)
        else:
            new_content = content.rstrip("\n") + f"\n{key_name}={key_value}\n"
    else:
        new_content = f"{key_name}={key_value}\n"
    env_path.write_text(new_content, encoding="utf-8")
    return env_path


_MODEL_TIER_COLOR = {
    "free":  ("38;2;120;200;150", "free"),
    "paid":  ("38;2;247;200;100", "paid"),
    "local": ("38;2;160;180;220", "local"),
}


def build_model_rows(
    current_model: str, filt: str, ui: "ConsoleUI"
) -> tuple[list[str], list[tuple[str, Optional[str]]], bool]:
    """
    Build the /models header and its selectable rows.

    Returns ``(header_parts, rows, shown_any)`` where ``rows`` is a list of
    ``(styled_line, model_id)`` tuples in display order — ``model_id`` is
    ``None`` for a non-selectable provider group header. Shared by the
    classic REPL's inline picker (chat.py) and the TUI's native in-app
    picker (tui.py) so both list exactly the same models.
    """
    providers = providers_in_order()

    header_parts = [ui.badge("models", "15", "48;2;70;85;115")]
    if filt:
        header_parts.append(ui.style(f"filtered: {filt}", "38;2;200;210;230"))
    else:
        total = len(MODELS)
        free_count = sum(1 for m in MODELS if m.tier in {"free", "local"})
        paid_count = sum(1 for m in MODELS if m.tier == "paid")
        header_parts.append(
            ui.style(f"{total} models  ·  {free_count} free/local  ·  {paid_count} paid", "38;2;200;210;230")
        )

    rows: list[tuple[str, Optional[str]]] = []
    shown_any = False
    for provider in providers:
        entries = [m for m in MODELS if m.provider == provider]
        if filt and not any(
            filt in m.model_id.lower() or filt in m.display_name.lower() or filt == m.provider
            for m in entries
        ):
            continue

        provider_rows: list[tuple[str, Optional[str]]] = []
        for entry in entries:
            if filt and filt not in entry.model_id.lower() and filt not in entry.display_name.lower() and filt != provider:
                continue
            shown_any = True

            is_current = entry.model_id == current_model
            has_key    = is_key_available(entry)
            ctx        = format_context_window(entry.context_window)
            tier_color, tier_label = _MODEL_TIER_COLOR.get(entry.tier, ("38;2;200;200;200", entry.tier))

            if is_current:
                status_icon = ui.style("●", "38;2;120;200;150")
            elif has_key or entry.tier == "local":
                status_icon = ui.style("○", "38;2;140;170;200")
            else:
                status_icon = ui.style("○", "38;2;120;100;90")

            name_style = ("1", "38;2;220;225;240") if is_current else ("38;2;190;200;220",)
            name_text  = ui.style(entry.display_name, *name_style)
            tier_badge = ui.style(f"[{tier_label}]", tier_color)
            ctx_text   = ui.dim(f"{ctx} ctx")
            lifecycle = model_lifecycle(entry)

            if has_key or entry.tier == "local":
                key_text = ui.style("✓ key set", "38;2;120;200;150")
            else:
                key_text = ui.style(f"✗ needs {entry.env_var}", "38;2;220;120;100")

            aliases_hint = ""
            if entry.aliases:
                aliases_hint = "  " + ui.dim("alias: " + ", ".join(entry.aliases[:3]))

            if entry.access_restriction:
                health_text = ui.style("[access restricted]", "38;2;235;110;100")
            elif lifecycle == "retired":
                health_text = ui.style("[retired]", "38;2;235;110;100")
            elif lifecycle in {"retiring", "deprecated"}:
                health_text = ui.style(f"[{lifecycle}]", "38;2;247;200;100")
            else:
                health_text = ""

            line = (
                f"{status_icon} {name_text}  {tier_badge}  "
                f"{health_text}  {ui.dim(model_price_label(entry.model_id))}  {ctx_text}  {key_text}  "
                f"{ui.dim(entry.model_id)}{aliases_hint}"
            )
            provider_rows.append((line, entry.model_id if model_availability(entry)[0] else None))

        if provider_rows:
            rows.append((ui.style(provider.upper(), "1", "38;2;190;200;220"), None))
            rows.extend(provider_rows)

    return header_parts, rows, shown_any


_HELP_SECTIONS: list[tuple[str, list[tuple[str, str, str]]]] = [
    ("Conversation", [
        ("/add", "<path>", "Pin a file's contents into the context"),
        ("/history", "", "Show recent conversation turns"),
        ("/details", "", "Reveal the agent's internal steps from the last turn"),
        ("/clear", "", "Clear the in-memory conversation history"),
    ]),
    ("Models & keys", [
        ("/model", "", "Show the current model"),
        ("/model", "<name>", "Switch model (full id or short alias)"),
        ("/models", "[provider]", "Browse all models with key status"),
        ("/key", "list", "Show provider API keys and where they come from"),
        ("/key", "set <provider>", "Add or update a provider API key (hidden input)"),
        ("/key", "remove <provider>", "Delete a stored provider key"),
    ]),
    ("Session", [
        ("/status", "", "Token usage, context health, session cost"),
        ("/usage", "", "Local token totals by model and saved session"),
        ("/budget", "", "Current turn usage and model/tool/token limits"),
        ("/save", "", "Save the current session now"),
        ("/session", "", "Show session and workspace details"),
        ("/sessions", "", "List all saved sessions"),
        ("/sessions", "clear [name]", "Delete all sessions, or one by name"),
    ]),
    ("Workspace", [
        ("/diff", "", "Show Git status plus staged and unstaged changes"),
        ("/turns", "", "List atomic checkpoints grouped by agent turn"),
        ("/undo-turn", "[turn-id]", "Roll back every captured change from one turn"),
        ("/checkpoints", "", "List automatic file snapshots"),
        ("/undo", "[checkpoint-id]", "Restore the latest or selected snapshot"),
        ("/memory", "show", "Show persistent project memory"),
        ("/memory", "add <note>", "Remember project context for future turns"),
        ("/report", "[path]", "Export the latest run as Markdown"),
        ("/processes", "", "List background commands"),
        ("/logs", "<process-id>", "Show recent background-process output"),
        ("/stop", "<process-id>", "Stop a background process"),
    ]),
    ("Diagnostics", [
        ("/tools", "", "Show enabled tools with descriptions"),
        ("/skills", "[name]", "List skills or preview a skill without a model call"),
        ("/bug", "[--include-content]", "Save a privacy-safe diagnostic bundle"),
        ("/exit", "", "Quit the chat session"),
    ]),
]


def print_chat_help(ui: "ConsoleUI") -> None:
    total = sum(len(cmds) for _, cmds in _HELP_SECTIONS)
    # Column width from the widest "command args" pair, so descriptions align.
    col_w = max(len(f"{c} {a}".strip()) for _, cmds in _HELP_SECTIONS for c, a, _ in cmds) + 3

    ui.print_line()
    ui.print_line(
        f"  {ui.badge('help', '15', '48;2;70;85;115')}  "
        f"{ui.style(f'{total} commands', '38;2;200;210;230')}"
    )
    for section, cmds in _HELP_SECTIONS:
        ui.print_line()
        ui.print_line(f"  {ui.style(section.upper(), '1', '38;2;190;200;220')}")
        for cmd, args, desc in cmds:
            plain = f"{cmd} {args}".strip()
            pad = " " * (col_w - len(plain))
            cmd_styled = ui.style(cmd, "1", "38;2;180;210;255")
            args_styled = f" {ui.style(args, '38;2;247;220;150')}" if args else ""
            ui.print_line(f"    {cmd_styled}{args_styled}{pad}{ui.dim(desc)}")
    ui.print_line()
    ui.print_line(f"  {ui.dim('Esc+Enter newline  ·  ↑/↓ input history  ·  Tab completes /commands')}")
    ui.print_line()


def handle_chat_command(
    command_text: str,
    history: list[dict[str, Any]],
    current_model: str,
    options: "ResolvedOptions",
    config: object,
    ui: "ConsoleUI",
) -> tuple[bool, str]:
    if command_text in {"/exit", "/quit"}:
        turns = sum(1 for m in history if m.get("role") == "user")
        if turns > 0 and options.stateless:
            ui.warning(f"Stateless session — {turns} turn(s) will not be saved.")
            ui.print_line(
                f"  {ui.badge('↵  exit', '17', '48;2;80;170;140')}  "
                f"{ui.badge('n  stay', '17', '48;2;200;100;80')}"
            )
            key = ui.read_single_key()
            if key not in {"y", "Y", "\r", "\n", ""}:
                ui.info("Staying in session.")
                return True, current_model
        return False, current_model

    if command_text == "/help":
        print_chat_help(ui)
        return True, current_model

    if command_text == "/details":
        ui.show_hidden_events()
        return True, current_model

    if command_text == "/skills" or command_text.startswith("/skills "):
        from apsara_cli.engine.skills import discover_skills, read_skill_file
        name = command_text[len("/skills"):].strip()
        skills = discover_skills(options.workspace_root)
        if not name:
            ui.info("Available skills — ask Apsara to use a named skill for your task.")
            ui.print_block("\n".join(f"{skill.name} [{skill.source}] — {skill.description}" for skill in skills)
                           or "No skills found.")
        else:
            skill = next((item for item in skills if item.name == name), None)
            if skill is None:
                ui.error(f"Skill '{name}' was not found. Use /skills to list available names.")
            else:
                try:
                    ui.print_block(read_skill_file(skill))
                except (OSError, ValueError, UnicodeError) as exc:
                    ui.error(f"Cannot read skill: {exc}")
        return True, current_model

    if command_text == "/diff":
        from apsara_cli.engine.workspace_diff import workspace_diff
        result = workspace_diff(options.workspace_root)
        if result.startswith("Error"):
            ui.error(result)
        else:
            ui.info("Git workspace changes")
            ui.print_block(result)
        return True, current_model

    if command_text == "/usage":
        from apsara_cli.engine.usage_reports import format_usage_report
        snapshot = ui.usage_snapshot() if hasattr(ui, "usage_snapshot") else {}
        ui.info("Usage summary")
        ui.print_block(format_usage_report(options.workspace_root, snapshot))
        return True, current_model

    if command_text == "/budget":
        from apsara_cli.engine.budget import TurnBudget
        from apsara_cli.engine.executor import _max_steps
        from apsara_cli.engine.runtime import latest_run
        latest = latest_run(options.workspace_root) or {}
        budget = ui._run_budget or latest.get("budget") or TurnBudget.from_environment(_max_steps()).as_dict()
        ui.print_block(
            f"Model steps: {budget['steps_used']}/{budget['step_limit']}\n"
            f"Tool calls: {budget['tool_calls_used']}/{budget['tool_call_limit']}\n"
            f"Provider-reported tokens: {budget['reported_usage']:,}\n"
            f"Reserved estimated usage: {budget['estimated_usage']:,}\n"
            f"Turn token limit: {budget['usage_limit']:,}\n"
            f"Complete provider usage: {budget['usage_complete']}\n"
            f"Reused results: {budget['reused_checks']}\n\n"
            "Set APSARA_MAX_STEPS, APSARA_MAX_TOOL_CALLS, and APSARA_MAX_TURN_TOKENS before launching.\n"
            "Token enforcement uses local request estimates; this is not a provider billing cap."
        )
        return True, current_model

    if command_text == "/checkpoints":
        from apsara_cli.engine.tools import list_workspace_checkpoints
        with agent_runtime_context(workspace_root=options.workspace_root):
            ui.info(list_workspace_checkpoints())
        return True, current_model

    if command_text == "/turns":
        from apsara_cli.engine.tools import list_turn_checkpoints_tool
        with agent_runtime_context(workspace_root=options.workspace_root):
            ui.info(list_turn_checkpoints_tool())
        return True, current_model

    if command_text == "/undo-turn" or command_text.startswith("/undo-turn "):
        from apsara_cli.engine.tools import undo_turn_checkpoint
        parts = command_text[len("/undo-turn"):].split()
        force = "--force" in parts
        identifiers = [part for part in parts if part != "--force"]
        if len(identifiers) > 1:
            ui.error("Usage: /undo-turn [id] [--force]")
            return True, current_model
        turn_id = identifiers[0] if identifiers else ""
        if not ui.confirm_action("undo_turn", {"turn_id": turn_id or "latest", "force": force}):
            ui.info("Turn rollback cancelled.")
            return True, current_model
        with agent_runtime_context(workspace_root=options.workspace_root, read_only=options.read_only):
            result = undo_turn_checkpoint(turn_id, force=force)
        (ui.error if result.startswith("Error:") else ui.success)(result)
        return True, current_model

    if command_text == "/undo" or command_text.startswith("/undo "):
        from apsara_cli.engine.tools import undo_last_checkpoint
        checkpoint_id = command_text[len("/undo"):].strip()
        if not ui.confirm_action("undo_checkpoint", {"checkpoint_id": checkpoint_id or "latest"}):
            ui.info("Undo cancelled.")
            return True, current_model
        with agent_runtime_context(workspace_root=options.workspace_root, read_only=options.read_only):
            result = undo_last_checkpoint(checkpoint_id)
        (ui.error if result.startswith("Error:") else ui.success)(result)
        return True, current_model

    if command_text == "/memory" or command_text == "/memory show":
        from apsara_cli.engine.memory import read_memory
        content = read_memory(options.workspace_root)
        ui.info(content or "No project memory recorded.")
        return True, current_model

    if command_text.startswith("/memory add "):
        note = command_text[len("/memory add "):].strip()
        if not note:
            ui.error("Usage: /memory add <note>")
            return True, current_model
        from apsara_cli.engine.memory import add_memory
        path = add_memory(options.workspace_root, note)
        ui.success(f"Saved project memory to {path}")
        return True, current_model

    if command_text == "/report" or command_text.startswith("/report "):
        from apsara_cli.engine.reports import export_latest_report
        raw_path = command_text[len("/report"):].strip()
        try:
            path = export_latest_report(options.workspace_root, Path(raw_path) if raw_path else None)
            ui.success(f"Exported run report to {path}")
        except (FileNotFoundError, ValueError) as exc:
            ui.error(str(exc))
        return True, current_model

    if command_text == "/processes" or command_text.startswith("/logs ") or command_text.startswith("/stop "):
        from apsara_cli.engine.tools import list_processes, process_output, stop_process
        with agent_runtime_context(workspace_root=options.workspace_root, read_only=options.read_only):
            if command_text == "/processes":
                result = list_processes()
            elif command_text.startswith("/logs "):
                result = process_output(command_text[len("/logs "):].strip())
            else:
                result = stop_process(command_text[len("/stop "):].strip())
        (ui.error if result.startswith("Error:") else ui.info)(result)
        return True, current_model

    if command_text == "/clear":
        history.clear()
        ui.latest_hidden_events = []
        ui.warning("Session cleared in memory")
        return True, current_model

    if command_text == "/bug" or command_text.startswith("/bug "):
        argument = command_text[len("/bug"):].strip()
        if argument not in {"", "--include-content"}:
            ui.error("Usage: /bug [--include-content]")
            return True, current_model
        include_content = argument == "--include-content"
        if include_content and not ui.confirm_action(
            "export_diagnostic_content",
            {"message_count": len(history)},
        ):
            ui.info("Diagnostic bundle cancelled.")
            return True, current_model
        ui.info("Collecting diagnostic information for bug report...")
        try:
            from apsara_cli.engine.diagnostics import create_diagnostic_bundle

            bug_dir = create_diagnostic_bundle(
                options.workspace_root,
                current_model,
                history,
                options,
                ui.log_file,
                include_content=include_content,
            )
            ui.success(f"Bug report data collected in: {bug_dir}")
            if include_content:
                ui.warning(
                    "Conversation and tool content is included. Review every file before sharing."
                )
            else:
                ui.info("Conversation and source content was omitted by default.")
            ui.info("Review the bundle files before sharing them with the development team.")
        except Exception as e:
            ui.error(f"Failed to collect bug report data: {e}")
        return True, current_model

    if command_text.startswith("/add "):
        path_str = command_text[len("/add "):].strip()
        if not path_str:
            ui.error("Usage: /add <path>")
            return True, current_model

        from apsara_cli.engine.tools import read_file, _resolve_path, _display_path
        with agent_runtime_context(workspace_root=options.workspace_root):
            try:
                # We use _resolve_path and read_file to respect workspace boundaries
                resolved = _resolve_path(path_str, must_exist=True)
                content = read_file(str(resolved))
                
                if content.startswith("Error"):
                    ui.error(content)
                else:
                    display_p = _display_path(resolved)
                    history.append({
                        "role": "user",
                        "content": f"Please focus on this file: {display_p}\n\nContents of {display_p}:\n```\n{content}\n```",
                    })
                    ui.success(f"Added {display_p} to conversation context.")
            except Exception as e:
                ui.error(f"Could not add file: {e}")
        return True, current_model

    if command_text == "/history":
        if not history:
            ui.info("No conversation history yet.")
            return True, current_model

        total_msgs = len(history)
        user_turns = sum(1 for m in history if m.get("role") == "user")
        turn_plural = "s" if user_turns != 1 else ""
        ui.print_line()
        ui.print_line(
            f"  {ui.badge('history', '15', '48;2;70;85;115')}  "
            f"{ui.style(f'{user_turns} turn{turn_plural}  ·  {total_msgs} messages', '38;2;200;210;230')}"
        )
        ui.print_line()

        turn_num = 0
        i = 0
        while i < len(history):
            msg = history[i]
            role = msg.get("role", "")

            if role == "user":
                turn_num += 1
                content = str(msg.get("content") or "").strip().replace("\n", " ")
                if len(content) > 74:
                    content = content[:71] + "…"
                ui.print_line(
                    f"  {ui.style(f'#{turn_num}', '1', '38;2;180;210;255')}"
                    f"  {ui.style('you', '2', '38;2;130;140;160')}"
                    f"  {ui.style(content, '38;2;230;228;224')}"
                )
                i += 1

                # Collect assistant messages and tool calls for this turn
                tool_call_count = 0
                while i < len(history) and history[i].get("role") != "user":
                    inner = history[i]
                    inner_role = inner.get("role", "")
                    if inner_role == "assistant":
                        tool_calls = inner.get("tool_calls") or []
                        tool_call_count += len(tool_calls)
                        reply = str(inner.get("content") or "").strip().replace("\n", " ")
                        if reply:
                            if len(reply) > 74:
                                reply = reply[:71] + "…"
                            ui.print_line(
                                f"    {ui.style('apsara', '2', '38;2;130;140;160')}"
                                f"  {ui.style(reply, '38;2;210;208;204')}"
                            )
                    i += 1

                if tool_call_count:
                    plural = "s" if tool_call_count != 1 else ""
                    ui.print_line(
                        f"    {ui.dim(f'↳ {tool_call_count} tool call{plural}')}"
                    )
            else:
                i += 1

        ui.print_line()
        return True, current_model

    if command_text == "/tools":
        with agent_runtime_context(
            workspace_root=options.workspace_root,
            enable_bash=options.allow_bash,
            allowed_commands=options.allowed_commands,
            max_file_size_bytes=options.max_file_size,
            bash_timeout_seconds=options.bash_timeout,
            dry_run=options.dry_run,
            read_only=options.read_only,
            trust_callback=ui.confirm_action,
        ):
            tools = get_agent_tools()
        ui.print_line()
        ui.print_line(
            f"  {ui.badge('tools', '15', '48;2;70;85;115')}  "
            f"{ui.style(f'{len(tools)} enabled', '1', '38;2;200;210;230')}"
        )
        ui.print_line()
        for tool in tools:
            fn = tool.get("function", {})
            name = fn.get("name", "unknown")
            desc = fn.get("description", "")
            ui.print_line(
                f"  {ui.style('◆', '38;2;100;150;220')} "
                f"{ui.style(name, '1', '38;2;180;210;255')}"
            )
            if desc:
                short_desc = desc[:86] + "…" if len(desc) > 86 else desc
                ui.print_line(f"    {ui.dim(short_desc)}")
        ui.print_line()
        return True, current_model

    if command_text == "/models" or command_text.startswith("/models "):
        # ── /models [provider-filter] ──────────────────────────────────────
        # NOTE: the full-screen TUI intercepts /models in tui.py before it
        # reaches here (it drives a native in-app picker instead), so this
        # branch runs for the classic REPL and non-TTY fallbacks only.
        filt = command_text[len("/models"):].strip().lower()
        header_parts, rows, shown_any = build_model_rows(current_model, filt, ui)

        if not shown_any:
            ui.print_line()
            ui.print_line(f"  {'  '.join(header_parts)}")
            ui.print_line()
            ui.warning(f"No models match '{filt}'. Try a provider name like 'openai', 'groq', 'anthropic'.")
            ui.print_line()
            return True, current_model

        # ── Interactive arrow-key picker (falls back to a plain listing
        # when a real terminal isn't available). Renders inline through the
        # caller's ConsoleUI — see model_picker.py — so it stays in the
        # current terminal UI exactly like every other command, instead of
        # opening a separate screen. ──────────────────────────────────────
        if sys.stdout.isatty():
            ui.print_line()
            ui.print_line(f"  {'  '.join(header_parts)}")
            ui.print_line()
            chosen = pick_model(rows, current_model, ui)
            if chosen is None:
                ui.info("No change — model selection cancelled.")
                return True, current_model
            resolved = _switch_model(chosen, current_model, options, ui)
            return True, resolved

        ui.print_line()
        ui.print_line(f"  {'  '.join(header_parts)}")
        ui.print_line()
        for line_text, model_id in rows:
            if model_id is None:
                ui.print_line(f"  {line_text}")
            else:
                ui.print_line(f"    {line_text}")
        ui.print_line()
        ui.print_line(f"  {ui.dim('Switch with /model <id-or-alias>  ·  /models <provider> to filter')}")
        ui.print_line()
        return True, current_model

    if command_text == "/model":
        entry = lookup_model(current_model)
        if entry:
            ctx   = format_context_window(entry.context_window)
            has_k = is_key_available(entry)
            key_s = ui.style("✓ key set", "38;2;120;200;150") if (has_k or entry.tier == "local") else ui.style(f"✗ needs {entry.env_var}", "38;2;220;120;100")
            ui.info(
                f"Current model: {ui.style(entry.display_name, '1', '38;2;220;225;240')}  "
                f"{ui.dim(entry.model_id)}  {ui.dim(ctx + ' ctx')}  {key_s}"
            )
        else:
            ui.info(f"Current model: {current_model}")
        return True, current_model

    if command_text.startswith("/model "):
        raw_name = command_text[len("/model "):].strip()
        if not raw_name:
            ui.error("Usage: /model <model-id-or-alias>")
            return True, current_model

        resolved = _switch_model(raw_name, current_model, options, ui)
        return True, resolved

    if command_text == "/session":
        ui.info(f"Workspace: {options.workspace_root}")
        if options.stateless:
            ui.info("Session mode: stateless")
        else:
            ui.info(f"Session: {sanitize_session_name(options.session)}")
        config_path = getattr(config, "path", None)
        config_exists = getattr(config, "exists", False)
        ui.info(f"Config: {config_path} ({'loaded' if config_exists else 'default values'})")
        return True, current_model

    if command_text == "/save":
        save_if_needed(history, current_model, options, ui)
        return True, current_model

    if command_text == "/sessions" or command_text.startswith("/sessions "):
        sub = command_text[len("/sessions"):].strip()  # "", "clear", or "clear <name>"

        if not sub:
            # ── List all sessions ──────────────────────────────────────────
            sessions = list_sessions(options.workspace_root)
            if not sessions:
                ui.info("No saved sessions found.")
                return True, current_model

            current_name = (
                sanitize_session_name(options.session) if not options.stateless else None
            )
            ui.print_line()
            ui.print_line(
                f"  {ui.badge('sessions', '15', '48;2;70;85;115')}  "
                f"{ui.style(f'{len(sessions)} saved', '38;2;200;210;230')}"
            )
            ui.print_line()
            for path in sessions:
                name = path.stem
                is_current = name == current_name
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    msg_count = len(payload.get("messages", []))
                    updated_at = payload.get("updated_at", "")[:19].replace("T", " ")
                    model_name = payload.get("model", "?")
                except Exception:
                    msg_count, updated_at, model_name = 0, "?", "?"
                size_kb = path.stat().st_size / 1024
                current_marker = ui.style("  ← active", "38;2;120;200;150") if is_current else ""
                ui.print_line(
                    f"  {ui.style('◆', '38;2;100;150;220')} "
                    f"{ui.style(name, '1', '38;2;180;210;255')}"
                    f"{current_marker}"
                )
                ui.print_line(
                    f"    {ui.dim(f'{msg_count} messages  ·  {updated_at}  ·  {size_kb:.1f} kb  ·  {model_name}')}"
                )
            ui.print_line()
            ui.print_line(f"  {ui.dim('  /sessions clear         delete all sessions')}")
            ui.print_line(f"  {ui.dim('  /sessions clear <name>  delete a specific session')}")
            ui.print_line()
            return True, current_model

        if sub == "clear":
            # ── Delete all sessions ────────────────────────────────────────
            sessions = list_sessions(options.workspace_root)
            if not sessions:
                ui.info("No saved sessions to clear.")
                return True, current_model

            ui.warning(f"This will permanently delete {len(sessions)} session file(s).")
            ui.print_line(
                f"  {ui.badge('↵  confirm', '17', '48;2;80;170;140')}  "
                f"{ui.badge('n  cancel', '17', '48;2;200;100;80')}"
            )
            key = ui.read_single_key()
            if key not in {"y", "Y", "\r", "\n", ""}:
                ui.info("Cancelled.")
                return True, current_model

            deleted = 0
            for path in sessions:
                try:
                    path.unlink()
                    deleted += 1
                except Exception as exc:
                    ui.error(f"Could not delete {path.name}: {exc}")
            ui.success(f"Deleted {deleted} session file(s).")
            return True, current_model

        if sub.startswith("clear "):
            # ── Delete one session by name ─────────────────────────────────
            target_name = sub[len("clear "):].strip()
            if not target_name:
                ui.error("Usage: /sessions clear <name>")
                return True, current_model

            target_path = get_session_path(options.workspace_root, target_name)
            if not target_path.exists():
                ui.error(f"Session '{target_name}' not found.")
                return True, current_model

            ui.warning(f"Delete session '{target_name}'?")
            ui.print_line(
                f"  {ui.badge('↵  confirm', '17', '48;2;80;170;140')}  "
                f"{ui.badge('n  cancel', '17', '48;2;200;100;80')}"
            )
            key = ui.read_single_key()
            if key not in {"y", "Y", "\r", "\n", ""}:
                ui.info("Cancelled.")
                return True, current_model

            try:
                target_path.unlink()
                ui.success(f"Session '{target_name}' deleted.")
            except Exception as exc:
                ui.error(f"Could not delete session: {exc}")
            return True, current_model

        ui.error("Usage: /sessions  |  /sessions clear  |  /sessions clear <name>")
        return True, current_model

    if command_text == "/status":
        from apsara_cli.engine.executor import SYSTEM_PROMPT
        from apsara_cli.engine.llm import estimate_request_tokens

        base = [{"role": "system", "content": SYSTEM_PROMPT}]
        tokens = estimate_request_tokens(base + history, model=current_model)
        budget = input_token_budget(current_model)
        window = model_context_window(current_model)
        pct = int(tokens / budget * 100)
        turns = sum(1 for m in history if m.get("role") == "user")
        msgs = len(history)
        session_label = (
            sanitize_session_name(options.session) if not options.stateless else "stateless"
        )

        if pct < 70:
            health_color = "38;2;120;200;150"
            health_label = "good"
        elif pct < 90:
            health_color = "38;2;247;223;181"
            health_label = "warn"
        else:
            health_color = "38;2;255;168;168"
            health_label = "critical"

        ui.print_line()
        ui.print_line(
            f"  {ui.badge('status', '15', '48;2;70;85;115')}  "
            f"{ui.style('Session Context', '1', '38;2;200;210;230')}"
        )
        ui.print_line()
        
        # Governance flags
        if options.dry_run or options.read_only:
            flags = []
            if options.dry_run: flags.append(ui.style("DRY-RUN", "1", "38;2;247;200;100"))
            if options.read_only: flags.append(ui.style("READ-ONLY", "1", "38;2;220;120;100"))
            ui.print_line(f"  {ui.dim('  active   ')} {' '.join(flags)}")

        ui.print_line(f"  {ui.dim('  model    ')} {ui.style(current_model, '38;2;188;218;255')}")
        ui.print_line(f"  {ui.dim('  session  ')} {ui.style(session_label, '38;2;220;216;210')}")
        ui.print_line(f"  {ui.dim('  turns    ')} {ui.style(str(turns), '38;2;220;216;210')}")
        ui.print_line(f"  {ui.dim('  messages ')} {ui.style(str(msgs), '38;2;220;216;210')}")
        window_note = f"  of {window:,} window" if window else "  (window unknown)"
        ui.print_line(
            f"  {ui.dim('  tokens   ')} "
            f"{ui.style(f'{tokens:,}', health_color)} "
            f"{ui.dim(f'/ {budget:,} budget  ({pct}%  {health_label}){window_note}')}"
        )
        ui.print_line(
            f"  {ui.dim('  cost     ')} "
            f"{ui.style(ui.session_cost_label(), '38;2;120;200;150')} "
            f"{ui.dim('(local estimate; provider dashboard is authoritative)')}"
        )
        ui.print_line(
            f"  {ui.dim('  usage    ')} "
            f"{ui.style(f'in {ui._session_prompt_tokens:,} · out {ui._session_completion_tokens:,}', '38;2;220;216;210')}"
        )
        if ui._session_estimated_tokens:
            ui.print_line(
                f"  {ui.dim('  estimated')} "
                f"{ui.style(f'~{ui._session_estimated_tokens:,} input tokens · {ui._session_unreported_calls} unreported call(s)', '38;2;247;200;100')}"
            )
        if ui._session_cached_tokens or ui._session_cache_creation_tokens or ui._session_reasoning_tokens:
            ui.print_line(
                f"  {ui.dim('  details  ')} "
                f"{ui.style(f'cached {ui._session_cached_tokens:,} · cache write {ui._session_cache_creation_tokens:,} · reasoning {ui._session_reasoning_tokens:,}', '38;2;220;216;210')}"
            )
        if ui.rate_limit_label():
            ui.print_line(f"  {ui.dim('  limits   ')} {ui.dim(ui.rate_limit_label())}")
        ui.print_line()
        return True, current_model

    if command_text == "/key" or command_text.startswith("/key "):
        from apsara_cli.cli.auth import (
            get_active_provider,
            get_provider_key,
            remove_provider_key,
            save_provider_key,
            stored_providers,
        )
        from apsara_cli.engine.models import (
            KEY_HINTS,
            default_model_for_provider,
            provider_env_var,
            validate_key_format,
        )

        sub = command_text[len("/key"):].strip()
        keyed_providers = [p for p in providers_in_order() if provider_env_var(p)]

        if sub in {"", "list"}:
            active = get_active_provider()
            ui.print_line()
            ui.print_line(
                f"  {ui.badge('keys', '15', '48;2;70;85;115')}  "
                f"{ui.style('Provider API keys', '38;2;200;210;230')}"
            )
            ui.print_line()
            for provider in keyed_providers:
                env_var = provider_env_var(provider)
                in_store = get_provider_key(provider) is not None
                in_env = bool(os.environ.get(env_var))
                if in_store:
                    icon, source = ui.style("●", "38;2;120;200;150"), "stored in ~/.apsara"
                elif in_env:
                    icon, source = ui.style("●", "38;2;140;190;240"), f"from env {env_var}"
                else:
                    icon, source = ui.style("○", "38;2;120;100;90"), "not set"
                active_marker = ui.style("  ← active", "38;2;120;200;150") if provider == active else ""
                name = ui.style(provider.ljust(11), "1", "38;2;220;225;240")
                ui.print_line(f"    {icon} {name}{ui.dim(source)}{active_marker}")
            ui.print_line()
            ui.print_line(f"  {ui.dim('/key set <provider> to add  ·  /key remove <provider> to delete')}")
            ui.print_line()
            return True, current_model

        if sub.startswith("set"):
            provider = sub[len("set"):].strip().lower()
            if provider not in keyed_providers:
                ui.error(f"Usage: /key set <provider>  —  one of: {', '.join(keyed_providers)}")
                return True, current_model

            env_var = provider_env_var(provider)
            hint = KEY_HINTS.get(env_var)
            ui.print_line()
            ui.print_line(
                f"  {ui.style('?', '38;2;247;200;100')} "
                f"Enter your {ui.style(provider, '1', '38;2;220;225;240')} API key "
                f"{ui.dim('(hidden — Enter to cancel)')}"
            )
            if hint and hint[1]:
                ui.print_line(f"    {ui.dim(hint[1])}")
            try:
                raw_key = getpass("  → ").strip()
            except (EOFError, KeyboardInterrupt):
                raw_key = ""
            if not raw_key:
                ui.info("Cancelled — no key saved.")
                return True, current_model

            looks_valid, message = validate_key_format(env_var, raw_key)
            if not looks_valid:
                ui.warning(f"That doesn't match the usual format. {message}")
                ui.print_line(
                    f"  {ui.badge('↵  save anyway', '17', '48;2;80;170;140')}  "
                    f"{ui.badge('n  cancel', '17', '48;2;200;100;80')}"
                )
                if ui.read_single_key() not in {"y", "Y", "\r", "\n", ""}:
                    ui.info("Cancelled — no key saved.")
                    return True, current_model

            save_provider_key(provider, api_key=raw_key, default_model=default_model_for_provider(provider))
            os.environ[env_var] = raw_key
            ui.success(f"Saved {provider} key to ~/.apsara/credentials.json — active now.")
            default_model = default_model_for_provider(provider)
            if default_model and default_model != current_model:
                ui.print_line(f"  {ui.dim(f'Try /model {default_model} to switch, or /models {provider} to browse.')}")
            return True, current_model

        if sub.startswith("remove"):
            provider = sub[len("remove"):].strip().lower()
            if not provider:
                stored = stored_providers()
                hint = f"stored: {', '.join(stored)}" if stored else "no keys stored"
                ui.error(f"Usage: /key remove <provider>  —  {hint}")
                return True, current_model
            if remove_provider_key(provider):
                env_var = provider_env_var(provider)
                if env_var:
                    os.environ.pop(env_var, None)
                ui.success(f"Removed stored {provider} key.")
            else:
                ui.warning(f"No stored key for '{provider}'. /key list to see what's saved.")
            return True, current_model

        ui.error("Usage: /key list  |  /key set <provider>  |  /key remove <provider>")
        return True, current_model

    # Unknown command — suggest the closest match instead of a bare error.
    import difflib
    known = [c for c, _, _ in (cmd for _, cmds in _HELP_SECTIONS for cmd in cmds)]
    word = command_text.split()[0]
    matches = difflib.get_close_matches(word, set(known), n=1, cutoff=0.5)
    suggestion = f" Did you mean {matches[0]}?" if matches else ""
    ui.error(f"Unknown command '{word}'.{suggestion} Type /help for the full list.")
    return True, current_model


async def execute_instruction(
    instruction: str,
    model: str,
    history: list[dict[str, Any]],
    options: "ResolvedOptions",
    ui: "ConsoleUI",
) -> tuple[list[dict[str, Any]], Optional[dict[str, Any]]]:
    from apsara_cli.engine.executor import run_agent_stream
    from apsara_cli.engine.llm import DEFAULT_MAX_COMPLETION_TOKENS

    entry = lookup_model(model)
    if entry:
        selectable, reason = model_availability(entry)
        if not selectable:
            ui.error(reason)
            ui.last_run_state = "failed"
            return list(history), None

    next_history = list(history)
    next_history.append({"role": "user", "content": instruction})
    aggregate_usage: Optional[dict[str, Any]] = None
    turn_checkpoint_id: Optional[str] = None
    ui.begin_turn()
    if entry and entry.tier == "paid":
        ui.info(f"Model: {model} · {model_price_label(model)} · billed by {entry.provider}.")

    def merge_usage(data: dict[str, Any]) -> None:
        nonlocal aggregate_usage
        from apsara_cli.engine.usage import add_usage, normalize_usage

        if aggregate_usage is None:
            aggregate_usage = {"model_usage": {}}
        add_usage(aggregate_usage, data)
        usage_model = str(data.get("apsara_model") or model)
        by_model = aggregate_usage["model_usage"].setdefault(usage_model, {})
        add_usage(by_model, normalize_usage(data))

    with agent_runtime_context(
        workspace_root=options.workspace_root,
        enable_bash=options.allow_bash,
        allowed_commands=options.allowed_commands,
        max_file_size_bytes=options.max_file_size,
        bash_timeout_seconds=options.bash_timeout,
        # The UI applies --auto-approve only to reversible workspace writes.
        # Commands and external mutations still require explicit approval.
        confirmation_callback=ui.confirm_action,
        dry_run=options.dry_run,
        read_only=options.read_only,
        # Always ask, even under --auto-approve: that flag waives confirmation
        # for file writes, not for executing code shipped with the project.
        trust_callback=ui.confirm_action,
        model=model,
    ):
        trim_result = await trim_history_for_request(next_history, model=model)
        if trim_result.auxiliary_usage:
            merge_usage(trim_result.auxiliary_usage)
        ui.set_context_usage(trim_result.trimmed_tokens, input_token_budget(model))
        if trim_result.dropped_turns:
            ui.warning(
                f"Trimmed {trim_result.dropped_turns} earlier turn(s) "
                f"({trim_result.dropped_messages} messages) to stay within the request budget."
            )
            if trim_result.summary:
                ui.info(f"Generated summary of earlier conversation: {ui.dim(trim_result.summary[:100] + '...')}")
            
            ui.info(
                f"Estimated input tokens: {trim_result.original_tokens} -> {trim_result.trimmed_tokens}. "
                f"Response budget capped at about {DEFAULT_MAX_COMPLETION_TOKENS} tokens."
            )
            # Use the trimmed history (including summary) for the rest of this turn
            next_history = list(trim_result.request_history)

        try:
            async for chunk_str in run_agent_stream(next_history, model=model):
                event = json.loads(chunk_str)
                if event.get("run_id"):
                    turn_checkpoint_id = str(event["run_id"])
                if event.get("type") == "usage":
                    merge_usage(event.get("data") or {})
                else:
                    print_event(event, ui)
                    update_history_from_event(next_history, event)
        except asyncio.CancelledError:
            # Completed calls may already have provider totals; retain them,
            # then record the in-flight request separately as an estimate.
            if aggregate_usage:
                ui.usage(aggregate_usage)
            ui.record_interrupted_usage(trim_result.trimmed_tokens, model)
            from apsara_cli.cli.history import recover_interrupted_history
            history[:] = recover_interrupted_history(next_history)
            save_if_needed(history, model, options, ui)
            raise

    if turn_checkpoint_id:
        try:
            from apsara_cli.engine.turn_checkpoints import list_turn_checkpoints
            manifest = next(
                (item for item in list_turn_checkpoints(options.workspace_root)
                 if item.get("id") == turn_checkpoint_id),
                None,
            )
            if manifest and manifest.get("changes"):
                ui.info(f"Turn checkpoint {turn_checkpoint_id} · rollback with /undo-turn {turn_checkpoint_id}")
                ui.print_block("\n".join(
                    f"{change.get('action', 'changed'):>8}  {change.get('path', '')}"
                    for change in manifest["changes"]
                ))
        except (OSError, ValueError):
            pass

    from apsara_cli.engine.executor import SYSTEM_PROMPT
    from apsara_cli.engine.llm import estimate_request_tokens

    current_tokens = estimate_request_tokens(
        [{"role": "system", "content": SYSTEM_PROMPT}] + next_history,
        model=model,
    )
    ui.set_context_usage(current_tokens, input_token_budget(model))
    entry = lookup_model(model)
    ui.finish_turn(
        model_label=entry.display_name if entry else model.split("/")[-1],
        mode=turn_mode_word(options),
    )
    return next_history, aggregate_usage


def save_if_needed(
    history: list[dict[str, Any]],
    model: str,
    options: "ResolvedOptions",
    ui: "ConsoleUI",
) -> None:
    if options.stateless:
        return
    session_path = save_session_messages(
        workspace_root=options.workspace_root,
        session_name=options.session,
        model=model,
        messages=history,
        usage=ui.usage_snapshot() if hasattr(ui, "usage_snapshot") else {},
    )
    ui.session_saved(session_path)


async def run_once(args: object, config: object) -> int:
    options = resolve_runtime_options(args, config.defaults)

    # Load stored API keys into environment so litellm can use them
    _load_stored_keys()

    # Build theme from config overrides
    theme = Theme()
    config_theme = getattr(config, "theme", None)
    if config_theme is not None:
        config_theme.apply_to(theme)

    ui = ConsoleUI(use_color=options.use_color, auto_approve=options.auto_approve, theme=theme)
    history: list[dict[str, Any]] = []

    if not options.stateless:
        history = load_session_messages(options.workspace_root, options.session)
        from apsara_cli.cli.history import recover_interrupted_history
        history = recover_interrupted_history(history)
        ui.restore_usage(load_session_usage(options.workspace_root, options.session))

    async with mcp_session(config, options, ui):
        updated_history, latest_usage = await execute_instruction(
            instruction=args.instruction,
            model=options.model,
            history=history,
            options=options,
            ui=ui,
        )

    if latest_usage and latest_usage.get("total_tokens") is not None:
        ui.usage(latest_usage)
    save_if_needed(updated_history, options.model, options, ui)

    state = getattr(ui, "last_run_state", "completed")
    return 0 if state in {"completed", "completed_verified"} else 2 if state == "completed_unverified" else 1


async def chat_loop(args: object, config: object) -> int:
    from apsara_cli.cli.banner import print_welcome_banner

    options = resolve_runtime_options(args, config.defaults)

    # Load stored API keys into environment so litellm can use them
    _load_stored_keys()

    # Build theme from config overrides
    theme = Theme()
    config_theme = getattr(config, "theme", None)
    if config_theme is not None:
        config_theme.apply_to(theme)

    ui = ConsoleUI(use_color=options.use_color, auto_approve=options.auto_approve, theme=theme)
    history: list[dict[str, Any]] = []
    current_model = options.model
    turn_count = 0

    if not options.stateless:
        history = load_session_messages(options.workspace_root, options.session)
        from apsara_cli.cli.history import recover_interrupted_history
        history = recover_interrupted_history(history)
        ui.restore_usage(load_session_usage(options.workspace_root, options.session))

    print_welcome_banner(ui, config)

    # ── OpenCode-style welcome chrome: hints, tip, footer ─────────────────
    from apsara_cli.cli.auth import get_active_provider
    from apsara_cli.shared.ui import terminal_width
    from apsara_cli import __version__

    _terminal = max(12, min(terminal_width(), 112))

    def _center_pad(plain_len: int) -> str:
        return " " * max((_terminal - plain_len) // 2, 2)

    _active_provider = get_active_provider()
    _model_entry = lookup_model(current_model)
    _key_ok = _model_entry is not None and (
        is_key_available(_model_entry) or _model_entry.tier == "local"
    )

    # Keyboard hints, 'tab agents  ctrl+p commands' style.
    _hints = [("/", "commands"), ("esc+enter", "newline"), ("↑↓", "history")]
    if _terminal < 64:
        _hints = [("/", "commands"), ("↑↓", "history")]
    _hints_plain = "   ".join(f"{key} {label}" for key, label in _hints)
    _hints_styled = "   ".join(
        f"{ui.style(key, '1', '38;2;140;180;255')} {ui.dim(label)}" for key, label in _hints
    )
    ui.print_line(_center_pad(len(_hints_plain)) + _hints_styled)

    if history:
        prior_turns = sum(1 for m in history if m.get("role") == "user")
        plural = "s" if prior_turns != 1 else ""
        _resumed = f"resumed {prior_turns} prior turn{plural}"
        ui.print_line()
        ui.print_line(_center_pad(len(_resumed)) + ui.dim(_resumed))
        turn_count = prior_turns

    # Tip line: '● Tip Run /key set … to add an AI provider and start coding'.
    _needs_setup = not _active_provider and _model_entry is not None and not _key_ok
    if _needs_setup:
        _tip_cmd = f"/key set {_model_entry.provider}"
        _tip_rest = "to add an AI provider and start coding"
        _tip_plain = f"● Tip Run {_tip_cmd} {_tip_rest}"
        _tip_styled = (
            f"{ui.style('●', '38;2;240;170;90')} {ui.style('Tip', '1', '38;2;240;170;90')} "
            f"{ui.style('Run', '38;2;200;205;215')} {ui.style(_tip_cmd, '1', '38;2;225;230;242')} "
            f"{ui.style(_tip_rest, '38;2;200;205;215')}"
        )
    else:
        _tip_rest = 'Ask anything — e.g. "What is the tech stack of this project?"'
        _tip_plain = f"● Tip {_tip_rest}"
        _tip_styled = (
            f"{ui.style('●', '38;2;240;170;90')} {ui.style('Tip', '1', '38;2;240;170;90')} "
            f"{ui.style(_tip_rest, '38;2;200;205;215')}"
        )
    ui.print_line()
    ui.print_line(_center_pad(len(_tip_plain)) + _tip_styled)

    # First-run guidance, styled like OpenCode's 'Getting started' card.
    if _needs_setup:
        _gs_w = min(_terminal - 8, 56)
        ui.print_line()
        ui.print_line(f"  {ui.style('◇ Getting started', '1', '38;2;130;170;250')}")
        ui.print_line()
        _gs_body = (
            "Apsara includes free-tier and local models so you can start "
            "quickly. Connect a provider to use other models, including "
            "Claude, GPT, Gemini etc."
        )
        import textwrap as _tw
        for _line in _tw.wrap(_gs_body, width=_gs_w):
            ui.print_line(f"    {ui.dim(_line)}")
        ui.print_line()
        _gs_cmd = f"/key set {_model_entry.provider}"
        _gs_gap = " " * max(_gs_w - len("Connect provider") - len(_gs_cmd), 2)
        ui.print_line(
            f"    {ui.style('Connect provider', '1', '38;2;225;230;242')}"
            f"{_gs_gap}{ui.style(_gs_cmd, '1', '38;2;180;210;255')}"
        )

    # Keep the welcome footer to workspace and version; session details are
    # available through /session rather than competing with the composer.
    from rich.text import Text
    _workspace = str(options.workspace_root)
    if options.workspace_root.is_relative_to(Path.home()):
        _workspace = "~/" + str(options.workspace_root.relative_to(Path.home()))
    _right_plain = f"v{__version__}"
    _path = Text(_workspace)
    _path.truncate(max(1, _terminal - len(_right_plain) - 9), overflow="ellipsis")
    _left_plain = f"⌂ {_path.plain}"
    _gap = max(_terminal - len(_left_plain) - len(_right_plain) - 4, 2)
    _left_styled = f"{ui.style('⌂', '38;2;240;190;110')} {ui.dim(_path.plain)}"
    _right_styled = ui.style(_right_plain, '38;2;190;150;250')
    ui.print_line()
    ui.print_line(f"  {_left_styled}{' ' * _gap}{_right_styled}")

    # Servers stay connected for the whole session rather than being
    # respawned each turn.
    async with mcp_session(config, options, ui):
        while True:
            _prompt, _toolbar, _continuation = build_scrolling_composer(
                ui, options, current_model, terminal_width()
            )
            try:
                instruction = (
                    await get_input_async(
                        _prompt, options.workspace_root,
                        toolbar=_toolbar, continuation=_continuation,
                        use_color=options.use_color,
                    )
                ).strip()
            except KeyboardInterrupt:
                ui.print_line()
                ui.info("Ctrl+C pressed. Type /exit to quit.")
                continue
            except EOFError:
                ui.print_line()
                break

            if not instruction:
                continue
            if instruction.startswith("/"):
                should_continue, current_model = handle_chat_command(
                    instruction, history, current_model, options, config, ui
                )
                if not should_continue:
                    break
                continue

            turn_count += 1

            # Proactive token budget warning before executing
            try:
                from apsara_cli.engine.executor import SYSTEM_PROMPT
                from apsara_cli.engine.llm import estimate_request_tokens
                _base = [{"role": "system", "content": SYSTEM_PROMPT}]
                _curr_tokens = estimate_request_tokens(_base + history, model=current_model)
                _budget = input_token_budget(current_model)
                if _curr_tokens >= int(_budget * 0.75):
                    _pct = int(_curr_tokens / _budget * 100)
                    ui.warning(
                        f"Context at {_pct}% capacity ({_curr_tokens:,} / {_budget:,} tokens). "
                        "Oldest turns may be trimmed — use /clear to reset."
                    )
            except Exception:
                pass

            ui.print_turn_separator(turn_count)

            history, latest_usage = await execute_instruction(
                instruction=instruction,
                model=current_model,
                history=history,
                options=options,
                ui=ui,
            )

            if latest_usage and latest_usage.get("total_tokens") is not None:
                ui.usage(latest_usage)
            save_if_needed(history, current_model, options, ui)

        return 0
