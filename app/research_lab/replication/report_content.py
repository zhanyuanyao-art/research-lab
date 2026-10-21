"""What a replication report says, assembled into the slides of deck v2 (card 0196).

Parts, in the order a seminar talk runs: the study, the data, the methods, the results, the conclusion. Everything here
is read from what the app already holds, with no model call: the paper's stored summary, the plan, the author's code
(method_notes reads which methods it uses and supplies the teaching chapter), the latest valid run, its comparison with
the paper's numbers, crops of the paper's own tables, and the check charts (checkviz) when that module is present.
Text a model wrote for this paper (ai_text.json beside the deck, see `load_ai_text`) replaces the plainer fallbacks.

All strings are outside text; they are cut with deck.clean before they reach a slide.
"""
from __future__ import annotations

import json
import logging
import os
import posixpath
import re
import sqlite3
import time
import uuid
from dataclasses import MISSING, dataclass, field
from pathlib import Path
from typing import Any

from . import coverage, method_notes
from . import deck as D
from . import workspace as W
from .slides import (Card, Deck, Slide, chapter_assumptions, chapter_checks, chapter_formula, first_bullets,
                     first_paragraph, first_sentence)

log = logging.getLogger(__name__)

PARTS = ("第一部分", "第二部分", "第三部分", "第四部分", "第五部分")
MAX_METHODS = 4
MAX_RESULT_SLIDES = 24
OVERVIEW_ROWS = 10
COMPARE_ROWS = 9
AI_TEXT = "ai_text.json"
AI_TEXT_BYTES = 200_000
OK = ("exact", "within")


@dataclass
class Built:
    deck: Deck
    notes: list[str] = field(default_factory=list)
    replicated: int = 0
    pending: int = 0


_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069]")


def code_text(v: Any, n: int = 600) -> str:
    """Author code for a slide: line breaks kept, control and bidi characters dropped, cut."""
    return _CONTROL.sub("", str(v or ""))[:n]


def _write_json(folder: Path, name: str, data: Any) -> None:
    """Write a JSON file into the deck folder by a fresh temporary file and a rename, never through a link."""
    W._checked(folder, folder / name)
    tmp = folder / f".{name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp, folder / name)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _typed(cls: Any, raw: dict[str, Any]) -> dict[str, Any]:
    """The fields of a saved Slide or Card with the types the dataclass declares; anything else is dropped."""
    out: dict[str, Any] = {}
    for name, f in cls.__dataclass_fields__.items():
        if name not in raw or name == "cards":
            continue
        v = raw[name]
        default = (f.default if f.default is not MISSING else
                   f.default_factory() if f.default_factory is not MISSING else "")   # `kind` has no default: text
        if isinstance(default, bool):
            out[name] = bool(v)
        elif isinstance(default, str):
            out[name] = c(v, 600) if isinstance(v, (str, int, float)) else ""
        elif isinstance(default, list) and isinstance(v, list):
            if name == "rows":
                out[name] = [[c(x, 120) for x in r[:12]] for r in v[:60] if isinstance(r, list)]
            elif name == "hits":
                out[name] = [bool(x) for x in v[:60]]
            elif name == "code":
                out[name] = code_text(v)
            else:
                out[name] = [c(x, 400) for x in v[:40] if isinstance(x, (str, int, float))]
    if "code" in raw and isinstance(raw["code"], str):
        out["code"] = code_text(raw["code"])
    if "formula" in raw and isinstance(raw["formula"], str):
        out["formula"] = code_text(raw["formula"], 1200)
    return out


def c(v: Any, n: int = 120) -> str:
    return D.clean(v, n)


def assets_dir(plan_id: str) -> Path:
    return D.exports_dir(plan_id) / "deck"


def load_ai_text(plan_id: str) -> dict[str, Any]:
    """The model-written text for this paper, when one was made and is sane; {} otherwise."""
    path = D.exports_dir(plan_id) / AI_TEXT
    try:
        st = os.lstat(path)
        if not (st.st_mode & 0o170000 == 0o100000) or st.st_size > AI_TEXT_BYTES:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return {}
    return data if isinstance(data, dict) else {}


def _texts(v: Any, n: int = 6, each: int = 120) -> list[str]:
    return [x for x in (c(i, each) for i in D._list(v, n)) if x]


# ---------------------------------------------------------------- pictures

class Assets:
    def __init__(self, deadline: float | None):
        self.imgs: dict[str, D.Img] = {}
        self.pictures = D.Pictures(deadline)

    def add(self, name: str, img: D.Img | None) -> str:
        if img is None or not self.pictures.take(img):
            return ""
        self.imgs[name] = img
        return name


def _png(data: bytes | None) -> D.Img | None:
    return D._png_bounded(data) if data else None


def _cover_thumb(pdf: D.PaperPdf) -> D.Img | None:
    for doc, _ in pdf.files[:1]:
        if doc is not None and doc.page_count:
            try:
                return D.render(doc[0], None, 60)
            except (RuntimeError, ValueError):
                return None
    return None


def _charts():
    try:
        from . import checkviz
        return checkviz
    except ImportError:
        return None


# ---------------------------------------------------------------- parts

def _where(row: dict[str, Any]) -> str:
    return f"{'正文' if row.get('source') == 'main' else '附录'}{D._cn(row)}"


