"""Replication v2 step 4 (card 0160): line a run's output tables up against the paper's reported targets.

Pure functions: no DB, no AI, inputs are never mutated. The shapes are the appendix of docs/design/REPLICATION-v2.md:

- targets as R1 keeps them: the appendix spelling (table, numeric value / se / ci, sig, n, plus value_text and
  se_text, the paper's printed text), or card 0157's all-text fields (label "Table 2", value "−0.231", uncertainty
  "(0.11)" or "[0.10, 0.35]", significance "**" or "p < 0.05", n "1,204"). A target without an id is T1, T2, … in
  list order.
- results: R2's outputs/results.json, {"tables": [{"label", "model", "rows": [{"term", "estimate", "se", "p", "ci",
  "n"}], "needs_r"}]}; and mapping: outputs/mapping.json, {"T1": ["Table 2", "(3)", "treated_post"]}.

The verdicts only say where a number does or does not line up. Nothing here says a paper is wrong; the summary is a
rule-built paragraph for Leo, and the possible reasons for a mismatch are the AI's job in a later step."""
from __future__ import annotations

import math
import re
import unicodedata
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Sequence

VERDICTS = ("exact", "within", "direction", "mismatch", "missing")
LABELS = {"exact": "完全一致", "within": "在误差范围内", "direction": "方向一致但大小或显著性不同",
          "mismatch": "对不上", "missing": "没找到对应输出"}
# p thresholds for one, two and three stars. econ is the "* p<.1, ** p<.05, *** p<.01" legend; apa is the
# .05 / .01 / .001 legend common in political science and psychology. A paper's own legend can be passed as a tuple.
STAR_CONVENTIONS: dict[str, tuple[float, ...]] = {"econ": (0.10, 0.05, 0.01), "apa": (0.05, 0.01, 0.001)}
Z95 = 1.959963984540054   # a printed CI is read as 95%: se = width / (2 × 1.96)

# Squashed spelling → canonical term. The interaction aliases cover how DID papers and code name treat × post.
TERM_ALIASES = {
    **dict.fromkeys(("did", "dd", "diffindiff", "treatpost", "treatedpost", "treatmentpost", "posttreat",
                     "posttreated", "posttreatment", "treatxpost", "treatedxpost", "treatafter", "treatedafter"),
                    "post×treat"),
    **dict.fromkeys(("const", "constant", "cons", "intercept", "(intercept)", "截距", "常数项", "常数"), "const"),
}
# The parts of an interaction, so treat×post, treated:post and post*treatment all land on post×treat.
COMPONENT_ALIASES = {**dict.fromkeys(("treat", "treated", "treatment", "treatgroup"), "treat"),
                     **dict.fromkeys(("post", "after", "postperiod"), "post")}
_SIGN_WORDS = {**dict.fromkeys(("+", "positive", "pos", "正", "正向", "为正", "正相关", "↑"), 1),
               **dict.fromkeys(("-", "negative", "neg", "负", "负向", "为负", "负相关", "↓"), -1)}
_NUM = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?", re.I)
_P_BOUND = re.compile(r"(?:p\s*)?(<=|≤|<|=|>=|≥|>)\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?)", re.I)
_INTERACTION = re.compile(r"\s*(?:×|✕|\*|:|#|·|&)\s*|\s+x\s+")
_LOG = re.compile(r"^(?:log|ln)\s*(?:\((.+)\)|[_\s]+(.+))$")
_SQUASH = re.compile(r"[\s_\-.]+")
_ROMAN = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100}
_ROMAN_FORM = re.compile(r"(?=[ivxlc])c{0,3}(?:xc|xl|l?x{0,3})(?:ix|iv|v?i{0,3})")


# ── reading printed values ───────────────────────────────────────────────────────────────────────────────────

def _text(v: Any) -> str:
    return "" if v is None else unicodedata.normalize("NFKC", str(v)).replace("−", "-").strip()


def _blank(v: Any) -> bool:
    """None, empty text, or a non-finite float (results.json written by json.dump can carry NaN and Infinity)."""
    return v is None or (isinstance(v, str) and not v.strip()) or (isinstance(v, float) and not math.isfinite(v))


def _float(v: Any) -> float | None:
    if _blank(v) or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    n = printed_number(v)
    return n[0] if n else None


