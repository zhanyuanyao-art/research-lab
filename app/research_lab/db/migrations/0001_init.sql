-- Research Lab V1 schema. Markdown/YAML in the repo stay the source of truth for
-- research writing; these tables hold working records and derived caches.

CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);

CREATE TABLE papers (
  id TEXT PRIMARY KEY,
  doi TEXT UNIQUE, isbn TEXT, openalex_id TEXT,
  title TEXT NOT NULL, title_hash TEXT NOT NULL, authors_json TEXT NOT NULL DEFAULT '[]',
  year INTEGER, venue TEXT, kind TEXT CHECK (kind IN ('article','book','chapter','report','other')),
  abstract TEXT,
  metadata_status TEXT NOT NULL DEFAULT 'unverified' CHECK (metadata_status IN ('unverified','candidate','verified')),
  crossref_json TEXT, openalex_json TEXT,
  camp TEXT, claim_text TEXT, mechanism TEXT, period TEXT, method TEXT, level_of_analysis TEXT,
  concept_usage TEXT, cannot_explain TEXT, relation_to_puzzle TEXT,
  read_depth TEXT CHECK (read_depth IN ('abstract','skimmed','read','reread')),
  ai_summary_json TEXT, ai_summary_run_id INTEGER,
  private INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX papers_title_hash ON papers(title_hash);
CREATE TABLE paper_projects (
  paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
  project_slug TEXT NOT NULL, PRIMARY KEY (paper_id, project_slug));

CREATE TABLE paper_files (
  id INTEGER PRIMARY KEY, paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
  sha256 TEXT NOT NULL UNIQUE, rel_path TEXT NOT NULL, original_name TEXT,
  bytes INTEGER NOT NULL, page_count INTEGER NOT NULL, has_text_layer INTEGER NOT NULL,
  extractor TEXT NOT NULL, is_primary INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
CREATE TABLE paper_pages (
  file_id INTEGER NOT NULL REFERENCES paper_files(id) ON DELETE CASCADE,
  page_index INTEGER NOT NULL, page_label TEXT, text TEXT NOT NULL,
  PRIMARY KEY (file_id, page_index));
CREATE TABLE paper_sections (
  file_id INTEGER NOT NULL REFERENCES paper_files(id) ON DELETE CASCADE,
  kind TEXT NOT NULL, heading TEXT, page_start INTEGER, page_end INTEGER);

CREATE TABLE camps (id TEXT PRIMARY KEY, project_slug TEXT NOT NULL, label TEXT NOT NULL, core_claim TEXT, sort INTEGER);
CREATE TABLE concepts (
  id TEXT PRIMARY KEY, label TEXT NOT NULL, dimensions_json TEXT NOT NULL DEFAULT '[]',
  working_definition TEXT, def_version TEXT, ladder TEXT, updated_at TEXT NOT NULL);
CREATE TABLE concept_versions (
  concept_id TEXT NOT NULL REFERENCES concepts(id) ON DELETE CASCADE, version TEXT NOT NULL,
  text TEXT NOT NULL, reason TEXT, artifact_ref TEXT, created_at TEXT NOT NULL,
  PRIMARY KEY (concept_id, version));
CREATE TABLE cases (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK (kind IN ('forum','episode')), project_slug TEXT,
  label TEXT NOT NULL, date_start TEXT, date_end TEXT,
  is_observation INTEGER NOT NULL DEFAULT 0, selection_note TEXT, sort INTEGER);
CREATE TABLE claims (
  id INTEGER PRIMARY KEY, project_slug TEXT NOT NULL, code TEXT,
  kind TEXT NOT NULL CHECK (kind IN ('explanation','mechanism','definition','claim')),
  statement TEXT NOT NULL, is_own INTEGER NOT NULL DEFAULT 0,
  origin TEXT NOT NULL DEFAULT 'user' CHECK (origin IN ('user','ai-suggested')),
  status TEXT NOT NULL DEFAULT 'accepted' CHECK (status IN ('accepted','proposed','rejected')),
  strength TEXT, source_excerpts_json TEXT NOT NULL DEFAULT '[]', ai_run_id INTEGER,
  created_at TEXT NOT NULL,
  UNIQUE (project_slug, code));
CREATE VIEW hypotheses AS SELECT * FROM claims WHERE kind = 'explanation';

CREATE TABLE excerpts (
  id TEXT PRIMARY KEY,
  paper_id TEXT NOT NULL REFERENCES papers(id),
  file_id INTEGER REFERENCES paper_files(id), page_index INTEGER, locator TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'quote' CHECK (kind IN ('quote','data','paraphrase','idea')),
  quote TEXT NOT NULL, quote_verified INTEGER NOT NULL DEFAULT 0,
  uses_json TEXT NOT NULL DEFAULT '[]', camp TEXT, my_note TEXT NOT NULL,
  strength TEXT CHECK (strength IN ('direct','inferential','circumstantial')),
  source_type TEXT NOT NULL DEFAULT 'secondary-scholarly',
  project_slug TEXT, status TEXT NOT NULL DEFAULT 'inbox' CHECK (status IN ('inbox','linked','used','discarded')),
  private INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX excerpts_paper ON excerpts(paper_id);
CREATE INDEX excerpts_created ON excerpts(created_at);

CREATE TABLE evidence_cells (
  id INTEGER PRIMARY KEY, claim_id INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
  case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT 'missing' CHECK (status IN ('missing','weak','strong','contradicting','searched-none','na')),
  implication TEXT, implication_origin TEXT NOT NULL DEFAULT 'user',
  test_type TEXT CHECK (test_type IN ('straw','hoop','smoking-gun','doubly-decisive')),
  discriminating INTEGER, feasibility INTEGER, priority INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL, UNIQUE (claim_id, case_id));
CREATE TABLE cell_excerpts (
  cell_id INTEGER NOT NULL REFERENCES evidence_cells(id) ON DELETE CASCADE,
  excerpt_id TEXT NOT NULL REFERENCES excerpts(id) ON DELETE CASCADE, PRIMARY KEY (cell_id, excerpt_id));
CREATE TABLE search_log (
  id INTEGER PRIMARY KEY, cell_id INTEGER NOT NULL REFERENCES evidence_cells(id) ON DELETE CASCADE,
  searched_on TEXT NOT NULL, where_searched TEXT NOT NULL, terms TEXT, result_note TEXT);

CREATE TABLE checks (
  id INTEGER PRIMARY KEY, project_slug TEXT NOT NULL, claim_id INTEGER REFERENCES claims(id) ON DELETE SET NULL,
  title TEXT NOT NULL, prediction TEXT NOT NULL, data_needed TEXT, method TEXT, would_weaken_if TEXT,
  status TEXT NOT NULL DEFAULT 'planned' CHECK (status IN ('planned','data-ready','run','recorded')),
  result_note TEXT, reading TEXT, script_ref TEXT, artifact_ref TEXT,
  origin TEXT NOT NULL DEFAULT 'user', ai_run_id INTEGER,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);

CREATE TABLE milestone_confirmations (
  project_slug TEXT NOT NULL, milestone TEXT NOT NULL, item TEXT NOT NULL,
  confirmed_at TEXT NOT NULL, note TEXT, PRIMARY KEY (project_slug, milestone, item));

-- One typed edge table: map edges, tags, excerpt links, suggestions. Rejected rows stay so they are not re-proposed.
CREATE TABLE associations (
  id INTEGER PRIMARY KEY,
  src_type TEXT NOT NULL, src_id TEXT NOT NULL,
  rel TEXT NOT NULL,
  dst_type TEXT NOT NULL, dst_id TEXT NOT NULL,
  stance TEXT,
  origin TEXT NOT NULL CHECK (origin IN ('user','ai-suggested','derived')),
  status TEXT NOT NULL DEFAULT 'accepted' CHECK (status IN ('accepted','proposed','rejected')),
  score REAL, reason TEXT, reject_reason TEXT, ai_run_id INTEGER, created_at TEXT NOT NULL,
  UNIQUE (src_type, src_id, rel, dst_type, dst_id));
CREATE INDEX assoc_src ON associations(src_type, src_id);
CREATE INDEX assoc_dst ON associations(dst_type, dst_id);

-- Derived cache of _artifacts/*.md; rebuildable at any time with `rl artifact index`.
CREATE TABLE artifacts (
  ref TEXT PRIMARY KEY, path TEXT NOT NULL, title TEXT, week TEXT, type TEXT, level TEXT,
  project TEXT, status TEXT, version TEXT, created TEXT, related_json TEXT NOT NULL DEFAULT '[]',
  body TEXT, counts INTEGER NOT NULL DEFAULT 0, sha256 TEXT NOT NULL, mtime REAL NOT NULL);

CREATE TABLE ai_jobs (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('queued','claimed','done','failed','cancelled')),
  target_type TEXT, target_id TEXT,
  request_json TEXT NOT NULL, input_files_json TEXT NOT NULL DEFAULT '[]', schema_json TEXT NOT NULL,
  dedupe_key TEXT, result_json TEXT, error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, claimed_at TEXT, completed_at TEXT);
CREATE UNIQUE INDEX ai_jobs_open ON ai_jobs(dedupe_key) WHERE status IN ('queued','claimed') AND dedupe_key IS NOT NULL;
CREATE TABLE ai_runs (
  id INTEGER PRIMARY KEY, job_id TEXT NOT NULL REFERENCES ai_jobs(id),
  provider TEXT NOT NULL, model TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
  prompt_tokens INTEGER, cache_hit_tokens INTEGER, completion_tokens INTEGER, reasoning_tokens INTEGER,
  cost_usd REAL, valid INTEGER, repaired INTEGER NOT NULL DEFAULT 0, log_path TEXT, error TEXT);
CREATE TABLE http_cache (url TEXT PRIMARY KEY, fetched_at TEXT NOT NULL, status INTEGER, body TEXT);

-- Full text (external-content FTS5, kept in sync by triggers).
CREATE VIRTUAL TABLE excerpts_fts USING fts5(quote, my_note, content='excerpts', content_rowid='rowid',
  tokenize='porter unicode61 remove_diacritics 2');
CREATE TRIGGER excerpts_ai AFTER INSERT ON excerpts BEGIN
  INSERT INTO excerpts_fts(rowid, quote, my_note) VALUES (new.rowid, new.quote, new.my_note); END;
CREATE TRIGGER excerpts_ad AFTER DELETE ON excerpts BEGIN
  INSERT INTO excerpts_fts(excerpts_fts, rowid, quote, my_note) VALUES ('delete', old.rowid, old.quote, old.my_note); END;
CREATE TRIGGER excerpts_au AFTER UPDATE ON excerpts BEGIN
  INSERT INTO excerpts_fts(excerpts_fts, rowid, quote, my_note) VALUES ('delete', old.rowid, old.quote, old.my_note);
  INSERT INTO excerpts_fts(rowid, quote, my_note) VALUES (new.rowid, new.quote, new.my_note); END;

CREATE VIRTUAL TABLE artifacts_fts USING fts5(title, body, content='artifacts', content_rowid='rowid',
  tokenize='porter unicode61 remove_diacritics 2');
CREATE TRIGGER artifacts_ai AFTER INSERT ON artifacts BEGIN
  INSERT INTO artifacts_fts(rowid, title, body) VALUES (new.rowid, new.title, new.body); END;
CREATE TRIGGER artifacts_ad AFTER DELETE ON artifacts BEGIN
  INSERT INTO artifacts_fts(artifacts_fts, rowid, title, body) VALUES ('delete', old.rowid, old.title, old.body); END;
CREATE TRIGGER artifacts_au AFTER UPDATE ON artifacts BEGIN
  INSERT INTO artifacts_fts(artifacts_fts, rowid, title, body) VALUES ('delete', old.rowid, old.title, old.body);
  INSERT INTO artifacts_fts(rowid, title, body) VALUES (new.rowid, new.title, new.body); END;

CREATE VIRTUAL TABLE papers_fts USING fts5(title, abstract, claim_text, content='papers', content_rowid='rowid',
  tokenize='porter unicode61 remove_diacritics 2');
CREATE TRIGGER papers_ai AFTER INSERT ON papers BEGIN
  INSERT INTO papers_fts(rowid, title, abstract, claim_text) VALUES (new.rowid, new.title, new.abstract, new.claim_text); END;
CREATE TRIGGER papers_ad AFTER DELETE ON papers BEGIN
  INSERT INTO papers_fts(papers_fts, rowid, title, abstract, claim_text) VALUES ('delete', old.rowid, old.title, old.abstract, old.claim_text); END;
CREATE TRIGGER papers_au AFTER UPDATE ON papers BEGIN
  INSERT INTO papers_fts(papers_fts, rowid, title, abstract, claim_text) VALUES ('delete', old.rowid, old.title, old.abstract, old.claim_text);
  INSERT INTO papers_fts(rowid, title, abstract, claim_text) VALUES (new.rowid, new.title, new.abstract, new.claim_text); END;

CREATE VIRTUAL TABLE pages_fts USING fts5(text, content='paper_pages', content_rowid='rowid',
  tokenize='porter unicode61 remove_diacritics 2');
CREATE TRIGGER pages_ai AFTER INSERT ON paper_pages BEGIN
  INSERT INTO pages_fts(rowid, text) VALUES (new.rowid, new.text); END;
CREATE TRIGGER pages_ad AFTER DELETE ON paper_pages BEGIN
  INSERT INTO pages_fts(pages_fts, rowid, text) VALUES ('delete', old.rowid, old.text); END;

CREATE VIRTUAL TABLE terms_vocab USING fts5vocab(excerpts_fts, 'row');
