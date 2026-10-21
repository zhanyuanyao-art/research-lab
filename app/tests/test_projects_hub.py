"""课题 page: the 说明 card shows only what Leo wrote in index.md, with links that work in the app."""
from __future__ import annotations

from research_lab.web.routes.projects import readme_text

SLUG = "asean-centrality"

JEKYLL_INDEX = """---
title: "Project: ASEAN Centrality"
slug: asean-centrality
---

# ASEAN Centrality

<p class="kicker">stage: exploring · since 2026-09</p>

## Question

Why does ASEAN stay central? See [the camps](literature/) and [cases](./evidence.md).

## Current answer (one paragraph, revised as artifacts accumulate)

...

## 论文结构

1. 导论，参考 [外部](https://example.org/x) 和 [产出](/artifacts/ART-2026-0001)

## Files

- [literature](literature/) — the reading, by camp

## Artifacts

{% assign items = site.artifacts | where: "project", page.slug %}
{% include artifact-list.html items=items %}
"""


def test_readme_strips_site_scaffolding_and_fixes_links():
    out = readme_text(JEKYLL_INDEX, SLUG)
    for gone in ("---", "slug:", "# ASEAN Centrality", "kicker", "{%", "## Files", "## Artifacts", "Current answer", "..."):
        assert gone not in out
    assert out.startswith("## 研究问题")
    assert f"](/projects/{SLUG}/files/literature.md)" in out and f"](/projects/{SLUG}/files/evidence.md)" in out
    assert "](https://example.org/x)" in out and "](/artifacts/ART-2026-0001)" in out
    assert "## 论文结构" in out


def test_readme_without_front_matter_keeps_text():
    assert readme_text("当前优先研究履约顺序。\n\n1. [设计](/artifacts/ART-2026-4003)\n", SLUG) == \
        "当前优先研究履约顺序。\n\n1. [设计](/artifacts/ART-2026-4003)"


def test_files_tab_renders_clean_readme(client, repo):
    (repo / "projects" / SLUG / "index.md").write_text(JEKYLL_INDEX, encoding="utf-8")
    html = client.get(f"/projects/{SLUG}?tab=files").text
    card = html[html.index("<h3>说明</h3>"):]
    card = card[:card.index("</section>")]
    assert "{%" not in card and "slug:" not in card and "研究问题" in card
    assert f'href="/projects/{SLUG}/files/literature.md"' in card
    assert "项目主页" not in html and "说明 index.md" not in html  # label shown without the file name
