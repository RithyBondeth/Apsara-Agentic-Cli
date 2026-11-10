"""Makor's event-driven personality and renderer for ordinary text terminals."""

import json
import os
import sys
import threading
import time
from functools import lru_cache
from importlib.resources import files
from typing import Any, Callable

_LABELS = {
    "idle": "Ready when you are", "thinking": "Thinking", "working": "Using tools",
    "verifying": "Checking changes", "speaking": "Responding", "waiting": "Awaiting approval",
    "retrying": "Waiting to retry",
    "success": "Finished", "unverified": "Needs verification", "blocked": "Needs attention",
    "error": "Turn failed", "cancelled": "Interrupted",
}
_TERMINAL_STATES = frozenset({"success", "unverified", "blocked", "error", "cancelled"})
_OUTCOMES = {
    "completed": "success", "completed_verified": "success",
    "completed_unverified": "unverified", "blocked": "blocked", "failed": "error",
    "cancelled": "cancelled",
}

# A small, deliberately simplified head: curled trunk, crest, blue eye and tusks.
# Five columns by four pixels fits beside activity text in two terminal rows.
_HEAD_PIXELS = (
    " GG H",
    "G GGH",
    " GGBG",
    "  IGH",
)


def ascii_required() -> bool:
    try:
        "▀▄█·".encode(sys.stdout.encoding or "utf-8")
        return os.environ.get("TERM") == "dumb"
    except UnicodeEncodeError:
        return True


@lru_cache(maxsize=1)
def artwork() -> dict[str, Any]:
    return json.loads(files("apsara_cli.shared").joinpath("assets/makor.json").read_text("utf-8"))


@lru_cache(maxsize=32)
def _scaled(width: int) -> tuple[str, ...]:
    asset = artwork()
    # Two transparent columns and rows leave room for tail sway and a small hop.
    inner_width = width - 2
    height = max(4, round(asset["height"] * inner_width / asset["width"]))
    rows = [" " * width]
    for y in range(height):
        source_y = min(asset["height"] - 1, int((y + .5) * asset["height"] / height))
        row = asset["pixels"][source_y]
        rows.append(" " + "".join(row[min(asset["width"] - 1, int(
            (x + .5) * asset["width"] / inner_width
        ))] for x in range(inner_width)) + " ")
    rows.append(" " * width)
    if len(rows) % 2:
        rows.append(" " * width)
    return tuple(rows)


def _animated_pixels(width: int, state: str, phase: int) -> list[list[str]]:
    base = _scaled(width)
    pixels = [list(row) for row in base]
    height = len(pixels)
    # Blink and eye pulse preserve the silhouette instead of adding a new face.
    blink = phase == 15
    for y, row in enumerate(base):
        for x, cell in enumerate(row):
            if cell == "B" and x < width * .36 and y < height * .65:
                if blink:
                    pixels[y][x] = "O"
                elif state == "thinking" and phase % 4 == 2:
                    pixels[y][x] = "I"
    if state in {"thinking", "working", "verifying", "speaking"}:
        delta = (0, -1, 0, 1)[phase % 4]
        tail = [(x, y, cell) for y, row in enumerate(base) for x, cell in enumerate(row)
                if x >= width * .76 and y < height * .72 and cell != " "]
        for x, y, _ in tail:
            pixels[y][x] = " "
        for x, y, cell in tail:
            pixels[max(0, min(height - 1, y + delta))][x] = cell
    elif state == "success" and phase in {1, 3}:
        pixels = pixels[1:] + [[" "] * width]
    return pixels


@lru_cache(maxsize=512)
def _render(width: int, state: str, phase: int, color: bool, ascii_only: bool) -> tuple[str, ...]:
    pixels = _animated_pixels(width, state, phase)
    return _render_pixels(pixels, color, ascii_only)


def _render_pixels(pixels: list[list[str]], color: bool, ascii_only: bool) -> tuple[str, ...]:
    width = len(pixels[0])
    palette = artwork()["palette"]
    rgb = {key: tuple(bytes.fromhex(value[1:])) for key, value in palette.items()}
    lines = []
    for y in range(0, len(pixels), 2):
        runs: list[tuple[tuple[str, str], str]] = []
        for x in range(width):
            top, bottom = pixels[y][x], pixels[y + 1][x]
            pair = (top, bottom)
            if ascii_only:
                cell = "B" if "B" in pair else top if top != " " else bottom
                glyph = " " if cell == " " else "*" if cell == "B" else "+" if cell == "O" else "#"
            else:
                glyph = "█" if top != " " and bottom != " " and (top == bottom or not color) else (
                    "▀" if top != " " else "▄" if bottom != " " else " "
                )
            if runs and runs[-1][0] == pair:
                runs[-1] = (pair, runs[-1][1] + glyph)
            else:
                runs.append((pair, glyph))
        pieces = []
        for (top, bottom), glyphs in runs:
            if not color or (top == bottom == " "):
                pieces.append(glyphs)
                continue
            foreground = rgb[top if top != " " else bottom]
            codes = ["38;2;" + ";".join(map(str, foreground))]
            if top != " " and bottom != " " and top != bottom and not ascii_only:
                codes.append("48;2;" + ";".join(map(str, rgb[bottom])))
            pieces.append("\x1b[" + ";".join(codes) + "m" + glyphs + "\x1b[0m")
        lines.append("".join(pieces))
    return tuple(lines)


