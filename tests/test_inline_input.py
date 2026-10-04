import asyncio
from io import StringIO

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output.vt100 import Vt100_Output
from prompt_toolkit.data_structures import Size

from apsara_cli.cli import input as chat_input


async def _wait_for_render(session):
    for _ in range(100):
        if session.app.is_running and session.app.renderer._last_screen is not None:
            return session.app.renderer._last_screen
        await asyncio.sleep(0.01)
    raise AssertionError("Composer did not render")


def _output():
    return Vt100_Output(StringIO(), lambda: Size(rows=24, columns=80), enable_cpr=False)


def test_inline_composer_preserves_multiline_and_command_completion(monkeypatch, tmp_path):
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(chat_input.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(chat_input, "_session", None)

    async def exercise():
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=_output()):
            task = asyncio.create_task(chat_input.get_input_async(
                "\n  ─────────────\n  ▌ ", tmp_path,
                toolbar="  ▌ Build · Space Bunny Free", continuation="  ▌ ",
            ))
            for _ in range(100):
                if chat_input._session is not None:
                    break
                await asyncio.sleep(0.01)
            session = chat_input._session
            screen = await _wait_for_render(session)
            rows = ["".join(cell.char for _, cell in sorted(row.items()))
                    for _, row in sorted(screen.data_buffer.items())]
            footer_row = next(i for i, row in enumerate(rows) if "Build · Space Bunny Free" in row)
            assert footer_row == 3  # directly below the input, rather than row 23
            pipe.send_text("\x10")  # Ctrl+P opens slash commands.
            for _ in range(100):
                state = session.default_buffer.complete_state
                if state is not None:
                    break
                await asyncio.sleep(0.01)
            assert "/status" in [c.text for c in state.completions]
            session.default_buffer.reset()
            pipe.send_text("first\x1b\rsecond\r")
            assert await asyncio.wait_for(task, 2) == "first\nsecond"

    asyncio.run(exercise())


@pytest.mark.parametrize("colored", [True, False])
def test_inline_color_choice_overrides_inherited_no_color(monkeypatch, tmp_path, colored):
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLORTERM", "truecolor")
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(chat_input.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(chat_input, "_session", None)

    async def exercise():
        stream = StringIO()
        output = Vt100_Output(stream, lambda: Size(rows=24, columns=80), enable_cpr=False)
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
            task = asyncio.create_task(chat_input.get_input_async(
                "\033[38;2;96;150;250m▌\033[0m ", tmp_path, use_color=colored,
            ))
            for _ in range(100):
                if chat_input._session is not None:
                    break
                await asyncio.sleep(.01)
            await _wait_for_render(chat_input._session)
            assert ("38;2;" in stream.getvalue()) is colored
            pipe.send_text("/exit\r")
            assert await asyncio.wait_for(task, 2) == "/exit"

    asyncio.run(exercise())


@pytest.mark.parametrize("keys,exception", [("\x03", KeyboardInterrupt), ("\x04", EOFError)])
def test_inline_composer_retains_cancel_and_exit(monkeypatch, tmp_path, keys, exception):
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(chat_input.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(chat_input, "_session", None)

    async def exercise():
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=_output()):
            async def read():
                try:
                    await chat_input.get_input_async("  ▌ ", tmp_path, toolbar="  Build")
                except (KeyboardInterrupt, EOFError) as exc:
                    return type(exc)
                raise AssertionError("Expected input to stop")

            task = asyncio.create_task(read())
            for _ in range(100):
                if chat_input._session is not None:
                    break
                await asyncio.sleep(0.01)
            await _wait_for_render(chat_input._session)
            pipe.send_text(keys)
            assert await asyncio.wait_for(task, 2) is exception

    asyncio.run(exercise())
