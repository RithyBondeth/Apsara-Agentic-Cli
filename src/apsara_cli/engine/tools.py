import difflib
import json
import importlib.util
import glob as _glob
import os
import shlex
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from pathlib import Path, PurePath
from typing import Any, Callable, Dict, Iterator, Optional, Set

from apsara_cli.config import trust
from apsara_cli.config.defaults import settings
from apsara_cli.shared.types import ToolRisk


class ToolSecurityError(Exception):
    """Raised when a tool request violates the configured tool policy."""


ConfirmationCallback = Callable[[str, Dict[str, Any]], bool]
# Asked before executing code that came from the workspace. Deliberately
# separate from ConfirmationCallback: --auto-approve means "don't ask before
# writing files", which is much weaker consent than "run this repo's code".
TrustCallback = Callable[[str, Dict[str, Any]], bool]

_workspace_root_override: ContextVar[Optional[Path]] = ContextVar(
    "workspace_root_override", default=None
)
_enable_bash_override: ContextVar[Optional[bool]] = ContextVar(
    "enable_bash_override", default=None
)
_allowed_commands_override: ContextVar[Optional[Set[str]]] = ContextVar(
    "allowed_commands_override", default=None
)
_max_file_size_override: ContextVar[Optional[int]] = ContextVar(
    "max_file_size_override", default=None
)
_bash_timeout_override: ContextVar[Optional[int]] = ContextVar(
    "bash_timeout_override", default=None
)
_confirmation_callback_override: ContextVar[Optional[ConfirmationCallback]] = ContextVar(
    "confirmation_callback_override", default=None
)
_dry_run_override: ContextVar[Optional[bool]] = ContextVar(
    "dry_run_override", default=None
)
_read_only_override: ContextVar[Optional[bool]] = ContextVar(
    "read_only_override", default=None
)
_model_override: ContextVar[Optional[str]] = ContextVar(
    "model_override", default=None
)
_auxiliary_usage_override: ContextVar[Optional[list[dict[str, Any]]]] = ContextVar(
    "auxiliary_usage_override", default=None
)
_trust_callback_override: ContextVar[Optional[TrustCallback]] = ContextVar(
    "trust_callback_override", default=None
)
# Set once per session (not per turn) so MCP servers are spawned once and stay
# connected. ContextVars propagate down the call stack, so the per-turn runtime
# context does not need to thread it through.
_mcp_manager_override: ContextVar[Optional[Any]] = ContextVar(
    "mcp_manager_override", default=None
)


@contextmanager
def mcp_manager_context(manager: Optional[Any]) -> Iterator[None]:
    """Make an McpManager visible to tool discovery and dispatch."""
    if manager is None:
        yield
        return
    token = _mcp_manager_override.set(manager)
    try:
        yield
    finally:
        _mcp_manager_override.reset(token)


def get_mcp_manager() -> Optional[Any]:
    return _mcp_manager_override.get()
MAX_CONFIRMATION_FILE_BYTES = 200_000
MAX_CONFIRMATION_DIFF_PREVIEW_LINES = 80
MAX_CONFIRMATION_DIFF_FULL_LINES = 240


@contextmanager
def agent_runtime_context(
    workspace_root: Optional[Path] = None,
    enable_bash: Optional[bool] = None,
    allowed_commands: Optional[Set[str]] = None,
    max_file_size_bytes: Optional[int] = None,
    confirmation_callback: Optional[ConfirmationCallback] = None,
    dry_run: Optional[bool] = None,
    read_only: Optional[bool] = None,
    trust_callback: Optional[TrustCallback] = None,
    bash_timeout_seconds: Optional[int] = None,
    model: Optional[str] = None,
) -> Iterator[None]:
    workspace_token = None
    bash_token = None
    commands_token = None
    file_size_token = None
    confirmation_token = None
    dry_run_token = None
    read_only_token = None
    trust_token = None
    bash_timeout_token = None
    model_token = None
    auxiliary_usage_token = _auxiliary_usage_override.set([])

    try:
        if workspace_root is not None:
            workspace_token = _workspace_root_override.set(workspace_root.resolve())
        if enable_bash is not None:
            bash_token = _enable_bash_override.set(enable_bash)
        if allowed_commands is not None:
            commands_token = _allowed_commands_override.set(set(allowed_commands))
        if max_file_size_bytes is not None:
            file_size_token = _max_file_size_override.set(max_file_size_bytes)
        if confirmation_callback is not None:
            confirmation_token = _confirmation_callback_override.set(
                confirmation_callback
            )
        if dry_run is not None:
            dry_run_token = _dry_run_override.set(dry_run)
        if read_only is not None:
            read_only_token = _read_only_override.set(read_only)
        if trust_callback is not None:
            trust_token = _trust_callback_override.set(trust_callback)
        if bash_timeout_seconds is not None:
            bash_timeout_token = _bash_timeout_override.set(bash_timeout_seconds)
        if model is not None:
            model_token = _model_override.set(model)
        yield
    finally:
        _auxiliary_usage_override.reset(auxiliary_usage_token)
        if model_token is not None:
            _model_override.reset(model_token)
        if bash_timeout_token is not None:
            _bash_timeout_override.reset(bash_timeout_token)
        if trust_token is not None:
            _trust_callback_override.reset(trust_token)
        if read_only_token is not None:
            _read_only_override.reset(read_only_token)
        if dry_run_token is not None:
            _dry_run_override.reset(dry_run_token)
        if confirmation_token is not None:
            _confirmation_callback_override.reset(confirmation_token)
        if file_size_token is not None:
            _max_file_size_override.reset(file_size_token)
        if commands_token is not None:
            _allowed_commands_override.reset(commands_token)
        if bash_token is not None:
            _enable_bash_override.reset(bash_token)
        if workspace_token is not None:
            _workspace_root_override.reset(workspace_token)


def _workspace_root() -> Path:
    overridden_root = _workspace_root_override.get()
    if overridden_root is not None:
        return overridden_root
    return settings.agent_workspace_root_path


def _bash_enabled() -> bool:
    overridden_value = _enable_bash_override.get()
    if overridden_value is not None:
        return overridden_value
    return settings.AGENT_ENABLE_BASH_TOOL


def _allowed_commands() -> Set[str]:
    overridden_commands = _allowed_commands_override.get()
    if overridden_commands is not None:
        return overridden_commands
    return settings.agent_allowed_commands


def _dry_run() -> bool:
    overridden_dry = _dry_run_override.get()
    if overridden_dry is not None:
        return overridden_dry
    return False


def _read_only() -> bool:
    overridden_ro = _read_only_override.get()
    if overridden_ro is not None:
        return overridden_ro
    return False


def _current_model() -> str:
    from apsara_cli.engine.models import DEFAULT_MODEL
    return _model_override.get() or DEFAULT_MODEL


def consume_auxiliary_usage() -> list[dict[str, Any]]:
    usage = list(_auxiliary_usage_override.get() or [])
    _auxiliary_usage_override.set([])
    return usage


def _record_auxiliary_usage(data: dict[str, Any]) -> None:
    pending = list(_auxiliary_usage_override.get() or [])
    pending.append(data)
    _auxiliary_usage_override.set(pending)


def classify_tool_risk(tool_name: str) -> ToolRisk:
    """Stable permission category used by journals, policies, and future UIs."""
    if tool_name.startswith("mcp__"):
        return ToolRisk.EXTERNAL
    if tool_name in {"delete_file"}:
        return ToolRisk.DESTRUCTIVE
    if tool_name in {"write_to_file", "edit_file", "replace_file_lines", "replace_symbol", "move_file", "create_directory", "undo_last_checkpoint", "undo_turn_checkpoint", "remember_project_note"}:
        return ToolRisk.WRITE
    if tool_name in {"run_bash_command", "start_process", "stop_process", "verify_project"}:
        return ToolRisk.EXECUTE
    return ToolRisk.READ


def request_workspace_trust(
    key: str,
    digest: str,
    payload: Dict[str, Any],
) -> bool:
    """Gate execution of workspace-supplied code behind a recorded approval.

    Returns True only if this exact content was approved before, or the user
    approves it now. With no trust callback installed there is no one to ask,
    so the answer is no — silently running a cloned repo's code is the exact
    failure this guards against.
    """
    workspace = _workspace_root()
    if trust.is_trusted(workspace, key, digest):
        return True

    callback = _trust_callback_override.get()
    if callback is None:
        return False

    if not callback("trust_workspace_code", {**payload, "key": key, "digest": digest}):
        return False

    trust.record_trust(workspace, key, digest)
    return True


# Loading a plugin executes it, and the registry is rebuilt on every LLM request
# and every tool call — so cache by content and only re-exec when it changes.
_plugin_cache: dict[str, tuple[str, list]] = {}


