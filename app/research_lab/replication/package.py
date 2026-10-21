"""Reading an author's replication package without running it (card 0183, docs/design/REPLICATION-v3.md §2A).

Given the text of the author's R, Stata and Python scripts and the names of the files the repository holds, this finds
which data file each script reads and which models it fits, and picks the *main scripts* whose data is worth fetching.
Pure and rule-based: nothing is executed, nothing is downloaded, no AI.

What it understands
  data reads   R read.csv/read_csv/fread/readRDS/read_dta/haven::read_*/readxl/load(...) · Stata use, import delimited,
               import excel, insheet, merge/append using · Python pd.read_*
  paths        string literals, file.path / here / paste0 / os.path.join of literals, "./data/x.csv" against the script's
               folder and the repository root, an absolute path of the author's laptop by its longest tail that is unique
               in the repository; a path built from variables is reported as unresolved, never guessed
  models       R lm, glm, glm.nb, felm, feols, lm_robust, plm, lmer, coxph … · Stata reg, xtreg, logit, … · Python smf.ols,
               sm.OLS, PanelOLS, feols …
  helpers      source("x.R"), do "x.do", import of a local module: what a helper reads and fits counts for the scripts
               that load it, and the helper itself is never a main script

Main scripts: the scripts no other script loads that read a data file of the repository and fit a model, minus (a) the
ones that look like robustness or bootstrap variants (App, Iter, Robust … in the name, or "robustness check",
"appendix", "bootstrapped" in the README's line for that script) and (b) the ones that need a file the default pick
cannot supply (too large, needs R). Each step is skipped when it would leave no script at all, so a package whose
scripts are all called "Appendix…" still gets an answer. `main=` names the scripts by hand and replaces the rule.
"""
from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from typing import Iterable

CODE_EXTS = (".r", ".rmd", ".do", ".py", ".sql", ".sh")
DOC_EXTS = (".md", ".txt", ".pdf", ".rst", "")
DOC_STEM = re.compile(r"readme|code[\s_-]?book|replication", re.I)
LANGS = {".r": "r", ".rmd": "r", ".do": "stata", ".py": "python"}      # the languages that are scanned
SECONDARY_TOKENS = {"app", "appendix", "robust", "robustness", "supp", "supplement", "supplementary", "sensitivity",
                    "placebo", "alt", "alternative", "bootstrap", "bootstrapped", "iter", "iteration", "old", "backup",
                    "draft", "tmp", "temp", "test", "check", "checks", "archive", "scratch", "otherdata", "external",
                    "vendor", "legacy"}
SECONDARY_DOC = re.compile(r"robust|appendix|supplement|sensitivity|placebo|bootstrap|alternative|replications? of|extension",
                           re.I)
MAX_ARG = 600                                                           # characters of one call's first argument
MAX_PATH = 500                                                          # a longer "path" is not one


def ext_of(name: str) -> str:
    return posixpath.splitext(name.lower())[1]


def is_code(name: str) -> bool:
    return ext_of(name) in CODE_EXTS


def is_doc(name: str) -> bool:
    return ext_of(name) in DOC_EXTS and bool(DOC_STEM.search(posixpath.splitext(posixpath.basename(name))[0]))


def is_side(path: str) -> bool:
    """True for a file under a folder that holds somebody else's material or old versions (otherdata/, archive/ …)."""
    return bool(_tokens(posixpath.dirname(path)) & SECONDARY_TOKENS)


def lang_of(name: str) -> str | None:
    return LANGS.get(ext_of(name))


def format_models(models: dict[str, int]) -> str:
    return ", ".join(f"{k} ×{v}" if v > 1 else k for k, v in models.items())


# ---------------------------------------------------------------- source text

def _blank(chunk: str) -> str:
    return "".join("\n" if ch == "\n" else " " for ch in chunk)