def printed_number(text: Any) -> tuple[float, int] | None:
    """A number as printed and its decimals: '−0.140' → (-0.14, 3), '1,204' → (1204.0, 0), '(0.051)' → (0.051, 3)."""
    s = re.sub(r"^[‒–—]", "-", _text(text))
    s = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", s).strip("()[]{} ")
    if not _NUM.fullmatch(s):
        return None
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    exp = d.as_tuple().exponent
    return float(d), max(0, -exp) if isinstance(exp, int) else 0


def _reported(v: Any) -> dict[str, Any]:
    """value, decimals (None when they cannot be counted), stars printed after the value, and sign."""
    if _blank(v) or isinstance(v, bool):
        return {"value": None, "decimals": None, "stars": "", "sign": None}
    if isinstance(v, (int, float)):
        return {"value": float(v), "decimals": None, "stars": "", "sign": _sign(float(v))}
    s = _text(v)
    stars = ""
    if m := re.search(r"(\*+|†)\s*$", s):
        stars, s = m.group(1), s[:m.start()].strip()
    if n := printed_number(s):
        return {"value": n[0], "decimals": n[1], "stars": stars, "sign": _sign(n[0])}
    return {"value": None, "decimals": None, "stars": stars, "sign": _SIGN_WORDS.get(s.casefold())}


def _sign(x: float | None) -> int | None:
    if x is None:
        return None
    return (x > 0) - (x < 0)


def _range(v: Any) -> tuple[float, float] | None:
    """A printed interval: [0.10, 0.35], (0.10; 0.35), 0.10–0.35, 0.10 to 0.35, or a JSON pair."""
    if isinstance(v, (list, tuple)):
        nums = [_float(x) for x in v]
    else:
        s = re.sub(r"^[‒–—]", "-", _text(v)).strip("()[]{} ")
        parts = re.split(r"\s*[,;]\s*|\s+to\s+|(?<=\d)\s*[-‒–—~～]\s*(?=[+-]?\.?\d)", s, maxsplit=1)
        nums = [_float(p) for p in parts]
    if len(nums) != 2 or None in nums:
        return None
    lo, hi = sorted(nums)          # type: ignore[type-var]
    return lo, hi


def _count(v: Any) -> int | None:
    if _blank(v) or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(round(v))
    s = re.sub(r"^\s*(?:n|obs\.?|observations)\s*[=:]?\s*", "", _text(v), flags=re.I)
    n = printed_number(s.replace(" ", ""))
    return int(round(n[0])) if n else None


# ── significance ─────────────────────────────────────────────────────────────────────────────────────────────

def thresholds(stars: str | Sequence[float] = "econ") -> tuple[float, ...]:
    """p thresholds for one, two, three … stars, loosest first."""
    if isinstance(stars, str):
        if stars not in STAR_CONVENTIONS:
            raise ValueError(f"unknown star convention {stars!r}; known: {', '.join(STAR_CONVENTIONS)}")
        return STAR_CONVENTIONS[stars]
    th = tuple(sorted((float(t) for t in stars), reverse=True))
    if not th or any(not 0 < t < 1 for t in th):
        raise ValueError("star thresholds are p values between 0 and 1")
    return th


def p_band(p: float, stars: str | Sequence[float] = "econ") -> int:
    """How many stars a p value earns under the convention: 0.006 → 3 under econ, 2 under apa."""
    return sum(1 for t in thresholds(stars) if p < t)


def sig_band(text: Any, stars: str | Sequence[float] = "econ") -> int | None:
    """The significance band a paper prints: '***' → 3, 'p < .05' → 2 (econ), 'n.s.' → 0, '†' → p < .1, a bare
    '0.032' → its p. None when nothing usable is printed. A p bound wins over stars, since it carries its own legend."""
    th = thresholds(stars)
    if _blank(text) or isinstance(text, bool):
        return None
    if isinstance(text, (int, float)):
        return p_band(float(text), th) if 0 <= text <= 1 else None
    s = _text(text).casefold()
    if m := _P_BOUND.search(s):
        op, bound = m.group(1), float(m.group(2))
        if op == "=":
            return p_band(bound, th)
        if op in (">", ">=", "≥"):
            return 0                                        # "p > .05" is how a paper says not significant
        return sum(1 for t in th if bound <= t + 1e-12)
    if re.fullmatch(r"n\.?\s*s\.?|not\s+significant|insignificant|不显著", s):
        return 0
    if "*" in s:
        return min(s.count("*"), len(th))
    if s in ("†", "+"):
        return sum(1 for t in th if 0.10 <= t + 1e-12)
    n = printed_number(s)
    if n and 0 <= n[0] <= 1:
        return p_band(n[0], th)
    return None