def _load_local_plugins() -> list[dict[str, Any]]:
    """
    Load custom tools from .apsara/tools/*.py in the workspace.
    Returns a list of tuples (metadata_dict, execution_callable).

    Each file must be approved by the user before it is executed; see
    request_workspace_trust.
    """
    plugins: list = []
    workspace = _workspace_root()
    tools_dir = workspace / ".apsara" / "tools"
    if not tools_dir.is_dir():
        return plugins

    candidates = sorted(
        py_file
        for py_file in tools_dir.glob("*.py")
        if not py_file.name.startswith("__")
    )
    if not candidates:
        return plugins

    sources: list[tuple[Path, str, str, dict[str, Any]]] = []
    for py_file in candidates:
        try:
            source = py_file.read_text(encoding="utf-8")
        except Exception as exc:
            print(f"Warning: Failed to read local plugin {py_file.name}: {exc}")
            continue
        manifest: dict[str, Any] = {}
        manifest_path = py_file.with_suffix(".json")
        if manifest_path.exists():
            try:
                import json
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if not isinstance(manifest, dict):
                    raise ValueError("manifest must be a JSON object")
                permissions = manifest.get("permissions", [])
                if not isinstance(permissions, list) or not all(isinstance(p, str) for p in permissions):
                    raise ValueError("permissions must be a list of strings")
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                print(f"Warning: Invalid plugin manifest {manifest_path.name}: {exc}")
                continue
        if manifest.get("enabled") is False:
            continue
        digest_source = source + "\n" + repr(sorted(manifest.items()))
        sources.append((py_file, source, trust.digest_text(digest_source), manifest))

    cache_key = str(workspace)
    combined_digest = trust.digest_text(
        "".join(f"{py_file.name}:{digest}" for py_file, _, digest, _ in sources)
    )
    cached = _plugin_cache.get(cache_key)
    if cached is not None and cached[0] == combined_digest:
        return list(cached[1])

    for py_file, source, digest, manifest in sources:
        try:
            relative = py_file.relative_to(workspace)
        except ValueError:
            relative = py_file

        if not request_workspace_trust(
            f"plugin:{relative}",
            digest,
            {
                "kind": "plugin",
                "display_path": str(relative),
                "path": str(py_file),
                "source_preview": source[:1200],
                "line_count": source.count("\n") + 1,
            },
        ):
            print(
                f"Skipped untrusted local plugin {py_file.name}. "
                "Run `apsara` in this project to review and approve it."
            )
            continue

        try:
            module_name = f"apsara_plugin_{py_file.stem}"
            spec = importlib.util.spec_from_file_location(module_name, py_file)
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)

                metadata = getattr(module, "METADATA", None)
                run_func = getattr(module, "run", None)

                if metadata and callable(run_func):
                    metadata = dict(metadata)
                    # Wrap in tool definition format if only function part provided
                    if "type" in metadata:
                        tool_def = dict(metadata)
                        function = dict(tool_def.get("function", {}))
                        tool_def["function"] = function
                    else:
                        function = metadata
                        tool_def = {
                            "type": "function",
                            "function": function,
                        }
                    function.setdefault("name", manifest.get("name", py_file.stem))
                    if manifest.get("description"):
                        function["description"] = manifest["description"]

                    plugins.append((tool_def, run_func))
        except Exception as exc:
            # We don't want a single bad plugin to crash the whole agent
            print(f"Warning: Failed to load local plugin {py_file.name}: {exc}")

    _plugin_cache[cache_key] = (combined_digest, list(plugins))
    return plugins


def _bash_timeout() -> int:
    overridden = _bash_timeout_override.get()
    if overridden is not None:
        return max(1, int(overridden))
    return max(1, int(settings.AGENT_BASH_TIMEOUT_SECONDS))


def _max_file_size_bytes() -> int:
    overridden_max = _max_file_size_override.get()
    if overridden_max is not None:
        return overridden_max
    return settings.AGENT_MAX_FILE_SIZE_BYTES


def _resolve_path(path: str, *, must_exist: bool = False) -> Path:
    requested_path = Path(path).expanduser()
    candidate = requested_path
    if not requested_path.is_absolute():
        candidate = _workspace_root() / requested_path

    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(_workspace_root())
    except ValueError as exc:
        raise ToolSecurityError(
            f"Path '{path}' is outside the configured workspace root."
        ) from exc

    if must_exist and not resolved.exists():
        raise FileNotFoundError(f"Path '{path}' does not exist.")

    return resolved


def _format_exception(prefix: str, exc: Exception) -> str:
    return f"{prefix}: {str(exc)}"


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(_workspace_root()))
    except ValueError:
        return str(path)


def _syntax_diagnostic_suffix(path: Path) -> str:
    """Give the agent immediate parser feedback after a source-file edit."""
    try:
        from apsara_cli.engine.intelligence import LANGUAGES, format_diagnostics, syntax_diagnostics
        if path.suffix.lower() not in LANGUAGES:
            return ""
        issues = syntax_diagnostics(path)
    except (OSError, UnicodeError):
        return ""
    if not issues:
        return "\nSyntax diagnostics: clean."
    return "\nSyntax diagnostics:\n" + format_diagnostics(_workspace_root(), issues)


def _read_confirmation_text(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ""
    if path.stat().st_size > MAX_CONFIRMATION_FILE_BYTES:
        return ""
    with path.open("r", encoding="utf-8") as file_handle:
        return file_handle.read()


def _build_text_diff(
    before_text: str,
    after_text: str,
    display_path: str,
) -> tuple[str, str, str, bool]:
    diff_lines = list(
        difflib.unified_diff(
            before_text.splitlines(),
            after_text.splitlines(),
            fromfile=f"a/{display_path}",
            tofile=f"b/{display_path}",
            lineterm="",
        )
    )
    if not diff_lines:
        diff_lines = [f"No textual changes for {display_path}."]
    editor_diff = "\n".join(diff_lines)

    preview_lines = diff_lines[:MAX_CONFIRMATION_DIFF_PREVIEW_LINES]
    preview_truncated = len(diff_lines) > MAX_CONFIRMATION_DIFF_PREVIEW_LINES
    if preview_truncated:
        preview_lines.append("... [diff preview truncated]")

    full_lines = diff_lines[:MAX_CONFIRMATION_DIFF_FULL_LINES]
    full_truncated = len(diff_lines) > MAX_CONFIRMATION_DIFF_FULL_LINES
    if full_truncated:
        full_lines.append("... [full diff truncated]")

    return (
        "\n".join(preview_lines),
        "\n".join(full_lines),
        editor_diff,
        full_truncated,
    )


def _confirm_action(action: str, payload: Dict[str, Any]) -> bool:
    callback = _confirmation_callback_override.get()
    if callback is None:
        return True
    return callback(action, payload)


def _checkpoint(paths: list[Path], label: str) -> Optional[str]:
    if _dry_run():
        return None
    try:
        from apsara_cli.engine.checkpoints import create_checkpoint
        from apsara_cli.engine.turn_checkpoints import capture_turn_paths

        workspace = _workspace_root().resolve()
        turn_paths = list(paths)
        for path in paths:
            resolved = path.resolve()
            try:
                resolved.relative_to(workspace)
            except ValueError:
                continue
            parent = resolved.parent
            while parent != workspace and not parent.exists():
                turn_paths.append(parent)
                parent = parent.parent
        capture_turn_paths(workspace, turn_paths)
        return create_checkpoint(_workspace_root(), paths, label)
    except Exception:
        return None


def read_file(path: str) -> str:
    try:
        resolved_path = _resolve_path(path, must_exist=True)
        if not resolved_path.is_file():
            return f"Error reading file: '{path}' is not a file."

        file_size = resolved_path.stat().st_size
        if file_size > _max_file_size_bytes():
            return (
                "Error reading file: "
                f"'{path}' exceeds {_max_file_size_bytes()} bytes."
            )

        with resolved_path.open("r", encoding="utf-8") as file_handle:
            return file_handle.read()
    except Exception as exc:
        return _format_exception("Error reading file", exc)


def parallel_read_files(paths: list[str]) -> str:
    """Read up to eight independent files concurrently, preserving input order."""
    selected = paths[:8]
    if not selected:
        return "Error: At least one path is required."
    with ThreadPoolExecutor(max_workers=min(4, len(selected))) as pool:
        # ContextVars do not cross thread boundaries automatically. Give every
        # worker its own context copy so workspace and file-size restrictions
        # remain identical to a normal, single-file read.
        futures = [
            pool.submit(copy_context().run, read_file, path)
            for path in selected
        ]
        contents = [future.result() for future in futures]
    return "\n\n".join(f"===== {path} =====\n{content}" for path, content in zip(selected, contents))


def read_file_lines(path: str, start_line: int, end_line: int) -> str:
    """Read a specific line range from a file (1-indexed, inclusive).
    Returns lines prefixed with their line number so the model can use
    replace_file_lines accurately."""
    try:
        resolved_path = _resolve_path(path, must_exist=True)
        if not resolved_path.is_file():
            return f"Error: '{path}' is not a file."

        if start_line < 1:
            return "Error: start_line must be >= 1."
        if end_line < start_line:
            return "Error: end_line must be >= start_line."

        with resolved_path.open("r", encoding="utf-8") as fh:
            all_lines = fh.readlines()

        total = len(all_lines)
        if start_line > total:
            return f"Error: start_line {start_line} exceeds file length ({total} lines)."

        actual_end = min(end_line, total)
        selected = all_lines[start_line - 1 : actual_end]

        numbered = "".join(
            f"{start_line + i:4d}: {line}" for i, line in enumerate(selected)
        )
        header = f"# {_display_path(resolved_path)}  (lines {start_line}–{actual_end} of {total})\n"
        return header + numbered
    except Exception as exc:
        return _format_exception("Error reading file lines", exc)


def create_directory(path: str) -> str:
    """Create a directory (and any missing parents) inside the workspace."""
    try:
        if _read_only():
            return "Error: Destructive operations are disabled in read-only mode."

        resolved_path = _resolve_path(path)
        if resolved_path.exists() and resolved_path.is_dir():
            return f"Directory already exists: {_display_path(resolved_path)}"

        display = _display_path(resolved_path)
        if not _confirm_action(
            "create_directory",
            {
                "path": str(resolved_path),
                "display_path": display,
            },
        ):
            return f"Error creating directory: creation of '{display}' was not approved."

        if _dry_run():
            return f"[Dry Run] Successfully created directory: {display} (simulated)"

        _checkpoint([resolved_path], f"Before creating directory {display}")
        resolved_path.mkdir(parents=True, exist_ok=True)
        return f"Created directory: {display}"
    except Exception as exc:
        return _format_exception("Error creating directory", exc)


def delete_file(path: str) -> str:
    """Delete a file inside the workspace (requires confirmation)."""
    try:
        if _read_only():
            return "Error: Destructive operations are disabled in read-only mode."

        resolved_path = _resolve_path(path, must_exist=True)
        if not resolved_path.is_file():
            return f"Error: '{path}' is not a file. Use the bash tool to remove directories."

        display = _display_path(resolved_path)
        preview = _read_confirmation_text(resolved_path)

        if not _confirm_action(
            "delete_file",
            {
                "path": str(resolved_path),
                "display_path": display,
                "content_preview": preview[:800] if preview else "(binary or unreadable)",
            },
        ):
            return f"Error: deletion of '{display}' was not approved."

        if _dry_run():
            return f"[Dry Run] Successfully deleted {display} (simulated)"

        _checkpoint([resolved_path], f"Before deleting {display}")
        resolved_path.unlink()
        return f"Deleted: {display}"
    except Exception as exc:
        return _format_exception("Error deleting file", exc)


def move_file(src: str, dest: str) -> str:
    """Move or rename a file within the workspace (requires confirmation)."""
    try:
        if _read_only():
            return "Error: Destructive operations are disabled in read-only mode."

        resolved_src = _resolve_path(src, must_exist=True)
        resolved_dest = _resolve_path(dest)

        if not resolved_src.is_file():
            return f"Error: '{src}' is not a file."
        if resolved_dest.is_dir():
            resolved_dest = resolved_dest / resolved_src.name

        display_src = _display_path(resolved_src)
        display_dest = _display_path(resolved_dest)

        if not _confirm_action(
            "move_file",
            {
                "path": str(resolved_src),
                "display_path": display_src,
                "dest_path": str(resolved_dest),
                "display_dest": display_dest,
                "overwrites": resolved_dest.exists(),
            },
        ):
            return f"Error: move '{display_src}' → '{display_dest}' was not approved."

        if _dry_run():
            return f"[Dry Run] Successfully moved {display_src} → {display_dest} (simulated)"

        _checkpoint([resolved_src, resolved_dest], f"Before moving {display_src} to {display_dest}")
        resolved_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(resolved_src), str(resolved_dest))
        return f"Moved: {display_src} → {display_dest}"
    except Exception as exc:
        return _format_exception("Error moving file", exc)


