"""Idempotent seed of the method records from docs/design/method-layer.md (§2.6, §3.3, §5, §6).

Everything seeded is scaffolding, not findings: every evidence cell starts `missing`, every
implication is a placeholder (`implication_origin = 'placeholder'`, so it counts toward no
milestone until the user rewrites it), and working definitions stay empty.
"""
from __future__ import annotations

import json
import sqlite3

from ..db.connection import now_iso

ASEAN = "asean-centrality"
USROK = "us-rok-reassurance"

# Vocab B minus `other`: a catch-all camp would fail "every camp has ≥3 papers" forever.
CAMPS = [
    ("institutional-constructivist", "Institutional-constructivist",
     "ASEAN norms (the 'ASEAN way') build a nascent security community that others come to accept; "
     "external norms are localized through ASEAN rather than adopted wholesale."),
    ("small-state-strategy", "Small-state strategy",
     "Secondary states hedge, and cooperative security and the balance of power operate together inside "
     "ASEAN institutions."),
    ("great-power-hierarchy", "Great-power acceptance / hierarchy",
     "ASEAN states enmesh great powers inside a layered hierarchy; ASEAN's role depends on the great-power bargain."),
    ("sceptic", "Sceptic",
     "ASEAN produces process, not outcomes; centrality is largely rhetorical and its institutional weakness is structural."),
    ("relational-network", "Relational / network",
     "Centrality rests on ASEAN's position in overlapping regional networks."),
]

HYPOTHESES = [
    ("H1", "Institutional inertia: centrality persists because the forums, rules and sunk costs exist.", 0),
    ("H2", "Small-state coalition: ASEAN members actively defend centrality as a collective strategy.", 0),
    ("H3", "Great-power acceptance: centrality persists while great powers find ASEAN venues useful and non-threatening.", 0),
    ("H4", "Emergent relational property: centrality persists because ASEAN forums are the brokerage positions "
           "linking actors with few direct multilateral ties, whatever anyone intends.", 1),
]

VERIFY = "Date from the design notes; verify against the primary record before relying on it."
OBSERVATIONS = [
    ("O1-1994-arf", "O1 ARF founded 1994", "1994-07-25",
     "Origin case: the first ASEAN-led forum that brings in all great powers. " + VERIFY,
     "ASEAN PMC / AMM 1993–94 statements; Leifer, The ASEAN Regional Forum (Adelphi Paper 302, 1996)"),
    ("O2-2005-eas-criteria", "O2 EAS 2005 entry criteria (TAC, dialogue partner, substantive relations)", "2005-12-14",
     "Membership gatekeeping: who wrote the entry rule. " + VERIFY,
     "Kuala Lumpur Declaration 2005; foreign-ministry records of the criteria negotiation"),
    ("O3-2011-us-russia-eas", "O3 US and Russia join the EAS 2011", "2011-11-19",
     "Great powers joining an ASEAN-chaired leader-level venue. " + VERIFY,
     "US State Department briefings 2010–11; EAS chairman's statements"),
    ("O4-2012-phnom-penh-amm", "O4 Phnom Penh AMM 2012: no joint communiqué", "2012-07-13",
     "Failure case: the institution did not carry itself. " + VERIFY,
     "AMM 2012 chair's remarks; later AMM communiqués"),
    ("O5-2019-aoip", "O5 ASEAN Outlook on the Indo-Pacific 2019", "2019-06-23",
     "Members author a centrality framing under Indo-Pacific pressure. " + VERIFY,
     "AOIP text; national statements at adoption"),
    ("O6-2021-quad", "O6 Quad statements invoke 'ASEAN centrality' (2021–)", "2021-03-12",
     "Bypass minilateral that still names ASEAN centrality. " + VERIFY,
     "Quad leaders' joint statements; count bypass-forum meetings"),
]

FORUMS = [
    ("ARF", "ASEAN Regional Forum", "1994"), ("APT", "ASEAN Plus Three", "1997"),
    ("EAS", "East Asia Summit", "2005"), ("ADMM+", "ADMM-Plus", "2010"),
    ("AOIP", "ASEAN Outlook on the Indo-Pacific", "2019"), ("RCEP", "Regional Comprehensive Economic Partnership", "2020"),
    ("QUAD", "Quadrilateral Security Dialogue", "2007"),
]

