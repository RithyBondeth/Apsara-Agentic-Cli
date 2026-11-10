"""Tests for testable helpers in the chat surface (cli/chat.py)."""
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from apsara_cli.cli import chat
from apsara_cli.cli import auth
from apsara_cli.cli.session import load_session_messages


# ── _save_api_key_to_env ──────────────────────────────────────────────────────

def test_save_api_key_creates_env_file(tmp_path):
    path = chat._save_api_key_to_env(tmp_path, "OPENAI_API_KEY", "sk-1")
    assert path == tmp_path / ".env"
    assert path.read_text(encoding="utf-8") == "OPENAI_API_KEY=sk-1\n"


def test_save_api_key_appends_to_existing_env(tmp_path):
    (tmp_path / ".env").write_text("FOO=bar\n", encoding="utf-8")
    chat._save_api_key_to_env(tmp_path, "OPENAI_API_KEY", "sk-1")
    content = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "FOO=bar" in content
    assert "OPENAI_API_KEY=sk-1" in content


def test_save_api_key_updates_existing_value(tmp_path):
    (tmp_path / ".env").write_text("OPENAI_API_KEY=old\nFOO=bar\n", encoding="utf-8")
    chat._save_api_key_to_env(tmp_path, "OPENAI_API_KEY", "sk-new")
    content = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "OPENAI_API_KEY=sk-new" in content
    assert "OPENAI_API_KEY=old" not in content
    assert "FOO=bar" in content  # unrelated keys are preserved


# ── save_if_needed ────────────────────────────────────────────────────────────

class _StubUI:
    def __init__(self):
        self.saved_path = None

    def session_saved(self, path):
        self.saved_path = path


def test_save_if_needed_skips_when_stateless(tmp_path):
    ui = _StubUI()
    options = SimpleNamespace(stateless=True, workspace_root=tmp_path, session="default")
    chat.save_if_needed([{"role": "user", "content": "x"}], "gpt-4o", options, ui)
    assert ui.saved_path is None
    assert not (tmp_path / ".apsara-cli").exists()  # nothing written


def test_save_if_needed_persists_when_stateful(tmp_path):
    ui = _StubUI()
    options = SimpleNamespace(stateless=False, workspace_root=tmp_path, session="default")
    messages = [{"role": "user", "content": "x"}]
    chat.save_if_needed(messages, "gpt-4o", options, ui)

    assert ui.saved_path is not None and ui.saved_path.exists()
    assert load_session_messages(tmp_path, "default") == messages


# ── /key command group ────────────────────────────────────────────────────────

class _RecordingUI:
    """Minimal ConsoleUI stand-in that records every rendered line."""

    def __init__(self):
        self.lines: list[str] = []
        self.log_file = None

    def _record(self, text=""):
        self.lines.append(str(text))

    print_line = _record
    def badge(self, text, *codes): return text
    def style(self, text, *codes): return text
    def dim(self, text): return text
    def muted(self, text): return text
    def print_block(self, text, *args): self._record(text)
    def info(self, text): self._record(f"INFO {text}")
    def success(self, text): self._record(f"OK {text}")
    def warning(self, text): self._record(f"WARN {text}")
    def error(self, text): self._record(f"ERROR {text}")
    def read_single_key(self): return "\r"

    @property
    def text(self):
        return "\n".join(self.lines)


class _ChoiceUI(_RecordingUI):
    def __init__(self, choice):
        super().__init__()
        self.choice = choice

    def read_single_key(self):
        return self.choice


class _ConfirmUI(_RecordingUI):
    def __init__(self, approved):
        super().__init__()
        self.approved = approved
        self.confirmations = []

    def confirm_action(self, action, payload):
        self.confirmations.append((action, payload))
        return self.approved


@pytest.fixture
def key_env(tmp_path, monkeypatch):
    """Isolated credential store + clean provider env for /key tests."""
    monkeypatch.setattr(auth, "CREDENTIALS_PATH", tmp_path / "credentials.json")
    for var in ("OPENAI_API_KEY", "GROQ_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    return SimpleNamespace(
        workspace_root=tmp_path, session="default", stateless=False,
        allow_bash=False, allowed_commands=None, max_file_size=None,
        dry_run=False, read_only=False,
    )


def _run_cmd(cmd, options, ui=None):
    ui = ui or _RecordingUI()
    keep, model = chat.handle_chat_command(cmd, [], "groq/llama-3.3-70b-versatile", options, object(), ui)
    return keep, model, ui


def test_diff_and_usage_commands_render_local_reports(key_env):
    keep, _model, diff_ui = _run_cmd("/diff", key_env)
    assert keep is True
    assert "Git workspace changes" in diff_ui.text

    keep, _model, usage_ui = _run_cmd("/usage", key_env)
    assert keep is True
    assert "LOCAL USAGE" in usage_ui.text
    assert "no usage data is uploaded" in usage_ui.text


