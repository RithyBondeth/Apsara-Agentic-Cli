"""
Full-screen split-pane TUI with a scrollable boxed transcript, persistent
details sidebar, focused composer, centered overlays, and compact telemetry
bar. On narrow terminals the sidebar collapses to protect the conversation.

Design note: this file does NOT reimplement any of ConsoleUI's formatting
logic (badges, diffs, markdown/code rendering, confirm-dialog boxes, the
/models picker) -- `TuiConsoleUI` only swaps *where* that already-styled
output goes (into an in-memory transcript buffer that gets redrawn, instead
of straight to stdout). Most interactive flows (confirmations, the /models
picker) stay fully inline this way: they call ui.print_line()/read_single_key()
like any other command, and nothing ever leaves the running Application or
erases the screen.

Agent turns run on a worker thread so synchronous tool-approval callbacks can
wait while prompt-toolkit keeps painting and operating a native review overlay.
The passthrough path remains only as a defensive fallback for blocking console
calls made before the full-screen application is attached.
"""

import asyncio
import os
import textwrap
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from prompt_toolkit.application import Application
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import (
    ConditionalContainer,
    DynamicContainer,
    Float,
    FloatContainer,
    HSplit,
    VSplit,
    Window,
    WindowAlign,
)
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.layout.processors import (
    ConditionalProcessor,
    PasswordProcessor,
    Processor,
    Transformation,
)
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame

from apsara_cli.cli.chat import (
    build_mode_line,
    build_model_rows,
    execute_instruction,
    handle_chat_command,
    save_if_needed,
    turn_mode_word,
)
from apsara_cli.cli.input import SlashCompleter, color_depth_for_ui
from apsara_cli.cli.options import resolve_runtime_options
from apsara_cli.cli.session import load_session_messages, load_session_usage, sanitize_session_name
from apsara_cli.engine.models import (
    format_context_window,
    is_key_available,
    lookup_model,
    model_availability,
    model_price_label,
)
from apsara_cli.shared.ui import (
    ConsoleUI,
    PANEL_BACKGROUND_ANSI,
    PANEL_BACKGROUND_HEX,
    Theme,
    action_allows_blanket_approval,
    describe_action,
    terminal_width,
)

# Accent / chrome colors (ANSI truecolor), kept in one place.
_ACCENT = "38;2;96;150;250"      # blue left-bar / user accent
_DIMTXT = "38;2;120;125;138"
_PANEL_GUTTER = 3
_SIDEBAR_CONTENT_WIDTH = 34
_SIDEBAR_TOTAL_WIDTH = _SIDEBAR_CONTENT_WIDTH + (_PANEL_GUTTER * 2)
_MIN_CONVERSATION_WIDTH = 48
_MIN_CARD_PANEL_WIDTH = 8


