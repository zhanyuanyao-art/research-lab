-- What the assistant proposed or changed, one row per field (docs/design/ISSUE-AGENT.md §4, §5).
-- Confirmation cards, undo and the after-the-fact audit all read this table; pm/changes.py owns it.
-- kind: issue now, setting from card 0080. A batch is one request (direct changes) or one card.
-- No foreign keys on purpose: the audit outlives threads and tasks, and a change made on the
-- settings page has neither.
CREATE TABLE assistant_changes (
  id INTEGER PRIMARY KEY,
  batch_id TEXT NOT NULL,
  task_id TEXT,                              -- agent_tasks.id
  thread_id TEXT,                            -- chat_threads.id, for the 来自对话 link and undo by thread
  message_id INTEGER,                        -- chat_messages.id the card or undo button hangs under
  kind TEXT NOT NULL,                        -- issue | setting
  action TEXT NOT NULL DEFAULT 'set',        -- set | create | comment
  target TEXT NOT NULL DEFAULT '',           -- issue ref / setting key; a create's new ref once applied
  label TEXT NOT NULL DEFAULT '',            -- the target's title when recorded, for cards and audit
  field TEXT NOT NULL DEFAULT '',
  old_json TEXT,                             -- the value the assistant saw
  new_json TEXT,                             -- the value it wants; a create's fields
  status TEXT NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed','applied','undone','dismissed')),
  result TEXT,                               -- conflict | noop | error | kept | deleted | parked | gone
  note TEXT,                                 -- why a row was skipped or left alone, in Chinese
  detail_json TEXT,                          -- e.g. {"carried": 1}: what undo needs besides old/new
  reason TEXT NOT NULL DEFAULT '',
  actor TEXT NOT NULL DEFAULT 'assistant',   -- assistant | user (a settings-page change, card 0080)
  created_at TEXT NOT NULL,
  applied_at TEXT,
  undone_at TEXT,
  dismissed_at TEXT
);
CREATE INDEX assistant_changes_batch ON assistant_changes(batch_id, id);
CREATE INDEX assistant_changes_thread ON assistant_changes(thread_id, id);
CREATE INDEX assistant_changes_task ON assistant_changes(task_id);
CREATE INDEX assistant_changes_target ON assistant_changes(kind, target);
