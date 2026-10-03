"""Tests for the core agent execution loop (engine/executor.py).

These are fully offline: the LLM stream and tool execution are patched, so we
exercise the loop's control flow (tool dispatch, final-answer handling, error
abort, and loop/stuck detection) without any network or real tool side effects.
"""
import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from apsara_cli.engine import executor


def _scripted_llm(scripts):
    """Return a fake call_llm_stream that yields each script's events per call.

    `scripts` is a list of event-lists; call N replays scripts[N] (the last
    script repeats if the loop makes more calls than scripts provided).
    """
    state = {"calls": 0}

    async def _fake(messages, model):
        idx = min(state["calls"], len(scripts) - 1)
        state["calls"] += 1
        for event in scripts[idx]:
            yield event

    return _fake, state


def _run(conversation):
    """Drive run_agent_stream to completion, returning parsed event dicts."""
    async def _collect():
        return [json.loads(chunk) async for chunk in executor.run_agent_stream(conversation)]

    return asyncio.run(_collect())


def _tool_call_event(content="", name="read_file", arguments='{"path": "a.txt"}', call_id="c1", usage=None):
    return {
        "type": "stream_done",
        "content": content,
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": arguments},
        }],
        "usage": usage or {},
    }


def _final_event(content="All done.", usage=None):
    return {"type": "stream_done", "content": content, "tool_calls": None, "usage": usage or {}}


def _verification(arguments):
    phase = arguments.get("phase", "full")
    return "Verification passed.\n" + json.dumps({"phase": phase, "status": "passed", "results": [
        {"command": ["pytest", "-q"], "status": "passed", "returncode": 0},
    ]})


def test_direct_final_answer():
    fake, _ = _scripted_llm([[_final_event("The answer is 42.")]])
    with patch.object(executor, "call_llm_stream", fake):
        events = _run([{"role": "user", "content": "hi"}])

    types = [e["type"] for e in events]
    assert "final_answer" in types
    final = next(e for e in events if e["type"] == "final_answer")
    assert final["content"] == "The answer is 42."


def test_empty_provider_responses_are_retried_before_completion():
    empty = _final_event("")
    fake, state = _scripted_llm([[empty], [empty], [_final_event("Recovered.")]])
    with patch.object(executor, "call_llm_stream", fake):
        events = _run([{"role": "user", "content": "finish the task"}])

    retries = [
        event for event in events
        if event["type"] == "status" and "empty response" in event.get("message", "")
    ]
    assert len(retries) == 2
    assert events[-1] == {"type": "final_answer", "content": "Recovered."}
    assert state["calls"] == 3


def test_repeated_empty_provider_responses_fail_instead_of_completing():
    fake, state = _scripted_llm([[_final_event("")]])
    with patch.object(executor, "call_llm_stream", fake):
        events = _run([{"role": "user", "content": "finish the task"}])

    assert events[-1]["type"] == "error"
    assert "empty response repeatedly" in events[-1]["message"]
    assert not any(event["type"] == "final_answer" for event in events)
    assert state["calls"] == executor.MAX_EMPTY_RESPONSE_RETRIES + 1


def test_provider_request_timeout_is_retried_once_before_completion():
    calls = {"count": 0}

    async def slow_then_recover(_messages, _model):
        calls["count"] += 1
        if calls["count"] == 1:
            await asyncio.sleep(1)
        yield _final_event("Recovered after timeout.")

    with patch.object(executor, "call_llm_stream", slow_then_recover), \
         patch.object(executor, "llm_call_timeout", return_value=0.01):
        events = _run([{"role": "user", "content": "finish the task"}])

    assert any(
        event["type"] == "status" and "retrying once" in event.get("message", "")
        for event in events
    )
    assert events[-1] == {
        "type": "final_answer",
        "content": "Recovered after timeout.",
    }
    assert calls["count"] == 2


def test_repeated_provider_request_timeout_fails_the_turn():
    calls = {"count": 0}

    async def always_slow(_messages, _model):
        calls["count"] += 1
        await asyncio.sleep(1)
        yield _final_event("too late")

    with patch.object(executor, "call_llm_stream", always_slow), \
         patch.object(executor, "llm_call_timeout", return_value=0.01):
        events = _run([{"role": "user", "content": "finish the task"}])

    assert events[-1]["type"] == "error"
    assert "timed out" in events[-1]["message"]
    assert calls["count"] == executor.MAX_PROVIDER_TIMEOUT_RETRIES + 1


