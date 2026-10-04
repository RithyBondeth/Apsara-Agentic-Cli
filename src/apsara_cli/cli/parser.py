import argparse
import sys
from typing import Optional, Sequence

from apsara_cli.config.cli_config import DEFAULT_CONFIG_PATH


def _argv_with_default_command(argv: Optional[Sequence[str]]) -> list[str]:
    """Make the empty command line open the interactive Apsara experience."""
    raw = list(sys.argv[1:] if argv is None else argv)
    if not raw:
        return ["chat"]

    # Keep root-level options ahead of the injected subcommand. Everything
    # else beginning with '-' is an interactive option such as --model,
    # --workspace, or --read-only and belongs after the implicit `chat`.
    prefix: list[str] = []
    remainder = list(raw)
    if remainder[0] == "--config":
        if len(remainder) < 2:
            return raw
        prefix = remainder[:2]
        remainder = remainder[2:]
    elif remainder[0].startswith("--config="):
        prefix = remainder[:1]
        remainder = remainder[1:]

    if not remainder:
        return [*prefix, "chat"]
    if remainder[0] in {"-h", "--help", "--version"}:
        return raw
    if remainder[0].startswith("-"):
        return [*prefix, "chat", *remainder]
    return raw


def _benchmark_repeat_count(value: str) -> int:
    try:
        repeat_count = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("repeat count must be an integer") from exc
    if not 1 <= repeat_count <= 10:
        raise argparse.ArgumentTypeError("repeat count must be between 1 and 10")
    return repeat_count


def _benchmark_pass_rate(value: str) -> float:
    try:
        percentage = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("pass rate must be a percentage") from exc
    if not 0 <= percentage <= 100:
        raise argparse.ArgumentTypeError("pass rate must be between 0 and 100")
    return percentage / 100


