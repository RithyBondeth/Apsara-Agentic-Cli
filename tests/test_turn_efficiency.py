"""Completion, freshness, and resource-limit regressions using real files."""

import asyncio
import json

import pytest

from apsara_cli.engine import executor
from apsara_cli.engine.budget import TurnBudget
from apsara_cli.engine.evidence import critic_evidence
from apsara_cli.engine.tools import agent_runtime_context
from apsara_cli.engine.runtime import latest_run


def _drive(monkeypatch, root, actions, *, before_request=None, on_tool=None, usage=10):
    calls, requests = [], []

    async def stream(messages, model):
        index = len(requests)
        requests.append(list(messages))
        if before_request:
            before_request(index)
        action = actions[min(index, len(actions) - 1)]
        tools = []
        for offset, (name, arguments) in enumerate(action or []):
            tools.append({"id": f"c{index}-{offset}", "type": "function", "function": {
                "name": name, "arguments": json.dumps(arguments),
            }})
        yield {"type": "stream_done", "content": "Finished." if not tools else "",
               "tool_calls": tools or None, "usage": {"total_tokens": usage}}

    async def execute(name, arguments):
        calls.append((name, arguments))
        if on_tool:
            override = on_tool(name, arguments)
            if override is not None:
                return override
        if name == "verify_project":
            return json.dumps({"phase": arguments["phase"], "status": "passed", "results": [
                {"command": ["pytest"], "status": "passed", "returncode": 0},
            ]})
        if name == "write_to_file":
            (root / arguments["path"]).write_text(arguments["content"])
            return "Wrote file."
        if name == "request_critic":
            return '{"verdict":"approved","findings":[]}'
        return (root / arguments["path"]).read_text()

    monkeypatch.setattr(executor, "call_llm_stream", stream)
    monkeypatch.setattr(executor, "execute_tool_async", execute)
    monkeypatch.setattr(executor, "estimate_request_tokens", lambda *args, **kwargs: 100)
    async def collect():
        with agent_runtime_context(workspace_root=root):
            return [json.loads(e) async for e in executor.run_agent_stream([
                {"role": "user", "content": "Repair the implementation."},
            ])]
    events = asyncio.run(collect())
    return events, calls, requests


BASE = [("verify_project", {"phase": "baseline"})]
WRITE = [("write_to_file", {"path": "a.py", "content": "fixed"})]
FULL = [("verify_project", {"phase": "full"})]


def test_reuses_passing_checks_then_guides_final_completion(monkeypatch, tmp_path):
    events, calls, requests = _drive(monkeypatch, tmp_path, [BASE, WRITE, FULL, FULL, None])
    assert sum(name == "verify_project" and args["phase"] == "full" for name, args in calls) == 1
    assert events[-1]["type"] == "final_answer"
    assert latest_run(tmp_path)["state"] == "completed_verified"
    assert latest_run(tmp_path)["budget"]["reused_checks"] == 1
    progress = next(m["content"] for m in requests[-1] if m["content"].startswith(executor.PROGRESS_PREFIX))
    assert "return the final answer now" in progress


def test_external_edit_invalidates_reusable_checks(monkeypatch, tmp_path):
    def edit(index):
        if index == 3:
            (tmp_path / "a.py").write_text("external update")
    events, calls, _ = _drive(monkeypatch, tmp_path, [BASE, WRITE, FULL, FULL,
        [("request_critic", {})], None], before_request=edit)
    assert sum(name == "verify_project" and args["phase"] == "full" for name, args in calls) == 2
    assert latest_run(tmp_path)["state"] == "completed_verified"


def test_verification_policy_change_requires_fresh_evidence(monkeypatch, tmp_path):
    def edit(index):
        if index == 3:
            path = tmp_path / ".apsara/config.toml"
            path.parent.mkdir(exist_ok=True)
            path.write_text('[verification]\ncommands = [["new-check"]]\n')
    events, calls, _ = _drive(monkeypatch, tmp_path, [BASE, WRITE, FULL, None, FULL, None], before_request=edit)
    assert sum(name == "verify_project" and args["phase"] == "full" for name, args in calls) == 2
    assert latest_run(tmp_path)["state"] == "completed_verified"
    assert any(e.get("state") == "verifying" for e in events)