def test_streamed_text_emits_response_end_not_final_answer():
    script = [
        {"type": "text_chunk", "content": "Think"},
        {"type": "text_chunk", "content": "ing..."},
        _final_event("Thinking..."),
    ]
    fake, _ = _scripted_llm([script])
    with patch.object(executor, "call_llm_stream", fake):
        events = _run([{"role": "user", "content": "hi"}])

    types = [e["type"] for e in events]
    assert "response_start" in types
    assert types.count("text_chunk") == 2
    assert "response_end" in types
    assert "final_answer" not in types


def test_tool_call_then_final_answer():
    scripts = [[_tool_call_event(usage={"total_tokens": 10})], [_final_event("Read it.")]]
    fake, state = _scripted_llm(scripts)
    with patch.object(executor, "call_llm_stream", fake), \
         patch.object(executor, "execute_tool_async", AsyncMock(return_value="file contents")) as exec_mock:
        events = _run([{"role": "user", "content": "read a.txt"}])

    # Tool was dispatched with parsed arguments.
    exec_mock.assert_called_once_with("read_file", {"path": "a.txt"})

    tool_call = next(e for e in events if e["type"] == "tool_call")
    assert tool_call["arguments"] == {"path": "a.txt"}

    tool_result = next(e for e in events if e["type"] == "tool_result")
    assert tool_result["result"] == "file contents"

    # usage event surfaced from the first step
    assert any(e["type"] == "usage" and e["data"]["total_tokens"] == 10 for e in events)

    # Ended on a final answer after the tool round-trip (2 LLM calls total).
    assert events[-1]["type"] == "final_answer"
    assert state["calls"] == 2


def test_stream_error_aborts():
    fake, state = _scripted_llm([[{"type": "stream_error", "error": "boom"}]])
    with patch.object(executor, "call_llm_stream", fake):
        events = _run([{"role": "user", "content": "hi"}])

    assert events[-1]["type"] == "error"
    assert "boom" in events[-1]["message"]
    assert state["calls"] == 1  # aborted immediately, no further steps


def test_stream_error_falls_back_before_output(monkeypatch):
    monkeypatch.setenv("APSARA_FALLBACK_MODELS", "ollama/llama3.2")
    seen = []

    async def fake(messages, model):
        seen.append(model)
        if model == executor.DEFAULT_MODEL:
            yield {"type": "stream_error", "error": "primary unavailable"}
        else:
            yield _final_event("Recovered.")

    with patch.object(executor, "call_llm_stream", fake):
        events = _run([{"role": "user", "content": "hi"}])

    assert seen == [executor.DEFAULT_MODEL, "ollama/llama3.2"]
    assert events[-1]["type"] == "final_answer"
    assert any("falling back" in e.get("message", "") for e in events)


def test_free_model_never_falls_back_to_paid_or_unknown(monkeypatch):
    monkeypatch.setenv("APSARA_FALLBACK_MODELS", "gpt-4o,custom/maybe-paid")
    seen = []

    async def fake(messages, model):
        seen.append(model)
        yield {"type": "stream_error", "error": "primary unavailable"}

    with patch.object(executor, "call_llm_stream", fake):
        events = _run([{"role": "user", "content": "hi"}])

    assert seen == [executor.DEFAULT_MODEL]
    assert events[-1]["type"] == "error"
    assert not any("falling back" in e.get("message", "") for e in events)
    assert any("Skipped paid or unknown" in e.get("message", "") for e in events)


def test_usage_event_identifies_the_model_that_generated_it():
    fake, _ = _scripted_llm([[_final_event("Done.", usage={"total_tokens": 12})]])
    with patch.object(executor, "call_llm_stream", fake):
        events = _run([{"role": "user", "content": "hi"}])

    usage = next(e["data"] for e in events if e["type"] == "usage")
    assert usage["apsara_model"] == executor.DEFAULT_MODEL
    assert usage["provider_reported_calls"] == 1


def test_missing_provider_usage_is_kept_as_separate_estimate():
    fake, _ = _scripted_llm([[_final_event("Done.")]])
    with patch.object(executor, "call_llm_stream", fake), \
         patch.object(executor, "estimate_request_tokens", return_value=321):
        events = _run([{"role": "user", "content": "hi"}])

    usage = next(e["data"] for e in events if e["type"] == "usage")
    assert usage["estimated_input_tokens"] == 321
    assert usage["unreported_calls"] == 1
    assert usage.get("total_tokens", 0) == 0