def _mask(text: str, lang: str) -> tuple[str, str]:
    """(clean, masked): the code with comments blanked, and the same with every string's content blanked too, so a
    keyword inside a string never matches. Both keep the length and the line breaks of the original, so a match in
    `masked` can be read back from `clean` at the same offset. A string ends at the end of its line."""
    clean, masked = [], []
    i, n, quote = 0, len(text), ""
    bol = True                                   # only blanks since the last line break
    while i < n:
        c = text[i]
        if quote:
            if c == "\\" and lang != "stata" and i + 1 < n and text[i + 1] != "\n":
                clean.append(text[i:i + 2])
                masked.append("  ")
                i += 2
                continue
            if c == "\n" or c == quote:
                quote = ""
            clean.append(c)
            masked.append(c if c in ("\n", '"', "'") else " ")
            bol = c == "\n"
            i += 1
            continue
        end = -1                                 # a comment from i to `end`, blanked
        if lang == "stata":
            if text.startswith("/*", i):
                stop = text.find("*/", i + 2)
                end = n if stop < 0 else stop + 2
            elif text.startswith("//", i) or (c == "*" and bol):
                stop = text.find("\n", i)
                end = n if stop < 0 else stop
        elif c == "#":
            stop = text.find("\n", i)
            end = n if stop < 0 else stop
        if end >= 0:
            clean.append(_blank(text[i:end]))
            masked.append(clean[-1])
            i = end
            continue
        if c == '"' or (c == "'" and lang != "stata"):
            quote = c
        bol = c == "\n" or (bol and c in " \t")
        clean.append(c)
        masked.append(c)
        i += 1
    return "".join(clean), "".join(masked)


def _rmd_code(text: str) -> str:
    """The R chunks of an R Markdown file, in place (the prose becomes blank lines)."""
    out, inside = [], False
    for line in text.splitlines():
        if re.match(r"^\s*```+\s*\{\s*r[\s,}]", line):
            inside = True
            out.append("")
        elif inside and re.match(r"^\s*```+\s*$", line):
            inside = False
            out.append("")
        else:
            out.append(line if inside else "")
    return "\n".join(out)


def _first_arg(clean: str, pos: int) -> str:
    """The first argument of the call whose "(" ends just before `pos`: up to a top-level comma or the closing bracket."""
    depth, i, quote = 0, pos, ""
    while i < len(clean) and i - pos < MAX_ARG:
        c = clean[i]
        if quote:
            if c == "\\":
                i += 1
            elif c == quote:
                quote = ""
        elif c in "\"'":
            quote = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif c == "," and depth == 0:
            break
        i += 1
    return clean[pos:i].strip()


STR = re.compile(r'"((?:[^"\\\n]|\\.)*)"|\'((?:[^\'\\\n]|\\.)*)\'')


def _paths_from(expr: str) -> list[str]:
    """The file names an argument expression can mean, best first: the literals joined the way file.path / here /
    paste0 join them, then the last literal alone (the file name when the folder is a variable)."""
    expr = re.sub(r"^\s*(?:file|input|path|con|filename|x|file_path)\s*=\s*", "", expr.strip())
    named = dict(re.findall(r"\b(\w+)\s*=\s*[\"']([^\"']*)[\"']", expr))      # sep = "/" is not a part of the path
    expr = re.sub(r"\b\w+\s*=\s*[\"'][^\"']*[\"']", "", expr)
    lits = [a or b for a, b in STR.findall(expr)]
    if not lits:
        return []
    if len(lits) == 1:
        return [lits[0]]
    sep = ""
    if re.search(r"\b(?:file\.path|here|path|join|Path|PurePath)\s*\(", expr) or re.search(r"/\s*[\"']", expr):
        sep = "/"
    elif re.search(r"\bpaste\s*\(", expr):
        sep = named.get("sep", " ")
    out = [sep.join(lits), lits[-1]]
    return [p for i, p in enumerate(out) if p not in out[:i]]


# ---------------------------------------------------------------- paths against the repository listing

def _norm(raw: str) -> str:
    s = raw.strip().replace("\\\\", "/").replace("\\", "/")
    s = re.sub(r"^file://", "", s)
    s = re.sub(r"\$\{?\w+\}?|`\w+'", "", s)            # Stata globals and locals: "$data/x.dta"
    s = re.sub(r"^[A-Za-z]:", "", s)
    return s


