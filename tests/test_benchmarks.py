import json
import asyncio
from types import SimpleNamespace

import pytest

from apsara_cli.cli.parser import build_parser, dispatch_command
from apsara_cli.engine.evals import (
    aggregate_benchmark_results,
    is_benchmark_suite,
    run_benchmark_suite,
    score_benchmark_case,
    score_benchmark_results,
    verification_is_flaky,
)


def _case():
    return {
        "name": "python-fix",
        "language": "python",
        "allowed_changes": ["src/*.py"],
        "max_tool_calls": 10,
        "max_tokens": 1000,
        "pass_score": 80,
    }


def test_benchmark_score_requires_verification_and_constrained_changes():
    result = score_benchmark_case(_case(), {
        "agent_state": "completed",
        "verification": [{"status": "passed"}],
        "changed_files": ["src/calc.py"],
        "tool_calls": 4,
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    })
    assert result.passed is True
    assert result.score == 100


def test_benchmark_score_detects_test_edit_and_failed_verification():
    result = score_benchmark_case(_case(), {
        "agent_state": "completed",
        "verification": [{"status": "failed"}],
        "changed_files": ["src/calc.py", "tests/test_calc.py"],
        "tool_calls": 11,
        "usage": {"total_tokens": 1200},
    })
    assert result.passed is False
    assert result.score == 15
    assert "tests/test_calc.py" in " ".join(result.checks)


def test_benchmark_cannot_pass_when_fixture_baseline_was_already_green():
    result = score_benchmark_case(_case(), {
        "agent_state": "completed",
        "baseline_failed": False,
        "verification": [{"status": "passed"}],
        "changed_files": ["src/calc.py"],
        "tool_calls": 2,
        "usage": {"total_tokens": 100},
    })
    assert result.score == 100
    assert result.passed is False
    assert "baseline did not fail consistently" in " ".join(result.checks)


def test_benchmark_cannot_pass_without_completed_agent_state():
    result = score_benchmark_case(_case(), {
        "agent_state": "failed",
        "baseline_failed": True,
        "verification": [{"status": "passed"}],
        "changed_files": ["src/calc.py"],
        "tool_calls": 2,
        "usage": {"total_tokens": 100},
    })

    assert result.score == 85
    assert result.passed is False
    assert "agent state: failed" in result.checks


def test_benchmark_rejects_flaky_verification_even_when_last_run_passes():
    result = score_benchmark_case(_case(), {
        "agent_state": "completed",
        "baseline_failed": True,
        "verification": [{"status": "passed", "returncode": 0}],
        "verification_runs": [
            [{"status": "failed", "returncode": 1}],
            [{"status": "passed", "returncode": 0}],
        ],
        "changed_files": ["src/calc.py"],
        "tool_calls": 2,
        "usage": {"total_tokens": 100},
    })

    assert result.passed is False
    assert "flaky" in " ".join(result.checks)


def test_verification_flakiness_uses_status_and_return_code():
    stable = [
        [{"status": "failed", "returncode": 1}],
        [{"status": "failed", "returncode": 1}],
    ]
    changed_code = [
        [{"status": "failed", "returncode": 1}],
        [{"status": "failed", "returncode": 2}],
    ]
    assert verification_is_flaky(stable) is False
    assert verification_is_flaky(changed_code) is True


def test_existing_evidence_can_be_rescored_without_api_calls(tmp_path):
    suite = tmp_path / "suite.json"
    suite.write_text(json.dumps({
        "kind": "coding-benchmark",
        "cases": [_case()],
    }), encoding="utf-8")
    evidence = tmp_path / "results.json"
    evidence.write_text(json.dumps({"cases": [{
        "name": "python-fix",
        "agent_state": "completed",
        "verification": [{"status": "passed"}],
        "changed_files": ["src/calc.py"],
        "tool_calls": 3,
        "usage": {"total_tokens": 200},
    }]}), encoding="utf-8")

    assert is_benchmark_suite(suite) is True
    results = score_benchmark_results(suite, evidence)
    assert len(results) == 1 and results[0].passed


