"""Tests for the BYO-key credential store (cli/auth.py) and provider helpers.

Every test points CREDENTIALS_PATH at a temp directory so the real
~/.apsara/credentials.json is never touched, and clears relevant env vars.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from apsara_cli.cli import auth
from apsara_cli.engine import models

_PROVIDER_ENV_VARS = (
    "OPENCODE_API_KEY",
    "OPENAI_API_KEY",
    "GROQ_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
)


@pytest.fixture
def temp_creds(tmp_path, monkeypatch):
    """Redirect the credential store to a temp file and clear provider env vars.

    apply_credentials_to_env() mutates os.environ directly, so we snapshot and
    restore these vars ourselves rather than relying on monkeypatch alone.
    """
    monkeypatch.setattr(auth, "CREDENTIALS_PATH", tmp_path / "credentials.json")
    saved = {var: os.environ.get(var) for var in _PROVIDER_ENV_VARS}
    for var in _PROVIDER_ENV_VARS:
        os.environ.pop(var, None)
    yield tmp_path
    for var, value in saved.items():
        if value is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = value


# ── Provider registry helpers ─────────────────────────────────────────────────

def test_provider_helpers_consistent_with_registry():
    providers = models.providers_in_order()
    assert providers[0] == "opencode"
    assert "openai" in providers
    assert "ollama" in providers

    # OpenAI is a keyed provider; ollama is local (no key).
    assert models.provider_env_var("openai") == "OPENAI_API_KEY"
    assert models.provider_env_var("opencode") == "OPENCODE_API_KEY"
    assert models.provider_env_var("ollama") is None
    assert models.default_model_for_provider("openai") == "gpt-4o"
    assert models.default_model_for_provider("groq") == "groq/openai/gpt-oss-120b"
    assert models.default_model_for_provider("opencode") == models.DEFAULT_MODEL


def test_big_pickle_alias_and_litellm_routing(monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "zen-test-key")

    assert models.resolve_model_id("pickle") == "opencode/big-pickle"
    model_id, options = models.resolve_litellm_request("pickle")
    assert model_id == "openai/big-pickle"
    assert options == {
        "api_base": models.OPENCODE_API_BASE,
        "api_key": "zen-test-key",
    }


def test_default_is_selectable_and_restricted_free_model_is_not():
    assert models.model_availability(models.lookup_model(models.DEFAULT_MODEL)) == (True, "")
    available, reason = models.model_availability(models.lookup_model("pickle"))
    assert available is False
    assert "OpenCode client" in reason
    assert models.default_model_for_provider("opencode") != "opencode/big-pickle"


def test_other_zen_chat_model_uses_opencode_endpoint_and_key(monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "zen-test-key")
    monkeypatch.setenv("OPENAI_API_KEY", "other-provider-key")
    monkeypatch.setenv("OPENCODE_API_BASE", "https://zen.example/v1")
    model_id, options = models.resolve_litellm_request("opencode/minimax-m2.5")
    assert model_id == "openai/minimax-m2.5"
    assert options == {"api_base": "https://zen.example/v1", "api_key": "zen-test-key"}
    assert models.resolve_litellm_request("gpt-4o") == ("gpt-4o", {})


def test_free_testing_model_routes_without_paid_fallback(monkeypatch):
    from apsara_cli.engine.executor import _model_candidates
    monkeypatch.setenv("OPENCODE_API_KEY", "zen-test-key")
    monkeypatch.setenv("APSARA_FALLBACK_MODELS", "opencode/minimax-m2.5,gpt-4o")
    model = models.resolve_model_id("bunny")
    assert model == "opencode/space-bunny-free"
    assert models.lookup_model(model).tier == "free"
    assert models.resolve_litellm_request(model) == (
        "openai/space-bunny-free",
        {"api_base": os.environ.get("OPENCODE_API_BASE", models.OPENCODE_API_BASE), "api_key": "zen-test-key"},
    )
    assert _model_candidates(model) == [model]


def test_zen_does_not_borrow_openai_credentials(monkeypatch):
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "other-provider-key")
    monkeypatch.delenv("OPENCODE_API_BASE", raising=False)
    _, options = models.resolve_litellm_request("opencode/minimax-m2.5")
    assert options == {"api_base": models.OPENCODE_API_BASE}


def test_login_restriction_is_rejected_and_environment_restored(monkeypatch):
    import asyncio
    import litellm
    from apsara_cli.cli.auth_cli import _verify_key

    monkeypatch.setenv("OPENCODE_API_KEY", "original")
    async def restricted(**kwargs):
        assert kwargs["api_key"] == "candidate"
        raise RuntimeError("OpenCode's free tier can only be used from within OpenCode")
    monkeypatch.setattr(litellm, "acompletion", restricted)
    verdict, detail = asyncio.run(_verify_key("opencode/big-pickle", "OPENCODE_API_KEY", "candidate"))
    assert verdict is False
    assert "provider access restriction" in detail
    assert os.environ["OPENCODE_API_KEY"] == "original"


def test_login_does_not_save_key_when_paid_account_has_no_funds(temp_creds, monkeypatch):
    import asyncio
    import litellm
    from apsara_cli.cli import auth_cli

    monkeypatch.setattr(auth_cli, "_prompt_choice", lambda ui, providers: providers.index("opencode"))
    monkeypatch.setattr(auth_cli, "default_model_for_provider", lambda provider: "opencode/minimax-m2.5")
    monkeypatch.setattr(auth_cli.getpass, "getpass", lambda prompt: "candidate")
    async def unfunded(**kwargs):
        assert kwargs["model"] == "openai/minimax-m2.5"
        raise RuntimeError("Upstream request failed: Insufficient account funds")
    monkeypatch.setattr(litellm, "acompletion", unfunded)
    assert asyncio.run(auth_cli.login()) == 1
    assert auth.stored_providers() == []
    assert "OPENCODE_API_KEY" not in os.environ


# ── Credential store round-trips ──────────────────────────────────────────────

def test_save_and_read_provider_key(temp_creds):
    auth.save_provider_key("openai", api_key="sk-test123", default_model="gpt-4o")

    assert auth.get_provider_key("openai") == "sk-test123"
    assert auth.get_active_provider() == "openai"
    assert auth.get_active_default_model() == "gpt-4o"
    assert auth.stored_providers() == ["openai"]


def test_credentials_file_is_owner_only(temp_creds):
    auth.save_provider_key("openai", api_key="sk-test123")
    mode = (temp_creds / "credentials.json").stat().st_mode & 0o777
    assert mode == 0o600


def test_local_provider_needs_no_key(temp_creds):
    auth.save_provider_key("ollama", default_model="ollama/llama3.2")
    assert auth.get_provider_key("ollama") is None
    assert auth.get_active_provider() == "ollama"
    # A local model is always considered "available".
    assert auth.credentials_present_for_model("ollama/llama3.2") is True


def test_set_active_provider_requires_stored(temp_creds):
    auth.save_provider_key("openai", api_key="sk-a")
    auth.save_provider_key("groq", api_key="gsk_b")
    # groq became active on save; switch back to openai.
    assert auth.set_active_provider("openai") is True
    assert auth.get_active_provider() == "openai"
    # Unknown/unstored provider can't be made active.
    assert auth.set_active_provider("anthropic") is False


def test_remove_provider_key(temp_creds):
    auth.save_provider_key("openai", api_key="sk-a")
    auth.save_provider_key("groq", api_key="gsk_b")
    assert auth.get_active_provider() == "groq"

    # Removing the active provider moves the pointer to a remaining one.
    assert auth.remove_provider_key("groq") is True
    assert auth.get_active_provider() == "openai"
    assert auth.stored_providers() == ["openai"]

    # Removing a provider that isn't stored reports False.
    assert auth.remove_provider_key("groq") is False

    # Removing the last provider clears the active pointer.
    assert auth.remove_provider_key("openai") is True
    assert auth.get_active_provider() is None


def test_clear_credentials(temp_creds):
    auth.save_provider_key("openai", api_key="sk-a")
    auth.clear_credentials()
    assert auth.stored_providers() == []
    assert auth.get_active_provider() is None


# ── Environment injection + gating ────────────────────────────────────────────

def test_apply_credentials_to_env_fills_missing_key(temp_creds, monkeypatch):
    auth.save_provider_key("openai", api_key="sk-fromstore")
    assert os.environ.get("OPENAI_API_KEY") is None
    auth.apply_credentials_to_env()
    assert os.environ["OPENAI_API_KEY"] == "sk-fromstore"


def test_apply_credentials_does_not_override_existing_env(temp_creds, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-shell")
    auth.save_provider_key("openai", api_key="sk-fromstore")
    auth.apply_credentials_to_env()
    # An explicitly-set env var wins over the stored key.
    assert os.environ["OPENAI_API_KEY"] == "sk-from-shell"


def test_credentials_present_for_model(temp_creds):
    # No key stored → the OpenAI model is gated.
    assert auth.credentials_present_for_model("gpt-4o") is False
    auth.save_provider_key("openai", api_key="sk-test")
    # After saving, the key is injected and the model is allowed.
    assert auth.credentials_present_for_model("gpt-4o") is True


def test_corrupt_credentials_file_is_ignored(temp_creds):
    (temp_creds / "credentials.json").write_text("not valid json {", encoding="utf-8")
    assert auth.load_credentials() == {}
    assert auth.stored_providers() == []