def cover(ctx: D.Ctx, ai: dict[str, Any], thumb: str) -> Slide:
    p = ctx.paper
    venue = " · ".join(x for x in (c(p.get("venue"), 80), c(p.get("year"), 6)) if x)
    meta = [x for x in (D.authors_of(p.get("authors_json")), venue) if x]
    mode = D._dict(ctx.run.get("workspace") if ctx.run else {}).get("mode") if ctx.run else None
    if ctx.run is not None:
        meta.append("复现：用 R 原样运行作者代码" if mode == "rrun" else "复现：按作者代码重写运行")
    seminar = c(ai.get("seminar"), 30)
    return Slide("cover", title=c(p.get("title") or "未关联文献", 160), subtitle=c(ai.get("title_zh"), 60),
                 kicker=seminar or "论文复现", meta=meta, thumb=thumb)


def study(ctx: D.Ctx, ai: dict[str, Any]) -> list[Slide]:
    crumb = f"{PARTS[0]} · 研究问题与理论"
    s = ctx.summary
    out = [Slide("section", title="研究问题与理论", no=PARTS[0], sub=c(ai.get("study_sub"), 40),
                 topics=["研究问题", "论点", "主要发现"])]
    models = [D._dict(m) for m in D._list(ai.get("models"), 4)]
    if models:
        tones = ("green", "blue", "rust", "warm")
        cards = [Card(head=c(m.get("name"), 20), body=c(m.get("claim"), 110), note=c(m.get("measure"), 40),
                      tag=c(m.get("tag"), 10), tone=tones[i]) for i, m in enumerate(models)]
        out.append(Slide("cards", crumb=crumb, title=c(ai.get("question_title") or "研究问题", 40),
                         lede=c(ai.get("question") or D._cited(s, "research_question", []), 70), cards=cards))
    else:
        pairs = [("研究问题", D._cited(s, "research_question", [])), ("核心论点", D._cited(s, "argument", [])),
                 ("机制", D._cited(s, "mechanism", []))]
        cards = [Card(head=k, body=c(v, 160), tone=t) for (k, v), t in zip(pairs, ("green", "blue", "rust")) if v]
        if cards:
            out.append(Slide("cards", crumb=crumb, title="研究问题", cards=cards))
    findings = _texts(ai.get("findings"), 4)
    if findings:
        out.append(Slide("cards", crumb=crumb, title="主要发现",
                         cards=[Card(points=findings)]))
    return out


def data(ctx: D.Ctx, ai: dict[str, Any], scripts: dict[str, str], docs: dict[str, str]) -> list[Slide]:
    crumb = f"{PARTS[1]} · 数据与测量"
    pl = D._dict(ctx.plan.get("plan"))
    out = [Slide("section", title="数据与测量", no=PARTS[1], sub=c(ai.get("data_sub"), 40),
                 topics=["数据来源", "测量", "复现包"])]
    tones = ("green", "green", "blue", "warm")
    sources = [D._dict(x) for x in D._list(ai.get("sources"), 4)]
    if sources:
        cards = [Card(big=c(x.get("big"), 8), head=c(x.get("name"), 22), body=c(x.get("what"), 90), tone=tones[i])
                 for i, x in enumerate(sources)]
    else:
        raw = [D._dict(x) for x in D._list(pl.get("data_sources"), 4)]
        cards = [Card(head=c(x.get("name"), 30), body=c(x.get("url_or_where"), 90), tone=tones[i])
                 for i, x in enumerate(raw) if x.get("name")]
    lede = c(ai.get("unit") or pl.get("unit_of_analysis"), 70)
    if cards:
        out.append(Slide("cards", crumb=crumb, title="数据来源", lede=lede, cards=cards))
    steps = [D._dict(x) for x in D._list(ai.get("measurement"), 3)]
    if steps:
        out.append(Slide("flow", crumb=crumb, title=c(ai.get("measurement_title") or "关键变量怎么测", 30),
                         lede=c(ai.get("measurement_lede"), 70), src=c(ai.get("measurement_note"), 90),
                         cards=[Card(tag=f"第{'一二三'[i]}步", head=c(x.get("name"), 16), body=c(x.get("what"), 90),
                                     tone=("green", "blue", "rust")[i]) for i, x in enumerate(steps)]))
    files = [c(posixpath.basename(c(k, 160)), 60) for k in D._list(D._dict(ctx.plan).get("datasets_files"), 8)]
    names = sorted(c(posixpath.basename(k), 60) for k in list(scripts)[:400])[:8]
    doc_names = sorted(c(posixpath.basename(k), 60) for k in list(docs)[:100])[:4]
    pkg = [Card(head="作者脚本", points=names or ["没有取到作者代码"]),
           Card(head="说明书", points=doc_names or ["没有说明书"], tone="warm")]
    if files:
        pkg.insert(0, Card(head="数据文件", points=files, tone="blue"))
    out.append(Slide("cards", crumb=crumb, title="复现包里有什么", cards=pkg))
    return out


def _pick_methods(found: list[method_notes.Found]) -> list[method_notes.Found]:
    designs = [f for f in found if f.key in ("did", "rdd", "synth", "iv", "matching")]
    estimators = sorted((f for f in found if f.key in ("ols", "binary", "ordered", "poisson", "negbin", "survival")),
                        key=lambda f: -f.count)[:2]
    structure = [f for f in found if f.key in ("fixed_effects", "multilevel")]
    measure = [f for f in found if f.key in ("ml", "topic", "dictionary", "bootstrap")]
    return (designs + estimators + structure + measure)[:MAX_METHODS]     # taught in this order: design, estimator, structure


def _used_for(f: method_notes.Found, rows: list[dict[str, Any]]) -> list[str]:
    mine = {posixpath.basename(s).lower() for s in f.scripts}
    out = []
    for r in rows:
        if r.get("ticked") and mine & {n.lower() for n in D._scripts_of(r)}:
            w = _where(r)
            if w not in out:
                out.append(w)
    return out[:6]


