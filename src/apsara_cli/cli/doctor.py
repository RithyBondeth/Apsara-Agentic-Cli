import subprocess
import asyncio
import os
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apsara_cli.shared.types import ResolvedOptions

from apsara_cli.cli.options import detect_model_credentials
from apsara_cli.cli.session import get_sessions_dir
from apsara_cli.engine.tools import agent_runtime_context, execute_tool, get_agent_tools
from apsara_cli.shared.types import DoctorCheckResult


def render_doctor_result(ui, result: "DoctorCheckResult") -> None:
    if result.status == "pass":
        ui.success(f"[{result.name}] {result.detail}")
    elif result.status == "warn":
        ui.warning(f"[{result.name}] {result.detail}")
    else:
        ui.error(f"[{result.name}] {result.detail}")


def check_mcp_servers(options, config) -> list:
    """Report configured MCP servers and whether they're approved to run.

    Deliberately does not connect: doctor is a read-only diagnostic, and
    connecting would launch subprocesses. `apsara mcp` does that on request.
    """
    from apsara_cli.config import trust as trust_store

    results = []

    for message in getattr(config, "mcp_errors", []) or []:
        results.append(DoctorCheckResult("mcp-config", "fail", message))

    servers = list(getattr(config, "mcp_servers", []) or [])
    if not servers:
        results.append(DoctorCheckResult(
            "mcp", "warn",
            "No MCP servers configured. Add an [mcp_servers.<name>] section to "
            ".apsara/config.toml to connect external tools.",
        ))
        return results

    trusted = trust_store.list_trusted(options.workspace_root)
    for server in servers:
        label = f"mcp:{server.name}"
        if not server.enabled:
            results.append(DoctorCheckResult(label, "warn", f"'{server.name}' is disabled in config."))
            continue

        problem = server.validate()
        if problem:
            results.append(DoctorCheckResult(label, "fail", problem))
            continue

        entry = trusted.get(f"mcp:{server.name}")
        digest = trust_store.digest_text(server.trust_digest_source())
        if isinstance(entry, dict) and entry.get("sha256") == digest:
            results.append(DoctorCheckResult(
                label, "pass", f"'{server.name}' ({server.transport}) is configured and approved."
            ))
        else:
            results.append(DoctorCheckResult(
                label, "warn",
                f"'{server.name}' ({server.transport}) is configured but not yet approved. "
                "Run `apsara mcp` to review and approve it.",
            ))

    return results


