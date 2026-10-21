-- 复现目标 (card 0157, docs/design/REPLICATION-v2.md 第一步 and 附：步骤之间的数据格式): the numbers a replication aims
-- at, as the paper reports them. Kept apart from plan_json, which never carries results, and from our own runs, so a
-- 论文报告值 can never be read as a result of ours. Values are stored as printed (−0.231, (0.054), [0.10, 0.35],
-- 1,204); plans.target_records() hands them on in the appendix's shape, numbers parsed, for R4's comparison.
-- tid is the target's id within its plan (T1, T2…), never reused: replication_plans.targets_seq only goes up, and
-- R2's mapping.json names targets by it. origin: ai-suggested (methods.replicate, checked against the cited page)
-- or user. Nothing counts until Leo marks the targets reviewed (targets_reviewed_at). 0024 is card 0158's.
CREATE TABLE replication_targets (
  id INTEGER PRIMARY KEY,
  plan_id TEXT NOT NULL REFERENCES replication_plans(id) ON DELETE CASCADE,
  tid TEXT NOT NULL,
  position INTEGER NOT NULL DEFAULT 0,
  label TEXT NOT NULL DEFAULT '',          -- the table or figure (the format's `table`): Table 2, Figure 3
  model TEXT NOT NULL DEFAULT '',          -- the column or model: (3)
  term TEXT NOT NULL DEFAULT '',           -- as the paper writes it
  value TEXT NOT NULL DEFAULT '',          -- as printed; empty when the paper gives none (a figure's direction)
  se TEXT NOT NULL DEFAULT '',
  ci TEXT NOT NULL DEFAULT '',
  sig TEXT NOT NULL DEFAULT '',            -- the paper's own marker: **, p < 0.05
  n TEXT NOT NULL DEFAULT '',
  page TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  origin TEXT NOT NULL DEFAULT 'user' CHECK (origin IN ('ai-suggested', 'user')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (plan_id, tid)
);
CREATE INDEX replication_targets_plan ON replication_targets(plan_id, position);
ALTER TABLE replication_plans ADD COLUMN targets_seq INTEGER NOT NULL DEFAULT 0;
ALTER TABLE replication_plans ADD COLUMN targets_reviewed_at TEXT;