def methods(ctx: D.Ctx, ai: dict[str, Any], found: list[method_notes.Found]) -> list[Slide]:
    crumb = f"{PARTS[2]} · 方法"
    picked = _pick_methods(found)
    if not picked:
        return []
    usage = D._dict(ai.get("method_usage"))
    out = [Slide("section", title="方法", no=PARTS[2], sub=c(ai.get("methods_sub"), 40),
                 topics=[f.chapter.name for f in picked if f.chapter])]
    for f in picked:
        ch = f.chapter
        if ch is None:
            continue
        used = _used_for(f, ctx.rows)
        how = c(usage.get(f.key), 120) or ("用在" + "、".join(used) if used else "作者代码里用到")
        reading = first_bullets(ch.sections.get("怎么读结果", ""), 2, 80)
        main_scripts = {n.lower() for row in ctx.rows if row.get("ticked") and row.get("group") == "main"
                        for n in D._scripts_of(row)}
        snip = next((h for h in f.snippets if posixpath.basename(h.script).lower() in main_scripts),
                    f.snippets[0] if f.snippets else None)
        out.append(Slide("method", crumb=crumb, title=ch.name, lede=c(ch.summary, 70),
                         cards=[Card(head="怎么读", points=reading) if reading else Card(head="怎么读", body=c(ch.summary, 120)),
                                Card(head="本文怎么用", body=how, tone="rust",
                                     note="、".join(c(posixpath.basename(s), 40) for s in f.scripts[:3]))],
                         formula=chapter_formula(ch.sections.get("模型设定", "")),
                         code=code_text(snip.code) if snip else "",
                         code_head=f"{c(posixpath.basename(snip.script), 60)} · 第 {snip.line} 行" if snip else ""))
        checks = chapter_checks(ch.sections.get("稳健性检验：做什么，为什么", ""))
        assumptions = chapter_assumptions(ch.sections.get("识别假设", ""))
        if checks:
            lede = "前提：" + " · ".join(a for a, _ in assumptions[:4]) if assumptions else ""
            out.append(Slide("table", crumb=crumb, title=f"{ch.name}：要做哪些检验，为什么", lede=c(lede, 80),
                             columns=["检验", "检查哪条前提", "怎么看"], rows=checks))
    return out


def _compare_rows(items: list[dict[str, Any]]) -> tuple[list[list[str]], list[bool]]:
    rows, hits = [], []
    for it in items[:COMPARE_ROWS]:
        paper, ours = D._dict(it.get("paper")), D._dict(it.get("ours"))
        pv = D._num(paper.get("value"))
        ov = D._num(ours.get("estimate"))
        rows.append([c(it.get("term"), 28), c(it.get("model"), 14), D.fmt_number(pv) if pv is not None else "—",
                     D.fmt_number(ov) if ov is not None else "—"])
        hits.append(it.get("verdict") in OK)
    return rows, hits


def _verdict(items: list[dict[str, Any]]) -> tuple[str, bool]:
    n = len(items)
    ok = sum(1 for it in items if it.get("verdict") in OK)
    exact = sum(1 for it in items if it.get("verdict") == "exact")
    if n and exact == n:
        return "完全一致", True
    if n and ok == n:
        return "在误差范围内一致", True
    missing = sum(1 for it in items if it.get("verdict") == "missing")
    if missing == n:
        return "未复现", False
    return f"{ok} / {n} 一致", not any(it.get("verdict") == "mismatch" for it in items)


_MISSING_FN = re.compile(r'could not find function "([\w.]{1,40})"|there is no package called .([\w.]{1,40}).')


def _failures(ctx: D.Ctx) -> dict[str, str]:
    """Script name → what stopped it, from the run's record: a function or package R could not find."""
    out: dict[str, str] = {}
    failed = D._dict(D._dict(ctx.run.get("workspace")).get("failed")) if ctx.run is not None else {}
    for script, err in list(failed.items())[:50]:
        m = _MISSING_FN.search(str(err)[:2000])
        name = posixpath.basename(c(script, 160)).lower()
        out[name] = f"缺 {m.group(1) or m.group(2)}" if m else "脚本中途出错"
    return out


