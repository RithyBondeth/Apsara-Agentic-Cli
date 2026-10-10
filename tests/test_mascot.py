"""The mascot must reflect real outcomes and remain safe for terminal layout."""

import hashlib
import re
from importlib.resources import files
from types import SimpleNamespace

import pytest
from prompt_toolkit.utils import get_cwidth

from apsara_cli.config.cli_config import load_cli_config
from apsara_cli.shared.mascot import Makor, artwork


def test_bundled_sprite_matches_the_approved_artwork():
    asset = artwork()
    data = files("apsara_cli.shared").joinpath("assets/makor.png").read_bytes()
    assert hashlib.sha256(data).hexdigest() == asset["source_sha256"]
    assert len(asset["pixels"]) == asset["height"]
    assert all(len(row) == asset["width"] for row in asset["pixels"])
    assert set("".join(asset["pixels"])) <= set(asset["palette"]) | {" "}


def test_activity_follows_tools_streaming_retries_and_verification():
    mascot = Makor()
    mascot.begin_turn()
    assert mascot.state == "thinking"
    for event, state in [
        ({"type": "tool_call", "name": "read_file"}, "working"),
        ({"type": "tool_result"}, "thinking"),
        ({"type": "response_start"}, "speaking"),
        ({"type": "text_chunk", "content": "hello"}, "speaking"),
        ({"type": "response_end"}, "thinking"),
        ({"type": "retry_notice"}, "retrying"),
        ({"type": "assistant_dispatch"}, "thinking"),
        ({"type": "tool_call", "name": "verify_project"}, "verifying"),
        ({"type": "run_state", "state": "verifying"}, "verifying"),
    ]:
        mascot.observe(event)
        assert mascot.state == state
    mascot.set_state("waiting")
    assert mascot.label() == "Makor · Awaiting approval"


@pytest.mark.parametrize("outcome,expected", [
    ("completed_verified", "success"), ("completed_unverified", "unverified"),
    ("failed", "error"), ("blocked", "blocked"), ("cancelled", "cancelled"),
])
def test_final_outcome_survives_final_answer_and_next_turn_resets(outcome, expected):
    mascot = Makor()
    mascot.begin_turn()
    mascot.observe({"type": "run_state", "state": outcome})
    mascot.observe({"type": "final_answer", "content": "Done"})
    mascot.observe({"type": "response_end"})
    mascot.finish_turn()
    assert mascot.state == expected
    mascot.begin_turn()
    assert mascot.state == "thinking"
    mascot.observe({"type": "tool_call", "name": "read_file"})
    assert mascot.state == "working"


def test_finish_and_interrupt_without_terminal_event():
    mascot = Makor()
    mascot.begin_turn()
    mascot.finish_turn("error", "running")
    assert mascot.state == "error"
    mascot.begin_turn()
    mascot.set_state("cancelled")
    mascot.finish_turn("", "running")
    assert mascot.state == "cancelled"
    mascot.begin_turn()
    mascot.finish_turn("", "completed_unverified")
    assert mascot.label() == "Makor · Needs verification"


@pytest.mark.parametrize("width", [6, 16, 24, 32, 40, 64, 96])
@pytest.mark.parametrize("color,ascii_only", [(True, False), (False, False), (False, True)])
def test_frames_fit_the_terminal_and_keep_constant_dimensions(width, color, ascii_only):
    clock = [0.0]
    mascot = Makor(clock=lambda: clock[0])
    for state in ("idle", "thinking", "working", "verifying", "waiting", "success", "error"):
        mascot.begin_turn()
        mascot.set_state(state)
        for elapsed in (0, .25, .5, .75, 3.75):
            clock[0] = elapsed
            lines = mascot.render(width, color=color, ascii_only=ascii_only)
            assert len(lines) == mascot.height(width)
            for line in lines:
                plain = re.sub(r"\x1b\[[0-9;]*m", "", line)
                assert get_cwidth(plain) == width
                if not color:
                    assert "\x1b" not in line
                if ascii_only:
                    assert plain.isascii()