def test_read_cache_is_invalidated_by_content_changes(monkeypatch, tmp_path):
    (tmp_path / "a.py").write_text("old")
    read = [("read_file", {"path": "a.py"})]
    def edit(index):
        if index == 2:
            (tmp_path / "a.py").write_text("new")
    events, calls, _ = _drive(monkeypatch, tmp_path, [read, read, read, FULL,
        [("request_critic", {})], None], before_request=edit)
    assert sum(name == "read_file" for name, args in calls) == 2
    assert [e["result"] for e in events if e["type"] == "tool_result" and e["name"] == "read_file"] == ["old", "old", "new"]


def test_tool_limit_stops_a_batch_before_unapproved_extra_work(monkeypatch, tmp_path):
    monkeypatch.setenv("APSARA_MAX_TOOL_CALLS", "2")
    (tmp_path / "a.py").write_text("unchanged")
    reads = [("read_file", {"path": "a.py"})] * 3
    events, calls, _ = _drive(monkeypatch, tmp_path, [reads])
    assert sum(e["type"] == "tool_call" for e in events) == 2
    assert events[-1]["type"] == "blocked"
    assert latest_run(tmp_path)["budget"]["tool_calls_used"] == 2


def test_token_limit_prevents_the_next_provider_request(monkeypatch, tmp_path):
    monkeypatch.setenv("APSARA_MAX_TURN_TOKENS", "5000")
    (tmp_path / "a.py").write_text("unchanged")
    events, calls, requests = _drive(monkeypatch, tmp_path, [[("read_file", {"path": "a.py"})]], usage=4500)
    assert len(requests) == 1
    assert events[-1]["type"] == "blocked"
    assert "cannot fit another request" in events[-1]["message"]


def test_token_overrun_does_not_execute_pending_mutations(monkeypatch, tmp_path):
    monkeypatch.setenv("APSARA_MAX_TURN_TOKENS", "5000")
    events, calls, _ = _drive(monkeypatch, tmp_path, [WRITE], usage=6000)
    assert not calls
    assert not (tmp_path / "a.py").exists()
    assert events[-1]["type"] == "blocked"


def test_budget_keeps_unknown_usage_separate_from_reported_totals():
    budget = TurnBudget(step_limit=25)
    budget.observe_usage({"prompt_tokens": 100, "completion_tokens": 50})
    budget.observe_usage({"estimated_input_tokens": 200, "unreported_calls": 1})
    assert budget.reported_usage == 150
    assert budget.estimated_usage == 200 + 4096
    assert budget.usage_complete is False


@pytest.mark.parametrize("fence", ["json", "JSON", ""])
def test_critic_accepts_a_single_fenced_verdict_without_weakening_review(fence):
    assert critic_evidence(f'```{fence}\n{{"verdict":"approved","findings":[]}}\n```')["verdict"] == "approved"
    assert critic_evidence(f'```{fence}\n{{"verdict":"approved","findings":[{{"description":"Wrong result"}}]}}\n```')["verdict"] == "changes_requested"
    assert critic_evidence('```json\n{"verdict":"approved","findings":[]}\n```\n{"verdict":"changes_requested"}')["verdict"] == "unavailable"


def test_review_uses_original_user_objective(monkeypatch, tmp_path):
    write_b = [("write_to_file", {"path": "b.py", "content": "fixed"})]
    events, calls, requests = _drive(monkeypatch, tmp_path, [BASE, WRITE, write_b, FULL,
        [("request_critic", {"objective": "Only check formatting"})], None])
    assert next(args for name, args in calls if name == "request_critic")["objective"] == "Repair the implementation."
    assert latest_run(tmp_path)["state"] == "completed_verified"


def test_passing_review_is_reused_only_for_the_current_snapshot(monkeypatch, tmp_path):
    write_b = [("write_to_file", {"path": "b.py", "content": "fixed"})]
    review = [("request_critic", {})]
    events, calls, _ = _drive(monkeypatch, tmp_path, [BASE, WRITE, write_b, FULL, review, review, None])
    assert sum(name == "request_critic" for name, args in calls) == 1
    assert latest_run(tmp_path)["state"] == "completed_verified"


def test_live_budget_warning_is_once_per_turn():
    from apsara_cli.shared.ui import ConsoleUI
    from apsara_cli.shared.events import print_event
    from unittest.mock import Mock
    ui = ConsoleUI(use_color=False)
    ui.warning = Mock()
    budget = TurnBudget(step_limit=10, steps_used=8).as_dict()
    print_event({"type": "budget", "data": budget}, ui)
    print_event({"type": "budget", "data": budget}, ui)
    ui.warning.assert_called_once()
    assert ui._run_budget == budget
    ui.begin_turn()
    assert ui._run_budget == {}


