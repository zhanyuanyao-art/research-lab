"""Section detection (intake/sections.py): the existing English, numbered and Chinese headings, and since card
0170 ALL-CAPS headings, also when layout-preserving extraction puts them on one line with the other column."""
from __future__ import annotations

import pytest

from research_lab.intake.extract import PageText
from research_lab.intake.sections import caps_heading, detect

FILLER = "        a line of filler prose that stands in for the running text    and the column beside it says much the same"


def pages(*texts: str) -> list[PageText]:
    return [PageText(i, str(144 + i), t) for i, t in enumerate(texts)]


def kinds(secs) -> list[tuple[str, str | None]]:
    return [(s.kind, s.heading) for s in secs]


def test_existing_headings_still_found():
    secs = detect(pages("Abstract\nWe study things.\n1. Introduction\nText.", "2. Data and Methods\nMore text.",
                        "Conclusion\nDone.", "References\nAuthor, A. 2001."))
    assert kinds(secs) == [("abstract", "Abstract"), ("introduction", "1. Introduction"),
                           ("heading", "2. Data and Methods"), ("conclusion", "Conclusion"), ("references", "References")]


def test_chinese_headings_still_found():
    secs = detect(pages("摘  要：本文研究……\n一、引言\n正文。", "三、研究设计与数据来源\n正文。", "参考文献\n作者. 2001."))
    assert [s.kind for s in secs if s.heading] == ["abstract", "introduction", "methods", "references"]


def test_all_caps_headings_on_their_own_line():
    secs = detect(pages(f"{FILLER}\nRESEARCH DESIGN\n{FILLER}", f"DATA\n{FILLER}\nRESULTS\n{FILLER}",
                        f"{FILLER}\nREFERENCES\nAuthor, A. 2001."))
    assert ("heading", "RESEARCH DESIGN") in kinds(secs) and ("heading", "DATA") in kinds(secs)
    assert ("heading", "RESULTS") in kinds(secs) and ("references", "REFERENCES") in kinds(secs)


def test_a_heading_beside_the_other_column():
    # APSR as extracted: "OUR EMPIRICAL DESIGN" sits in the left column, the right column's text on the same line
    line = "     OUR EMPIRICAL DESIGN                                    that are employed to capture minor imbalances"
    secs = detect(pages(f"{FILLER}\n{line}\n{FILLER}", f"{FILLER}\n       REFERENCES          ments: From Local Innovations"))
    design = next(s for s in secs if s.heading == "OUR EMPIRICAL DESIGN")
    assert design.kind == "heading" and design.line_no == 1 and design.inline
    assert any(s.kind == "references" and s.page_start == 1 for s in secs)


def test_a_two_line_heading_is_one_heading():
    text = f"{FILLER}\n          TESTING THE CAUSAL             right column\n          MECHANISM AND ITS LIMITS   more\n{FILLER}"
    assert [s.heading for s in detect(pages(text)) if s.kind == "heading"] == ["TESTING THE CAUSAL"]


def test_figure_labels_table_headers_and_running_heads_are_not_headings():
    figure = "\n".join(["FIGURE 3.  Analysis", "INFRASTRUCTURE INDEX", "HEALTH SERVICES INDEX", "EDUCATION INDEX",
                        "COMMUNICATIONS INDEX", "-.2   -.1   0"])
    table = "DEPENDENT VARIABLE        (1)        (2)        (3)\nRegional FE   YES   YES   YES"
    running = "INTERNATIONAL ORGANIZATION"
    secs = detect(pages(f"{running}\n{figure}", f"{running}\n{table}", f"{running}\n{FILLER}", f"{running}\n{FILLER}"))
    assert [s for s in secs if s.kind == "heading"] == []


@pytest.mark.parametrize("text,ok", [
    ("OUR EMPIRICAL DESIGN", True), ("DATA", True), ("RESULTS", True), ("THE LITERATURE ON DECENTRALIZATION", True),
    ("DPCS AND VIETNAM’S GOVERNMENT", True),
    ("YES", False), ("TABLE", False), ("OLS", False), ("PCI)", False), ("VND,", False), ("II I", False),
    ("TABLE 3", False), ("Our Empirical Design", False), ("A VERY LONG HEADING THAT HAS FAR TOO MANY WORDS", False),
    ("CONCLUSION.", False),
])
def test_caps_heading_rule(text, ok):
    assert caps_heading(text) is ok