def _configure_output_streams() -> None:
    """Keep Unicode UI glyphs from crashing legacy Windows code pages."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="replace")
        except (AttributeError, OSError, ValueError):
            pass


def _add_shared_options(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument("--workspace", default=None, help="Workspace root the agent is allowed to access.")
    subparser.add_argument("--model", default=None, help="Model name to send through LiteLLM.")
    session_group = subparser.add_mutually_exclusive_group()
    session_group.add_argument(
        "--session",
        default=None,
        help="Open or create a specific named session.",
    )
    session_group.add_argument(
        "-c",
        "--continue",
        dest="continue_session",
        action="store_true",
        default=False,
        help="Resume the most recently updated saved session.",
    )
    subparser.add_argument("--stateless", dest="stateless", action="store_true", default=None,
                           help="Run without loading or saving local session history.")
    subparser.add_argument("--stateful", dest="stateless", action="store_false",
                           help="Force session history on even if the config enables stateless mode.")
    subparser.add_argument("--allow-bash", dest="allow_bash", action="store_true", default=None,
                           help="Enable the local bash tool for allowlisted non-interactive commands.")
    subparser.add_argument("--no-bash", dest="allow_bash", action="store_false",
                           help="Disable the local bash tool for this run.")
    subparser.add_argument("--allowed-commands", default=None,
                           help="Comma-separated command allowlist used with bash tool access. "
                                "Supports presets: @verify (test/build tools), @read, @git — "
                                "e.g. '@verify,git'.")
    subparser.add_argument("--bash-timeout", type=int, default=None, metavar="SECONDS",
                           help="Timeout for a single bash command. Defaults to 120s; raise it "
                                "for slow test suites.")
    subparser.add_argument("--max-file-size", type=int, default=None,
                           help="Override the maximum readable file size in bytes for this run.")
    subparser.add_argument("--auto-approve", dest="auto_approve", action="store_true", default=None,
                           help="Skip confirmations for workspace file mutations. Commands, "
                                "workspace code, and external mutations still require approval.")
    subparser.add_argument("--confirm", dest="auto_approve", action="store_false",
                           help="Require confirmations even if the config auto-approves actions.")
    subparser.add_argument("--dry-run", action="store_true",
                           help="Preview all tool calls and file changes without modifying the disk.")
    subparser.add_argument("--read-only", action="store_true",
                           help="Disable all destructive tools (write, delete, bash, etc.).")
    subparser.add_argument("--color", dest="color", action="store_true", default=None,
                           help="Force colored terminal output.")
    subparser.add_argument("--no-color", dest="color", action="store_false",
                           help="Disable colored terminal output.")


def build_parser() -> argparse.ArgumentParser:
    from apsara_cli import __version__

    parser = argparse.ArgumentParser(prog="apsara", description="Local CLI for Apsara.")
    parser.add_argument("--version", action="version", version=f"apsara {__version__}",
                        help="Show the installed Apsara version and exit.")
    parser.add_argument("--config", default=None,
                        help=f"Path to a TOML config file. Defaults to {DEFAULT_CONFIG_PATH}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run one instruction against the local workspace.")
    run_parser.add_argument("instruction", help="Instruction to send to the agent.")
    _add_shared_options(run_parser)

    chat_parser = subparsers.add_parser(
        "chat", help="Open the interactive UI explicitly (bare `apsara` does this by default)."
    )
    _add_shared_options(chat_parser)
    chat_parser.add_argument("--tui", action="store_true", default=False,
                             help="Open the full-screen interface (the default in a real terminal).")
    chat_parser.add_argument("--classic", action="store_true", default=False,
                             help="Use scrolling terminal chat instead of the full-screen interface.")

    init_parser = subparsers.add_parser("init", help="Initialize Apsara in the current project and open chat.")
    _add_shared_options(init_parser)
    init_parser.add_argument("--force", action="store_true",
                             help="Rewrite the local .apsara/config.toml file even if it already exists.")
    init_parser.add_argument("--no-chat", action="store_true",
                             help="Initialize the project without opening chat immediately.")

    sessions_parser = subparsers.add_parser("sessions", help="List saved local sessions for a workspace.")
    sessions_parser.add_argument("--workspace", default=None,
                                 help="Workspace root whose saved sessions should be listed.")

    doctor_parser = subparsers.add_parser(
        "doctor", help="Validate config, workspace access, tool readiness, and likely model credentials."
    )
    _add_shared_options(doctor_parser)
    doctor_parser.add_argument("--live", action="store_true", default=False,
                               help="Also make a real model API call to verify credentials (uses tokens).")
    doctor_parser.add_argument("--no-live", dest="live", action="store_false",
                               help="Skip the live model probe (this is the default).")

    mcp_parser = subparsers.add_parser(
        "mcp", help="List configured MCP servers and check that they connect."
    )
    mcp_parser.add_argument("--workspace", default=None,
                            help="Workspace root whose MCP servers should be checked.")
    mcp_parser.add_argument("--no-connect", dest="connect", action="store_false", default=True,
                            help="List the configured servers without connecting to them.")
    mcp_parser.add_argument("--color", dest="color", action="store_true", default=None,
                            help="Force colored terminal output.")
    mcp_parser.add_argument("--no-color", dest="color", action="store_false",
                            help="Disable colored terminal output.")

    trust_parser = subparsers.add_parser(
        "trust", help="Review or revoke approvals for this project's executable definitions."
    )
    trust_parser.add_argument("--workspace", default=None,
                              help="Workspace root whose approvals should be listed.")
    trust_parser.add_argument("--reset", action="store_true", default=False,
                              help="Revoke every approval recorded for the workspace.")
    trust_parser.add_argument("--color", dest="color", action="store_true", default=None,
                              help="Force colored terminal output.")
    trust_parser.add_argument("--no-color", dest="color", action="store_false",
                              help="Disable colored terminal output.")

    eval_parser = subparsers.add_parser("eval", help="Score recorded runs or execute coding benchmark suites.")
    eval_parser.add_argument("suite", help="Path to an evaluation suite JSON file.")
    eval_parser.add_argument("--workspace", default=None, help="Workspace containing .apsara/runs.")
    eval_parser.add_argument("--live", action="store_true", default=False,
                             help="Run coding benchmark cases with the configured model (uses provider tokens).")
    eval_parser.add_argument("--model", default=None, help="Model used by a live coding benchmark.")
    eval_parser.add_argument("--output", default=None,
                             help="Directory for disposable benchmark workspaces and result evidence.")
    eval_parser.add_argument("--results", default=None,
                             help="Re-score an existing benchmark results.json without provider calls.")
    eval_parser.add_argument("--compare", action="store_true", default=False,
                             help="Run optimized and full-tool reference profiles in independent workspaces.")
    eval_parser.add_argument(
        "--repeat",
        type=_benchmark_repeat_count,
        default=1,
        metavar="N",
        help="Run each live benchmark case N times (1-10; multiplies provider usage).",
    )
    eval_parser.add_argument(
        "--min-pass-rate",
        type=_benchmark_pass_rate,
        default=None,
        metavar="PERCENT",
        help="Override the suite's aggregate release pass-rate threshold.",
    )

    subparsers.add_parser(
        "login", help="Optional standalone provider setup; the UI can request keys inline."
    )
    subparsers.add_parser("logout", help="Clear stored provider API keys.")

    return parser


async def dispatch_command(args: argparse.Namespace, config: object) -> int:
    if args.command == "run":
        from apsara_cli.cli.chat import run_once
        return await run_once(args, config)
    if args.command == "chat":
        # The centered welcome and anchored composer need a full-screen layout.
        # Keep scrolling chat available explicitly and for non-TTY contexts.
        want_tui = getattr(args, "tui", False) or (
            sys.stdout.isatty() and not getattr(args, "classic", False)
        )
        if want_tui:
            try:
                from apsara_cli.cli.tui import tui_loop
                return await tui_loop(args, config)
            except ImportError:
                pass  # prompt_toolkit unavailable — fall back to classic
        from apsara_cli.cli.chat import chat_loop
        return await chat_loop(args, config)
    if args.command == "init":
        from apsara_cli.cli.workspace import init_workspace
        return await init_workspace(args, config)
    if args.command == "sessions":
        from apsara_cli.cli.workspace import print_sessions
        return print_sessions(args, config)
    if args.command == "doctor":
        from apsara_cli.cli.doctor import doctor
        return await doctor(args, config)
    if args.command == "mcp":
        from apsara_cli.cli.mcp_cli import mcp_status
        return await mcp_status(args, config)
    if args.command == "trust":
        from apsara_cli.cli.trust_cli import trust_command
        return trust_command(args, config)
    if args.command == "eval":
        from pathlib import Path
        from apsara_cli.engine.evals import (
            aggregate_benchmark_results,
            format_benchmark_aggregate,
            is_benchmark_suite,
            run_benchmark_suite,
            run_suite,
            score_benchmark_results,
        )
        suite_path = Path(args.suite).resolve()
        if is_benchmark_suite(suite_path):
            if args.results:
                if args.repeat != 1 or args.compare:
                    print("--repeat is only valid with --live; --compare also requires --live without --results.")
                    return 2
                results = score_benchmark_results(suite_path, Path(args.results).resolve())
            elif args.live:
                output = Path(args.output or ".apsara/benchmarks")
                if args.compare:
                    results, results_path = await run_benchmark_suite(
                        suite_path, output, args.model or config.defaults.model, repeats=args.repeat,
                        min_pass_rate=args.min_pass_rate, compare=True,
                    )
                    comparison_path = results_path.parent.parent / "comparison.json"
                    print(f"Comparison: {comparison_path}")
                    print(comparison_path.read_text(encoding="utf-8"))
                else:
                    results, results_path = await run_benchmark_suite(
                        suite_path, output, args.model or config.defaults.model,
                        repeats=args.repeat, min_pass_rate=args.min_pass_rate,
                    )
                print(f"Evidence: {results_path}")
                print(f"Summary: {results_path.with_name('summary.json')}")
            else:
                print("Coding benchmark suites require --live or --results <results.json>.")
                return 2
            for result in results:
                trial = result.details.get("trial")
                trial_label = f" trial {trial}" if trial is not None else ""
                print(
                    f"{'PASS' if result.passed else 'FAIL'} {result.name}{trial_label} "
                    f"[{result.language}] {result.score}/100: {'; '.join(result.checks)}"
                )
            aggregate = aggregate_benchmark_results(
                suite_path,
                results,
                min_pass_rate=args.min_pass_rate,
            )
            print(format_benchmark_aggregate(aggregate))
            return 0 if aggregate["passed"] else 1
        workspace = Path(args.workspace or ".").resolve()
        results = run_suite(suite_path, workspace)
        for result in results:
            print(f"{'PASS' if result.passed else 'FAIL'} {result.name}: {'; '.join(result.checks)}")
        return 0 if all(result.passed for result in results) else 1
    if args.command == "login":
        from apsara_cli.cli.auth_cli import login
        return await login()
    if args.command == "logout":
        from apsara_cli.cli.auth_cli import logout
        return logout()
    raise ValueError(f"Unknown command: {args.command}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    import asyncio

    from apsara_cli.config.cli_config import load_cli_config
    from apsara_cli.cli.options import load_cli_environment
    from apsara_cli.cli.auth import apply_credentials_to_env

    _configure_output_streams()
    parser = build_parser()
    args = parser.parse_args(_argv_with_default_command(argv))

    try:
        config = load_cli_config(args.config, getattr(args, "workspace", None))
        load_cli_environment(args, config)
        from apsara_cli.engine.pricing import refresh_pricing_if_stale
        refresh_pricing_if_stale()
        # Make stored BYO-key provider keys visible to LiteLLM (env/.env keys win).
        apply_credentials_to_env()
        return asyncio.run(dispatch_command(args, config))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