def glob_search(pattern: str, root_dir: str = ".") -> str:
    """Find files matching a glob pattern (e.g. **/*.py, src/**/*.ts).
    Returns paths relative to the workspace root, capped at 200 results."""
    try:
        resolved_root = _resolve_path(root_dir, must_exist=True)
        if not resolved_root.is_dir():
            return f"Error: '{root_dir}' is not a directory."

        matches = _glob.glob(
            str(resolved_root / pattern), recursive=True
        )
        # Filter to files only and enforce workspace boundary
        results: list[str] = []
        for m in sorted(matches):
            mp = Path(m)
            try:
                mp.relative_to(_workspace_root())
            except ValueError:
                continue
            if mp.is_file() or mp.is_dir():
                results.append(_display_path(mp))
            if len(results) >= 200:
                break

        if not results:
            return f"No matches for pattern '{pattern}' in '{root_dir}'."

        suffix = f"\n... (capped at 200)" if len(results) == 200 else ""
        return "\n".join(results) + suffix
    except Exception as exc:
        return _format_exception("Error in glob search", exc)


def write_to_file(path: str, content: str) -> str:
    try:
        if _read_only():
            return "Error: Destructive operations are disabled in read-only mode."

        resolved_path = _resolve_path(path)
        existing_content = _read_confirmation_text(resolved_path)
        display_path = _display_path(resolved_path)
        diff_preview, diff_full, diff_editor, diff_truncated = _build_text_diff(
            existing_content,
            content,
            display_path,
        )
        if not _confirm_action(
            "write_to_file",
            {
                "path": str(resolved_path),
                "display_path": display_path,
                "content_preview": content[:800],
                "existing_preview": existing_content[:800],
                "diff_preview": diff_preview,
                "diff_full": diff_full,
                "diff_editor": diff_editor,
                "diff_truncated": diff_truncated,
                "is_new_file": not resolved_path.exists(),
            },
        ):
            return f"Error writing file: write to '{resolved_path}' was not approved."
        
        if _dry_run():
            return f"[Dry Run] Successfully wrote to {resolved_path} (simulated)"

        _checkpoint([resolved_path], f"Before writing {display_path}")
        resolved_path.parent.mkdir(parents=True, exist_ok=True)
        with resolved_path.open("w", encoding="utf-8") as file_handle:
            file_handle.write(content)
        return f"Successfully wrote to {resolved_path}" + _syntax_diagnostic_suffix(resolved_path)
    except Exception as exc:
        return _format_exception("Error writing file", exc)


def _extract_command_names(command: str) -> list[str]:
    """Return every executable name in a pipeline/chain.

    Splits on |, ||, &&, ; and a single & (background) so that a second
    command hidden behind a separator can't slip past the allowlist. Python's
    ``-m`` target is also an executable boundary: allowing ``python`` must not
    silently allow ``python -m pip`` when ``pip`` itself is not approved.
    """
    import re
    # Operators inside quoted arguments are data, not command boundaries.
    segments = []
    quote = None
    escaped = False
    start = 0
    for index, character in enumerate(command):
        if escaped:
            escaped = False
            continue
        if character == "\\" and quote != "'":
            escaped = True
            continue
        if quote:
            if character == quote:
                quote = None
            continue
        if character in {"'", '"'}:
            quote = character
        elif character in {"|", "&", ";"}:
            segments.append(command[start:index])
            start = index + 1
    segments.append(command[start:])
    names: list[str] = []
    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        try:
            tokens = shlex.split(seg)
        except ValueError:
            tokens = seg.split()
        if tokens:
            name = tokens[0]
            names.append(name)
            executable = PurePath(name).name.lower()
            if re.fullmatch(r"python(?:\d+(?:\.\d+)*)?(?:\.exe)?", executable):
                try:
                    module_index = tokens.index("-m") + 1
                except ValueError:
                    continue
                if module_index >= len(tokens):
                    continue
                module = tokens[module_index].split(".", 1)[0]
                if module:
                    names.append(module)
    return names


# Substrings that let a command spawn another command outside the allowlist.
_BASH_FORBIDDEN_SUBSTRINGS = ("`", "$(", "<(", ">(")
# Flags that run an arbitrary command as an argument (e.g. `find . -exec rm {} \;`).
_BASH_EXEC_FLAGS = {"-exec", "-execdir", "-ok", "-okdir"}
_BASH_REDIRECT_OPS = (">>", "&>", "<<<", "<<", ">", "<")


def _redirection_target_escapes(tokens: list[str]) -> bool:
    """True if a redirection (>, >>, <, ...) targets a path outside the workspace."""
    for i, tok in enumerate(tokens):
        target: Optional[str] = None
        if tok in {">", ">>", "<", "<<", "<<<", "&>", ">&"}:
            target = tokens[i + 1] if i + 1 < len(tokens) else None
        else:
            for op in _BASH_REDIRECT_OPS:
                if tok.startswith(op) and len(tok) > len(op):
                    target = tok[len(op):]
                    break
        if not target:
            continue
        # File-descriptor duplication like `2>&1` / `>&2` is not a path.
        if target.isdigit() or target.startswith("&"):
            continue
        if os.path.isabs(target) or ".." in Path(target).parts:
            return True
    return False


def _command_is_allowed(name: str) -> bool:
    """Check one command name against the allowlist.

    Also accepts a path whose final component is allowlisted, because that is
    how project-local tooling is normally invoked: `.venv/bin/pytest`,
    `./node_modules/.bin/jest`, `/usr/local/bin/node`. Without this, enabling
    `pytest` would still fail for most real projects.

    The tradeoff, stated plainly: allowlisting `pytest` also permits
    `<any-path>/pytest`. The allowlist constrains *which program names* run, not
    which file on disk backs them — it is a coarse guard layered under the
    human confirmation gate, not a sandbox.
    """
    allowed = _allowed_commands()
    if name in allowed:
        return True
    if "/" in name or "\\" in name:
        return PurePath(name).name in allowed
    return False


def _validate_bash_command(command: str) -> Optional[str]:
    """Return an error string if *command* is unsafe under the allowlist, else None.

    Defense-in-depth for the `shell=True` execution below: the shell is
    Turing-complete, so this is a best-effort denylist layered on top of the
    allowlist and the human confirmation gate — not a complete sandbox.
    """
    if not command.strip():
        return "Command cannot be empty."
    if "\n" in command:
        return "Multi-line commands are not allowed."
    for sub in _BASH_FORBIDDEN_SUBSTRINGS:
        if sub in command:
            return "Command/process substitution (`, $(), <(), >()) is not allowed."

    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    for tok in tokens:
        if tok in _BASH_EXEC_FLAGS:
            return f"The '{tok}' option is not allowed (it can execute arbitrary commands)."
    if _redirection_target_escapes(tokens):
        return "Redirecting to a path outside the workspace is not allowed."

    command_names = _extract_command_names(command)
    if not command_names:
        return "Command cannot be empty."
    disallowed = [n for n in command_names if not _command_is_allowed(n)]
    if disallowed:
        allowed = ", ".join(sorted(_allowed_commands()))
        return f"Command(s) not allowed: {', '.join(disallowed)}. Allowed: {allowed}"
    return None