def test_repeated_evidence_keeps_every_trial_when_rescored(tmp_path):
    suite = tmp_path / "suite.json"
    suite.write_text(json.dumps({
        "kind": "coding-benchmark",
        "cases": [_case()],
    }), encoding="utf-8")
    evidence = tmp_path / "results.json"
    base = {
        "name": "python-fix",
        "agent_state": "completed",
        "baseline_failed": True,
        "verification": [{"status": "passed"}],
        "changed_files": ["src/calc.py"],
        "tool_calls": 3,
        "usage": {"total_tokens": 200},
    }
    evidence.write_text(json.dumps({"cases": [
        {**base, "trial": 1},
        {**base, "trial": 2},
    ]}), encoding="utf-8")

    results = score_benchmark_results(suite, evidence)

    assert len(results) == 2
    assert [result.details["trial"] for result in results] == [1, 2]


def test_aggregate_reports_variance_failures_and_release_thresholds(tmp_path):
    suite = tmp_path / "suite.json"
    suite.write_text(json.dumps({
        "kind": "coding-benchmark",
        "thresholds": {
            "min_pass_rate": 0.5,
            "max_unstable_cases": 1,
        },
        "cases": [_case()],
    }), encoding="utf-8")
    passed = score_benchmark_case(_case(), {
        "agent_state": "completed",
        "baseline_failed": True,
        "verification": [{"status": "passed"}],
        "changed_files": ["src/calc.py"],
        "tool_calls": 2,
        "usage": {"total_tokens": 100},
    })
    unsafe = score_benchmark_case(_case(), {
        "agent_state": "completed",
        "baseline_failed": True,
        "verification": [{"status": "passed"}],
        "changed_files": ["tests/test_calc.py"],
        "tool_calls": 6,
        "usage": {"total_tokens": 300},
    })

    blocked = aggregate_benchmark_results(suite, [passed, unsafe])

    assert blocked["passed"] is False
    assert blocked["unsafe_trials"] == 1

    payload = json.loads(suite.read_text(encoding="utf-8"))
    payload["thresholds"]["max_unsafe_trials"] = 1
    suite.write_text(json.dumps(payload), encoding="utf-8")
    aggregate = aggregate_benchmark_results(suite, [passed, unsafe])

    assert aggregate["passed"] is True
    assert aggregate["pass_rate"] == 0.5
    assert aggregate["unstable_cases"] == 1
    assert aggregate["unsafe_trials"] == 1
    assert aggregate["tool_calls"]["average"] == 4
    assert aggregate["tool_calls"]["variance"] == 4
    assert aggregate["tokens"]["variance"] == 10_000
    assert aggregate["failure_categories"]["unsafe_edit"] == 1
    assert aggregate_benchmark_results(
        suite, [passed, unsafe], min_pass_rate=0.75
    )["passed"] is False


def test_live_benchmark_rejects_case_name_path_traversal(tmp_path):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "file.py").write_text("x = 1\n", encoding="utf-8")
    suite = tmp_path / "suite.json"
    suite.write_text(json.dumps({
        "kind": "coding-benchmark",
        "cases": [{
            "name": "../escape",
            "language": "python",
            "fixture": "fixture",
            "instruction": "do nothing",
            "verify": [],
        }],
    }), encoding="utf-8")

    with pytest.raises(ValueError, match="Unsafe benchmark case name"):
        asyncio.run(run_benchmark_suite(suite, tmp_path / "output", "test/model"))


def test_live_benchmark_rejects_zero_verification_repeats(tmp_path):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "file.py").write_text("x = 1\n", encoding="utf-8")
    suite = tmp_path / "suite.json"
    suite.write_text(json.dumps({
        "kind": "coding-benchmark",
        "verification_repeats": 0,
        "cases": [{
            "name": "invalid-repeats",
            "language": "python",
            "fixture": "fixture",
            "instruction": "do nothing",
            "verify": [["python", "-m", "unittest", "-q"]],
        }],
    }), encoding="utf-8")

    with pytest.raises(ValueError, match="verification_repeats"):
        asyncio.run(run_benchmark_suite(suite, tmp_path / "output", "test/model"))


@pytest.mark.parametrize("agent_timeout", [0, -1, 3601])
def test_live_benchmark_rejects_invalid_agent_timeout(tmp_path, agent_timeout):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "app.py").write_text("broken = True\n", encoding="utf-8")
    suite = tmp_path / "suite.json"
    suite.write_text(json.dumps({
        "kind": "coding-benchmark",
        "name": "invalid-agent-timeout",
        "agent_timeout": agent_timeout,
        "cases": [{
            "name": "python-fix",
            "language": "python",
            "fixture": "fixture",
            "instruction": "fix app.py",
            "verify": [["python", "-m", "unittest", "-q"]],
            "allowed_changes": ["app.py"],
        }],
    }), encoding="utf-8")

    with pytest.raises(ValueError, match="agent_timeout"):
        asyncio.run(run_benchmark_suite(suite, tmp_path / "output", "test/model"))