def _near(hits: list[str], base: str) -> str | None:
    """The hit that shares the most leading folders with the script; None when two tie."""
    def shared(p: str) -> int:
        n = 0
        for a, b in zip(posixpath.dirname(p).split("/"), base.split("/")):
            if a.lower() != b.lower():
                break
            n += 1
        return n
    scored = sorted(((shared(h), h) for h in hits), reverse=True)
    return scored[0][1] if len(scored) == 1 or scored[0][0] > scored[1][0] else None


class Index:
    """The repository's paths, indexed once: exact, lower-case, and by every tail of whole path components."""

    def __init__(self, names: Iterable[str]):
        self.names = list(names)
        self.exact = set(self.names)
        self.lower = {n.lower(): n for n in self.names}
        self.tails: dict[str, list[str]] = {}
        for n in self.names:
            parts = n.lower().split("/")
            for k in range(1, len(parts) + 1):
                self.tails.setdefault("/".join(parts[-k:]), []).append(n)


def resolve(raw: str, script: str, names: Iterable[str] | Index) -> str | None:
    """The repository file a script's path means, or None. `names` are repository paths ("data/panel.csv")."""
    idx = names if isinstance(names, Index) else Index(names)
    s = _norm(raw)
    if not s or len(s) > MAX_PATH or s.endswith("/") or "{" in s or "%" in s:
        return None
    base = posixpath.dirname(script)
    tries = []
    if not s.startswith("/"):
        tries = [posixpath.normpath(posixpath.join(base, s)), posixpath.normpath(s)]
    for t in tries:
        t = t.removeprefix("./")
        if t in idx.exact:
            return t
        if t.lower() in idx.lower:
            return idx.lower[t.lower()]
    parts = [p for p in s.split("/") if p not in ("", ".", "..")]
    for k in range(len(parts), 0, -1):
        hits = idx.tails.get("/".join(parts[-k:]).lower(), [])
        if len(hits) == 1:
            return hits[0]
        if hits:
            return _near(hits, base)
    return None


# ---------------------------------------------------------------- what a script reads, loads and fits

def _alt(words: Iterable[str]) -> str:
    return "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))


R_READS = ("read.csv", "read.csv2", "read.table", "read.delim", "read.delim2", "read_csv", "read_csv2", "read_tsv",
           "read_delim", "read_table", "read_table2", "read_fwf", "fread", "vroom", "readRDS", "read_rds", "read_dta",
           "read_stata", "read.dta", "read.dta13", "read_sav", "read_spss", "read.spss", "read_sas", "read_xpt",
           "read_excel", "read_xlsx", "read_xls", "read_feather", "read_parquet", "read_fst", "load")
R_MODELS = ("lm", "glm", "glm.nb", "felm", "feols", "fepois", "feglm", "fenegbin", "lm_robust", "iv_robust", "plm",
            "lmer", "glmer", "glmmTMB", "coxph", "survreg", "polr", "multinom", "ivreg", "rq", "nls", "gam", "bam",
            "svyglm", "zeroinfl", "hurdle", "tobit", "stan_glm", "stan_lmer", "brm", "rdrobust")
PY_READS = ("read_csv", "read_table", "read_excel", "read_stata", "read_parquet", "read_pickle", "read_json",
            "read_feather", "read_sas", "read_spss", "read_hdf", "read_fwf", "read_dta", "read_sav", "loadtxt",
            "genfromtxt")
PY_MODELS = ("ols", "logit", "probit", "glm", "poisson", "mnlogit", "mixedlm", "OLS", "WLS", "GLS", "Logit", "Probit",
             "Poisson", "GLM", "MNLogit", "NegativeBinomial", "PanelOLS", "RandomEffects", "PooledOLS", "FamaMacBeth",
             "IV2SLS", "IVGMM", "feols", "fepois", "feglm", "LinearRegression", "LogisticRegression")
