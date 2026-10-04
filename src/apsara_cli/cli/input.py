import asyncio
import os
from pathlib import Path
from typing import Optional

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.styles import Style
    from prompt_toolkit.output.color_depth import ColorDepth
    from prompt_toolkit.filters import Condition, is_done
    from prompt_toolkit.layout import HSplit, Layout, Window, ConditionalContainer
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.layout.dimension import Dimension
    HAS_PROMPT_TOOLKIT = True
except ImportError:
    HAS_PROMPT_TOOLKIT = False

_KEY_PROVIDERS = ["openai", "anthropic", "groq", "google", "mistral", "deepseek"]
_ALL_PROVIDERS = _KEY_PROVIDERS + ["ollama"]

_TOP_LEVEL = [
    "/help", "/details", "/clear", "/history", "/tools", "/skills", "/add", "/bug",
    "/status", "/model", "/models", "/key", "/session", "/save",
    "/sessions", "/usage", "/diff", "/turns", "/undo-turn", "/checkpoints", "/undo", "/processes", "/logs", "/stop",
    "/report", "/memory", "/exit", "/quit",
]

_SUB_COMMANDS: dict[str, list[str]] = {
    "/key": ["list", "set", "remove", *[f"set {p}" for p in _KEY_PROVIDERS], *[f"remove {p}" for p in _KEY_PROVIDERS]],
    "/models": list(_ALL_PROVIDERS),
    "/sessions": ["clear"],
    "/memory": ["show", "add"],
    "/bug": ["--include-content"],
}


class SlashCompleter(Completer):
    """Progressive slash-command completer.

    Typing ``/`` shows only top-level commands.  After a known prefix
    (e.g. ``/key ``) the sub-commands for that group appear instead.
    """

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        # Only complete when the user is typing a slash command.
        if not text.startswith("/"):
            return

        word = document.get_word_before_cursor(WORD=True)

        # Decide whether to show top-level or sub-commands.
        parts = text.split(None, 1)  # e.g. ["/key", "list"]
        if len(parts) == 1 or (len(parts) == 2 and not text.endswith(" ")):
            # Still on the first token — show top-level (or sub-commands
            # that share the prefix, e.g. typing "/ke" matches "/key").
            prefix = parts[0]
            for cmd in _TOP_LEVEL:
                if cmd.startswith(prefix) and cmd != prefix:
                    yield Completion(cmd, -len(word), display=cmd)
        else:
            # After the first token — check if it's a known group.
            parent = parts[0]
            sub_commands = _SUB_COMMANDS.get(parent)
            if sub_commands is None:
                return
            suffix = parts[1] if len(parts) > 1 else ""
            for sub in sub_commands:
                if sub.startswith(suffix) and sub != suffix:
                    yield Completion(sub, -len(word), display=sub)

_session: Optional[object] = None
_input_chrome: dict[str, object] = {"footer": None, "continuation": "  ▌ "}


def color_depth_for_ui(use_color: Optional[bool]):
    """Let an explicit color choice override inherited NO_COLOR for the renderer."""
    if use_color is None or not HAS_PROMPT_TOOLKIT:
        return None
    if not use_color:
        return ColorDepth.DEPTH_1_BIT
    if os.environ.get("COLORTERM", "").lower() in {"truecolor", "24bit"}:
        return ColorDepth.DEPTH_24_BIT
    return ColorDepth.DEPTH_8_BIT


def _build_session(workspace_root: Path) -> object:
    history_dir = Path.home() / ".apsara"
    history_dir.mkdir(parents=True, exist_ok=True)

    completer = SlashCompleter()

    kb = KeyBindings()

    @kb.add("enter")
    def _submit(event):
        event.current_buffer.validate_and_handle()

    @kb.add("escape", "enter")
    def _newline(event):
        event.current_buffer.insert_text("\n")

    @kb.add("c-p")
    def _command_palette(event):
        """ctrl+p: browse every slash command in the completion menu."""
        buffer = event.current_buffer
        if not buffer.text.startswith("/"):
            buffer.text = "/"
            buffer.cursor_position = 1
        buffer.start_completion(select_first=False)

    session = PromptSession(
        history=FileHistory(str(history_dir / "input_history")),
        auto_suggest=AutoSuggestFromHistory(),
        completer=completer,
        complete_while_typing=True,
        key_bindings=kb,
        multiline=True,
        prompt_continuation=lambda width, line_number, is_soft_wrap: ANSI(
            str(_input_chrome["continuation"])
        ),
        reserve_space_for_menu=0,
        style=Style.from_dict({
            "auto-suggestion": "fg:#6f7483",
            "completion-menu.completion": "bg:#161a24 fg:#c4cee0",
            "completion-menu.completion.current": "bg:#253451 fg:#ffffff",
        }),
    )
    # Keep model/status directly below the composer without reserving the
    # terminal's bottom row. Retain PromptSession's editing, search and menus.
    original_layout = session.app.layout
    # Make room for an open completion menu, but keep idle input compact.
    original_layout.current_window.height = lambda: Dimension(
        min=8 if session.default_buffer.complete_state is not None else 1
    )
    footer = ConditionalContainer(
        Window(
            FormattedTextControl(lambda: ANSI(str(_input_chrome["footer"] or ""))),
            height=1,
            dont_extend_height=True,
        ),
        filter=Condition(lambda: _input_chrome["footer"] is not None) & ~is_done,
    )
    session.app.layout = Layout(
        HSplit([original_layout.container, footer]),
        focused_element=original_layout.current_control,
    )
    return session


async def get_password_async(prompt_text: str) -> str:
    """
    Read a masked secret (API key) using prompt_toolkit's password mode.
    Characters are hidden as the user types. Falls back to getpass when
    prompt_toolkit is unavailable.
    """
    if not HAS_PROMPT_TOOLKIT:
        import getpass as _gp
        return _gp.getpass(prompt_text)
    from prompt_toolkit import PromptSession as _PS
    from prompt_toolkit.formatted_text import ANSI as _ANSI
    session = _PS()
    try:
        return await session.prompt_async(_ANSI(prompt_text), is_password=True)
    except (EOFError, KeyboardInterrupt):
        return ""


async def get_input_async(
    prompt_text: str,
    workspace_root: Path,
    toolbar: Optional[str] = None,
    continuation: Optional[str] = None,
    use_color: Optional[bool] = None,
) -> str:
    """
    Async input using prompt_toolkit's prompt_async() so it doesn't conflict
    with the outer asyncio event loop started by asyncio.run() in parser.py.
    `toolbar` renders inline below the input while typing. Falls back to a stdin read
    when prompt_toolkit is unavailable. Raises KeyboardInterrupt or EOFError.
    """
    if not HAS_PROMPT_TOOLKIT:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, lambda: input(prompt_text))
        if toolbar:
            print(toolbar)
        return result

    global _session
    if _session is None:
        _session = _build_session(workspace_root)

    _input_chrome["footer"] = toolbar
    _input_chrome["continuation"] = continuation or "  ▌ "
    return await _session.prompt_async(
        ANSI(prompt_text),
        color_depth=color_depth_for_ui(use_color),
    )