def results(ctx: D.Ctx, pdf: D.PaperPdf | None, assets: Assets, notes: list[str]) -> tuple[list[Slide], int, int]:
    crumb = f"{PARTS[3]} · 复现结果"
    items = [D._dict(i) for i in D._list(D._dict(ctx.comparison).get("items"), D.MAX_COMPARED)]
    by_table: dict[str, list[dict[str, Any]]] = {}
    for it in items:
        by_table.setdefault(D._nt(it.get("table")), []).append(it)
    figures = D.Figures(ctx.plan_id, str(ctx.run["id"])) if ctx.run is not None else D.Figures()
    # a result is a table or figure some script makes, one the paper's numbers were taken from, or a figure the run drew;
    # a diagram of the theory or a list of dictionary words is none of these and is not something to replicate
    rows = [r for r in ctx.rows if r.get("ticked") and r.get("group") in ("main", "robust")
            and (D._scripts_of(r) or D._nt(r.get("label")) in by_table
                 or (r.get("kind") == "figure" and figures.find(r) is not None))]
    viz = _charts()
    failures = _failures(ctx)
    out: list[Slide] = []
    overview_rows, chips, replicated, pending = [], [], 0, 0
    per_row: list[Slide] = []
    for n, row in enumerate(rows[:MAX_RESULT_SLIDES]):
        label = c(row.get("label"), 24)
        mine = by_table.get(D._nt(label), [])
        scripts = D._scripts_of(row)
        src = f"{_where(row)} · 论文第 {c(row.get('page'), 8)} 页 · " + ("、".join(scripts[:2]) or "未对上脚本")
        crop = None
        if pdf is not None and pdf.files and not assets.pictures.late():
            try:
                crop = pdf.crop(row)
            except (RuntimeError, ValueError, OSError) as e:
                log.info("crop failed for %s: %s", label, e)
        orig = assets.add(f"orig-{n}.png", crop.img if crop else None)
        title = c(f"{D._cn(row)} · {c(row.get('caption'), 80)}".strip(" ·"), 72)
        kind_cn = "表" if row.get("kind") == "table" else "图"
        slide = Slide("compare", crumb=crumb, title=title, src=src, image=orig,
                      image_label=f"论文原{kind_cn} · 第 {c(row.get('page'), 8)} 页" if orig else f"论文原{kind_cn}没有截到")
        status = ""
        if row.get("kind") == "figure" and ctx.run is not None:
            path = figures.find(row)
            img = D.figure_image(path) if path is not None else None
            ours = assets.add(f"ours-{n}.png", img)
            if ours:
                slide.image2, slide.image2_label = ours, "复现的图"
                status, ok = "已生成", True
                replicated += 1
        elif mine:
            slide.columns = ["变量", "模型", "论文", "复现"]
            slide.rows, slide.hits = _compare_rows(mine)
            slide.verdict, slide.verdict_ok = _verdict(mine)
            stopped = next((failures[n.lower()] for n in scripts if n.lower() in failures), "")
            waiting = D._waiting_for(ctx, row) if slide.verdict == "未复现" else []
            if waiting:
                slide.verdict, slide.verdict_note = "等数据", "缺 " + "、".join(waiting[:2])
            elif slide.verdict == "未复现" and stopped:
                slide.verdict, slide.verdict_note = ("缺依赖", stopped) if stopped.startswith("缺") else ("运行出错", stopped)
            elif not ctx.reviewed and any(it.get("verdict") in ("direction", "mismatch") for it in mine):
                # an exact match confirms the number taken from the paper; a miss may be a misread number
                slide.verdict_note = "论文的数未核对"
            status, ok = slide.verdict, slide.verdict_ok
            if any(it.get("verdict") != "missing" for it in mine):
                replicated += 1
            else:
                pending += 1
        if not status:
            waiting = D._waiting_for(ctx, row)
            absent = [D.clean(posixpath.basename(D.clean(D._dict(x).get("path"), 160)), 60)
                      for x in D._list(row.get("scripts"), 10) if not D._dict(x).get("have")]
            have_any = any(D._dict(x).get("have") for x in D._list(row.get("scripts"), 10))
            if waiting:
                status, note = "等数据", "缺 " + "、".join(waiting[:2])
            elif absent and not have_any:
                status, note = "缺脚本", absent[0]
            else:
                status, note = "未复现", ""
            ok = False
            slide.verdict, slide.verdict_ok, slide.verdict_note = status, False, note
            pending += 1
        overview_rows.append([_where(row), c(row.get("caption"), 34), "、".join(scripts[:1]) or "—", status])
        chips.append("ok" if ok else "no")
        if mine or slide.image2:              # a result page only where there is something to put beside the original
            per_row.append(slide)
        if viz is not None and mine and any(D._dict(it.get("ours")) for it in mine):
            try:
                png = viz.table_chart(mine, fmt="png")
                name = assets.add(f"chart-{n}.png", _png(png))
                if name:
                    per_row.append(Slide("chart", crumb=crumb, title=f"{D._cn(row)} · 对照图", image=name,
                                         lede="空心是论文的估计，实心是复现的估计，横线是 95% 置信区间"))
            except Exception as e:                                  # a chart never stops the deck
                log.info("check chart failed for %s: %s", label, e)
    if len(rows) > MAX_RESULT_SLIDES:
        notes.append(f"另有 {len(rows) - MAX_RESULT_SLIDES} 项没排进展示稿")
    if not rows:
        return [], 0, 0
    total = sum(1 for _ in items)
    ok_n = sum(1 for it in items if it.get("verdict") in OK)
    sub = f"{total} 个数里 {ok_n} 个一致" if total else ""
    out.append(Slide("section", title="复现结果", no=PARTS[3], sub=sub,
                     topics=["复现概览"] + [D._cn(r) for r in rows[:5]]))
    pages = (len(overview_rows) + OVERVIEW_ROWS - 1) // OVERVIEW_ROWS
    for k in range(pages):                    # a slide holds ten rows: more go on further pages, never dropped
        part = slice(k * OVERVIEW_ROWS, (k + 1) * OVERVIEW_ROWS)
        out.append(Slide("table", crumb=crumb, title="复现概览" + (f" {k + 1}/{pages}" if pages > 1 else ""),
                         columns=["论文", "内容", "脚本", "结果"], rows=overview_rows[part], chips=chips[part]))
    if viz is not None and items:
        try:
            groups = {}
            for it in items:
                groups.setdefault(c(it.get("table"), 24), []).append(it)
            name = assets.add("overview.png", _png(viz.overview_chart(groups, fmt="png")))
            if name:
                out.append(Slide("chart", crumb=crumb, title="每个数的对照", image=name))
        except Exception as e:
            log.info("overview chart failed: %s", e)
    return out + per_row, replicated, pending


