"""Which estimation methods the author code uses, and a teaching chapter for each (card 0193).

The chapters in methods/*.md are written once and reviewed, at the level of lecture notes: what the method is for, its
model, its assumptions, which robustness checks it calls for and why, how to read the output, pitfalls and code. The
replication report and the deck show them without spending tokens.

Detection is by rules over the masked code (comments and strings blanked, as coverage.scan_script does): every pattern
is linear with bounded repeats, lines are truncated, and the hits kept are capped, because author code is outside text
(AGENTS.md, "Scanning outside text").
"""
from __future__ import annotations

import functools
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import package
from .coverage import MAX_CODE_LINE, MAX_CODE_LINES, _call_args

CHAPTERS_DIR = Path(__file__).with_name("methods")
SECTIONS = ("适用的问题", "核心思想", "模型设定", "识别假设", "稳健性检验：做什么，为什么", "怎么读结果", "常见陷阱",
            "代码写法", "延伸阅读")
# report order: designs first, then the estimators they run on, then measurement and inference
ORDER = ("did", "rdd", "synth", "iv", "matching", "fixed_effects", "multilevel", "survival", "ols", "binary", "ordered",
         "poisson", "negbin", "ml", "topic", "dictionary", "bootstrap")

MAX_HITS = 400                 # model calls kept over one script
SCRIPT_CHARS = 600_000         # read of one script (real ones are tens of KB): masking is a Python loop over characters, ~0.3 s per MB
TOTAL_CHARS = 1_500_000        # read over all scripts of a package
MAX_TOTAL_HITS = 2_000         # model calls kept over all scripts
SNIPPETS_PER_METHOD = 3
SNIPPET_LINES = 6
SNIPPET_CHARS = 600
ARGS_WINDOW = 1_200


@dataclass(frozen=True)
class Chapter:
    key: str
    name: str                  # 双重差分
    english: str
    summary: str               # one sentence
    sections: dict[str, str]   # heading → its Markdown, in SECTIONS order
    markdown: str              # the whole chapter without its front matter


def _parse(text: str) -> Chapter:
    if not text.startswith("---\n"):
        raise ValueError("chapter without front matter")
    head, body = text[4:].split("\n---\n", 1)
    meta = dict(line.split(":", 1) for line in head.split("\n") if ":" in line)
    meta = {k.strip(): v.strip() for k, v in meta.items()}   # plain `key: value` lines; a colon in a value stays in it
    sections: dict[str, str] = {}
    current, buf = None, []
    for line in body.split("\n"):
        if line.startswith("## "):
            if current:
                sections[current] = "\n".join(buf).strip()
            current, buf = line[3:].strip(), []
        elif current:
            buf.append(line)
    if current:
        sections[current] = "\n".join(buf).strip()
    return Chapter(str(meta["key"]), str(meta["name"]), str(meta.get("english") or ""), str(meta["summary"]),
                   sections, body.strip())


@functools.cache
def chapters() -> dict[str, Chapter]:
    out = {}
    for path in sorted(CHAPTERS_DIR.glob("*.md")):
        ch = _parse(path.read_text(encoding="utf-8"))
        out[ch.key] = ch
    return out


def chapter(key: str) -> Chapter | None:
    return chapters().get(key)


@dataclass
class Hit:
    key: str
    script: str
    line: int
    code: str


@dataclass
class Found:
    key: str
    scripts: list[str] = field(default_factory=list)
    count: int = 0
    snippets: list[Hit] = field(default_factory=list)

    @property
    def chapter(self) -> Chapter | None:
        return chapter(self.key)


# R and Python calls, matched on the masked code: one alternation of plain words, `[ \t]*` the only repeat.
_CALL_NAMES = (r"(glm\.nb|glmer\.nb|glmmTMB|glmer|lmer|glm|lm|felm|feols|fepois|feglm|fenegbin|plm|"
                   r"coxph|survreg|ivreg|iv_robust|polr|clm|multinom|mlogit|keyATM|stm|LDA|textmodel_lda|train|gbm|"
                   r"xgboost|randomForest|cv\.glmnet|glmnet|boot|dfm_lookup|tokens_lookup|dictionary|att_gt|"
                   r"did_multiplegt|sunab|rdrobust|rdplot|rddensity|synth|dataprep|synthdid_estimate|gsynth|matchit|"
                   r"Match|ebalance|weightit|lm_robust|ols|poisson|logit|probit|negativebinomial|mixedlm|OrderedModel|"
                   r"MNLogit|CoxPHFitter|IV2SLS|OLS|Logit|Probit|Poisson|GLM|PanelOLS|LogisticRegression)[ \t]*\(")