def run_bash_command(command: str) -> str:
    if not _bash_enabled():
        return "Error: The bash tool is disabled by configuration."
    
    if _read_only():
        return "Error: Bash commands are disabled in read-only mode."

    try:
        validation_error = _validate_bash_command(command)
        if validation_error:
            return f"Error: {validation_error}"

        command_names = _extract_command_names(command)

        if not _confirm_action(
            "run_bash_command",
            {
                "command": command,
                "command_name": command_names[0],
                "cwd": str(_workspace_root()),
            },
        ):
            return f"Error executing command: command '{command}' was not approved."

        if _dry_run():
            return f"[Dry Run] Successfully executed command: {command} (simulated)"

        from apsara_cli.engine.turn_checkpoints import capture_turn_workspace
        capture_turn_workspace(_workspace_root())
        timeout_seconds = _bash_timeout()
        from apsara_cli.engine.cancellation import run_command
        result = run_command(command, shell=True, timeout=timeout_seconds, cwd=str(_workspace_root()))
        output = (
            f"STDOUT:\n{result.stdout}\n"
            f"STDERR:\n{result.stderr}\n"
            f"EXIT CODE: {result.returncode}"
        )
        if result.returncode != 0:
            return f"Error: Command exited with code {result.returncode}.\n{output}"
        return output
    except subprocess.TimeoutExpired:
        return (
            f"Error: Command timed out after {_bash_timeout()} seconds. "
            "Raise it with --bash-timeout if the command legitimately takes longer."
        )
    except Exception as exc:
        return _format_exception("Error executing command", exc)


def start_process(command: str) -> str:
    """Start an allowlisted command in the background and return its id."""
    if not _bash_enabled():
        return "Error: The bash tool is disabled by configuration."
    if _read_only():
        return "Error: Background processes are disabled in read-only mode."
    validation_error = _validate_bash_command(command)
    if validation_error:
        return f"Error: {validation_error}"
    if not _confirm_action("start_process", {"command": command, "cwd": str(_workspace_root())}):
        return f"Error: Background command '{command}' was not approved."
    if _dry_run():
        return f"[Dry Run] Would start background process: {command}"
    from apsara_cli.engine.turn_checkpoints import capture_turn_workspace
    capture_turn_workspace(_workspace_root())
    from apsara_cli.engine.processes import PROCESS_MANAGER
    item = PROCESS_MANAGER.start(command, _workspace_root())
    return f"Started process {item.process_id}: {command}"


def list_processes() -> str:
    from apsara_cli.engine.processes import PROCESS_MANAGER
    items = PROCESS_MANAGER.list(_workspace_root())
    return "\n".join(f"{p.process_id}  {p.status}  {p.command}" for p in items) or "No background processes."


def process_output(process_id: str, lines: int = 100) -> str:
    from apsara_cli.engine.processes import PROCESS_MANAGER
    item = PROCESS_MANAGER.get(process_id)
    if item is None or item.cwd != _workspace_root():
        return f"Error: Process '{process_id}' not found."
    output = list(item.output)[-max(1, min(lines, 1000)):]
    return f"STATUS: {item.status}\n" + ("\n".join(output) or "No output yet.")


def stop_process(process_id: str) -> str:
    if _read_only():
        return "Error: Process control is disabled in read-only mode."
    from apsara_cli.engine.processes import PROCESS_MANAGER
    item = PROCESS_MANAGER.get(process_id)
    if item is None or item.cwd != _workspace_root():
        return f"Error: Process '{process_id}' not found."
    if not _confirm_action("stop_process", {"process_id": process_id, "command": item.command}):
        return "Error: Stopping the process was not approved."
    PROCESS_MANAGER.stop(process_id)
    return f"Stopped process {process_id}."


def search_files(pattern: str, root_dir: str = ".") -> str:
    try:
        resolved_root = _resolve_path(root_dir, must_exist=True)
        if not resolved_root.is_dir():
            return f"Error searching files: '{root_dir}' is not a directory."

        try:
            result = subprocess.run(
                [
                    "rg",
                    "--line-number",
                    "--no-heading",
                    "--text",
                    "--max-count",
                    "100",
                    "--",
                    pattern,
                    str(resolved_root),
                ],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(_workspace_root()),
            )
        except FileNotFoundError:
            result = subprocess.run(
                ["grep", "-rnIE", *[f"--exclude-dir={directory}" for directory in (
                    ".git", ".apsara", "node_modules", ".venv", "__pycache__", ".pytest_cache",
                )], "--", pattern, str(resolved_root)],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(_workspace_root()),
            )

        if result.returncode not in (0, 1):
            return _format_exception("Error searching files", Exception(result.stderr))
        return result.stdout if result.stdout else "No matches found."
    except Exception as exc:
        return _format_exception("Error searching files", exc)


def list_project_structure(root_dir: str = ".") -> str:
    try:
        resolved_root = _resolve_path(root_dir, must_exist=True)
        if not resolved_root.is_dir():
            return f"Error listing structure: '{root_dir}' is not a directory."

        entries = []
        max_depth = 3
        max_entries = 100
        root_depth = len(resolved_root.parts)

        for current_root, dir_names, file_names in os.walk(resolved_root):
            current_path = Path(current_root)
            depth = len(current_path.parts) - root_depth
            dir_names[:] = sorted(
                [
                    directory
                    for directory in dir_names
                    if not directory.startswith(".") and depth < max_depth
                ]
            )

            if depth > max_depth:
                continue

            if current_path != resolved_root:
                entries.append(str(current_path))
                if len(entries) >= max_entries:
                    break

            for file_name in sorted(
                file_name for file_name in file_names if not file_name.startswith(".")
            ):
                file_path = current_path / file_name
                entries.append(str(file_path))
                if len(entries) >= max_entries:
                    break

            if len(entries) >= max_entries:
                break

        return "\n".join(entries) if entries else "Empty or could not read."
    except Exception as exc:
        return _format_exception("Error listing structure", exc)


def replace_file_lines(
    path: str,
    start_line: int,
    end_line: int,
    replacement_content: str,
) -> str:
    try:
        if _read_only():
            return "Error: Destructive operations are disabled in read-only mode."

        resolved_path = _resolve_path(path, must_exist=True)
        if not resolved_path.is_file():
            return f"Error replacing lines: '{path}' is not a file."

        with resolved_path.open("r", encoding="utf-8") as file_handle:
            lines = file_handle.readlines()

        if start_line < 1 or start_line > len(lines):
            return f"Error: start_line {start_line} is out of bounds."
        if end_line < start_line:
            return "Error: end_line cannot be before start_line."

        original_content = "".join(lines)
        original_slice = "".join(lines[start_line - 1 : end_line])
        prefix = lines[: start_line - 1]
        suffix = lines[end_line:] if end_line <= len(lines) else []
        updated_content = "".join(prefix)
        if replacement_content:
            updated_content += replacement_content
            if not replacement_content.endswith("\n"):
                updated_content += "\n"
        updated_content += "".join(suffix)
        display_path = _display_path(resolved_path)
        diff_preview, diff_full, diff_editor, diff_truncated = _build_text_diff(
            original_content,
            updated_content,
            display_path,
        )

        if not _confirm_action(
            "replace_file_lines",
            {
                "path": str(resolved_path),
                "display_path": display_path,
                "start_line": start_line,
                "end_line": end_line,
                "original_preview": original_slice[:800],
                "replacement_preview": replacement_content[:800],
                "diff_preview": diff_preview,
                "diff_full": diff_full,
                "diff_editor": diff_editor,
                "diff_truncated": diff_truncated,
            },
        ):
            return (
                "Error replacing lines: "
                f"update to '{resolved_path}' was not approved."
            )

        if _dry_run():
            return (
                f"[Dry Run] Successfully replaced lines "
                f"{start_line} to {end_line} in {resolved_path} (simulated)."
            )

        _checkpoint([resolved_path], f"Before replacing lines in {display_path}")
        with resolved_path.open("w", encoding="utf-8") as file_handle:
            file_handle.writelines(prefix)
            if replacement_content:
                file_handle.write(replacement_content)
                if not replacement_content.endswith("\n"):
                    file_handle.write("\n")
            file_handle.writelines(suffix)

        return (
            "Successfully replaced lines "
            f"{start_line} to {end_line} in {resolved_path}."
        ) + _syntax_diagnostic_suffix(resolved_path)
    except Exception as exc:
        return _format_exception("Error replacing lines", exc)