def test_repeated_erroring_tool_calls_trigger_blocked():
    # Every LLM turn returns the identical failing tool call.
    fake, state = _scripted_llm([[_tool_call_event()]])
    with patch.object(executor, "call_llm_stream", fake), \
         patch.object(executor, "execute_tool_async", AsyncMock(return_value="Error: nope")):
        events = _run([{"role": "user", "content": "loop"}])

    assert any(e["type"] == "blocked" for e in events)
    # The guardrail must break the loop well before the 15-step ceiling.
    assert state["calls"] < 15


# ── step budget ───────────────────────────────────────────────────────────────

def test_exhausted_step_budget_is_announced(monkeypatch):
    """Running out of steps must not look like a completed turn."""
    monkeypatch.setenv("APSARA_MAX_STEPS", "3")

    # Every turn asks for a different tool call, so no stuck-detector fires.
    scripts = [
        [_tool_call_event(name="read_file", arguments=f'{{"path": "f{i}.txt"}}', call_id=f"c{i}")]
        for i in range(6)
    ]
    fake, state = _scripted_llm(scripts)
    with patch.object(executor, "call_llm_stream", fake), \
         patch.object(executor, "execute_tool_async", AsyncMock(return_value="ok")):
        events = _run([{"role": "user", "content": "big job"}])

    assert state["calls"] == 3, "must stop at the configured budget"
    assert events[-1]["type"] == "blocked"
    assert "all 3 steps" in events[-1]["message"]
    assert "APSARA_MAX_STEPS" in events[-1]["message"]


def test_step_budget_is_configurable(monkeypatch):
    monkeypatch.setenv("APSARA_MAX_STEPS", "2")
    assert executor._max_steps() == 2
    monkeypatch.setenv("APSARA_MAX_STEPS", "not-a-number")
    assert executor._max_steps() == executor.DEFAULT_MAX_STEPS
    monkeypatch.delenv("APSARA_MAX_STEPS")
    assert executor._max_steps() == executor.DEFAULT_MAX_STEPS


def test_step_budget_is_clamped(monkeypatch):
    monkeypatch.setenv("APSARA_MAX_STEPS", "9999")
    assert executor._max_steps() == 100
    monkeypatch.setenv("APSARA_MAX_STEPS", "0")
    assert executor._max_steps() == 1


def test_completed_turn_has_no_budget_warning():
    fake, _ = _scripted_llm([[_final_event("Done.")]])
    with patch.object(executor, "call_llm_stream", fake):
        events = _run([{"role": "user", "content": "hi"}])
    assert not any(e["type"] == "blocked" for e in events)


def test_first_mutation_requires_a_baseline_attempt():
    scripts = [
        [_tool_call_event(name="write_to_file", arguments='{"path":"a.py","content":"x"}')],
        [_final_event("Stopped.")],
    ]
    fake, _ = _scripted_llm(scripts)
    with patch.object(executor, "call_llm_stream", fake), \
         patch.object(executor, "execute_tool_async", AsyncMock(return_value="wrote")) as execute:
        events = _run([{"role": "user", "content": "change a.py"}])

    execute.assert_not_called()
    result = next(event["result"] for event in events if event["type"] == "tool_result")
    assert "phase=baseline" in result


def test_generic_bash_success_does_not_replace_full_verification():
    scripts = [
        [_tool_call_event(name="verify_project", arguments='{"phase":"baseline"}', call_id="baseline")],
        [_tool_call_event(name="write_to_file", arguments='{"path":"a.py","content":"x"}', call_id="write")],
        [_tool_call_event(name="run_bash_command", arguments='{"command":"echo ok"}', call_id="bash")],
        [_final_event("Done without structured verification.")],
        [_tool_call_event(name="verify_project", arguments='{"phase":"full"}', call_id="full")],
        [_final_event("Verified.")],
    ]
    fake, state = _scripted_llm(scripts)

    async def execute(name, _arguments):
        if name == "verify_project":
            return _verification(_arguments)
        return "ok"

    with patch.object(executor, "call_llm_stream", fake), \
         patch.object(executor, "execute_tool_async", execute):
        events = _run([{"role": "user", "content": "change and test"}])

    assert state["calls"] == 6
    assert events[-1]["type"] == "final_answer"
    assert events[-1]["content"] == "Verified."
    assert any(event.get("state") == "verifying" for event in events)