_CALL = {"r": re.compile(r"(?<![\w.$])" + _CALL_NAMES),    # a dot is part of an R name: my.lm( is not lm(
         "python": re.compile(r"(?<![\w$])" + _CALL_NAMES)}  # smf.ols( is a call of ols
_FE_FORMULA = re.compile(r"[+~][ \t]{0,8}(?:as\.)?factor[ \t]{0,8}\(|[+~][ \t]{0,8}C\(")
_DID_TERM = re.compile(r"\b(?:treat|treated|treatment)\w{0,20}[ \t]{0,4}(?:[:*]|##)[ \t]{0,4}(?:[ci]\.)?"
                       r"(?:post|after)\w{0,20}\b|\b(?:post|after)\w{0,20}[ \t]{0,4}(?:[:*]|##)[ \t]{0,4}(?:[ci]\.)?"
                       r"(?:treat|treated|treatment)\w{0,20}\b", re.I)
_STATA_CMD = re.compile(r"^[ \t]{0,40}((?:[A-Za-z_]\w{0,20}[ \t]{0,4}:[ \t]{0,4}){0,3})([A-Za-z_]\w{0,20})\b")
_STATA_PREFIX = {"quietly", "qui", "capture", "cap", "noisily", "xi", "eststo", "est", "estimates", "by", "bysort",
                 "svy", "nestreg", "stepwise"}
_CALL_KEYS = {"glm.nb": "negbin", "glmer.nb": "negbin", "fenegbin": "negbin", "negativebinomial": "negbin",
              "lmer": "multilevel", "glmer": "multilevel", "glmmTMB": "multilevel", "mixedlm": "multilevel",
              "lm": "ols", "ols": "ols", "felm": "ols", "feols": "ols", "plm": "ols", "fepois": "poisson",
              "poisson": "poisson", "logit": "binary", "probit": "binary", "polr": "ordered", "clm": "ordered",
              "OrderedModel": "ordered", "multinom": "ordered", "mlogit": "ordered", "MNLogit": "ordered",
              "coxph": "survival", "survreg": "survival", "CoxPHFitter": "survival", "ivreg": "iv",
              "iv_robust": "iv", "IV2SLS": "iv", "keyATM": "topic", "stm": "topic", "LDA": "topic",
              "textmodel_lda": "topic", "train": "ml", "gbm": "ml", "xgboost": "ml", "randomForest": "ml",
              "cv.glmnet": "ml", "glmnet": "ml", "boot": "bootstrap", "dfm_lookup": "dictionary",
              "tokens_lookup": "dictionary", "dictionary": "dictionary", "att_gt": "did", "did_multiplegt": "did",
              "sunab": "did", "rdrobust": "rdd", "rdplot": "rdd", "rddensity": "rdd", "synth": "synth",
              "dataprep": "synth", "synthdid_estimate": "synth", "gsynth": "synth", "matchit": "matching",
              "Match": "matching", "ebalance": "matching", "weightit": "matching", "lm_robust": "ols",
              "OLS": "ols", "PanelOLS": "ols", "Logit": "binary", "Probit": "binary", "LogisticRegression": "binary",
              "Poisson": "poisson"}
_PY_ONLY = {"ols", "poisson", "logit", "probit", "negativebinomial", "mixedlm", "OrderedModel", "MNLogit",
            "CoxPHFitter", "IV2SLS", "OLS", "Logit", "Probit", "Poisson", "GLM", "PanelOLS", "LogisticRegression"}
