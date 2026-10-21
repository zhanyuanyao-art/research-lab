-- 复现工作区 (card 0158, docs/design/REPLICATION-v2.md 第三步): Leo's Start, Stop and Re-run as jobs, and the files of
-- a finished workspace run beside its analysis_runs row (method 'workspace'; status keeps its done | error). The
-- files themselves are frozen under <data>/replication/<plan>/runs/<RUN id>/. 0023 is card 0157's.
CREATE TABLE replication_jobs (
  id INTEGER PRIMARY KEY,
  plan_id TEXT NOT NULL REFERENCES replication_plans(id) ON DELETE CASCADE,
  mode TEXT NOT NULL CHECK (mode IN ('ai', 'rerun')),
  state TEXT NOT NULL CHECK (state IN ('running', 'done', 'error', 'stopped')),
  source_run TEXT,                           -- the run a re-run repeats
  run_id TEXT REFERENCES analysis_runs(id) ON DELETE SET NULL,
  pid INTEGER,                               -- the process group Stop kills (the CLI, or a re-run's script)
  host_pid INTEGER,                          -- the app process running the job: when it is gone, the job failed
  tokens INTEGER,
  note TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT
);
CREATE INDEX replication_jobs_plan ON replication_jobs(plan_id, id);
-- {mode, state (done | error | stopped), source_run, job, code (scripts in run order), tables, figures, unreadable,
--  needs_r (「需要 R」 notes), log, note, turns, seconds}
ALTER TABLE analysis_runs ADD COLUMN workspace_json TEXT;
ALTER TABLE analysis_runs ADD COLUMN tokens INTEGER;         -- what the AI session used; 0 for a re-run