def conclusion(ctx: D.Ctx, ai: dict[str, Any]) -> list[Slide]:
    crumb = f"{PARTS[4]} · 结论与讨论"
    points = _texts(ai.get("takeaways"), 4) or [x for x in (D._cited(ctx.summary, "argument", []),) if x]
    questions = _texts(ai.get("questions"), 5, 110)
    if not points and not questions:
        return []
    if questions:
        return [Slide("questions", crumb=crumb, title="结论与疑问", cards=[Card(head="结论", points=points)],
                      points=questions)]
    return [Slide("points", crumb=crumb, title="结论", points=points)]


def build(conn: sqlite3.Connection, plan_id: str, *, deadline: float | None = None, seminar_date: str = "") -> Built:
    ctx = D.gather(conn, plan_id, seminar_date=seminar_date)
    ai = load_ai_text(plan_id)
    scripts, docs = coverage.author_texts(plan_id)
    found = method_notes.detect(scripts)
    pdf = D.PaperPdf(conn, ctx.plan.get("paper_id"))
    assets = Assets(deadline)
    notes = list(ctx.notes)
    try:
        thumb = assets.add("cover.png", _cover_thumb(pdf))
        slides = [cover(ctx, ai, thumb)]
        slides += study(ctx, ai)
        slides += data(ctx, ai, scripts, docs)
        slides += methods(ctx, ai, found)
        res, replicated, pending = results(ctx, pdf, assets, notes)
        slides += res
        slides += conclusion(ctx, ai)
    finally:
        pdf.close()
    _renumber(slides)
    notes += pdf.notes
    title = c(ctx.paper.get("title") or plan_id, 200)
    return Built(Deck(title, slides, assets.imgs), notes, replicated, pending)


def _renumber(slides: list[Slide]) -> None:
    """第一部分, 第二部分 … in the order the parts appear: a paper with no method found has no gap."""
    names: dict[str, str] = {}
    for s in slides:
        if s.kind == "section":
            names[s.no] = PARTS[min(len(names), len(PARTS) - 1)]
            s.no = names[s.no]
    for s in slides:
        old = s.crumb.split(" · ")[0]
        if s.kind == "section" or not old.startswith("第"):
            continue
        if old not in names:                      # a part with no title slide, like 结论: the next number
            names[old] = PARTS[min(len(names), len(PARTS) - 1)]
        s.crumb = names[old] + s.crumb[len(old):]


def save_assets(plan_id: str, deck: Deck) -> Path:
    """The deck's pictures as files for the preview page, replacing the last set."""
    folder = assets_dir(plan_id)
    ws = W.workspace_dir(plan_id)
    W._checked(ws, folder / "x")
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.iterdir():
        if old.is_file() and not old.is_symlink() and old.suffix in (".png", ".svg"):
            old.unlink()
    for name, img in deck.assets.items():
        path = folder / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, "wb") as fh:
            fh.write(img.data)
    _write_json(folder, "slides.json",
                {"title": deck.title, "at": time.strftime("%Y-%m-%dT%H:%M:%S") + f".{uuid.uuid4().hex[:6]}",
                 "slides": [s.__dict__ | {"cards": [x.__dict__ for x in s.cards]} for s in deck.slides],
                 "sizes": {k: [v.w, v.h] for k, v in deck.assets.items()}})
    return folder


# ---------------------------------------------------------------- Leo's edits to the deck's words

EDITS = "edits.json"
EDIT_CHARS = 400
MAX_EDITS = 2_000
EDITABLE = re.compile(r"(?:title|lede|src|subtitle|kicker|sub|verdict_note)|meta\.[0-3]|points\.\d{1,2}"
                      r"|cards\.[0-3]\.(?:tag|big|head|body|note)|cards\.[0-3]\.points\.[0-5]")


def slide_keys(slides: list[Slide]) -> list[str]:
    """A key per slide that survives a remake: its kind and its title as made, numbered when two share them."""
    seen: dict[str, int] = {}
    out = []
    for s in slides:
        k = f"{s.kind}:{s.title}"
        n = seen.get(k, 0)
        seen[k] = n + 1
        out.append(k if n == 0 else f"{k}#{n}")
    return out


def _set(slide: Slide, path: str, value: str) -> bool:
    """Put `value` at a field path of one slide (EDITABLE), only where that field already exists."""
    parts = path.split(".")
    try:
        if parts[0] == "cards":
            card = slide.cards[int(parts[1])]
            if parts[2] == "points":
                card.points[int(parts[3])] = value
            else:
                setattr(card, parts[2], value)
        elif parts[0] in ("meta", "points"):
            getattr(slide, parts[0])[int(parts[1])] = value
        else:
            setattr(slide, parts[0], value)
    except (IndexError, ValueError, AttributeError):
        return False
    return True


def load_edits(plan_id: str) -> dict[str, dict[str, str]]:
    path = assets_dir(plan_id) / EDITS
    try:
        if path.is_symlink() or path.stat().st_size > 2_000_000:
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return {}
    return {str(k): {str(f): c(v, EDIT_CHARS) for f, v in D._dict(fields).items() if EDITABLE.fullmatch(str(f))}
            for k, fields in D._dict(raw).items()}


def apply_edits(deck: Deck, edits: dict[str, dict[str, str]]) -> Deck:
    for key, slide in zip(slide_keys(deck.slides), deck.slides):
        for path, value in edits.get(key, {}).items():
            _set(slide, path, value)
    return deck


class StaleDeck(D.DeckError):
    """The deck was made again after the page with the edits was loaded."""


def saved_at(plan_id: str) -> str:
    """The stamp of the slides saved by the last make: the deck page sends it back with edits."""
    path = assets_dir(plan_id) / "slides.json"
    try:
        if path.is_symlink() or path.stat().st_size > 5_000_000:
            return ""
        return c(D._dict(json.loads(path.read_text(encoding="utf-8"))).get("at"), 40)
    except (OSError, ValueError, RecursionError):
        return ""