_R_ONLY = {"lm", "glm", "lmer", "glmer", "glm.nb", "glmer.nb", "felm", "plm", "lm_robust"}
# Stata prefixes before the command: quietly / noisily / capture / xi, `by …:` and `bysort …:`, and `eststo name:`
_STATA_LEAD = re.compile(r"(?:(?:qui(?:etly)?|noi(?:sily)?|cap(?:ture)?|xi)\b[ \t]{0,4}:?[ \t]{0,4}"
                         r"|(?:by|bys|bysort)\b[^:\n]{1,100}:[ \t]{0,4}"
                         r"|(?:eststo|est(?:imates)?[ \t]+store|svy|nestreg|stepwise)\b[^:\n]{0,40}:[ \t]{0,4}){1,5}")
_STATA_KEYS = {"reg": "ols", "regress": "ols", "areg": "ols", "reghdfe": "ols", "xtreg": "ols", "poisson": "poisson",
               "ppmlhdfe": "poisson", "xtpoisson": "poisson", "nbreg": "negbin", "xtnbreg": "negbin", "logit": "binary",
               "logistic": "binary", "xtlogit": "binary", "probit": "binary", "xtprobit": "binary",
               "ologit": "ordered", "oprobit": "ordered", "mlogit": "ordered", "mprobit": "ordered",
               "mixed": "multilevel", "melogit": "multilevel", "mepoisson": "multilevel", "stcox": "survival",
               "streg": "survival", "ivregress": "iv", "ivreg2": "iv", "xtivreg": "iv", "ivreghdfe": "iv",
               "bootstrap": "bootstrap", "didregress": "did", "xtdidregress": "did", "csdid": "did",
               "did_multiplegt": "did", "eventstudyinteract": "did", "did_imputation": "did", "rdrobust": "rdd",
               "rdplot": "rdd", "rddensity": "rdd", "synth": "synth", "synth_runner": "synth",
               "psmatch2": "matching", "teffects": "matching", "cem": "matching", "ebalance": "matching"}
_FE_STATA = {"areg", "reghdfe", "ppmlhdfe", "ivreghdfe"}
_REGRESSIONS = {"ols", "poisson", "negbin", "binary", "ordered"}


def _glm_family(args: str) -> str:
    a = args.lower()
    if "negative.binomial" in a or "negbin" in a or "negativebinomial" in a:
        return "negbin"
    if "poisson" in a:
        return "poisson"
    if "binomial" in a or "logit" in a or "probit" in a:
        return "binary"
    return "ols"


def _snippet(clean_lines: list[str], line: int) -> str:
    """The statement that starts on `line`: up to SNIPPET_LINES lines, until its brackets close."""
    out, depth = [], 0
    for ln in clean_lines[line - 1:line - 1 + SNIPPET_LINES]:
        out.append(ln.rstrip())
        depth += ln.count("(") - ln.count(")")
        if depth <= 0 and not ln.rstrip().endswith(("+", ",", "///", "%>%", "|>")):
            break
    return "\n".join(out).strip("\n")[:SNIPPET_CHARS]


_CONTINUES = ("+", ",", "(", "~", "=", "///", "%>%", "|>")
STATEMENT_BACK = 12            # lines a formula term is traced back to the start of its statement


def _term_snippet(clean_lines: list[str], line: int) -> str:
    """A formula term found on `line`, shown from the start of its statement: a long statement keeps its first two
    lines and the term's line."""
    start = line
    while start > 1 and line - start < STATEMENT_BACK and clean_lines[start - 2].rstrip().endswith(_CONTINUES):
        start -= 1
    if line - start < SNIPPET_LINES:
        return "\n".join(ln.rstrip() for ln in clean_lines[start - 1:line])[:SNIPPET_CHARS]
    head = [ln.rstrip() for ln in clean_lines[start - 1:start + 1]]
    return "\n".join(head + ["    …", clean_lines[line - 1].rstrip()])[:SNIPPET_CHARS]


