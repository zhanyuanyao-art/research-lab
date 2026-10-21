-- 全书复现清单 (card 0187, docs/design/REPLICATION-v4-DCL.md §3B): every table and figure of the paper and its online
-- appendix, found by rules (replication/coverage.py, no AI), with the author's script that produces it. Kept apart from
-- plan_json and from replication_targets: a row says "this item exists and this script makes it", never what its numbers
-- are. ticked and owner are Leo's: a re-scan rewrites caption, page, group and scripts of the items it finds and leaves
-- these two alone. item_key is source + label ("main:Table 3", "appendix:Table A41"); it is what a re-scan matches on.
-- grp: main = 正文结果, robust = 附录稳健性, other = 附录其他. scripts_json: [{path, full, how, ref, have}], best first.
CREATE TABLE replication_coverage (
  id INTEGER PRIMARY KEY,
  plan_id TEXT NOT NULL REFERENCES replication_plans(id) ON DELETE CASCADE,
  item_key TEXT NOT NULL,
  source TEXT NOT NULL CHECK (source IN ('main', 'appendix')),
  kind TEXT NOT NULL CHECK (kind IN ('table', 'figure')),
  label TEXT NOT NULL,                     -- as the paper prints it: Table 3, Figure A16
  caption TEXT NOT NULL DEFAULT '',
  page TEXT NOT NULL DEFAULT '',           -- the printed page label of the file the item is in
  section TEXT NOT NULL DEFAULT '',        -- the appendix heading it sits under: 5.2 Disaggregating Results by Meeting Type
  grp TEXT NOT NULL CHECK (grp IN ('main', 'robust', 'other')),
  position INTEGER NOT NULL DEFAULT 0,
  scripts_json TEXT NOT NULL DEFAULT '[]',
  outputs_json TEXT NOT NULL DEFAULT '[]', -- files the scripts write for it: figures/coefPlotMain.pdf
  ticked INTEGER NOT NULL DEFAULT 0,
  owner TEXT NOT NULL DEFAULT '',          -- 汇报人
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (plan_id, item_key)
);
CREATE INDEX replication_coverage_plan ON replication_coverage(plan_id, grp, position);
-- A second PDF given by path (the CLI) when it is not attached to the paper: its page texts, read once and never edited.
CREATE TABLE replication_coverage_sources (
  id INTEGER PRIMARY KEY,
  plan_id TEXT NOT NULL REFERENCES replication_plans(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  page_count INTEGER NOT NULL DEFAULT 0,
  pages_json TEXT NOT NULL,                -- [[page label, text], …]
  added_at TEXT NOT NULL,
  UNIQUE (plan_id, sha256)
);
-- How many tables and figures the last scan found beyond the list's ceiling (coverage.MAX_ITEMS): the page says 「另有 N 项未列出」.
ALTER TABLE replication_plans ADD COLUMN coverage_omitted INTEGER NOT NULL DEFAULT 0;
