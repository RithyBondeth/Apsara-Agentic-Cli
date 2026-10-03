"""Offline coverage for selective schemas, skills and in-turn context limits."""

import asyncio
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from apsara_cli.engine import executor, llm
from apsara_cli.engine.capabilities import CORE_TOOLS, capability_context, current_capabilities
from apsara_cli.engine.context import ContextBudgetError, MAX_RESULT_CHARS, bound_tool_result, build_task_state, prepare_context, read_result
from apsara_cli.engine.skills import discover_skills, read_skill_file
from apsara_cli.engine.tools import (
    agent_runtime_context, discover_tools, execute_tool_async, get_agent_tools,
    get_request_tools, list_skills, mcp_manager_context, read_skill, read_skill_resource,
)


def _skill(root: Path, name: str, description: str = "Focused workflow", body: str = "Inspect before editing"):
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n---\n# Workflow\n{body}\n")
    return directory


def _names(tools):
    return {tool["function"]["name"] for tool in tools}


class LargeMcp:
    def tool_definitions(self):
        return [{"type": "function", "function": {
            "name": f"mcp__tickets__lookup_{index}", "description": "Lookup a ticket " + "details " * 100,
            "parameters": {"type": "object", "properties": {"id": {"type": "string"}}},
        }} for index in range(100)]


def test_mcp_schemas_are_discovered_without_loading_all_into_requests(tmp_path):
    with agent_runtime_context(workspace_root=tmp_path), mcp_manager_context(LargeMcp()), capability_context():
        initial = get_request_tools()
        assert _names(initial) == CORE_TOOLS
        assert len(json.dumps(initial)) < len(json.dumps(get_agent_tools())) / 3
        matches = json.loads(discover_tools("mcp__tickets__lookup_42", limit=1))
        assert matches["tools"][0]["name"] == "mcp__tickets__lookup_42"
        assert _names(get_request_tools()) == CORE_TOOLS | {"mcp__tickets__lookup_42"}


def test_tool_activation_is_isolated_and_resets_on_error(tmp_path):
    with agent_runtime_context(workspace_root=tmp_path):
        with pytest.raises(RuntimeError), capability_context():
            discover_tools("list_symbols", limit=1)
            assert "list_symbols" in _names(get_request_tools())
            raise RuntimeError("interrupted")
        assert current_capabilities() is None
        with capability_context():
            assert "list_symbols" not in _names(get_request_tools())


def test_tool_activation_has_a_fixed_upper_bound(tmp_path):
    from apsara_cli.engine.capabilities import MAX_ACTIVE_TOOLS
    with agent_runtime_context(workspace_root=tmp_path), mcp_manager_context(LargeMcp()), capability_context() as state:
        for index in range(MAX_ACTIVE_TOOLS + 3):
            discover_tools(f"mcp__tickets__lookup_{index}", limit=1)
        assert len(state.tools) == MAX_ACTIVE_TOOLS
        assert "mcp__tickets__lookup_34" not in _names(get_request_tools())


def test_discovery_does_not_enable_disabled_shell_or_bypass_read_only(tmp_path):
    with agent_runtime_context(workspace_root=tmp_path, enable_bash=False, read_only=True), capability_context():
        assert "run_bash_command" not in _names(get_agent_tools())
        discover_tools("run_bash_command", limit=1)
        assert "run_bash_command" not in _names(get_request_tools())
        result = asyncio.run(execute_tool_async("write_to_file", {"path": "a.txt", "content": "x"}))
        assert result.startswith("Error")
        assert not (tmp_path / "a.txt").exists()


def test_model_request_and_token_estimate_use_identical_selected_schemas(tmp_path):
    captured = {}

    def counter(**kwargs):
        captured.update(kwargs)
        return 123

    with agent_runtime_context(workspace_root=tmp_path), capability_context(), patch.object(llm.litellm, "token_counter", counter):
        discover_tools("list_symbols", limit=1)
        assert llm.estimate_request_tokens([{"role": "user", "content": "hi"}]) == 123
        assert _names(captured["tools"]) == _names(get_request_tools())


def test_skill_precedence_and_metadata_only_discovery(tmp_path):
    user = tmp_path / "user-skills"
    _skill(user, "debug", "User version", "USER BODY")
    _skill(tmp_path / ".apsara" / "skills", "debug", "Project version", "PROJECT BODY")
    skills = discover_skills(tmp_path, user_root=user)
    debug = next(item for item in skills if item.name == "debug")
    assert debug.source == "project"
    assert debug.description == "Project version"
    assert "BODY" not in json.dumps(debug.metadata())
    assert "PROJECT BODY" in read_skill_file(debug)
    assert {item.name for item in skills} >= {"debug", "test", "review"}


