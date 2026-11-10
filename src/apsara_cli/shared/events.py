import json
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from apsara_cli.shared.ui import ConsoleUI

from apsara_cli.shared.text import is_tool_error, truncate_text


def _error_suggestion(message: str) -> Optional[str]:
    """Return a short actionable hint for a known error pattern, or None.

    Ordering matters: API-key problems are checked before network patterns,
    because provider errors often arrive wrapped in a generic 'LLM Connection
    Error' prefix that would otherwise match the network hint.
    """
    m = message.lower()
    provider = _error_provider(message)
    key_target = provider or "<provider>"
    if ("invalid api key" in m or "invalid_api_key" in m or "incorrect api key" in m
            or "unauthorized" in m or "401" in m or "authentication" in m or "auth_error" in m):
        return f"Run /key set {key_target} to update your key, or /models to switch models."
    if "rate limit" in m or "429" in m or "too many requests" in m or "ratelimit" in m:
        return "Wait a moment and retry, or switch models with /model <name>."
    if ("context" in m or "token" in m) and ("length" in m or "too long" in m or "maximum" in m or "exceed" in m):
        return "Use /clear to reset history, or /status to check context size."
    if "not found" in m or "404" in m or ("model" in m and ("exist" in m or "unknown" in m)):
        return "Verify the model name with /model, or run `apsara doctor --live`."
    if "permission" in m or "403" in m or "forbidden" in m:
        return "Your API key may lack access to this model. Run `apsara doctor --live`."
    if ("timed out" in m or "timeout" in m or "network" in m or "resolve" in m
            or "connection refused" in m or "econn" in m or "unreachable" in m):
        return "Check your internet connection and try again."
    return "Run `apsara doctor --live` for full diagnostics."


def _error_provider(message: str) -> str:
    """Best-effort provider name from e.g. 'GroqException' in the message."""
    import re
    match = re.search(r"([A-Z][a-zA-Z]+?)Exception", message)
    return match.group(1).lower() if match else ""


def _summarize_error(message: str) -> tuple[str, str]:
    """
    Distill raw LiteLLM noise into (title, detail).

    'litellm.BadRequestError: GroqException - {"error":{"message":"Invalid
    API Key","code":"invalid_api_key"}}' becomes
    ('Invalid API Key', 'Groq · invalid_api_key').
    """
    import re
    msg = message.strip()
    first_line = next((ln.strip() for ln in msg.splitlines() if ln.strip()), msg)

    # Pull the human-readable message out of an embedded provider JSON blob.
    title = ""
    code = ""
    blob = re.search(r"\{.*\}", msg, re.DOTALL)
    if blob:
        try:
            payload = json.loads(blob.group(0))
            err = payload.get("error", payload)
            if isinstance(err, dict):
                title = str(err.get("message") or "").strip()
                code = str(err.get("code") or err.get("type") or "").strip()
        except Exception:
            pass

    provider = _error_provider(msg)
    if title:
        detail_bits = [b for b in (provider.capitalize() if provider else "", code) if b]
        return title, " · ".join(detail_bits)

    # No JSON payload — clean up the wrapper prefixes and use the first line
    # as the title; any remaining lines become the detail.
    cleaned = re.sub(r"^(llm connection error:\s*)?", "", first_line, flags=re.IGNORECASE)
    cleaned = re.sub(r"^litellm\.\w+:\s*", "", cleaned)
    cleaned = cleaned.strip() or "Something went wrong"
    rest = " ".join(ln.strip() for ln in msg.splitlines()[1:] if ln.strip())
    if len(cleaned) > 90:
        return cleaned[:87] + "…", (first_line + (" " + rest if rest else ""))
    return cleaned, rest


