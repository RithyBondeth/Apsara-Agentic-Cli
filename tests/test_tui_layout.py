import asyncio
from io import StringIO
import pytest

from prompt_toolkit.application import Application, create_app_session
from prompt_toolkit.data_structures import Point, Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output.vt100 import Vt100_Output
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.utils import get_cwidth

from apsara_cli.cli import tui
from apsara_cli.cli.parser import build_parser
from apsara_cli.config.cli_config import load_cli_config


@pytest.mark.parametrize("lines,cursor_row", [(2, 79), (1, 40)])
def test_scroll_cursor_stays_inside_captured_text_snapshot(lines, cursor_row):
    # Output grows between fragment capture and the cursor callback, or the
    # sidebar shrinks while retaining an older mouse-scroll position.
    control = tui._ScrollTextControl(
        "\n".join(f"line {i}" for i in range(lines)),
        get_cursor_position=lambda: Point(x=0, y=cursor_row),
    )
    content = control.create_content(80, 24)
    from prompt_toolkit.layout import Window
    window = Window(content=control, wrap_lines=True)
    window._scroll(content, 80, 24)
    assert content.cursor_position.y == lines - 1
    assert content.get_line(content.cursor_position.y)


class _BackgroundOutput(DummyOutput):
    """Track painted cells against a non-black terminal profile background."""

    def __init__(self, columns, rows):
        self.columns, self.rows = columns, rows
        self.x = self.y = 0
        self.background = "terminal-profile"
        self.erase_down()

    def get_size(self):
        return Size(rows=self.rows, columns=self.columns)

    def set_attributes(self, attrs, color_depth):
        self.background = attrs.bgcolor or "terminal-profile"

    def reset_attributes(self):
        self.background = "terminal-profile"

    def erase_down(self):
        self.cells = [["terminal-profile"] * self.columns for _ in range(self.rows)]

    def write(self, text):
        for char in text:
            if char == "\r":
                self.x = 0
            elif char == "\n":
                self.y += 1
            else:
                width = get_cwidth(char)
                for offset in range(width):
                    if self.y < self.rows and self.x + offset < self.columns:
                        self.cells[self.y][self.x + offset] = self.background
                self.x += width

    def cursor_up(self, amount):
        self.y = max(0, self.y - amount)

    def cursor_forward(self, amount):
        self.x += amount

    def cursor_backward(self, amount):
        self.x = max(0, self.x - amount)


@pytest.mark.parametrize("columns,rows", [(133, 45), (80, 24)])
def test_welcome_paints_blank_regions_over_a_non_black_terminal(monkeypatch, tmp_path, columns, rows):
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(tui.Path, "home", lambda: tmp_path)
    monkeypatch.setattr("apsara_cli.cli.chat._load_stored_keys", lambda: None)
    captured = {}

    def make_app(**kwargs):
        captured["app"] = Application(**kwargs)
        return captured["app"]

    monkeypatch.setattr(tui, "Application", make_app)
    args = build_parser().parse_args(["chat", "--workspace", str(tmp_path), "--stateless", "--color"])
    config = load_cli_config(str(tmp_path / "config.toml"), str(tmp_path))

    async def exercise():
        output = _BackgroundOutput(columns, rows)
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
            task = asyncio.create_task(tui.tui_loop(args, config))
            try:
                for _ in range(100):
                    if "app" in captured and captured["app"].renderer._last_screen is not None:
                        break
                    await asyncio.sleep(.01)
                assert all(bg != "terminal-profile" for row in output.cells for bg in row)
                assert output.cells[0][0] == "0a0b10"
                assert output.cells[rows - 2][columns - 1] == "0a0b10"
                output.columns, output.rows = ((80, 24) if columns > 80 else (133, 45))
                captured["app"].invalidate()
                for _ in range(100):
                    if len(output.cells) == output.rows and len(output.cells[0]) == output.columns:
                        break
                    await asyncio.sleep(.01)
                assert len(output.cells) == output.rows
                assert len(output.cells[0]) == output.columns
                assert all(bg != "terminal-profile" for row in output.cells for bg in row)
            finally:
                pipe.send_text("\x04")
                assert await asyncio.wait_for(task, 2) == 0

    asyncio.run(exercise())


