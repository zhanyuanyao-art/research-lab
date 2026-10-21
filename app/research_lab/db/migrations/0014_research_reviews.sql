-- Research work remains a suggestion until a human adopts a particular revision.
CREATE TABLE research_sessions (
  id TEXT PRIMARY KEY,
  thread_id TEXT REFERENCES chat_threads(id),
  kind TEXT NOT NULL CHECK(kind IN ('literature-map','case-timeline')),
  question TEXT NOT NULL,
  checkpoint_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
ALTER TABLE agent_tasks ADD COLUMN research_session_id TEXT REFERENCES research_sessions(id);
CREATE TABLE research_sources (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES research_sessions(id),
  title TEXT NOT NULL,
  url TEXT NOT NULL,
  kind TEXT NOT NULL,
  body TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  retrieved_at TEXT NOT NULL
);
CREATE INDEX research_sources_session ON research_sources(session_id);
CREATE TABLE research_reads (
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES research_sources(id),
  locator TEXT NOT NULL,
  start_offset INTEGER NOT NULL,
  end_offset INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE research_searches (
  id INTEGER PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES research_sessions(id),
  backend TEXT NOT NULL,
  query TEXT NOT NULL,
  cutoff TEXT,
  status TEXT NOT NULL,
  results_json TEXT NOT NULL,
  error TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE research_drafts (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES research_sessions(id),
  task_id TEXT NOT NULL REFERENCES agent_tasks(id),
  revision INTEGER NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  citations_json TEXT NOT NULL,
  checkpoint_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('working','review','accepted')),
  artifact_ref TEXT,
  created_at TEXT NOT NULL,
  accepted_at TEXT,
  UNIQUE(session_id, revision)
);
CREATE INDEX research_drafts_session ON research_drafts(session_id, revision);
CREATE TABLE research_adoptions (
  draft_id TEXT PRIMARY KEY REFERENCES research_drafts(id),
  artifact_ref TEXT NOT NULL UNIQUE,
  relative_path TEXT NOT NULL UNIQUE,
  content TEXT NOT NULL,
  reviewed_title TEXT NOT NULL,
  reviewed_body TEXT NOT NULL,
  created_at TEXT NOT NULL
);
