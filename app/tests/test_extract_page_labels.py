"""Card 0141: a PDF whose page labels start after page 1 (a JSTOR cover sheet) imports; pages without a
label fall back to their position."""
from __future__ import annotations

import pymupdf

from research_lab.intake import extract


def test_pages_before_the_first_label_range_use_their_position(tmp_path):
    doc = pymupdf.open()
    for text in ("cover sheet", "first page of the article", "second page"):
        doc.new_page().insert_text((72, 72), text)
    doc.set_page_labels([{"startpage": 1, "prefix": "", "style": "D", "firstpagenum": 7}])
    path = tmp_path / "jstor.pdf"
    doc.save(path)
    doc.close()

    pages = extract.extract(path).pages
    assert [p.label for p in pages] == ["1", "7", "8"]