def run_workspace_checks(options, config, args) -> list:
    results = []

    if sys.version_info >= (3, 10):
        results.append(DoctorCheckResult("python", "pass", f"Python {sys.version.split()[0]} is supported."))
    else:
        results.append(DoctorCheckResult("python", "fail", f"Python {sys.version.split()[0]} is below the required 3.10+."))

    from apsara_cli import __version__
    results.append(DoctorCheckResult("version", "pass", f"Apsara {__version__}."))

    # Check for git
    try:
        git_ver = subprocess.run(["git", "--version"], capture_output=True, text=True, check=True).stdout.strip()
        results.append(DoctorCheckResult("git", "pass", f"Found {git_ver}."))
    except (subprocess.CalledProcessError, FileNotFoundError):
        results.append(DoctorCheckResult("git", "fail", "Git is not installed or not in PATH. Required for git_status/git_diff tools."))

    # Check for ripgrep
    try:
        rg_ver = subprocess.run(["rg", "--version"], capture_output=True, text=True, check=True).stdout.splitlines()[0]
        results.append(DoctorCheckResult("ripgrep", "pass", f"Found {rg_ver}."))
    except (subprocess.CalledProcessError, FileNotFoundError):
        results.append(DoctorCheckResult("ripgrep", "warn", "ripgrep (rg) not found. search_files tool will fallback to slower 'grep'."))

    from apsara_cli.engine.intelligence import capabilities
    intelligence = capabilities()
    if intelligence["tree_sitter"]:
        detail = "Python AST and optional Tree-sitter parsers are available."
        status = "pass"
    else:
        detail = (
            "Python AST intelligence is available; other languages use definition-pattern "
            "fallbacks. Install 'apsara-agentic[intelligence]' for Tree-sitter parsing."
        )
        status = "warn"
    checkers = intelligence["project_checkers"]
    if checkers:
        detail += f" Project checkers found: {', '.join(checkers)}."
    results.append(DoctorCheckResult("code-intelligence", status, detail))

    # Check for a configured BYO-key provider
    from apsara_cli.cli.auth import get_active_provider, stored_providers, apply_credentials_to_env
    apply_credentials_to_env()
    active_provider = get_active_provider()
    stored = stored_providers()
    if active_provider:
        others = [p for p in stored if p != active_provider]
        extra = f" (also stored: {', '.join(others)})" if others else ""
        results.append(DoctorCheckResult("provider", "pass", f"Active provider: {active_provider}{extra}."))
    else:
        results.append(DoctorCheckResult(
            "provider", "warn",
            "No provider configured. Start `apsara`; the UI will request a key on first use.",
        ))

    config_path = getattr(config, "path", None)
    config_exists = getattr(config, "exists", False)
    args_config = getattr(args, "config", None)

    if config_exists:
        results.append(DoctorCheckResult("config", "pass", f"Loaded config from {config_path}."))
    elif args_config:
        results.append(DoctorCheckResult("config", "fail", f"Config file was requested but not found at {config_path}."))
    else:
        results.append(DoctorCheckResult("config", "warn", f"No config file found at {config_path}; using defaults and CLI flags."))

    if options.workspace_root.exists() and options.workspace_root.is_dir():
        results.append(DoctorCheckResult("workspace", "pass", f"Workspace exists at {options.workspace_root}."))
        
        # Check for team instructions
        inst_file = options.workspace_root / ".apsara" / "instructions.md"
        if inst_file.exists():
            results.append(DoctorCheckResult("instructions", "pass", "Found team-specific instructions file."))
        else:
            results.append(DoctorCheckResult("instructions", "warn", "No .apsara/instructions.md found. Using default system prompt."))
    elif options.workspace_root.exists():
        results.append(DoctorCheckResult("workspace", "fail", f"Workspace path exists but is not a directory: {options.workspace_root}."))
    else:
        results.append(DoctorCheckResult("workspace", "fail", f"Workspace does not exist: {options.workspace_root}."))
        return results

    try:
        session_dir = get_sessions_dir(options.workspace_root)
        session_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", dir=str(session_dir), prefix="doctor-", suffix=".tmp", delete=False) as f:
            f.write("ok")
            temp_path = Path(f.name)
        temp_path.unlink(missing_ok=True)
        status = "pass" if not options.stateless else "warn"
        detail = (
            f"Session storage is writable at {session_dir}."
            if not options.stateless
            else f"Session storage is writable at {session_dir}, but stateless mode is enabled."
        )
        results.append(DoctorCheckResult("session-store", status, detail))
    except Exception as exc:
        results.append(DoctorCheckResult("session-store", "fail", f"Could not write session data: {exc}"))

    with agent_runtime_context(
        workspace_root=options.workspace_root,
        enable_bash=options.allow_bash,
        allowed_commands=options.allowed_commands,
        max_file_size_bytes=options.max_file_size,
        bash_timeout_seconds=options.bash_timeout,
        confirmation_callback=lambda action, payload: False,
    ):
        tool_names = [tool["function"]["name"] for tool in get_agent_tools()]
        results.append(DoctorCheckResult("tools", "pass", f"Enabled tools: {', '.join(tool_names)}"))

        structure_result = execute_tool("list_project_structure", {"root_dir": "."})
        if structure_result.startswith("Error"):
            results.append(DoctorCheckResult("workspace-scan", "fail", structure_result))
        else:
            results.append(DoctorCheckResult("workspace-scan", "pass", "Workspace structure scan succeeded."))

        if options.allow_bash:
            default_command = "pwd"
            if options.allowed_commands and default_command not in options.allowed_commands:
                default_command = sorted(options.allowed_commands)[0]
            command_result = execute_tool("run_bash_command", {"command": default_command})
            if "not approved" in command_result:
                results.append(DoctorCheckResult(
                    "bash-tool", "pass",
                    f"Bash tool is enabled with allowlist {', '.join(sorted(options.allowed_commands or set()))}; "
                    "approval prompt would be required during normal use.",
                ))
            elif command_result.startswith("Error"):
                results.append(DoctorCheckResult("bash-tool", "fail", command_result))
            else:
                results.append(DoctorCheckResult("bash-tool", "pass", f"Bash tool is enabled and command '{default_command}' succeeded."))
        else:
            results.append(DoctorCheckResult(
                "bash-tool", "warn",
                "Bash tool is disabled, so the agent cannot run your tests to check its own "
                "work. Enable it with: --allow-bash --allowed-commands @verify",
            ))

    results.extend(check_mcp_servers(options, config))

    from apsara_cli.engine.model_capabilities import model_capabilities, completion_limit
    try:
        capabilities = model_capabilities(options.model)
        capabilities.require(tools=True, streaming=True)
        confirmed = capabilities.tools is True and capabilities.streaming is True
        results.append(DoctorCheckResult(
            "model-capabilities", "pass" if confirmed else "warn",
            f"Source: {capabilities.source}; tools={capabilities.tools}, streaming={capabilities.streaming}, "
            f"context={capabilities.context_window or 'unknown'}, output budget={completion_limit(options.model)}. "
            "Use --live to confirm provider behavior.",
        ))
    except ValueError as exc:
        results.append(DoctorCheckResult("model-capabilities", "fail", str(exc)))

    provider, env_vars, note = detect_model_credentials(options.model)
    if env_vars is None:
        status = "pass" if provider == "ollama" else "warn"
        results.append(DoctorCheckResult("credentials", status, note))
    else:
        present = [ev for ev in env_vars if os.environ.get(ev)]
        if present:
            results.append(DoctorCheckResult("credentials", "pass", f"{note} Found {', '.join(present)}."))
        else:
            results.append(DoctorCheckResult("credentials", "fail", f"{note} Missing any of: {', '.join(env_vars)}."))

    return results