def test_skill_supports_folded_description_and_skips_invalid_definitions(tmp_path):
    directory = _skill(tmp_path / ".apsara" / "skills", "folded")
    (directory / "SKILL.md").write_text("---\nname: folded\ndescription: >\n  Diagnose failures\n  with evidence\n---\n# Body\n")
    invalid = _skill(tmp_path / ".apsara" / "skills", "invalid")
    (invalid / "SKILL.md").write_text("---\nname: ../../escape\n---\n")
    found = {skill.name: skill for skill in discover_skills(tmp_path, user_root=tmp_path / "missing")}
    assert found["folded"].description == "Diagnose failures with evidence"
    assert "invalid" not in found


def test_skill_loading_pins_only_selected_instructions(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    directory = _skill(tmp_path / ".apsara" / "skills", "custom", body="Keep this workflow active")
    (directory / "reference.md").write_text("Resource evidence")
    with agent_runtime_context(workspace_root=tmp_path), capability_context() as state:
        assert "custom" in list_skills("custom")
        assert state.skills == {}
        acknowledgment = read_skill("custom")
        assert "Loaded skill" in acknowledgment
        assert "Keep this workflow active" not in acknowledgment
        assert "Keep this workflow active" in state.skills["custom"]
        assert read_skill_resource("custom", "reference.md") == "Resource evidence"
        assert read_skill_resource("custom", "../reference.md").startswith("Error")


def test_skill_resource_never_executes_scripts_and_rejects_symlinks(tmp_path):
    directory = _skill(tmp_path / ".apsara" / "skills", "custom")
    marker = tmp_path / "executed"
    (directory / "script.py").write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()")
    skill = next(item for item in discover_skills(tmp_path) if item.name == "custom")
    assert "touch" in read_skill_file(skill, "script.py")
    assert not marker.exists()
    try:
        (directory / "link.md").symlink_to(directory / "script.py")
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(ValueError):
        read_skill_file(skill, "link.md")


def test_oversized_skill_activation_is_rejected_without_losing_loaded_skills(tmp_path):
    _skill(tmp_path / ".apsara" / "skills", "large", body="x" * 25000)
    with agent_runtime_context(workspace_root=tmp_path), capability_context() as state:
        assert "Loaded skill" in read_skill("debug")
        assert read_skill("large").startswith("Error")
        assert set(state.skills) == {"debug"}


def test_skill_directory_symlink_cannot_expose_outside_documents(tmp_path):
    outside = _skill(tmp_path / "outside", "outside-skill")
    root = tmp_path / "workspace" / ".apsara" / "skills"
    root.mkdir(parents=True)
    try:
        (root / "linked").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    assert "outside-skill" not in {skill.name for skill in discover_skills(tmp_path / "workspace")}


def test_large_results_preserve_error_and_allow_exact_section_retrieval(tmp_path):
    text = "Error: check failed\n" + "".join(f"line {number}\n" for number in range(2000))
    bounded = bound_tool_result(tmp_path, text)
    identifier = hashlib.sha256(text.encode()).hexdigest()
    assert len(bounded) <= MAX_RESULT_CHARS
    assert bounded.startswith("Error: check failed")
    assert identifier in bounded
    assert read_result(tmp_path, identifier, 101, 2) == "101: line 99\n102: line 100\n"
    artifact = tmp_path / ".apsara" / "tool-results" / f"{identifier}.txt"
    assert artifact.read_text() == text
    if __import__("os").name != "nt":
        assert artifact.stat().st_mode & 0o777 == 0o600


def test_long_single_line_result_is_retrievable_by_character_offset(tmp_path):
    text = "ក" * 7000 + "IMPORTANT MIDDLE" + "x" * 7000
    bound_tool_result(tmp_path, text)
    identifier = hashlib.sha256(text.encode()).hexdigest()
    section = read_result(tmp_path, identifier, char_offset=6998)
    assert "កកIMPORTANT MIDDLE" in section
    assert len(section) <= MAX_RESULT_CHARS


def test_result_identifiers_and_storage_reject_path_escape(tmp_path):
    with pytest.raises(ValueError):
        read_result(tmp_path, "../../outside")
    outside = tmp_path / "outside"
    outside.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    try:
        (workspace / ".apsara").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    result = bound_tool_result(workspace, "x" * 10000)
    assert "could not be stored" in result
    assert list(outside.iterdir()) == []


def test_result_reader_rejects_symlinked_artifacts(tmp_path):
    directory = tmp_path / ".apsara" / "tool-results"
    directory.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("private source")
    try:
        (directory / ("a" * 64 + ".txt")).symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(ValueError):
        read_result(tmp_path, "a" * 64)


def _size(messages, **_kwargs):
    return sum(len(json.dumps(message)) for message in messages)


def test_compaction_preserves_policy_objective_and_complete_tool_exchange(tmp_path):
    messages = [
        {"role": "system", "content": "Permission policy"},
        {"role": "user", "content": "Earlier task"},
        {"role": "assistant", "content": "old answer " * 300},
        {"role": "user", "content": "Repair the current bug"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "old", "function": {"name": "read_file"}}]},
        {"role": "tool", "tool_call_id": "old", "content": "old evidence " * 200},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "a"}, {"id": "b"}]},
        {"role": "tool", "tool_call_id": "a", "content": "Latest evidence A"},
        {"role": "tool", "tool_call_id": "b", "content": "Latest evidence B"},
    ]
    prepared = prepare_context(messages, budget=900, estimate=_size, task_state="Unresolved failure; changed app.py", workspace=tmp_path)
    assert prepared.tokens <= 900
    assert prepared.dropped_messages > 0
    assert messages[0] in prepared.messages and messages[3] in prepared.messages
    assert prepared.messages[-3:] == messages[-3:]
    assert not any(message.get("tool_call_id") == "old" for message in prepared.messages)
    assert "changed app.py" in prepared.messages[1]["content"]
    assert "old answer" in messages[2]["content"]  # Caller-owned history is untouched.


