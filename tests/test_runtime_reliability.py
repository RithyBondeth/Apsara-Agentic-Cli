"""Regression coverage for evidence freshness and interruption safety."""

import asyncio
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from apsara_cli.cli.history import recover_interrupted_history
from apsara_cli.engine import executor
from apsara_cli.engine.cancellation import cancellation_context, run_command, run_interruptible
from apsara_cli.engine.evidence import critic_evidence, verification_evidence
from apsara_cli.engine.tools import agent_runtime_context
from apsara_cli.engine.turn_checkpoints import (
    activate_turn_checkpoint, begin_turn_checkpoint, capture_turn_paths,
    capture_turn_workspace, deactivate_turn_checkpoint, finish_turn_checkpoint,
    restore_turn_checkpoint,
)


def evidence(phase, status="passed"):
    return json.dumps({"phase": phase, "status": status, "results": [
        {"command": ["python", "-m", "unittest"], "returncode": 0 if status == "passed" else 1,
         "status": status},
    ] if status != "unavailable" else []})


def test_evidence_requires_actual_passing_commands():
    assert verification_evidence("Verification passed")['status'] == "failed"
    assert verification_evidence('{"phase":"full","status":"passed","results":[]}')['status'] == "failed"
    assert verification_evidence(evidence("full"))['status'] == "passed"
    assert verification_evidence(evidence("full", "unavailable"))['status'] == "unavailable"
    assert critic_evidence('APPROVED because everything is fine')['verdict'] == "unavailable"
    finding = {"verdict": "approved", "findings": [{"path": "a.py", "description": "Drops valid input"}]}
    assert critic_evidence(json.dumps(finding))['verdict'] == "changes_requested"


def drive(monkeypatch, tmp_path, actions, *, statuses=None, critic=None, mutate_check=False, final_hook=False):
    index = 0
    async def stream(_messages, _model):
        nonlocal index
        action = actions[min(index, len(actions) - 1)]
        index += 1
        calls = [{"id": f"c{index}", "type": "function", "function": {
            "name": action[0], "arguments": json.dumps(action[1]),
        }}] if action else None
        yield {"type": "stream_done", "content": "Finished." if not calls else "",
               "tool_calls": calls, "usage": {"total_tokens": 1}}

    async def execute(name, arguments):
        if name == "verify_project":
            phase = arguments.get("phase", "full")
            if mutate_check and phase == "full":
                (tmp_path / "a.py").write_text("changed during checks")
            return evidence(phase, (statuses or {}).get(phase, "passed"))
        if name == "request_critic":
            return json.dumps(critic or {"verdict": "approved", "findings": []})
        (tmp_path / arguments["path"]).write_text("updated")
        return "Wrote file."

    monkeypatch.setattr(executor, "call_llm_stream", stream)
    monkeypatch.setattr(executor, "execute_tool_async", execute)
    if final_hook:
        from apsara_cli.engine import hooks
        original = hooks.run_hooks
        def hook(event, *args):
            if event == "turn_end":
                (tmp_path / "a.py").write_text("changed by hook")
            return original(event, *args)
        monkeypatch.setattr(hooks, "run_hooks", hook)

    async def collect():
        with agent_runtime_context(workspace_root=tmp_path):
            return [json.loads(event) async for event in executor.run_agent_stream([
                {"role": "user", "content": "Repair these files."},
            ])]
    return asyncio.run(collect())


BASELINE = ("verify_project", {"phase": "baseline"})
WRITE_A = ("write_to_file", {"path": "a.py"})
WRITE_B = ("write_to_file", {"path": "b.py"})
FULL = ("verify_project", {"phase": "full"})
CRITIC = ("request_critic", {})


def test_single_file_requires_full_verification(monkeypatch, tmp_path):
    events = drive(monkeypatch, tmp_path, [BASELINE, WRITE_A, None])
    assert events[-1]["type"] == "blocked"
    assert not any(event["type"] == "final_answer" for event in events)


def test_verified_and_unverified_states_are_distinct(monkeypatch, tmp_path):
    events = drive(monkeypatch, tmp_path, [BASELINE, WRITE_A, FULL, None])
    assert any(event.get("state") == "completed_verified" for event in events)
    events = drive(monkeypatch, tmp_path, [BASELINE, WRITE_B, FULL, None], statuses={"full": "unavailable"})
    assert any(event.get("state") == "completed_unverified" for event in events)