def test_critic_budget_preflight_makes_no_billable_request(monkeypatch, tmp_path):
    from apsara_cli.engine.critic import request_critique
    from apsara_cli.engine.budget import _ACTIVE
    from apsara_cli.engine import llm
    from unittest.mock import AsyncMock
    mocked = AsyncMock()
    monkeypatch.setattr(llm, "call_llm", mocked)
    monkeypatch.setattr(llm, "estimate_request_tokens", lambda *args, **kwargs: 100)
    token = _ACTIVE.set(TurnBudget(step_limit=25, usage_limit=5000, reported_usage=4900))
    try:
        content, usage = asyncio.run(request_critique(tmp_path, objective="fix", focus="", model=executor.DEFAULT_MODEL))
    finally:
        _ACTIVE.reset(token)
    mocked.assert_not_called()
    assert "Remaining turn token budget" in content
    assert usage == {"request_skipped": True}


def test_budget_command_works_before_any_model_request(tmp_path):
    from apsara_cli.cli.chat import handle_chat_command
    from apsara_cli.shared.ui import ConsoleUI
    from apsara_cli.shared.types import ResolvedOptions
    from unittest.mock import Mock
    ui = ConsoleUI(use_color=False)
    ui.print_block = Mock()
    options = ResolvedOptions(tmp_path, executor.DEFAULT_MODEL, "test", True, False, None, None, False, False)
    result = handle_chat_command("/budget", [], executor.DEFAULT_MODEL, options, None, ui)
    assert result[0] is True
    assert "Turn token limit: 100,000" in ui.print_block.call_args.args[0]


def test_identical_arguments_with_changing_results_do_not_trigger_false_loop(monkeypatch, tmp_path):
    counter = 0
    def edit(index):
        nonlocal counter
        counter += 1
        (tmp_path / "a.py").write_text(str(counter))
    read = [("read_file", {"path": "a.py"})]
    events, _, _ = _drive(monkeypatch, tmp_path, [read, read, read, FULL, None], before_request=edit)
    assert not any("repeated action" in e.get("message", "").lower() for e in events)


def test_approval_policy_update_is_not_a_source_edit(monkeypatch, tmp_path):
    def approve(name, arguments):
        if name == "verify_project" and arguments["phase"] == "baseline":
            path = tmp_path / ".apsara/config.toml"
            path.parent.mkdir(exist_ok=True)
            path.write_text("# Approved verification policy\n")
    events, _, _ = _drive(monkeypatch, tmp_path, [BASE, None], on_tool=approve)
    assert events[-1]["type"] == "final_answer"
    run = latest_run(tmp_path)
    assert run["state"] == "completed"
    assert run["changed_files"] == []


def test_explicit_fresh_verification_bypasses_reused_evidence(monkeypatch, tmp_path):
    fresh = [("verify_project", {"phase": "full", "fresh": True})]
    events, calls, _ = _drive(monkeypatch, tmp_path, [BASE, WRITE, FULL, fresh, fresh, None])
    assert sum(name == "verify_project" and args["phase"] == "full" for name, args in calls) == 3
    assert events[-1]["type"] == "final_answer"


def test_explicit_fresh_critic_requests_a_new_review(monkeypatch, tmp_path):
    write_b = [("write_to_file", {"path": "b.py", "content": "fixed"})]
    fresh = [("request_critic", {"fresh": True})]
    events, calls, _ = _drive(monkeypatch, tmp_path, [BASE, WRITE, write_b, FULL, fresh, fresh, None])
    assert sum(name == "request_critic" for name, args in calls) == 2
    assert events[-1]["type"] == "final_answer"


def test_reads_outside_the_source_fingerprint_are_not_reused(monkeypatch, tmp_path):
    path = tmp_path / ".apsara/local-note.txt"
    path.parent.mkdir(exist_ok=True)
    path.write_text("before")
    def edit(index):
        if index == 1:
            path.write_text("after")
    read = [("read_file", {"path": ".apsara/local-note.txt"})]
    events, calls, _ = _drive(monkeypatch, tmp_path, [read, read, None], before_request=edit)
    assert sum(name == "read_file" for name, args in calls) == 2
    assert [e["result"] for e in events if e["type"] == "tool_result"] == ["before", "after"]