# (observation id, code) -> (placeholder note, test_type, discriminating, feasibility, na)
# The sample statuses in method-layer §3.3 are format illustrations, not findings, so only the
# test design is seeded; the placeholder text keeps the sample's hint about what to test.
CELLS: dict[tuple[str, str], tuple[str, str | None, int, int, bool]] = {
    ("O1-1994-arf", "H1"): ("n/a: no prior institution to be inert (H1 explains persistence, not origin).", None, 0, 0, True),
    ("O1-1994-arf", "H2"): ("Did ASEAN members propose and shape the forum as a collective move?", "straw", 1, 2, False),
    ("O1-1994-arf", "H3"): ("Did great powers accept an ASEAN-chaired format because it was low-cost?", "straw", 1, 2, False),
    ("O1-1994-arf", "H4"): ("Did the ARF link actors that had no other multilateral tie in 1994?", "straw", 1, 1, False),
    ("O2-2005-eas-criteria", "H1"): ("Did existing ASEAN rules carry over into the EAS without new bargaining?", "straw", 0, 2, False),
    ("O2-2005-eas-criteria", "H2"): ("Smoking-gun candidate: did members draft the criteria against outside proposals?", "smoking-gun", 2, 2, False),
    ("O2-2005-eas-criteria", "H3"): ("Did great powers accept the criteria as the price of entry?", "straw", 1, 2, False),
    ("O2-2005-eas-criteria", "H4"): ("Did the criteria admit actors lacking other leader-level ties?", "straw", 1, 1, False),
    ("O3-2011-us-russia-eas", "H1"): ("Did joining follow from existing membership rules alone?", "straw", 0, 2, False),
    ("O3-2011-us-russia-eas", "H2"): ("Did members lobby the US and Russia to join as a collective strategy?", "straw", 1, 2, False),
    ("O3-2011-us-russia-eas", "H3"): ("Hoop for H3: did the US and Russia judge the EAS useful and non-threatening when joining?", "hoop", 1, 2, False),
    ("O3-2011-us-russia-eas", "H4"): ("Hoop for H4: did the US lack another venue with China at leader level?", "hoop", 2, 2, False),
    ("O4-2012-phnom-penh-amm", "H1"): ("Hoop for H1: did the institution carry itself through the split?", "hoop", 2, 2, False),
    ("O4-2012-phnom-penh-amm", "H2"): ("Did the coalition hold when members disagreed over the South China Sea?", "straw", 1, 2, False),
    ("O4-2012-phnom-penh-amm", "H3"): ("Did great powers change their use of ASEAN venues after the failure?", "straw", 0, 2, False),
    ("O4-2012-phnom-penh-amm", "H4"): ("Did outside actors route around ASEAN afterwards?", "straw", 1, 1, False),
    ("O5-2019-aoip", "H1"): ("Did the AOIP restate existing rules rather than respond to outside frames?", "straw", 0, 2, False),
    ("O5-2019-aoip", "H2"): ("Did members author the centrality framing collectively?", "straw", 1, 2, False),
    ("O5-2019-aoip", "H3"): ("Did great powers endorse the AOIP framing?", "straw", 0, 2, False),
    ("O5-2019-aoip", "H4"): ("Did the AOIP position ASEAN as the link between Indo-Pacific frames?", "straw", 0, 2, False),
    ("O6-2021-quad", "H1"): ("Does invoking centrality reflect institutional habit only?", "straw", 0, 2, False),
    ("O6-2021-quad", "H2"): ("Did ASEAN members press Quad states to name centrality?", "straw", 0, 2, False),
    ("O6-2021-quad", "H3"): ("Straw-in-the-wind only: do Quad statements signal acceptance of ASEAN venues?", "straw", 1, 2, False),
    ("O6-2021-quad", "H4"): ("Hoop for H4: minilaterals are bypass ties, so centrality should fall as they grow.", "hoop", 2, 2, False),
}
PLACEHOLDER = "Placeholder, rewrite before looking: "

CENTRALITY_DIMENSIONS = [
    {"key": "convening", "label": "Convening", "description": "ASEAN hosts and chairs", "indicators": []},
    {"key": "agenda", "label": "Agenda", "description": "ASEAN sets the agenda text", "indicators": []},
    {"key": "membership", "label": "Membership gatekeeping", "description": "ASEAN rules decide entry", "indicators": []},
    {"key": "positional", "label": "Positional", "description": "network betweenness", "indicators": []},
    {"key": "normative", "label": "Normative", "description": "others invoke 'ASEAN centrality'", "indicators": []},
]
CONCEPTS = {
    "centrality": ("Centrality", CENTRALITY_DIMENSIONS),
    "reassurance": ("Reassurance", []),
}
PROJECT_CONCEPTS = {ASEAN: ["centrality", "reassurance"], USROK: ["reassurance"]}
SEEDABLE = (ASEAN, USROK)


