"""Manifest-aware, structured project verification."""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib

from apsara_cli.engine.isolation import isolated_workspace


@dataclass(frozen=True)
class VerificationCommand:
    name: str
    command: tuple[str, ...]
    kind: str = "test"


@dataclass
class VerificationResult:
    name: str
    command: list[str]
    status: str
    returncode: int | None
    duration_seconds: float
    output: str = ""


@dataclass
class VerificationReport:
    phase: str
    status: str
    isolated: bool
    workspace: str
    results: list[VerificationResult]

    @property
    def passed(self) -> bool:
        return self.status == "passed"

    def as_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "status": self.status,
            "isolated": self.isolated,
            "workspace": self.workspace,
            "results": [asdict(item) for item in self.results],
        }


def _command(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        parts = shlex.split(value)
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        parts = list(value)
    else:
        raise ValueError("verification commands must be strings or string arrays")
    if not parts:
        raise ValueError("verification commands cannot be empty")
    return tuple(parts)


def _configured_commands(workspace: Path, phase: str) -> list[VerificationCommand]:
    path = workspace / ".apsara" / "config.toml"
    if not path.exists():
        return []
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
        section = payload.get("verification", {})
        if not isinstance(section, dict):
            return []
        key = "targeted_commands" if phase == "targeted" else "commands"
        values = section.get(key)
        if values is None and phase == "targeted":
            values = section.get("commands")
        if values is None:
            return []
        if not isinstance(values, list):
            values = [values]
        return [
            VerificationCommand(f"configured-{index}", _command(value), "configured")
            for index, value in enumerate(values, 1)
        ]
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"Invalid verification configuration in {path}: {exc}") from exc


def _pytest_command(workspace: Path) -> tuple[str, ...] | None:
    candidates = [
        workspace / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python"),
        workspace / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python"),
    ]
    project_python = next((path for path in candidates if path.is_file()), None)
    if project_python is not None:
        return (str(project_python), "-m", "pytest", "-q")
    pytest_executable = shutil.which("pytest")
    if pytest_executable:
        return (pytest_executable, "-q")
    if importlib.util.find_spec("pytest") is not None:
        return (sys.executable, "-m", "pytest", "-q")
    return None


def detect_verification_commands(
    workspace: Path, phase: str = "full"
) -> list[VerificationCommand]:
    """Detect deterministic checks from explicit config and common manifests."""
    workspace = workspace.resolve()
    configured = _configured_commands(workspace, phase)
    if configured:
        return configured

    commands: list[VerificationCommand] = []
    pyproject = workspace / "pyproject.toml"
    setup_cfg = workspace / "setup.cfg"
    try:
        pyproject_uses_pytest = pyproject.exists() and "[tool.pytest" in pyproject.read_text(
            encoding="utf-8"
        )
    except OSError:
        pyproject_uses_pytest = False
    try:
        setup_uses_pytest = setup_cfg.exists() and "[tool:pytest]" in setup_cfg.read_text(
            encoding="utf-8"
        )
    except OSError:
        setup_uses_pytest = False
    pytest_project = (
        (workspace / "pytest.ini").exists()
        or (workspace / "tests").is_dir()
        or pyproject_uses_pytest
        or setup_uses_pytest
    )
    if pytest_project:
        pytest_command = _pytest_command(workspace)
        if pytest_command is not None:
            commands.append(VerificationCommand("pytest", pytest_command))

    package_json = workspace / "package.json"
    if package_json.exists():
        try:
            scripts = json.loads(package_json.read_text(encoding="utf-8")).get("scripts", {})
        except (OSError, json.JSONDecodeError, AttributeError):
            scripts = {}
        if isinstance(scripts, dict) and shutil.which("npm"):
            names = ["test"] if phase == "targeted" else ["test", "typecheck", "lint", "build"]
            for name in names:
                if name in scripts:
                    commands.append(VerificationCommand(f"npm-{name}", ("npm", "run", name), name))

    if (workspace / "go.mod").exists() and shutil.which("go"):
        commands.append(VerificationCommand("go-test", ("go", "test", "./...")))
    if (workspace / "Cargo.toml").exists() and shutil.which("cargo"):
        commands.append(VerificationCommand("cargo-test", ("cargo", "test", "--quiet")))

    deduplicated: list[VerificationCommand] = []
    seen: set[tuple[str, ...]] = set()
    for item in commands:
        if item.command not in seen:
            seen.add(item.command)
            deduplicated.append(item)
    return deduplicated


def verification_digest_source(commands: list[VerificationCommand]) -> str:
    return json.dumps(
        [{"name": item.name, "command": list(item.command), "kind": item.kind} for item in commands],
        sort_keys=True,
    )


def _run_commands(
    commands: list[VerificationCommand], workspace: Path, timeout: int
) -> list[VerificationResult]:
    results: list[VerificationResult] = []
    env = dict(os.environ)
    source_dir = workspace / "src"
    if source_dir.is_dir():
        env["PYTHONPATH"] = str(source_dir) + os.pathsep + env.get("PYTHONPATH", "")
    for item in commands:
        started = time.monotonic()
        executable = item.command[0]
        if not (Path(executable).is_file() or shutil.which(executable)):
            results.append(VerificationResult(
                item.name, list(item.command), "unavailable", None, 0.0,
                f"Executable not found: {executable}",
            ))
            continue
        try:
            from apsara_cli.engine.cancellation import run_command
            completed = run_command(list(item.command), cwd=workspace, env=env, timeout=max(1, timeout))
            output = (completed.stdout + "\n" + completed.stderr).strip()[-8000:]
            results.append(VerificationResult(
                item.name,
                list(item.command),
                "passed" if completed.returncode == 0 else "failed",
                completed.returncode,
                round(time.monotonic() - started, 3),
                output,
            ))
        except subprocess.TimeoutExpired as exc:
            results.append(VerificationResult(
                item.name, list(item.command), "timeout", None,
                round(time.monotonic() - started, 3), str(exc),
            ))
        except OSError as exc:
            results.append(VerificationResult(
                item.name, list(item.command), "failed", None,
                round(time.monotonic() - started, 3), str(exc),
            ))
    return results


def run_verification(
    workspace: Path,
    *,
    phase: str = "full",
    isolated: bool = False,
    timeout: int = 120,
) -> VerificationReport:
    if phase not in {"baseline", "targeted", "full"}:
        raise ValueError("verification phase must be baseline, targeted, or full")
    timeout = max(1, min(int(timeout), 900))
    commands = detect_verification_commands(workspace, "targeted" if phase == "targeted" else "full")
    if not commands:
        return VerificationReport(phase, "unavailable", isolated, str(workspace), [])
    if isolated:
        with isolated_workspace(workspace) as snapshot:
            results = _run_commands(commands, snapshot, timeout)
    else:
        results = _run_commands(commands, workspace, timeout)
    status = "passed" if results and all(item.status == "passed" for item in results) else "failed"
    if results and all(item.status == "unavailable" for item in results):
        status = "unavailable"
    return VerificationReport(phase, status, isolated, str(workspace), results)


def format_verification_report(report: VerificationReport) -> str:
    summary = json.dumps(report.as_dict(), indent=2)
    if report.passed:
        return f"Verification passed ({report.phase}).\n{summary}"
    if report.status == "unavailable":
        return f"Error: Verification unavailable.\n{summary}"
    return f"Error: Verification failed ({report.phase}).\n{summary}"