def test_multi_file_change_requires_read_only_critic_after_full_verification():
    scripts = [
        [_tool_call_event(name="verify_project", arguments='{"phase":"baseline"}', call_id="baseline")],
        [_tool_call_event(name="write_to_file", arguments='{"path":"a.py","content":"a"}', call_id="a")],
        [_tool_call_event(name="write_to_file", arguments='{"path":"b.py","content":"b"}', call_id="b")],
        [_tool_call_event(name="verify_project", arguments='{"phase":"full"}', call_id="full")],
        [_final_event("Ready without review.")],
        [_tool_call_event(name="request_critic", arguments='{"objective":"change two files"}', call_id="critic")],
        [_final_event("Verified and reviewed.")],
    ]
    fake, state = _scripted_llm(scripts)
    calls = []

    async def execute(name, arguments):
        calls.append((name, arguments))
        if name == "verify_project":
            return _verification(arguments)
        if name == "request_critic":
            return "APPROVED"
        return "ok"

    with patch.object(executor, "call_llm_stream", fake), \
         patch.object(executor, "execute_tool_async", execute):
        events = _run([{"role": "user", "content": "change two files"}])

    assert state["calls"] == 7
    critic_arguments = next(arguments for name, arguments in calls if name == "request_critic")
    assert critic_arguments["_changed_files"] == ["a.py", "b.py"]
    assert any("independent review" in event.get("message", "") for event in events)
    assert events[-1]["content"] == "Verified and reviewed."


def test_mutation_after_full_verification_requires_fresh_full_verification():
    scripts = [
        [_tool_call_event(name="verify_project", arguments='{"phase":"baseline"}', call_id="baseline")],
        [_tool_call_event(name="write_to_file", arguments='{"path":"a.py","content":"one"}', call_id="write-1")],
        [_tool_call_event(name="verify_project", arguments='{"phase":"full"}', call_id="full-1")],
        [_tool_call_event(name="write_to_file", arguments='{"path":"a.py","content":"two"}', call_id="write-2")],
        [_final_event("Done with stale verification.")],
        [_tool_call_event(name="verify_project", arguments='{"phase":"full"}', call_id="full-2")],
        [_final_event("Done with fresh verification.")],
    ]
    fake, state = _scripted_llm(scripts)

    async def execute(name, _arguments):
        return _verification(_arguments) if name == "verify_project" else "ok"

    with patch.object(executor, "call_llm_stream", fake), \
         patch.object(executor, "execute_tool_async", execute):
        events = _run([{"role": "user", "content": "change a file twice"}])

    assert state["calls"] == 7
    assert events[-1]["content"] == "Done with fresh verification."
    assert sum(event.get("state") == "verifying" for event in events) == 1


# ── cycle detection ───────────────────────────────────────────────────────────

def test_alternating_tool_calls_are_detected_as_a_loop():
    """A,B,A,B is as stuck as A,A,A — the consecutive check alone misses it."""
    scripts = [
        [_tool_call_event(name="read_file", arguments='{"path": "a.txt"}', call_id="a")],
        [_tool_call_event(name="write_to_file", arguments='{"path": "b.txt"}', call_id="b")],
        [_tool_call_event(name="read_file", arguments='{"path": "a.txt"}', call_id="a")],
        [_tool_call_event(name="write_to_file", arguments='{"path": "b.txt"}', call_id="b")],
        [_tool_call_event(name="read_file", arguments='{"path": "a.txt"}', call_id="a")],
    ]
    fake, state = _scripted_llm(scripts)
    with patch.object(executor, "call_llm_stream", fake), \
         patch.object(executor, "execute_tool_async", AsyncMock(return_value="ok")):
        events = _run([{"role": "user", "content": "loop"}])

    assert any(e["type"] == "blocked" for e in events)
    assert state["calls"] < executor.DEFAULT_MAX_STEPS, (
        "must break out before exhausting the step budget"
    )