def save_edits(plan_id: str, changes: dict[str, Any], at: str | None = None) -> int:
    """Merge edits from the deck page, given as {"<slide index>|<field path>": text}, keyed to the slides as made.
    Returns how many were kept; a path that is not EDITABLE or names no slide is dropped. `at` is the stamp of the
    slides the page showed: a remake since then would put the edits on other slides, so they are refused."""
    made = load_saved(plan_id, edited=False)
    if made is None:
        raise D.DeckError("还没有展示稿")
    if at is not None and at != saved_at(plan_id):
        raise StaleDeck("展示稿刚重做过，刷新后再改")
    keys = slide_keys(made.slides)
    edits = load_edits(plan_id)
    kept = 0
    for spot, text in list(changes.items())[:MAX_EDITS]:
        index, _, path = str(spot).partition("|")
        if not re.fullmatch(r"[0-9]{1,4}", index) or int(index) >= len(keys) or not EDITABLE.fullmatch(path):
            continue
        probe = Slide(**{k: v for k, v in made.slides[int(index)].__dict__.items()})
        if not _set(probe, path, c(text, EDIT_CHARS)):
            continue
        edits.setdefault(keys[int(index)], {})[path] = c(text, EDIT_CHARS)
        kept += 1
    folder = assets_dir(plan_id)
    W._checked(W.workspace_dir(plan_id), folder / EDITS)
    _write_json(folder, EDITS, edits)
    return kept


def rewrite_pptx(plan_id: str) -> Path:
    """The pptx again from the saved slides, their pictures and Leo's edits: no rebuild, no model call."""
    deck = load_saved(plan_id, images=True)
    if deck is None:
        raise D.DeckError("还没有展示稿")
    listed = set(load_saved(plan_id, edited=False).assets)          # the pictures the slides were made with
    if listed - set(deck.assets) or any(not img.data for img in deck.assets.values()):
        raise D.DeckError("展示稿的图片不全，先 Remake 一次")      # never overwrite a good pptx with one missing pictures
    path = D.deck_path(plan_id)
    D._save_v2(plan_id, deck, path)
    return path


def load_saved(plan_id: str, *, edited: bool = True, images: bool = False) -> Deck | None:
    """The deck saved with the last pptx, with Leo's edits on top unless `edited` is False. For the preview page the
    pictures are only their sizes (the page reads them on request); `images` reads them for a pptx."""
    path = assets_dir(plan_id) / "slides.json"
    try:
        if path.is_symlink() or path.stat().st_size > 5_000_000:
            return None
        raw = D._dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, RecursionError):
        return None
    slides = []
    for s in D._list(raw.get("slides"), 200):
        s = D._dict(s)
        cards = [Card(**_typed(Card, D._dict(x))) for x in D._list(s.get("cards"), 8)]
        slides.append(Slide(**_typed(Slide, s), cards=cards))
    sizes = {}
    for k, v in list(D._dict(raw.get("sizes")).items())[:200]:
        if isinstance(v, list) and len(v) == 2 and all(isinstance(x, int) and 0 < x < 100_000 for x in v):
            sizes[str(k)] = D.Img(b"", v[0], v[1])
    if images:
        from .slides import asset_file
        for name in list(sizes):
            f = asset_file(assets_dir(plan_id), name)
            data = D._read_file(f) if f is not None else None
            img = D._png_bounded(data) if data else None
            if img is None:
                sizes.pop(name)
            else:
                sizes[name] = img
    deck = Deck(c(raw.get("title"), 200), slides, sizes)
    return apply_edits(deck, load_edits(plan_id)) if edited else deck


# ---------------------------------------------------------------- the report page (the same content, read as a page)

