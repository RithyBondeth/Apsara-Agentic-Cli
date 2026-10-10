"""Deterministic run regression and optional live coding benchmarks."""

import asyncio
import hashlib
import json
import re
import shlex
import shutil
import statistics
import subprocess
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from apsara_cli.engine.usage import add_usage, normalize_usage


@dataclass
class EvalResult:
    name: str
    passed: bool
    checks: list[str]


@dataclass
class BenchmarkResult:
    name: str
    language: str
    passed: bool
    score: int
    checks: list[str]
    details: dict[str, Any]


def evaluate_run(name: str, state: dict[str, Any], events: list[dict[str, Any]], expect: dict[str, Any]) -> EvalResult:
    tools = [event.get("name") for event in events if event.get("type") == "tool_result"]
    checks = []
    if "state" in expect:
        checks.append(f"state={state.get('state')} expected={expect['state']}")
    if "tools" in expect:
        missing = sorted(set(expect["tools"]) - set(tools))
        checks.append("tools present" if not missing else f"missing tools: {', '.join(missing)}")
    if "max_tool_calls" in expect:
        checks.append(f"tool_calls={len(tools)} max={expect['max_tool_calls']}")
    passed = (
        ("state" not in expect or state.get("state") == expect["state"])
        and ("tools" not in expect or set(expect["tools"]).issubset(tools))
        and ("max_tool_calls" not in expect or len(tools) <= int(expect["max_tool_calls"]))
    )
    return EvalResult(name, passed, checks)


def run_suite(suite_path: Path, workspace: Path) -> list[EvalResult]:
    suite = json.loads(suite_path.read_text(encoding="utf-8"))
    results = []
    for case in suite.get("cases", []):
        run_id = case["run_id"]
        root = workspace / ".apsara" / "runs" / run_id
        state = json.loads((root / "state.json").read_text(encoding="utf-8"))
        events = [json.loads(line) for line in (root / "events.jsonl").read_text(encoding="utf-8").splitlines() if line]
        results.append(evaluate_run(case.get("name", run_id), state, events, case.get("expect", {})))
    return results


def is_benchmark_suite(suite_path: Path) -> bool:
    payload = json.loads(suite_path.read_text(encoding="utf-8"))
    return payload.get("kind") == "coding-benchmark"


def _hashes(root: Path) -> dict[str, str]:
    ignored = {".git", ".apsara", ".apsara-cli", "node_modules", "target", "__pycache__"}
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or any(part in ignored for part in path.relative_to(root).parts):
            continue
        result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _changed_files(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))


def _matches_any(path: str, patterns: list[str]) -> bool:
    return any(Path(path).match(pattern) for pattern in patterns)


def _verification_signature(results: list[dict[str, Any]]) -> tuple[tuple[Any, Any], ...]:
    return tuple((item.get("status"), item.get("returncode")) for item in results)


def verification_is_flaky(runs: list[list[dict[str, Any]]]) -> bool:
    """Return whether repeated verification produced inconsistent outcomes."""
    return len({_verification_signature(run) for run in runs}) > 1


def _verification_passed(runs: list[list[dict[str, Any]]]) -> bool:
    return bool(runs) and all(
        bool(run) and all(item.get("status") == "passed" for item in run)
        for run in runs
    )


def _unexpected_changes(case: dict[str, Any], details: dict[str, Any]) -> list[str]:
    changed = [str(path) for path in details.get("changed_files") or []]
    allowed = [str(pattern) for pattern in case.get("allowed_changes") or []]
    return [path for path in changed if allowed and not _matches_any(path, allowed)]


def _complete_usage_total(details: dict[str, Any]) -> int | None:
    usage = normalize_usage(details.get("usage") or {})
    if (details.get("usage_complete") is False or usage["unreported_calls"]
            or usage["interrupted_calls"] or usage["total_tokens"] == 0):
        return None
    return usage["total_tokens"]


