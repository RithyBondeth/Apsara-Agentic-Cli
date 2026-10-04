"""
OpenCode-inspired welcome banner: a centered two-tone pixel-block logo with
no surrounding box. The rest of the welcome chrome (hint row, tip line,
path/version footer) is composed by chat.py so it can react to session state.
"""

import os
import sys
import textwrap
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apsara_cli.shared.ui import ConsoleUI
    from apsara_cli.config.cli_config import CliConfig


# ── Pixel logo ────────────────────────────────────────────────────────────────
# 4x5 pixel glyphs; every "X" cell renders as a 2-char "██" block, giving the
# chunky low-res look of the OpenCode logo.

_PIXELS: dict[str, tuple[str, ...]] = {
    "A": (".XX.", "X..X", "XXXX", "X..X", "X..X"),
    "P": ("XXX.", "X..X", "XXX.", "X...", "X..."),
    "S": (".XXX", "X...", ".XX.", "...X", "XXX."),
    "R": ("XXX.", "X..X", "XXX.", "X.X.", "X..X"),
    " ": ("....", "....", "....", "....", "...."),
}

_LOGO_WORD = "APSARA"
_LOGO_SPLIT = 3  # "APS" cool half + "ARA" warm half — two-tone, opencode-style

# Per-row gradients: cool blue→green on the left, gold→violet on the right.
_LOGO_LEFT_COLORS = [
    "38;2;120;175;255",
    "38;2;110;198;252",
    "38;2;120;214;230",
    "38;2;138;220;195",
    "38;2;162;220;160",
]
_LOGO_RIGHT_COLORS = [
    "38;2;255;215;90",
    "38;2;252;176;120",
    "38;2;245;150;170",
    "38;2;220;145;235",
    "38;2;160;160;255",
]
_SUBTITLE_COLOR = "38;2;168;172;205"


def _render_letters(letters: str, row: int) -> str:
    return "  ".join(
        "".join("██" if px == "X" else "  " for px in _PIXELS.get(ch, _PIXELS[" "])[row])
        for ch in letters
    )


def logo_row_parts() -> list[tuple[str, str]]:
    """Per-row (dim_half, bright_half) raw text for the pixel logo."""
    rows: list[tuple[str, str]] = []
    for row in range(5):
        dim_half = _render_letters(_LOGO_WORD[:_LOGO_SPLIT], row) + "  "
        bright_half = _render_letters(_LOGO_WORD[_LOGO_SPLIT:], row)
        rows.append((dim_half, bright_half))
    return rows


def logo_width() -> int:
    dim_half, bright_half = logo_row_parts()[0]
    return len(dim_half) + len(bright_half)


def styled_logo_lines(ui: "ConsoleUI", pad: int = 0) -> list[str]:
    """The logo with per-row gradient styling, left-padded by ``pad`` spaces."""
    left = " " * pad
    return [
        left
        + ui.style(cool_half, "1", _LOGO_LEFT_COLORS[row])
        + ui.style(warm_half, "1", _LOGO_RIGHT_COLORS[row])
        for row, (cool_half, warm_half) in enumerate(logo_row_parts())
    ]


def small_logo_line(ui: "ConsoleUI") -> str:
    """Narrow-terminal fallback: letter-spaced two-tone wordmark."""
    cool_part = " ".join(_LOGO_WORD[:_LOGO_SPLIT])
    warm_part = " ".join(_LOGO_WORD[_LOGO_SPLIT:])
    return (
        ui.style(cool_part + " ", "1", _LOGO_LEFT_COLORS[0])
        + ui.style(warm_part, "1", _LOGO_RIGHT_COLORS[0])
    )


# ── Helpers ──────────────────────────────────────────────────────────────────

def center_text(text: str, width: int) -> str:
    if len(text) >= width:
        return text
    return text.center(width)


def should_animate_welcome(config: "CliConfig") -> bool:
    if config.ui.welcome_animation is False:
        return False
    if os.environ.get("CI"):
        return False
    return sys.stdout.isatty()


def welcome_frame_delay_seconds(config: "CliConfig") -> float:
    ms = config.ui.welcome_frame_delay_ms
    if ms is None:
        ms = 18
    return max(0, min(ms, 250)) / 1000.0


def wrap_banner_text(text: str, width: int) -> list[str]:
    wrapped: list[str] = []
    for raw_line in text.splitlines() or [""]:
        line = raw_line.strip()
        if not line:
            wrapped.append("")
            continue
        wrapped.extend(textwrap.wrap(line, width=width, break_long_words=False, break_on_hyphens=False) or [""])
    return wrapped


def _centered(ui: "ConsoleUI", text: str, terminal: int, *codes: str) -> str:
    pad = " " * max((terminal - len(text)) // 2, 2)
    return pad + ui.style(text, *codes)


def banner_taglines(config: "CliConfig") -> tuple[str, str]:
    """(subtitle, powered-by credit) with the Bondeth-branded defaults."""
    from apsara_cli import __version__

    subtitle = config.ui.welcome_subtitle or "Elegant local coding assistance for your workspace"
    powered = config.ui.powered_by or f"Powered by Bondeth · v{__version__}"
    return subtitle, powered


# ── Banner ───────────────────────────────────────────────────────────────────

def render_welcome_banner(ui: "ConsoleUI", config: "CliConfig") -> list[str]:
    from apsara_cli.shared.ui import terminal_width

    terminal = max(12, min(terminal_width(), 112))
    lines: list[str] = [""]

    if terminal >= logo_width() + 4:
        pad = max((terminal - logo_width()) // 2, 2)
        lines.extend(styled_logo_lines(ui, pad))
    else:
        plain_len = len(" ".join(_LOGO_WORD))
        lines.append(" " * max((terminal - plain_len) // 2, 2) + small_logo_line(ui))

    wrap_w = max(1, min(68, terminal - 4))
    title = config.ui.welcome_title
    subtitle, powered = banner_taglines(config)

    lines.append("")
    if title:
        for text in wrap_banner_text(title, wrap_w):
            lines.append(_centered(ui, text, terminal, "1", "38;2;225;230;242"))
    for text in wrap_banner_text(subtitle, wrap_w):
        lines.append(_centered(ui, text, terminal, _SUBTITLE_COLOR))
    for text in wrap_banner_text(powered, wrap_w):
        lines.append(_centered(ui, text, terminal, "38;2;200;166;110"))
    return lines


def print_welcome_banner(ui: "ConsoleUI", config: "CliConfig") -> None:
    lines = render_welcome_banner(ui, config)
    if not lines:
        return

    if should_animate_welcome(config):
        delay = welcome_frame_delay_seconds(config)
        for line in lines:
            print(line)
            time.sleep(delay)
        time.sleep(delay * 2)
    else:
        for line in lines:
            print(line)

    print()
