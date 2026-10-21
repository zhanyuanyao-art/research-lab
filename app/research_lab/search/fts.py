"""FTS5 query building and BM25 search over excerpts and artifacts.

The query keeps the 12 highest tf·idf terms of the source text (document frequencies from
`terms_vocab`, which holds porter-stemmed terms of `excerpts_fts`), adds concept labels as quoted
phrases, and OR-joins everything. Every term is quoted so user text can never inject FTS syntax.
"""
from __future__ import annotations

import math
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field

MAX_TERMS = 12
# Column weights from architecture §4: title 2.0, quote 1.0, body 0.8, note 0.6.
EXCERPT_WEIGHTS = (1.0, 0.6)     # quote, my_note
ARTIFACT_WEIGHTS = (2.0, 0.8)    # title, body

TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9'+]*")
STOPWORDS = frozenset("""
a about above after again against all also although am among an and any are aren't as at be because been
before being below between both but by can cannot could did do does doing down during each either else
even ever every few for from further had has have having he her here hers herself him himself his how however
i if in into is isn't it it's its itself just least less let like made make many may me might more most much
must my myself neither no nor not now of off often on once one only or other others otherwise ought our ours
ourselves out over own per perhaps rather same see seem seems several shall she should since so some such
than that that's the their theirs them themselves then there therefore these they this those though through
thus to too toward towards under until up upon us use used using very via was we well were what when where
whether which while who whom whose why will with within without would yet you your yours yourself
p pp ibid et al cf eg ie vs etc
""".split())


@dataclass
class Query:
    match: str                                   # the FTS5 MATCH expression ('' = nothing to search)
    terms: list[tuple[str, float]] = field(default_factory=list)   # (term, tf·idf)
    phrases: list[str] = field(default_factory=list)


def tokens(text: str) -> list[str]:
    out = []
    for t in TOKEN_RE.findall((text or "").lower()):
        t = t.strip("'")
        if len(t) < 3 and not t.isdigit():
            continue
        if t in STOPWORDS or (t.isdigit() and len(t) < 4):
            continue
        out.append(t)
    return out


def _stem_variants(t: str) -> list[str]:
    """Cheap suffix variants so unstemmed source tokens find their porter-stemmed vocab entry."""
    v = [t]
    for suf, rep in (("ies", "i"), ("ied", "i"), ("sses", "ss"), ("ing", ""), ("edly", ""), ("ed", ""), ("es", ""),
                     ("s", ""), ("ly", ""), ("ment", ""), ("ness", ""), ("ation", "at"), ("ational", "at"),
                     ("ity", ""), ("ive", ""), ("al", ""), ("er", ""), ("e", "")):
        if t.endswith(suf) and len(t) - len(suf) >= 3:
            v.append(t[: len(t) - len(suf)] + rep)
    return v


def doc_frequencies(conn: sqlite3.Connection, terms: list[str]) -> dict[str, int]:
    variants = {t: _stem_variants(t) for t in terms}
    flat = sorted({x for vs in variants.values() for x in vs})
    found: dict[str, int] = {}
    for i in range(0, len(flat), 400):
        chunk = flat[i:i + 400]
        q = "SELECT term, doc FROM terms_vocab WHERE term IN (%s)" % ",".join("?" * len(chunk))
        for row in conn.execute(q, chunk):
            found[row[0]] = row[1]
    return {t: max((found.get(x, 0) for x in vs), default=0) for t, vs in variants.items()}


def quote_term(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def concept_labels_in(conn: sqlite3.Connection, text: str) -> list[str]:
    low = (text or "").lower()
    out = []
    for r in conn.execute("SELECT label FROM concepts"):
        label = (r[0] or "").strip()
        if label and label.lower() in low:
            out.append(label)
    return out


def build_query(conn: sqlite3.Connection, text: str, concept_labels: list[str] | None = None,
                max_terms: int = MAX_TERMS) -> Query:
    toks = tokens(text)
    if not toks and not concept_labels:
        return Query("")
    tf = Counter(toks)
    n_docs = conn.execute("SELECT COUNT(*) FROM excerpts").fetchone()[0]
    df = doc_frequencies(conn, list(tf))
    scored = []
    for term, count in tf.items():
        idf = math.log((n_docs + 1) / (df.get(term, 0) + 1)) + 1.0
        scored.append((term, round(count * idf, 4)))
    scored.sort(key=lambda x: (-x[1], x[0]))
    top = scored[:max_terms]
    phrases = []
    seen = set()
    for label in concept_labels or []:
        key = label.strip().lower()
        if key and key not in seen:
            seen.add(key)
            phrases.append(label.strip())
    parts = [quote_term(p) for p in phrases] + [quote_term(t) for t, _ in top]
    return Query(" OR ".join(parts), top, phrases)


def search_excerpts(conn: sqlite3.Connection, query: Query | str, limit: int = 50) -> list[tuple[str, float]]:
    """(excerpt id, score) with score = -bm25 (higher is better)."""
    match = query.match if isinstance(query, Query) else query
    if not match:
        return []
    sql = ("SELECT e.id, -bm25(excerpts_fts, ?, ?) AS s FROM excerpts_fts JOIN excerpts e ON e.rowid = excerpts_fts.rowid "
           "WHERE excerpts_fts MATCH ? ORDER BY s DESC LIMIT ?")
    try:
        return [(r[0], float(r[1])) for r in conn.execute(sql, (*EXCERPT_WEIGHTS, match, limit))]
    except sqlite3.OperationalError:
        return []


def search_artifacts(conn: sqlite3.Connection, query: Query | str, limit: int = 50) -> list[tuple[str, float]]:
    """(artifact ref, score) with score = -bm25 (higher is better)."""
    match = query.match if isinstance(query, Query) else query
    if not match:
        return []
    sql = ("SELECT a.ref, -bm25(artifacts_fts, ?, ?) AS s FROM artifacts_fts JOIN artifacts a ON a.rowid = artifacts_fts.rowid "
           "WHERE artifacts_fts MATCH ? ORDER BY s DESC LIMIT ?")
    try:
        return [(r[0], float(r[1])) for r in conn.execute(sql, (*ARTIFACT_WEIGHTS, match, limit))]
    except sqlite3.OperationalError:
        return []
