"""Completion, freshness, and resource-limit regressions using real files."""

import asyncio
import json

import pytest

from apsara_cli.engine import executor
from apsara_cli.engine.budget import TurnBudget
from apsara_cli.engine.evidence import critic_evidence
from apsara_cli.engine.tools import agent_runtime_context
from apsara_cli.engine.runtime import latest_run


def _drive(monkeypatch, root, actions, *, before_request=None, on_tool=None, usage=10, estimate=None, objective="Repair the implementation.", actual_critic=False):
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
            if actual_critic:
                from apsara_cli.engine.tools import execute_tool_async
                return await execute_tool_async(name, arguments)
            return '{"verdict":"approved","findings":[]}'
        return (root / arguments["path"]).read_text()

    monkeypatch.setattr(executor, "call_llm_stream", stream)
    monkeypatch.setattr(executor, "execute_tool_async", execute)
    monkeypatch.setattr(executor, "estimate_request_tokens", estimate or (lambda *args, **kwargs: 100))
    async def collect():
        with agent_runtime_context(workspace_root=root):
            return [json.loads(e) async for e in executor.run_agent_stream([
                {"role": "user", "content": objective},
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
        [("request_critic", {"objective": "Only check formatting", "focus": "Add unrequested new features"})], None])
    assert next(args for name, args in calls if name == "request_critic")["objective"] == "Repair the implementation."
    args = next(args for name, args in calls if name == "request_critic")
    assert "unrequested new features" not in args["focus"]
    assert args["_verification"]["status"] == "passed"
    assert args["_verification"]["phase"] == "full"
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


def test_phase_stop_compacts_older_reads_and_retains_the_objective(monkeypatch, tmp_path):
    (tmp_path / "a.py").write_text("x" * 6000)
    reads = [[("read_file_lines", {"path":"a.py", "start_line":i, "end_line":i+1})] for i in range(1,6)]
    estimate = lambda messages, **kwargs: len(json.dumps(messages)) // 3 + 500
    events, calls, requests = _drive(monkeypatch, tmp_path, reads + [None], usage=10000, estimate=estimate)
    assert events[-1]["type"] == "blocked"
    assert "phase allowance" in events[-1]["message"]
    assert any("Compacted" in e.get("message", "") for e in events)
    assert any(m.get("role") == "user" and m.get("content") == "Repair the implementation." for m in requests[-1])
    assert requests[-1][-1]["role"] == "tool"
    assert requests[-1][-1]["content"] == "x" * 6000
    assert estimate(requests[-1]) <= 8192


def test_soft_compaction_target_never_drops_a_large_required_objective(monkeypatch, tmp_path):
    (tmp_path / "a.py").write_text("x")
    objective = "Required detail: " + "x" * 45000
    estimate = lambda messages, **kwargs: len(json.dumps(messages)) // 3 + 500
    events, calls, requests = _drive(monkeypatch, tmp_path, [[("read_file", {"path":"a.py"})],None], usage=20000, estimate=estimate, objective=objective)
    assert events[-1]["type"] == "blocked"
    assert "phase allowance" in events[-1]["message"]
    assert any(m.get("role") == "user" and m.get("content") == objective for m in requests[-1])
    assert estimate(requests[-1]) >= len(objective) // 3


def test_approvals_for_another_workspace_do_not_invalidate_this_turn(monkeypatch, tmp_path):
    from apsara_cli.config import trust
    path = tmp_path / ".apsara/trust-test.json"
    monkeypatch.setattr(trust, "TRUST_PATH", path)
    def edit(index):
        if index == 3:
            path.parent.mkdir(exist_ok=True)
            path.write_text(json.dumps({"workspaces": {"/another/project": {"verification:project": {"sha256":"approved"}}}}))
    events, calls, _ = _drive(monkeypatch, tmp_path, [BASE, WRITE, FULL, FULL, None], before_request=edit)
    assert sum(name == "verify_project" and args["phase"] == "full" for name, args in calls) == 1
    assert latest_run(tmp_path)["state"] == "completed_verified"


def test_current_workspace_trust_change_invalidates_reused_evidence(monkeypatch, tmp_path):
    from apsara_cli.config import trust
    path = tmp_path / ".apsara/trust-test.json"
    monkeypatch.setattr(trust, "TRUST_PATH", path)
    def edit(index):
        if index == 3:
            path.parent.mkdir(exist_ok=True)
            path.write_text(json.dumps({"workspaces": {str(tmp_path.resolve()): {"verification:project": {"sha256":"changed"}}}}))
    events, calls, _ = _drive(monkeypatch, tmp_path, [BASE, WRITE, FULL, FULL, None], before_request=edit)
    assert sum(name == "verify_project" and args["phase"] == "full" for name, args in calls) == 2
    assert latest_run(tmp_path)["state"] == "completed_verified"


def test_phase_stop_preserves_review_and_finish_allowances(monkeypatch, tmp_path):
    actions = []
    for i in range(12):
        (tmp_path / f'a{i}.py').write_text('unchanged')
        actions.append([('read_file', {'path': f'a{i}.py'})])
    events, calls, requests = _drive(monkeypatch, tmp_path, actions, usage=6000)
    saved = latest_run(tmp_path)['budget']
    assert events[-1]['type'] == 'blocked'
    assert 'phase allowance' in events[-1]['message']
    assert saved['reported_usage'] < saved['usage_limit']
    assert saved['phase_spent'].get('review', 0) == 0
    assert saved['phase_spent'].get('finish', 0) == 0
    assert sum(saved['phase_limits'].values()) == saved['usage_limit']
    assert all((tmp_path / f'a{i}.py').read_text() == 'unchanged' for i in range(12))


def test_phase_budgets_follow_verification_and_reopen_after_edit(monkeypatch, tmp_path):
    write_b = [('write_to_file', {'path':'b.py', 'content':'fixed'})]
    events, _, requests = _drive(monkeypatch, tmp_path, [BASE, WRITE, write_b, FULL,
        [('request_critic', {})], WRITE, FULL, [('request_critic', {})], None])
    assert latest_run(tmp_path)['state'] == 'completed_verified'
    budget = latest_run(tmp_path)['budget']
    assert budget['phase_spent']['review'] == 20
    assert budget['phase_spent']['finish'] == 10  # Late edits use implementation, not the final-answer reserve.
    assert budget['phase_spent']['implement'] >= 20
    assert budget['phase'] == 'finish'


def _review_run(monkeypatch, tmp_path, responses, budget=None, key='snapshot'):
    from apsara_cli.engine import critic, llm
    from apsara_cli.engine.budget import _ACTIVE
    from unittest.mock import AsyncMock
    mocked = AsyncMock(side_effect=responses)
    monkeypatch.setattr(llm, 'call_llm', mocked)
    monkeypatch.setattr(llm, 'estimate_request_tokens', lambda *args, **kwargs: 100)
    monkeypatch.setattr(critic, '_read_only_context', lambda *args: 'COMPLETE DIFF')
    async def run():
        token = _ACTIVE.set(budget)
        try:
            return await critic.request_critique(tmp_path, objective='Fix the requested behavior.',
                focus='', model=executor.DEFAULT_MODEL, review_key=key)
        finally:
            _ACTIVE.reset(token)
    return asyncio.run(run()), mocked


def test_review_recovers_once_and_accounts_each_attempt(monkeypatch, tmp_path):
    budget = TurnBudget(step_limit=25)
    (content, usage), mocked = _review_run(monkeypatch, tmp_path, [
        ({'content':'Concrete concern: the new boundary must reject invalid sizes.'}, {'total_tokens':200}),
        ({'content':'{"verdict":"changes_requested","findings":[{"description":"Invalid sizes accepted"}]}'}, {'total_tokens':300}),
    ], budget)
    assert mocked.await_count == 2
    assert critic_evidence(content)['verdict'] == 'changes_requested'
    assert 'Concrete concern' in mocked.await_args.args[0][-1]['content']
    assert 'COMPLETE DIFF' in mocked.await_args.args[0][1]['content']
    assert usage['total_tokens'] == 500 and usage['auxiliary_calls'] == 2
    assert usage['provider_reported_calls'] == 2 and usage['budget_accounted'] is True
    assert budget.reported_usage == 500 and budget.phase_spent['review'] == 500
    assert budget.review_attempts == 2


def test_review_findings_never_trigger_a_retry_for_approval(monkeypatch, tmp_path):
    (content, _), mocked = _review_run(monkeypatch, tmp_path, [
        ({'content':'{"verdict":"approved","findings":[{"description":"Wrong result"}]}'}, {'total_tokens':10}),
    ], TurnBudget(step_limit=25))
    assert mocked.await_count == 1
    assert critic_evidence(content)['verdict'] == 'changes_requested'


def test_review_timeout_unknown_usage_is_reserved_before_retry(monkeypatch, tmp_path):
    budget = TurnBudget(step_limit=25)
    (content, usage), mocked = _review_run(monkeypatch, tmp_path, [
        ({'error':'Provider response timed out.'}, {}),
        ({'content':'APPROVED'}, {'total_tokens':50}),
    ], budget)
    assert content == 'APPROVED' and mocked.await_count == 2
    assert usage['unreported_calls'] == 1 and usage['provider_reported_calls'] == 1
    assert budget.estimated_usage == 100 + 8192
    assert budget.reported_usage == 50 and budget.usage_complete is False


def test_review_retry_refuses_to_spend_finish_reservation(monkeypatch, tmp_path):
    budget = TurnBudget(step_limit=25, usage_limit=40_000)
    (content, usage), mocked = _review_run(monkeypatch, tmp_path, [({'error':'Provider response timed out.'}, {})], budget)
    assert mocked.await_count == 1
    assert 'phase allowance' in content
    assert usage['unreported_calls'] == 1
    assert budget.remaining_usage > 30_000


def test_agent_cannot_reset_review_attempts_with_fresh_or_new_focus(monkeypatch, tmp_path):
    budget = TurnBudget(step_limit=25)
    _, mocked = _review_run(monkeypatch, tmp_path, [({'content':''}, {'total_tokens':10})] * 2, budget)
    (content, usage), second = _review_run(monkeypatch, tmp_path, [], budget)
    assert mocked.await_count == 2 and second.await_count == 0
    assert 'attempts exhausted' in content and usage['request_skipped']
    # Different source/policy fingerprint grants another independent review.
    (content, _), third = _review_run(monkeypatch, tmp_path, [({'content':'APPROVED'}, {'total_tokens':10})], budget, key='changed-snapshot')
    assert third.await_count == 1 and content == 'APPROVED'


def test_critic_dispatch_does_not_count_recovery_twice(monkeypatch, tmp_path):
    from apsara_cli.engine import critic, llm, tools
    from apsara_cli.engine.budget import _ACTIVE
    from unittest.mock import AsyncMock
    mocked = AsyncMock(side_effect=[({'content':''}, {'total_tokens':20}), ({'content':'APPROVED'}, {'total_tokens':30})])
    monkeypatch.setattr(llm, 'call_llm', mocked)
    monkeypatch.setattr(llm, 'estimate_request_tokens', lambda *a, **kw: 100)
    monkeypatch.setattr(critic, '_read_only_context', lambda *a: 'DIFF')
    async def run():
        budget = TurnBudget(step_limit=25)
        token = _ACTIVE.set(budget)
        try:
            with agent_runtime_context(workspace_root=tmp_path):
                content = await tools.execute_tool_async('request_critic', {'objective':'Fix','_review_key':'source'})
                usage = tools.consume_auxiliary_usage()
                for u in usage:
                    if not u.get('budget_accounted'):
                        budget.observe_usage(u, completion_reserve=u.get('completion_reserve'), phase='review')
                return budget, content, usage
        finally:
            _ACTIVE.reset(token)
    budget, content, usages = asyncio.run(run())
    assert content == 'APPROVED'
    assert budget.reported_usage == 50 and usages[0]['auxiliary_calls'] == 2
    assert budget.phase_spent['review'] == 50


def test_cancelled_review_reserves_unknown_usage(monkeypatch, tmp_path):
    budget = TurnBudget(step_limit=25)
    with pytest.raises(asyncio.CancelledError):
        _review_run(monkeypatch, tmp_path, [asyncio.CancelledError()], budget)
    assert budget.estimated_usage == 8292
    assert budget.phase_spent['review'] == 8292 and not budget.usage_complete


def test_oversized_review_evidence_cannot_be_silently_truncated(monkeypatch, tmp_path):
    from apsara_cli.engine import critic, llm
    from unittest.mock import AsyncMock
    import subprocess
    subprocess.run(['git','init','-q',str(tmp_path)], check=True)
    (tmp_path/'large.py').write_text('x'*12_001)
    mocked = AsyncMock()
    monkeypatch.setattr(llm, 'call_llm', mocked)
    content, usage = asyncio.run(critic.request_critique(tmp_path, objective='Review all changes',
        focus='', model=executor.DEFAULT_MODEL, changed_files=['large.py']))
    assert 'no partial review' in content and usage['request_skipped']
    mocked.assert_not_called()


def test_aggregate_unknown_calls_each_reserve_an_output_ceiling():
    budget = TurnBudget(step_limit=25)
    budget.observe_usage({'estimated_input_tokens':200,'unreported_calls':2},completion_reserve=8192,phase='review')
    assert budget.estimated_usage == 200 + 2*8192


def test_executor_stops_after_review_recovery_and_accounts_once(monkeypatch,tmp_path):
    from apsara_cli.engine import critic,llm
    from unittest.mock import AsyncMock
    mocked=AsyncMock(side_effect=[({'content':''},{'total_tokens':20}),({'content':''},{'total_tokens':30})])
    monkeypatch.setattr(llm,'call_llm',mocked)
    monkeypatch.setattr(llm,'estimate_request_tokens',lambda *a,**kw:100)
    monkeypatch.setattr(critic,'_read_only_context',lambda *a:'COMPLETE DIFF')
    actions=[BASE,WRITE,[('write_to_file',{'path':'b.py','content':'fixed'})],FULL,[('request_critic',{})],None]
    events,calls,requests=_drive(monkeypatch,tmp_path,actions,actual_critic=True)
    run=latest_run(tmp_path)
    assert mocked.await_count==2 and len(requests)==5
    assert run['state']=='blocked' and run['critic_status']=='unavailable'
    assert run['budget']['reported_usage']==100  # Five primary calls plus both reviews, once each.
    assert run['budget']['review_attempts']==2
    assert events[-1]['type']=='blocked' and 'bounded recovery' in events[-1]['message']
    assert (tmp_path/'a.py').read_text()=='fixed' and (tmp_path/'b.py').read_text()=='fixed'


def test_optional_review_does_not_spend_final_answer_allowance(monkeypatch,tmp_path):
    events,_,_=_drive(monkeypatch,tmp_path,[BASE,WRITE,FULL,[('request_critic',{})],None],usage=4000)
    saved=latest_run(tmp_path)
    assert saved['state']=='completed_verified'
    assert saved['budget']['phase_spent']['review']==4000
    assert saved['budget']['phase_spent']['finish']==4000
    assert saved['budget']['phase_spent']['verify']==4000


def test_closed_implementation_can_use_reserved_full_verification(monkeypatch,tmp_path):
    reads=[[('read_file_lines',{'path':'a.py','start_line':i,'end_line':i})] for i in range(1,5)]
    events,_,_=_drive(monkeypatch,tmp_path,[BASE,WRITE]+reads+[FULL,None],usage=6000)
    saved=latest_run(tmp_path)
    assert saved['budget']['implementation_closed']
    assert saved['state']=='completed_verified'
    assert saved['budget']['phase_spent']['implement']==30000
    assert saved['budget']['phase_spent']['verify']==6000
    assert saved['budget']['phase_spent']['finish']==6000


def test_verification_reserve_cannot_fund_more_edits(monkeypatch,tmp_path):
    reads=[[('read_file_lines',{'path':'a.py','start_line':i,'end_line':i})] for i in range(1,5)]
    more=[('write_to_file',{'path':'b.py','content':'must not be written'})]
    events,_,_=_drive(monkeypatch,tmp_path,[BASE,WRITE]+reads+[more],usage=6000)
    assert events[-1]['type']=='blocked'
    assert not (tmp_path/'b.py').exists()
    assert latest_run(tmp_path)['budget']['implementation_closed']


def test_finishing_can_use_unused_funds_without_expanding_implementation():
    budget=TurnBudget(step_limit=25,reported_usage=60_000,phase_spent={'finish':5000,'implement':30000})
    assert budget.request_fits(8000,4096,phase='finish')
    assert not budget.request_fits(8000,4096,phase='implement')
    assert not budget.request_fits(40_000,4096,phase='finish')


def test_final_answer_with_larger_tool_overhead_uses_unused_finish_funds(monkeypatch,tmp_path):
    events,_,_=_drive(monkeypatch,tmp_path,[BASE,WRITE,FULL,[('request_critic',{})],None],
        usage=100,estimate=lambda *a,**kw:8000)
    assert latest_run(tmp_path)['state']=='completed_verified'
    assert latest_run(tmp_path)['budget']['phase_spent']['finish']==100


def test_verification_uses_unused_work_funds_and_preserves_review_and_finish():
    budget=TurnBudget(step_limit=25,reported_usage=50_000,
        phase_spent={'explore':15_000,'implement':0,'verify':35_000})
    assert budget.remaining_for('verify')==10_000
    assert budget.remaining_for('implement')==10_000
    assert not budget.request_fits(10_000,4096,phase='implement')
    budget.observe_usage({'total_tokens':10_000},phase='verify')
    assert budget.remaining_for('implement')==0
    assert budget.remaining_for('verify')==0
    assert budget.remaining_for('review')==30_000
    assert budget.remaining_for('finish')==40_000


def test_targeted_then_full_checks_with_large_request_overhead_can_finish(monkeypatch,tmp_path):
    targeted=[('verify_project',{'phase':'targeted'})]
    events,_,_=_drive(monkeypatch,tmp_path,[BASE,WRITE,targeted,FULL,None],usage=5000,
        estimate=lambda *a,**kw:8000)
    saved=latest_run(tmp_path)
    assert saved['state']=='completed_verified'
    assert saved['budget']['phase_spent']['verify']==10000
    assert saved['budget']['phase_spent']['finish']==5000
