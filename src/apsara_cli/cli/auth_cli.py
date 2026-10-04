"""Interactive bring-your-own-key setup: choose a provider, enter your API key."""
import getpass
import os
from pathlib import Path
from typing import Optional

from apsara_cli.cli.auth import (
    CREDENTIALS_PATH,
    clear_credentials,
    get_active_provider,
    save_provider_key,
    stored_providers,
)
from apsara_cli.engine.models import (
    KEY_HINTS,
    default_model_for_provider,
    format_context_window,
    models_for_provider,
    model_price_label,
    lookup_model,
    provider_env_var,
    providers_in_order,
    resolve_litellm_request,
    validate_key_format,
)
from apsara_cli.shared.ui import ConsoleUI
from apsara_cli.engine.model_capabilities import compatibility_error

CREDENTIALS_DISPLAY = str(CREDENTIALS_PATH).replace(str(Path.home()), "~", 1)


def _prompt_choice(ui: ConsoleUI, providers: list[str]) -> Optional[int]:
    """Render the provider menu and read a 1-based selection. None on cancel."""
    ui.print_line()
    ui.info("Choose your AI model provider")
    ui.print_line(ui.dim("You'll use your own API key — Apsara never sees or stores it remotely."))
    ui.print_line()

    active = get_active_provider()
    for idx, provider in enumerate(providers, 1):
        env_var = provider_env_var(provider)
        default_model = default_model_for_provider(provider) or provider
        models = models_for_provider(provider)
        ctx = format_context_window(models[0].context_window) if models else "?"
        need = "local, no key" if env_var is None else f"key: {env_var}"
        marker = ui.style(" (current)", "38;2;120;200;150") if provider == active else ""
        ui.print_line(f"  {idx}. {provider}{marker}")
        ui.print_line(ui.dim(f"       {default_model}  ·  {ctx} ctx  ·  {need}  ·  {model_price_label(default_model)}"))

    ui.print_line()
    try:
        raw = input("  Select provider number: ").strip()
    except (EOFError, KeyboardInterrupt):
        ui.print_line()
        return None
    if not raw:
        return None
    if not raw.isdigit() or not (1 <= int(raw) <= len(providers)):
        ui.error(f"Please enter a number between 1 and {len(providers)}.")
        return None
    return int(raw) - 1


async def _verify_key(model: str, env_var: str, api_key: str) -> tuple[Optional[bool], str]:
    """Best-effort live check. Returns (True ok, False rejected, None inconclusive)."""
    import litellm

    previous = os.environ.get(env_var)
    os.environ[env_var] = api_key
    try:
        resolved_model, provider_options = resolve_litellm_request(model)
        await litellm.acompletion(
            model=resolved_model,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
            timeout=30,
            **provider_options,
        )
        return True, ""
    except litellm.AuthenticationError as exc:
        return False, str(exc)
    except Exception as exc:  # network/rate-limit/etc. — can't confirm, don't block
        detail = compatibility_error(exc)
        if detail.startswith(("OpenCode provider access restriction:", "Provider account has insufficient credits")):
            return False, detail
        return None, detail
    finally:
        if previous is None:
            os.environ.pop(env_var, None)
        else:
            os.environ[env_var] = previous


async def login() -> int:
    ui = ConsoleUI(use_color=True, auto_approve=True)
    providers = providers_in_order()

    choice = _prompt_choice(ui, providers)
    if choice is None:
        ui.info("Setup cancelled.")
        return 0

    provider = providers[choice]
    env_var = provider_env_var(provider)
    default_model = default_model_for_provider(provider)
    if default_model is None:
        ui.error(f"No compatible default model is available for {provider}.")
        return 1
    ui.info(f"Selected model: {default_model} · {model_price_label(default_model)}")
    entry = lookup_model(default_model)
    if provider == "opencode" and entry and entry.tier == "paid":
        ui.info("OpenCode Zen API requests require account credits and are billed at provider rates.")

    # Local providers (ollama) need no key.
    if env_var is None:
        save_provider_key(provider, default_model=default_model)
        ui.print_line()
        ui.success(f"Configured local provider '{provider}'. No API key required.")
        ui.info(f"Default model: {default_model}. Make sure Ollama is running locally.")
        return 0

    hint = KEY_HINTS.get(env_var)
    ui.print_line()
    ui.info(f"Enter your {provider} API key")
    if hint and hint[1]:
        ui.print_line(ui.dim(f"  {hint[1]}"))
    try:
        api_key = getpass.getpass("  API key (input hidden): ").strip()
    except (EOFError, KeyboardInterrupt):
        ui.print_line()
        ui.error("Setup aborted.")
        return 1

    if not api_key:
        ui.error("API key cannot be empty.")
        return 1

    looks_valid, message = validate_key_format(env_var, api_key)
    if not looks_valid:
        ui.warning(f"That key doesn't match the expected format. {message}")
        ui.print_line(ui.dim("  Continuing anyway — validating with the provider..."))

    ui.status(f"Verifying key with {provider}...")
    verdict, detail = await _verify_key(default_model, env_var, api_key)

    if verdict is False:
        ui.error(f"The provider rejected access to {default_model}: {detail}")
        ui.info("Nothing was saved. Check model access and credentials, then try again.")
        return 1

    save_provider_key(provider, api_key=api_key, default_model=default_model)
    ui.print_line()
    if verdict is True:
        ui.success(f"Verified and saved your {provider} key.")
    else:
        ui.warning("Could not reach the provider to verify (offline?). Saved the key anyway.")
    ui.info(f"Default model: {default_model}")
    ui.print_line(ui.dim(f"  Stored securely in {CREDENTIALS_DISPLAY}"))
    return 0


def logout() -> int:
    ui = ConsoleUI(use_color=True, auto_approve=True)
    if not stored_providers():
        ui.info("No stored credentials to clear.")
        return 0
    clear_credentials()
    ui.success("Cleared all stored provider API keys.")
    return 0
