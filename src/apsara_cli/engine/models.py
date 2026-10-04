"""
Model registry for Apsara Agentic CLI.

Each entry describes a supported model: its LiteLLM model_id, display name,
provider, context window, pricing tier, required env var, and optional aliases
that users can type as shortcuts in /model <name>.

Tiers
  free  — provider offers a free tier (e.g. Groq, Gemini free quota)
  paid  — requires a paid API key
  local — runs locally via Ollama, no key needed
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from typing import Optional


DEFAULT_MODEL = "opencode/space-bunny-free"
OPENCODE_API_BASE = "https://opencode.ai/zen/v1"


@dataclass
class ModelEntry:
    model_id: str           # LiteLLM model string, e.g. "groq/llama-3.3-70b-versatile"
    display_name: str       # Human-friendly name shown in /models
    provider: str           # Grouping key, e.g. "groq", "openai"
    tier: str               # "free" | "paid" | "local"
    context_window: int     # Max tokens (input + output)
    env_var: Optional[str]  # Primary env var needed, None for local
    notes: str              # One-line description shown in /models
    aliases: list[str] = field(default_factory=list)  # Short names, e.g. ["sonnet"]
    input_cost_per_million: Optional[float] = None
    output_cost_per_million: Optional[float] = None
    cache_read_cost_per_million: Optional[float] = None
    cache_write_cost_per_million: Optional[float] = None
    lifecycle: str = "active"  # active | deprecated | retiring | retired
    shutdown_date: Optional[str] = None
    replacement: Optional[str] = None
    supports_tools: bool = True
    supports_streaming: bool = True
    promotional_pricing: bool = False
    pricing_verified_on: Optional[str] = None
    pricing_source_url: Optional[str] = None
    access_restriction: Optional[str] = None


# ── Registry ──────────────────────────────────────────────────────────────────

MODELS: list[ModelEntry] = [
    # ── OpenCode Zen ───────────────────────────────────────────────────────
    ModelEntry(
        model_id=DEFAULT_MODEL,
        display_name="Space Bunny Free",
        provider="opencode",
        tier="free",
        # Published models.dev metadata; Apsara still caps input at 128k.
        context_window=1_048_576,
        env_var="OPENCODE_API_KEY",
        notes="Temporary free coding model; streaming/tool-call probe passed 2026-10-03",
        aliases=["zen", "space-bunny", "bunny"],
        input_cost_per_million=0.0,
        output_cost_per_million=0.0,
        cache_read_cost_per_million=0.0,
        promotional_pricing=True,
        pricing_verified_on="2026-10-03",
        pricing_source_url="https://opencode.ai/docs/zen/",
    ),
    ModelEntry(
        model_id="opencode/minimax-m2.5",
        display_name="MiniMax M2.5 (OpenCode Zen)",
        provider="opencode",
        tier="paid",
        context_window=204_800,
        env_var="OPENCODE_API_KEY",
        notes="Paid Zen model; deprecated by OpenCode",
        aliases=["minimax", "minimax-m2.5"],
        input_cost_per_million=0.30,
        output_cost_per_million=1.20,
        cache_read_cost_per_million=0.06,
        lifecycle="deprecated",
        replacement="opencode/minimax-m2.7",
        pricing_verified_on="2026-10-03",
        pricing_source_url="https://opencode.ai/docs/zen/",
    ),
    ModelEntry(
        model_id="opencode/big-pickle",
        display_name="Big Pickle",
        provider="opencode",
        tier="free",
        context_window=200_000,
        env_var="OPENCODE_API_KEY",
        notes="Reasoning coding model; free temporarily (avoid confidential code)",
        aliases=["big-pickle", "pickle"],
        input_cost_per_million=0.0,
        output_cost_per_million=0.0,
        promotional_pricing=True,
        pricing_verified_on="2026-08-09",
        pricing_source_url="https://opencode.ai/docs/zen",
        access_restriction=(
            "Big Pickle's free tier rejected Apsara requests because it requires the OpenCode client. "
            "Choose opencode/space-bunny-free, or another supported provider with --model or /model. "
            "Changing the API key alone may not resolve this restriction."
        ),
    ),

    # ── Groq (free tier — fastest hosted inference) ───────────────────────────
    ModelEntry(
        model_id="groq/openai/gpt-oss-120b",
        display_name="GPT OSS 120B",
        provider="groq",
        tier="free",
        context_window=131_072,
        env_var="GROQ_API_KEY",
        notes="Groq-hosted open-weight reasoning and coding model",
        aliases=["gpt-oss", "oss-120b"],
    ),
    ModelEntry(
        model_id="groq/llama-3.3-70b-versatile",
        display_name="Llama 3.3 70B",
        provider="groq",
        tier="free",
        context_window=128_000,
        env_var="GROQ_API_KEY",
        notes="Fast, general-purpose — default model",
        aliases=["llama", "llama70b", "llama-70b"],
        lifecycle="retiring",
        shutdown_date="2026-08-16",
        replacement="groq/openai/gpt-oss-120b",
    ),
    ModelEntry(
        model_id="groq/deepseek-r1-distill-llama-70b",
        display_name="DeepSeek R1 Distill 70B",
        provider="groq",
        tier="free",
        context_window=128_000,
        env_var="GROQ_API_KEY",
        notes="Reasoning model, high speed via Groq",
        aliases=["r1-groq", "deepseek-r1-groq"],
        lifecycle="retired",
        shutdown_date="2025-10-02",
    ),

    # ── OpenAI (paid) ─────────────────────────────────────────────────────────
    ModelEntry(
        model_id="gpt-4o",
        display_name="GPT-4o",
        provider="openai",
        tier="paid",
        context_window=128_000,
        env_var="OPENAI_API_KEY",
        notes="OpenAI flagship multimodal model",
        aliases=["4o"],
    ),
    ModelEntry(
        model_id="gpt-4o-mini",
        display_name="GPT-4o Mini",
        provider="openai",
        tier="paid",
        context_window=128_000,
        env_var="OPENAI_API_KEY",
        notes="Affordable and fast GPT-4o",
        aliases=["4o-mini", "gpt-mini"],
    ),
    ModelEntry(
        model_id="o3-mini",
        display_name="o3-mini",
        provider="openai",
        tier="paid",
        context_window=200_000,
        env_var="OPENAI_API_KEY",
        notes="OpenAI o3 reasoning model",
        aliases=["o3mini", "o3"],
    ),

    # ── Anthropic (paid) ──────────────────────────────────────────────────────
    ModelEntry(
        model_id="anthropic/claude-3-5-sonnet-20241022",
        display_name="Claude 3.5 Sonnet",
        provider="anthropic",
        tier="paid",
        context_window=200_000,
        env_var="ANTHROPIC_API_KEY",
        notes="Anthropic's best balanced model for coding",
        aliases=["sonnet", "claude-sonnet", "claude"],
        input_cost_per_million=3.0,
        output_cost_per_million=15.0,
        cache_read_cost_per_million=0.30,
        cache_write_cost_per_million=3.75,
        lifecycle="deprecated",
    ),
    ModelEntry(
        model_id="anthropic/claude-3-5-haiku-20241022",
        display_name="Claude 3.5 Haiku",
        provider="anthropic",
        tier="paid",
        context_window=200_000,
        env_var="ANTHROPIC_API_KEY",
        notes="Fast and affordable Claude",
        aliases=["haiku", "claude-haiku"],
        input_cost_per_million=0.80,
        output_cost_per_million=4.0,
        cache_read_cost_per_million=0.08,
        cache_write_cost_per_million=1.0,
        lifecycle="deprecated",
    ),

    # ── Google Gemini (free quota + paid) ─────────────────────────────────────
    ModelEntry(
        model_id="gemini/gemini-2.0-flash",
        display_name="Gemini 2.0 Flash",
        provider="google",
        tier="free",
        context_window=1_000_000,
        env_var="GEMINI_API_KEY",
        notes="Latest Gemini Flash, 1M context, free quota",
        aliases=["gemini-flash", "flash", "gemini2"],
        lifecycle="retired",
        shutdown_date="2026-06-01",
        replacement="gemini/gemini-2.5-flash",
    ),
    ModelEntry(
        model_id="gemini/gemini-1.5-pro",
        display_name="Gemini 1.5 Pro",
        provider="google",
        tier="paid",
        context_window=2_000_000,
        env_var="GEMINI_API_KEY",
        notes="Massive 2M context window, advanced reasoning",
        aliases=["gemini-pro", "pro"],
        input_cost_per_million=1.25,
        output_cost_per_million=5.0,
        cache_read_cost_per_million=0.3125,
        cache_write_cost_per_million=1.25,
        lifecycle="deprecated",
    ),

    # ── Mistral (paid but affordable) ─────────────────────────────────────────
    ModelEntry(
        model_id="mistral/codestral-latest",
        display_name="Codestral",
        provider="mistral",
        tier="paid",
        context_window=256_000,
        env_var="MISTRAL_API_KEY",
        notes="Mistral's code-specialist model",
        aliases=["codestral"],
    ),

    # ── DeepSeek (very affordable) ────────────────────────────────────────────
    ModelEntry(
        model_id="deepseek/deepseek-chat",
        display_name="DeepSeek V3",
        provider="deepseek",
        tier="paid",
        context_window=64_000,
        env_var="DEEPSEEK_API_KEY",
        notes="High quality, extremely low cost",
        aliases=["deepseek", "deepseek-v3"],
    ),
    ModelEntry(
        model_id="deepseek/deepseek-reasoner",
        display_name="DeepSeek R1",
        provider="deepseek",
        tier="paid",
        context_window=64_000,
        env_var="DEEPSEEK_API_KEY",
        notes="DeepSeek reasoning model",
        aliases=["r1", "deepseek-r1"],
    ),

    # ── Ollama (local — no key required) ─────────────────────────────────────
    ModelEntry(
        model_id="ollama/llama3.2",
        display_name="Llama 3.2 (local)",
        provider="ollama",
        tier="local",
        context_window=128_000,
        env_var=None,
        notes="Runs locally via Ollama, no API key",
        aliases=["ollama-llama", "local-llama"],
    ),
    ModelEntry(
        model_id="ollama/qwen2.5-coder:7b",
        display_name="Qwen 2.5 Coder 7B (local)",
        provider="ollama",
        tier="local",
        context_window=32_000,
        env_var=None,
        notes="Code-specialist model, runs locally",
        aliases=["qwen-coder", "local-coder"],
    ),
]


# ── Look-up helpers ───────────────────────────────────────────────────────────

def _alias_map() -> dict[str, ModelEntry]:
    m: dict[str, ModelEntry] = {}
    for entry in MODELS:
        m[entry.model_id.lower()] = entry
        for alias in entry.aliases:
            m[alias.lower()] = entry
    return m


_ALIAS_MAP: dict[str, ModelEntry] | None = None


def lookup_model(name: str) -> Optional[ModelEntry]:
    """Return the ModelEntry for an exact model_id or any registered alias."""
    global _ALIAS_MAP
    if _ALIAS_MAP is None:
        _ALIAS_MAP = _alias_map()
    return _ALIAS_MAP.get(name.strip().lower())


def resolve_model_id(name: str) -> str:
    """
    If *name* is a registered alias, return the canonical model_id.
    Otherwise return *name* unchanged (allows arbitrary LiteLLM strings).
    """
    entry = lookup_model(name)
    return entry.model_id if entry else name


def model_lifecycle(entry: ModelEntry, today: date | None = None) -> str:
    """Return lifecycle status, enforcing a registered shutdown date."""
    if entry.shutdown_date:
        try:
            if (today or date.today()) >= date.fromisoformat(entry.shutdown_date):
                return "retired"
        except ValueError:
            pass
    return entry.lifecycle


def model_availability(entry: ModelEntry, today: date | None = None) -> tuple[bool, str]:
    """Whether Apsara can safely select this model, plus a user-facing reason."""
    if entry.access_restriction:
        return False, entry.access_restriction
    lifecycle = model_lifecycle(entry, today)
    replacement = f" Use {entry.replacement} instead." if entry.replacement else ""
    if lifecycle == "retired":
        date_note = f" on {entry.shutdown_date}" if entry.shutdown_date else ""
        return False, f"{entry.display_name} was retired{date_note}.{replacement}"
    if not entry.supports_streaming or not entry.supports_tools:
        missing = []
        if not entry.supports_streaming:
            missing.append("streaming")
        if not entry.supports_tools:
            missing.append("tool calling")
        return False, f"{entry.display_name} does not support {' and '.join(missing)} required by Apsara."
    if lifecycle == "retiring":
        return True, f"{entry.display_name} retires on {entry.shutdown_date}.{replacement}"
    if lifecycle == "deprecated":
        return True, f"{entry.display_name} is deprecated.{replacement}"
    return True, ""


def model_usage_cost(
    model: str, prompt_tokens: int, completion_tokens: int
) -> Optional[float]:
    """Return known provider cost, or None when pricing is not registered.

    Apsara never invents a blended token price. Local models have no provider
    token charge, and Big Pickle's explicit zero pricing is stored above.
    """
    from apsara_cli.engine.pricing import usage_cost

    return usage_cost(model, {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    })


def model_price_label(model: str) -> str:
    """Short honest pricing label for model-selection surfaces."""
    entry = lookup_model(model)
    if entry is None:
        return "pricing unknown"
    if entry.tier == "local":
        return "$0 provider cost"
    if entry.input_cost_per_million == 0 and entry.output_cost_per_million == 0:
        return "$0"
    if entry.tier == "paid":
        from apsara_cli.engine.pricing import pricing_for_model

        prices, _source = pricing_for_model(model)
        if prices:
            return (
                f"${prices['input'] * 1_000_000:g}/${prices['output'] * 1_000_000:g} "
                "per 1M in/out"
            )
        return "paid · pricing unavailable"
    return "free quota · provider limits apply"


# ── Key hints ────────────────────────────────────────────────────────────────
# (env_var → (expected_prefix_or_None, human_description))

KEY_HINTS: dict[str, tuple[Optional[str], str]] = {
    "OPENCODE_API_KEY":  (None,      "OpenCode Zen API key — no fixed prefix"),
    "OPENAI_API_KEY":     ("sk-",     "OpenAI keys start with  sk-"),
    "ANTHROPIC_API_KEY":  ("sk-ant-", "Anthropic keys start with  sk-ant-"),
    "GROQ_API_KEY":       ("gsk_",    "Groq keys start with  gsk_"),
    "GEMINI_API_KEY":     ("AI",      "Google AI keys start with  AI"),
    "MISTRAL_API_KEY":    (None,      "Mistral API key — no fixed prefix"),
    "DEEPSEEK_API_KEY":   ("sk-",     "DeepSeek keys start with  sk-"),
}


def resolve_litellm_request(model: str) -> tuple[str, dict[str, str]]:
    """Return the LiteLLM model ID and provider-specific request options."""
    canonical_model = resolve_model_id(model)
    if canonical_model.startswith("opencode/"):
        options = {
            "api_base": os.environ.get("OPENCODE_API_BASE", OPENCODE_API_BASE),
        }
        api_key = os.environ.get("OPENCODE_API_KEY")
        if api_key:
            options["api_key"] = api_key
        return "openai/" + canonical_model.split("/", 1)[1], options
    return canonical_model, {}


def validate_key_format(env_var: str, value: str) -> tuple[bool, str]:
    """
    Returns (looks_valid, hint_message).
    Always True when no prefix pattern is known for this key.
    """
    hint = KEY_HINTS.get(env_var)
    if not hint or not hint[0]:
        return True, ""
    prefix, description = hint
    if not value.startswith(prefix):
        return False, f"Expected format: {description}"
    return True, description


def env_vars_with_providers() -> list[tuple[str, list[str]]]:
    """Return [(env_var, [provider, ...]), ...] for all unique env vars in MODELS."""
    seen: dict[str, list[str]] = {}
    for entry in MODELS:
        if entry.env_var:
            if entry.env_var not in seen:
                seen[entry.env_var] = []
            if entry.provider not in seen[entry.env_var]:
                seen[entry.env_var].append(entry.provider)
    return list(seen.items())


def is_key_available(entry: ModelEntry) -> bool:
    """True if the model's required env var is set (or no key is needed)."""
    if entry.env_var is None:
        return True
    return bool(os.environ.get(entry.env_var))


def format_context_window(tokens: int) -> str:
    if tokens >= 1_000_000:
        return f"{tokens // 1_000_000}M"
    if tokens >= 1_000:
        return f"{tokens // 1_000}k"
    return str(tokens)


def providers_in_order() -> list[str]:
    """Return provider names in the order they first appear in MODELS."""
    seen: list[str] = []
    for e in MODELS:
        if e.provider not in seen:
            seen.append(e.provider)
    return seen


def models_for_provider(provider: str) -> list[ModelEntry]:
    """Return all registered models belonging to *provider* (in registry order)."""
    return [e for e in MODELS if e.provider == provider]


def provider_env_var(provider: str) -> Optional[str]:
    """Primary API-key env var for *provider*, or None for local providers (e.g. ollama)."""
    models = models_for_provider(provider)
    return models[0].env_var if models else None


def default_model_for_provider(provider: str) -> Optional[str]:
    """The first active compatible model for *provider*, or None if unavailable."""
    models = models_for_provider(provider)
    return next((entry.model_id for entry in models if model_availability(entry)[0]), None)
