import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from io import StringIO
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

try:
    import termios
    import tty
    import select as _select
except ImportError:
    termios = None
    tty = None
    _select = None

try:
    import msvcrt
except ImportError:
    msvcrt = None

from apsara_cli.shared.text import format_rich_text_lines, truncate_text
from apsara_cli.shared.mascot import Makor, ascii_required


# ── Terminal helpers ──────────────────────────────────────────────────────────

def terminal_width(default: int = 96) -> int:
    return shutil.get_terminal_size((default, 24)).columns


def default_use_color() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


# ── Theme ─────────────────────────────────────────────────────────────────────

@dataclass
class Theme:
    """Named color roles for the entire CLI.  Override any field to re-theme."""

    body: str = "38;2;240;236;231"
    muted: str = "2;38;2;160;166;178"
    dim: str = "38;2;140;130;118"
    heading: str = "1;38;2;250;216;143"
    accent: str = "1;38;2;96;150;250"

    success: str = "38;2;120;200;150"
    info_text: str = "38;2;188;218;255"
    warning_text: str = "38;2;247;223;181"
    error_text: str = "38;2;255;205;205"
    blocked_text: str = "38;2;247;200;100"

    info_bg: str = "48;2;73;127;221"
    ok_bg: str = "48;2;61;153;117"
    warn_bg: str = "48;2;239;167;74"
    error_bg: str = "48;2;191;87;84"
    status_bg: str = "48;2;242;201;76"
    blocked_bg: str = "48;2;200;120;40"
    spinner_bg: str = "48;2;133;92;219"
    muted_bg: str = "48;2;70;85;115"

    assistant_label: str = "2;38;2;133;92;219"
    user_label: str = "2;38;2;96;150;250"

    turn_separator: str = "2;38;2;130;125;140"
    border: str = "38;2;80;100;140"

    palette_thinking: list[str] = field(default_factory=lambda: [
        "38;2;111;154;255", "38;2;128;168;255", "38;2;144;182;255",
        "38;2;158;174;248", "38;2;175;154;244", "38;2;197;154;244",
        "38;2;160;154;255", "38;2;132;182;255", "38;2;122;210;222",
        "38;2;140;200;220",
    ])
    palette_executing: list[str] = field(default_factory=lambda: [
        "38;2;238;206;124", "38;2;248;216;130", "38;2;255;196;108",
        "38;2;255;185;100", "38;2;242;201;76",  "38;2;248;210;100",
        "38;2;255;220;120", "38;2;248;200;90",  "38;2;236;190;80",
        "38;2;244;206;110",
    ])
    palette_writing: list[str] = field(default_factory=lambda: [
        "38;2;121;210;184", "38;2;130;220;190", "38;2;140;224;180",
        "38;2;150;216;168", "38;2;166;216;168", "38;2;140;200;140",
        "38;2;120;210;160", "38;2;110;200;180", "38;2;130;218;186",
        "38;2;142;222;174",
    ])

    content_width: int = 84

    def spinner_palette(self, message: str) -> list[str]:
        m = message.lower()
        if any(w in m for w in ("writ", "creat", "updat", "file", "saving")):
            return self.palette_writing
        if any(w in m for w in ("run", "execut", "command", "bash", "scan")):
            return self.palette_executing
        return self.palette_thinking


DEFAULT_THEME = Theme()

# Shared dark surface used by transcript cards and the full-screen TUI's
# composer/sidebar. Keeping both representations here prevents the three
# boxed surfaces from drifting apart again.
PANEL_BACKGROUND_HEX = "#191d2b"
PANEL_BACKGROUND_ANSI = "48;2;25;29;43"


# Blanket approval is intentionally allowlisted to reversible workspace
# changes. Unknown and newly added actions therefore require an explicit
# decision until they have been reviewed here.
_BLANKET_APPROVAL_ACTIONS = frozenset({
    "create_directory",
    "write_to_file",
    "edit_file",
    "replace_file_lines",
    "delete_file",
    "move_file",
    "replace_symbol",
    "remember_project_note",
    "undo_checkpoint",
    "undo_turn",
})


def action_allows_blanket_approval(action: str) -> bool:
    return action in _BLANKET_APPROVAL_ACTIONS


_BRAILLE = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

# OpenCode-style accents shared across the renderer.
_THOUGHT = "38;2;240;170;90"     # orange "+ Thought:" marker / Tip dot
_BRIGHT = "38;2;225;230;242"     # near-white emphasis text


def _fmt_elapsed(seconds: float) -> str:
    if seconds < 1:
        return f"{int(seconds * 1000)}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{int(seconds // 60)}m{int(seconds % 60):02d}s"


# ── Action description helpers ────────────────────────────────────────────────