def scan_script(path: str, text: str) -> list[Hit]:
    """The model calls in one script, in order, at most MAX_HITS. Two methods are read from formulas rather than calls,
    once per script: fixed effects, when a formula adds `factor(x)` or `C(x)`, a felm/feols formula has a `|` part, or
    a Stata command absorbs a group; and difference-in-differences, when a treatment term is interacted with a
    post-period term."""
    lang = package.lang_of(path)
    if not lang:
        return []
    if package.ext_of(path) == ".rmd":
        text = package._rmd_code(text[:SCRIPT_CHARS * 2])
    lines = [ln[:MAX_CODE_LINE] for ln in text[:SCRIPT_CHARS].split("\n")[:MAX_CODE_LINES]]
    clean, masked = package._mask("\n".join(lines), lang)
    clean_lines, masked_lines = clean.split("\n"), masked.split("\n")
    hits: list[Hit] = []
    fe_line = 0

    def add(key: str, n: int) -> bool:
        hits.append(Hit(key, path, n, _snippet(clean_lines, n)))
        return len(hits) >= MAX_HITS

    if lang == "stata":
        for n, ln in enumerate(masked_lines, 1):
            rest = ln.lstrip(" \t")
            lead = _STATA_LEAD.match(rest)
            m = _STATA_CMD.match(rest[lead.end():] if lead else rest)
            if not m:
                continue
            prefixes = {p.strip().split()[0] for p in m.group(1).split(":") if p.strip()}
            cmd = m.group(2)
            if "bootstrap" in prefixes and add("bootstrap", n):
                break
            if cmd in _STATA_PREFIX:
                continue
            key = _STATA_KEYS.get(cmd)
            if not key:
                continue
            options = clean_lines[n - 1].partition(",")[2][:300]          # one bounded window, not a scan per comma
            if not fe_line and (cmd in _FE_STATA or (cmd.startswith("xt") and re.search(r"\bfe\b", options))):
                fe_line = n
            if add(key, n):
                break
    else:
        starts = [0]
        for ln in clean_lines:
            starts.append(starts[-1] + len(ln) + 1)
        line_no = 1
        for m in _CALL["python" if lang == "python" else "r"].finditer(masked):
            while line_no < len(starts) - 1 and starts[line_no] <= m.start():
                line_no += 1
            name = m.group(1)
            if (lang == "r" and name in _PY_ONLY) or (lang == "python" and name in _R_ONLY):
                continue                                   # not a call of this language; a user function shares the name
            if lang == "python" and name in ("boot", "train", "Match", "dictionary"):
                continue                                   # common Python method and class names
            key = (_glm_family(_call_args(clean, m.end(), ARGS_WINDOW)) if name in ("glm", "feglm", "GLM")
                   else _CALL_KEYS.get(name))
            if not key:
                continue
            if name in ("felm", "feols", "fepois", "feglm", "fenegbin") and not fe_line \
                    and "|" in _call_args(clean, m.end(), ARGS_WINDOW):
                fe_line = line_no
            if add(key, line_no):
                break
            if name == "glmer.nb" and add("multilevel", line_no):   # a multilevel model with a negative binomial outcome
                break
        if not fe_line:
            for n, ln in enumerate(clean_lines, 1):
                if _FE_FORMULA.search(ln):
                    fe_line = n
                    break
    if fe_line and any(h.key in _REGRESSIONS for h in hits):
        hits.append(Hit("fixed_effects", path, fe_line, _term_snippet(clean_lines, fe_line)))
    if not any(h.key == "did" for h in hits):
        for n, ln in enumerate(clean_lines, 1):
            if _DID_TERM.search(ln):
                hits.append(Hit("did", path, n, _term_snippet(clean_lines, n)))
                break
    return hits


def detect(scripts: dict[str, str]) -> list[Found]:
    """Methods over all scripts in ORDER, each with the scripts using it and up to SNIPPETS_PER_METHOD calls."""
    found: dict[str, Found] = {}
    budget, hits_left = TOTAL_CHARS, MAX_TOTAL_HITS
    for path in sorted(scripts):
        if budget <= 0:
            break
        text = scripts[path][:min(SCRIPT_CHARS, budget)]
        budget -= len(text)
        hits = scan_script(path, text)[:hits_left]
        hits_left -= len(hits)
        for h in hits:
            f = found.setdefault(h.key, Found(h.key))
            f.count += 1
            if path not in f.scripts:                      # the first call in each script, so every script is shown
                f.scripts.append(path)
                if len(f.snippets) < SNIPPETS_PER_METHOD:
                    f.snippets.append(h)
    return [found[k] for k in ORDER if k in found]