def edit_file(
    path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> str:
    """Replace an exact snippet of text in a file.

    Preferred over replace_file_lines: line numbers go stale as soon as an
    earlier edit shifts them, and a stale range silently rewrites the wrong
    region. Matching on the text itself fails loudly instead.
    """
    try:
        if _read_only():
            return "Error: Destructive operations are disabled in read-only mode."

        if not old_string:
            return (
                "Error editing file: old_string must not be empty. "
                "Use write_to_file to create a file or replace its entire contents."
            )
        if old_string == new_string:
            return "Error editing file: old_string and new_string are identical."

        resolved_path = _resolve_path(path, must_exist=True)
        if not resolved_path.is_file():
            return f"Error editing file: '{path}' is not a file."

        file_size = resolved_path.stat().st_size
        if file_size > _max_file_size_bytes():
            return (
                "Error editing file: "
                f"'{path}' exceeds {_max_file_size_bytes()} bytes."
            )

        original_content = resolved_path.read_text(encoding="utf-8")

        occurrences = original_content.count(old_string)
        if occurrences == 0:
            return (
                f"Error editing file: old_string was not found in '{path}'. "
                "It must match the file byte-for-byte, including indentation and "
                "trailing whitespace. Re-read the file and copy the text exactly."
            )
        if occurrences > 1 and not replace_all:
            return (
                f"Error editing file: old_string appears {occurrences} times in "
                f"'{path}'. Include more surrounding context so the match is unique, "
                "or pass replace_all=true to change every occurrence."
            )

        updated_content = original_content.replace(
            old_string, new_string, -1 if replace_all else 1
        )
        display_path = _display_path(resolved_path)
        diff_preview, diff_full, diff_editor, diff_truncated = _build_text_diff(
            original_content,
            updated_content,
            display_path,
        )

        if not _confirm_action(
            "edit_file",
            {
                "path": str(resolved_path),
                "display_path": display_path,
                "occurrences": occurrences,
                "replace_all": replace_all,
                "original_preview": old_string[:800],
                "replacement_preview": new_string[:800],
                "diff_preview": diff_preview,
                "diff_full": diff_full,
                "diff_editor": diff_editor,
                "diff_truncated": diff_truncated,
            },
        ):
            return (
                f"Error editing file: update to '{resolved_path}' was not approved."
            )

        replaced = occurrences if replace_all else 1
        plural = "s" if replaced != 1 else ""

        if _dry_run():
            return (
                f"[Dry Run] Successfully replaced {replaced} occurrence{plural} "
                f"in {resolved_path} (simulated)."
            )

        _checkpoint([resolved_path], f"Before editing {display_path}")
        resolved_path.write_text(updated_content, encoding="utf-8")

        return (
            f"Successfully replaced {replaced} occurrence{plural} in {resolved_path}."
        ) + _syntax_diagnostic_suffix(resolved_path)
    except Exception as exc:
        return _format_exception("Error editing file", exc)


def git_status() -> str:
    """Get the current git status of the workspace (short format)."""
    try:
        result = subprocess.run(
            ["git", "status", "--short"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(_workspace_root()),
        )
        if result.returncode != 0:
            return f"Error: Not a git repository or git not installed. {result.stderr}"
        return result.stdout if result.stdout else "No changes (clean repository)."
    except Exception as exc:
        return _format_exception("Error getting git status", exc)


def git_diff(staged: bool = False) -> str:
    """Get the git diff of the workspace.
    Set staged=True to see changes already added to the index."""
    try:
        cmd = ["git", "diff"]
        if staged:
            cmd.append("--cached")

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=20,
            cwd=str(_workspace_root()),
        )
        if result.returncode != 0:
            return f"Error running git diff: {result.stderr}"
        return result.stdout if result.stdout else "No diff available."
    except Exception as exc:
        return _format_exception("Error getting git diff", exc)


def _git_read(args: list[str], timeout: int = 20) -> str:
    try:
        result = subprocess.run(["git", *args], capture_output=True, text=True, timeout=timeout, cwd=str(_workspace_root()))
        if result.returncode != 0:
            return f"Error running git {' '.join(args)}: {result.stderr.strip()}"
        return result.stdout or "No output."
    except Exception as exc:
        return _format_exception("Error running git", exc)


def git_log(limit: int = 20, path: str = "") -> str:
    args = ["log", f"-{max(1, min(limit, 100))}", "--date=short", "--pretty=format:%h %ad %an %s"]
    if path:
        resolved = _resolve_path(path, must_exist=True)
        args.extend(["--", str(resolved.relative_to(_workspace_root()))])
    return _git_read(args)


def git_show(revision: str = "HEAD") -> str:
    if not revision or revision.startswith("-") or not all(c.isalnum() or c in "._/-" for c in revision):
        return "Error: Invalid revision."
    return _git_read(["show", "--stat", "--oneline", "--decorate", revision])


def git_blame(path: str, start_line: int = 1, end_line: int = 0) -> str:
    resolved = _resolve_path(path, must_exist=True)
    relative = str(resolved.relative_to(_workspace_root()))
    args = ["blame", "--date=short"]
    if end_line:
        args.extend(["-L", f"{max(1, start_line)},{max(start_line, end_line)}"])
    args.extend(["--", relative])
    return _git_read(args)


def list_workspace_checkpoints() -> str:
    """List recoverable snapshots created before file mutations."""
    from apsara_cli.engine.checkpoints import list_checkpoints

    checkpoints = list_checkpoints(_workspace_root())
    if not checkpoints:
        return "No checkpoints available."
    return "\n".join(
        f"{item['id']}  {item.get('label', '')}  ({len(item.get('files', []))} files)"
        for item in checkpoints
    )


def undo_last_checkpoint(checkpoint_id: str = "") -> str:
    """Restore a checkpoint, defaulting to the most recent snapshot."""
    if _read_only():
        return "Error: Undo is disabled in read-only mode."
    requested = checkpoint_id or "latest"
    if not _confirm_action("undo_checkpoint", {"checkpoint_id": requested}):
        return f"Error: restore of checkpoint '{requested}' was not approved."
    from apsara_cli.engine.checkpoints import restore_checkpoint

    try:
        result = restore_checkpoint(_workspace_root(), checkpoint_id or None)
    except FileNotFoundError as exc:
        return f"Error: {exc}"
    changed = result["restored"] + result["removed"]
    return (
        f"Restored checkpoint {result['id']} ({result['label']}). "
        f"Updated {len(changed)} file(s): {', '.join(changed) or 'none'}."
    )


def list_turn_checkpoints_tool() -> str:
    """List atomic checkpoints grouped by complete agent turn."""
    from apsara_cli.engine.turn_checkpoints import format_turn_checkpoint, list_turn_checkpoints

    turns = list_turn_checkpoints(_workspace_root())
    if not turns:
        return "No turn checkpoints available."
    return "\n".join(format_turn_checkpoint(item) for item in turns[:20])


def undo_turn_checkpoint(turn_id: str = "", force: bool = False) -> str:
    """Restore every built-in file mutation from one agent turn."""
    if _read_only():
        return "Error: Turn rollback is disabled in read-only mode."
    requested = turn_id or "latest"
    if not _confirm_action("undo_turn", {"turn_id": requested, "force": force}):
        return f"Error: rollback of turn '{requested}' was not approved."
    from apsara_cli.engine.turn_checkpoints import restore_turn_checkpoint

    try:
        result = restore_turn_checkpoint(_workspace_root(), turn_id or None, force=force)
    except (FileNotFoundError, OSError, ValueError) as exc:
        return f"Error: {exc}"
    rollback = result.get("rollback") or {}
    changed = list(rollback.get("restored") or []) + list(rollback.get("removed") or [])
    conflicts = list(rollback.get("conflicts") or [])
    suffix = f" Conflicts left untouched: {', '.join(conflicts)}." if conflicts else ""
    return f"Rolled back turn {result['id']}. Updated {len(changed)} path(s): {', '.join(changed) or 'none'}.{suffix}"


def list_symbols(path: str) -> str:
    """List semantic definitions in any supported source file."""
    try:
        resolved_path = _resolve_path(path, must_exist=True)
        if not resolved_path.is_file():
            return f"Error: '{path}' is not a file."
        from apsara_cli.engine.intelligence import LANGUAGES, symbols_in_file
        if resolved_path.suffix.lower() not in LANGUAGES:
            return f"Error: '{path}' is not a supported source file."
        symbols = symbols_in_file(resolved_path)
        if not symbols:
            return f"No symbols found in {path}."
        return "\n".join(
            f"{item.kind.title()}: {item.name} (lines {item.start_line}-{item.end_line}, {item.provider})"
            for item in symbols
        )
    except Exception as exc:
        return _format_exception("Error listing symbols", exc)


def repository_map(max_files: int = 200) -> str:
    """Summarize languages, manifests, files, and important symbols."""
    from apsara_cli.engine.intelligence import repository_map as build_map
    return build_map(_workspace_root(), max(1, min(max_files, 1000)))


def find_symbol(query: str) -> str:
    """Find symbol definitions across supported source languages."""
    from apsara_cli.engine.intelligence import find_symbol as search_symbols
    return search_symbols(_workspace_root(), query)


def go_to_definition(name: str, path_hint: str = "") -> str:
    """Resolve exact definitions, preferring an optional path hint."""
    from apsara_cli.engine.intelligence import definitions
    matches = [item for item in definitions(_workspace_root(), name, path_hint) if item.name == name]
    if not matches:
        return f"No definition found for '{name}'."
    return "\n".join(
        f"{item.location(_workspace_root())}: {item.name} ({item.kind}, {item.provider})"
        for item in matches
    )


def find_references(name: str, path: str = "") -> str:
    """Find source references and label known definitions."""
    from apsara_cli.engine.intelligence import references
    matches = references(_workspace_root(), name, path)
    if not matches:
        return f"No references found for '{name}'."
    lines = []
    for item in matches:
        relative = item.path.relative_to(_workspace_root())
        label = "definition" if item.is_definition else "reference"
        lines.append(f"{relative}:{item.line}:{item.column + 1}: {label} ({item.provider})")
    return "\n".join(lines)


def lsp_go_to_definition(path: str, line: int, column: int) -> str:
    """Use an installed language server for type-aware definition lookup."""
    try:
        resolved = _resolve_path(path, must_exist=True)
        from apsara_cli.engine.lsp import query_locations
        lines = query_locations(
            _workspace_root(), resolved, line=line, column=column, operation="definition"
        )
        return "\n".join(lines) if lines else "No LSP definition found."
    except Exception as exc:
        return _format_exception("Error querying language server", exc)


def lsp_find_references(path: str, line: int, column: int) -> str:
    """Use an installed language server for type-aware reference lookup."""
    try:
        resolved = _resolve_path(path, must_exist=True)
        from apsara_cli.engine.lsp import query_locations
        lines = query_locations(
            _workspace_root(), resolved, line=line, column=column, operation="references"
        )
        return "\n".join(lines) if lines else "No LSP references found."
    except Exception as exc:
        return _format_exception("Error querying language server", exc)


def code_diagnostics(path: str = "", project: bool = False) -> str:
    """Run syntax diagnostics for a file or an explicit native project check."""
    from apsara_cli.engine.intelligence import format_diagnostics, project_diagnostics, syntax_diagnostics
    if project:
        checker, issues = project_diagnostics(_workspace_root())
        if checker == "none":
            return "No supported project checker found (pyright, tsc, go, or cargo)."
        return format_diagnostics(_workspace_root(), issues) or f"Project diagnostics clean ({checker})."
    if not path:
        return "Error: path is required unless project=true."
    resolved_path = _resolve_path(path, must_exist=True)
    issues = syntax_diagnostics(resolved_path)
    return format_diagnostics(_workspace_root(), issues) or f"Syntax diagnostics clean for {_display_path(resolved_path)}."


def verify_project(
    phase: str = "full", isolated: bool = False, timeout: int = 0, fresh: bool = False
) -> str:
    """Run manifest-aware project checks and return structured evidence."""
    from apsara_cli.engine.verification import (
        detect_verification_commands,
        format_verification_report,
        run_verification,
        verification_digest_source,
    )

    if _read_only():
        return "Error: Project verification is disabled in read-only mode because it executes workspace code."
    try:
        commands = detect_verification_commands(
            _workspace_root(), "targeted" if phase == "targeted" else "full"
        )
    except (OSError, ValueError) as exc:
        return f"Error: Verification could not run: {exc}"
    if not commands:
        return format_verification_report(
            run_verification(_workspace_root(), phase=phase, isolated=isolated, timeout=timeout or _bash_timeout())
        )
    digest = trust.digest_text(verification_digest_source(commands))
    if not request_workspace_trust(
        "verification:project",
        digest,
        {
            "kind": "verification",
            "display_path": ", ".join(" ".join(item.command) for item in commands),
            "commands": [list(item.command) for item in commands],
            "isolated": isolated,
            "phase": phase,
        },
    ):
        return "Error: Project verification was not approved."
    if _dry_run():
        return "Verification skipped in dry-run mode (commands were not executed)."
    from apsara_cli.engine.turn_checkpoints import capture_turn_workspace
    capture_turn_workspace(_workspace_root())
    try:
        report = run_verification(
            _workspace_root(),
            phase=phase,
            isolated=isolated,
            timeout=timeout or _bash_timeout(),
        )
    except (OSError, RuntimeError, ValueError) as exc:
        return f"Error: Verification could not run: {exc}"
    return format_verification_report(report)


def replace_symbol(path: str, symbol: str, replacement: str) -> str:
    """Replace one complete semantic definition by its parser-derived span."""
    try:
        if _read_only():
            return "Error: Destructive operations are disabled in read-only mode."
        resolved_path = _resolve_path(path, must_exist=True)
        from apsara_cli.engine.intelligence import symbols_in_file
        matches = [item for item in symbols_in_file(resolved_path) if item.name == symbol]
        if not matches:
            return f"Error replacing symbol: definition '{symbol}' was not found in '{path}'."
        if len(matches) > 1:
            locations = ", ".join(str(item.start_line) for item in matches)
            return f"Error replacing symbol: '{symbol}' is ambiguous at lines {locations}."
        target = matches[0]
        if target.provider == "pattern":
            return "Error replacing symbol: a precise parser span is unavailable. Install 'apsara-agentic[intelligence]' or use edit_file."
        content = resolved_path.read_text(encoding="utf-8")
        lines = content.splitlines(keepends=True)
        normalized = replacement
        if normalized and not normalized.endswith("\n"):
            normalized += "\n"
        updated = "".join(lines[:target.start_line - 1]) + normalized + "".join(lines[target.end_line:])
        display = _display_path(resolved_path)
        diff_preview, diff_full, diff_editor, diff_truncated = _build_text_diff(content, updated, display)
        if not _confirm_action("replace_symbol", {
            "path": str(resolved_path), "display_path": display, "symbol": symbol,
            "start_line": target.start_line, "end_line": target.end_line,
            "diff_preview": diff_preview, "diff_full": diff_full,
            "diff_editor": diff_editor, "diff_truncated": diff_truncated,
        }):
            return f"Error replacing symbol: update to '{resolved_path}' was not approved."
        if _dry_run():
            return f"[Dry Run] Replaced symbol '{symbol}' in {resolved_path} (simulated)."
        _checkpoint([resolved_path], f"Before replacing symbol {symbol} in {display}")
        resolved_path.write_text(updated, encoding="utf-8")
        return f"Replaced symbol '{symbol}' in {resolved_path}." + _syntax_diagnostic_suffix(resolved_path)
    except Exception as exc:
        return _format_exception("Error replacing symbol", exc)


def read_project_memory() -> str:
    from apsara_cli.engine.memory import read_memory
    return read_memory(_workspace_root()) or "No project memory recorded."


def remember_project_note(note: str) -> str:
    if _read_only():
        return "Error: Project memory writes are disabled in read-only mode."
    if not note.strip():
        return "Error: Memory note cannot be empty."
    if not _confirm_action("remember_project_note", {"note": note}):
        return "Error: Project memory update was not approved."
    from apsara_cli.engine.memory import add_memory
    path = add_memory(_workspace_root(), note)
    return f"Saved project memory to {_display_path(path)}."


def _tool_definition(
    name: str,
    description: str,
    properties: Dict[str, Any],
    required: Optional[list[str]] = None,
) -> Dict[str, Any]:
    definition = {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
            },
        },
    }
    if required:
        definition["function"]["parameters"]["required"] = required
    return definition


