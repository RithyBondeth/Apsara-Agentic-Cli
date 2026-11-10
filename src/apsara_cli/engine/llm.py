"""
LLM module using LiteLLM for provider abstraction.
Supports 100+ LLM providers through a unified interface.
"""
import json
import asyncio
from typing import Any, AsyncGenerator
import litellm
from apsara_cli.engine.models import DEFAULT_MODEL, resolve_litellm_request
from apsara_cli.engine.tools import get_request_tools
from apsara_cli.engine.model_capabilities import model_capabilities, completion_limit, compatibility_error

import os as _os
# Silence huggingface_hub's unauthenticated-request warning triggered by
# litellm's tokenizer downloads — it's noise in the chat UI.
_os.environ.setdefault("HF_HUB_VERBOSITY", "error")
litellm.suppress_debug_info = True
litellm.return_response_headers = True
# Max tokens per completion. Kept generous enough to avoid truncating
# multi-step tool calls and longer code responses; override per-call if needed.
DEFAULT_MAX_COMPLETION_TOKENS = 4096
DEFAULT_LLM_CALL_TIMEOUT = 180.0

_RETRY_DELAYS = [5, 15, 30]


def llm_call_timeout() -> float:
    """Bound one provider request without letting project files raise the limit."""
    raw = _os.environ.get("APSARA_LLM_CALL_TIMEOUT")
    if not raw:
        return DEFAULT_LLM_CALL_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_LLM_CALL_TIMEOUT
    return max(30.0, min(value, 600.0))


def _rate_limits_from_response(value: Any) -> dict[str, str]:
    """Extract only non-sensitive rate-limit headers from a LiteLLM response."""
    hidden = getattr(value, "_hidden_params", {}) or {}
    headers = hidden.get("additional_headers", {}) if isinstance(hidden, dict) else {}
    if not isinstance(headers, dict):
        return {}
    lowered = {str(k).lower(): str(v) for k, v in headers.items() if v is not None}

    def find(*suffixes: str) -> str | None:
        for suffix in suffixes:
            for key, item in lowered.items():
                if key == suffix or key.endswith("-" + suffix):
                    return item
        return None

    result = {
        "remaining_requests": find(
            "x-ratelimit-remaining-requests", "ratelimit-remaining-requests",
            "anthropic-ratelimit-requests-remaining",
        ),
        "remaining_tokens": find(
            "x-ratelimit-remaining-tokens", "ratelimit-remaining-tokens",
            "anthropic-ratelimit-tokens-remaining",
        ),
        "limit_requests": find("x-ratelimit-limit-requests", "ratelimit-limit-requests"),
        "limit_tokens": find("x-ratelimit-limit-tokens", "ratelimit-limit-tokens"),
        "reset": find(
            "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens",
            "anthropic-ratelimit-requests-reset", "anthropic-ratelimit-tokens-reset",
        ),
        "retry_after": find("retry-after"),
    }
    return {key: item for key, item in result.items() if item is not None}


def estimate_request_tokens(messages: list[dict], model: str = DEFAULT_MODEL, *, with_tools: bool = True) -> int:
    try:
        resolved_model, _provider_options = resolve_litellm_request(model)
        options = {"tools": get_request_tools(), "tool_choice": "auto"} if with_tools else {}
        return litellm.token_counter(model=resolved_model, messages=messages, **options)
    except Exception:
        return max(
            1,
            len(json.dumps({"messages": messages, "tools": get_request_tools() if with_tools else []}, ensure_ascii=False, default=str)) // 3,
        )


async def summarize_messages(messages: list[dict], model: str = DEFAULT_MODEL) -> str:
    """
    Summarize a list of messages into a concise paragraph.
    """
    summary, _usage = await summarize_messages_with_usage(messages, model=model)
    return summary


async def summarize_messages_with_usage(
    messages: list[dict], model: str = DEFAULT_MODEL
) -> tuple[str, dict[str, Any]]:
    """Summarize messages and return provider usage for session accounting."""
    summary_prompt = (
        "You are an assistant helping to manage conversation history. "
        "Summarize the following conversation turns into a single concise paragraph. "
        "Focus on the technical problems discussed, the actions taken by the agent, and the current state of the task. "
        "Do not include pleasantries. Be extremely concise."
    )
    summary_messages = [
        {"role": "system", "content": summary_prompt},
        {"role": "user", "content": json.dumps(messages)},
    ]

    try:
        resolved_model, provider_options = resolve_litellm_request(model)
        response = await asyncio.wait_for(
            litellm.acompletion(
                model=resolved_model,
                messages=summary_messages,
                max_tokens=completion_limit(model, 300),
                **provider_options,
            ),
            timeout=llm_call_timeout(),
        )
        usage = response.usage.model_dump() if response.usage else {}
        if usage:
            usage = dict(usage)
            usage.update({
                "apsara_model": model,
                "provider_reported_calls": 1,
                "auxiliary_calls": 1,
            })
        else:
            usage = {
                "apsara_model": model,
                "estimated_input_tokens": estimate_request_tokens(summary_messages, model=model),
                "unreported_calls": 1,
                "auxiliary_calls": 1,
            }
        return response.choices[0].message.content.strip(), usage
    except asyncio.TimeoutError:
        return (
            f"[Summary failed: Provider response timed out after "
            f"{llm_call_timeout():g} seconds.]",
            {},
        )
    except Exception as e:
        return f"[Summary failed: {e}]", {}


def _is_malformed_tool_call(exc: Exception) -> bool:
    """Detect a provider rejecting the model's tool call.

    Groq returns `"code": "tool_use_failed"`; the OpenAI-compatible error parser
    expects that field to be numeric and raises `ValueError: invalid literal for
    int()` instead of a useful error, so match on both shapes.
    """
    text = str(exc)
    return "tool_use_failed" in text or "failed_generation" in text