def test_live_benchmark_repeats_trials_and_detects_flaky_verification(
    tmp_path, monkeypatch
):
    from apsara_cli.engine import evals, executor, tools

    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "app.py").write_text("broken = True\n", encoding="utf-8")
    suite = tmp_path / "suite.json"
    suite.write_text(json.dumps({
        "kind": "coding-benchmark",
        "name": "repeat-smoke",
        "verification_repeats": 2,
        "cases": [{
            "name": "python-fix",
            "language": "python",
            "fixture": "fixture",
            "instruction": "fix app.py",
            "verify": [["python", "-m", "unittest", "-q"]],
            "allowed_changes": ["app.py"],
        }],
    }), encoding="utf-8")

    calls = 0

    async def fake_verify(_commands, _workspace, _timeout):
        nonlocal calls
        calls += 1
        # Trial 1: stable failing baseline, stable passing final.
        # Trial 2: stable failing baseline, flaky final verification.
        outcomes = [1, 1, 0, 0, 1, 1, 0, 1]
        code = outcomes[calls - 1]
        return [{
            "command": ["python", "-m", "unittest", "-q"],
            "status": "passed" if code == 0 else "failed",
            "returncode": code,
        }]

    async def fake_agent(_history, model):
        workspace = tools._workspace_root()
        (workspace / "app.py").write_text("broken = False\n", encoding="utf-8")
        yield json.dumps({"type": "run_state", "state": "completed"})
        yield json.dumps({
            "type": "usage",
            "data": {"total_tokens": 50, "apsara_model": model},
        })

    monkeypatch.setattr(evals, "_run_verification_commands", fake_verify)
    monkeypatch.setattr(executor, "run_agent_stream", fake_agent)

    results, results_path = asyncio.run(run_benchmark_suite(
        suite,
        tmp_path / "output",
        "test/model",
        repeats=2,
    ))
    evidence = json.loads(results_path.read_text(encoding="utf-8"))
    summary = json.loads(results_path.with_name("summary.json").read_text(encoding="utf-8"))

    assert calls == 8
    assert len(results) == 2
    assert results[0].passed is True
    assert results[1].passed is False
    assert evidence["schema_version"] == 2
    assert evidence["repeat_count"] == 2
    assert evidence["cases"][1]["verification_flaky"] is True
    assert summary["verification_flaky_trials"] == 1
    assert summary["unstable_cases"] == 1
    assert (results_path.parent / "python-fix" / "trial-2").is_dir()


def test_live_benchmark_skips_provider_when_baseline_is_invalid(
    tmp_path, monkeypatch
):
    from apsara_cli.engine import evals, executor

    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "app.py").write_text("already_fixed = True\n", encoding="utf-8")
    suite = tmp_path / "suite.json"
    suite.write_text(json.dumps({
        "kind": "coding-benchmark",
        "name": "invalid-baseline",
        "verification_repeats": 2,
        "cases": [{
            "name": "python-fix",
            "language": "python",
            "fixture": "fixture",
            "instruction": "fix app.py",
            "verify": [["python", "-m", "unittest", "-q"]],
            "allowed_changes": ["app.py"],
        }],
    }), encoding="utf-8")
    outcomes = iter([1, 0])

    async def fake_verify(_commands, _workspace, _timeout):
        code = next(outcomes)
        return [{
            "command": ["python", "-m", "unittest", "-q"],
            "status": "passed" if code == 0 else "failed",
            "returncode": code,
        }]

    async def unexpected_agent(_history, model):
        raise AssertionError(f"provider should not be called for {model}")
        yield  # pragma: no cover

    monkeypatch.setattr(evals, "_run_verification_commands", fake_verify)
    monkeypatch.setattr(executor, "run_agent_stream", unexpected_agent)

    results, results_path = asyncio.run(run_benchmark_suite(
        suite, tmp_path / "output", "test/model"
    ))
    evidence = json.loads(results_path.read_text(encoding="utf-8"))

    assert results[0].passed is False
    assert evidence["cases"][0]["agent_state"] == "skipped_invalid_baseline"
    assert evidence["cases"][0]["baseline_verification_flaky"] is True
    assert evidence["cases"][0]["usage"]["total_tokens"] == 0