def get_request_tools() -> list[Dict[str, Any]]:
    """Only core and explicitly discovered schemas enter the model request."""
    from apsara_cli.engine.capabilities import CORE_TOOLS, current_capabilities
    state = current_capabilities()
    names = CORE_TOOLS | (state.tools if state is not None else set())
    return [tool for tool in get_agent_tools() if tool["function"]["name"] in names]


def discover_tools(query: str, limit: int = 5) -> str:
    """Search descriptions and activate a bounded set for subsequent requests."""
    from apsara_cli.engine.capabilities import CORE_TOOLS, MAX_ACTIVE_TOOLS, current_capabilities
    if not query.strip() or not 1 <= limit <= 8:
        return "Error: Supply a non-empty query and a limit between 1 and 8."
    terms = query.lower().replace("_", " ").split()
    ranked = []
    for tool in get_agent_tools():
        function = tool["function"]
        name = function["name"]
        description = function.get("description", "")
        haystack = (name.replace("_", " ") + " " + description).lower()
        score = sum(term in haystack for term in terms)
        if name.lower() == query.strip().lower():
            score += len(terms) + 10
        if score:
            ranked.append((score, name, description))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    state = current_capabilities()
    if state is None:
        return "Error: Tool activation requires an active agent turn."
    results = []
    for _, name, description in ranked[:limit]:
        if name not in CORE_TOOLS and name not in state.tools and len(state.tools) >= MAX_ACTIVE_TOOLS:
            continue
        if name not in CORE_TOOLS:
            state.tools.add(name)
        results.append({"name": name, "description": description[:240], "active": True})
    return json.dumps({"tools": results, "message": "Matched tools are now available. Existing permissions still apply." if results else "No tools matched or the activation limit was reached."})


def list_skills(query: str = "") -> str:
    from apsara_cli.engine.skills import discover_skills
    skills = discover_skills(_workspace_root())
    terms = query.lower().split()
    return json.dumps({"skills": [skill.metadata() for skill in skills
                                 if all(term in (skill.name + " " + skill.description).lower() for term in terms)]})


def read_skill(name: str) -> str:
    from apsara_cli.engine.capabilities import MAX_ACTIVE_SKILL_CHARS, current_capabilities
    from apsara_cli.engine.skills import discover_skills, read_skill_file
    skill = next((item for item in discover_skills(_workspace_root()) if item.name == name), None)
    if skill is None:
        return f"Error: Skill '{name}' was not found. Use list_skills."
    try:
        content = read_skill_file(skill)
    except (OSError, ValueError, UnicodeError) as exc:
        return f"Error reading skill: {exc}"
    state = current_capabilities()
    if state is None:
        return "Error: Loading a skill requires an active agent turn."
    used = sum(len(text) for key, text in state.skills.items() if key != name)
    if used + len(content) > MAX_ACTIVE_SKILL_CHARS:
        return "Error: Active skill instructions exceed the per-turn limit. Use fewer or shorter skills."
    state.skills[name] = content
    return f"Loaded skill '{name}' ({skill.source}): {skill.description}. Full instructions are attached to subsequent requests for this turn."


def read_skill_resource(name: str, path: str) -> str:
    from apsara_cli.engine.skills import discover_skills, read_skill_file
    skill = next((item for item in discover_skills(_workspace_root()) if item.name == name), None)
    if skill is None:
        return f"Error: Skill '{name}' was not found."
    try:
        return read_skill_file(skill, path)
    except (OSError, ValueError, UnicodeError) as exc:
        return f"Error reading skill resource: {exc}"


