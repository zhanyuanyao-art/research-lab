"""Card 0142: Leo copies text from every page of the desktop app (chat replies included), so nothing
but the window-drag strips and button labels turns selection off."""
from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "research_lab" / "web" / "static"
ALLOWED = {"[data-window-drag]", ".titlebar-drag", "button", ".btn"}


def _no_select_selectors(css: str) -> list[str]:
    out = []
    for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        if re.search(r"(?<![-\w])user-select:\s*none", body):
            out += [s.strip() for s in selectors.split(",")]
    return out


def test_only_drag_strips_and_buttons_turn_selection_off_on_the_desktop():
    blocked = [s for s in _no_select_selectors((STATIC / "app.css").read_text()) if s.startswith("body.desktop")]
    assert blocked and {s.removeprefix("body.desktop").strip() for s in blocked} <= ALLOWED


def test_reader_and_library_text_is_selectable():
    assert not _no_select_selectors((STATIC / "papers.css").read_text())