@pytest.mark.parametrize("review", [None, {"verdict": "changes_requested", "findings": [
    {"path": "a.py", "description": "Missing input validation"},
]}])
def test_multi_file_requires_critic_approval(monkeypatch, tmp_path, review):
    actions = [BASELINE, WRITE_A, WRITE_B, FULL] + ([CRITIC] if review else []) + [None]
    events = drive(monkeypatch, tmp_path, actions, critic=review)
    assert events[-1]["type"] == "blocked"


def test_mutation_after_review_requires_new_verification(monkeypatch, tmp_path):
    actions = [BASELINE, WRITE_A, WRITE_B, FULL, CRITIC,
               ("write_to_file", {"path": "c.py"}), None]
    assert drive(monkeypatch, tmp_path, actions)[-1]["type"] == "blocked"


def test_single_file_material_critic_finding_blocks_completion(monkeypatch, tmp_path):
    review = {"verdict": "changes_requested", "findings": [{"path": "a.py", "description": "Invalid input crashes"}]}
    events = drive(monkeypatch, tmp_path, [BASELINE, WRITE_A, FULL, CRITIC, None], critic=review)
    assert events[-1]["type"] == "blocked"


def test_commands_require_baseline_before_execution(monkeypatch, tmp_path):
    events = drive(monkeypatch, tmp_path, [("run_bash_command", {"command": "printf hello"}), None])
    results = [event for event in events if event["type"] == "tool_result"]
    assert "phase=baseline" in results[0]["result"]
    assert not list(tmp_path.glob("a.py"))


@pytest.mark.parametrize("option", ["mutate_check", "final_hook"])
def test_checks_and_completion_hooks_cannot_make_evidence_stale(monkeypatch, tmp_path, option):
    events = drive(monkeypatch, tmp_path, [BASELINE, WRITE_A, FULL, None], **{option: True})
    assert events[-1]["type"] == "blocked"


def test_undo_preserves_later_user_edits_and_new_files(tmp_path):
    path = tmp_path / "a.py"
    path.write_text("original")
    begin_turn_checkpoint(tmp_path, "turn", "repair")
    token = activate_turn_checkpoint("turn")
    try:
        capture_turn_workspace(tmp_path)
        path.write_text("agent edit")
        finish_turn_checkpoint(tmp_path, "turn", "completed_verified")
    finally:
        deactivate_turn_checkpoint(token)
    path.write_text("later user edit")
    later = tmp_path / "user.txt"
    later.write_text("user data")
    manifest = restore_turn_checkpoint(tmp_path, "turn")
    assert manifest["rollback"]["conflicts"] == ["a.py"]
    assert path.read_text() == "later user edit"
    assert later.read_text() == "user data"
    forced = restore_turn_checkpoint(tmp_path, "turn", force=True)
    assert forced["status"] == "rolled_back"
    assert path.read_text() == "original"
    assert later.read_text() == "user data"


def test_active_checkpoint_without_final_snapshot_requires_force(tmp_path):
    path = tmp_path / "a.py"
    path.write_text("original")
    begin_turn_checkpoint(tmp_path, "active", "repair")
    token = activate_turn_checkpoint("active")
    try:
        capture_turn_paths(tmp_path, [path])
        path.write_text("interrupted edit")
    finally:
        deactivate_turn_checkpoint(token)
    assert restore_turn_checkpoint(tmp_path, "active")["rollback"]["conflicts"] == ["a.py"]
    assert path.read_text() == "interrupted edit"