def read_tool_result(identifier: str, start_line: int = 1, line_count: int = 80, char_offset: int | None = None) -> str:
    from apsara_cli.engine.context import read_result
    try:
        return read_result(_workspace_root(), identifier, start_line, line_count, char_offset=char_offset)
    except (OSError, ValueError, UnicodeError) as exc:
        return f"Error reading tool result: {exc}"


def get_agent_tools() -> list[Dict[str, Any]]:
    tools = [
        _tool_definition(
            "read_file",
            "Read the complete text contents of a file inside the workspace.",
            {
                "path": {
                    "type": "string",
                    "description": "The file path to read. Relative paths resolve from the workspace root.",
                }
            },
            ["path"],
        ),
        _tool_definition(
            "read_file_lines",
            "Read a specific range of lines from a file (1-indexed, inclusive). "
            "Use this instead of read_file when you only need part of a large file. "
            "Returns lines prefixed with their line numbers.",
            {
                "path": {
                    "type": "string",
                    "description": "The file path to read.",
                },
                "start_line": {
                    "type": "integer",
                    "description": "First line to read (1-indexed).",
                },
                "end_line": {
                    "type": "integer",
                    "description": "Last line to read (1-indexed, inclusive).",
                },
            },
            ["path", "start_line", "end_line"],
        ),
        _tool_definition(
            "parallel_read_files",
            "Read up to eight independent workspace files concurrently.",
            {"paths": {"type": "array", "items": {"type": "string"}, "maxItems": 8}},
            ["paths"],
        ),
        _tool_definition(
            "glob_search",
            "Find files or directories matching a glob pattern inside the workspace "
            "(e.g. '**/*.py', 'src/**/*.ts', 'tests/test_*.py'). Returns up to 200 matches.",
            {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern. Use ** for recursive matching.",
                },
                "root_dir": {
                    "type": "string",
                    "description": "Directory to search from. Defaults to workspace root.",
                },
            },
            ["pattern"],
        ),
        _tool_definition(
            "create_directory",
            "Create a directory (and any missing parent directories) inside the workspace after user approval.",
            {
                "path": {
                    "type": "string",
                    "description": "Directory path to create. Relative paths resolve from the workspace root.",
                }
            },
            ["path"],
        ),
        _tool_definition(
            "delete_file",
            "Delete a file inside the workspace. Requires user confirmation.",
            {
                "path": {
                    "type": "string",
                    "description": "Path of the file to delete.",
                }
            },
            ["path"],
        ),
        _tool_definition(
            "move_file",
            "Move or rename a file within the workspace. Requires user confirmation.",
            {
                "src": {
                    "type": "string",
                    "description": "Current path of the file.",
                },
                "dest": {
                    "type": "string",
                    "description": "Destination path or directory.",
                },
            },
            ["src", "dest"],
        ),
        _tool_definition(
            "write_to_file",
            "Create or overwrite a file with exact string contents inside the configured workspace.",
            {
                "path": {
                    "type": "string",
                    "description": "The file path to write. Relative paths resolve from the workspace root.",
                },
                "content": {
                    "type": "string",
                    "description": "The complete text content to write.",
                },
            },
            ["path", "content"],
        ),
        _tool_definition(
            "search_files",
            "Search for a string or regex inside files under the configured workspace.",
            {
                "pattern": {
                    "type": "string",
                    "description": "The search term or regex.",
                },
                "root_dir": {
                    "type": "string",
                    "description": "The directory to search. Defaults to the workspace root.",
                },
            },
            ["pattern"],
        ),
        _tool_definition(
            "list_project_structure",
            "List files and folders up to three levels deep under the configured workspace.",
            {
                "root_dir": {
                    "type": "string",
                    "description": "The directory to inspect. Defaults to the workspace root.",
                }
            },
        ),
        _tool_definition(
            "edit_file",
            "Edit a file by replacing an exact snippet of text. This is the preferred "
            "way to modify an existing file. old_string must match the file exactly, "
            "including indentation and whitespace, and must be unique unless "
            "replace_all is true — include a few surrounding lines to make it unique. "
            "The edit fails loudly if the text is not found, so it is safe to use "
            "after earlier edits have shifted the file.",
            {
                "path": {
                    "type": "string",
                    "description": "The file path to update.",
                },
                "old_string": {
                    "type": "string",
                    "description": "Exact existing text to replace, copied verbatim from the file.",
                },
                "new_string": {
                    "type": "string",
                    "description": "Text to write in its place. Use an empty string to delete the snippet.",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace every occurrence instead of requiring a unique match. Defaults to false.",
                },
            },
            ["path", "old_string", "new_string"],
        ),
        _tool_definition(
            "replace_file_lines",
            "Replace a range of lines in a file by line number. Prefer edit_file: line "
            "numbers become stale as soon as an earlier edit shifts them. Use this only "
            "when the target genuinely has no unique text to match on, and always "
            "re-read the file with read_file_lines immediately beforehand.",
            {
                "path": {
                    "type": "string",
                    "description": "The file path to update.",
                },
                "start_line": {
                    "type": "integer",
                    "description": "1-indexed starting line number of the block to replace.",
                },
                "end_line": {
                    "type": "integer",
                    "description": "1-indexed ending line number of the block to replace.",
                },
                "replacement_content": {
                    "type": "string",
                    "description": "The exact string content to insert over the replaced lines.",
                },
            },
            ["path", "start_line", "end_line", "replacement_content"],
        ),
        _tool_definition(
            "git_status",
            "Show the working tree status (short format). Use this to see which files are modified or untracked.",
            {},
        ),
        _tool_definition(
            "git_diff",
            "Show changes between the working tree and the index. Useful for reviewing work.",
            {
                "staged": {
                    "type": "boolean",
                    "description": "If true, show diff for changes already added to git (staged).",
                }
            },
        ),
        _tool_definition("git_log", "Show recent commit history, optionally for one path.", {"limit": {"type": "integer"}, "path": {"type": "string"}}),
        _tool_definition("git_show", "Show a revision summary and patch.", {"revision": {"type": "string"}}),
        _tool_definition("git_blame", "Show line authorship for a file or line range.", {"path": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}}, ["path"]),
        _tool_definition(
            "list_symbols",
            "List parser-derived classes, functions, methods, types, interfaces, and enums in a supported source file.",
            {
                "path": {
                    "type": "string",
                    "description": "Path to the file to analyze.",
                }
            },
            ["path"],
        ),
        _tool_definition(
            "list_workspace_checkpoints",
            "List automatic snapshots created before this agent changed files.",
            {},
        ),
        _tool_definition(
            "undo_last_checkpoint",
            "Restore an automatic file checkpoint after user approval. Omit checkpoint_id to undo the latest mutation.",
            {"checkpoint_id": {"type": "string", "description": "Optional checkpoint id."}},
        ),
        _tool_definition("list_turn_checkpoints_tool", "List atomic checkpoints grouped by agent turn.", {}),
        _tool_definition(
            "undo_turn_checkpoint",
            "Roll back every captured file mutation from an agent turn after user approval.",
            {"turn_id": {"type": "string", "description": "Optional turn/run id; defaults to latest."},
             "force": {"type": "boolean", "description": "Overwrite later user edits only when the user explicitly requests a forced restore."}},
        ),
        _tool_definition(
            "repository_map",
            "Build a compact multi-language repository map with manifests and symbols.",
            {"max_files": {"type": "integer", "description": "Maximum source files to include."}},
        ),
        _tool_definition(
            "find_symbol",
            "Find matching class, function, type, interface, or enum definitions across the repository.",
            {"query": {"type": "string", "description": "Case-insensitive symbol name fragment."}},
            ["query"],
        ),
        _tool_definition(
            "go_to_definition",
            "Resolve an exact symbol definition across the repository. Use path_hint to narrow ambiguous names.",
            {
                "name": {"type": "string", "description": "Exact symbol name."},
                "path_hint": {"type": "string", "description": "Optional path fragment used to narrow results."},
            },
            ["name"],
        ),
        _tool_definition(
            "find_references",
            "Find definitions and references for an identifier across supported source files.",
            {
                "name": {"type": "string", "description": "Exact identifier to locate."},
                "path": {"type": "string", "description": "Optional path fragment used to limit the search."},
            },
            ["name"],
        ),
        _tool_definition(
            "lsp_go_to_definition",
            "Use an installed language server for type-aware definition lookup at a source position. "
            "Fall back to go_to_definition when no server is installed.",
            {
                "path": {"type": "string", "description": "Source file path."},
                "line": {"type": "integer", "description": "1-based line."},
                "column": {"type": "integer", "description": "1-based column."},
            },
            ["path", "line", "column"],
        ),
        _tool_definition(
            "lsp_find_references",
            "Use an installed language server for type-aware references at a source position. "
            "Fall back to find_references when no server is installed.",
            {
                "path": {"type": "string", "description": "Source file path."},
                "line": {"type": "integer", "description": "1-based line."},
                "column": {"type": "integer", "description": "1-based column."},
            },
            ["path", "line", "column"],
        ),
        _tool_definition(
            "code_diagnostics",
            "Check one source file for syntax errors, or run the project's native checker when project=true. Project checks may take time.",
            {
                "path": {"type": "string", "description": "Source file for a fast syntax check."},
                "project": {"type": "boolean", "description": "Run pyright, tsc, go test, or cargo check for the whole project."},
            },
        ),
        _tool_definition(
            "verify_project",
            "Run manifest-aware tests, type checks, lint, and build checks with structured results. "
            "Use phase=baseline before editing, targeted while repairing, and full before finishing. "
            "Set isolated=true to run in a disposable snapshot.",
            {
                "phase": {
                    "type": "string",
                    "enum": ["baseline", "targeted", "full"],
                    "description": "Verification stage.",
                },
                "isolated": {
                    "type": "boolean",
                    "description": "Run checks in a disposable workspace snapshot.",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Per-command timeout in seconds; zero uses the configured default.",
                },
                "fresh": {"type": "boolean", "description": "Execute again even if unchanged passing evidence exists; use for deliberate repeated or flaky checks."},
            },
        ),
        _tool_definition(
            "request_critic",
            "Ask an independent read-only critic to review the current plan, diff, or tests. "
            "Use before a risky implementation and after full verification for non-trivial changes.",
            {
                "objective": {
                    "type": "string",
                    "description": "The user objective the critic should validate.",
                },
                "focus": {
                    "type": "string",
                    "description": "Specific plan, implementation, or test concern to review.",
                },
                "fresh": {"type": "boolean", "description": "Request a new independent review even if this request was already approved."},
            },
            ["objective"],
        ),
        _tool_definition(
            "replace_symbol",
            "Replace one complete function, class, method, or type using its parser-derived source span. Requires confirmation and creates a checkpoint.",
            {
                "path": {"type": "string", "description": "Supported source file containing the definition."},
                "symbol": {"type": "string", "description": "Exact definition name; it must be unique in the file."},
                "replacement": {"type": "string", "description": "Complete replacement definition, including indentation."},
            },
            ["path", "symbol", "replacement"],
        ),
        _tool_definition("read_project_memory", "Read persistent workspace-specific notes.", {}),
        _tool_definition(
            "remember_project_note",
            "Save a durable workspace fact or convention for future agent turns.",
            {"note": {"type": "string", "description": "Concise fact or convention to remember."}},
            ["note"],
        ),
    ]

    tools.extend([
        _tool_definition("discover_tools", "Find and activate specialized built-in, plugin, or MCP tools by name, server, or task description. Search before using a tool absent from your current schemas.", {
            "query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 8},
        }, ["query"]),
        _tool_definition("list_skills", "List short metadata for bundled, user, and project skills. Full instructions are loaded only when needed.", {"query": {"type": "string"}}),
        _tool_definition("read_skill", "Load a named skill's instructions for this turn. Skills do not grant permission to execute commands or external actions.", {"name": {"type": "string"}}, ["name"]),
        _tool_definition("read_skill_resource", "Read a referenced file relative to a named skill directory. Scripts are read as text, never executed by this tool.", {"name": {"type": "string"}, "path": {"type": "string"}}, ["name", "path"]),
        _tool_definition("read_tool_result", "Retrieve a bounded line range from a full tool result saved locally when output was shortened.", {
            "identifier": {"type": "string"}, "start_line": {"type": "integer", "minimum": 1},
            "line_count": {"type": "integer", "minimum": 1, "maximum": 200},
            "char_offset": {"type": "integer", "minimum": 0, "description": "Use character pagination for a long single-line result; overrides line selection."},
        }, ["identifier"]),
    ])

    # Load local workspace plugins
    local_plugins = _load_local_plugins()
    for tool_def, _ in local_plugins:
        tools.append(tool_def)

    if _bash_enabled():
        tools.append(
            _tool_definition(
                "run_bash_command",
                "Execute an allowlisted shell command from the workspace root. Pipes (|), &&, ||, and ; are supported as long as every command name is in the allowlist. Command substitution ($() and backticks) is not allowed.",
                {
                    "command": {
                        "type": "string",
                        "description": "Shell command string. Pipes and &&/||/; chaining are allowed between allowlisted commands.",
                    }
                },
                ["command"],
            )
        )
        tools.extend([
            _tool_definition("start_process", "Start an allowlisted long-running command in the background.", {"command": {"type": "string"}}, ["command"]),
            _tool_definition("list_processes", "List background processes for this workspace.", {}),
            _tool_definition("process_output", "Read recent output and status from a background process.", {"process_id": {"type": "string"}, "lines": {"type": "integer"}}, ["process_id"]),
            _tool_definition("stop_process", "Stop a background process.", {"process_id": {"type": "string"}}, ["process_id"]),
        ])

    manager = _mcp_manager_override.get()
    if manager is not None:
        tools.extend(manager.tool_definitions())

    return tools


