"""Tests for the LLM layer (engine/llm.py).

Auth gating and streaming-chunk aggregation are covered offline by patching
`credentials_present_for_model` and `litellm.acompletion`. No network calls are made.
"""
import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import litellm

from apsara_cli.engine import llm


def _run(coro):
    return asyncio.run(coro)


def _drain(async_gen):
    async def _collect():
        return [event async for event in async_gen]

    return asyncio.run(_collect())


def test_estimate_request_tokens_positive():
    messages = [{"role": "user", "content": "hello world"}]
    tokens = llm.estimate_request_tokens(messages, model="gpt-3.5-turbo")
    assert isinstance(tokens, int)
    assert tokens >= 1


def test_call_llm_requires_auth():
    with patch("apsara_cli.cli.auth.credentials_present_for_model", return_value=False):
        message, usage = _run(llm.call_llm([{"role": "user", "content": "hi"}]))
    assert isinstance(message, dict)
    assert "error" in message
    assert "start `apsara`" in message["error"].lower()
    assert usage == {}


def test_llm_call_timeout_is_bounded(monkeypatch):
    monkeypatch.delenv("APSARA_LLM_CALL_TIMEOUT", raising=False)
    assert llm.llm_call_timeout() == llm.DEFAULT_LLM_CALL_TIMEOUT
    monkeypatch.setenv("APSARA_LLM_CALL_TIMEOUT", "1")
    assert llm.llm_call_timeout() == 30
    monkeypatch.setenv("APSARA_LLM_CALL_TIMEOUT", "900")
    assert llm.llm_call_timeout() == 600
    monkeypatch.setenv("APSARA_LLM_CALL_TIMEOUT", "invalid")
    assert llm.llm_call_timeout() == llm.DEFAULT_LLM_CALL_TIMEOUT


def test_non_streaming_llm_call_reports_provider_timeout():
    async def never_returns(**_kwargs):
        await asyncio.sleep(1)

    with patch("apsara_cli.cli.auth.credentials_present_for_model", return_value=True), \
         patch.object(litellm, "acompletion", never_returns), \
         patch.object(llm, "llm_call_timeout", return_value=0.01):
        message, usage = _run(llm.call_llm([{"role": "user", "content": "hi"}]))

    assert "timed out after 0.01 seconds" in message["error"]
    assert usage == {}


def test_summary_reports_provider_timeout():
    async def never_returns(**_kwargs):
        await asyncio.sleep(1)

    with patch.object(litellm, "acompletion", never_returns), \
         patch.object(llm, "llm_call_timeout", return_value=0.01):
        summary, usage = _run(llm.summarize_messages_with_usage([
            {"role": "user", "content": "hi"}
        ]))

    assert "timed out after 0.01 seconds" in summary
    assert usage == {}


def test_call_llm_routes_default_through_opencode(monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "zen-test-key")
    request = {}
    response = SimpleNamespace(
        choices=[SimpleNamespace(message="ok")],
        usage=None,
    )

    async def fake_acompletion(**kwargs):
        request.update(kwargs)
        return response

    with patch.object(litellm, "acompletion", fake_acompletion):
        message, usage = _run(llm.call_llm([{"role": "user", "content": "hi"}]))

    assert message == "ok"
    assert usage == {}
    assert request["model"] == "openai/space-bunny-free"
    assert request["api_base"] == "https://opencode.ai/zen/v1"
    assert request["api_key"] == "zen-test-key"


def test_call_llm_stream_requires_auth():
    with patch("apsara_cli.cli.auth.credentials_present_for_model", return_value=False):
        events = _drain(llm.call_llm_stream([{"role": "user", "content": "hi"}]))
    assert len(events) == 1
    assert events[0]["type"] == "stream_error"
    assert "start `apsara`" in events[0]["error"].lower()


def _chunk(content=None, tool_calls=None, usage=None):
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(delta=delta)
    chunk = SimpleNamespace(choices=[choice])
    if usage is not None:
        chunk.usage = SimpleNamespace(model_dump=lambda: usage)
    return chunk


def _usage_only_chunk(usage):
    return SimpleNamespace(
        choices=[],
        usage=SimpleNamespace(model_dump=lambda: usage),
    )


def _tc(index, call_id=None, name=None, arguments=None):
    function = SimpleNamespace(name=name, arguments=arguments)
    return SimpleNamespace(index=index, id=call_id, function=function)