STATA_MODELS = ("reg", "regress", "areg", "reghdfe", "xtreg", "xtivreg", "xtlogit", "xtprobit", "xtpoisson", "xtnbreg",
                "xtmixed", "mixed", "logit", "logistic", "probit", "oprobit", "ologit", "mlogit", "poisson", "nbreg",
                "zinb", "zip", "tobit", "ivregress", "ivreg2", "ivreghdfe", "stcox", "streg", "heckman", "rdrobust",
                "csdid", "did_multiplegt", "synth", "qreg", "sureg", "glm", "cloglog")

_R_CALL = re.compile(rf"(?<![\w.$@])(?:[A-Za-z_][\w.]*::)?({_alt(R_READS)})\s*\(")
_R_MODEL = re.compile(rf"(?<![\w.$@])(?:[A-Za-z_][\w.]*::)?({_alt(R_MODELS)})\s*\(")
_R_SOURCE = re.compile(r"(?<![\w.$@])(?:sys\.source|source)\s*\(")
_PY_CALL = re.compile(rf"(?<![\w$@])({_alt(PY_READS)})\s*\(")        # no dotted prefix: it matched after every "."
_PY_MODEL = re.compile(rf"(?<![\w$@.])((?:[A-Za-z_]\w*\.){{0,4}}(?:{_alt(PY_MODELS)}))\s*\(")
_PY_IMPORT = re.compile(r"^[ \t]*(?:from[ \t]+([\w.]+)[ \t]+import|import[ \t]+([\w.]+))", re.M)
_ST_LEAD = r"^[ \t]*(?:(?:qui(?:etly)?|noi(?:sily)?|cap(?:ture)?|xi)[ \t]+)*"
_ST_USE = re.compile(rf"{_ST_LEAD}(?:use|u)[ \t]+(?!using\b)", re.M)
_ST_USING = re.compile(rf"{_ST_LEAD}(?:use|u|merge|append|insheet|infile|infix|joinby)\b[^\n]*?\busing[ \t]+", re.M)
_ST_IMPORT = re.compile(rf"{_ST_LEAD}import[ \t]+(?:delimited|excel|sas|spss|sasxport\d*|dbase)\b[ \t]*(?:using[ \t]+)?",
                        re.M)
_ST_DO = re.compile(rf"{_ST_LEAD}(?:do|run|include)[ \t]+", re.M)
_ST_MODEL = re.compile(rf"(?:{_ST_LEAD}|:[ \t]*)({_alt(STATA_MODELS)})(?=[ \t,]|$)", re.M)


def _stata_path(clean: str, pos: int) -> str:
    m = re.match(r"\s*`?\"([^\"\n]*)\"'?|\s*([^\s,\"]+)", clean[pos:pos + 400])
    return (m.group(1) or m.group(2) or "") if m else ""


@dataclass
class Script:
    path: str
    lang: str
    reads: list[str] = field(default_factory=list)          # repository files read directly
    unresolved: list[str] = field(default_factory=list)     # paths it reads that match no repository file
    sources: list[str] = field(default_factory=list)        # scripts it loads
    models: dict[str, int] = field(default_factory=dict)    # model call → how many times


def _add(lst: list[str], item: str | None, seen: set[str]) -> None:
    """Append `item` once, in first-seen order. `seen` is what `lst` already holds: a script that names tens of thousands
    of files costs that many lookups, not their square (the panel reads author text on every page load, card 0188)."""
    if item and item not in seen:
        seen.add(item)
        lst.append(item)