@dataclass
class PageView:
    plan: dict[str, Any]
    title: str
    status: str                                   # one line: how the replication came out
    deck: Deck | None                             # what the last make saved: study, data and results, ready to draw
    methods: list[dict[str, Any]]
    needs: list[dict[str, Any]] = field(default_factory=list)
    ai: dict[str, Any] = field(default_factory=dict)
    package: dict[str, list[str]] = field(default_factory=dict)

    def results(self) -> list[Slide]:
        """The result pages, in the overview's order: one per table or figure with something to set beside the paper."""
        return [s for s in self.part("复现结果") if s.kind == "compare"]

    def overview(self) -> Slide | None:
        """The overview, its pages joined back into one table."""
        pages = [s for s in self.part("复现结果") if s.kind == "table" and s.title.startswith("复现概览")]
        if not pages:
            return None
        return Slide("table", title="复现概览", columns=pages[0].columns, rows=[r for s in pages for r in s.rows],
                     chips=[x for s in pages for x in s.chips])

    def chart(self, title: str) -> Slide | None:
        """The check chart that follows a result page, if one was drawn."""
        head = title.split(" · ")[0]
        return next((s for s in self.part("复现结果") if s.kind == "chart" and s.title.startswith(head + " ")), None)

    def conclusion(self) -> Slide | None:
        return next((s for s in self.part("复现结果") if s.kind in ("questions", "points")), None)

    def entries(self) -> dict[str, str]:
        """One line under each way into the report."""
        st = self.study()
        backed = [m["tag"] or m["name"] for m in st["models"] if m["support"] == "支持"]
        if st["models"]:
            question = f"{len(st['models'])} 种解释" + (f"，{'、'.join(backed)}得到支持" if backed else "")
        else:
            question = st["question"]
        data = self.part("数据与测量")
        sources = [f"{c.big} {c.head}".strip() for s in data[:1] for c in s.cards if c.head]
        ov = self.overview()
        problems = [f"{r[0].replace('正文', '')} {r[3]}" for r, chip in zip(ov.rows, ov.chips) if chip != "ok"] if ov else []
        n = len(self.deck.slides) if self.deck else 0
        return {"study": question, "data": " · ".join(sources[:3]),
                "methods": " · ".join(m["name"] for m in self.methods[:4]),
                "results": "；".join([self.status] + (["、".join(problems[:3]) + (" 等" if len(problems) > 3 else "")] if problems else [])),
                "deck": f"{n} 页" if n else ""}

    def study(self) -> dict[str, Any]:
        """The question, the competing explanations with what the paper's results say of each, the findings and the
        questions: from the model-written text when there is one, else from the saved slides."""
        ai = self.ai
        support = {"支持": "ok", "部分支持": "warn", "不支持": "crit"}
        models = [{"tag": c(m.get("tag"), 12), "name": c(m.get("name"), 24), "claim": c(m.get("claim"), 120),
                   "measure": c(m.get("measure"), 60), "support": c(m.get("support"), 6),
                   "tone": support.get(str(m.get("support") or ""), "")}
                  for m in (D._dict(x) for x in D._list(ai.get("models"), 4)) if m.get("name") or m.get("tag")]
        slides = self.part("研究问题与理论")
        if not models:
            for sl in slides[:1]:
                models = [{"tag": "", "name": x.head, "claim": x.body, "measure": "", "support": "", "tone": ""} for x in sl.cards]
        end = self.conclusion()
        return {"title": c(ai.get("question_title"), 24),
                "question": c(ai.get("question"), 160) or next((sl.lede for sl in slides if sl.lede), ""),
                "models": models,
                "findings": _texts(ai.get("findings"), 4, 120) or [p for sl in slides if sl.title == "主要发现" for x in sl.cards for p in x.points],
                "takeaways": _texts(ai.get("takeaways"), 4, 120) or (end.cards[0].points if end is not None and end.cards else []),
                "questions": _texts(ai.get("questions"), 5, 120) or (end.points if end is not None and end.kind == "questions" else [])}

    def data(self) -> dict[str, Any]:
        """Headline numbers of the data, the steps that build the key measure, and what the replication package holds."""
        ai = self.ai
        sources = [{"big": c(x.get("big"), 10), "name": c(x.get("name"), 24), "what": c(x.get("what"), 90)}
                   for x in (D._dict(y) for y in D._list(ai.get("sources"), 4)) if x.get("name")]
        slides = self.part("数据与测量")
        if not sources:
            for sl in slides[:1]:
                sources = [{"big": x.big, "name": x.head, "what": x.body} for x in sl.cards]
        steps = [{"name": c(x.get("name"), 16), "what": c(x.get("what"), 100)}
                 for x in (D._dict(y) for y in D._list(ai.get("measurement"), 3)) if x.get("name")]
        flow = next((sl for sl in slides if sl.kind == "flow"), None)
        if not steps and flow is not None:
            steps = [{"name": x.head, "what": x.body} for x in flow.cards]
        return {"unit": c(ai.get("unit"), 120) or next((sl.lede for sl in slides if sl.lede), ""),
                "sources": sources, "steps": steps,
                "measure_title": c(ai.get("measurement_title"), 24) or (flow.title if flow is not None else ""),
                "measure_note": c(ai.get("measurement_note"), 120) or (flow.src if flow is not None else ""),
                "package": self.package}

    def matrix(self) -> tuple[list[str], list[dict[str, Any]]]:
        """Which method makes which result: the result labels as columns, one row per method."""
        cols = [it["label"].replace("正文", "").replace("附录", "附") for it in self.result_items() if it.get("page")]
        rows = [{"name": m["name"], "key": m["key"], "cells": [lab in m.get("used_short", []) for lab in cols]}
                for m in self.methods]
        return cols, [r for r in rows if any(r["cells"])]

    def result_items(self) -> list[dict[str, Any]]:
        """Every row of the overview, with what opens under it: the paper's original, our table or figure, the check
        chart, and a few words on how to read each picture (shown beside it when it is opened large)."""
        ov = self.overview()
        if ov is None:
            return []
        pages = {s.title.split(" · ")[0]: s for s in self.results()}
        out = []
        for row, chip in zip(ov.rows, ov.chips):
            where, caption, script, status = (list(row) + ["", "", "", ""])[:4]
            short = where.replace("正文", "").replace("附录", "")
            s = pages.get(short)
            item = {"label": where, "caption": caption, "script": script, "status": status, "ok": chip == "ok",
                    "page": s is not None}
            if s is not None:
                kind = "图" if short.startswith("图") else "表"
                item.update(title=s.title, src=s.src, columns=s.columns, rows=s.rows, hits=s.hits, verdict=s.verdict,
                            verdict_ok=s.verdict_ok, verdict_note=s.verdict_note,
                            original={"name": s.image, "label": s.image_label,
                                      "note": f"{s.image_label}。复现要对上的数字取自这张原{kind}。"} if s.image else None,
                            original_missing="" if s.image else s.image_label,
                            ours={"name": s.image2, "label": s.image2_label or "复现的图",
                                  "note": "作者的脚本在这次运行里画出的图。和论文原图对照，看形状、方向和位置是否一致。"} if s.image2 else None)
                chart = self.chart(s.title)
                if chart is not None and chart.image:
                    item["chart"] = {"name": chart.image, "label": f"{short} · 对照图",
                                     "note": "每一行是一个系数。空心点是论文报告的估计，实心点是这次复现的估计，横线是 95% "
                                             "置信区间。两点重合就是一致；实心点的颜色是判定：绿色一致，黄色只有方向一致，红色对不上。"
                                             + (f" 这张表：{s.verdict}。" if s.verdict else "")}
            out.append(item)
        return out

    def part(self, kind_title: str) -> list[Slide]:
        """The saved slides of one part, by its section title, without the section slide itself."""
        if self.deck is None:
            return []
        out, inside = [], False
        for s in self.deck.slides:
            if s.kind == "section":
                inside = s.title == kind_title
                continue
            if inside:
                out.append(s)
        return out