def test_call_llm_stream_aggregates_text_and_tool_calls():
    chunks = [
        _chunk(content="Hel"),
        _chunk(content="lo"),
        _chunk(tool_calls=[_tc(0, call_id="c1", name="read_file", arguments='{"path":')]),
        _chunk(tool_calls=[_tc(0, arguments='"a.txt"}')]),
        _usage_only_chunk({"total_tokens": 42}),
    ]

    request = {}

    async def fake_acompletion(**kwargs):
        request.update(kwargs)
        async def gen():
            for c in chunks:
                yield c
        return gen()

    with patch("apsara_cli.cli.auth.credentials_present_for_model", return_value=True), \
         patch.object(litellm, "acompletion", fake_acompletion):
        events = _drain(llm.call_llm_stream([{"role": "user", "content": "read a.txt"}]))

    text_chunks = [e for e in events if e["type"] == "text_chunk"]
    assert "".join(e["content"] for e in text_chunks) == "Hello"

    done = next(e for e in events if e["type"] == "stream_done")
    assert done["content"] == "Hello"
    assert done["usage"] == {"total_tokens": 42}
    assert request["stream_options"] == {"include_usage": True}
    assert done["tool_calls"] is not None
    assert len(done["tool_calls"]) == 1

    tool_call = done["tool_calls"][0]
    assert tool_call["id"] == "c1"
    assert tool_call["function"]["name"] == "read_file"
    # Arguments streamed across two deltas are concatenated.
    assert tool_call["function"]["arguments"] == '{"path":"a.txt"}'


def test_rate_limit_headers_are_normalized_without_other_headers():
    response = SimpleNamespace(_hidden_params={"additional_headers": {
        "llm_provider-x-ratelimit-remaining-requests": "42",
        "x-ratelimit-remaining-tokens": "9000",
        "llm_provider-x-ratelimit-reset-requests": "2s",
        "llm_provider-authorization": "secret",
    }})

    assert llm._rate_limits_from_response(response) == {
        "remaining_requests": "42",
        "remaining_tokens": "9000",
        "reset": "2s",
    }


def test_call_llm_stream_retries_on_rate_limit():
    attempts = {"n": 0}

    async def flaky_acompletion(**kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise litellm.RateLimitError("slow down", llm_provider="test", model="m")

        async def gen():
            yield _chunk(content="ok")
        return gen()

    # Patch sleep so the retry delay doesn't actually block the test.
    async def no_sleep(_seconds):
        return None

    with patch("apsara_cli.cli.auth.credentials_present_for_model", return_value=True), \
         patch.object(litellm, "acompletion", flaky_acompletion), \
         patch("apsara_cli.engine.llm.asyncio.sleep", no_sleep):
        events = _drain(llm.call_llm_stream([{"role": "user", "content": "hi"}]))

    types = [e["type"] for e in events]
    assert "retry_notice" in types
    assert attempts["n"] == 2  # failed once, then succeeded
    done = next(e for e in events if e["type"] == "stream_done")
    assert done["content"] == "ok"


# ── malformed tool calls (provider rejection) ────────────────────────────────

def test_detects_malformed_tool_call_errors():
    # Groq's "tool_use_failed" trips the SDK's numeric error-code parser.
    assert llm._is_malformed_tool_call(
        ValueError("invalid literal for int() with base 10: 'tool_use_failed'")
    )
    assert llm._is_malformed_tool_call(Exception("failed_generation: ..."))
    assert not llm._is_malformed_tool_call(ValueError("something else entirely"))


def test_malformed_tool_call_is_retried_then_succeeds():
    attempts = {"n": 0}

    async def flaky_acompletion(**kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ValueError("invalid literal for int() with base 10: 'tool_use_failed'")

        async def gen():
            yield _chunk(content="recovered")
        return gen()

    async def no_sleep(_seconds):
        return None

    with patch("apsara_cli.cli.auth.credentials_present_for_model", return_value=True), \
         patch.object(litellm, "acompletion", flaky_acompletion), \
         patch("apsara_cli.engine.llm.asyncio.sleep", no_sleep):
        events = _drain(llm.call_llm_stream([{"role": "user", "content": "hi"}]))

    assert attempts["n"] == 2
    done = next(e for e in events if e["type"] == "stream_done")
    assert done["content"] == "recovered"


def test_persistent_malformed_tool_call_gives_an_actionable_error():
    async def always_fails(**kwargs):
        raise ValueError("invalid literal for int() with base 10: 'tool_use_failed'")

    async def no_sleep(_seconds):
        return None

    with patch("apsara_cli.cli.auth.credentials_present_for_model", return_value=True), \
         patch.object(litellm, "acompletion", always_fails), \
         patch("apsara_cli.engine.llm.asyncio.sleep", no_sleep):
        events = _drain(llm.call_llm_stream([{"role": "user", "content": "hi"}]))

    error = next(e for e in events if e["type"] == "stream_error")
    assert "int()" not in error["error"], "must not leak the raw parser error"
    assert "tool call" in error["error"]
    assert "stronger model" in error["error"]
