import asyncio
import json
from types import SimpleNamespace

import pytest

from apsara_cli.engine.model_capabilities import compatibility_error, completion_limit, model_capabilities


def profile(monkeypatch, tmp_path, value):
    path = tmp_path / "models.json"
    path.write_text(json.dumps({"openai/test-small": value}))
    monkeypatch.setenv("APSARA_MODEL_CAPABILITIES", str(path))


def test_user_model_limits_constrain_input_and_output(monkeypatch, tmp_path):
    from apsara_cli.cli.history import input_token_budget
    profile(monkeypatch, tmp_path, {"tools": True, "streaming": True, "context_window": 2048, "max_output_tokens": 300})
    capabilities = model_capabilities("openai/test-small")
    assert capabilities.source == "user profile"
    assert completion_limit("openai/test-small") == 300
    assert 0 < input_token_budget("openai/test-small") < 1748


def test_provider_client_restriction_is_distinct_from_missing_credentials():
    message = compatibility_error(RuntimeError("OpenCode's free tier can only be used from within OpenCode"))
    assert "provider access restriction" in message
    assert "Changing the API key alone may not resolve" in message


def test_missing_credit_error_explains_recovery():
    message = compatibility_error(RuntimeError("OpenAIException - Upstream request failed: Insufficient account funds"))
    assert "Add credits" in message
    assert "apsara doctor --live" in message
    assert "API key alone" in message


def test_restricted_model_fails_before_live_probe(monkeypatch, tmp_path):
    from apsara_cli.cli.doctor import run_live_probe
    from apsara_cli.engine import llm
    monkeypatch.setenv("OPENCODE_API_KEY", "test-key")
    async def forbidden(**kwargs):
        pytest.fail("restricted model must not reach the provider")
    monkeypatch.setattr(llm.litellm, "acompletion", forbidden)
    options = SimpleNamespace(workspace_root=tmp_path, model="opencode/big-pickle", allow_bash=False,
                              allowed_commands=set(), max_file_size=10000, bash_timeout=30)
    result = asyncio.run(run_live_probe(options))
    assert result.status == "fail"
    assert "OpenCode client" in result.detail


def test_explicit_restricted_model_returns_error_without_history_or_provider_changes(tmp_path):
    from apsara_cli.cli.chat import execute_instruction
    from apsara_cli.shared.ui import ConsoleUI
    ui = ConsoleUI(use_color=False)
    original = [{"role": "user", "content": "previous request"}]
    # Preflight must stop before reading workspace or request options.
    history, usage = asyncio.run(execute_instruction("new request", "opencode/big-pickle", original, None, ui))
    assert history == original
    assert usage is None
    assert ui.last_run_state == "failed"


def test_known_incompatible_models_fail_before_provider_call(monkeypatch, tmp_path):
    from apsara_cli.engine import llm
    from apsara_cli.cli import auth
    profile(monkeypatch, tmp_path, {"tools": False, "streaming": True})
    monkeypatch.setattr(auth, "credentials_present_for_model", lambda _model: True)
    async def forbidden(**_kwargs):
        pytest.fail("provider must not be called for known incompatible model")
    monkeypatch.setattr(llm.litellm, "acompletion", forbidden)
    async def collect():
        return [event async for event in llm.call_llm_stream([], model="openai/test-small")]
    events = asyncio.run(collect())
    assert "does not support tool calling" in events[0]["error"]


@pytest.mark.parametrize("value", [
    {"tools": "yes"}, {"context_window": True}, {"max_output_tokens": -1},
    {"context_window": 1000, "max_output_tokens": 1000}, [],
])
def test_invalid_model_profiles_are_rejected(monkeypatch, tmp_path, value):
    profile(monkeypatch, tmp_path, value)
    with pytest.raises(ValueError, match="Invalid model capabilities"):
        model_capabilities("openai/test-small")


def test_live_probe_checks_streamed_tool_arguments_without_execution(monkeypatch, tmp_path):
    from apsara_cli.cli.doctor import run_live_probe
    from apsara_cli.engine import llm
    calls = []
    async def stream(_messages, *, model, tools, max_completion_tokens):
        calls.append((model, tools, max_completion_tokens))
        yield {"type": "stream_done", "content": "", "tool_calls": [{"function": {
            "name": "apsara_probe", "arguments": '{"nonce":"apsara-ready"}',
        }}], "usage": {"total_tokens": 12}}
    monkeypatch.setattr(llm, "call_llm_stream", stream)
    options = SimpleNamespace(workspace_root=tmp_path, model="openai/test-small", allow_bash=False,
                              allowed_commands=set(), max_file_size=10000, bash_timeout=30)
    result = asyncio.run(run_live_probe(options))
    assert result.status == "pass"
    assert calls[0][1][0]["function"]["name"] == "apsara_probe"
    assert calls[0][2] == 128
    assert not list(tmp_path.iterdir())