def _z_band(value: float | None, se: float | None, th: tuple[float, ...]) -> int | None:
    if value is None or not se:
        return None
    return p_band(math.erfc(abs(value / se) / math.sqrt(2)), th)


# ── names: tables, models, terms ─────────────────────────────────────────────────────────────────────────────

def _roman(s: str) -> int | None:
    if not _ROMAN_FORM.fullmatch(s):
        return None
    total = 0
    for a, b in zip(s, s[1:] + " "):
        v = _ROMAN[a]
        total += -v if b in _ROMAN and _ROMAN[b] > v else v
    return total


def norm_table(s: Any) -> str:
    """'Table 2', 'table  2', 'TABLE 2.', '表 2' and 'Table II' are one table; 'Table 2' and 'Table 2A' are not."""
    t = _text(s).casefold()
    t = re.sub(r"^(?:tab\.|tab(?=\s*\d)|表)\s*", "table ", t)
    t = re.sub(r"^(?:fig\.|fig(?=\s*\d)|图)\s*", "figure ", t)
    t = re.sub(r"[\s.:：]+", "", t)
    if (m := re.fullmatch(r"(table|figure)([ivxlc]+)", t)) and (n := _roman(m.group(2))):
        t = f"{m.group(1)}{n}"
    return t


def norm_model(s: Any) -> str:
    """'(3)', 'Model 3', '3', 'M3', 'Column (3)', '(iii)' and '第3列' are one model."""
    t = _text(s).casefold()
    t = re.sub(r"^(?:models?|columns?|col\.?|spec(?:ification)?|no\.?|m|模型|第)\s*(?=[(\[\d]|[ivxlc]+\)?$)", "", t)
    t = re.sub(r"列$", "", t)
    t = re.sub(r"[\s()\[\]#.]+", "", t)
    if (n := _roman(t)) is not None:
        t = str(n)
    return t


def norm_term(s: Any, aliases: dict[str, str] | None = None) -> str:
    """'treat×post', 'treated_post', 'Post:Treated' and 'did' are one term; so are '_cons' and '(Intercept)', and
    'log(GDP)' and 'ln_gdp'. `aliases` adds squashed spellings → canonical terms on top of TERM_ALIASES."""
    table = {**TERM_ALIASES, **{_SQUASH.sub("", k.casefold()): v for k, v in (aliases or {}).items()}}
    t = _text(s).casefold()
    if (whole := _SQUASH.sub("", t)) in table:
        return table[whole]
    parts = []
    for p in (p for p in _INTERACTION.split(t) if p.strip()):
        if m := _LOG.match(p.strip()):
            p = f"log({m.group(1) or m.group(2)})"
        q = _SQUASH.sub("", p)
        parts.append(table.get(q, COMPONENT_ALIASES.get(q, q)))
    out = "×".join(sorted(parts) if len(parts) > 1 else parts)
    return table.get(out, out)


# ── normalizing the two sides ────────────────────────────────────────────────────────────────────────────────

def _pick(d: dict[str, Any], *keys: str) -> Any:
    return next((d[k] for k in keys if not _blank(d.get(k))), None)