def test_skills_command_lists_and_previews_without_a_provider_call(key_env):
    with patch("apsara_cli.engine.llm.call_llm") as provider:
        keep, _model, ui = _run_cmd("/skills", key_env)
        assert keep is True and "debug [bundled]" in ui.text
        keep, _model, ui = _run_cmd("/skills debug", key_env)
        assert keep is True and "# Debugging workflow" in ui.text
        keep, _model, ui = _run_cmd("/skills missing", key_env)
        assert keep is True and "was not found" in ui.text
    provider.assert_not_called()


def test_paid_model_switch_requires_explicit_confirmation(key_env, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    ui = _ChoiceUI("n")

    keep, model, ui = _run_cmd("/model gpt-4o", key_env, ui)

    assert keep is True
    assert model == "groq/llama-3.3-70b-versatile"
    assert "paid model" in ui.text
    assert "cancelled" in ui.text


def test_paid_model_switch_proceeds_after_confirmation(key_env, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    ui = _ChoiceUI("y")

    keep, model, ui = _run_cmd("/model gpt-4o", key_env, ui)

    assert keep is True
    assert model == "gpt-4o"
    assert "paid model" in ui.text


def test_key_list_shows_all_keyed_providers(key_env):
    keep, _model, ui = _run_cmd("/key list", key_env)
    assert keep is True
    for provider in ("groq", "openai", "anthropic", "google", "mistral", "deepseek"):
        assert provider in ui.text
    assert "not set" in ui.text


def test_key_set_saves_to_store_and_env(key_env):
    with patch("apsara_cli.cli.chat.getpass", return_value="gsk_newkey123"):
        keep, _model, ui = _run_cmd("/key set groq", key_env)
    assert keep is True
    assert "Saved groq key" in ui.text
    assert auth.get_provider_key("groq") == "gsk_newkey123"
    assert os.environ.get("GROQ_API_KEY") == "gsk_newkey123"
    os.environ.pop("GROQ_API_KEY", None)


def test_key_set_unknown_provider_errors(key_env):
    keep, _model, ui = _run_cmd("/key set nonsense", key_env)
    assert keep is True
    assert "ERROR" in ui.text
    assert auth.stored_providers() == []


def test_key_set_empty_input_cancels(key_env):
    with patch("apsara_cli.cli.chat.getpass", return_value=""):
        _keep, _model, ui = _run_cmd("/key set openai", key_env)
    assert "Cancelled" in ui.text
    assert auth.stored_providers() == []


def test_key_remove_deletes_stored_key(key_env):
    auth.save_provider_key("groq", api_key="gsk_x")
    _keep, _model, ui = _run_cmd("/key remove groq", key_env)
    assert "Removed stored groq key" in ui.text
    assert auth.stored_providers() == []


def test_key_remove_missing_warns(key_env):
    _keep, _model, ui = _run_cmd("/key remove openai", key_env)
    assert "WARN" in ui.text


def test_unknown_command_suggests_closest(key_env):
    _keep, _model, ui = _run_cmd("/ky set", key_env)
    assert "Did you mean /key?" in ui.text


def test_help_renders_all_sections(key_env):
    ui = _RecordingUI()
    chat.print_chat_help(ui)
    for section in ("CONVERSATION", "MODELS & KEYS", "SESSION", "DIAGNOSTICS"):
        assert section in ui.text
    assert "/key" in ui.text


def test_bug_command_creates_privacy_safe_bundle(key_env):
    _keep, _model, ui = _run_cmd("/bug", key_env)

    bundles = list((key_env.workspace_root / ".apsara" / "bugs").iterdir())
    assert len(bundles) == 1
    assert (bundles[0] / "manifest.json").is_file()
    assert "content was omitted by default" in ui.text


def test_bug_include_content_requires_explicit_confirmation(key_env):
    ui = _ConfirmUI(False)
    history = [{"role": "user", "content": "private source"}]

    keep, model = chat.handle_chat_command(
        "/bug --include-content",
        history,
        "groq/llama-3.3-70b-versatile",
        key_env,
        object(),
        ui,
    )

    assert keep is True
    assert model == "groq/llama-3.3-70b-versatile"
    assert ui.confirmations == [("export_diagnostic_content", {"message_count": 1})]
    assert "cancelled" in ui.text
    assert not (key_env.workspace_root / ".apsara" / "bugs").exists()


def test_bug_include_content_proceeds_after_confirmation(key_env):
    ui = _ConfirmUI(True)
    history = [{"role": "user", "content": "private reproduction"}]

    chat.handle_chat_command(
        "/bug --include-content",
        history,
        "groq/llama-3.3-70b-versatile",
        key_env,
        object(),
        ui,
    )

    bundle = next((key_env.workspace_root / ".apsara" / "bugs").iterdir())
    state = json.loads((bundle / "session_state.json").read_text(encoding="utf-8"))
    assert state["privacy_mode"] == "content-included"
    assert state["history"][0]["content"] == "private reproduction"
    assert "Review every file before sharing" in ui.text


def test_bug_command_rejects_unknown_arguments(key_env):
    _keep, _model, ui = _run_cmd("/bug --raw", key_env)

    assert "Usage: /bug [--include-content]" in ui.text
    assert not (key_env.workspace_root / ".apsara" / "bugs").exists()