def get_tool_registry() -> Dict[str, Callable[..., str]]:
    registry: Dict[str, Callable[..., str]] = {
        "discover_tools": discover_tools,
        "list_skills": list_skills,
        "read_skill": read_skill,
        "read_skill_resource": read_skill_resource,
        "read_tool_result": read_tool_result,
        "read_file": read_file,
        "read_file_lines": read_file_lines,
        "parallel_read_files": parallel_read_files,
        "write_to_file": write_to_file,
        "search_files": search_files,
        "glob_search": glob_search,
        "list_project_structure": list_project_structure,
        "edit_file": edit_file,
        "replace_file_lines": replace_file_lines,
        "create_directory": create_directory,
        "delete_file": delete_file,
        "move_file": move_file,
        "git_status": git_status,
        "git_diff": git_diff,
        "git_log": git_log,
        "git_show": git_show,
        "git_blame": git_blame,
        "list_symbols": list_symbols,
        "list_workspace_checkpoints": list_workspace_checkpoints,
        "undo_last_checkpoint": undo_last_checkpoint,
        "list_turn_checkpoints_tool": list_turn_checkpoints_tool,
        "undo_turn_checkpoint": undo_turn_checkpoint,
        "repository_map": repository_map,
        "find_symbol": find_symbol,
        "go_to_definition": go_to_definition,
        "find_references": find_references,
        "lsp_go_to_definition": lsp_go_to_definition,
        "lsp_find_references": lsp_find_references,
        "code_diagnostics": code_diagnostics,
        "verify_project": verify_project,
        "replace_symbol": replace_symbol,
        "read_project_memory": read_project_memory,
        "remember_project_note": remember_project_note,
    }

    # Register local workspace plugins
    local_plugins = _load_local_plugins()
    for tool_def, run_func in local_plugins:
        name = tool_def["function"]["name"]
        registry[name] = run_func

    if _bash_enabled():
        registry["run_bash_command"] = run_bash_command
        registry.update({
            "start_process": start_process,
            "list_processes": list_processes,
            "process_output": process_output,
            "stop_process": stop_process,
        })
    return registry


async def execute_tool_async(tool_name: str, arguments: Dict[str, Any]) -> str:
    """Dispatch a tool call, routing MCP tools to their server.

    Built-in tools stay synchronous; only remote calls need to await.
    """
    if tool_name == "request_critic":
        from apsara_cli.engine.critic import request_critique, CRITIC_MAX_COMPLETION_TOKENS
        content, usage = await request_critique(
            _workspace_root(),
            objective=str(arguments.get("objective") or "Review the current change"),
            focus=str(arguments.get("focus") or ""),
            model=_current_model(),
            changed_files=[str(item) for item in arguments.get("_changed_files", [])],
            verification=arguments.get("_verification"),
            review_key=arguments.get("_review_key"),
        )
        usage = dict(usage or {})
        if usage.pop("request_skipped", False):
            return content
        usage_reported = bool(usage.get("total_tokens") or usage.get("prompt_tokens")
                              or usage.get("completion_tokens") or usage.get("input_tokens")
                              or usage.get("output_tokens"))
        usage["apsara_model"] = _current_model()
        usage.setdefault("auxiliary_calls", 1)
        usage.setdefault("provider_reported_calls", 1 if usage_reported else 0)
        usage.setdefault("unreported_calls", 0 if usage_reported else 1)
        usage.setdefault("completion_reserve", CRITIC_MAX_COMPLETION_TOKENS)
        _record_auxiliary_usage(usage)
        return content

    manager = _mcp_manager_override.get()
    if manager is not None and manager.has_tool(tool_name):
        remote_name = tool_name.rsplit("__", 1)[-1].lower()
        read_prefixes = ("get", "list", "read", "search", "find", "lookup", "fetch", "status", "describe")
        described = manager.describe_tool(tool_name) if hasattr(manager, "describe_tool") else None
        annotated_read_only = getattr(described, "read_only", None)
        is_read = annotated_read_only is True or (
            annotated_read_only is None and remote_name.startswith(read_prefixes)
        )
        if _read_only() and not is_read:
            return f"Error: MCP tool '{tool_name}' is not allowed in read-only mode."
        if not is_read and not _confirm_action(
            "mcp_tool_call",
            {"tool": tool_name, "arguments": arguments, "risk": "external"},
        ):
            return f"Error: MCP tool '{tool_name}' was not approved."
        return await manager.call(tool_name, arguments)
    if tool_name in {"verify_project", "run_bash_command"}:
        from apsara_cli.engine.cancellation import run_interruptible
        return await run_interruptible(execute_tool, tool_name, arguments)
    return execute_tool(tool_name, arguments)


# NOTE: there used to be a module-level `AGENT_TOOLS = get_agent_tools()` here.
# It was unused, and building the tool list at import time loaded and executed
# workspace plugins before --workspace had been resolved — so the trust gate ran
# against the process's startup directory and printed to stdout during import.
# Call get_agent_tools() inside a runtime context instead.


def execute_tool(tool_name: str, arguments: Dict[str, Any]) -> str:
    registry = get_tool_registry()
    if tool_name not in registry:
        return f"Error: Tool '{tool_name}' not found."

    try:
        func = registry[tool_name]
        return func(**arguments)
    except Exception as exc:
        return _format_exception("Error executing internal tool", exc)