def _target(t: dict[str, Any], i: int, th: tuple[float, ...]) -> dict[str, Any]:
    # Decimals come only from printed text: value_text, or a value that is itself text (card 0157). A bare JSON
    # number has lost its trailing zeros (0.140 arrives as 0.14), so its decimals stay unknown and exact is off.
    printed = _reported(_pick(t, "value_text"))
    rep = _reported(_pick(t, "value", "estimate", "reported_value"))
    value = rep["value"] if rep["value"] is not None else printed["value"]
    decimals = printed["decimals"] if printed["decimals"] is not None else rep["decimals"]
    rep["stars"] = rep["stars"] or printed["stars"]
    rep["sign"] = rep["sign"] or printed["sign"]
    se, ci, se_from = _float(_pick(t, "se", "std_error")), _range(_pick(t, "ci", "confidence_interval")), "se"
    se_printed = printed_number(_pick(t, "se_text"))
    unc = _pick(t, "uncertainty", "se_text")
    if unc is not None and se is None and ci is None:
        s = _text(unc)
        if m := re.search(r"\b[tz]\s*[=:]\s*(\S+)", s, re.I):
            tstat = _float(m.group(1))
            se, se_from = (abs(value / tstat), "t") if value is not None and tstat else (None, None)
        elif (ci := _range(unc)) is None:
            se = _float(unc)
    if se is None and ci is not None:
        se, se_from = (ci[1] - ci[0]) / (2 * Z95), "ci"
    se = abs(se) if se is not None else None
    sig_text = _pick(t, "sig", "significance", "stars", "p") or rep["stars"]
    band, band_from = sig_band(sig_text, th), "printed"
    if band is None and (band := _z_band(value, se, th)) is not None:
        band_from = "z"
    sign = rep["sign"]
    if value is None:
        explicit = _pick(t, "direction", "sign")
        sign = _SIGN_WORDS.get(_text(explicit).casefold()) if isinstance(explicit, str) else _sign(_float(explicit))
        sign = sign or rep["sign"] or (_sign(ci[0]) if ci and _sign(ci[0]) == _sign(ci[1]) else None)
    return {"id": str(t["id"]) if not _blank(t.get("id")) else f"T{i + 1}",
            "table": _text(_pick(t, "table", "label", "figure")), "model": _text(_pick(t, "model", "column")),
            "term": _text(_pick(t, "term", "coefficient", "variable")), "page": _pick(t, "page"),
            "value": value, "decimals": decimals, "sign": sign, "se": se, "se_from": se_from if se is not None else None,
            "se_decimals": se_printed[1] if se_printed else None,
            "sig": _text(sig_text), "band": band, "band_from": band_from if band is not None else None,
            "n": _count(_pick(t, "n", "N", "observations"))}


def _entries(results: dict[str, Any] | None) -> list[dict[str, Any]]:
    out = []
    for table in (results or {}).get("tables") or []:
        for row in table.get("rows") or []:
            out.append({"label": _text(table.get("label")), "model": _text(row.get("model") or table.get("model")),
                        "term": _text(row.get("term")), "row": row, "table": table})
    return out


def _ours(e: dict[str, Any], th: tuple[float, ...]) -> dict[str, Any]:
    row = e["row"]
    est, se, p, ci = _float(row.get("estimate")), _float(row.get("se")), _float(row.get("p")), _range(row.get("ci"))
    if se is None and ci is not None:
        se = (ci[1] - ci[0]) / (2 * Z95)
    band = p_band(p, th) if p is not None and 0 <= p <= 1 else sig_band(_pick(row, "sig", "stars"), th)
    if band is None:
        band = _z_band(est, se, th)
    n = _count(row.get("n"))
    return {"estimate": est, "se": abs(se) if se is not None else None, "p": p, "band": band,
            "sig": "*" * band if band is not None else "", "n": n if n is not None else _count(e["table"].get("n"))}


# ── matching ─────────────────────────────────────────────────────────────────────────────────────────────────

def _where(table: str, model: str, term: str) -> str:
    return " · ".join(x for x in (table, model, term) if x) or "这一项"


def _from_mapping(spec: Any, entries: list[dict[str, Any]], aliases: dict[str, str] | None
                  ) -> tuple[dict[str, Any] | None, str]:
    if isinstance(spec, dict):
        spec = [spec.get("table") or spec.get("label"), spec.get("model"), spec.get("term")]
    if not spec:
        return None, "mapping.json 标明这一项没有对应输出"
    table, model, term = (list(spec) + [None, None, None])[:3]
    hits = [e for e in entries if norm_table(e["label"]) == norm_table(table)
            and (not norm_model(model) or norm_model(e["model"]) == norm_model(model))
            and norm_term(e["term"], aliases) == norm_term(term, aliases)]
    where = _where(_text(table), _text(model), _text(term))
    if not hits:
        return None, f"mapping.json 指定的 {where} 在输出里找不到"
    if len(hits) > 1:
        return None, f"mapping.json 指定的 {where} 命中 {len(hits)} 行，要把 mapping 写清楚，比如写上模型列"
    return hits[0], ""