def test_resume_keeps_observed_results_without_invalid_tool_messages():
    dispatch = {"role": "assistant", "tool_calls": [{"id": "one"}, {"id": "two"}]}
    messages = [{"role": "user", "content": "repair"}, dispatch,
                {"role": "tool", "tool_call_id": "one", "name": "write_to_file", "content": "Wrote a.py"}]
    recovered = recover_interrupted_history(messages)
    assert recovered[0] == messages[0]
    assert not any(message.get("role") == "tool" or message.get("tool_calls") for message in recovered)
    assert "Wrote a.py" in recovered[-1]["content"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group regression")
def test_cancel_stops_descendants_before_returning(tmp_path):
    marker = tmp_path / "late.txt"
    ready = tmp_path / "ready.txt"
    child = f"import time; from pathlib import Path; time.sleep(.7); Path({str(marker)!r}).write_text('late')"
    parent = (f"import subprocess, sys, time; from pathlib import Path; "
              f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
              f"Path({str(ready)!r}).write_text('ready'); time.sleep(10)")
    async def scenario():
        with cancellation_context():
            task = asyncio.create_task(run_interruptible(run_command, [sys.executable, "-c", parent], cwd=tmp_path, timeout=20))
            for _ in range(200):
                if ready.exists():
                    break
                await asyncio.sleep(.01)
            assert ready.exists(), "child command did not start"
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.sleep(.9)
    asyncio.run(scenario())
    assert not marker.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group regression")
def test_timeout_stops_child_when_parent_already_exited(tmp_path):
    marker = tmp_path / "late.txt"
    child = f"import time; from pathlib import Path; time.sleep(.7); Path({str(marker)!r}).write_text('late')"
    parent = f"import subprocess, sys; subprocess.Popen([sys.executable, '-c', {child!r}])"
    with pytest.raises(subprocess.TimeoutExpired):
        run_command([sys.executable, "-c", parent], cwd=tmp_path, timeout=.2)
    import time
    time.sleep(.9)
    assert not marker.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX command cancellation regression")
def test_cancelled_agent_checkpoint_matches_stopped_command(monkeypatch, tmp_path):
    from apsara_cli.engine import tools
    from apsara_cli.engine.turn_checkpoints import list_turn_checkpoints
    path = tmp_path / "a.py"
    path.write_text("original")
    ready = tmp_path / "ready.txt"
    command = shlex.join([sys.executable, "-c", (
        "import time; from pathlib import Path; Path('a.py').write_text('agent'); "
        "Path('ready.txt').write_text('ready'); time.sleep(20)"
    )])
    count = 0
    async def stream(_messages, _model):
        nonlocal count
        count += 1
        name, arguments = ("verify_project", {"phase": "baseline"}) if count == 1 else ("run_bash_command", {"command": command})
        yield {"type": "stream_done", "content": "", "usage": {"total_tokens": 1}, "tool_calls": [{
            "id": str(count), "function": {"name": name, "arguments": json.dumps(arguments)}, "type": "function",
        }]}
    original_execute = tools.execute_tool_async
    async def execute(name, arguments):
        return evidence("baseline") if name == "verify_project" else await original_execute(name, arguments)
    monkeypatch.setattr(executor, "call_llm_stream", stream)
    monkeypatch.setattr(executor, "execute_tool_async", execute)
    async def scenario():
        events = []
        async def collect():
            async for _event in executor.run_agent_stream([{"role": "user", "content": "run repair"}]):
                events.append(json.loads(_event))
        with agent_runtime_context(workspace_root=tmp_path, enable_bash=True,
                                   allowed_commands={Path(sys.executable).name},
                                   confirmation_callback=lambda *_: True):
            task = asyncio.create_task(collect())
            for _ in range(300):
                if ready.exists() or task.done():
                    break
                await asyncio.sleep(.01)
            assert ready.exists(), [event.get("result") for event in events if event.get("type") == "tool_result"]
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    asyncio.run(scenario())
    manifest = list_turn_checkpoints(tmp_path)[0]
    assert manifest["status"] == "cancelled"
    assert next(entry for entry in manifest["files"] if entry["path"] == "a.py")["after"]["kind"] == "file"
    assert path.read_text() == "agent"
    restored = restore_turn_checkpoint(tmp_path, manifest["id"])
    assert restored["rollback"]["conflicts"] == []
    assert path.read_text() == "original"
    assert not ready.exists()


def test_forced_undo_requires_explicit_approval_even_with_blanket_approval():
    from apsara_cli.shared.ui import ConsoleUI
    ui = ConsoleUI(use_color=False, auto_approve=True)
    assert ui.confirm_action("undo_turn", {"turn_id": "turn"}) is True
    assert ui.confirm_action("undo_turn", {"turn_id": "turn", "force": True}) is False
