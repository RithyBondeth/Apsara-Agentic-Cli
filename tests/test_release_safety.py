import asyncio
import json
import re
import subprocess
import threading
from datetime import date
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib

from apsara_cli.cli.session import save_session_messages
from apsara_cli.cli.tui import TurnController
from apsara_cli.cli import parser as cli_parser
from apsara_cli.engine.executor import _fallback_allowed
from apsara_cli.engine.models import lookup_model, model_availability, model_lifecycle
from apsara_cli.engine.usage_reports import format_usage_report, workspace_usage
from apsara_cli.engine.workspace_diff import workspace_diff


ROOT = Path(__file__).resolve().parents[1]


def test_cli_output_uses_replacement_for_legacy_encodings(monkeypatch):
    class LegacyStream:
        errors = "strict"

        def reconfigure(self, **options):
            self.errors = options["errors"]

    stdout = LegacyStream()
    stderr = LegacyStream()
    monkeypatch.setattr(cli_parser.sys, "stdout", stdout)
    monkeypatch.setattr(cli_parser.sys, "stderr", stderr)

    cli_parser._configure_output_streams()

    assert stdout.errors == "replace"
    assert stderr.errors == "replace"


def test_retired_models_are_blocked_and_retiring_models_warn():
    retired = lookup_model("r1-groq")
    retiring = lookup_model("llama70b")
    assert retired is not None and retiring is not None
    assert model_lifecycle(retired, date(2026, 8, 9)) == "retired"
    assert model_availability(retired, date(2026, 8, 9))[0] is False
    allowed, message = model_availability(retiring, date(2026, 8, 9))
    assert allowed is True
    assert "retires on 2026-08-16" in message
    assert model_lifecycle(retiring, date(2026, 8, 16)) == "retired"


def test_retired_model_cannot_be_an_automatic_fallback():
    assert _fallback_allowed("opencode/big-pickle", "groq/deepseek-r1-distill-llama-70b") is False


def test_turn_controller_cancels_worker_coroutine():
    controller = TurnController()
    started = threading.Event()
    result = {}

    async def work():
        started.set()
        await asyncio.sleep(30)

    def runner():
        try:
            controller.run(work())
        except asyncio.CancelledError:
            result["cancelled"] = True

    thread = threading.Thread(target=runner)
    thread.start()
    assert started.wait(timeout=2)
    assert controller.cancel() is True
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert result == {"cancelled": True}


def test_workspace_diff_reports_git_changes(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "tracked.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "initial"],
        check=True,
    )
    tracked.write_text("after\n", encoding="utf-8")
    (tmp_path / "new.txt").write_text("new\n", encoding="utf-8")

    report = workspace_diff(tmp_path)
    assert "STATUS" in report
    assert "tracked.txt" in report
    assert "new.txt" in report
    assert "UNSTAGED" in report
    assert "-before" in report and "+after" in report


def test_workspace_usage_aggregates_saved_sessions_locally(tmp_path):
    save_session_messages(
        tmp_path,
        "one",
        "opencode/big-pickle",
        [],
        usage={
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "model_usage": {
                "opencode/big-pickle": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
            },
        },
    )
    report = workspace_usage(tmp_path)
    assert report["total_tokens"] == 15
    assert report["models"]["opencode/big-pickle"]["total_tokens"] == 15
    rendered = format_usage_report(tmp_path, {"prompt_tokens": 2, "completion_tokens": 3})
    assert "Current session  5 tokens" in rendered
    assert "Stored locally" in rendered
    assert "not a billing ledger" in rendered
    assert "provider dashboard remains authoritative" in rendered


def test_alpha_distribution_uses_pep440_prerelease_version():
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = metadata["project"]["version"]

    assert re.fullmatch(r"\d+\.\d+\.\d+a\d+", version)
    assert f"Version: `{version}`" in (ROOT / "RELEASE_NOTES_ALPHA.md").read_text(encoding="utf-8")
    assert (
        f"apsara_agentic-{version}-py3-none-any.whl"
        in (ROOT / "README.md").read_text(encoding="utf-8")
    )
    assert (
        f"apsara_agentic-{version}-py3-none-any.whl"
        in (ROOT / "TESTER_QUICKSTART.md").read_text(encoding="utf-8")
    )


def test_public_install_docs_do_not_reference_removed_entrypoints():
    public_docs = "\n".join(
        (ROOT / filename).read_text(encoding="utf-8")
        for filename in ("README.md", "TESTER_QUICKSTART.md", "ALPHA_TESTING.md")
    )

    for stale_instruction in (
        "npm install -g apsara-cli",
        "python3 -m app.cli",
        "Python 3.9",
        "your-repo",
    ):
        assert stale_instruction not in public_docs


def test_pypi_readme_does_not_use_relative_markdown_links():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    relative_links = re.findall(r"\[[^]]+\]\(((?!https?://|#)[^)]+)\)", readme)

    assert relative_links == []


def test_supported_python_versions_include_current_feature_release():
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    classifiers = metadata["project"]["classifiers"]
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "Programming Language :: Python :: 3.14" in classifiers
    assert 'python-version: ["3.10", "3.11", "3.12", "3.13", "3.14"]' in workflow
    assert 'python-version: ["3.10", "3.14"]' in workflow


def test_core_benchmark_has_repeatable_release_gates_and_multifile_case():
    suite = json.loads((ROOT / "evals" / "coding-core.json").read_text(encoding="utf-8"))
    thresholds = suite["thresholds"]
    cases = {case["name"]: case for case in suite["cases"]}

    assert suite["verification_repeats"] >= 2
    assert thresholds["min_pass_rate"] == 0.8
    assert thresholds["max_verification_flaky_trials"] == 0
    assert thresholds["max_unstable_cases"] == 0
    assert thresholds["max_unsafe_trials"] == 0
    assert cases["python-layered-settings"]["allowed_changes"] == ["settings/*.py"]