def _auto(t: dict[str, Any], entries: list[dict[str, Any]], labels: set[str], aliases: dict[str, str] | None
          ) -> tuple[dict[str, Any] | None, str]:
    """Match on (table, model, term) after normalization, inside one table only: a term that only turns up in
    another table is missing here, never borrowed."""
    wanted = norm_table(t["table"])
    if not wanted:
        if len(labels) != 1:
            return None, "复现目标没写表名，输出里又不止一张表"
        wanted = next(iter(labels))
    if wanted not in labels:
        return None, f"输出里没有 {t['table']}"
    same = [e for e in entries if norm_table(e["label"]) == wanted]
    term = norm_term(t["term"], aliases)
    rows = [e for e in same if norm_term(e["term"], aliases) == term]
    if not rows:
        return None, f"{t['table'] or '输出表'} 里没有 {t['term'] or '这一项'}"
    if model := norm_model(t["model"]):
        named = [e for e in rows if norm_model(e["model"]) == model]
        bare = [e for e in rows if not norm_model(e["model"])]
        if not named and bare and len({norm_model(e["model"]) for e in same}) > 1:
            return None, (f"{t['table']} 的输出行没写模型列，这张表又不止一个模型，"
                          f"对不上 {t['model']}，要在 mapping.json 里指定")
        rows = named or bare        # a row without a model stands in only when its table has just that one model
        if not rows:
            return None, f"{t['table']} 里没有模型 {t['model']} 的 {t['term']}"
    if len(rows) > 1:
        return None, f"{_where(t['table'], t['model'], t['term'])} 在输出里有 {len(rows)} 个候选，要在 mapping.json 里指定"
    return rows[0], ""


# ── judging ──────────────────────────────────────────────────────────────────────────────────────────────────

def _fmt(x: float) -> str:
    return f"{x:.4g}"


def _stars(band: int) -> str:
    return " " + "*" * band if band else "不显著"


def _rounded(x: float, decimals: int) -> Decimal:
    return Decimal(repr(x)).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)


def _judge(p: dict[str, Any], o: dict[str, Any]) -> tuple[str, list[str], float | None, float | None]:
    """verdict, reasons, difference, difference in SEs. Order matters: an opposite sign, or the paper significant and
    ours not, is a mismatch even when the estimate rounds to the paper's. The paper not significant and ours
    significant with the same sign is only a direction."""
    est = o["estimate"]
    if est is None:
        return "missing", ["输出行没有估计值"], None, None
    if p["value"] is None:                               # direction only: compared by sign alone
        if p["sign"] is None:
            return "missing", ["论文报告值没有数值也没有方向，没法比"], None, None
        if _sign(est) == p["sign"]:
            return "direction", ["论文只给了方向，方向一致"], None, None
        return "mismatch", ["论文只给了方向，复现的方向相反" if est else "论文只给了方向，复现估计为 0"], None, None
    value = p["value"]
    diff = est - value
    se = p["se"] if p["se"] else o["se"]
    diff_se = abs(diff) / se if se else None
    if value * est < 0:
        return "mismatch", [f"符号相反：论文 {_fmt(value)}，复现 {_fmt(est)}"], diff, diff_se
    bp, bo = p["band"], o["band"]
    if bp and bo == 0:
        return "mismatch", [f"论文显著{_stars(bp)}，复现不显著"], diff, diff_se
    same_band = bp is None or bo is None or bp == bo
    band_note = f"显著性档不同：论文{_stars(bp)}，复现{_stars(bo)}" if not same_band else ""
    decimals = p["decimals"]
    if decimals is not None and _rounded(est, decimals) == _rounded(value, decimals):
        if same_band:
            return "exact", [f"四舍五入到 {decimals} 位小数后相同"], diff, diff_se
        return "direction", [f"数值四舍五入后相同，但{band_note}"], diff, diff_se
    size = f"差 {_fmt(diff)}" + (f"，约 {diff_se:.2g} 个标准误" if diff_se is not None else "，论文和输出都没有标准误")
    if decimals is None:
        size += "，论文没留原文，小数位未知，不判完全一致"
    if diff_se is not None and abs(diff) <= se * (1 + 1e-9) and same_band:
        return "within", [size + "，显著性档相同" if bp is not None and bo is not None else size], diff, diff_se
    return "direction", [x for x in (size, band_note) if x], diff, diff_se