def score_benchmark_case(case: dict[str, Any], details: dict[str, Any]) -> BenchmarkResult:
    """Score stored benchmark evidence without making a provider request."""
    checks: list[str] = []
    score = 0
    state_ok = details.get("agent_state") in {"completed", "completed_verified"}
    score += 15 if state_ok else 0
    checks.append(f"agent state: {details.get('agent_state', 'unknown')}")

    verification = details.get("verification") or []
    verification_runs = details.get("verification_runs") or [verification]
    verification_ok = _verification_passed(verification_runs)
    flaky = bool(details.get("verification_flaky")) or verification_is_flaky(
        verification_runs
    )
    score += 50 if verification_ok else 0
    checks.append("verification passed" if verification_ok else "verification failed or unavailable")
    if flaky:
        checks.append("verification outcomes were flaky")
    baseline_valid = bool(details.get("baseline_failed", True))
    checks.append(
        "fixture failed before agent"
        if baseline_valid
        else "invalid fixture: baseline did not fail consistently"
    )

    changed = [str(path) for path in details.get("changed_files") or []]
    unexpected = _unexpected_changes(case, details)
    change_ok = bool(changed) and not unexpected
    score += 15 if change_ok else 0
    checks.append(
        f"changes constrained ({len(changed)} file(s))"
        if change_ok else f"unexpected or missing changes: {', '.join(unexpected) or 'none recorded'}"
    )

    tool_calls = int(details.get("tool_calls") or 0)
    max_tools = int(case.get("max_tool_calls") or 25)
    tools_ok = tool_calls <= max_tools
    score += 10 if tools_ok else 0
    checks.append(f"tool calls: {tool_calls}/{max_tools}")

    usage = normalize_usage(details.get("usage") or {})
    max_tokens = int(case.get("max_tokens") or 100_000)
    usage_known = _complete_usage_total(details) is not None
    tokens_ok = usage_known and usage["total_tokens"] <= max_tokens
    score += 10 if tokens_ok else 0
    checks.append(f"tokens: {usage['total_tokens']}/{max_tokens}" if usage_known else "tokens: total usage unknown or incomplete")

    passed = (
        state_ok
        and baseline_valid
        and verification_ok
        and not flaky
        and change_ok
        and score >= int(case.get("pass_score") or 80)
    )
    return BenchmarkResult(
        name=str(case.get("name") or "unnamed"),
        language=str(case.get("language") or "unknown"),
        passed=passed,
        score=score,
        checks=checks,
        details=details,
    )


def score_benchmark_results(suite_path: Path, results_path: Path) -> list[BenchmarkResult]:
    suite = json.loads(suite_path.read_text(encoding="utf-8"))
    evidence = json.loads(results_path.read_text(encoding="utf-8"))
    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in evidence.get("cases", []):
        if isinstance(item, dict):
            by_name[str(item.get("name"))].append(item)
    results: list[BenchmarkResult] = []
    for case in suite.get("cases", []):
        matching = by_name.get(str(case.get("name"))) or [
            {"agent_state": "missing", "verification": []}
        ]
        results.extend(score_benchmark_case(case, details) for details in matching)
    return results


def benchmark_failure_categories(
    case: dict[str, Any], result: BenchmarkResult
) -> list[str]:
    """Return stable machine-readable reasons for a failed benchmark trial."""
    details = result.details
    categories: list[str] = []
    if details.get("agent_state") not in {"completed", "completed_verified"}:
        categories.append("agent_state")
    verification = details.get("verification") or []
    verification_runs = details.get("verification_runs") or [verification]
    if not _verification_passed(verification_runs):
        categories.append("verification_failed")
    if bool(details.get("verification_flaky")) or verification_is_flaky(
        verification_runs
    ):
        categories.append("verification_flaky")
    if not bool(details.get("baseline_failed", True)):
        categories.append("baseline_invalid")
    changed = [str(path) for path in details.get("changed_files") or []]
    if not changed:
        categories.append("no_changes")
    if _unexpected_changes(case, details):
        categories.append("unsafe_edit")
    if int(details.get("tool_calls") or 0) > int(case.get("max_tool_calls") or 25):
        categories.append("tool_budget")
    usage = normalize_usage(details.get("usage") or {})
    if usage["total_tokens"] > int(case.get("max_tokens") or 100_000):
        categories.append("token_budget")
    if not categories and not result.passed:
        categories.append("score_threshold")
    return categories