def status_line(conn: sqlite3.Connection, plan_id: str) -> str:
    """「20 个数里 14 个一致」 from the last comparison, or what is missing before there can be one."""
    run = W.latest_valid(conn, plan_id)
    if run is None:
        return "还没有运行"
    summary = D._dict(D._dict(D._dict(run.get("workspace")).get("comparison")).get("summary"))
    if not summary:                                   # no Compare stored yet: compare now, as the deck does
        from . import compare, plans
        result = D._dict(run.get("result"))
        targets = plans.target_records(conn, plan_id)
        try:
            if targets and result.get("results"):
                summary = D._dict(compare.compare(targets, result["results"], D._dict(result.get("mapping")) or None).get("summary"))
        except (ValueError, TypeError, KeyError, ArithmeticError) as e:
            log.info("status compare failed for %s: %s", plan_id, e)
    counts = D._dict(summary.get("counts"))
    total = sum(v for v in counts.values() if isinstance(v, int))
    if not total:
        return "已运行，还没有和论文对照"
    ok = sum(counts.get(k, 0) for k in OK if isinstance(counts.get(k), int))
    return f"{total} 个数里 {ok} 个一致"


def needs_of(plan_id: str) -> list[dict[str, Any]]:
    try:
        from . import pipeline
    except ImportError:
        return []
    try:
        return [D._dict(n) for n in D._list(D._dict(pipeline.state(plan_id)).get("needs"), 20)]
    except Exception as e:                                   # a broken state file never breaks the page
        log.info("pipeline state unreadable for %s: %s", plan_id, e)
        return []


def page_view(conn: sqlite3.Connection, plan: dict[str, Any]) -> PageView:
    plan_id = str(plan["id"])
    saved = load_saved(plan_id) if D.info(plan_id)["exists"] else None
    scripts, docs = coverage.author_texts(plan_id)
    found = method_notes.detect(scripts)
    ai = load_ai_text(plan_id)
    usage = D._dict(ai.get("method_usage"))
    rows = coverage.items(conn, plan_id)
    methods = []
    picked = _pick_methods(found)
    main_scripts = {n.lower() for r in rows if r.get("group") == "main" for n in D._scripts_of(r)}
    for f in picked + [x for x in found if x not in picked]:    # the deck's teaching order, then the rest
        if f.chapter is None:
            continue
        mine = {posixpath.basename(s).lower() for s in f.scripts}
        used = [_where(r) for r in rows if r.get("group") == "main" and mine & {n.lower() for n in D._scripts_of(r)}]
        snip = next((h for h in f.snippets if posixpath.basename(h.script).lower() in main_scripts),
                    f.snippets[0] if f.snippets else None)
        ch = f.chapter
        methods.append({"key": f.key, "name": ch.name, "summary": ch.summary, "chapter": ch.markdown,
                        "formula": chapter_formula(ch.sections.get("模型设定", "")),
                        "checks": chapter_checks(ch.sections.get("稳健性检验：做什么，为什么", ""), 8),
                        "assumptions": [a for a, _ in chapter_assumptions(ch.sections.get("识别假设", ""), 6)],
                        "reading": first_bullets(ch.sections.get("怎么读结果", ""), 2, 90),
                        "used_short": [u.replace("正文", "").replace("附录", "附") for u in used],
                        "usage": c(usage.get(f.key), 120) or ("用在" + "、".join(used[:5]) if used else ""),
                        "scripts": [c(posixpath.basename(s), 60) for s in f.scripts[:4]],
                        "code": code_text(snip.code) if snip else "",
                        "where": f"{c(posixpath.basename(snip.script), 60)} · 第 {snip.line} 行" if snip else ""})
    title = c(plan.get("paper_title") or plan_id, 200)
    data_files = [c(d.get("name") or d.get("id"), 60) for d in W.plan_datasets(conn, plan)][:12]
    package = {"scripts": sorted(c(posixpath.basename(k), 60) for k in list(scripts)[:400])[:40],
               "docs": sorted(c(posixpath.basename(k), 60) for k in list(docs)[:100])[:12], "data": data_files}
    return PageView(plan, title, status_line(conn, plan_id), saved, methods, needs_of(plan_id), ai, package)


def method_page(conn: sqlite3.Connection, plan_id: str, key: str) -> dict[str, Any] | None:
    """One method of this paper: its chapter, how the paper uses it, and every place the author's code calls it."""
    ch = method_notes.chapter(key)
    if ch is None:
        return None
    scripts, _ = coverage.author_texts(plan_id)
    found = next((f for f in method_notes.detect(scripts) if f.key == key), None)
    usage = c(D._dict(load_ai_text(plan_id).get("method_usage")).get(key), 120)
    return {"chapter": ch, "usage": usage,
            "snippets": [{"code": h.code, "where": f"{posixpath.basename(h.script)} · 第 {h.line} 行"} for h in (found.snippets if found else [])],
            "in_paper": found is not None}
