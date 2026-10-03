import asyncio
import json

import pytest

from apsara_cli.engine import evals, executor, tools
from apsara_cli.engine.evals import _apply_case_setup, compare_benchmark_results, run_benchmark_suite


def test_comparison_counterbalances_trials_and_records_auxiliary_usage(monkeypatch, tmp_path):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "app.py").write_text("broken")
    suite = tmp_path / "suite.json"
    suite.write_text(json.dumps({"kind": "coding-benchmark", "name": "paired", "cases": [{
        "name": "repair", "fixture": "fixture", "instruction": "repair app.py",
        "verify": [["python", "-m", "unittest"]], "allowed_changes": ["app.py"],
    }]}))
    order = []
    async def verify(_commands, workspace, _timeout):
        code = 1 if (workspace / "app.py").read_text() == "broken" else 0
        return [{"command": ["python"], "returncode": code, "status": "failed" if code else "passed"}]
    async def agent(_messages, model, *, profile="optimized"):
        order.append(profile)
        (tools._workspace_root() / "app.py").write_text("fixed")
        yield json.dumps({"type": "request_context", "schema_count": 15 if profile == "optimized" else 40})
        yield json.dumps({"type": "usage", "data": {"total_tokens": 80, "provider_reported_calls": 1}})
        yield json.dumps({"type": "usage", "data": {"total_tokens": 20, "provider_reported_calls": 1, "auxiliary_calls": 1}})
        yield json.dumps({"type": "run_state", "state": "completed_verified"})
    monkeypatch.setattr(evals, "_run_verification_commands", verify)
    monkeypatch.setattr(executor, "run_agent_stream", agent)
    results, path = asyncio.run(run_benchmark_suite(suite, tmp_path / "output", "test/model", repeats=2, compare=True))
    assert order == ["optimized", "reference", "reference", "optimized"]
    assert len(results) == 2 and all(result.passed for result in results)
    report = json.loads((path.parent.parent / "comparison.json").read_text())
    assert report["total_tokens"]["optimized"]["value"] == 100
    assert report["sample_counts"] == {"optimized": 2, "reference": 2}
    assert report["outcomes"]["optimized"]["verified_success_rate"] == 1
    assert results[0].details["usage"]["auxiliary_calls"] == 1
    assert results[0].details["request_context"][0]["schema_count"] == 15


def test_partial_usage_cannot_be_reported_as_measured_total(tmp_path):
    row = {"name": "repair", "agent_state": "completed_verified", "usage": {
        "total_tokens": 100, "unreported_calls": 1,
    }, "verification": [{"status": "failed", "returncode": 1}], "unexpected_changes": ["test.py"]}
    payload = {"model": "test/model", "suite": "same", "cases": [row]}
    paths = [tmp_path / "optimized.json", tmp_path / "reference.json"]
    for path in paths:
        path.write_text(json.dumps(payload))
    report = compare_benchmark_results(*paths)
    assert report["total_tokens"]["optimized"]["value"] is None
    assert report["unknown_usage_trials"]["optimized"] == 1
    assert report["outcomes"]["optimized"]["false_completion_trials"] == 1
    assert report["outcomes"]["optimized"]["unexpected_edit_trials"] == 1
    paths[1].write_text(json.dumps({**payload, "model": "another/model"}))
    with pytest.raises(ValueError, match="different model"):
        compare_benchmark_results(*paths)


def test_setup_requires_exact_matches_and_confined_support_files(tmp_path):
    suite = tmp_path / "suite.json"
    suite.write_text("{}")
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "app.py").write_text("before before")
    with pytest.raises(ValueError, match="exactly once"):
        _apply_case_setup({"regressions": [{"path": "app.py", "before": "before", "after": "after"}]}, suite, workspace)
    with pytest.raises(ValueError, match="Unsafe"):
        _apply_case_setup({"support_files": {"../outside.py": "suite.json"}}, suite, workspace)
    with pytest.raises(ValueError):
        _apply_case_setup({"support_files": {"new.py": "../outside.py"}}, suite, workspace)
    with pytest.raises(ValueError):
        _apply_case_setup({"support_files": {"app.py": "suite.json"}}, suite, workspace)


def test_reference_profile_keeps_permissions_and_changes_only_exposed_tools(tmp_path):
    from apsara_cli.engine.capabilities import capability_context
    with tools.agent_runtime_context(workspace_root=tmp_path, enable_bash=False):
        with capability_context():
            optimized = tools.get_request_tools()
        with capability_context(eager=True):
            reference = tools.get_request_tools()
        assert len(reference) > len(optimized)
        assert "run_bash_command" not in {item["function"]["name"] for item in reference}