async def call_llm(
    messages: list[dict], model: str = DEFAULT_MODEL, *, with_tools: bool = True,
    max_completion_tokens: int = DEFAULT_MAX_COMPLETION_TOKENS,
) -> tuple[Any, Any]:
    """
    Send the conversation to LLM with configured tools via LiteLLM.
    Returns (Response Message Object, Usage Dictionary Object)
    """
    from apsara_cli.cli.auth import credentials_present_for_model
    if not credentials_present_for_model(model):
        return {"error": f"No API key found for model '{model}'. Start `apsara` and add it when prompted."}, {}

    try:
        model_capabilities(model).require(tools=with_tools, streaming=False)
        resolved_model, provider_options = resolve_litellm_request(model)
        request_options: dict[str, Any] = {}
        if with_tools:
            request_options.update(tools=get_request_tools(), tool_choice="auto")
        response = await asyncio.wait_for(
            litellm.acompletion(
                model=resolved_model,
                messages=messages,
                max_tokens=completion_limit(model, max_completion_tokens),
                **request_options,
                **provider_options,
            ),
            timeout=llm_call_timeout(),
        )
        return response.choices[0].message, response.usage.model_dump() if response.usage else {}
    except asyncio.TimeoutError:
        return {
            "error": f"Provider response timed out after {llm_call_timeout():g} seconds."
        }, {}
    except Exception as e:
        return {"error": compatibility_error(e)}, {}


async def call_llm_stream(
    messages: list[dict], model: str = DEFAULT_MODEL, *, tools: list[dict] | None = None,
    max_completion_tokens: int = DEFAULT_MAX_COMPLETION_TOKENS,
) -> AsyncGenerator[dict, None]:
    """
    Streaming LLM call with automatic retry on rate-limit errors.
    Yields dicts:
      {"type": "retry_notice", "delay": int, "attempt": int}
      {"type": "text_chunk", "content": str}
      {"type": "stream_done", "content": str, "tool_calls": list|None, "usage": dict}
      {"type": "stream_error", "error": str}
    """
    from apsara_cli.cli.auth import credentials_present_for_model
    if not credentials_present_for_model(model):
        yield {"type": "stream_error", "error": f"No API key found for model '{model}'. Start `apsara` and add it when prompted."}
        return

    for attempt in range(len(_RETRY_DELAYS) + 1):
        try:
            model_capabilities(model).require(tools=True, streaming=True)
            resolved_model, provider_options = resolve_litellm_request(model)
            response = await litellm.acompletion(
                model=resolved_model,
                messages=messages,
                tools=get_request_tools() if tools is None else tools,
                tool_choice="auto",
                max_tokens=completion_limit(model, max_completion_tokens),
                stream=True,
                stream_options={"include_usage": True},
                **provider_options,
            )

            content_parts: list[str] = []
            tool_calls_acc: dict[int, dict] = {}
            usage: dict = {}
            rate_limits = _rate_limits_from_response(response)

            async for chunk in response:
                chunk_limits = _rate_limits_from_response(chunk)
                if chunk_limits:
                    rate_limits.update(chunk_limits)
                # With include_usage, OpenAI-compatible providers commonly
                # send a final usage-only chunk whose choices list is empty.
                if hasattr(chunk, "usage") and chunk.usage:
                    try:
                        usage = chunk.usage.model_dump()
                    except Exception:
                        pass
                choice = chunk.choices[0] if chunk.choices else None
                if not choice:
                    continue
                delta = choice.delta

                if delta.content:
                    content_parts.append(delta.content)
                    yield {"type": "text_chunk", "content": delta.content}

                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in tool_calls_acc:
                            tool_calls_acc[idx] = {
                                "id": "",
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            }
                        if tc.id:
                            tool_calls_acc[idx]["id"] = tc.id
                        if tc.function:
                            if tc.function.name:
                                tool_calls_acc[idx]["function"]["name"] += tc.function.name
                            if tc.function.arguments:
                                tool_calls_acc[idx]["function"]["arguments"] += tc.function.arguments

            yield {
                "type": "stream_done",
                "content": "".join(content_parts),
                "tool_calls": list(tool_calls_acc.values()) if tool_calls_acc else None,
                "usage": usage,
                "rate_limits": rate_limits,
            }
            return

        except litellm.RateLimitError as e:
            if attempt < len(_RETRY_DELAYS):
                delay = _RETRY_DELAYS[attempt]
                yield {"type": "retry_notice", "delay": delay, "attempt": attempt + 1}
                await asyncio.sleep(delay)
            else:
                yield {"type": "stream_error", "error": str(e)}
                return

        except litellm.AuthenticationError as e:
            yield {
                "type": "stream_error",
                "error": str(e),
                "auth_error": True,
            }
            return

        except Exception as e:
            if _is_malformed_tool_call(e):
                # The model emitted a tool call the provider rejected. Groq
                # reports this as code "tool_use_failed", which the SDK's error
                # parser then chokes on, so the surfaced exception is a bare
                # ValueError about int() — useless on its own. It's usually
                # transient, so retry once before giving up.
                if attempt < len(_RETRY_DELAYS):
                    yield {"type": "retry_notice", "delay": 1, "attempt": attempt + 1}
                    await asyncio.sleep(1)
                    continue
                yield {
                    "type": "stream_error",
                    "error": (
                        "The model produced a tool call the provider rejected. This "
                        "usually means the model struggled with the tool schemas — try "
                        "a stronger model, or reduce the number of connected MCP servers."
                    ),
                }
                return
            yield {"type": "stream_error", "error": compatibility_error(e)}
            return