def test_local_plugins_disable_builtin_result_reuse(monkeypatch, tmp_path):
    plugins = tmp_path / ".apsara/tools"
    plugins.mkdir(parents=True)
    (plugins / "local.py").write_text("# local tools may override built-in names\n")
    (tmp_path / "a.py").write_text("unchanged")
    read = [("read_file", {"path": "a.py"})]
    events, calls, _ = _drive(monkeypatch, tmp_path, [read, read, None])
    assert sum(name == "read_file" for name, args in calls) == 2


def test_timeout_reservation_prevents_an_unaffordable_retry(monkeypatch, tmp_path):
    monkeypatch.setenv("APSARA_MAX_TURN_TOKENS", "5000")
    monkeypatch.setenv("APSARA_FALLBACK_MODELS", "")
    monkeypatch.setattr(executor, "estimate_request_tokens", lambda *args, **kwargs: 100)
    calls = []
    async def stream(messages, model):
        calls.append(model)
        raise asyncio.TimeoutError()
        yield  # async generator contract
    monkeypatch.setattr(executor, "call_llm_stream", stream)
    async def collect():
        with agent_runtime_context(workspace_root=tmp_path):
            return [json.loads(e) async for e in executor.run_agent_stream([
                {"role": "user", "content": "Explain this code."},
            ])]
    events = asyncio.run(collect())
    assert len(calls) == 1
    assert events[-1]["type"] == "blocked"
    run = latest_run(tmp_path)
    assert run["budget"]["usage_complete"] is False
    assert run["budget"]["estimated_usage"] == 4196


def test_critic_preserves_actionable_provider_errors(monkeypatch, tmp_path):
    from apsara_cli.engine import critic, llm
    from unittest.mock import AsyncMock
    monkeypatch.setattr(llm, "call_llm", AsyncMock(return_value=(
        {"error": "Provider response timed out."}, {})))
    monkeypatch.setattr(critic, "_read_only_context", lambda *args: "DIFF")
    content, usage = asyncio.run(critic.request_critique(tmp_path, objective="fix", focus="", model=executor.DEFAULT_MODEL))
    assert "Provider response timed out" in content
    assert "no structured verdict" not in content


def test_unknown_critic_usage_reserves_its_full_output_allowance():
    budget = TurnBudget(step_limit=25)
    budget.observe_usage({"estimated_input_tokens": 200, "unreported_calls": 1}, completion_reserve=8192)
    assert budget.reported_usage == 0
    assert budget.estimated_usage == 8392


def test_failed_fresh_check_cannot_resurrect_an_older_passing_result(monkeypatch, tmp_path):
    def fail_fresh(name, arguments):
        if name == "verify_project" and arguments.get("fresh"):
            return json.dumps({"phase": "full", "status": "failed", "results": [{
                "command": ["pytest"], "status": "failed", "returncode": 1}]})
    fresh = [("verify_project", {"phase": "full", "fresh": True})]
    events, calls, _ = _drive(monkeypatch, tmp_path, [BASE, WRITE, FULL, fresh, FULL, None], on_tool=fail_fresh)
    assert sum(name == "verify_project" and args["phase"] == "full" for name, args in calls) == 3
    assert latest_run(tmp_path)["state"] == "completed_verified"


def test_unreported_critic_call_retains_input_estimate(monkeypatch, tmp_path):
    from apsara_cli.engine import critic, llm, tools
    from unittest.mock import AsyncMock
    monkeypatch.setattr(llm, "call_llm", AsyncMock(return_value=({"content": "APPROVED"}, {})))
    monkeypatch.setattr(llm, "estimate_request_tokens", lambda *args, **kwargs: 123)
    monkeypatch.setattr(critic, "_read_only_context", lambda *args: "DIFF")
    async def execute():
        with tools.agent_runtime_context(workspace_root=tmp_path):
            content = await tools.execute_tool_async("request_critic", {"objective":"fix"})
            return content, tools.consume_auxiliary_usage()
    content, usages = asyncio.run(execute())
    assert content == "APPROVED"
    assert usages[0]["estimated_input_tokens"] == 123
    assert usages[0]["unreported_calls"] == 1
    assert usages[0]["provider_reported_calls"] == 0
    budget = TurnBudget(step_limit=25)
    budget.observe_usage(usages[0], completion_reserve=usages[0]["completion_reserve"])
    assert budget.estimated_usage == 8315
    assert budget.usage_complete is False
