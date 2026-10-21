-- 数据复现: dataset registry, replication plans and analysis runs. The data itself lives in DuckDB
-- (app/data/lab.duckdb, one table ds_<year>_<seq> per dataset) with a parquet copy and the original
-- file under app/data/datasets/<id>/; these SQLite rows are the index and the provenance.

CREATE TABLE datasets (
  id TEXT PRIMARY KEY,                       -- DS-YYYY-NNNN
  name TEXT NOT NULL,
  source_kind TEXT NOT NULL CHECK (source_kind IN ('csv','xlsx','dta','sav','parquet')),
  original_name TEXT NOT NULL,
  rows INTEGER NOT NULL DEFAULT 0,
  cols INTEGER NOT NULL DEFAULT 0,
  schema_json TEXT NOT NULL DEFAULT '{}',    -- {columns:[{name,dtype,label}], sheet, sheets, encoding, delimiter}
  profile_json TEXT NOT NULL DEFAULT '{}',
  paper_id TEXT REFERENCES papers(id) ON DELETE SET NULL,
  direction TEXT,
  note TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT
);
CREATE INDEX datasets_paper ON datasets(paper_id);

CREATE TABLE replication_plans (
  id TEXT PRIMARY KEY,                       -- RP-YYYY-NNNN
  paper_id TEXT REFERENCES papers(id) ON DELETE SET NULL,
  direction TEXT,
  status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','reviewed','in-progress','done')),
  plan_json TEXT NOT NULL,
  origin TEXT NOT NULL DEFAULT 'user' CHECK (origin IN ('user','ai-suggested')),
  ai_run_id INTEGER,
  artifact_ref TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX replication_plans_paper ON replication_plans(paper_id);
CREATE INDEX replication_plans_run ON replication_plans(ai_run_id);

CREATE TABLE analysis_runs (
  id TEXT PRIMARY KEY,                       -- RUN-YYYY-NNNN
  dataset_id TEXT REFERENCES datasets(id) ON DELETE SET NULL,
  plan_id TEXT REFERENCES replication_plans(id) ON DELETE SET NULL,
  check_id INTEGER REFERENCES checks(id) ON DELETE SET NULL,
  method TEXT NOT NULL,
  params_json TEXT NOT NULL DEFAULT '{}',
  result_json TEXT,
  figure_path TEXT,
  code TEXT,
  reading TEXT,                              -- written by the user only
  ai_reading TEXT,                           -- analysis.read output (JSON), display only
  status TEXT NOT NULL DEFAULT 'done' CHECK (status IN ('done','error')),
  artifact_ref TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT
);
CREATE INDEX analysis_runs_dataset ON analysis_runs(dataset_id);
CREATE INDEX analysis_runs_plan ON analysis_runs(plan_id);