def _welcome_panel_width(columns: int) -> int:
    """Keep the centered composer inside even very narrow terminals."""
    columns = max(1, columns)
    margin = min(16, max(4, columns // 5), max(columns - 1, 0))
    return max(1, min(84, columns - margin))


def _model_needs_key(model: str) -> bool:
    entry = lookup_model(model)
    return bool(entry and entry.tier != "local" and not is_key_available(entry))


class TurnController:
    """Own the worker event loop so the UI can cancel an active agent task."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._task: Optional[asyncio.Task] = None

    def run(self, coroutine):
        loop = asyncio.new_event_loop()
        task = loop.create_task(coroutine)
        with self._lock:
            self._loop = loop
            self._task = task
        try:
            return loop.run_until_complete(task)
        finally:
            with self._lock:
                self._loop = None
                self._task = None
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    def cancel(self) -> bool:
        with self._lock:
            loop, task = self._loop, self._task
        if loop is None or task is None or task.done():
            return False
        loop.call_soon_threadsafe(task.cancel)
        return True


class _ResponsiveCard:
    """Semantic transcript entry rendered at the pane's current width."""

    __slots__ = ("role", "text", "timestamp", "rendered_width", "rendered_lines")

    def __init__(self, role: str, text: str, timestamp: Optional[str] = None) -> None:
        self.role = role
        self.text = text
        self.timestamp = timestamp or datetime.now().strftime("%I:%M").lstrip("0")
        self.rendered_width: Optional[int] = None
        self.rendered_lines: list[str] = []


class TuiConsoleUI(ConsoleUI):
    """
    ConsoleUI backend for the full-screen TUI. All higher-level formatting
    methods are inherited unchanged from ConsoleUI; only the low-level
    output primitives are overridden here.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.lines: list[str | _ResponsiveCard] = []
        self.app: Optional[Application] = None
        self._passthrough = False
        self._spinner_frame = ""
        self._spinner_tick = 0
        self._spinner_task: Optional[asyncio.Task] = None
        self.sidebar_visible = True
        self.native_confirmation_handler = None

    def content_width(self, fallback: Optional[int] = None) -> int:
        """Use the full conversation pane, stopping at the sidebar."""
        columns = self.terminal_columns()
        sidebar_width = (_SIDEBAR_TOTAL_WIDTH + 1) if self.sidebar_is_rendered() else 0
        available = max(8, columns - sidebar_width)
        if fallback is not None:
            return max(8, min(fallback, available))
        return available

    def terminal_columns(self) -> int:
        columns = terminal_width()
        if self.app is not None:
            try:
                columns = self.app.output.get_size().columns
            except Exception:
                pass
        return max(1, columns)

    def sidebar_is_rendered(self) -> bool:
        required = _SIDEBAR_TOTAL_WIDTH + 1 + _MIN_CONVERSATION_WIDTH
        return self.sidebar_visible and self.terminal_columns() >= required

    def attach(self, app: Application) -> None:
        self.app = app

    def _invalidate(self) -> None:
        if self.app is not None:
            try:
                self.app.invalidate()
            except Exception:
                pass

    # ── Output primitives ─────────────────────────────────────────────────

    def print_line(self, text: str = "") -> None:
        if self._passthrough or self.app is None:
            super().print_line(text)
            return
        self._stop_spinner_tick()
        self.lines.extend(text.split("\n") if text else [""])
        self._invalidate()

    def _user_card_lines(self, card: "_ResponsiveCard", width: int) -> list[str]:
        """Reflow a user request on the shared filled panel surface."""
        card_width = max(2, width - 2)
        body_width = max(1, card_width - 1)
        horizontal_padding = min(3, max(0, (body_width - 1) // 2))
        inner_width = max(1, body_width - (horizontal_padding * 2))
        bar = self.style("▌", "1", _ACCENT)

        def card_line(content: str = "", color: str = "38;2;225;230;242") -> str:
            clipped = content[:inner_width]
            padded = clipped.ljust(inner_width)
            gutter = " " * horizontal_padding
            return (
                f"  {bar}"
                f"{self.style(gutter + padded + gutter, color, PANEL_BACKGROUND_ANSI)}"
            )

        lines = [card_line()]
        for raw in card.text.split("\n"):
            wrapped = textwrap.wrap(
                raw,
                width=inner_width,
                replace_whitespace=False,
                drop_whitespace=True,
            ) or [""]
            lines.extend(card_line(line) for line in wrapped)
        lines.extend([
            card_line(),
            card_line(f"you  {card.timestamp}", "2;38;2;112;116;126"),
            card_line(),
        ])
        return lines

    def rendered_lines(self) -> list[str]:
        """Expand responsive cards using the terminal's current dimensions."""
        rendered: list[str] = []
        card_width = max(_MIN_CARD_PANEL_WIDTH, self.content_width() - 3)
        for item in self.lines:
            if isinstance(item, _ResponsiveCard):
                if item.rendered_width != card_width:
                    item.rendered_lines = (
                        self._user_card_lines(item, card_width)
                        if item.role == "user"
                        else self._markdown_card_lines(
                            item.text, width=card_width, timestamp=item.timestamp
                        )
                    )
                    item.rendered_width = card_width
                rendered.extend(item.rendered_lines)
            else:
                rendered.extend(str(item).split("\n"))
        return rendered

    def render_markdown_panel(self, text: str) -> None:
        if self._passthrough or self.app is None:
            super().render_markdown_panel(text)
            return
        self.lines.append(_ResponsiveCard("assistant", text))
        self._invalidate()

    def _print_typed(self, prefix: str, content: str, color_code: str, delay: float) -> None:
        # No fake per-character typing effect inside the pane -- the LLM's
        # real token stream already provides the incremental feel via
        # stream_text_chunk. Append the fully-styled line in one shot.
        if self._passthrough or self.app is None:
            super()._print_typed(prefix, content, color_code, delay)
            return
        self._stop_spinner_tick()
        self.lines.append(f"{prefix}{self.style(content, color_code)}")
        self._invalidate()

    def stream_text_start(self) -> None:
        super().stream_text_start()

    def stream_text_chunk(self, chunk: str) -> None:
        super().stream_text_chunk(chunk)

    def stream_text_end(self) -> None:
        super().stream_text_end()

    def redraw_block(self, prev_line_count: int, new_lines: list[str]) -> None:
        """Splice the transcript buffer in place instead of touching real
        stdout — keeps inline pickers (e.g. /models) inside the scrolling
        chat pane rather than a separate screen."""
        if self._passthrough or self.app is None:
            super().redraw_block(prev_line_count, new_lines)
            return
        if prev_line_count:
            del self.lines[-prev_line_count:]
        self.lines.extend(new_lines)
        self._invalidate()

    # ── Spinner: asyncio ticker instead of a raw-stdout thread ───────────

    def start_spinner(self, message: str) -> None:
        if self._passthrough or self.app is None:
            super().start_spinner(message)
            return
        self.spinner_message = message
        self._spinner_start_time = time.monotonic()
        self.spinner_stop_event.clear()
        if self._spinner_task is None or self._spinner_task.done():
            self._spinner_task = asyncio.get_event_loop().create_task(self._spinner_loop())

    async def _spinner_loop(self) -> None:
        frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        i = 0
        while not self.spinner_stop_event.is_set():
            self._spinner_frame = frames[i % len(frames)]
            self._spinner_tick = i
            i += 1
            self._invalidate()
            await asyncio.sleep(0.08)
        self._spinner_frame = ""
        self._invalidate()

    def _stop_spinner_tick(self) -> None:
        self.spinner_stop_event.set()
        self._spinner_frame = ""

    def stop_spinner(self) -> None:
        if self._passthrough or self.app is None:
            super().stop_spinner()
            return
        self._stop_spinner_tick()
        self._invalidate()

    def update_spinner_action(self, action: str) -> None:
        if self._passthrough or self.app is None:
            super().update_spinner_action(action)
            return
        self.spinner_message = action

    # ── Transcript panels (used by the submit handler) ────────────────────

    def append_user_message(self, text: str) -> None:
        """Store a user request so it can reflow when the pane resizes."""
        self.lines.append("")
        self.lines.append(_ResponsiveCard("user", text))
        self._invalidate()

    # ── Native confirmation bridge + defensive terminal fallback ─────────

    def read_single_key(self) -> str:
        was = self._passthrough
        self._passthrough = True
        try:
            return super().read_single_key()
        finally:
            self._passthrough = was

    def confirm_action(self, action: str, payload: dict) -> bool:
        if self.native_confirmation_handler is not None and not self._passthrough:
            return bool(self.native_confirmation_handler(action, payload))
        return self._run_passthrough(lambda: super(TuiConsoleUI, self).confirm_action(action, payload))

    def prompt_confirmation_choice(self, **kwargs: Any) -> str:
        return self._run_passthrough(lambda: super(TuiConsoleUI, self).prompt_confirmation_choice(**kwargs))

    def _run_passthrough(self, fn):
        """
        Hand the real terminal to a blocking call, then force the outer
        full-screen app to fully repaint once it's done.
        """
        self._passthrough = True
        if self.app is not None:
            try:
                self.app.renderer.erase()
            except Exception:
                pass
        try:
            return fn()
        finally:
            self._passthrough = False
            self._invalidate()


class _ScrollTextControl(FormattedTextControl):
    """Keep scrolling within the text snapshot captured for this render."""

    def create_content(self, width, height):
        content = super().create_content(width, height)
        # Worker output can change after fragments are captured but before the
        # cursor callback runs. Never index the snapshot with a newer position.
        cursor = content.cursor_position
        content.cursor_position = Point(
            x=cursor.x, y=max(0, min(cursor.y, content.line_count - 1))
        )
        return content


class _PlaceholderProcessor(Processor):
    """OpenCode-style dim hint text shown inside the input box while empty."""

    def __init__(self, text: str) -> None:
        self.text = text

    def apply_transformation(self, ti):
        if ti.lineno == 0 and not ti.document.text:
            return Transformation([("class:placeholder", self.text)])
        return Transformation(ti.fragments)


# ── Sidebar / status bar rendering ────────────────────────────────────────

def _shorten_path(path: str, width: int = 29) -> str:
    if len(path) <= width:
        return path
    return "…" + path[-(width - 1):]


# Sidebar accent colors — one hue per section, plus tier/health colors.
_C_CONTEXT = "38;2;110;200;235"   # cyan
_C_MODEL = "38;2;190;150;250"     # violet
_C_SESSION = "38;2;130;210;160"   # green
_C_WORKSPACE = "38;2;240;190;110" # gold
_C_CARD = "38;2;130;170;250"      # blue
_C_VALUE = "38;2;225;230;242"     # bright values
_TIER_COLORS = {"free": "38;2;130;210;160", "paid": "38;2;240;190;110", "local": "38;2;140;180;250"}

_SIDEBAR_RULE_W = 27


def _section(ui: "ConsoleUI", icon: str, title: str, color: str) -> str:
    """Colored section header with a faint trailing rule: '◍ Context ────'."""
    rule = "─" * max(_SIDEBAR_RULE_W - len(title) - len(icon) - 2, 0)
    return f" {ui.style(icon + ' ' + title, '1', color)} {ui.style(rule, '2', color)}"


def _meter(ui: "ConsoleUI", pct: int, width: int = 10) -> str:
    """Colored usage meter: '▰▰▰▱▱▱▱▱▱▱ 30%'."""
    filled = max(0, min(width, round(pct / 100 * width)))
    if pct < 70:
        color = "38;2;120;200;150"
    elif pct < 90:
        color = "38;2;247;200;100"
    else:
        color = "38;2;235;110;100"
    bar = ui.style("▰" * filled, color) + ui.style("▱" * (width - filled), "38;2;62;68;86")
    return f"{bar} {ui.style(f'{pct}%', color)}"


def _sidebar_text(
    ui: TuiConsoleUI,
    options: Any,
    current_model: str,
    session_label: str,
    history: list[dict[str, Any]],
    session_started: str,
) -> ANSI:
    from apsara_cli import __version__

    body: list[str] = []
    lines = body.append

    # Session title + start time, like OpenCode's 'New session – <ts>'.
    lines("")
    lines(f" {ui.style('◆', ui.theme.accent)} {ui.style(session_label, '1', _C_VALUE)}")
    lines(f"   {ui.style(session_started, _DIMTXT)}")
    lines("")

    # Context is current request capacity. Session tokens are cumulative usage.
    total = ui._session_total_tokens
    context_tokens = ui._context_tokens
    context_budget = ui._context_budget
    entry = lookup_model(current_model)
    lines(_section(ui, "◍", "Context", _C_CONTEXT))
    if context_budget:
        pct = min(100, int(context_tokens / context_budget * 100))
        lines(f"   {_meter(ui, pct)}")
    lines(
        f"   {ui.style(f'{context_tokens:,}', _C_VALUE)} "
        f"{ui.style(f'/ {context_budget:,} context', _DIMTXT)}"
    )
    lines(f"   {ui.style(f'{total:,}', _C_VALUE)} {ui.style('session tokens', _DIMTXT)}")
    if ui._session_estimated_tokens:
        lines(
            f"   {ui.style(f'~{ui._session_estimated_tokens:,}', '38;2;247;200;100')} "
            f"{ui.style('estimated input · provider omitted usage', _DIMTXT)}"
        )
    lines(
        f"   {ui.style(f'in {ui._session_prompt_tokens:,} · out {ui._session_completion_tokens:,}', _DIMTXT)}"
    )
    if ui._session_cached_tokens or ui._session_cache_creation_tokens or ui._session_reasoning_tokens:
        lines(
            f"   {ui.style(f'cached {ui._session_cached_tokens:,} · cache write {ui._session_cache_creation_tokens:,}', _DIMTXT)}"
        )
        lines(f"   {ui.style(f'reasoning {ui._session_reasoning_tokens:,}', _DIMTXT)}")
    lines(f"   {ui.style(ui.session_cost_label(), '38;2;130;210;160')} {ui.style('cost', _DIMTXT)}")
    if ui.rate_limit_label():
        lines(f"   {ui.style(ui.rate_limit_label(), _DIMTXT)}")
    lines("")

    budget = ui._run_budget
    if budget:
        lines(_section(ui, "◷", "Turn budget", _C_CONTEXT))
        lines(f"   {budget['steps_used']}/{budget['step_limit']} steps · "
              f"{budget['tool_calls_used']}/{budget['tool_call_limit']} tools")
        spent = budget['reported_usage'] + budget['estimated_usage']
        lines(f"   {spent:,}/{budget['usage_limit']:,} tokens")
        if not budget['usage_complete']:
            lines(f"   {ui.style('Includes reserved estimates', '38;2;247;200;100')}")
        lines(f"   {budget['reused_checks']} results reused")
        lines("")

    # Model: name, provider · tier, context window, key status.
    key_ok = True
    if entry:
        key_ok = is_key_available(entry) or entry.tier == "local"
        tier_color = _TIER_COLORS.get(entry.tier, _DIMTXT)
        lines(_section(ui, "✦", "Model", _C_MODEL))
        lines(f"   {ui.style(entry.display_name, '1', _C_VALUE)}")
        lines(
            f"   {ui.style(entry.provider.capitalize(), '38;2;190;200;220')}"
            f" {ui.style('·', _DIMTXT)} {ui.style(entry.tier, tier_color)}"
        )
        lines(f"   {ui.style(model_price_label(entry.model_id), _DIMTXT)}")
        lines(f"   {ui.style(format_context_window(entry.context_window) + ' ctx', _DIMTXT)}")
        lines(
            f"   {ui.style('✓ key set', '38;2;120;200;150')}"
            if key_ok
            else f"   {ui.style(f'✗ needs {entry.env_var}', '38;2;235;110;100')}"
        )
        lines("")

    # Session detail: turns, messages, mode and governance flags.
    turns = sum(1 for m in history if m.get("role") == "user")
    turn_word = "turns" if turns != 1 else "turn"
    lines(_section(ui, "❯", "Session", _C_SESSION))
    lines(
        f"   {ui.style(str(turns), _C_VALUE)} {ui.style(turn_word, _DIMTXT)}"
        f" {ui.style('·', _DIMTXT)} {ui.style(str(len(history)), _C_VALUE)} {ui.style('messages', _DIMTXT)}"
    )
    if options.dry_run:
        lines(f"   {ui.style('mode', _DIMTXT)}  {ui.style('Dry-run', '1', '38;2;247;200;100')}")
    elif options.read_only:
        lines(f"   {ui.style('mode', _DIMTXT)}  {ui.style('Read-only', '1', '38;2;240;170;90')}")
    else:
        lines(f"   {ui.style('mode', _DIMTXT)}  {ui.style('Build', '1', ui.theme.accent)}")
    bash_state = (
        ui.style("enabled", "38;2;130;210;160") if options.allow_bash else ui.style("off", _DIMTXT)
    )
    lines(f"   {ui.style('bash', _DIMTXT)}  {bash_state}")
    if options.auto_approve:
        lines(
            f"   {ui.style('file auto-approve', _DIMTXT)}  "
            f"{ui.style('on', '38;2;247;200;100')}"
        )
    steps = len(ui.latest_hidden_events)
    if steps:
        step_word = "steps" if steps != 1 else "step"
        lines(
            f"   {ui.style('last turn', _DIMTXT)} {ui.style(str(steps), _C_VALUE)}"
            f" {ui.style(step_word + ' · /details', _DIMTXT)}"
        )
    lines("")

    # Workspace.
    lines(_section(ui, "⌂", "Workspace", _C_WORKSPACE))
    lines(f"   {ui.style(_shorten_path(str(options.workspace_root)), _DIMTXT)}")
    lines("")

    # Getting-started card — only while no key is configured, OpenCode style.
    if not key_ok:
        lines(_section(ui, "◇", "Getting started", _C_CARD))
        for text in (
            "Apsara includes free-tier and",
            "local models so you can start",
            "quickly.",
            "",
            "Connect a provider to use",
            "other models, including",
            "Claude, GPT, Gemini etc",
        ):
            lines(f"   {ui.style(text, _DIMTXT)}" if text else "")
        lines("")
        lines(
            f"   {ui.style('Connect provider', '1', _C_VALUE)}  "
            f"{ui.style('/key set', '1', '38;2;140;180;255')}"
        )
        lines("")

    # Shortcuts.
    for cmd, desc in [
        ("/models", "switch models"),
        ("/status", "session status"),
        ("/help", "all commands"),
    ]:
        lines(f"   {ui.style(cmd, '1', '38;2;140;180;255')}  {ui.style(desc, _DIMTXT)}")

    # Footer: brand + version + developer credit.
    lines("")
    lines(
        f" {ui.style('●', '38;2;120;200;150')} "
        f"{ui.style('Apsara', '1', _C_VALUE)} "
        f"{ui.style('v' + __version__, _C_MODEL)}"
    )
    lines(f"   {ui.style('by Bondeth', _C_WORKSPACE)}")
    lines("")

    return ANSI("\n".join(body))


def _chat_text(ui: TuiConsoleUI) -> ANSI:
    body_lines = ui.rendered_lines()
    if ui._spinner_frame:
        body_lines.append("")
        body_lines.append(f"  {ui.compose_spinner_line(ui._spinner_tick)}")
    return ANSI("\n".join(body_lines))


def _status_left(ui: TuiConsoleUI, options: Any) -> ANSI:
    from apsara_cli import __version__

    workspace = Path(options.workspace_root)
    columns = ui.terminal_columns()
    path_width = max(8, min(38, columns - 58))
    label = _shorten_path(str(workspace), width=path_width)
    workspace_label = "" if columns < 64 else f"  {ui.style(label, _C_WORKSPACE)}"
    return ANSI(
        f"{' ' * _PANEL_GUTTER}{ui.style('apsara', '1', '38;2;210;216;242')} "
        f"{ui.style('v' + __version__, _DIMTXT)}"
        f"{workspace_label}"
    )


def _status_right(ui: TuiConsoleUI, options: Any, current_model: str) -> ANSI:
    total = ui._context_tokens
    tok = f"{total / 1000:.1f}K" if total >= 1000 else str(total)
    entry = lookup_model(current_model)
    budget = ui._context_budget or (entry.context_window if entry else 0)
    context = format_context_window(budget) if budget else "—"
    mode = turn_mode_word(options).upper()
    mode_color = {
        "DRY-RUN": "48;2;112;84;35",
        "READ-ONLY": "48;2;105;72;38",
    }.get(mode, "48;2;78;64;122")
    pct = ""
    if ui._context_budget:
        pct = f" ({min(100, int(ui._context_tokens / ui._context_budget * 100))}% ctx)"
    cost_label = ui.session_cost_label()
    if ui.terminal_columns() < 64:
        right = (
            f"{ui.style(f' {mode} ', '1', '38;2;238;232;255', mode_color)}"
            f"{' ' * _PANEL_GUTTER}"
        )
    elif ui.terminal_columns() < 100:
        right = (
            f"{ui.style(cost_label, '38;2;130;210;160')}  "
            f"{ui.style(f' {mode} ', '1', '38;2;238;232;255', mode_color)}"
            f"{' ' * _PANEL_GUTTER}"
        )
    else:
        right = (
            f"{ui.style(f'ctx {tok}/{context}{pct}', _DIMTXT)}  "
            f"{ui.style(cost_label, '38;2;130;210;160')}  "
            f"{ui.style(f' {mode} ', '1', '38;2;238;232;255', mode_color)}"
            f"{' ' * _PANEL_GUTTER}"
        )
    return ANSI(right)


def _approval_text(ui: TuiConsoleUI, approval: dict[str, Any]) -> ANSI:
    """Content for the inline tool-permission card."""
    lines: list[str] = [
        (
            ui.style("◆", "1", "38;2;247;190;90")
            + " "
            + ui.style("Permission required", "1", "38;2;247;214;145")
        ),
        "",
        ui.style(str(approval.get("title", "Action requires approval")), "1", _C_VALUE),
    ]
    content = (
        approval.get("full")
        if approval.get("show_full") and approval.get("full")
        else approval.get("preview")
    )
    if content:
        lines.extend(["", ui.style("Review", "1", "38;2;164;173;196")])
        for raw in str(content).splitlines():
            if raw.startswith(("+++", "---", "@@")):
                color = "38;2;140;191;255"
            elif raw.startswith("+"):
                color = "38;2;152;224;171"
            elif raw.startswith("-"):
                color = "38;2;255;168;168"
            else:
                color = "38;2;205;211;222"
            lines.append(ui.style("│ ", "38;2;70;82;112") + ui.style(raw, color))
    else:
        lines.extend(["", ui.style("Review the action above before allowing it.", _DIMTXT)])
    return ANSI("\n".join(lines))


def _approval_footer(ui: TuiConsoleUI, approval: dict[str, Any]) -> ANSI:
    allow_always = approval.get("allow_always", not approval.get("is_trust"))
    has_full_diff = approval.get("full") and approval.get("full") != approval.get("preview")
    width = ui.content_width()
    if width < 60:
        # Account for the outer/card gutters so the deny key cannot be
        # clipped off-screen on narrow terminals.
        available = max(8, width - 12)
        if available < 20:
            hints = [
                ui.style("↵", "1", "38;2;130;210;160") + " allow",
                ui.style("n", "1", "38;2;225;125;115") + " deny",
            ]
            visible_length = len("↵ allow · n deny")
        else:
            hints = [
                ui.style("enter", "1", "38;2;130;210;160") + " allow",
                ui.style("n", "1", "38;2;225;125;115") + " deny",
            ]
            visible_length = len("enter allow · n deny")
        optional: list[tuple[str, str]] = []
        if allow_always:
            optional.append(
                (ui.style("a", "1", "38;2;120;165;235") + " always", "a always")
            )
        if has_full_diff:
            optional.append(
                (ui.style("v", "1", "38;2;190;150;250") + " diff", "v diff")
            )
        for styled_hint, visible_hint in optional:
            candidate_length = visible_length + len(" · ") + len(visible_hint)
            if candidate_length <= available:
                hints.append(styled_hint)
                visible_length = candidate_length
        return ANSI(" · ".join(hints))

    hints = [
        ui.style(" enter ", "1", "38;2;18;24;20", "48;2;130;210;160")
        + ui.style(" allow once", "38;2;170;224;188"),
        ui.style(" n/esc ", "1", "38;2;30;18;18", "48;2;225;125;115")
        + ui.style(" deny", "38;2;242;172;164"),
    ]
    if allow_always:
        hints.append(
            ui.style(" a ", "1", "38;2;15;21;32", "48;2;120;165;235")
            + ui.style(" always allow", "38;2;166;198;246")
        )
    if has_full_diff:
        label = "preview" if approval.get("show_full") else "full diff"
        hints.append(ui.style("v", "1", "38;2;190;150;250") + ui.dim(f" {label}"))
    hints.append(ui.dim("↑↓ scroll"))
    return ANSI("  ".join(hints))


def _restore_history(ui: TuiConsoleUI, history: list[dict[str, Any]]) -> None:
    """Restore user and final-assistant messages without exposing tool internals."""
    turns = sum(1 for message in history if message.get("role") == "user")
    ui.lines.extend([
        "",
        f"  {ui.style('↺', '38;2;130;210;160')} "
        f"{ui.style(f'Resumed {turns} prior turn' + ('s' if turns != 1 else ''), '1', _C_VALUE)}",
        f"  {ui.dim('Saved conversation restored below')}",
    ])
    for message in history:
        role = message.get("role")
        content = str(message.get("content") or "").strip()
        if not content:
            continue
        if role == "user":
            ui.append_user_message(content)
        elif role == "assistant" and not message.get("tool_calls"):
            ui.assistant(content)


def _refresh_context_usage(
    ui: TuiConsoleUI, history: list[dict[str, Any]], model: str
) -> None:
    """Keep the sidebar's capacity meter tied to the current request."""
    from apsara_cli.cli.history import input_token_budget
    from apsara_cli.engine.executor import SYSTEM_PROMPT
    from apsara_cli.engine.llm import estimate_request_tokens

    tokens = estimate_request_tokens(
        [{"role": "system", "content": SYSTEM_PROMPT}] + history,
        model=model,
    )
    ui.set_context_usage(tokens, input_token_budget(model))


async def tui_loop(args: object, config: object) -> int:
    options = resolve_runtime_options(args, config.defaults)

    from apsara_cli.cli.chat import _load_stored_keys
    _load_stored_keys()

    theme = Theme()
    config_theme = getattr(config, "theme", None)
    if config_theme is not None:
        config_theme.apply_to(theme)

    ui = TuiConsoleUI(use_color=options.use_color, auto_approve=options.auto_approve, theme=theme)

    history: list[dict[str, Any]] = []
    if not options.stateless:
        history = load_session_messages(options.workspace_root, options.session)
        ui.restore_usage(load_session_usage(options.workspace_root, options.session))
    _refresh_context_usage(ui, history, options.model)
    session_label = sanitize_session_name(options.session) if not options.stateless else "stateless"

    # First run (nothing to resume) opens on the OpenCode-style welcome
    # screen: centered logo with the message box directly below it. The
    # split chat/sidebar layout takes over on the first submission.
    state = {"model": options.model, "welcome": not history, "busy": False}
    # The classic split-pane layout keeps details visible by default. The
    # responsive renderer collapses it only when the conversation would be
    # starved, and Ctrl+B remains available as an explicit override.
    turn_controller = TurnController()
    sidebar_state = {"visible": ui.sidebar_visible}
    ui.sidebar_visible = sidebar_state["visible"]

    # /models runs as a native centered overlay. An asyncio.Future bridges the
    # background submission coroutine to the application's key bindings.
    picker: dict[str, Any] = {
        "active": False, "rows": [], "selectable": [],
        "selected": 0, "future": None, "filter": "",
    }
    picker_active = Condition(lambda: picker["active"])

    # Inline key entry (when a picked model has no key): a masked prompt in
    # the chat pane — NOT getpass/run_in_terminal, which would suspend the
    # whole TUI onto a bare terminal screen. mode is None | "key" | "yesno";
    # a Future bridges the async switch coroutine to the key-press bindings.
    keyprompt: dict[str, Any] = {"mode": None, "future": None}
    kp_key = Condition(lambda: keyprompt["mode"] == "key")
    kp_yesno = Condition(lambda: keyprompt["mode"] == "yesno")
    kp_any = Condition(lambda: keyprompt["mode"] is not None)

    approval: dict[str, Any] = {
        "active": False,
        "title": "",
        "preview": "",
        "full": "",
        "show_full": False,
        "result": "reject",
        "event": None,
        "is_trust": False,
        "action": "",
    }
    approval_active = Condition(lambda: approval["active"])

    from apsara_cli.cli.banner import banner_taglines, logo_width, small_logo_line, styled_logo_lines
    _subtitle, _powered = banner_taglines(config)

    # ── Layout ─────────────────────────────────────────────────────────────

    # Sidebar session subtitle: 'resumed · <ts>' or 'new · <ts>', OpenCode's
    # 'New session – <timestamp>' equivalent.
    session_started = (
        ("resumed · " if history else "new · ") + datetime.now().strftime("%Y-%m-%d %H:%M")
    )

    def _chat_cursor_position() -> Point:
        # The Window clamps its scroll to keep this 'cursor' visible on every
        # render — so pointing it at the last line implements follow-bottom,
        # pointing it at the current scroll row freezes manual browsing, and
        total = len(ui.rendered_lines()) + (2 if ui._spinner_frame else 0)
        if follow["on"]:
            return Point(x=0, y=max(total - 1, 0))
        return Point(x=0, y=max(chat_window.vertical_scroll, 0))

    chat_control = _ScrollTextControl(
        lambda: _chat_text(ui), focusable=False, get_cursor_position=_chat_cursor_position
    )
    chat_window = Window(content=chat_control, wrap_lines=True, always_hide_cursor=True)

    def _sidebar_cursor_position() -> Point:
        # Keep Prompt Toolkit from snapping a mouse-scrolled sidebar back to
        # its logical cursor at row zero on the next render.
        return Point(x=0, y=max(sidebar_window.vertical_scroll, 0))

    sidebar_control = _ScrollTextControl(
        lambda: _sidebar_text(ui, options, state["model"], session_label, history, session_started),
        focusable=False,
        get_cursor_position=_sidebar_cursor_position,
    )
    sidebar_window = Window(
        content=sidebar_control,
        width=_SIDEBAR_CONTENT_WIDTH,
        wrap_lines=True,
        always_hide_cursor=True,
        style="class:sidebar",
    )
    sidebar_shell = VSplit([
        Window(width=_PANEL_GUTTER, char=" ", style="class:sidebar"),
        sidebar_window,
        Window(width=_PANEL_GUTTER, char=" ", style="class:sidebar"),
    ], width=_SIDEBAR_TOTAL_WIDTH, style="class:sidebar")

    status_content = VSplit([
        Window(content=FormattedTextControl(lambda: _status_left(ui, options)), height=1),
        Window(
            content=FormattedTextControl(lambda: _status_right(ui, options, state["model"])),
            height=1,
            align=WindowAlign.RIGHT,
            dont_extend_width=True,
        ),
    ], height=1, style="class:statusbar")
    status_bar = status_content

    history_dir = Path.home() / ".apsara"
    history_dir.mkdir(parents=True, exist_ok=True)
    input_buffer = Buffer(
        completer=SlashCompleter(),
        auto_suggest=AutoSuggestFromHistory(),
        history=FileHistory(str(history_dir / "input_history")),
        multiline=True,
    )
    # ── Typing box (shared builder for the welcome and chat layouts) ──────
    # A flat filled surface, colored left rail and mode/model inside the panel,
    # matching the supplied OpenCode references without losing Apsara's palette.
    _placeholder = _PlaceholderProcessor('Ask anything… "What is the tech stack of this project?"')

    def _make_input_box(input_height, width=None):
        control_window = Window(
            content=BufferControl(
                buffer=input_buffer,
                input_processors=[
                    # Mask characters while entering an API key; hide the
                    # regular placeholder during any key prompt.
                    ConditionalProcessor(PasswordProcessor(), kp_key),
                    ConditionalProcessor(_placeholder, ~kp_any),
                ],
            ),
            height=input_height,
            dont_extend_height=True,
            style="class:composer",
        )
        mode_window = Window(
            content=FormattedTextControl(
                lambda: ANSI(build_mode_line(ui, options, state["model"])), focusable=False
            ),
            height=1,
        )
        def _row(content, height) -> VSplit:
            return VSplit([
                Window(width=1, char="▌", style="class:accent"),
                Window(width=_PANEL_GUTTER, char=" "),
                content,
                Window(width=_PANEL_GUTTER, char=" "),
            ], height=height)

        padding_row = lambda: _row(Window(char=" "), 1)

        def _input_rows() -> Dimension:
            panel_width = width().preferred if callable(width) else ui.content_width() - 5
            text_width = max(1, panel_width - 1 - 2 * _PANEL_GUTTER)
            preferred = control_window.preferred_height(text_width, 6).preferred
            return Dimension.exact(max(1, preferred))

        box = HSplit([
            padding_row(),
            _row(control_window, _input_rows),
            padding_row(),
            _row(mode_window, 1),
            padding_row(),
        ], width=width, style="class:composer")
        return box, control_window

    chat_box, chat_input_window = _make_input_box(Dimension(min=1, preferred=1, max=6))

    # Permission requests stay in the conversation flow. Keeping this card
    # inside the left transcript pane preserves the user's context and the
    # workspace sidebar while the agent worker waits for a decision.
    approval_window = Window(
        content=FormattedTextControl(lambda: _approval_text(ui, approval), focusable=False),
        height=Dimension(min=4, preferred=7, max=11),
        wrap_lines=True,
        always_hide_cursor=True,
        style="class:approval",
    )
    approval_footer_window = Window(
        FormattedTextControl(lambda: _approval_footer(ui, approval), focusable=False),
        height=1,
        style="class:approval",
    )
    approval_card = VSplit([
            Window(width=1, char="▌", style="class:approvalaccent"),
            Window(width=3, char=" ", style="class:approval"),
            HSplit([
                Window(height=1, char=" ", style="class:approval"),
                approval_window,
                Window(height=1, char=" ", style="class:approval"),
                approval_footer_window,
                Window(height=1, char=" ", style="class:approval"),
            ], style="class:approval"),
            Window(width=3, char=" ", style="class:approval"),
        ],
        height=Dimension.exact(11),
        style="class:approval",
    )
    approval_inline = ConditionalContainer(
        content=VSplit([
            Window(width=2, char=" "),
            approval_card,
            Window(width=3, char=" "),
        ], height=Dimension.exact(11)),
        filter=approval_active,
    )

    # The composer belongs to the conversation pane. The sidebar continues
    # alongside it down to the shared status strip, like the supplied reference.
    def _chat_heading() -> ANSI:
        from rich.text import Text
        title = next((str(m.get("content", "")).split("\n")[0]
                      for m in history if m.get("role") == "user"), "New session")
        heading = Text(title)
        heading.truncate(max(1, ui.content_width() - 8), overflow="ellipsis")
        return ANSI(f"  {ui.style('✦', '1', '38;2;190;150;250')} "
                    f"{ui.style(heading.plain, '1', '38;2;225;230;242')}")

    def _composer_hints() -> ANSI:
        left = (
            ui.style("● working", "38;2;247;200;100") + ui.dim(" · esc interrupt")
            if state["busy"] else ui.style("enter", "38;2;140;180;255") + ui.dim(" send")
        )
        right = ui.style("ctrl+p", "38;2;190;150;250") + ui.dim(" commands")
        if ui.content_width() >= 70:
            right = ui.style("ctrl+b", "38;2;130;210;160") + ui.dim(" details   ") + right
        return ANSI(left + "   " + right)

    def _transcript_container():
        conversation = HSplit([
            Window(height=1, char=" "),
            Window(FormattedTextControl(_chat_heading), height=1),
            Window(height=1, char=" "),
            chat_window,
            approval_inline,
            Window(height=1, char=" "),
            VSplit([Window(width=2), chat_box, Window(width=3)]),
            VSplit([Window(width=2), Window(FormattedTextControl(_composer_hints), height=1),
                    Window(width=3)], height=1),
            Window(height=1, char=" "),
        ])
        if sidebar_state["visible"] and ui.sidebar_is_rendered():
            return VSplit([
                conversation,
                Window(width=1, char="│", style="class:sep"),
                sidebar_shell,
            ])
        return conversation

    chat_root = HSplit([
        DynamicContainer(_transcript_container),
        status_bar,
    ])

    # ── Welcome layout: everything vertically centered, OpenCode-style ─────
    from apsara_cli import __version__

    box_dimension = lambda: Dimension.exact(_welcome_panel_width(ui.terminal_columns()))
    welcome_box, welcome_input_window = _make_input_box(
        Dimension(min=1, preferred=1, max=4), width=box_dimension
    )

    def _welcome_logo() -> ANSI:
        return ANSI(
            "\n".join(styled_logo_lines(ui, 0))
            if ui.terminal_columns() >= logo_width() + 2 else small_logo_line(ui)
        )

    logo_window = Window(
        FormattedTextControl(_welcome_logo, focusable=False),
        height=lambda: 5 if ui.terminal_columns() >= logo_width() + 2 else 1,
        align=WindowAlign.CENTER,
    )
    subtitle_window = Window(
        FormattedTextControl(ANSI(ui.style(_subtitle, "38;2;168;172;205")), focusable=False),
        height=1,
        align=WindowAlign.CENTER,
    )
    powered_window = Window(
        FormattedTextControl(ANSI(ui.style(_powered, "38;2;200;166;110")), focusable=False),
        height=1,
        align=WindowAlign.CENTER,
    )
    roomy_welcome = Condition(
        lambda: ui.app is not None and ui.app.output.get_size().rows >= 28
    )

    _key = "1", "38;2;140;180;255"
    hints_ansi = ANSI(
        f"{ui.style('/', *_key)} {ui.style('commands', _DIMTXT)}   "
        f"{ui.style('ctrl+p', *_key)} {ui.style('palette', _DIMTXT)}   "
        f"{ui.style('↑↓', *_key)} {ui.style('history', _DIMTXT)}"
    )
    hints_row = VSplit([
        Window(),
        Window(FormattedTextControl(hints_ansi, focusable=False), width=box_dimension,
               height=1, align=WindowAlign.RIGHT),
        Window(),
    ], height=1)

    _entry = lookup_model(options.model)
    _key_ok = _entry is None or is_key_available(_entry) or _entry.tier == "local"
    if _entry and _entry.tier == "paid":
        tip_ansi = ANSI(
            ui.style(f"Paid model · {model_price_label(options.model)} · funded {_entry.provider} account required", '38;2;240;190;110')
        )
    elif not _key_ok:
        tip_ansi = ANSI(
            f"{ui.style('●', '38;2;240;170;90')} {ui.style('Tip', '1', '38;2;240;170;90')} "
            f"{ui.style('Ask anything — your provider key will be requested securely on first use', '38;2;200;205;215')}"
        )
    else:
        tip_ansi = ANSI(
            f"{ui.style('●', '38;2;240;170;90')} {ui.style('Tip', '1', '38;2;240;170;90')} "
            f"{ui.style('Ask anything, or type', '38;2;200;205;215')} "
            f"{ui.style('/', '1', '38;2;225;230;242')} "
            f"{ui.style('to browse commands', '38;2;200;205;215')}"
        )
    tip_window = Window(
        FormattedTextControl(tip_ansi, focusable=False), height=1, align=WindowAlign.CENTER
    )

    _home = str(Path.home())
    _ws = str(options.workspace_root)
    _ws_display = "~" + _ws[len(_home):] if _ws.startswith(_home) else _ws
    welcome_bar = VSplit([
        Window(
            FormattedTextControl(ANSI(" " + ui.style(_ws_display, "38;2;240;190;110")), focusable=False),
            height=1,
        ),
        Window(
            FormattedTextControl(ANSI(ui.style("Apsara " + __version__, "38;2;190;150;250") + " "), focusable=False),
            height=1,
            align=WindowAlign.RIGHT,
            dont_extend_width=True,
        ),
    ], height=1)

    welcome_root = HSplit([
        Window(height=Dimension(weight=2)),
        logo_window,
        Window(height=1, char=" "),
        ConditionalContainer(subtitle_window, filter=roomy_welcome),
        ConditionalContainer(powered_window, filter=roomy_welcome),
        Window(height=2, char=" "),
        VSplit([Window(), welcome_box, Window()]),
        Window(height=1, char=" "),
        hints_row,
        Window(height=2, char=" "),
        tip_window,
        Window(height=Dimension(weight=3)),
        welcome_bar,
    ])

    # ── Chat-pane scrolling: auto-follow the newest line, but let PageUp/
    # PageDown and the mouse wheel browse history (e.g. long /help output) ──
    follow = {"on": True, "last_set": 0}

    def _chat_max_scroll() -> int:
        ri = chat_window.render_info
        if ri is None:
            return 0
        return max(ri.content_height - ri.window_height, 0)

    def _before_render(app_) -> None:
        # While the /models picker is open, _chat_cursor_position drives the
        # scroll (keeping the highlighted row visible) — don't fight it.
        if state["welcome"] or picker["active"]:
            return
        max_scroll = _chat_max_scroll()
        cur = chat_window.vertical_scroll
        if follow["on"]:
            if cur < follow["last_set"]:
                follow["on"] = False  # mouse wheel scrolled up — stop following
            else:
                chat_window.vertical_scroll = max_scroll
                follow["last_set"] = max_scroll
        elif cur >= max_scroll:
            follow["on"] = True  # reached the bottom again — resume following
            follow["last_set"] = cur

    def _after_render(app_) -> None:
        sidebar_info = sidebar_window.render_info
        if sidebar_info is not None:
            sidebar_max = max(sidebar_info.content_height - sidebar_info.window_height, 0)
            if sidebar_window.vertical_scroll > sidebar_max:
                sidebar_window.vertical_scroll = sidebar_max
                app_.invalidate()

        # render_info is only fresh AFTER a render, so when following we may
        # discover the true bottom just now — snap to it and repaint once.
        if state["welcome"] or picker["active"] or not follow["on"]:
            return
        max_scroll = _chat_max_scroll()
        if chat_window.vertical_scroll != max_scroll:
            chat_window.vertical_scroll = max_scroll
            follow["last_set"] = max_scroll
            app_.invalidate()

    style = Style.from_dict({
        "surface": "bg:#0a0b10 fg:#e1e6f2",
        "accent": "fg:#6096fa",
        "sep": "fg:#3d4668",
        "inputborder": "fg:#5a6cb4",
        "composer": f"bg:{PANEL_BACKGROUND_HEX}",
        "approvalaccent": "fg:#f0b35f bg:#0e1015",
        "placeholder": "fg:#5a616e",
        "sidebar": f"bg:{PANEL_BACKGROUND_HEX}",
        "statusbar": "bg:#25243b",
        "overlay": "bg:#08090d",
        "approval": "bg:#0e1015 fg:#e1e6f2",
        "approval.border": "fg:#65739a bg:#0e1015",
        "approval.label": "fg:#f0c878 bg:#0e1015 bold",
        "picker": "bg:#0e1015 fg:#e1e6f2",
        "picker.border": "fg:#7180aa bg:#0e1015",
        "picker.label": "fg:#dce4ff bg:#0e1015 bold",
        "completion-menu.completion": "bg:#1c1f27 fg:#c8cede",
        "completion-menu.completion.current": "bg:#f7b76a fg:#1a1a1a",
    })

    # ── /models native in-app picker ─────────────────────────────────────
    _PICKER_CURSOR = ("1", "38;2;120;200;150")
    _PICKER_HINT = "type search  ·  ↑↓ move  ·  enter select  ·  esc close"

    def _picker_row(txt: str, model_id, highlighted: bool) -> str:
        if model_id is None:               # provider group header
            return f"  {txt}"
        entry = lookup_model(model_id)
        if entry is not None:
            key_ok = is_key_available(entry) or entry.tier == "local"
            context = format_context_window(entry.context_window)
            availability = "ready" if key_ok else "key needed"
            txt = (
                f"{entry.display_name}  "
                f"{ui.dim(entry.provider + ' · ' + entry.tier + ' · ' + context + ' · ' + availability)}"
            )
        if highlighted:
            return f"  {ui.style('❯ ', *_PICKER_CURSOR)}{txt}"
        return f"    {txt}"

    def _picker_text() -> ANSI:
        query = picker.get("filter", "")
        search = query + "▏" if query else "Search models…"
        search_color = "38;2;205;212;232" if query else _DIMTXT
        lines = [
            f" {ui.style('⌕', '38;2;140;180;255')} {ui.style(search, search_color)}",
            f" {ui.style('─' * max(4, min(52, ui.content_width() - 6)), '38;2;48;55;78')}",
            "",
        ]
        lines.extend(
            _picker_row(txt, mid, i == picker["selected"])
            for i, (txt, mid) in enumerate(picker["rows"])
        )
        return ANSI("\n".join(lines))

    def _picker_cursor_position() -> Point:
        return Point(x=0, y=max(picker["selected"] + 3, 0))

    picker_window = Window(
        content=FormattedTextControl(
            _picker_text,
            focusable=False,
            get_cursor_position=_picker_cursor_position,
        ),
        wrap_lines=False,
        always_hide_cursor=True,
    )
    picker_footer = Window(
        content=FormattedTextControl(
            lambda: ANSI(ui.dim(_PICKER_HINT)),
            focusable=False,
        ),
        height=1,
    )
    picker_dialog = ConditionalContainer(
        content=Frame(
            HSplit([picker_window, Window(height=1), picker_footer]),
            title="Select model",
            style="class:picker",
            width=Dimension(min=1, preferred=64, max=72),
        ),
        filter=picker_active,
    )
    picker_centered = ConditionalContainer(
        content=HSplit([
            Window(char=" ", style="class:overlay"),
            VSplit(
                [
                    Window(char=" ", style="class:overlay"),
                    picker_dialog,
                    Window(char=" ", style="class:overlay"),
                ],
                height=Dimension(min=12, preferred=22, max=30),
            ),
            Window(char=" ", style="class:overlay"),
        ]),
        filter=picker_active,
    )

    body = FloatContainer(
        content=DynamicContainer(lambda: welcome_root if state["welcome"] else chat_root),
        # An explicit container style paints spacer windows and margins too.
        # A default theme alone leaves blank cells in the terminal's own color.
        style="class:surface",
        floats=[
            Float(xcursor=True, ycursor=True, content=CompletionsMenu(max_height=16, scroll_offset=1)),
            Float(left=0, right=0, top=0, bottom=0, content=picker_centered),
        ],
    )

    def _render_picker() -> None:
        ui._invalidate()

    def _apply_picker_filter(query: str) -> bool:
        """Rebuild model rows while preserving a useful active selection."""
        _headers, rows, shown_any = build_model_rows(state["model"], query, ui)
        picker["filter"] = query
        if not shown_any:
            picker["rows"] = [(ui.dim("No models match this search."), None)]
            picker["selectable"] = []
            picker["selected"] = 0
            _render_picker()
            return False

        selectable = [i for i, (_, mid) in enumerate(rows) if mid is not None]
        previous_id = None
        if picker["rows"] and picker["selected"] < len(picker["rows"]):
            previous_id = picker["rows"][picker["selected"]][1]
        picker["rows"] = rows
        picker["selectable"] = selectable
        picker["selected"] = next(
            (
                i
                for i in selectable
                if rows[i][1] in {previous_id, state["model"]}
            ),
            selectable[0],
        )
        picker_window.vertical_scroll = 0
        _render_picker()
        return True

    def _finalize_picker(chosen) -> None:
        ui._invalidate()

    def _picker_move(delta: int) -> None:
        sel = picker["selectable"]
        if not sel:
            return
        pos = sel.index(picker["selected"])
        picker["selected"] = sel[(pos + delta) % len(sel)]
        _render_picker()

    def _picker_resolve(model_id) -> None:
        fut = picker["future"]
        if fut is not None and not fut.done():
            fut.set_result(model_id)

    async def _run_model_picker(filt: str) -> None:
        picker["rows"] = []
        picker["selected"] = 0
        if not _apply_picker_filter(filt):
            ui.warning(f"No models match '{filt}'. Try a provider like 'openai', 'groq', 'anthropic'.")
            return

        picker["future"] = asyncio.get_event_loop().create_future()
        picker["active"] = True
        picker_window.vertical_scroll = 0
        _render_picker()
        try:
            chosen = await picker["future"]
        finally:
            picker["active"] = False
            picker["future"] = None

        input_buffer.reset()  # drop any stray keys that reached the input box
        _finalize_picker(chosen)
        if chosen is None:
            ui.info("No change — model selection cancelled.")
            return

        # Switch — with fully inline key entry if the model needs a key.
        state["model"] = await _switch_model_tui(chosen)

    # ── Inline API-key entry (native, in-pane) ────────────────────────────
    def _kp_resolve(value) -> None:
        fut = keyprompt["future"]
        if fut is not None and not fut.done():
            fut.set_result(value)

    async def _prompt_for_key(env_var: str) -> Optional[str]:
        """Masked key entry in the chat pane; returns the key or None if skipped."""
        ui.lines.append("")
        ui.lines.append(
            f"  {ui.style('?', '38;2;247;200;100')} Enter your "
            f"{ui.style(env_var, '1', '38;2;255;220;140')} "
            f"{ui.dim('(hidden — Enter to skip)')}"
        )
        input_buffer.reset()
        keyprompt["future"] = asyncio.get_event_loop().create_future()
        keyprompt["mode"] = "key"
        ui._invalidate()
        try:
            raw = await keyprompt["future"]
        finally:
            keyprompt["mode"] = None
            keyprompt["future"] = None
            input_buffer.reset()
        return raw.strip() or None

    async def _prompt_yes_no(
        question: str, yes_label: str = "y  save", no_label: str = "n  session only"
    ) -> bool:
        ui.lines.append(
            f"  {question}  "
            f"{ui.badge(yes_label, '17', '48;2;80;170;140')}  "
            f"{ui.badge(no_label, '17', '48;2;120;100;80')}"
        )
        keyprompt["future"] = asyncio.get_event_loop().create_future()
        keyprompt["mode"] = "yesno"
        ui._invalidate()
        try:
            return await keyprompt["future"]
        finally:
            keyprompt["mode"] = None
            keyprompt["future"] = None

    async def _switch_model_tui(raw_name: str) -> str:
        """
        TUI-native equivalent of chat._switch_model: same display, but the
        missing-key prompt and the save y/n happen inline in the chat pane
        (masked input + key bindings) instead of via getpass, so the
        full-screen UI is never suspended onto a bare terminal.
        """
        from apsara_cli.engine.models import resolve_model_id

        resolved = resolve_model_id(raw_name)
        entry = lookup_model(raw_name)
        if entry is None:
            ui.warning(
                f"'{raw_name}' is not in the built-in registry. Its pricing is unknown "
                "and the provider may bill requests."
            )
            if resolved != state["model"] and not await _prompt_yes_no(
                "Switch to this custom model?", "y  switch", "n  cancel"
            ):
                ui.info("Model switch cancelled — continuing with the current model.")
                return state["model"]
            if resolved != state["model"]:
                ui.info(f"Switched to {ui.style(resolved, '1', '38;2;188;218;255')}")
            _refresh_context_usage(ui, history, resolved)
            return resolved

        selectable, health_message = model_availability(entry)
        if not selectable:
            ui.error(health_message)
            return state["model"]
        if health_message:
            ui.warning(health_message)

        ctx = format_context_window(entry.context_window)
        ui.print_line()
        ui.print_line(
            f"  {ui.style('◆', '38;2;100;150;220')} "
            f"{ui.style(entry.display_name, '1', '38;2;220;225;240')}  "
            f"{ui.dim(entry.model_id)}  {ui.dim(ctx + ' ctx')}"
        )
        ui.print_line(f"  {ui.dim(model_price_label(entry.model_id))}")
        if entry.tier == "paid" and resolved != state["model"]:
            ui.warning(
                f"{entry.display_name} is a paid model. Requests may be billed by "
                f"{entry.provider.capitalize()} at its current rates."
            )
            confirmed = await _prompt_yes_no(
                "Switch to this paid model?", "y  switch", "n  cancel"
            )
            if not confirmed:
                ui.info("Model switch cancelled — continuing with the current model.")
                return state["model"]
        if entry.tier == "local":
            ui.print_line(f"  {ui.style('✓ local model — no API key required', '38;2;120;200;150')}")
        elif is_key_available(entry):
            ui.print_line(f"  {ui.style(f'✓ {entry.env_var} is set', '38;2;120;200;150')}")
        else:
            ui.print_line(
                f"  {ui.style('✗', '38;2;220;120;100')} "
                f"{ui.style(entry.env_var, '1', '38;2;255;220;140')} "
                f"{ui.style('is not set', '38;2;220;120;100')}"
            )
            raw_key = await _prompt_for_key(entry.env_var)
            if raw_key:
                from apsara_cli.engine.models import validate_key_format
                looks_valid, message = validate_key_format(entry.env_var, raw_key)
                if not looks_valid:
                    ui.warning(f"That doesn't match the usual format. {message}")
                    if not await _prompt_yes_no(
                        "Use this key anyway?", "y  use", "n  cancel"
                    ):
                        raw_key = None
            if raw_key:
                os.environ[entry.env_var] = raw_key
                ui.success(f"{entry.env_var} active for this session.")
                if await _prompt_yes_no("Save securely for future sessions?"):
                    try:
                        from apsara_cli.cli.auth import save_provider_key
                        save_provider_key(
                            entry.provider,
                            api_key=raw_key,
                            default_model=entry.model_id,
                        )
                        ui.success("Saved securely to ~/.apsara/credentials.json")
                    except Exception as exc:
                        ui.error(f"Could not save the provider key: {exc}")
                else:
                    ui.info("Key active for this session only — not saved to disk.")
            else:
                ui.warning(
                    f"No key entered. Use /key set {entry.provider} when you're ready."
                )

        if resolved != state["model"]:
            ui.info(f"Switched to {ui.style(resolved, '1', '38;2;188;218;255')}")
        _refresh_context_usage(ui, history, resolved)
        return resolved

    def _native_confirm(action: str, payload: dict[str, Any]) -> bool:
        """Block only the agent worker while the main TUI operates the dialog."""
        is_trust = action == "trust_workspace_code"
        allows_blanket = action_allows_blanket_approval(action)
        if ui.approve_all and allows_blanket:
            return True

        title, preview, diff_preview, diff_full, _diff_editor, _path_hint = describe_action(
            action, payload
        )
        done = threading.Event()
        approval.update({
            "active": True,
            "action": action.replace("_", " "),
            "title": title,
            "preview": diff_preview or preview or "",
            "full": diff_full or diff_preview or preview or "",
            "show_full": False,
            "result": "reject",
            "event": done,
            "is_trust": is_trust,
            "allow_always": allows_blanket,
        })
        approval_window.vertical_scroll = 0
        ui.stop_spinner()
        ui._invalidate()
        done.wait()

        result = approval["result"]
        if result == "always" and allows_blanket:
            ui.approve_all = True
        ui.start_spinner("Apsara is working")
        return result in {"approve", "always"}

    kb = KeyBindings()

    @kb.add("c-c", filter=~approval_active & ~picker_active & ~kp_any)
    def _interrupt(event) -> None:
        if state["busy"]:
            if turn_controller.cancel():
                ui.warning("Cancelling the active turn…")
            return
        event.app.exit()

    @kb.add("c-d", filter=~approval_active)
    def _eof(event) -> None:
        event.app.exit()

    @kb.add("escape", "enter", filter=~picker_active & ~kp_any & ~approval_active)
    def _newline(event) -> None:
        event.current_buffer.insert_text("\n")

    # ── Native action-review overlay ──────────────────────────────────────
    def _approval_resolve(result: str) -> None:
        approval["result"] = result
        approval["active"] = False
        done = approval.get("event")
        if done is not None:
            done.set()
        ui._invalidate()

    @kb.add("enter", filter=approval_active, eager=True)
    @kb.add("y", filter=approval_active, eager=True)
    @kb.add("Y", filter=approval_active, eager=True)
    def _approval_accept(event) -> None:
        _approval_resolve("approve")

    @kb.add("a", filter=approval_active, eager=True)
    @kb.add("A", filter=approval_active, eager=True)
    def _approval_always(event) -> None:
        if approval.get("allow_always"):
            _approval_resolve("always")

    @kb.add("n", filter=approval_active, eager=True)
    @kb.add("N", filter=approval_active, eager=True)
    @kb.add("q", filter=approval_active, eager=True)
    @kb.add("escape", filter=approval_active, eager=True)
    @kb.add("c-c", filter=approval_active, eager=True)
    def _approval_reject(event) -> None:
        _approval_resolve("reject")

    @kb.add("v", filter=approval_active, eager=True)
    @kb.add("V", filter=approval_active, eager=True)
    def _approval_toggle_full(event) -> None:
        if approval.get("full") and approval.get("full") != approval.get("preview"):
            approval["show_full"] = not approval["show_full"]
            approval_window.vertical_scroll = 0
            ui._invalidate()

    @kb.add("up", filter=approval_active, eager=True)
    @kb.add("k", filter=approval_active, eager=True)
    def _approval_scroll_up(event) -> None:
        approval_window.vertical_scroll = max(approval_window.vertical_scroll - 1, 0)

    @kb.add("down", filter=approval_active, eager=True)
    @kb.add("j", filter=approval_active, eager=True)
    def _approval_scroll_down(event) -> None:
        approval_window.vertical_scroll += 1

    @kb.add("pageup", filter=approval_active, eager=True)
    def _approval_page_up(event) -> None:
        approval_window.vertical_scroll = max(approval_window.vertical_scroll - 10, 0)

    @kb.add("pagedown", filter=approval_active, eager=True)
    def _approval_page_down(event) -> None:
        approval_window.vertical_scroll += 10

    # ── /models picker navigation (only while the picker is open) ─────────
    @kb.add("up", filter=picker_active, eager=True)
    def _picker_up(event) -> None:
        _picker_move(-1)

    @kb.add("down", filter=picker_active, eager=True)
    def _picker_down(event) -> None:
        _picker_move(1)

    @kb.add("enter", filter=picker_active, eager=True)
    def _picker_accept(event) -> None:
        if picker["selectable"]:
            _picker_resolve(picker["rows"][picker["selected"]][1])

    @kb.add("escape", filter=picker_active, eager=True)
    @kb.add("c-c", filter=picker_active, eager=True)
    def _picker_cancel(event) -> None:
        _picker_resolve(None)

    @kb.add("backspace", filter=picker_active, eager=True)
    def _picker_backspace(event) -> None:
        _apply_picker_filter(picker["filter"][:-1])

    @kb.add(Keys.Any, filter=picker_active, eager=True, is_global=True)
    def _picker_search(event) -> None:
        char = event.data
        if char and char.isprintable():
            _apply_picker_filter(picker["filter"] + char)

    # ── Inline key-prompt bindings (only while a key prompt is open) ──────
    @kb.add("enter", filter=kp_key, eager=True)
    def _kp_key_submit(event) -> None:
        text = input_buffer.text
        input_buffer.reset()
        _kp_resolve(text)

    @kb.add("escape", filter=kp_key, eager=True)
    def _kp_key_skip(event) -> None:
        input_buffer.reset()
        _kp_resolve("")  # empty → skipped

    @kb.add("y", filter=kp_yesno, eager=True)
    @kb.add("Y", filter=kp_yesno, eager=True)
    @kb.add("enter", filter=kp_yesno, eager=True)
    def _kp_yes(event) -> None:
        _kp_resolve(True)

    @kb.add("n", filter=kp_yesno, eager=True)
    @kb.add("N", filter=kp_yesno, eager=True)
    @kb.add("escape", filter=kp_yesno, eager=True)
    def _kp_no(event) -> None:
        _kp_resolve(False)

    @kb.add("c-p")
    def _command_palette(event) -> None:
        """ctrl+p: browse every slash command in the completion menu."""
        if not input_buffer.text.startswith("/"):
            input_buffer.reset()
            input_buffer.insert_text("/")
        input_buffer.start_completion(select_first=False)

    @kb.add("c-b")
    def _toggle_sidebar(event) -> None:
        """Show details on demand without permanently shrinking the chat."""
        sidebar_state["visible"] = not sidebar_state["visible"]
        ui.sidebar_visible = sidebar_state["visible"]
        event.app.invalidate()

    @kb.add("c-up")
    def _scroll_sidebar_up(event) -> None:
        """Scroll the workspace details independently of the transcript."""
        if not sidebar_state["visible"]:
            return
        sidebar_window.vertical_scroll = max(sidebar_window.vertical_scroll - 1, 0)
        event.app.invalidate()

    @kb.add("c-down")
    def _scroll_sidebar_down(event) -> None:
        if not sidebar_state["visible"]:
            return
        info = sidebar_window.render_info
        max_scroll = max(info.content_height - info.window_height, 0) if info else 0
        sidebar_window.vertical_scroll = min(sidebar_window.vertical_scroll + 1, max_scroll)
        event.app.invalidate()

    @kb.add("pageup")
    def _scroll_up(event) -> None:
        ri = chat_window.render_info
        page = max((ri.window_height if ri else 10) - 1, 1)
        follow["on"] = False
        chat_window.vertical_scroll = max(chat_window.vertical_scroll - page, 0)

    @kb.add("pagedown")
    def _scroll_down(event) -> None:
        ri = chat_window.render_info
        page = max((ri.window_height if ri else 10) - 1, 1)
        max_scroll = _chat_max_scroll()
        new_scroll = min(chat_window.vertical_scroll + page, max_scroll)
        chat_window.vertical_scroll = new_scroll
        if new_scroll >= max_scroll:
            follow["on"] = True
            follow["last_set"] = new_scroll

    async def _handle_submission(text: str) -> None:
        try:
            ui.append_user_message(text)

            # /models needs the native async picker (not the blocking pick_model
            # inside handle_chat_command, which can't read keys under the TUI).
            if text == "/models" or text.startswith("/models "):
                await _run_model_picker(text[len("/models"):].strip().lower())
                return

            if text.startswith("/model "):
                raw_name = text[len("/model "):].strip()
                if not raw_name:
                    ui.error("Usage: /model <model-id-or-alias>")
                    return
                state["model"] = await _switch_model_tui(raw_name)
                return

            if text.startswith("/"):
                keep, new_model = handle_chat_command(
                    text, history, state["model"], options, config, ui
                )
                state["model"] = new_model
                if not keep:
                    app.exit()
                return

            # Bare `apsara` should be the complete onboarding path. Prompt for
            # the default model's key inside the TUI on first use instead of
            # requiring a separate `apsara login` step.
            if _model_needs_key(state["model"]):
                state["model"] = await _switch_model_tui(state["model"])
                if _model_needs_key(state["model"]):
                    ui.warning("A provider key is required before this request can run.")
                    return

            # execute_instruction drives begin_turn/finish_turn, which renders
            # the '+ Thought:' marker and preserves details for /details.
            def _run_agent_turn():
                return turn_controller.run(
                    execute_instruction(text, state["model"], list(history), options, ui)
                )

            try:
                new_history, usage = await asyncio.to_thread(_run_agent_turn)
            except asyncio.CancelledError:
                ui.stop_spinner()
                ui.warning("Turn cancelled. Your previous conversation and file checkpoints are preserved.")
                return
            history[:] = new_history
            if usage and usage.get("total_tokens") is not None:
                ui.usage(usage)
            save_if_needed(history, state["model"], options, ui)
        finally:
            state["busy"] = False
            ui._invalidate()

    @kb.add(
        "enter",
        filter=(
            Condition(lambda: not input_buffer.complete_state)
            & ~picker_active
            & ~kp_any
            & ~approval_active
        ),
    )
    def _submit(event) -> None:
        text = input_buffer.text.strip()
        input_buffer.reset()
        if not text:
            return
        if state["busy"]:
            ui.warning("Apsara is still working. Wait for this turn to finish before sending another.")
            return
        if state["welcome"]:
            # First message: swap the centered welcome for the chat layout.
            state["welcome"] = False
            event.app.layout.focus(chat_input_window)
        state["busy"] = True
        event.app.create_background_task(_handle_submission(text))

    app = Application(
        layout=Layout(
            body,
            focused_element=welcome_input_window if state["welcome"] else chat_input_window,
        ),
        key_bindings=kb,
        style=style,
        color_depth=color_depth_for_ui(options.use_color),
        full_screen=True,
        mouse_support=True,
        before_render=_before_render,
        after_render=_after_render,
    )
    ui.attach(app)
    ui.native_confirmation_handler = _native_confirm

    if history:
        _restore_history(ui, history)

    await app.run_async()
    return 0