def test_composer_keeps_focus_across_submission_sidebar_toggle_and_resize(monkeypatch, tmp_path):
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("OPENCODE_API_KEY", "layout-test")
    monkeypatch.setattr(tui.Path, "home", lambda: tmp_path)
    monkeypatch.setattr("apsara_cli.cli.chat._load_stored_keys", lambda: None)
    captured = {}

    def make_app(**kwargs):
        captured["app"] = Application(**kwargs)
        return captured["app"]

    async def execute(instruction, model, history, options, ui):
        ui.assistant("Layout test response\n\n" + "\n".join(f"- line {i}" for i in range(80)))
        return [*history, {"role": "user", "content": instruction},
                {"role": "assistant", "content": "Layout test response"}], None

    monkeypatch.setattr(tui, "Application", make_app)
    monkeypatch.setattr(tui, "execute_instruction", execute)
    args = build_parser().parse_args([
        "chat", "--workspace", str(tmp_path), "--model", "bunny", "--stateless",
    ])
    config = load_cli_config(str(tmp_path / "config.toml"), str(tmp_path))

    async def wait_for(predicate):
        for _ in range(100):
            if predicate():
                return
            await asyncio.sleep(.01)
        raise AssertionError("UI did not reach expected state")

    def rows():
        screen = captured["app"].renderer._last_screen
        return ["".join(c.char for _, c in sorted(row.items()))
                for _, row in sorted(screen.data_buffer.items())] if screen else []

    async def exercise():
        size = {"rows": 36, "columns": 144}
        output = Vt100_Output(StringIO(), lambda: Size(**size), enable_cpr=False)
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
            task = asyncio.create_task(tui.tui_loop(args, config))
            await wait_for(lambda: "app" in captured and any("OpenCode Zen" in r for r in rows()))
            pipe.send_text("Explain the code\r")
            await wait_for(lambda: any("line 79" in r for r in rows()))
            app = captured["app"]
            composer_row = next(r for r in rows() if "Build · Space Bunny Free" in r)
            assert composer_row.index("Space Bunny Free") < 100
            assert "│" in composer_row  # sidebar extends alongside the composer

            pipe.send_text("pending draft")
            await wait_for(lambda: app.current_buffer.text == "pending draft")
            pipe.send_text("\x1b[5~")  # PageUp browses output without moving input focus.
            await wait_for(lambda: not any("line 79" in r for r in rows()))
            assert app.current_buffer.text == "pending draft"
            pipe.send_text("\x1b[6~")
            await wait_for(lambda: any("line 79" in r for r in rows()))
            pipe.send_text("\x02")  # Ctrl+B toggles details while preserving the draft.
            await wait_for(lambda: not any("◍ Context" in r for r in rows()))
            assert app.current_buffer.text == "pending draft"

            pipe.send_text("\x02")
            await wait_for(lambda: any("◍ Context" in r for r in rows()))
            size["columns"] = 80
            app.invalidate()
            await wait_for(lambda: not any("◍ Context" in r for r in rows()))
            assert app.current_buffer.text == "pending draft"
            assert any("OpenCode Zen" in r for r in rows())
            pipe.send_text("\x04")
            assert await asyncio.wait_for(task, 2) == 0

    asyncio.run(exercise())


@pytest.mark.parametrize("flag,colored", [("--color", True), ("--no-color", False)])
def test_tui_color_choice_controls_rendered_output_with_no_color_env(monkeypatch, tmp_path, flag, colored):
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLORTERM", "truecolor")
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(tui.Path, "home", lambda: tmp_path)
    monkeypatch.setattr("apsara_cli.cli.chat._load_stored_keys", lambda: None)
    captured = {}

    def make_app(**kwargs):
        captured["app"] = Application(**kwargs)
        return captured["app"]

    monkeypatch.setattr(tui, "Application", make_app)
    args = build_parser().parse_args(["chat", "--workspace", str(tmp_path), "--stateless", flag])
    config = load_cli_config(str(tmp_path / "config.toml"), str(tmp_path))

    async def exercise():
        stream = StringIO()
        output = Vt100_Output(stream, lambda: Size(rows=36, columns=120), enable_cpr=False)
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
            task = asyncio.create_task(tui.tui_loop(args, config))
            for _ in range(100):
                if "app" in captured and captured["app"].renderer._last_screen is not None:
                    break
                await asyncio.sleep(.01)
            emitted = stream.getvalue()
            assert bool("38;2;" in emitted or "48;2;" in emitted) is colored
            pipe.send_text("\x04")
            assert await asyncio.wait_for(task, 2) == 0

    asyncio.run(exercise())