def describe_action(
    action: str, payload: dict[str, Any]
) -> tuple[str, Optional[str], Optional[str], Optional[str], Optional[str], Optional[str]]:
    if action == "write_to_file":
        path = payload.get("display_path") or payload.get("path", "<unknown>")
        preview = payload.get("content_preview")
        title = f"Create file {path}" if payload.get("is_new_file") else f"Update file {path}"
        return (
            title,
            preview if isinstance(preview, str) else None,
            payload.get("diff_preview") if isinstance(payload.get("diff_preview"), str) else None,
            payload.get("diff_full") if isinstance(payload.get("diff_full"), str) else None,
            payload.get("diff_editor") if isinstance(payload.get("diff_editor"), str) else None,
            path,
        )

    if action == "create_directory":
        path = payload.get("display_path") or payload.get("path", "<unknown>")
        return (f"Create directory {path}", None, None, None, None, path)

    if action == "edit_file":
        path = payload.get("display_path") or payload.get("path", "<unknown>")
        preview = payload.get("replacement_preview")
        if payload.get("replace_all"):
            count = payload.get("occurrences", "?")
            title = f"Edit {path} ({count} occurrences)"
        else:
            title = f"Edit {path}"
        return (
            title,
            preview if isinstance(preview, str) else None,
            payload.get("diff_preview") if isinstance(payload.get("diff_preview"), str) else None,
            payload.get("diff_full") if isinstance(payload.get("diff_full"), str) else None,
            payload.get("diff_editor") if isinstance(payload.get("diff_editor"), str) else None,
            path,
        )

    if action == "replace_file_lines":
        path = payload.get("display_path") or payload.get("path", "<unknown>")
        preview = payload.get("replacement_preview")
        return (
            f"Replace lines {payload.get('start_line', '?')}-{payload.get('end_line', '?')} in {path}",
            preview if isinstance(preview, str) else None,
            payload.get("diff_preview") if isinstance(payload.get("diff_preview"), str) else None,
            payload.get("diff_full") if isinstance(payload.get("diff_full"), str) else None,
            payload.get("diff_editor") if isinstance(payload.get("diff_editor"), str) else None,
            path,
        )

    if action == "delete_file":
        path = payload.get("display_path") or payload.get("path", "<unknown>")
        preview = payload.get("content_preview")
        return (
            f"Delete file {path}",
            preview if isinstance(preview, str) else None,
            None, None, None, path,
        )

    if action == "move_file":
        src = payload.get("display_path") or payload.get("path", "<unknown>")
        dest = payload.get("display_dest") or payload.get("dest_path", "<unknown>")
        overwrites = payload.get("overwrites", False)
        title = f"Move {src} → {dest}" + (" (overwrites existing)" if overwrites else "")
        return (title, None, None, None, None, src)

    if action == "run_bash_command":
        command = payload.get("command", "")
        cwd = payload.get("cwd", "")
        preview = f"$ {command}"
        if cwd:
            preview += f"\n  in {cwd}"
        return ("Run command", preview, None, None, None, None)

    if action == "start_process":
        command = payload.get("command", "")
        return ("Start background process", f"$ {command}", None, None, None, None)

    if action == "stop_process":
        return (f"Stop background process {payload.get('process_id', '')}?", None, None, None, None, None)

    if action == "undo_checkpoint":
        return (f"Restore checkpoint {payload.get('checkpoint_id', 'latest')}?", None, None, None, None, None)

    if action == "undo_turn":
        if payload.get("force"):
            return (f"Force rollback of agent turn {payload.get('turn_id', 'latest')}?",
                    "This overwrites conflicting edits made after the turn. Review /diff first.", None, None, None, None)
        return (f"Roll back agent turn {payload.get('turn_id', 'latest')}?", None, None, None, None, None)

    if action == "export_diagnostic_content":
        count = payload.get("message_count", 0)
        return (
            "Include conversation and tool content in diagnostics?",
            f"This bundle will include content from {count} message(s). "
            "Recognizable credentials are still redacted, but you must review every file before sharing.",
            None,
            None,
            None,
            None,
        )

    if action == "remember_project_note":
        return ("Save this note to project memory?", str(payload.get("note", "")), None, None, None, None)

    if action == "mcp_tool_call":
        import json
        arguments = json.dumps(payload.get("arguments", {}), ensure_ascii=False, indent=2)
        return (f"Allow external MCP action {payload.get('tool', '')}?", arguments, None, None, None, None)

    if action == "trust_workspace_code":
        path = payload.get("display_path") or payload.get("path", "<unknown>")
        if payload.get("kind") == "mcp":
            title = (
                f"Trust MCP server '{payload.get('server', path)}' from this project? "
                "It will be launched as a subprocess."
            )
        else:
            lines = payload.get("line_count")
            suffix = f" ({lines} lines)" if isinstance(lines, int) else ""
            title = (
                f"Trust and execute local plugin {path}{suffix}? "
                "This code comes from the project, not from you."
            )
        preview = payload.get("source_preview") or payload.get("command_preview")
        return (
            title,
            preview if isinstance(preview, str) else None,
            None, None, None, path,
        )

    return (f"Approve action: {action}", None, None, None, None, None)


# ── ConsoleUI ─────────────────────────────────────────────────────────────────