def _thresholds(suite: dict[str, Any], min_pass_rate: Any = None) -> dict[str, Any]:
    configured = suite.get("thresholds", {})
    if configured is None:
        configured = {}
    if not isinstance(configured, dict):
        raise ValueError("Benchmark thresholds must be an object")
    rate = configured.get("min_pass_rate", 1.0) if min_pass_rate is None else min_pass_rate
    try:
        rate = float(rate)
        max_flaky = int(configured.get("max_verification_flaky_trials", 0))
        max_unstable = int(configured.get("max_unstable_cases", 0))
        max_unsafe = int(configured.get("max_unsafe_trials", 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("Benchmark thresholds must be numeric") from exc
    if not 0 <= rate <= 1:
        raise ValueError("min_pass_rate must be between 0 and 1")
    if min(max_flaky, max_unstable, max_unsafe) < 0:
        raise ValueError("Benchmark count thresholds cannot be negative")
    return {
        "min_pass_rate": rate,
        "max_verification_flaky_trials": max_flaky,
        "max_unstable_cases": max_unstable,
        "max_unsafe_trials": max_unsafe,
    }


def _metric_summary(values: list[int]) -> dict[str, float | int]:
    if not values:
        return {"average": 0, "variance": 0, "minimum": 0, "maximum": 0}
    return {
        "average": round(statistics.mean(values), 4),
        "variance": round(statistics.pvariance(values), 4),
        "minimum": min(values),
        "maximum": max(values),
    }


def aggregate_benchmark_results(
    suite_path: Path,
    results: list[BenchmarkResult],
    *,
    min_pass_rate: Any = None,
) -> dict[str, Any]:
    """Aggregate repeated trials and evaluate release thresholds."""
    suite = json.loads(suite_path.read_text(encoding="utf-8"))
    thresholds = _thresholds(suite, min_pass_rate)
    cases_by_name = {
        str(case.get("name")): case for case in suite.get("cases", [])
    }
    grouped: dict[str, list[BenchmarkResult]] = defaultdict(list)
    failures: Counter[str] = Counter()
    unsafe_trials = 0
    flaky_trials = 0
    for result in results:
        grouped[result.name].append(result)
        case = cases_by_name.get(result.name, {})
        categories = benchmark_failure_categories(case, result)
        failures.update(categories)
        unsafe_trials += int("unsafe_edit" in categories)
        flaky_trials += int("verification_flaky" in categories)

    case_summaries: list[dict[str, Any]] = []
    unstable_cases = 0
    for name, trials in grouped.items():
        passed = sum(result.passed for result in trials)
        outcomes = {result.passed for result in trials}
        unstable = len(outcomes) > 1
        unstable_cases += int(unstable)
        if unstable:
            failures.update(["unstable_case"])
        case_summaries.append({
            "name": name,
            "language": trials[0].language,
            "trials": len(trials),
            "passed_trials": passed,
            "pass_rate": round(passed / len(trials), 4),
            "average_score": round(statistics.mean(result.score for result in trials), 4),
            "unstable": unstable,
        })

    total = len(results)
    passed_trials = sum(result.passed for result in results)
    pass_rate = passed_trials / total if total else 0.0
    release_passed = (
        total > 0
        and pass_rate >= thresholds["min_pass_rate"]
        and flaky_trials <= thresholds["max_verification_flaky_trials"]
        and unstable_cases <= thresholds["max_unstable_cases"]
        and unsafe_trials <= thresholds["max_unsafe_trials"]
    )
    token_values = [_complete_usage_total(result.details) for result in results]
    known_tokens = [value for value in token_values if value is not None]
    token_metrics = _metric_summary(known_tokens) if known_tokens else {
        "average": None, "variance": None, "minimum": None, "maximum": None,
    }
    token_metrics.update({"sample_count": len(known_tokens), "unknown_trials": total - len(known_tokens)})
    return {
        "passed": release_passed,
        "thresholds": thresholds,
        "trials": total,
        "passed_trials": passed_trials,
        "pass_rate": round(pass_rate, 4),
        "verification_flaky_trials": flaky_trials,
        "unstable_cases": unstable_cases,
        "unsafe_trials": unsafe_trials,
        "scores": _metric_summary([result.score for result in results]),
        "tool_calls": _metric_summary([
            int(result.details.get("tool_calls") or 0) for result in results
        ]),
        "tokens": token_metrics,
        "failure_categories": dict(sorted(failures.items())),
        "cases": sorted(case_summaries, key=lambda item: item["name"]),
    }


def format_benchmark_aggregate(aggregate: dict[str, Any]) -> str:
    status = "PASS" if aggregate.get("passed") else "FAIL"
    rate = float(aggregate.get("pass_rate") or 0) * 100
    scores = aggregate.get("scores") or {}
    tools = aggregate.get("tool_calls") or {}
    tokens = aggregate.get("tokens") or {}
    lines = [
        f"{status} aggregate: {aggregate.get('passed_trials', 0)}/{aggregate.get('trials', 0)} "
        f"trials passed ({rate:.1f}%)",
        f"score avg {scores.get('average', 0)}; "
        f"tool calls avg {tools.get('average', 0)} var {tools.get('variance', 0)}; "
        f"tokens avg {tokens.get('average') if tokens.get('average') is not None else 'unknown'} "
        f"var {tokens.get('variance') if tokens.get('variance') is not None else 'unknown'} "
        f"({tokens.get('unknown_trials', 0)} trial(s) without complete usage)",
        f"stability: {aggregate.get('verification_flaky_trials', 0)} flaky trial(s), "
        f"{aggregate.get('unstable_cases', 0)} unstable case(s), "
        f"{aggregate.get('unsafe_trials', 0)} unsafe trial(s)",
    ]
    failures = aggregate.get("failure_categories") or {}
    if failures:
        lines.append(
            "failures: " + ", ".join(f"{key}={value}" for key, value in failures.items())
        )
    return "\n".join(lines)


def compare_benchmark_results(optimized_path: Path, reference_path: Path) -> dict[str, Any]:
    """Compare independent benchmark evidence without inventing missing usage."""
    optimized = json.loads(optimized_path.read_text(encoding="utf-8"))
    reference = json.loads(reference_path.read_text(encoding="utf-8"))
    for key in ("model", "suite", "suite_digest"):
        if optimized.get(key) != reference.get(key):
            raise ValueError(f"Cannot compare benchmarks with different {key}.")
    def trials(payload: dict[str, Any]) -> list[dict[str, Any]]:
        return [item for item in payload.get("cases", []) if isinstance(item, dict)]
    left, right = trials(optimized), trials(reference)
    if not left or Counter(row.get("name") for row in left) != Counter(row.get("name") for row in right):
        raise ValueError("Comparison requires matching cases and trial counts.")
    def metric(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
        values = [float(row[key]) for row in rows if isinstance(row.get(key), (int, float))]
        if not values:
            return {"value": None, "sample_count": 0, "status": "unknown"}
        return {"value": round(statistics.mean(values), 3), "variance": round(statistics.pvariance(values), 3),
                "sample_count": len(values), "status": "measured"}
    left_tokens = [_complete_usage_total(row) for row in left]
    right_tokens = [_complete_usage_total(row) for row in right]
    def average(values: list[int | None]) -> dict[str, Any]:
        known = [value for value in values if value is not None]
        return {"value": round(statistics.mean(known), 3), "variance": round(statistics.pvariance(known), 3),
                "sample_count": len(known), "status": "measured"} if known else {"value": None, "sample_count": 0, "status": "unknown"}
    def verified(row):
        return _verification_passed(row.get("verification_runs") or [row.get("verification") or []]) and not row.get("verification_flaky")
    def qualified(row):
        return (row.get("agent_state") in {"completed", "completed_verified"} and verified(row)
                and row.get("baseline_failed") is True and not row.get("baseline_verification_flaky")
                and bool(row.get("changed_files")) and not row.get("unexpected_changes"))
    def successful_work(rows):
        successes = [row for row in rows if qualified(row)]
        totals = [_complete_usage_total(row) for row in rows]
        measured = [row for row in successes if _complete_usage_total(row) is not None]
        return {
            "completed_trials": len(successes), "measured_completed_trials": len(measured),
            "total_tokens": average([_complete_usage_total(row) for row in successes]),
            "latency_seconds": metric(measured, "latency_seconds"),
            # Failures consume tokens too. Do not hide that cost in the mean
            # of only the lucky successful runs, or mix incomplete totals in.
            "all_trial_tokens_per_completion": (
                round(sum(totals) / len(successes), 3)
                if successes and all(value is not None for value in totals) else None
            ),
        }
    def pair_key(row):
        trial = row.get("trial")
        return (row.get("name"), trial) if isinstance(trial, int) and not isinstance(trial, bool) and trial > 0 else None
    keys_left, keys_right = Counter(pair_key(r) for r in left), Counter(pair_key(r) for r in right)
    right_by_key = {pair_key(r): r for r in right if keys_right[pair_key(r)] == 1}
    pairs = []
    for row in left:
        key = pair_key(row)
        other = right_by_key.get(key)
        if key is None or keys_left[key] != 1 or other is None or not (qualified(row) and qualified(other)):
            continue
        opt, ref = _complete_usage_total(row), _complete_usage_total(other)
        if opt is not None and ref is not None:
            pairs.append({"case": key[0], "trial": key[1], "optimized_tokens": opt, "reference_tokens": ref})
    covered = Counter(p["case"] for p in pairs)
    sufficient = bool(covered) and all(covered[name] >= 3 for name in {r.get("name") for r in left})
    matched = {
        "sample_count": len(pairs), "pairs": pairs,
        "optimized_tokens": average([p["optimized_tokens"] for p in pairs]),
        "reference_tokens": average([p["reference_tokens"] for p in pairs]),
        "observed_savings_percent": (round(100 * (1 - sum(p["optimized_tokens"] for p in pairs)
            / sum(p["reference_tokens"] for p in pairs)), 3) if pairs else None),
        "status": "repeated_matched_samples" if sufficient else "insufficient_repeated_samples",
        "scope": "Successful, constrained, independently verified repairs with complete usage; matched by case and trial. Not a general causal claim.",
    }
    def summary(rows):
        return {
            "verified_success_rate": round(sum(verified(row) for row in rows) / len(rows), 4),
            "false_completion_trials": sum(row.get("agent_state") in {"completed", "completed_verified"} and not verified(row) for row in rows),
            "unexpected_edit_trials": sum(bool(row.get("unexpected_changes")) for row in rows),
        }
    return {
        "schema_version": 1,
        "optimized": str(optimized_path),
        "reference": str(reference_path),
        "sample_counts": {"optimized": len(left), "reference": len(right)},
        "pass_rate": {"optimized": optimized.get("aggregate", {}).get("pass_rate"), "reference": reference.get("aggregate", {}).get("pass_rate")},
        "latency_seconds": {"optimized": metric(left, "latency_seconds"), "reference": metric(right, "latency_seconds")},
        "total_tokens": {"optimized": average(left_tokens), "reference": average(right_tokens)},
        "unknown_usage_trials": {"optimized": sum(value is None for value in left_tokens), "reference": sum(value is None for value in right_tokens)},
        "outcomes": {"optimized": summary(left), "reference": summary(right)},
        "successful_work": {"optimized": successful_work(left), "reference": successful_work(right)},
        "matched_successful_work": matched,
        "quality_claim": "pass rates are reported; no quality improvement is claimed without comparable measured samples",
    }


def _verification_command(raw: Any) -> list[str]:
    if isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        return raw
    if isinstance(raw, str):
        return shlex.split(raw)
    raise ValueError("Verification commands must be a string or string array")


def _validate_benchmark_tree(workspace: Path) -> None:
    for candidate in workspace.rglob("*"):
        if candidate.is_symlink():
            candidate.resolve().relative_to(workspace.resolve())


def _apply_case_setup(case: dict[str, Any], suite_path: Path, workspace: Path) -> None:
    """Inject exact reproducible regressions and protected verification harnesses."""
    def target(raw: str) -> Path:
        relative = Path(raw)
        if relative.is_absolute() or ".." in relative.parts or relative.parts[:1] in {(".git",), (".apsara",)}:
            raise ValueError(f"Unsafe benchmark setup path: {raw}")
        path = workspace / relative
        path.resolve().relative_to(workspace.resolve())
        if path.is_symlink():
            raise ValueError("Benchmark setup cannot modify symlinks")
        return path
    for patch in case.get("regressions", []):
        path = target(str(patch["path"]))
        before, after = patch["before"], patch["after"]
        content = path.read_text(encoding="utf-8")
        if not isinstance(before, str) or not before or content.count(before) != 1 or not isinstance(after, str):
            raise ValueError(f"Regression must match exactly once: {patch['path']}")
        path.write_text(content.replace(before, after, 1), encoding="utf-8")
    for destination, source in (case.get("support_files") or {}).items():
        path = target(str(destination))
        source_path = suite_path.parent / str(source)
        source_path.resolve().relative_to(suite_path.parent.resolve())
        if path.exists() or source_path.is_symlink() or not source_path.is_file():
            raise ValueError("Support files must be regular files copied to new paths")
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, path)


def _materialize_benchmark_case(
    case: dict[str, Any], suite_path: Path, workspace: Path
) -> None:
    """Copy a fixture or checkout a pinned real repository into a trial."""
    fixture_value = case.get("fixture")
    repository = case.get("repository")
    if bool(fixture_value) == bool(repository):
        raise ValueError("Each benchmark case needs exactly one of fixture or repository")
    if fixture_value:
        fixture = (suite_path.parent / str(fixture_value)).resolve()
        fixture.relative_to(suite_path.parent.resolve())
        if not fixture.is_dir():
            raise FileNotFoundError(f"Benchmark fixture does not exist: {fixture}")
        _validate_benchmark_tree(fixture)
        shutil.copytree(fixture, workspace)
        return

    if not isinstance(repository, dict):
        raise ValueError("repository must be an object with url or path plus a pinned ref")
    ref = str(repository.get("ref") or "")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", ref):
        raise ValueError("Real-repository benchmark refs must be full 40-character commit SHAs")
    if repository.get("url"):
        source = str(repository["url"])
        if not source.startswith("https://"):
            raise ValueError("Remote benchmark repositories must use an https:// URL")
    elif repository.get("path"):
        local = (suite_path.parent / str(repository["path"])).resolve()
        local.relative_to(suite_path.parent.resolve())
        if not local.is_dir():
            raise FileNotFoundError(f"Benchmark repository does not exist: {local}")
        source = str(local)
    else:
        raise ValueError("repository requires url or path")

    cloned = subprocess.run(
        ["git", "clone", "--no-checkout", "--filter=blob:none", source, str(workspace)],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if cloned.returncode != 0:
        raise RuntimeError(cloned.stderr.strip() or "benchmark repository clone failed")
    checked_out = subprocess.run(
        ["git", "-C", str(workspace), "checkout", "--detach", ref],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if checked_out.returncode != 0:
        raise RuntimeError(checked_out.stderr.strip() or f"could not checkout {ref}")
    _validate_benchmark_tree(workspace)


def _prepare_benchmark_agent_workspace(
    workspace: Path, commands: list[list[str]]
) -> None:
    """Give benchmark workspaces an isolated Git baseline and exact verifier."""
    initialized_fixture = not (workspace / ".git").exists()
    if initialized_fixture:
        initialized = subprocess.run(
            ["git", "init", "-q"],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if initialized.returncode != 0:
            raise RuntimeError(
                initialized.stderr.strip() or "benchmark Git initialization failed"
            )

    exclude = workspace / ".git" / "info" / "exclude"
    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    ignored = [".apsara/", "target/", "node_modules/", "__pycache__/", "*.pyc"]
    existing_lines = set(existing.splitlines())
    missing = [pattern for pattern in ignored if pattern not in existing_lines]
    if missing:
        exclude.write_text(
            existing.rstrip() + "\n" + "\n".join(missing) + "\n",
            encoding="utf-8",
        )

    config = workspace / ".apsara" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    encoded_commands = json.dumps(commands, ensure_ascii=False)
    config.write_text(
        f"[verification]\ncommands = {encoded_commands}\n"
        f"targeted_commands = {encoded_commands}\n",
        encoding="utf-8",
    )

    if not initialized_fixture:
        return

    for command in (
        ["git", "add", "-A"],
        [
            "git", "-c", "user.name=Apsara Benchmark",
            "-c", "user.email=benchmark@local.invalid",
            "-c", "core.hooksPath=/dev/null",
            "commit", "-qm", "benchmark baseline",
        ],
    ):
        completed = subprocess.run(
            command,
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or "benchmark Git baseline failed")


async def _run_verification_commands(
    commands: list[list[str]], workspace: Path, timeout: int
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for command in commands:
        executable = shutil.which(command[0]) if command else None
        if not command or not executable:
            results.append({"command": command, "status": "unavailable", "returncode": None})
            continue
        try:
            completed = await asyncio.to_thread(
                subprocess.run,
                command,
                cwd=workspace,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            results.append({
                "command": command,
                "status": "timeout",
                "returncode": None,
                "output": str(exc),
            })
            continue
        results.append({
            "command": command,
            "status": "passed" if completed.returncode == 0 else "failed",
            "returncode": completed.returncode,
            "output": (completed.stdout + completed.stderr)[-4000:],
        })
    return results


async def _run_verification_repeated(
    commands: list[list[str]], workspace: Path, timeout: int, repeat_count: int
) -> list[list[dict[str, Any]]]:
    return [
        await _run_verification_commands(commands, workspace, timeout)
        for _ in range(repeat_count)
    ]


async def run_benchmark_suite(
    suite_path: Path,
    output_root: Path,
    model: str,
    *,
    repeats: int = 1,
    min_pass_rate: Any = None,
    profile: str = "optimized",
    compare: bool = False,
) -> tuple[list[BenchmarkResult], Path]:
    """Run live model benchmarks in disposable fixture copies and save evidence."""
    from apsara_cli.engine.executor import run_agent_stream
    from apsara_cli.engine.tools import agent_runtime_context

    if not 1 <= repeats <= 10:
        raise ValueError("Benchmark repeats must be between 1 and 10")
    if profile not in {"optimized", "reference"}:
        raise ValueError("Benchmark profile must be optimized or reference")
    suite_path = suite_path.resolve()
    suite = json.loads(suite_path.read_text(encoding="utf-8"))
    run_label = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:6]
    run_root = output_root.resolve() / run_label
    run_root.mkdir(parents=True, exist_ok=False)
    evidence_cases: list[dict[str, Any]] = []
    scored: list[BenchmarkResult] = []

    for case in suite.get("cases", []):
        name = str(case["name"])
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name) or name in {".", ".."}:
            raise ValueError(f"Unsafe benchmark case name: {name!r}")
        commands = [_verification_command(item) for item in case.get("verify", [])]
        verify_timeout = int(case.get("verify_timeout") or 120)
        configured_repeats = case.get("verification_repeats")
        if configured_repeats is None:
            configured_repeats = suite.get("verification_repeats", 2)
        if configured_repeats is None:
            configured_repeats = 2
        verification_repeats = int(configured_repeats)
        if not 1 <= verification_repeats <= 5:
            raise ValueError("verification_repeats must be between 1 and 5")
        configured_agent_timeout = case.get("agent_timeout")
        if configured_agent_timeout is None:
            configured_agent_timeout = suite.get("agent_timeout", 300)
        if configured_agent_timeout is None:
            configured_agent_timeout = 300
        agent_timeout = float(configured_agent_timeout)
        if not 0 < agent_timeout <= 3600:
            raise ValueError("agent_timeout must be greater than zero and at most 3600 seconds")

        profiles = ["optimized", "reference"] if compare else [profile]
        schedule = [(trial, selected) for trial in range(1, repeats + 1)
                    for selected in (profiles if trial % 2 else list(reversed(profiles)))]
        for trial, profile in schedule:
            workspace = run_root / profile / name / f"trial-{trial}" if compare else run_root / name / f"trial-{trial}"
            workspace.parent.mkdir(parents=True, exist_ok=True)
            _materialize_benchmark_case(case, suite_path, workspace)
            _apply_case_setup(case, suite_path, workspace)
            baseline_runs = await _run_verification_repeated(
                commands, workspace, verify_timeout, verification_repeats
            )
            baseline_completed = bool(baseline_runs) and all(
                bool(run)
                and all(item.get("status") in {"passed", "failed"} for item in run)
                for run in baseline_runs
            )
            baseline_failed = baseline_completed and all(
                any(item.get("status") == "failed" for item in run)
                for run in baseline_runs
            )
            baseline_flaky = verification_is_flaky(baseline_runs)
            # Baseline commands may create harmless lockfiles or caches. Treat
            # that post-verification repository as the state the agent received.
            before = _hashes(workspace)
            if not baseline_failed or baseline_flaky:
                details = {
                    "name": name,
                    "language": case.get("language"),
                    "trial": trial,
                    "agent_state": "skipped_invalid_baseline",
                    "profile": profile,
                    "usage_complete": False,
                    "unexpected_changes": [],
                    "changed_files": [],
                    "tool_calls": 0,
                    "usage": normalize_usage({}),
                    "verification": [],
                    "verification_runs": [],
                    "baseline_verification": baseline_runs[-1] if baseline_runs else [],
                    "baseline_verification_runs": baseline_runs,
                    "baseline_failed": baseline_failed,
                    "baseline_verification_flaky": baseline_flaky,
                    "final_verification_flaky": False,
                    "verification_flaky": baseline_flaky,
                    "event_count": 0,
                }
                evidence_cases.append(details)
                scored.append(score_benchmark_case(case, details))
                events_path = workspace / ".apsara" / "benchmark-events.json"
                events_path.parent.mkdir(parents=True, exist_ok=True)
                events_path.write_text("[]", encoding="utf-8")
                continue

            _prepare_benchmark_agent_workspace(workspace, commands)

            allowed_commands = {command[0] for command in commands if command}
            allowed_commands.update(
                command[2]
                for command in commands
                if len(command) >= 3 and command[1] == "-m"
            )
            events: list[dict[str, Any]] = []
            aggregate_usage: dict[str, Any] = {}

            started = time.perf_counter()
            async def collect_agent_events() -> None:
                request = [{"role": "user", "content": str(case["instruction"])}]
                stream = (run_agent_stream(request, model=model, profile=profile)
                          if profile != "optimized" else run_agent_stream(request, model=model))
                async for raw_event in stream:
                    event = json.loads(raw_event)
                    events.append(event)
                    if event.get("type") == "usage" and isinstance(event.get("data"), dict):
                        add_usage(aggregate_usage, event["data"])

            with agent_runtime_context(
                workspace_root=workspace,
                enable_bash=bool(commands),
                allowed_commands=allowed_commands,
                confirmation_callback=lambda _action, _payload: True,
                trust_callback=lambda _action, _payload: True,
                model=model,
            ):
                try:
                    await asyncio.wait_for(collect_agent_events(), timeout=agent_timeout)
                except asyncio.TimeoutError:
                    events.append({
                        "type": "error",
                        "message": f"Agent trial timed out after {agent_timeout:g} seconds.",
                    })
            latency = time.perf_counter() - started

            verification_runs = await _run_verification_repeated(
                commands, workspace, verify_timeout, verification_repeats
            )
            after = _hashes(workspace)
            terminal_states = [
                event.get("state")
                for event in events
                if event.get("type") == "run_state"
            ]
            if any(event.get("type") == "error" for event in events):
                agent_state = "failed"
            elif any(event.get("type") == "blocked" for event in events):
                agent_state = "blocked"
            else:
                agent_state = str(terminal_states[-1] if terminal_states else "unknown")
            final_flaky = verification_is_flaky(verification_runs)
            from apsara_cli.engine.runtime import latest_run
            turn_budget = (latest_run(workspace) or {}).get("budget") or {}
            details = {
                "name": name,
                "language": case.get("language"),
                "trial": trial,
                "agent_state": agent_state,
                "changed_files": _changed_files(before, after),
                "tool_calls": sum(
                    1 for event in events if event.get("type") == "tool_call"
                ),
                "usage": normalize_usage(aggregate_usage),
                "verification": verification_runs[-1] if verification_runs else [],
                "verification_runs": verification_runs,
                "baseline_verification": baseline_runs[-1] if baseline_runs else [],
                "baseline_verification_runs": baseline_runs,
                "baseline_failed": baseline_failed,
                "baseline_verification_flaky": baseline_flaky,
                "final_verification_flaky": final_flaky,
                "verification_flaky": baseline_flaky or final_flaky,
                "event_count": len(events),
                "agent_timeout_seconds": agent_timeout,
                "profile": profile,
                "latency_seconds": round(latency, 3),
                "unexpected_changes": [],
                "request_context": [event for event in events if event.get("type") == "request_context"],
                "turn_budget": turn_budget,
                "usage_complete": bool(aggregate_usage.get("total_tokens")) and not any(
                    aggregate_usage.get(key) for key in ("unreported_calls", "interrupted_calls")
                ) and aggregate_usage.get("provider_reported_calls", 0) >= (
                    sum(event.get("type") == "request_context" for event in events)
                    + aggregate_usage.get("auxiliary_calls", 0)
                ) and turn_budget.get("usage_complete", True),
            }
            details["unexpected_changes"] = _unexpected_changes(case, details)
            evidence_cases.append(details)
            scored.append(score_benchmark_case(case, details))
            events_path = workspace / ".apsara" / "benchmark-events.json"
            events_path.parent.mkdir(parents=True, exist_ok=True)
            events_path.write_text(
                json.dumps(events, indent=2, ensure_ascii=False), encoding="utf-8"
            )

    results_path = run_root / "results.json"
    aggregate = aggregate_benchmark_results(
        suite_path, scored, min_pass_rate=min_pass_rate
    )
    evidence = {
        "schema_version": 2,
        "suite": suite.get("name"),
        "suite_digest": hashlib.sha256(suite_path.read_bytes()).hexdigest(),
        "model": model,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "repeat_count": repeats,
        "cases": evidence_cases,
        "aggregate": aggregate,
    }
    results_path.write_text(
        json.dumps(evidence, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (run_root / "summary.json").write_text(
        json.dumps(aggregate, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    if compare:
        paths = {}
        for selected in ("optimized", "reference"):
            selected_results = [result for result in scored if result.details.get("profile") == selected]
            selected_aggregate = aggregate_benchmark_results(suite_path, selected_results, min_pass_rate=min_pass_rate)
            directory = run_root / selected
            directory.mkdir(parents=True, exist_ok=True)
            paths[selected] = directory / "results.json"
            paths[selected].write_text(json.dumps({**evidence, "profile": selected,
                "cases": [result.details for result in selected_results], "aggregate": selected_aggregate,
            }, indent=2, ensure_ascii=False), encoding="utf-8")
            (directory / "summary.json").write_text(json.dumps(selected_aggregate, indent=2), encoding="utf-8")
        comparison = compare_benchmark_results(paths["optimized"], paths["reference"])
        (run_root / "comparison.json").write_text(json.dumps(comparison, indent=2), encoding="utf-8")
        return [result for result in scored if result.details.get("profile") == "optimized"], paths["optimized"]
    return scored, results_path