def test_live_benchmark_records_agent_timeout_as_failed_trial(tmp_path, monkeypatch):
    from apsara_cli.engine import evals, executor

    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "app.py").write_text("broken = True\n", encoding="utf-8")
    suite = tmp_path / "suite.json"
    suite.write_text(json.dumps({
        "kind": "coding-benchmark",
        "name": "timeout-smoke",
        "agent_timeout": 0.01,
        "cases": [{
            "name": "python-fix",
            "language": "python",
            "fixture": "fixture",
            "instruction": "fix app.py",
            "verify": [["python", "-m", "unittest", "-q"]],
            "allowed_changes": ["app.py"],
        }],
    }), encoding="utf-8")

    async def fake_verify(_commands, _workspace, _timeout):
        return [{"command": ["python"], "status": "failed", "returncode": 1}]

    async def slow_agent(_history, model):
        del model
        await asyncio.sleep(1)
        yield json.dumps({"type": "run_state", "state": "completed"})

    monkeypatch.setattr(evals, "_run_verification_commands", fake_verify)
    monkeypatch.setattr(executor, "run_agent_stream", slow_agent)

    results, results_path = asyncio.run(run_benchmark_suite(
        suite, tmp_path / "output", "test/model"
    ))
    evidence = json.loads(results_path.read_text(encoding="utf-8"))
    events = json.loads(
        (results_path.parent / "python-fix" / "trial-1" / ".apsara" / "benchmark-events.json")
        .read_text(encoding="utf-8")
    )

    assert results[0].passed is False
    assert evidence["cases"][0]["agent_state"] == "failed"
    assert evidence["cases"][0]["agent_timeout_seconds"] == 0.01
    assert events[-1]["type"] == "error"
    assert "timed out" in events[-1]["message"]


def test_eval_cli_accepts_live_and_offline_benchmark_modes():
    parser = build_parser()
    live = parser.parse_args([
        "eval", "suite.json", "--live", "--model", "model/id",
        "--repeat", "3", "--min-pass-rate", "80",
    ])
    offline = parser.parse_args(["eval", "suite.json", "--results", "results.json"])
    assert live.live is True and live.model == "model/id"
    assert live.repeat == 3 and live.min_pass_rate == 0.8
    assert offline.results == "results.json" and offline.live is False


def test_eval_cli_rejects_invalid_repeat_and_pass_rate():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["eval", "suite.json", "--repeat", "0"])
    with pytest.raises(SystemExit):
        parser.parse_args(["eval", "suite.json", "--min-pass-rate", "101"])


def test_eval_cli_offline_exit_uses_aggregate_release_gate(tmp_path, capsys):
    suite = tmp_path / "suite.json"
    suite.write_text(json.dumps({
        "kind": "coding-benchmark",
        "thresholds": {"min_pass_rate": 0.5, "max_unstable_cases": 1},
        "cases": [_case()],
    }), encoding="utf-8")
    base = {
        "name": "python-fix",
        "agent_state": "completed",
        "baseline_failed": True,
        "changed_files": ["src/calc.py"],
        "tool_calls": 3,
        "usage": {"total_tokens": 200},
    }
    evidence = tmp_path / "results.json"
    evidence.write_text(json.dumps({"cases": [
        {**base, "trial": 1, "verification": [{"status": "passed"}]},
        {**base, "trial": 2, "verification": [{"status": "failed"}]},
    ]}), encoding="utf-8")
    args = build_parser().parse_args([
        "eval", str(suite), "--results", str(evidence),
    ])

    exit_code = asyncio.run(dispatch_command(
        args, SimpleNamespace(defaults=SimpleNamespace(model="test/model"))
    ))

    assert exit_code == 0
    assert "PASS aggregate: 1/2 trials passed (50.0%)" in capsys.readouterr().out


def test_eval_cli_rejects_repeat_with_offline_evidence(tmp_path, capsys):
    suite = tmp_path / "suite.json"
    suite.write_text(json.dumps({
        "kind": "coding-benchmark",
        "cases": [],
    }), encoding="utf-8")
    evidence = tmp_path / "results.json"
    evidence.write_text(json.dumps({"cases": []}), encoding="utf-8")
    args = build_parser().parse_args([
        "eval", str(suite), "--results", str(evidence), "--repeat", "2",
    ])

    exit_code = asyncio.run(dispatch_command(
        args, SimpleNamespace(defaults=SimpleNamespace(model="test/model"))
    ))

    assert exit_code == 2
    assert "--repeat is only valid with --live" in capsys.readouterr().out