async def run_live_probe(options: "ResolvedOptions") -> DoctorCheckResult:
    from apsara_cli.engine.llm import call_llm_stream
    import json
    probe_messages = [{"role": "user", "content": "Call apsara_probe with nonce apsara-ready. Do not execute any other action."}]
    probe_tool = {"type": "function", "function": {"name": "apsara_probe", "description": "Return the supplied readiness nonce. This probe never executes a tool.",
                  "parameters": {"type": "object", "properties": {"nonce": {"type": "string"}}, "required": ["nonce"]}}}

    async def collect():
        return [event async for event in call_llm_stream(probe_messages, model=options.model, tools=[probe_tool], max_completion_tokens=128)]

    with agent_runtime_context(
        workspace_root=options.workspace_root,
        enable_bash=options.allow_bash,
        allowed_commands=options.allowed_commands,
        max_file_size_bytes=options.max_file_size,
        bash_timeout_seconds=options.bash_timeout,
        confirmation_callback=lambda action, payload: False,
    ):
        try:
            events = await asyncio.wait_for(collect(), timeout=30)
        except asyncio.TimeoutError:
            return DoctorCheckResult("live-probe", "fail", "Streaming/tool-call probe timed out.")
    error = next((event.get("error") for event in events if event.get("type") == "stream_error"), None)
    if error:
        return DoctorCheckResult("live-probe", "fail", f"Live model probe failed: {error}")
    done = next((event for event in events if event.get("type") == "stream_done"), {})
    for call in done.get("tool_calls") or []:
        try:
            function = call["function"]
            if function["name"] == "apsara_probe" and json.loads(function["arguments"]) == {"nonce": "apsara-ready"}:
                usage = done.get("usage") or {}
                return DoctorCheckResult("live-probe", "pass", f"Streaming and valid tool arguments confirmed; provider reported {usage.get('total_tokens', 'unknown')} tokens. No tool was executed.")
        except (KeyError, ValueError, TypeError):
            continue
    return DoctorCheckResult("live-probe", "fail", "Model did not return the requested valid tool call. Agent compatibility remains unconfirmed.")


async def doctor(args: object, config: object) -> int:
    from apsara_cli.cli.options import resolve_runtime_options
    from apsara_cli.shared.ui import ConsoleUI

    options = resolve_runtime_options(args, config.defaults)
    ui = ConsoleUI(use_color=options.use_color, auto_approve=True)
    results = run_workspace_checks(options, config, args)

    if getattr(args, "live", False):
        credentials_status = next((r.status for r in results if r.name == "credentials"), "warn")
        workspace_status = next((r.status for r in results if r.name == "workspace"), "fail")
        if credentials_status == "fail" or workspace_status == "fail":
            results.append(DoctorCheckResult(
                "live-probe", "warn",
                "Live probe skipped because workspace or credentials checks failed.",
            ))
        else:
            results.append(await run_live_probe(options))

    pass_count = warn_count = fail_count = 0
    for result in results:
        render_doctor_result(ui, result)
        if result.status == "pass":
            pass_count += 1
        elif result.status == "warn":
            warn_count += 1
        else:
            fail_count += 1

    ui.print_line()
    if fail_count:
        ui.error(f"Doctor finished with {pass_count} passed, {warn_count} warnings, and {fail_count} failures.")
        return 1

    ui.success(f"Doctor finished with {pass_count} passed and {warn_count} warnings.")
    return 0