def scan(path: str, text: str, files: Iterable[str] | Index, code_files: Iterable[str] | Index) -> Script:
    """One script: the repository files it reads, the scripts it loads, the model calls it makes."""
    files = files if isinstance(files, Index) else Index(files)
    code_files = code_files if isinstance(code_files, Index) else Index(code_files)
    lang = lang_of(path) or ""
    s = Script(path, lang)
    seen_reads: set[str] = set()
    seen_unresolved: set[str] = set()
    seen_sources: set[str] = set()
    if lang == "r" and ext_of(path) == ".rmd":
        text = _rmd_code(text)
    clean, masked = _mask(text, lang)

    def read(expr_paths: list[str]) -> None:
        hit = next((r for r in (resolve(p, path, files) for p in expr_paths) if r), None)
        if hit:
            _add(s.reads, hit, seen_reads)
        elif expr_paths:
            _add(s.unresolved, expr_paths[0], seen_unresolved)

    def load(expr_paths: list[str], ext: str = "") -> None:
        for p in expr_paths:
            p = p + ext if ext and not posixpath.splitext(p)[1] else p
            hit = resolve(p, path, code_files)
            if hit and hit != path:
                _add(s.sources, hit, seen_sources)
                return

    if lang == "r":
        for m in _R_CALL.finditer(masked):
            paths = _paths_from(_first_arg(clean, m.end()))
            if m.group(1) == "load" and not any(re.search(r"\.(?:rdata|rda|rds)$", p, re.I) for p in paths):
                continue
            read(paths)
        for m in _R_SOURCE.finditer(masked):
            load(_paths_from(_first_arg(clean, m.end())))
        models = [m.group(1) for m in _R_MODEL.finditer(masked)]
    elif lang == "python":
        for m in _PY_CALL.finditer(masked):
            read(_paths_from(_first_arg(clean, m.end())))
        for m in _PY_IMPORT.finditer(masked):
            mod = (m.group(1) or m.group(2) or "").replace(".", "/")
            if mod:
                load([mod + ".py"])
        models = [m.group(1) for m in _PY_MODEL.finditer(masked)]
    elif lang == "stata":
        found = [(m.start(), "read", _stata_path(clean, m.end())) for m in _ST_USING.finditer(masked)]
        for m in _ST_USE.finditer(masked):
            eol = masked.find("\n", m.end())
            if not re.search(r"\busing\b", masked[m.end():len(masked) if eol < 0 else eol]):
                found.append((m.start(), "read", _stata_path(clean, m.end())))
        found += [(m.start(), "read", _stata_path(clean, m.end())) for m in _ST_IMPORT.finditer(masked)]
        found += [(m.start(), "load", _stata_path(clean, m.end())) for m in _ST_DO.finditer(masked)]
        for _, what, raw in sorted(found):
            read([raw]) if what == "read" else load([raw], ".do")
        models = [m.group(1) for m in _ST_MODEL.finditer(masked)]
    else:
        models = []
    for name in models:
        s.models[name] = s.models.get(name, 0) + 1
    return s


# ---------------------------------------------------------------- the package

@dataclass
class Package:
    scripts: dict[str, Script]
    reads: dict[str, list[str]]                  # script → the repository files it reads, helpers' reads included
    models: dict[str, dict[str, int]]            # script → its model calls, helpers' included
    helpers: list[str]                           # scripts another script loads
    main: list[str]                              # the scripts whose data is the default pick
    not_main: dict[str, str] = field(default_factory=dict)   # candidate → why it is not a main script
    default_data: list[str] = field(default_factory=list)    # union of the main scripts' data files
    left_out: dict[str, str] = field(default_factory=dict)   # data a main script reads that the default pick cannot supply

    def data_of(self, script: str) -> list[str]:
        return self.reads.get(script, [])

    def readers_of(self, data_file: str, *, all_scripts: bool = False) -> list[str]:
        """The main scripts (or any script) that read a data file, for the label next to it."""
        return [m for m in (self.reads if all_scripts else self.main) if data_file in self.reads.get(m, [])]


def _closure(start: str, scripts: dict[str, Script]) -> list[Script]:
    seen, order, stack = set(), [], [start]
    while stack:
        p = stack.pop(0)
        if p in seen or p not in scripts:
            continue
        seen.add(p)
        order.append(scripts[p])
        stack.extend(scripts[p].sources)
    return order


