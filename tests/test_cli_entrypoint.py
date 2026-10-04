import os
import re
import sys
from types import SimpleNamespace

import pytest

from apsara_cli.cli.options import resolve_runtime_options
from apsara_cli.cli.parser import _argv_with_default_command, build_parser
from apsara_cli.cli.session import save_session_messages


def _defaults(**overrides):
    values = {
        "workspace": None,
        "model": None,
        "session": None,
        "stateless": None,
        "allow_bash": None,
        "allowed_commands": None,
        "bash_timeout": None,
        "max_file_size": None,
        "auto_approve": None,
        "color": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_empty_command_line_defaults_to_interactive_chat():
    args = build_parser().parse_args(_argv_with_default_command([]))

    assert args.command == "chat"
    assert args.model is None
    assert args.tui is False


def test_explicit_commands_and_process_arguments_are_preserved(monkeypatch):
    assert _argv_with_default_command(["doctor"]) == ["doctor"]
    assert _argv_with_default_command(["--help"]) == ["--help"]

    monkeypatch.setattr(sys, "argv", ["apsara"])
    assert _argv_with_default_command(None) == ["chat"]


def test_bare_interactive_options_are_attached_to_default_chat():
    normalized = _argv_with_default_command([
        "--model", "opencode/big-pickle", "--read-only"
    ])
    args = build_parser().parse_args(normalized)

    assert normalized == [
        "chat", "--model", "opencode/big-pickle", "--read-only"
    ]
    assert args.command == "chat"
    assert args.model == "opencode/big-pickle"
    assert args.read_only is True


def test_bare_continue_option_is_attached_to_default_chat():
    normalized = _argv_with_default_command(["--continue"])
    args = build_parser().parse_args(normalized)

    assert normalized == ["chat", "--continue"]
    assert args.continue_session is True


def test_continue_and_named_session_are_mutually_exclusive():
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["chat", "--continue", "--session", "work"])


def test_global_config_stays_before_implicit_chat():
    normalized = _argv_with_default_command([
        "--config", "custom.toml", "--workspace", "/tmp/project"
    ])
    args = build_parser().parse_args(normalized)

    assert normalized == [
        "--config", "custom.toml", "chat", "--workspace", "/tmp/project"
    ]
    assert args.config == "custom.toml"
    assert args.workspace == "/tmp/project"


def test_missing_global_config_value_is_left_for_argparse_to_reject():
    assert _argv_with_default_command(["--config"]) == ["--config"]


def test_interactive_start_uses_zen_default_without_hidden_auth_default(tmp_path):
    args = build_parser().parse_args(["chat", "--workspace", str(tmp_path)])
    options = resolve_runtime_options(args, _defaults())

    assert options.model == "opencode/space-bunny-free"
    assert re.fullmatch(r"session-\d{8}-\d{6}-[0-9a-f]{4}", options.session)


def test_named_session_is_used_explicitly(tmp_path):
    args = build_parser().parse_args([
        "chat", "--workspace", str(tmp_path), "--session", "feature-work"
    ])

    options = resolve_runtime_options(args, _defaults())

    assert options.session == "feature-work"


def test_continue_uses_most_recent_saved_session(tmp_path):
    older = save_session_messages(tmp_path, "older", "model", [])
    newer = save_session_messages(tmp_path, "newer", "model", [])
    os.utime(older, ns=(1_000_000_000, 1_000_000_000))
    os.utime(newer, ns=(2_000_000_000, 2_000_000_000))
    args = build_parser().parse_args([
        "chat", "--workspace", str(tmp_path), "--continue"
    ])

    options = resolve_runtime_options(args, _defaults())

    assert options.session == "newer"


def test_continue_without_saved_history_starts_fresh(tmp_path):
    args = build_parser().parse_args([
        "chat", "--workspace", str(tmp_path), "--continue"
    ])

    options = resolve_runtime_options(args, _defaults())

    assert options.session.startswith("session-")


def test_configured_session_remains_an_explicit_pin(tmp_path):
    args = build_parser().parse_args(["chat", "--workspace", str(tmp_path)])

    options = resolve_runtime_options(args, _defaults(session="pinned"))

    assert options.session == "pinned"