@lru_cache(maxsize=128)
def _head_frame(state: str, phase: int, color: bool, ascii_only: bool) -> tuple[str, ...]:
    pixels = [list(row) for row in _HEAD_PIXELS]
    if phase == 15:
        pixels[2][3] = "H"  # Blink.
    elif state in {"thinking", "verifying"} and phase % 4 == 2:
        pixels[2][3] = "I"  # Pulse the eye in a repeating loading rhythm.
    elif state == "working" and phase % 4 == 2:
        pixels[2][3], pixels[2][4] = "G", "B"  # Look towards the action text.
    if state in {"thinking", "verifying"} and phase % 4 in {1, 3}:
        pixels[0][1 if phase % 4 == 1 else 4] = "I"  # Sweep a small crest highlight.
    if state == "speaking" and phase % 4 in {1, 3}:
        pixels[3][2] = "G"  # A small mouth movement as the answer arrives.
    if not color and not ascii_only and phase != 15:
        if state in {"thinking", "working", "verifying"} and phase % 4 == 2:
            pixels[2][3], pixels[2][4] = "G", " "
        else:
            pixels[2][3] = " "
    return _render_pixels(pixels, color, ascii_only)


class Makor:
    """Thread-safe state shared by the worker's events and the TUI's paint loop."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.enabled = True
        self.animation = True
        self.state = "idle"
        self._settled = False
        self._clock = clock
        self._since = clock()
        self._lock = threading.Lock()

    def configure(self, ui_config: Any) -> None:
        self.enabled = getattr(ui_config, "mascot", None) is not False
        self.animation = (getattr(ui_config, "mascot_animation", None) is not False
                          and not os.environ.get("CI") and os.environ.get("TERM") != "dumb")

    def begin_turn(self) -> None:
        with self._lock:
            self._settled = False
            self.state = "thinking"
            self._since = self._clock()

    def set_state(self, state: str) -> bool:
        if state not in _LABELS:
            raise ValueError(f"Unknown Makor state: {state}")
        with self._lock:
            if self._settled and state not in _TERMINAL_STATES:
                return False
            changed = self.state != state
            if changed:
                self.state = state
                self._since = self._clock()
            if state in _TERMINAL_STATES:
                self._settled = True
            return changed

    def observe(self, event: dict[str, Any]) -> bool:
        kind = event.get("type")
        if kind == "run_state":
            state = str(event.get("state", ""))
            activity = _OUTCOMES.get(state) or {
                "planning": "thinking", "running": "working", "verifying": "verifying",
            }.get(state)
        else:
            activity = {
                "status": "thinking", "assistant_dispatch": "thinking", "tool_call": "working",
                "tool_result": "thinking", "response_start": "speaking", "text_chunk": "speaking",
                "response_end": "thinking", "final_answer": "speaking",
                "retry_notice": "retrying", "blocked": "blocked", "error": "error",
            }.get(kind)
            if kind == "status" and "retry" in str(event.get("message", "")).lower():
                activity = "retrying"
            elif kind == "tool_call" and event.get("name") in {"verify_project", "request_critic"}:
                activity = "verifying"
        return self.set_state(activity) if activity else False

    def finish_turn(self, outcome: str = "", run_state: str = "completed") -> None:
        with self._lock:
            if self._settled:
                return
        self.set_state({"error": "error", "blocked": "blocked"}.get(
            outcome, _OUTCOMES.get(run_state, "success")
        ))

    def label(self, ascii_only: bool = False) -> str:
        with self._lock:
            label = _LABELS[self.state]
        return f"Makor {'|' if ascii_only else '·'} {label}"

    def render(self, width: int = 32, color: bool = True, ascii_only: bool = False) -> tuple[str, ...]:
        width = max(6, min(96, width))
        with self._lock:
            state = self.state
            phase = int(max(0, self._clock() - self._since) * 4) % 16 if self.animation else 0
            # The celebration settles after one short hop; it never implies verification.
            if state == "success" and self._clock() - self._since > 2:
                phase = 0
        return _render(width, state, phase, color, ascii_only)

    def render_head(self, color: bool = True, ascii_only: bool = False) -> tuple[str, ...]:
        with self._lock:
            phase = int(max(0, self._clock() - self._since) * 4) % 16 if self.animation else 0
            state = self.state
        return _head_frame(state, phase, color, ascii_only)

    @staticmethod
    def height(width: int) -> int:
        return len(_scaled(max(6, min(96, width)))) // 2
