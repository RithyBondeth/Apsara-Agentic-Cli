"""Model compatibility metadata and explicit user-owned capability overrides."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path


@dataclass(frozen=True)
class ModelCapabilities:
    tools: bool | None = None
    streaming: bool | None = None
    context_window: int | None = None
    max_output_tokens: int | None = None
    source: str = "unknown"

    def require(self, *, tools: bool, streaming: bool) -> None:
        missing = [name for name, needed, supported in (
            ("tool calling", tools, self.tools), ("streaming", streaming, self.streaming),
        ) if needed and supported is False]
        if missing:
            raise ValueError(f"Selected model does not support {' and '.join(missing)} required for this request.")


def model_capabilities(model: str) -> ModelCapabilities:
    from apsara_cli.engine.models import lookup_model, resolve_model_id, resolve_litellm_request
    canonical = resolve_model_id(model)
    entry = lookup_model(canonical)
    resolved, _ = resolve_litellm_request(canonical)
    import litellm
    metadata = litellm.model_cost.get(resolved) or litellm.model_cost.get(canonical) or {}
    result = ModelCapabilities(
        tools=entry.supports_tools if entry else metadata.get("supports_function_calling"),
        streaming=entry.supports_streaming if entry else metadata.get("supports_streaming"),
        context_window=entry.context_window if entry else metadata.get("max_input_tokens"),
        max_output_tokens=metadata.get("max_output_tokens"),
        source="registry" if entry else "LiteLLM metadata" if metadata else "unknown",
    )
    path = Path(os.environ.get("APSARA_MODEL_CAPABILITIES", str(Path.home() / ".apsara" / "models.json"))).expanduser()
    if path.exists():
        try:
            models = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(models, dict):
                raise ValueError("Model profiles must be an object keyed by model ID.")
            override = models.get(canonical)
            if override is not None:
                if not isinstance(override, dict):
                    raise ValueError("Model profile must be an object.")
                values = {key: getattr(result, key) for key in ("tools", "streaming", "context_window", "max_output_tokens")}
                for key in values:
                    if key not in override:
                        continue
                    value = override[key]
                    valid = isinstance(value, bool) if key in {"tools", "streaming"} else type(value) is int and value > 0
                    if not valid:
                        raise ValueError(f"Invalid model capability: {key}.")
                    values[key] = value
                if values["context_window"] and values["max_output_tokens"] and values["max_output_tokens"] >= values["context_window"]:
                    raise ValueError("Output limit must be smaller than the context window.")
                return ModelCapabilities(**values, source="user profile")
        except (OSError, ValueError, TypeError) as exc:
            raise ValueError(f"Invalid model capabilities in {path}: {exc}") from exc
    return result


def completion_limit(model: str, requested: int = 4096) -> int:
    capabilities = model_capabilities(model)
    limits = [requested]
    if capabilities.max_output_tokens:
        limits.append(capabilities.max_output_tokens)
    if capabilities.context_window:
        limits.append(max(1, capabilities.context_window // 4))
    return max(1, min(limits))


def compatibility_error(exc: Exception) -> str:
    message = str(exc)
    lowered = message.lower()
    if "opencode" in lowered and "free tier can only be used" in lowered:
        return (
            "OpenCode provider access restriction: this request was rejected because its free tier "
            "requires the OpenCode client. Select a model with supported third-party API access. "
            "Changing the API key alone may not resolve this restriction."
        )
    if any(term in lowered for term in ("tool_choice", "function calling", "does not support tools", "tools is not supported", "unsupported parameter")):
        return f"Model/provider capability mismatch: {message}. Inspect the selected model with apsara doctor --live or configure a user model profile."
    if any(term in lowered for term in ("context length", "maximum context", "context window")):
        return f"Model context limit exceeded: {message}. Configure its actual context window or narrow the request."
    return message
