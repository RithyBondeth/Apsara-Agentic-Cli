"""Human-readable run reports generated from the durable journal."""

from pathlib import Path
from typing import Any

from apsara_cli.engine.runtime import latest_run


def render_run_report(run: dict[str, Any]) -> str:
    lines = [f"# Apsara run {run.get('run_id', '')}", "", f"- State: **{run.get('state', 'unknown')}**", f"- Model: `{run.get('model', '')}`", f"- Objective: {run.get('objective', '')}", "", "## Plan", ""]
    for step in run.get("steps", []):
        mark = "x" if step.get("status") == "completed" else " "
        lines.append(f"- [{mark}] {step.get('title', '')} — {step.get('status', 'pending')}")
    budget = run.get("budget") or {}
    if budget:
        lines.extend(["", "## Turn budget", "",
                      f"- Model steps: {budget.get('steps_used', 0)}/{budget.get('step_limit', 0)}",
                      f"- Tool calls: {budget.get('tool_calls_used', 0)}/{budget.get('tool_call_limit', 0)}",
                      f"- Provider-reported tokens: {budget.get('reported_usage', 0)}",
                      f"- Reserved estimated usage: {budget.get('estimated_usage', 0)}",
                      f"- Token limit: {budget.get('usage_limit', 0)}",
                      f"- Complete provider usage: {budget.get('usage_complete', False)}",
                      f"- Reused results: {budget.get('reused_checks', 0)}"])
    if run.get("changed_files"):
        lines.extend(["", "## Changed files", "", *[f"- `{p}`" for p in run["changed_files"]]])
    if run.get("verification"):
        lines.extend(["", "## Verification", "", *[f"- `{v}`" for v in run["verification"]]])
    if run.get("error"):
        lines.extend(["", "## Error", "", str(run["error"])])
    lines.extend(["", "## Completion evidence", "",
                  f"- Verification: {run.get('verification_status', 'unknown')}",
                  f"- Critic: {run.get('critic_status', 'unknown')}",
                  f"- Outcome: {run.get('completion_reason') or 'No completion reason recorded.'}"])
    for evidence in run.get("verification_evidence", []):
        for result in evidence.get("results", []):
            lines.append(f"- `{result.get('command', [])}` — {result.get('status')}, exit {result.get('returncode')}")
    for finding in run.get("critic_findings", []):
        lines.append(f"- Finding: {finding.get('path', '')} — {finding.get('description', '')}")
    return "\n".join(lines) + "\n"


def export_latest_report(workspace: Path, destination: Path | None = None) -> Path:
    run = latest_run(workspace)
    if run is None:
        raise FileNotFoundError("No run journal is available")
    destination = destination or workspace / ".apsara" / "reports" / f"{run['run_id']}.md"
    if not destination.is_absolute():
        destination = workspace / destination
    destination.resolve().relative_to(workspace.resolve())
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render_run_report(run), encoding="utf-8")
    return destination