def test_motion_can_stop_and_success_celebration_settles():
    clock = [0.0]
    mascot = Makor(clock=lambda: clock[0])
    mascot.begin_turn()
    frame = mascot.render(40)
    clock[0] = .25
    assert mascot.render(40) != frame
    mascot.animation = False
    assert mascot.render(40) == frame
    clock[0] = 1
    assert mascot.render(40) == frame
    mascot.animation = True
    mascot.set_state("success")
    resting = mascot.render(40)
    clock[0] += .25
    assert mascot.render(40) != resting
    clock[0] += 4
    assert mascot.render(40) == resting


@pytest.mark.parametrize("color,ascii_only", [(True, False), (False, False), (False, True)])
def test_inline_head_keeps_two_rows_and_blinks_without_moving_text(color, ascii_only):
    clock = [0.0]
    mascot = Makor(clock=lambda: clock[0])
    mascot.begin_turn()
    first = mascot.render_head(color=color, ascii_only=ascii_only)
    for elapsed in (.5, 3.75):
        clock[0] = elapsed
        frame = mascot.render_head(color=color, ascii_only=ascii_only)
        assert len(frame) == 2
        assert all(get_cwidth(re.sub(r"\x1b\[[0-9;]*m", "", line)) == 5 for line in frame)
        assert frame != first
        if not color:
            assert all("\x1b" not in line for line in frame)
        if ascii_only:
            assert all(line.isascii() for line in frame)
    mascot.animation = False
    assert mascot.render_head(color=color, ascii_only=ascii_only) == first


def test_head_beside_activity_text_and_classic_spinner_cleanup(monkeypatch, capsys):
    from apsara_cli.shared.ui import ConsoleUI
    ui = ConsoleUI(use_color=False)
    ui.begin_turn()
    lines = ui.compose_activity_lines()
    assert len(lines) == 2
    assert lines[1].index("Makor is thinking") == 9
    assert "Makor is thinking" not in lines[0]
    ui.set_mascot_state("waiting")
    assert "Awaiting approval" in ui.compose_activity_lines()[1]
    ui.mascot.enabled = False
    assert len(ui.compose_activity_lines()) == 1
    ui.mascot.enabled = True
    ui.spinner_stop_event.clear()
    monkeypatch.setattr(ui.spinner_stop_event, "wait", lambda timeout: True)
    ui._spinner_worker()
    assert ui._spinner_rows == 2
    ui.spinner_thread = SimpleNamespace(join=lambda timeout: None)
    ui.stop_spinner()
    emitted = capsys.readouterr().out
    assert "\x1b[1A" in emitted  # Rewind to the head's first row, then clear both rows.
    assert ui._spinner_rows == 0


def test_thinking_loading_pulse_repeats_at_a_fixed_small_size():
    clock = [0.0]
    mascot = Makor(clock=lambda: clock[0])
    mascot.begin_turn()
    frames = []
    for elapsed in (0, .25, .5, .75, 1):
        clock[0] = elapsed
        frames.append(mascot.render_head())
    assert len(set(frames[:4])) == 4
    assert frames[4] == frames[0]
    assert all(len(frame) == 2 for frame in frames)


def test_mascot_preferences_and_noninteractive_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    path = tmp_path / "config.toml"
    path.write_text("[ui]\nmascot = false\nmascot_animation = false\n")
    config = load_cli_config(str(path), str(tmp_path))
    mascot = Makor()
    mascot.configure(config.ui)
    assert not mascot.enabled and not mascot.animation
    mascot.configure(SimpleNamespace())
    assert mascot.enabled and mascot.animation
    monkeypatch.setenv("CI", "1")
    mascot.configure(SimpleNamespace(mascot_animation=True))
    assert not mascot.animation
    monkeypatch.delenv("CI")
    monkeypatch.setenv("TERM", "dumb")
    mascot.configure(SimpleNamespace())
    assert not mascot.animation
