-- Task agent: a request handed to 助手 runs as a task under one skill on whichever engine is active.
-- Its timeline (tool calls, notes) is kept so the 助手 page shows what was done, Linear-style; usage
-- is recorded per skill and engine so token cost can be compared before switching engines.
CREATE TABLE agent_tasks (
  id TEXT PRIMARY KEY,
  thread_id TEXT REFERENCES chat_threads(id) ON DELETE CASCADE,
  message_id INTEGER,                        -- the assistant message that shows this task
  skill TEXT NOT NULL,
  engine TEXT,                               -- claude | deepseek
  model TEXT,
  status TEXT NOT NULL,                      -- queued | running | waiting | done | failed | cancelled
  prompt TEXT NOT NULL,
  session_id TEXT,                           -- Claude Code session, resumed for follow-ups
  history_json TEXT,                         -- DeepSeek message history, replayed for follow-ups
  parent_id TEXT,                            -- the task this one follows up
  report TEXT,
  question TEXT,
  error TEXT,
  tool_calls INTEGER NOT NULL DEFAULT 0,
  input_tokens INTEGER, cache_tokens INTEGER, output_tokens INTEGER,
  created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT
);
CREATE INDEX agent_tasks_thread ON agent_tasks(thread_id, created_at);
CREATE INDEX agent_tasks_status ON agent_tasks(status);

CREATE TABLE agent_task_events (
  id INTEGER PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES agent_tasks(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,                        -- tool | note | error
  text TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX agent_task_events_task ON agent_task_events(task_id, id);