def test_distinct_tool_calls_are_not_treated_as_a_loop():
    scripts = [
        [_tool_call_event(name="read_file", arguments=f'{{"path": "f{i}.txt"}}', call_id=f"c{i}")]
        for i in range(4)
    ] + [[_final_event("Read them all.")]]
    fake, _ = _scripted_llm(scripts)
    with patch.object(executor, "call_llm_stream", fake), \
         patch.object(executor, "execute_tool_async", AsyncMock(return_value="ok")):
        events = _run([{"role": "user", "content": "read several"}])

    assert not any(e["type"] == "blocked" for e in events)
    assert events[-1]["type"] == "final_answer"


# ── tool error detection ──────────────────────────────────────────────────────

def test_file_containing_the_word_error_is_not_a_tool_failure():
    """Reading a log file must not count toward the consecutive-error abort."""
    log_contents = "2026-01-01 WARN retrying\n2026-01-01 Error: connection refused\n"
    scripts = [
        [_tool_call_event(name="read_file", arguments=f'{{"path": "log{i}.txt"}}', call_id=f"c{i}")]
        for i in range(4)
    ] + [[_final_event("The log shows a connection error.")]]

    fake, _ = _scripted_llm(scripts)
    with patch.object(executor, "call_llm_stream", fake), \
         patch.object(executor, "execute_tool_async", AsyncMock(return_value=log_contents)):
        events = _run([{"role": "user", "content": "read the logs"}])

    assert not any(e["type"] == "blocked" for e in events), (
        "tool results merely containing 'Error:' must not trip the error abort"
    )
    assert events[-1]["type"] == "final_answer"


def test_real_tool_errors_still_abort():
    fake, state = _scripted_llm([
        [_tool_call_event(name="read_file", arguments=f'{{"path": "x{i}.txt"}}', call_id=f"c{i}")]
        for i in range(6)
    ])
    with patch.object(executor, "call_llm_stream", fake), \
         patch.object(executor, "execute_tool_async", AsyncMock(return_value="Error reading file: nope")):
        events = _run([{"role": "user", "content": "read"}])

    assert any(e["type"] == "blocked" for e in events)
    # 3 errors -> corrective nudge -> 3 more -> stop. Still far short of the
    # 25-step budget, which is the property that matters.
    assert state["calls"] <= 8, "must abort well before the step budget"


def test_rerunning_a_command_with_changing_output_is_not_a_loop():
    """The verification loop: run tests, fix, run again. Not cycling."""
    run_tests = _tool_call_event(
        name="run_bash_command", arguments='{"command": "pytest -q"}', call_id="t"
    )
    edit = _tool_call_event(
        name="edit_file", arguments='{"path": "calc.py"}', call_id="e"
    )
    baseline = _tool_call_event(name="verify_project", arguments='{"phase":"baseline"}', call_id="baseline")
    full = _tool_call_event(name="verify_project", arguments='{"phase":"full"}', call_id="full")
    scripts = [[baseline], [run_tests], [edit], [run_tests], [edit], [run_tests], [full], [_final_event("Green.")]]
    fake, _ = _scripted_llm(scripts)

    # Same command, different output each time — progress, not repetition.
    results = iter(["1 failed", "1 failed, 1 passed", "2 passed"])

    async def changing(name, args):
        if name == "verify_project":
            return _verification(args)
        return next(results, "") if name == "run_bash_command" else "edited"

    with patch.object(executor, "call_llm_stream", fake), \
         patch.object(executor, "execute_tool_async", changing):
        events = _run([{"role": "user", "content": "fix the tests"}])

    assert not any(e["type"] == "blocked" for e in events), (
        "re-running tests with changing results must not count as a loop"
    )
    assert events[-1]["type"] == "final_answer"


def test_rerunning_a_command_with_identical_output_is_a_loop():
    run_tests = _tool_call_event(
        name="run_bash_command", arguments='{"command": "pytest -q"}', call_id="t"
    )
    edit = _tool_call_event(name="edit_file", arguments='{"path": "x.py"}', call_id="e")
    baseline = _tool_call_event(name="verify_project", arguments='{"phase":"baseline"}', call_id="baseline")
    scripts = [[baseline], [run_tests], [edit], [run_tests], [edit], [run_tests], [edit], [run_tests]]
    fake, _ = _scripted_llm(scripts)

    async def unchanged(name, args):
        return _verification(args) if name == "verify_project" else "1 failed"
    with patch.object(executor, "call_llm_stream", fake), \
         patch.object(executor, "execute_tool_async", unchanged):
        events = _run([{"role": "user", "content": "fix the tests"}])

    assert any(e["type"] == "blocked" for e in events), (
        "identical command AND identical output means genuinely stuck"
    )