def test_irreducible_context_fails_before_an_oversized_request(tmp_path):
    messages = [{"role": "system", "content": "Policy"}, {"role": "user", "content": "x" * 2000}]
    with pytest.raises(ContextBudgetError):
        prepare_context(messages, budget=500, estimate=_size, task_state="Task", workspace=tmp_path)


def test_repeated_compaction_keeps_evidence_identifiers_and_refreshes_verification(tmp_path):
    result = bound_tool_result(tmp_path, "Evidence\n" * 2000)
    identifier = hashlib.sha256(("Evidence\n" * 2000).encode()).hexdigest()
    first = build_task_state([
        {"role": "assistant", "content": "Repair settings precedence"},
        {"role": "tool", "name": "read_file", "content": result},
    ], objective="Fix settings", changed_files=["settings.py"], baseline_attempted=True,
       verification_passed=True, critic_received=True)
    second = build_task_state([
        {"role": "system", "content": "Apsara task state after context compaction:\n" + first},
        {"role": "tool", "name": "edit_file", "content": "Edited settings.py"},
    ], objective="Fix settings", changed_files=["settings.py"], baseline_attempted=True,
       verification_passed=False, critic_received=False)
    state = json.loads(second)
    assert state["recent_actions"][0]["result_identifier"] == identifier
    assert state["earlier_notes"] == ["Repair settings precedence"]
    assert state["latest_full_verification_passed"] is False


def test_compaction_of_a_continue_request_retains_the_earlier_objective(tmp_path):
    state = json.loads(build_task_state([
        {"role": "user", "content": "Repair settings precedence; preserve public names."},
        {"role": "assistant", "content": "Found the loader"},
        {"role": "user", "content": "continue"},
    ], objective="continue", changed_files=[], baseline_attempted=True,
       verification_passed=False, critic_received=False))
    assert state["earlier_requests"] == ["Repair settings precedence; preserve public names."]


def test_executor_checks_budget_again_after_tool_calls_and_keeps_loaded_skills(tmp_path):
    requests = []

    async def provider(messages, model):
        requests.append([dict(message) for message in messages])
        if len(requests) == 1:
            yield {"type": "stream_done", "content": "", "usage": {}, "tool_calls": [{
                "id": "skill", "function": {"name": "read_skill", "arguments": '{"name":"debug"}'},
            }]}
        else:
            yield {"type": "stream_done", "content": "Finished", "usage": {}, "tool_calls": None}

    async def collect():
        return [json.loads(event) async for event in executor.run_agent_stream([{"role": "user", "content": "Use debug"}])]

    with agent_runtime_context(workspace_root=tmp_path), patch.object(executor, "call_llm_stream", provider), \
         patch.object(executor, "estimate_request_tokens", side_effect=_size) as counter:
        events = asyncio.run(collect())
    assert counter.call_count >= 2
    assert any("SKILL debug" in message.get("content", "") for message in requests[1])
    assert any(event["type"] == "final_answer" for event in events)
    assert current_capabilities() is None


def test_executor_does_not_call_provider_when_context_cannot_fit(tmp_path):
    calls = []

    async def provider(messages, model):
        calls.append(messages)
        yield {}

    async def collect():
        return [json.loads(event) async for event in executor.run_agent_stream([{"role": "user", "content": "large request"}])]

    with agent_runtime_context(workspace_root=tmp_path), patch.object(executor, "call_llm_stream", provider), \
         patch.object(executor, "estimate_request_tokens", return_value=1_000_000):
        events = asyncio.run(collect())
    assert calls == []
    assert events[-1]["type"] == "blocked"
    assert "input budget" in events[-1]["message"]