def _concepts(conn: sqlite3.Connection, slug: str, force: bool, counts: dict[str, int]) -> None:
    for sort, cid in enumerate(PROJECT_CONCEPTS.get(slug, [])):
        label, dims = CONCEPTS[cid]
        cur = conn.execute("INSERT OR IGNORE INTO concepts(id, label, dimensions_json, updated_at) VALUES (?,?,?,?)",
                           (cid, label, json.dumps(dims), now_iso()))
        counts["concepts"] += cur.rowcount
        if force and not cur.rowcount:
            row = conn.execute("SELECT dimensions_json FROM concepts WHERE id = ?", (cid,)).fetchone()
            if dims and json.loads(row[0] or "[]") == []:
                conn.execute("UPDATE concepts SET dimensions_json = ?, updated_at = ? WHERE id = ?", (json.dumps(dims), now_iso(), cid))
        conn.execute("INSERT OR IGNORE INTO concept_projects(concept_id, project_slug, sort) VALUES (?,?,?)", (cid, slug, sort))


def _asean(conn: sqlite3.Connection, force: bool, counts: dict[str, int]) -> None:
    now = now_iso()
    for sort, (cid, label, core) in enumerate(CAMPS):
        cur = conn.execute("INSERT OR IGNORE INTO camps(id, project_slug, label, core_claim, sort) VALUES (?,?,?,?,?)",
                           (cid, ASEAN, label, core, sort))
        counts["camps"] += cur.rowcount
        if force and not cur.rowcount:
            conn.execute("UPDATE camps SET label = ?, core_claim = ?, sort = ? WHERE id = ? AND project_slug = ?",
                         (label, core, sort, cid, ASEAN))
    claim_ids: dict[str, int] = {}
    for code, statement, own in HYPOTHESES:
        cur = conn.execute(
            "INSERT OR IGNORE INTO claims(project_slug, code, kind, statement, is_own, origin, status, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)", (ASEAN, code, "explanation", statement, own, "user", "accepted", now))
        counts["claims"] += cur.rowcount
        if force and not cur.rowcount:
            conn.execute("UPDATE claims SET statement = ?, is_own = ? WHERE project_slug = ? AND code = ?",
                         (statement, own, ASEAN, code))
        claim_ids[code] = conn.execute("SELECT id FROM claims WHERE project_slug = ? AND code = ?", (ASEAN, code)).fetchone()[0]
    for sort, (oid, label, date, note, _hint) in enumerate(OBSERVATIONS):
        cur = conn.execute(
            "INSERT OR IGNORE INTO cases(id, kind, project_slug, label, date_start, is_observation, selection_note, sort) "
            "VALUES (?,?,?,?,?,?,?,?)", (oid, "episode", ASEAN, label, date, 1, note, sort))
        counts["cases"] += cur.rowcount
        if force and not cur.rowcount:
            conn.execute("UPDATE cases SET label = ?, date_start = ?, selection_note = ?, sort = ?, is_observation = 1 WHERE id = ?",
                         (label, date, note, sort, oid))
    for sort, (fid, label, year) in enumerate(FORUMS):
        cur = conn.execute(
            "INSERT OR IGNORE INTO cases(id, kind, project_slug, label, date_start, is_observation, sort) VALUES (?,?,?,?,?,?,?)",
            (fid, "forum", ASEAN, label, year, 0, 100 + sort))
        counts["cases"] += cur.rowcount
    hints = {o[0]: o[4] for o in OBSERVATIONS}
    for (oid, code), (note, test, disc, feas, na) in CELLS.items():
        implication = note if na else PLACEHOLDER + note
        cur = conn.execute(
            "INSERT OR IGNORE INTO evidence_cells(claim_id, case_id, status, implication, implication_origin, test_type, "
            "discriminating, feasibility, hint, hint_origin, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (claim_ids[code], oid, "na" if na else "missing", implication, "placeholder", test, disc, feas, hints[oid], "seed", now))
        counts["cells"] += cur.rowcount
        if force and not cur.rowcount:
            # Only the seeded design is refreshed; a user-written implication, a status, excerpts and
            # search logs are never touched.
            conn.execute(
                "UPDATE evidence_cells SET implication = CASE WHEN implication_origin = 'placeholder' THEN ? ELSE implication END, "
                "hint = CASE WHEN hint_origin = 'seed' OR hint IS NULL THEN ? ELSE hint END, "
                "test_type = COALESCE(test_type, ?), discriminating = COALESCE(discriminating, ?), feasibility = COALESCE(feasibility, ?) "
                "WHERE claim_id = ? AND case_id = ?",
                (implication, hints[oid], test, disc, feas, claim_ids[code], oid))


def seed(conn: sqlite3.Connection, slug: str, force: bool = False) -> dict[str, int]:
    """Insert what is missing (or refresh seeded fields with force). Returns rows inserted per table."""
    if slug not in SEEDABLE:
        raise KeyError(f"no seed for project {slug!r}; seedable: {', '.join(SEEDABLE)}")
    counts = {"camps": 0, "claims": 0, "cases": 0, "cells": 0, "concepts": 0}
    conn.execute("BEGIN")
    try:
        if slug == ASEAN:
            _asean(conn, force, counts)
        _concepts(conn, slug, force, counts)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return counts