def _tool_spinner_label(tool_name: str, arguments: dict[str, Any]) -> str:
    """Return a short human-readable label for what a tool is doing."""
    n = tool_name.lower()

    def _first(*keys: str) -> str:
        for k in keys:
            v = arguments.get(k)
            if v and isinstance(v, str):
                display = v if len(v) <= 40 else v[:37] + "…"
                return f'"{display}"'
        return ""

    if n in {"read_file", "read_file_lines"}:
        return f"reading {_first('path', 'file_path')}"
    if n == "glob_search":
        return f"searching {_first('pattern')}"
    if n == "create_directory":
        return f"creating dir {_first('path', 'directory')}"
    if n == "delete_file":
        return f"deleting {_first('path')}"
    if n == "move_file":
        return f"moving {_first('src')} → {_first('dest')}"
    if n == "write_to_file":
        return f"writing {_first('path', 'file_path')}"
    if n in {"edit_file", "replace_file_lines"}:
        return f"editing {_first('path', 'file_path')}"
    if n in {"search_in_file", "search_codebase", "grep_search"}:
        return f"searching {_first('query', 'pattern', 'search_term')}"
    if n == "run_bash_command":
        cmd = arguments.get("command", "")
        short = cmd if len(cmd) <= 36 else cmd[:33] + "…"
        return f"running  {short}"
    if n == "list_project_structure":
        return "scanning workspace"
    if n == "create_directory":
        return f"creating dir {_first('path', 'directory')}"
    if n == "delete_file":
        return f"deleting {_first('path')}"
    if n == "move_file":
        return f"moving file"
    return f"calling {tool_name}"


def _tool_result_summary(tool_name: str, result: str) -> tuple[bool, str]:
    """Return (success, short summary) for a tool result."""
    is_error = is_tool_error(result)
    if is_error:
        short = result.split("\n")[0][:60]
        return False, short

    lines = result.splitlines()
    line_count = len(lines)
    char_count = len(result)

    n = tool_name.lower()
    if n in {"read_file", "read_file_lines"}:
        return True, f"{line_count} line{'s' if line_count != 1 else ''} read"
    if n == "glob_search":
        return True, f"{line_count} match{'es' if line_count != 1 else ''}"
    if n == "create_directory":
        return True, "directory created"
    if n == "delete_file":
        return True, "file deleted"
    if n == "move_file":
        return True, "file moved"
    if n == "write_to_file":
        return True, f"written  ({char_count} chars)"
    if n == "edit_file":
        return True, "file edited"
    if n == "replace_file_lines":
        return True, "lines replaced"
    if n in {"search_in_file", "search_codebase", "grep_search"}:
        return True, f"{line_count} result{'s' if line_count != 1 else ''}"
    if n == "run_bash_command":
        return True, f"exit ok  ({line_count} lines output)"
    if n == "list_project_structure":
        return True, f"{line_count} paths"
    return True, "done"


