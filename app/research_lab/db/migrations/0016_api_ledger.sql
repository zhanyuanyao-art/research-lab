-- One ledger of AI calls and one lease for running them (card 0001). Numbered 0016: version 15 was
-- recorded in Leo's database by an early draft of card 0025 (an artifacts.due column, since dropped).
--
-- Several processes share this database (the desktop app, `rl serve`, worktree servers). Only the
-- holder of the `ai` lease runs queued jobs and assistant tasks; the others only queue. `pid` on both
-- run tables says which process made a call, and `api_calls` puts job runs and task runs side by side
-- for the 开发 page: engine, model, what it was for, tokens, time and result.
ALTER TABLE ai_runs ADD COLUMN pid INTEGER;
ALTER TABLE agent_tasks ADD COLUMN pid INTEGER;

CREATE TABLE worker_lease (
  name TEXT PRIMARY KEY,
  holder TEXT NOT NULL,                      -- host:pid
  pid INTEGER NOT NULL,
  port INTEGER,
  acquired_at TEXT NOT NULL,
  renewed_at TEXT NOT NULL
);

CREATE VIEW api_calls AS
  SELECT r.started_at AS at,
         CASE WHEN r.provider LIKE 'claude%' THEN 'claude' ELSE r.provider END AS engine,
         r.model, 'job' AS kind, j.kind AS what, r.job_id AS ref, r.pid,
         r.prompt_tokens AS input_tokens, r.cache_hit_tokens AS cache_tokens, r.completion_tokens AS output_tokens,
         CAST(ROUND((julianday(r.finished_at) - julianday(r.started_at)) * 86400000) AS INTEGER) AS duration_ms,
         CASE WHEN r.finished_at IS NULL THEN 'running' WHEN r.valid = 1 THEN 'ok' ELSE 'error' END AS result,
         r.error
    FROM ai_runs r LEFT JOIN ai_jobs j ON j.id = r.job_id
  UNION ALL
  SELECT t.started_at, t.engine, t.model, 'task', t.skill, t.id, t.pid,
         t.input_tokens, t.cache_tokens, t.output_tokens,
         CAST(ROUND((julianday(t.finished_at) - julianday(t.started_at)) * 86400000) AS INTEGER),
         CASE t.status WHEN 'done' THEN 'ok' WHEN 'waiting' THEN 'ok' WHEN 'failed' THEN 'error'
                       WHEN 'cancelled' THEN 'cancelled' ELSE 'running' END,
         t.error
    FROM agent_tasks t WHERE t.started_at IS NOT NULL;