# ── corrective nudge before giving up ─────────────────────────────────────────

def test_a_loop_is_redirected_before_being_abandoned():
    """A stuck model usually just needs telling. Nudge, then let it recover."""
    stuck = _tool_call_event(name="read_file", arguments='{"path": "a.txt"}', call_id="s")
    scripts = [[stuck], [stuck], [stuck], [_final_event("Recovered, here's the answer.")]]
    fake, _ = _scripted_llm(scripts)

    with patch.object(executor, "call_llm_stream", fake), \
         patch.object(executor, "execute_tool_async", AsyncMock(return_value="same output")):
        events = _run([{"role": "user", "content": "go"}])

    assert any(
        e["type"] == "status" and "redirecting" in e.get("message", "").lower()
        for e in events
    ), "should announce the redirect"
    assert not any(e["type"] == "blocked" for e in events), (
        "a model that recovers after the nudge must not be reported as stuck"
    )
    assert events[-1]["type"] == "final_answer"


def test_persistent_loop_is_still_stopped():
    stuck = _tool_call_event(name="read_file", arguments='{"path": "a.txt"}', call_id="s")
    fake, state = _scripted_llm([[stuck]])

    with patch.object(executor, "call_llm_stream", fake), \
         patch.object(executor, "execute_tool_async", AsyncMock(return_value="same output")):
        events = _run([{"role": "user", "content": "go"}])

    assert events[-1]["type"] == "blocked"
    assert "even after changing course" in events[-1]["message"]
    assert state["calls"] < executor.DEFAULT_MAX_STEPS


def test_nudge_names_the_specific_repeated_tool():
    """A generic 'try something else' is much weaker than naming the action."""
    captured = {}

    async def capture_llm(messages, model):
        captured["messages"] = list(messages)
        yield _final_event("done")

    stuck = _tool_call_event(name="glob_search", arguments='{"pattern": "*.py"}', call_id="s")
    scripts = [[stuck], [stuck], [stuck]]
    fake, _ = _scripted_llm(scripts)

    calls = {"n": 0}

    async def llm(messages, model):
        calls["n"] += 1
        if calls["n"] <= 3:
            async for e in fake(messages, model):
                yield e
        else:
            captured["messages"] = list(messages)
            yield _final_event("done")

    with patch.object(executor, "call_llm_stream", llm), \
         patch.object(executor, "execute_tool_async", AsyncMock(return_value="no matches")):
        _run([{"role": "user", "content": "go"}])

    nudges = [
        m for m in captured["messages"]
        if m.get("role") == "system" and "STOP AND RECONSIDER" in (m.get("content") or "")
    ]
    assert nudges, "the corrective message must reach the model"
    assert "glob_search" in nudges[0]["content"], "should name the repeated tool"


def test_nudge_includes_the_error_when_failing():
    captured = {}
    failing = _tool_call_event(name="read_file", arguments='{"path": "x.txt"}', call_id="f")
    calls = {"n": 0}

    async def llm(messages, model):
        calls["n"] += 1
        if calls["n"] <= 3:
            yield failing
        else:
            captured["messages"] = list(messages)
            yield _final_event("done")

    with patch.object(executor, "call_llm_stream", llm), \
         patch.object(executor, "execute_tool_async",
                      AsyncMock(return_value="Error reading file: no such file zzz")):
        _run([{"role": "user", "content": "go"}])

    nudges = [
        m for m in captured.get("messages", [])
        if m.get("role") == "system" and "STOP AND RECONSIDER" in (m.get("content") or "")
    ]
    assert nudges
    assert "no such file zzz" in nudges[0]["content"], "should quote the actual error"


def test_only_one_nudge_per_turn():
    """The nudge must not become its own loop."""
    stuck = _tool_call_event(name="read_file", arguments='{"path": "a.txt"}', call_id="s")
    fake, _ = _scripted_llm([[stuck]])

    with patch.object(executor, "call_llm_stream", fake), \
         patch.object(executor, "execute_tool_async", AsyncMock(return_value="same")):
        events = _run([{"role": "user", "content": "go"}])

    redirects = [
        e for e in events
        if e["type"] == "status" and "redirecting" in e.get("message", "").lower()
    ]
    assert len(redirects) == 1, f"expected exactly one nudge, got {len(redirects)}"