def compare(targets: list[dict[str, Any]], results: dict[str, Any] | None, mapping: dict[str, Any] | None = None,
            *, stars: str | Sequence[float] = "econ", aliases: dict[str, str] | None = None) -> dict[str, Any]:
    """Per-target verdicts with both sides' numbers, plus a summary: counts per verdict, whether N matches, and a
    short rule-built paragraph. mapping.json, when it names a target, overrides the automatic match for that target."""
    th = thresholds(stars)
    entries = _entries(results)
    tables = (results or {}).get("tables") or []
    labels = {norm_table(x.get("label")) for x in tables}
    needs_r = {norm_table(x.get("label")) for x in tables if x.get("needs_r")}
    mapping = mapping or {}
    items = []
    for i, raw in enumerate(targets or []):
        t = _target(raw, i, th)
        if t["id"] in mapping:
            entry, why = _from_mapping(mapping[t["id"]], entries, aliases)
            via = "mapping"
        else:
            entry, why = _auto(t, entries, labels, aliases)
            via = "auto"
        ours = _ours(entry, th) if entry else None
        if entry is None:
            verdict, reasons, diff, diff_se = "missing", [why], None, None
            if norm_table(t["table"]) in needs_r:
                reasons.append("这张表需要 R，还没跑")
        else:
            verdict, reasons, diff, diff_se = _judge(t, ours)
        n_match = None if ours is None or t["n"] is None or ours["n"] is None else t["n"] == ours["n"]
        items.append({
            "id": t["id"], "table": t["table"], "model": t["model"], "term": t["term"], "page": t["page"],
            "verdict": verdict, "via": via if entry else None,
            "matched": {"table": entry["label"], "model": entry["model"], "term": entry["term"]} if entry else None,
            "paper": {k: t[k] for k in ("value", "decimals", "sign", "se", "se_from", "se_decimals", "sig", "band",
                                        "band_from", "n")},
            "ours": ours, "diff": diff, "diff_se": diff_se, "n_match": n_match, "reasons": reasons,
        })
    return {"items": items, "summary": summarize(items, th)}


def summarize(items: list[dict[str, Any]], th: tuple[float, ...] = STAR_CONVENTIONS["econ"]) -> dict[str, Any]:
    counts = {v: sum(1 for x in items if x["verdict"] == v) for v in VERDICTS}
    checked = [x for x in items if x["n_match"] is not None]
    n_diffs = [{"id": x["id"], "paper": x["paper"]["n"], "ours": x["ours"]["n"]} for x in checked if not x["n_match"]]
    n_match = None if not checked else not n_diffs
    return {"total": len(items), "counts": counts, "n_match": n_match, "n_diffs": n_diffs, "stars": list(th),
            "text": _paragraph(items, counts, n_match, n_diffs)}


def _ids(items: list[dict[str, Any]], limit: int = 5) -> str:
    ids = [x["id"] for x in items]
    return "、".join(ids[:limit]) + (f" 等 {len(ids)} 项" if len(ids) > limit else "")


def _paragraph(items: list[dict[str, Any]], counts: dict[str, int], n_match: bool | None,
               n_diffs: list[dict[str, Any]]) -> str:
    total = len(items)
    if not total:
        return "没有复现目标，没法对照。"
    out = [f"共 {total} 项复现目标：" + "，".join(f"{counts[v]} 项{LABELS[v]}" for v in VERDICTS if counts[v]) + "。"]
    if counts["exact"] == total:
        out.append("全部完全一致。")
    elif counts["exact"] + counts["within"] == total:
        out.append("全部在误差范围内复现出来了。")
    elif counts["missing"] == total:
        out.append("输出里没有找到任何对应的结果，先检查输出表的表名、模型列和变量名，或在 mapping.json 里指定。")
    elif not counts["mismatch"] and not counts["missing"]:
        out.append("方向全部一致，有的大小或显著性不同。")
    if n_match is True:
        out.append("能对照的样本量都一致。")
    elif n_match is False:
        shown = "；".join(f"{d['id']} 论文 {d['paper']}、复现 {d['ours']}" for d in n_diffs[:3])
        out.append(f"样本量不一致：{shown}" + (f"；另有 {len(n_diffs) - 3} 项" if len(n_diffs) > 3 else "") + "。")
    else:
        out.append("没有能对照的样本量。")
    if mism := [x for x in items if x["verdict"] == "mismatch"]:
        out.append(f"对不上的是 {_ids(mism)}，要逐项查原因。")
    miss = [x for x in items if x["verdict"] == "missing"]
    if miss and counts["missing"] < total:
        out.append(f"没找到输出的是 {_ids(miss)}。")
    if r_only := [x for x in miss if "这张表需要 R，还没跑" in x["reasons"]]:
        out.append(f"其中 {_ids(r_only)} 所在的表需要 R。")
    return "".join(out)