class ConsoleUI:
    def __init__(
        self,
        use_color: bool,
        auto_approve: bool = False,
        typing_delay: float = 0.008,
        theme: Optional[Theme] = None,
    ):
        self.use_color = use_color
        self.auto_approve = auto_approve
        self.approve_all = auto_approve
        self.typing_delay = typing_delay if sys.stdout.isatty() and not os.environ.get("CI") else 0.0
        self.theme = theme or DEFAULT_THEME
        self.mascot = Makor()

        self.hidden_events: list[Any] = []
        self.latest_hidden_events: list[Any] = []
        self.current_turn_hidden_events: list[Any] = []
        self.work_notice_shown = False

        self._turn_outcome: str = ""
        self._thought_pending: bool = False
        self._turn_started_at: float = time.monotonic()

        self.log_file: Optional[Path] = None
        self._logging_attempted: bool = False

        self._session_prompt_tokens: int = 0
        self._session_completion_tokens: int = 0
        self._session_total_tokens: int = 0
        self._session_cached_tokens: int = 0
        self._session_cache_creation_tokens: int = 0
        self._session_reasoning_tokens: int = 0
        self._session_estimated_tokens: int = 0
        self._session_provider_reported_calls: int = 0
        self._session_unreported_calls: int = 0
        self._session_interrupted_calls: int = 0
        self._session_auxiliary_calls: int = 0
        self._session_model_usage: dict[str, dict[str, Any]] = {}
        self._latest_rate_limits: dict[str, Any] = {}
        self._session_cost_usd: float = 0.0
        self._session_has_unpriced_usage: bool = False
        self._session_uses_list_pricing: bool = False
        self._session_uses_promotional_pricing: bool = False
        self._context_tokens: int = 0
        self._context_budget: int = 0
        self._run_budget: dict[str, Any] = {}
        self._budget_warning_shown = False

        self.spinner_message = "Makor is working"
        self.spinner_stop_event = threading.Event()
        self.spinner_thread: Optional[threading.Thread] = None
        self.spinner_lock = threading.Lock()
        self._spinner_start_time: float = 0.0
        self._spinner_frame_index: int = 0
        self._spinner_color_index: int = 0
        self._spinner_rows: int = 0
        self._stream_buffer: list[str] = []

    def _mascot_changed(self) -> None:
        """TUI subclasses repaint without writing into the transcript."""

    def set_mascot_state(self, state: str) -> None:
        if self.mascot.set_state(state):
            self._mascot_changed()

    def on_agent_event(self, event: dict[str, Any]) -> None:
        if self.mascot.observe(event):
            self._mascot_changed()

    def _ensure_log_file(self) -> None:
        if self._logging_attempted:
            return
        self._logging_attempted = True
        try:
            log_dir = Path.cwd() / ".apsara" / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.log_file = log_dir / f"session_{timestamp}.log"
            with self.log_file.open("a", encoding="utf-8") as f:
                f.write(f"--- Session started at {datetime.now()} ---\n")
        except Exception:
            self.log_file = None

    def log_event(self, category: str, title: str, detail: str = "") -> None:
        self._ensure_log_file()
        if not self.log_file:
            return
        try:
            with self.log_file.open("a", encoding="utf-8") as f:
                timestamp = datetime.now().strftime("%H:%M:%S")
                f.write(f"[{timestamp}] [{category.upper()}] {title}\n")
                if detail:
                    indented = "\n".join(f"  | {line}" for line in detail.splitlines())
                    f.write(f"{indented}\n")
        except Exception:
            pass

    # ── Low-level styling ─────────────────────────────────────────────────────

    def style(self, text: str, *codes: str) -> str:
        if not self.use_color or not codes:
            return text
        return f"\033[{';'.join(codes)}m{text}\033[0m"

    def badge(self, text: str, fg_code: str = "30", bg_code: str = "47") -> str:
        if not self.use_color:
            return f"[{text.upper()}]"
        return self.style(f" {text.upper()} ", "1", fg_code, bg_code)

    def muted(self, text: str) -> str:
        return self.style(text, self.theme.muted)

    def dim(self, text: str) -> str:
        return self.style(text, self.theme.dim)

    # ── Output primitives ─────────────────────────────────────────────────────

    def print_line(self, text: str = "") -> None:
        self.stop_spinner()
        print(text)

    def print_block(self, text: str, color_code: Optional[str] = None) -> None:
        for raw_line in text.splitlines() or [""]:
            line = f"  {raw_line}"
            self.print_line(self.style(line, color_code) if color_code else line)

    def content_width(self, fallback: Optional[int] = None) -> int:
        w = fallback or self.theme.content_width
        return max(44, min(w, terminal_width() - 8))

    def _print_typed(self, prefix: str, content: str, color_code: str, delay: float) -> None:
        self.stop_spinner()
        if not delay or not sys.stdout.isatty() or os.environ.get("CI"):
            print(f"{prefix}{self.style(content, color_code)}")
            return
        ansi_open = f"\033[{color_code}m" if color_code and self.use_color else ""
        ansi_close = "\033[0m" if color_code and self.use_color else ""
        sys.stdout.write(prefix + ansi_open)
        sys.stdout.flush()
        for char in content:
            sys.stdout.write(char)
            sys.stdout.flush()
            time.sleep(delay)
        sys.stdout.write(ansi_close + "\n")
        sys.stdout.flush()

    def redraw_block(self, prev_line_count: int, new_lines: list[str]) -> None:
        """
        Redraw a fixed block of ``prev_line_count`` previously-printed lines
        in place, replacing them with ``new_lines``. Used by interactive
        inline pickers (e.g. /models) so navigation stays inline in the
        current terminal UI instead of opening a separate full-screen view —
        the same idea as the spinner's \\r redraw, just multi-line.
        """
        self.stop_spinner()
        if prev_line_count and self.use_color and sys.stdout.isatty():
            sys.stdout.write(f"\033[{prev_line_count}A")
        for line in new_lines:
            sys.stdout.write(f"\r\033[2K{line}\n")
        sys.stdout.flush()

    def _print_box_line(self, left: str, content: str, right: str, color_code: str = "") -> None:
        self.stop_spinner()
        if color_code:
            print(f"  {self.style(left, color_code)}{content}{self.style(right, color_code)}")
        else:
            print(f"  {left}{content}{right}")

    # ── Rich text renderer ────────────────────────────────────────────────────

    def _markdown_card_lines(
        self,
        text: str,
        *,
        width: Optional[int] = None,
        timestamp: Optional[str] = None,
    ) -> list[str]:
        """Format a Markdown response in the original filled answer card."""
        from rich.console import Console
        from rich.markdown import Markdown
        from rich.text import Text

        panel_width = max(4, width or self.content_width())
        card_width = max(2, panel_width - 2)
        body_width = max(1, card_width - 1)
        horizontal_padding = min(3, max(0, (body_width - 1) // 2))
        render_width = max(1, body_width - (horizontal_padding * 2))
        rendered = StringIO()
        console = Console(
            file=rendered,
            force_terminal=self.use_color,
            color_system="truecolor" if self.use_color else None,
            width=render_width,
            # Rich 15 ignores an explicit width on a dumb terminal unless a
            # height is also supplied. Pin both so cards never spill into the
            # sidebar in captured terminals or CI.
            height=25,
            legacy_windows=False,
        )
        body = text.strip() or "_No response content._"
        console.print(Markdown(body, code_theme="monokai"))

        rail = self.style("▌", "1", "38;2;104;205;220")
        foreground = "38;2;225;230;242"
        # Terminal cells cannot use a smaller font, so keep card metadata
        # compact and faint instead of letting it compete with the message.
        timestamp = timestamp or datetime.now().strftime("%I:%M").lstrip("0")

        def card_line(content: str = "") -> str:
            visual_width = Text.from_ansi(content).cell_len
            content += " " * max(render_width - visual_width, 0)
            if self.use_color:
                content = content.replace(
                    "\033[0m", f"\033[0m\033[{PANEL_BACKGROUND_ANSI}m"
                )
            gutter = " " * horizontal_padding
            return (
                f"  {rail}"
                f"{self.style(gutter + content + gutter, foreground, PANEL_BACKGROUND_ANSI)}"
            )

        lines = [card_line(" " * render_width)]
        for line in rendered.getvalue().rstrip("\n").splitlines() or [""]:
            lines.append(card_line(line))
        lines.append(card_line(" " * render_width))
        footer = f"apsara  {timestamp}"[:render_width].ljust(render_width)
        if self.use_color:
            footer = self.style(footer, "2", "38;2;112;116;126").replace(
                "\033[0m", f"\033[0m\033[{PANEL_BACKGROUND_ANSI}m"
            )
        lines.extend([card_line(footer), card_line(" " * render_width)])
        return lines

    def render_markdown_panel(self, text: str) -> None:
        """Render an assistant response in the shared filled panel."""
        for line in self._markdown_card_lines(text):
            self.print_line(line)

    def render_rich_text(self, text: str, typing_delay: float = 0.0) -> None:
        import re as _re
        _ansi_re = _re.compile(r"\x1b\[[0-9;]*m")

        def _visual_len(s: str) -> int:
            return len(_ansi_re.sub("", s))

        def _highlight_code(src: str, lang: str) -> list[str]:
            if not lang or not self.use_color:
                return src.splitlines()
            try:
                from pygments import highlight
                from pygments.lexers import get_lexer_by_name
                from pygments.formatters import Terminal256Formatter
                lexer = get_lexer_by_name(lang, stripall=False)
                formatted = highlight(src, lexer, Terminal256Formatter(style="monokai"))
                return formatted.rstrip("\n").splitlines()
            except Exception:
                return src.splitlines()

        width = self.content_width()
        lines = format_rich_text_lines(text, width)
        in_code_block = False
        code_lines: list[str] = []
        current_lang = ""

        def flush_code(lines_buf: list[str], lang: str) -> None:
            # OpenCode-style flat block: a dim left bar, no surrounding box.
            if not lines_buf:
                return
            highlighted = _highlight_code("\n".join(lines_buf), lang)
            bc = self.theme.border
            bar = self.style("│", bc)
            self.print_line(f"  {bar} {self.dim(lang)}" if lang else f"  {bar}")
            for cl in highlighted:
                fallback = self.style(cl, "38;2;173;203;255") if not lang else cl
                print(f"  {bar} {fallback}")

        for item in lines:
            line_type = item[0]
            line = item[1] if len(item) > 1 else ""

            if line_type == "code_start":
                current_lang = line
                in_code_block = True
                continue
            if line_type == "code":
                code_lines.append(line)
                continue
            if in_code_block:
                flush_code(code_lines, current_lang)
                code_lines = []
                current_lang = ""
                in_code_block = False

            if line_type == "blank":
                self.print_line()
            elif line_type == "heading":
                self.print_line()
                self.print_line(f"  {self.style(line, self.theme.heading)}")
            elif line_type == "list":
                self._print_typed("  ", line, "38;2;220;226;240", typing_delay * 0.5)
            else:
                self._print_typed("  ", line, self.theme.body, typing_delay)

        if code_lines:
            flush_code(code_lines, current_lang)

    def render_diff_text(self, diff_text: str) -> None:
        for raw_line in diff_text.splitlines() or [""]:
            if raw_line.startswith(("---", "+++")):
                self.print_line(f"  {self.style(raw_line, '38;2;140;191;255')}")
            elif raw_line.startswith("@@"):
                self.print_line(f"  {self.style(raw_line, '38;2;250;216;143')}")
            elif raw_line.startswith("+") and not raw_line.startswith("+++"):
                self.print_line(f"  {self.style(raw_line, '38;2;152;224;171')}")
            elif raw_line.startswith("-") and not raw_line.startswith("---"):
                self.print_line(f"  {self.style(raw_line, '38;2;255;168;168')}")
            elif raw_line.startswith("... ["):
                self.print_line(f"  {self.muted(raw_line)}")
            else:
                self.print_line(f"  {self.style(raw_line, '38;2;220;225;235')}")

    def render_side_by_side_diff(self, old_text: str, new_text: str) -> None:
        import difflib
        width = self.content_width()
        half_w = (width // 2) - 4

        old_lines = old_text.splitlines()
        new_lines = new_text.splitlines()

        sm = difflib.SequenceMatcher(None, old_lines, new_lines)

        bc = self.theme.border
        header_old = self.badge("original", "15", "48;2;120;100;90")
        header_new = self.badge("proposed", "15", "48;2;100;150;110")

        sep = self.style(" │ ", bc)
        self.print_line(f"  {header_old}{' ' * (half_w - 6)}{sep}{header_new}")
        self.print_line(f"  {self.style('─' * half_w + '─┼─' + '─' * half_w, bc)}")

        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == 'equal':
                for i, j in zip(range(i1, i2), range(j1, j2)):
                    left = old_lines[i][:half_w].ljust(half_w)
                    right = new_lines[j][:half_w].ljust(half_w)
                    print(f"  {self.dim(left)}{sep}{self.dim(right)}")
            elif tag == 'replace':
                max_len = max(i2 - i1, j2 - j1)
                for k in range(max_len):
                    left_idx = i1 + k
                    right_idx = j1 + k
                    left_val = old_lines[left_idx] if left_idx < i2 else ""
                    right_val = new_lines[right_idx] if right_idx < j2 else ""

                    left_styled = self.style(left_val[:half_w].ljust(half_w), "38;2;255;168;168") if left_val else " " * half_w
                    right_styled = self.style(right_val[:half_w].ljust(half_w), "38;2;152;224;171") if right_val else " " * half_w
                    print(f"  {left_styled}{sep}{right_styled}")
            elif tag == 'delete':
                for i in range(i1, i2):
                    left = self.style(old_lines[i][:half_w].ljust(half_w), "38;2;255;168;168")
                    print(f"  {left}{sep}{' ' * half_w}")
            elif tag == 'insert':
                for j in range(j1, j2):
                    right = self.style(new_lines[j][:half_w].ljust(half_w), "38;2;152;224;171")
                    print(f"  {' ' * half_w}{sep}{right}")

        self.print_line(f"  {self.style('─' * half_w + '─┴─' + '─' * half_w, bc)}")

    # ── Spinner ───────────────────────────────────────────────────────────────

    def spinner_enabled(self) -> bool:
        return sys.stdout.isatty() and os.environ.get("CI") is None

    def _shimmer(self, text: str, frame_idx: int, bright: str) -> str:
        """A highlight window sweeping across ``text`` in the palette's hue."""
        if not self.use_color:
            return text
        base = "38;2;150;155;170"
        window = 5
        span = len(text) + window * 2
        pos = frame_idx % span
        out: list[str] = []
        for i, ch in enumerate(text):
            if pos - window <= i <= pos:
                out.append(self.style(ch, "1", bright))
            else:
                out.append(self.style(ch, base))
        return "".join(out)

    def compose_spinner_line(self, frame_idx: int) -> str:
        """One frame of the thinking animation: colored spinner + shimmering
        message + pulsing dots + elapsed. Shared by the classic REPL (which
        \\r-redraws it) and the TUI (which paints it into the chat pane)."""
        palette = self.theme.spinner_palette(self.spinner_message)
        frame = _BRAILLE[frame_idx % len(_BRAILLE)]
        color = palette[frame_idx % len(palette)]
        spin = self.style(frame, "1", color) if self.use_color else frame

        msg = self._shimmer(self.spinner_message, frame_idx, bright=color)
        dots = "·" * (1 + (frame_idx // 4) % 3)
        dots_styled = self.style(dots.ljust(3), "38;2;150;140;130")

        elapsed_str = ""
        if self._spinner_start_time:
            elapsed = time.monotonic() - self._spinner_start_time
            if elapsed >= 2:
                elapsed_str = self.style(f" {elapsed:.0f}s", "38;2;130;125;140")

        return f"{spin} {msg} {dots_styled}{elapsed_str}"

    def _spinner_worker(self) -> None:
        frame_idx = 0
        while not self.spinner_stop_event.is_set():
            with self.spinner_lock:
                lines = self.compose_activity_lines(frame_idx)
                sys.stdout.write("\r" + (f"\033[{self._spinner_rows - 1}A" if self._spinner_rows > 1 else ""))
                sys.stdout.write("\n".join("\033[2K" + line for line in lines))
                self._spinner_rows = len(lines)
                sys.stdout.flush()
            frame_idx += 1
            if self.spinner_stop_event.wait(0.08):
                break

    def compose_activity_lines(self, frame_idx: int = 0) -> list[str]:
        """Place the compact head directly beside the current activity text."""
        if not self.mascot.enabled:
            return [f"  {self.compose_spinner_line(frame_idx)}"]
        from rich.text import Text

        head = self.mascot.render_head(color=self.use_color, ascii_only=ascii_required())
        message = self.spinner_message
        if self.mascot.state in {"waiting", "retrying", "verifying", "speaking"}:
            message = self.mascot.label(ascii_only=True).split(" | ", 1)[1]
        elapsed = time.monotonic() - self._spinner_start_time if self._spinner_start_time else 0
        text = Text(message, style="rgb(190,195,210)" if self.use_color else None)
        if elapsed >= 2 and self.mascot.state not in {"waiting", "retrying"}:
            text.append(f" {elapsed:.0f}s", style="rgb(130,125,140)" if self.use_color else None)
        text.truncate(max(1, self.content_width() - 11), overflow="ellipsis")
        if ascii_required():
            text = Text(text.plain.replace("…", "."))
        # Text aligns with the face on the lower of the two small icon rows.
        label = self.style(text.plain, "38;2;190;195;210")
        return ["  " + line + ("  " + label if index == 1 else "")
                for index, line in enumerate(head)]

    def start_spinner(self, message: str) -> None:
        self.spinner_message = message
        if not self.spinner_enabled():
            self.print_notice("work", f"{message}...", "17", "48;2;242;201;76", "38;2;236;220;184")
            return
        if self.spinner_thread and self.spinner_thread.is_alive():
            return
        self._spinner_start_time = time.monotonic()
        self._spinner_rows = 0
        self.spinner_stop_event.clear()
        self.spinner_thread = threading.Thread(target=self._spinner_worker, daemon=True)
        self.spinner_thread.start()

    def stop_spinner(self) -> None:
        if not self.spinner_thread:
            return
        self.spinner_stop_event.set()
        self.spinner_thread.join(timeout=0.3)
        with self.spinner_lock:
            sys.stdout.write("\r" + (f"\033[{self._spinner_rows - 1}A" if self._spinner_rows > 1 else ""))
            sys.stdout.write("\n".join("\033[2K" for _ in range(max(1, self._spinner_rows))))
            if self._spinner_rows > 1:
                sys.stdout.write(f"\033[{self._spinner_rows - 1}A")
            sys.stdout.write("\r")
            sys.stdout.flush()
        self._spinner_rows = 0
        self.spinner_thread = None

    def update_spinner_action(self, action: str) -> None:
        with self.spinner_lock:
            self.spinner_message = action

    def note_working(self, message: str = "Makor is working") -> None:
        if self.work_notice_shown:
            return
        self.start_spinner(message)
        self.work_notice_shown = True

    # ── Turn structure ────────────────────────────────────────────────────────

    def print_turn_separator(self, turn: int = 0) -> None:
        # OpenCode stacks turns with whitespace only — no ruled separators.
        self.stop_spinner()
        print()

    def print_rule(self, text: str = "") -> None:
        w = min(terminal_width(), 88)
        if text:
            side = max((w - len(text) - 6) // 2, 2)
            line = "─" * side + f"  {text}  " + "─" * side
        else:
            line = "─" * (w - 4)
        print(f"  {self.style(line, self.theme.turn_separator)}")

    # ── Notification methods ──────────────────────────────────────────────────

    _NOTICE_ICONS = {
        "info": "●", "status": "●", "work": "●", "tip": "●",
        "ok": "✓", "warn": "▲", "error": "✗", "blocked": "■",
        "diff": "●", "editor": "●", "get started": "●",
    }
    # Routine notices show just the icon; only attention-worthy ones keep a label.
    _NOTICE_LABELS = {"tip": "Tip", "warn": "Warning", "error": "Error", "blocked": "Blocked"}

    def print_notice(self, label: str, text: str, fg_code: str, bg_code: str, body_color: str) -> None:
        # OpenCode-style minimal notice: colored dot + optional bold label +
        # body, no background badge. The badge bg color becomes the icon color.
        key = label.lower()
        icon_color = bg_code.replace("48;", "38;", 1) if bg_code.startswith("48;") else bg_code
        icon = self.style(self._NOTICE_ICONS.get(key, "●"), "1", icon_color)
        label_word = self._NOTICE_LABELS.get(key)
        label_part = f"{self.style(label_word, '1', icon_color)} " if label_word else ""
        self.print_line(f"  {icon} {label_part}{self.style(text, body_color)}")

    def status(self, text: str) -> None:
        self.print_notice("status", text, "17", self.theme.status_bg, "38;2;236;220;184")

    def info(self, text: str) -> None:
        self.print_notice("info", text, "15", self.theme.info_bg, self.theme.info_text)

    def success(self, text: str) -> None:
        self.print_notice("ok", text, "15", self.theme.ok_bg, "38;2;186;239;203")

    def warning(self, text: str) -> None:
        self.print_notice("warn", text, "17", self.theme.warn_bg, self.theme.warning_text)

    def error(self, text: str) -> None:
        self.print_notice("error", text, "15", self.theme.error_bg, self.theme.error_text)

    def error_panel(self, title: str, detail: str = "", tip: str = "") -> None:
        """
        Rich error block with a red left-accent bar, OpenCode panel style:

          ▌ ✗ Error  Invalid API Key
          ▌ Groq rejected the request · code invalid_api_key
          ▌
          ▌ ● Tip Run /key set groq to update your key
        """
        import textwrap as _tw
        self.stop_spinner()
        bar = self.style("▌", "1", "38;2;235;110;100")
        wrap_w = min(self.content_width(), 72)

        self.print_line()
        self.print_line(
            f"  {bar} {self.style('✗ Error', '1', '38;2;235;110;100')}  "
            f"{self.style(title, '1', '38;2;255;218;214')}"
        )
        if detail:
            wrapped: list[str] = []
            for raw in detail.splitlines():
                wrapped.extend(_tw.wrap(raw, width=wrap_w) or [""])
            for line in wrapped[:6]:
                self.print_line(f"  {bar} {self.style(line, '38;2;220;170;168')}")
        if tip:
            self.print_line(f"  {bar}")
            self.print_line(
                f"  {bar} {self.style('●', '38;2;240;170;90')} "
                f"{self.style('Tip', '1', '38;2;240;170;90')} "
                f"{self.style(tip, '38;2;228;214;196')}"
            )

    def blocked(self, text: str) -> None:
        self.print_line()
        self.print_notice("blocked", text, "17", self.theme.blocked_bg, self.theme.blocked_text)

    # ── Input bar ────────────────────────────────────────────────────────────

    def prompt(self, label: str = "you", hint: str = "") -> str:
        # Bare accent bar, matching OpenCode's input box left border.
        bar = self.style("▌", self.theme.accent)
        return f"\n{bar} "

    def session_saved(self, session_path: Path) -> None:
        # Saving is routine and visible through /sessions; keep normal turns
        # focused on the conversation.
        return

    def calculate_session_cost(self) -> Optional[float]:
        """Known session cost, or None if any provider pricing is unknown."""
        if self._session_has_unpriced_usage:
            return None
        return self._session_cost_usd

    def session_cost_label(self) -> str:
        cost = self.calculate_session_cost()
        if cost is None:
            return "provider billed"
        if self._session_uses_promotional_pricing and self._session_uses_list_pricing:
            suffix = " mixed"
        elif self._session_uses_promotional_pricing:
            suffix = " promo"
        else:
            suffix = " list" if self._session_uses_list_pricing else ""
        return f"${cost:.4f}{suffix}"

    def set_context_usage(self, tokens: int, budget: int) -> None:
        self._context_tokens = max(0, int(tokens))
        self._context_budget = max(0, int(budget))

    def usage_snapshot(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self._session_prompt_tokens,
            "completion_tokens": self._session_completion_tokens,
            "total_tokens": self._session_total_tokens,
            "cached_tokens": self._session_cached_tokens,
            "cache_creation_tokens": self._session_cache_creation_tokens,
            "reasoning_tokens": self._session_reasoning_tokens,
            "estimated_input_tokens": self._session_estimated_tokens,
            "provider_reported_calls": self._session_provider_reported_calls,
            "unreported_calls": self._session_unreported_calls,
            "interrupted_calls": self._session_interrupted_calls,
            "auxiliary_calls": self._session_auxiliary_calls,
            "model_usage": self._session_model_usage,
            "rate_limits": self._latest_rate_limits,
        }

    def restore_usage(self, usage_data: dict[str, Any]) -> None:
        """Restore persisted counters without printing another turn summary."""
        from apsara_cli.engine.usage import normalize_usage

        data = normalize_usage(usage_data)
        self._session_prompt_tokens = data["prompt_tokens"]
        self._session_completion_tokens = data["completion_tokens"]
        self._session_total_tokens = data["total_tokens"]
        self._session_cached_tokens = data["cached_tokens"]
        self._session_cache_creation_tokens = data["cache_creation_tokens"]
        self._session_reasoning_tokens = data["reasoning_tokens"]
        self._session_estimated_tokens = data["estimated_input_tokens"]
        self._session_provider_reported_calls = data["provider_reported_calls"]
        self._session_unreported_calls = data["unreported_calls"]
        self._session_interrupted_calls = data["interrupted_calls"]
        self._session_auxiliary_calls = data["auxiliary_calls"]
        raw_models = usage_data.get("model_usage")
        self._session_model_usage = {
            str(model): normalize_usage(tokens)
            for model, tokens in raw_models.items()
            if isinstance(tokens, dict)
        } if isinstance(raw_models, dict) else {}
        self._latest_rate_limits = (
            dict(usage_data["rate_limits"])
            if isinstance(usage_data.get("rate_limits"), dict) else {}
        )
        self._recalculate_session_cost()

    def _recalculate_session_cost(self) -> None:
        from apsara_cli.engine.pricing import pricing_for_model, usage_cost

        self._session_cost_usd = 0.0
        self._session_has_unpriced_usage = False
        self._session_uses_list_pricing = False
        self._session_uses_promotional_pricing = False
        for model, tokens in self._session_model_usage.items():
            if not tokens.get("total_tokens"):
                continue
            known_cost = usage_cost(model, tokens)
            _prices, source = pricing_for_model(model)
            if source.startswith("temporary provider promotion"):
                self._session_uses_promotional_pricing = True
            elif source not in {"local model", "Apsara model registry"}:
                self._session_uses_list_pricing = True
            if known_cost is None:
                self._session_has_unpriced_usage = True
            else:
                self._session_cost_usd += known_cost

    def rate_limit_label(self) -> str:
        limits = self._latest_rate_limits
        parts = []
        if limits.get("remaining_requests") is not None:
            parts.append(f"{limits['remaining_requests']} requests left")
        if limits.get("remaining_tokens") is not None:
            parts.append(f"{limits['remaining_tokens']} tokens left")
        reset = limits.get("reset") or limits.get("retry_after")
        if reset:
            parts.append(f"reset {reset}")
        return " · ".join(parts)

    def usage(self, usage_data: dict[str, Any]) -> None:
        from apsara_cli.engine.usage import add_usage, normalize_usage

        normalized = normalize_usage(usage_data)
        p = normalized["prompt_tokens"]
        c = normalized["completion_tokens"]
        t = normalized["total_tokens"]
        self._session_prompt_tokens     += p
        self._session_completion_tokens += c
        self._session_total_tokens      += t
        self._session_cached_tokens += normalized["cached_tokens"]
        self._session_cache_creation_tokens += normalized["cache_creation_tokens"]
        self._session_reasoning_tokens += normalized["reasoning_tokens"]
        self._session_estimated_tokens += normalized["estimated_input_tokens"]
        self._session_provider_reported_calls += normalized["provider_reported_calls"]
        self._session_unreported_calls += normalized["unreported_calls"]
        self._session_interrupted_calls += normalized["interrupted_calls"]
        self._session_auxiliary_calls += normalized["auxiliary_calls"]
        if normalized.get("rate_limits"):
            self._latest_rate_limits = normalized["rate_limits"]

        model_usage = usage_data.get("model_usage")
        if not isinstance(model_usage, dict):
            model = usage_data.get("apsara_model")
            model_usage = {model: usage_data} if model else {}
        for model, tokens in model_usage.items():
            if not isinstance(tokens, dict):
                continue
            model_total = int(tokens.get("total_tokens") or 0)
            model_estimated = int(
                tokens.get("estimated_input_tokens") or tokens.get("estimated_tokens") or 0
            )
            if model_total <= 0 and model_estimated <= 0:
                continue
            target = self._session_model_usage.setdefault(str(model), {})
            add_usage(target, tokens)
        self._recalculate_session_cost()

        # Usage remains available in the sidebar and through /usage or /status.
        # Printing it after every response makes the operational metadata more
        # prominent than the answer itself.

    def record_interrupted_usage(self, estimated_tokens: int, model: str) -> None:
        """Persist an honest local estimate for a call cancelled mid-stream."""
        from apsara_cli.engine.usage import add_usage

        data = {
            "estimated_input_tokens": max(0, int(estimated_tokens)),
            "unreported_calls": 1,
            "interrupted_calls": 1,
        }
        self._session_estimated_tokens += data["estimated_input_tokens"]
        self._session_unreported_calls += 1
        self._session_interrupted_calls += 1
        add_usage(self._session_model_usage.setdefault(str(model), {}), data)

    # ── Assistant message ─────────────────────────────────────────────────────

    def _print_thought_marker(self) -> None:
        """OpenCode-style collapsed '+ Thought: <elapsed>' marker, shown once
        per turn the moment the model starts emitting its answer."""
        if not self._thought_pending:
            return
        self._thought_pending = False
        elapsed = time.monotonic() - self._turn_started_at
        self.print_line()
        self.print_line(f"  {self.style('+ Thought: ' + _fmt_elapsed(elapsed), _THOUGHT)}")

    def assistant(self, text: str) -> None:
        self._print_thought_marker()
        self.print_line()
        self.render_markdown_panel(text)

    def stream_text_start(self) -> None:
        self._stream_buffer = []

    def stream_text_chunk(self, chunk: str) -> None:
        self._stream_buffer.append(chunk)

    def stream_text_end(self) -> None:
        text = "".join(self._stream_buffer)
        self._stream_buffer = []
        self.stop_spinner()
        self.assistant(text)

    # ── Tool activity (inline compact) ───────────────────────────────────────

    def tool_activity(self, tool_name: str, summary: str) -> None:
        icon = self.style("◆", "38;2;100;150;220")
        name = self.style(tool_name, "38;2;180;210;255")
        args = self.dim(f"  {summary}") if summary else ""
        self.print_line(f"    {icon} {name}{args}")

    def tool_result_activity(self, tool_name: str, success: bool, summary: str) -> None:
        # Compact one-liner per tool: '✓ read_file  12 lines read'.
        if success:
            icon = self.style("✓", "38;2;120;200;150")
        else:
            icon = self.style("✗", "38;2;220;100;100")
        name = self.style(tool_name, "38;2;180;210;255")
        detail = f"  {self.dim(summary)}" if summary else ""
        self.print_line(f"  {icon} {name}{detail}")

    # ── Hidden events log ─────────────────────────────────────────────────────

    def set_turn_outcome(self, outcome: str) -> None:
        self._turn_outcome = outcome

    def begin_turn(self) -> None:
        self.stop_spinner()
        self.spinner_message = "Makor is thinking"
        self._spinner_start_time = time.monotonic()
        self.last_run_state = "running"
        self.mascot.begin_turn()
        self._mascot_changed()
        self.current_turn_hidden_events = []
        self.work_notice_shown = False
        self._turn_outcome = ""
        self._thought_pending = True
        self._turn_started_at = time.monotonic()
        self._run_budget = {}
        self._budget_warning_shown = False

    def finish_turn(self, model_label: Optional[str] = None, mode: str = "Build") -> None:
        """Record turn details and surface only outcomes that need attention."""
        self.stop_spinner()
        self.mascot.finish_turn(self._turn_outcome, getattr(self, "last_run_state", "completed"))
        self._mascot_changed()
        self.latest_hidden_events = list(self.current_turn_hidden_events)
        count = len(self.latest_hidden_events)

        elapsed_text = _fmt_elapsed(time.monotonic() - self._turn_started_at)

        outcome = self._turn_outcome or "ok"
        if outcome == "ok":
            return

        if outcome == "error":
            square = self.style("◼", "38;2;220;100;100")
        elif outcome == "blocked":
            square = self.style("◼", "38;2;247;200;100")
        else:
            square = self.style("◼", self.theme.accent)

        tail = [elapsed_text]
        if model_label:
            tail.insert(0, model_label)
        if count:
            plural = "s" if count != 1 else ""
            tail.append(f"{count} step{plural} · /details")
        tail.append(outcome)

        self.print_line()
        self.print_line(
            f"  {square} {self.style(mode, '1', '38;2;200;210;228')}"
            f" {self.dim('· ' + ' · '.join(tail))}"
        )

    def hide_event(self, kind: str, title: str, detail: str = "") -> None:
        from apsara_cli.shared.types import HiddenCliEvent
        event = HiddenCliEvent(kind=kind, title=title, detail=detail)
        self.current_turn_hidden_events.append(event)
        self.hidden_events.append(event)
        self.hidden_events = self.hidden_events[-80:]

    def show_hidden_events(self) -> None:
        events = self.latest_hidden_events or self.hidden_events[-12:]
        if not events:
            self.info("No hidden internal activity yet.")
            return

        self.print_line()
        count = len(events)
        plural = "s" if count != 1 else ""
        self.print_line(
            f"  {self.style('◇ Details', '1', _BRIGHT)}  "
            f"{self.dim(f'{count} internal step{plural}')}"
        )
        self.print_line()

        bc = self.theme.border
        bar = self.style("│", bc)
        max_detail_w = min(self.content_width() - 4, 72)
        for index, event in enumerate(events, start=1):
            kind_text = self.style(event.kind.upper(), "1", "38;2;140;170;220")
            title_text = self.style(event.title, "38;2;210;218;235")
            self.print_line(f"  {bar} {kind_text}  {title_text}")

            if event.detail:
                for dl in truncate_text(event.detail, max_lines=12, max_chars=900).splitlines():
                    self.print_line(f"  {bar}   {self.style(dl[:max_detail_w], '38;2;175;182;200')}")

            if index < count:
                self.print_line(f"  {bar}")

    # ── Input confirmation ────────────────────────────────────────────────────

    @contextmanager
    def raw_terminal(self):
        """
        Hold the terminal in raw mode for the duration of the ``with`` block,
        instead of read_single_key()'s default of entering/restoring raw mode
        on every single call. Needed by callers that read several keystrokes
        in a tight loop with rendering in between (e.g. the /models
        arrow-key picker): toggling termios modes between each read opens a
        brief cooked-mode window where the kernel's line discipline can
        buffer an incoming multi-byte escape sequence until it sees a
        newline, instead of delivering it byte-by-byte — no read timeout can
        recover from that, since the bytes just aren't released yet.
        """
        if termios is None or tty is None or not sys.stdin.isatty():
            yield
            return
        file_descriptor = sys.stdin.fileno()
        original_settings = termios.tcgetattr(file_descriptor)
        tty.setraw(file_descriptor)
        try:
            yield
        finally:
            termios.tcsetattr(file_descriptor, termios.TCSADRAIN, original_settings)

    def read_raw_key(self) -> str:
        """
        Read one key (or one escape sequence, e.g. an arrow key) assuming a
        POSIX terminal is already in raw mode — call this only from inside a
        ``with self.raw_terminal():`` block. ``read_single_key()`` is the
        one-shot convenience wrapper around this for callers that just need
        a single keystroke.
        """
        if msvcrt is not None:
            key = msvcrt.getwch()
            if key in {"\x00", "\xe0"}:
                key += msvcrt.getwch()
            return key

        if termios is not None and tty is not None and sys.stdin.isatty():
            file_descriptor = sys.stdin.fileno()
            # Read via the raw fd (os.read), not sys.stdin.read(): Python's
            # buffered TextIOWrapper can slurp an entire multi-byte escape
            # sequence into its own userspace buffer on the very first read,
            # after which select() on the fd correctly reports "no more bytes
            # at the OS level" even though '[' and the final letter are
            # already sitting in Python's buffer — no timeout can fix that
            # mismatch, since the bytes are already gone from the level
            # select() is watching. Reading and select()-ing the same fd
            # keeps both operating in one buffering domain, so a full
            # arrow-key escape sequence (ESC [ A/B/C/D) actually assembles
            # instead of misfiring as a bare-ESC cancel.
            key = os.read(file_descriptor, 1).decode(errors="replace")
            if key == "\x1b" and _select.select([file_descriptor], [], [], 0.08)[0]:
                key += os.read(file_descriptor, 1).decode(errors="replace")
                if _select.select([file_descriptor], [], [], 0.08)[0]:
                    key += os.read(file_descriptor, 1).decode(errors="replace")
            return key

        return input().strip()[:1]

    def read_single_key(self) -> str:
        """One-shot keystroke read: enters raw mode, reads one key (or
        escape sequence), restores the terminal. For a tight loop of several
        reads (e.g. an arrow-key picker), hold `raw_terminal()` across the
        whole loop and call `read_raw_key()` directly instead — see
        model_picker.py."""
        with self.raw_terminal():
            return self.read_raw_key()

    def prompt_confirmation_choice(
        self,
        *,
        allow_view: bool = False,
        allow_editor: bool = False,
        allow_always: bool = True,
    ) -> str:
        options = [
            f"{self.badge('↵  approve', '17', '48;2;80;170;140')}",
            f"{self.badge('n  reject', '17', '48;2;200;100;80')}",
        ]
        if allow_always:
            options.append(f"{self.badge('a  always', '17', '48;2;80;120;200')}")
        if allow_view:
            options.append(f"{self.badge('v  full diff', '17', '48;2;80;95;125')}")
        if allow_editor:
            options.append(f"{self.badge('e  editor', '15', '48;2;110;80;180')}")
        self.print_line(f"  {'  '.join(options)}")

        while True:
            key = self.read_single_key()
            if key in {"", "\r", "\n", "y", "Y"}:
                self.print_line(f"  {self.style('  ↳ approved', '38;2;120;200;150')}")
                return "approve"
            if allow_always and key in {"a", "A"}:
                self.print_line(f"  {self.style('  ↳ always approve enabled', '38;2;120;180;255')}")
                return "always"
            if allow_view and key in {"v", "V"}:
                return "view"
            if allow_editor and key in {"e", "E"}:
                return "editor"
            if key in {"n", "N", "\x1b", "q", "Q", "\x03"}:
                self.print_line(f"  {self.style('  ↳ rejected', '38;2;220;120;100')}")
                return "reject"

    def confirm_action(self, action: str, payload: dict[str, Any]) -> bool:
        allows_blanket = action_allows_blanket_approval(action) and not (
            action == "undo_turn" and payload.get("force")
        )

        if self.approve_all and allows_blanket:
            return True

        if not sys.stdin.isatty():
            if action == "trust_workspace_code":
                self.error(
                    f"{payload.get('display_path', 'This workspace code')} needs approval "
                    "before it can run, but stdin is not interactive. Run `apsara` "
                    "in this project once to review and approve it."
                )
            elif allows_blanket:
                self.error(
                    f"Approval required for {action}, but stdin is not interactive. "
                    "Re-run with --auto-approve if you trust this action."
                )
            else:
                self.error(
                    f"Explicit approval required for {action}, but stdin is not interactive. "
                    "This action is not covered by --auto-approve."
                )
            return False

        title, preview, diff_preview, diff_full, diff_editor, path_hint = describe_action(action, payload)
        self.print_line()
        # Flat left-accent panel instead of a rounded box, OpenCode-style.
        bar = self.style("▌", "1", "38;2;240;170;90")
        label = self.style("Approve?", "1", "38;2;247;200;100")
        title_styled = self.style(title, "1", "38;2;247;230;190")
        self.print_line(f"  {bar} {label}  {title_styled}")

        if diff_preview:
            self.print_line()
            if action in {"edit_file", "replace_file_lines"} and "original_preview" in payload and "replacement_preview" in payload:
                self.render_side_by_side_diff(payload["original_preview"], payload["replacement_preview"])
            else:
                self.render_diff_text(diff_preview)
        elif preview:
            self.print_line()
            self.print_block(truncate_text(preview, max_lines=12, max_chars=900), "38;2;205;211;222")

        self.print_line()
        while True:
            choice = self.prompt_confirmation_choice(
                allow_view=bool(diff_full and diff_full != diff_preview),
                allow_editor=bool(diff_editor),
                allow_always=allows_blanket,
            )
            if choice == "view":
                self.print_line()
                self.print_notice("diff", "Full change preview", "15", "48;2;80;95;125", "38;2;210;220;240")
                self.render_diff_text(diff_full)
                continue
            if choice == "editor":
                self._open_editor_preview(title, diff_editor or diff_full or diff_preview or "", path_hint)
                continue
            if choice == "always":
                if allows_blanket:
                    self.approve_all = True
                # Defensive fallback for custom prompt implementations: an
                # unsupported "always" response becomes approval for this
                # action only, never a blanket grant.
                return True
            return choice == "approve"

    # ── Editor preview ────────────────────────────────────────────────────────

    def resolve_editor_command(self) -> list[str]:
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
        try:
            return shlex.split(editor)
        except ValueError:
            return [editor]

    def _open_editor_preview(self, title: str, diff_text: str, path_hint: Optional[str] = None) -> bool:
        if not diff_text.strip():
            self.warning("No diff available to open in an editor.")
            return False

        editor_command = self.resolve_editor_command()
        if not editor_command:
            self.error("No editor command is configured. Set $EDITOR or $VISUAL first.")
            return False

        header_lines = ["Apsara by Bondeth", f"Review: {title}", "Close the editor to return."]
        if path_hint:
            header_lines.append(f"Target: {path_hint}")
        review_text = "\n".join(header_lines) + "\n\n" + diff_text + "\n"

        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".diff", prefix="apsara-review-", delete=False,
        ) as temp_file:
            temp_file.write(review_text)
            temp_path = Path(temp_file.name)

        try:
            self.print_notice("editor", f"Opening in {editor_command[0]}", "15", "48;2;80;95;125", "38;2;210;220;240")
            subprocess.run(editor_command + [str(temp_path)], check=False)
            return True
        except FileNotFoundError:
            self.error(f"Editor '{editor_command[0]}' not found. Set $EDITOR or $VISUAL.")
            return False
        except Exception as exc:
            self.error(f"Could not open editor preview: {exc}")
            return False
        finally:
            temp_path.unlink(missing_ok=True)

    # ── Misc display helpers ──────────────────────────────────────────────────

    def tool_call(self, name: str, arguments: dict[str, Any]) -> None:
        arguments_text = json.dumps(arguments, ensure_ascii=True)
        self.log_event("tool_call", name, arguments_text)
        self.print_line(
            f"  {self.badge('tool', '15', '48;2;50;100;170')} "
            f"{self.style(name, '38;2;180;210;255')}"
        )
        self.print_block(arguments_text, "38;2;160;190;220")

    def tool_result(self, result: str) -> None:
        self.log_event("tool_result", "Output received", result)
        self.print_line(
            f"  {self.badge('result', '15', '48;2;40;90;155')} "
            f"{self.style('Tool output', '38;2;180;210;255')}"
        )
        self.print_block(result, "38;2;210;220;235")