def _tokens(path: str) -> set[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", posixpath.splitext(path)[0])
    return {t for t in re.split(r"[^A-Za-z]+", spaced.lower()) if t}


def _doc_hints(scripts: Iterable[str], docs: dict[str, str]) -> dict[str, str]:
    """script → the README's words that make it a robustness / appendix / bootstrap variant. Only the first mention of
    a script in a document counts (its own row of the file table); its line ends at another script's name, a blank line
    or the next list item."""
    names = {posixpath.basename(p): p for p in scripts}
    out: dict[str, str] = {}
    for text in docs.values():
        starts = {b: m for b in names if (m := re.search(rf"(?<![\w.]){re.escape(b)}(?!\w)", text))}
        for b, m in starts.items():
            if names[b] in out:
                continue
            window = text[m.end():m.end() + 260]
            ends = [w.start() for o in names if o != b and (w := re.search(rf"(?<![\w.]){re.escape(o)}(?!\w)", window))]
            ends += [w.start() for w in [re.search(r"\n[ \t]*\n|\n[ \t]*[-*•][ \t]", window)] if w]
            hit = SECONDARY_DOC.search(window[:min(ends + [len(window)])])
            if hit:
                out[names[b]] = f"说明书里写着“{hit.group(0).lower()}”"
    return out


def analyze(code: dict[str, str], files: Iterable[str], *, docs: dict[str, str] | None = None,
            unusable: dict[str, str] | None = None, main: Iterable[str] | None = None) -> Package:
    """`code` maps a script's repository path to its text; `files` are all the repository's paths (data, code,
    documents, everything); `docs` map a document's path to its text; `unusable` maps a repository file the default
    pick cannot supply to the reason (too large, needs R); `main` replaces the rule with the scripts named."""
    files = list(files)
    index, code_index = Index(files), Index(f for f in files if is_code(f))
    scripts = {p: scan(p, t, index, code_index) for p, t in code.items() if lang_of(p)}
    unusable = unusable or {}
    loaded = {c for s in scripts.values() for c in s.sources if c in scripts}
    reads, models = {}, {}
    for p in scripts:
        chain = _closure(p, scripts)
        reads[p] = list(dict.fromkeys(f for s in chain for f in s.reads))
        merged: dict[str, int] = {}
        for s in chain:
            for k, v in s.models.items():
                merged[k] = merged.get(k, 0) + v
        models[p] = merged
    helpers = sorted(loaded)
    pkg = Package(scripts, reads, models, helpers, [])
    if main is not None:
        pkg.main = _named(main, scripts)
    else:
        entry = [p for p in scripts if p not in loaded] or list(scripts)
        cands = [p for p in entry if reads[p] and models[p]]
        why: dict[str, str] = {}
        hints = _doc_hints(cands, docs or {})
        primary = []
        for p in cands:
            if _tokens(p) & SECONDARY_TOKENS:
                why[p] = "名字像附录、稳健性或重复抽样的版本"
            elif p in hints:
                why[p] = hints[p]
            else:
                primary.append(p)
        feasible = []
        for p in primary or cands:
            gone = [f for f in reads[p] if f in unusable]
            if gone:
                why[p] = f"要用的 {posixpath.basename(gone[0])} 默认不取：{unusable[gone[0]]}"
            else:
                feasible.append(p)
        pkg.main = feasible or primary or cands
        pkg.not_main = {p: r for p, r in why.items() if p not in pkg.main}
    for m in pkg.main:
        for f in reads.get(m, []):
            if f in unusable:
                pkg.left_out.setdefault(f, unusable[f])
            elif f not in pkg.default_data:
                pkg.default_data.append(f)
    return pkg


def _named(main: Iterable[str], scripts: dict[str, Script]) -> list[str]:
    """The scripts a person named, by path or by file name; a name that matches none or several is an error."""
    out = []
    for name in main:
        hits = [p for p in scripts if p == name or posixpath.basename(p) == name]
        if len(hits) != 1:
            raise ValueError(f"没有这个脚本，或者名字不唯一：{name}")
        if hits[0] not in out:
            out.append(hits[0])
    return out