def print_event(event: dict[str, Any], ui: "ConsoleUI") -> None:
    event_type = event.get("type")
    ui.on_agent_event(event)

    if event_type == "budget":
        budget = dict(event.get("data") or {})
        ui._run_budget = budget
        fractions = [
            budget.get("steps_used", 0) / max(1, budget.get("step_limit", 1)),
            budget.get("tool_calls_used", 0) / max(1, budget.get("tool_call_limit", 1)),
            (budget.get("reported_usage", 0) + budget.get("estimated_usage", 0))
            / max(1, budget.get("usage_limit", 1)),
        ]
        if max(fractions) >= .8 and not ui._budget_warning_shown:
            ui._budget_warning_shown = True
            ui.warning("This turn is approaching its budget. Use /budget to inspect usage and limits.")
        if hasattr(ui, "_invalidate"):
            ui._invalidate()
        return

    if event_type == "plan":
        steps = event.get("steps") or []
        titles = [str(step.get("title", "")) for step in steps if isinstance(step, dict)]
        ui.hide_event("plan", f"Plan · {len(titles)} steps", "\n".join(titles))
        return

    if event_type == "run_state":
        state = str(event.get("state", ""))
        ui.last_run_state = state
        if state == "completed_verified":
            ui.success("Verified completion — " + str(event.get("reason") or "current checks passed."))
        elif state == "completed_unverified":
            ui.warning("Unverified completion — changes require independent checks.")
        if state == "verifying":
            ui.update_spinner_action("Makor is verifying")
            ui.work_notice_shown = False
            ui.note_working("Makor is verifying")
        ui.hide_event("state", f"Run state: {state}", str(event.get("objective", "")))
        return

    if event_type == "retry_notice":
        delay = event.get("delay", 5)
        attempt = event.get("attempt", 1)
        ui.stop_spinner()
        ui.warning(f"Rate limited — retrying in {delay}s (attempt {attempt}/3)...")
        return

    if event_type == "status":
        message = str(event.get("message", "")).strip() or "Makor is thinking"
        normalized = "Makor is thinking" if "thinking" in message.lower() else "Makor is working"
        ui.note_working(normalized)
        ui.hide_event("status", message, message)
        return

    if event_type == "warning":
        ui.warning(str(event.get("message", "")).strip() or "Warning")
        return

    if event_type == "assistant_dispatch":
        content = str(event.get("content") or "").strip()
        tool_calls = event.get("tool_calls", [])
        detail_parts: list[str] = []
        if content:
            detail_parts.append(content)
        if tool_calls:
            tool_names = ", ".join(
                str(tc.get("function", {}).get("name", "unknown_tool")) for tc in tool_calls
            )
            detail_parts.append(f"Tool calls: {tool_names}")
        ui.note_working()
        ui.hide_event(
            "thinking",
            f"Apsara planned {len(tool_calls)} tool call(s)" if tool_calls else "Apsara drafted an internal step",
            "\n\n".join(part for part in detail_parts if part),
        )
        return

    if event_type == "tool_call":
        tool_name = str(event.get("name", "unknown_tool"))
        args = event.get("arguments", {})
        if not isinstance(args, dict):
            args = {}

        # Update spinner to show which tool is running
        label = _tool_spinner_label(tool_name, args)
        ui.update_spinner_action(label)
        ui.note_working(label)

        ui.hide_event(
            "tool",
            f"Tool call: {tool_name}",
            json.dumps(args, ensure_ascii=True, indent=2),
        )
        return

    if event_type == "tool_result":
        tool_name = str(event.get("name", "unknown_tool"))
        result = str(event.get("result", ""))
        success, summary = _tool_result_summary(tool_name, result)

        # Show compact inline indicator: one '✓ tool_name  summary' line
        ui.stop_spinner()
        ui.tool_result_activity(tool_name, success, summary)

        # Resume spinner for next step
        ui.update_spinner_action("Makor is working")
        ui.note_working()
        # Reset work_notice_shown so spinner restarts fresh
        ui.work_notice_shown = False
        ui.start_spinner("Makor is working")

        ui.hide_event(
            "result",
            f"Tool result: {tool_name}",
            truncate_text(result, max_lines=20, max_chars=1800),
        )
        return

    if event_type == "response_start":
        ui.stream_text_start()
        return

    if event_type == "text_chunk":
        ui.stream_text_chunk(str(event.get("content", "")))
        return

    if event_type == "response_end":
        ui.stream_text_end()
        return

    if event_type == "final_answer":
        ui.stop_spinner()
        ui.assistant(str(event.get("content", "")))
        return

    if event_type == "blocked":
        ui.last_run_state = "blocked"
        ui.blocked(str(event.get("message", "")))
        ui.set_turn_outcome("blocked")
        return

    if event_type == "error":
        ui.last_run_state = "failed"
        error_msg = str(event.get("message", ""))
        if event.get("auth_error"):
            provider = _error_provider(error_msg)
            ui.error_panel(
                "Authentication failed",
                "Your API key may be missing or invalid.",
                f"Run /key set {provider or '<provider>'} to update it, "
                "or `apsara doctor --live` to diagnose.",
            )
        else:
            title, detail = _summarize_error(error_msg)
            ui.error_panel(title, detail, _error_suggestion(error_msg) or "")
        ui.set_turn_outcome("error")
        return
