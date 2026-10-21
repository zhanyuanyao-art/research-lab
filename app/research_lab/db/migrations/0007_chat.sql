-- Chat layer: conversation threads and their messages. Assistant messages may carry a card
-- (card_kind + card_ref) rendered live from the working records, and the AI job that fills them.
CREATE TABLE chat_threads (
  id TEXT PRIMARY KEY,                       -- T-YYYY-NNNN
  title TEXT NOT NULL,
  project_slug TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  archived INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX chat_threads_updated ON chat_threads(archived, updated_at DESC);

CREATE TABLE chat_messages (
  id INTEGER PRIMARY KEY,
  thread_id TEXT NOT NULL REFERENCES chat_threads(id) ON DELETE CASCADE,
  role TEXT NOT NULL CHECK (role IN ('user','assistant','system')),
  content TEXT NOT NULL DEFAULT '',          -- markdown
  card_kind TEXT,
  card_ref TEXT,
  job_id TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX chat_messages_thread ON chat_messages(thread_id, id);
CREATE INDEX chat_messages_job ON chat_messages(job_id) WHERE job_id IS NOT NULL;
