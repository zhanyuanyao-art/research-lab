-- Local feed engine (research_lab/feeds): new journal articles and think-tank commentary for 首页 · 推送.
-- Rows are keyed by content (a DOI hash, a canonical-link hash), never by list position, so a refresh
-- or a reorder never moves the user's 收藏 / 忽略 marks onto another item.
--
-- status: new (not acted on) | saved (收藏) | dismissed (忽略, hidden). paper_id is set once the item
-- became a library paper. enrich_batch / enriched_at track the assistant's Chinese title + summary pass
-- (feeds.enrich jobs); ai_run_id records which run wrote the AI fields. origin: crossref | rss | history.

CREATE TABLE feed_papers (
  id TEXT PRIMARY KEY,                 -- sha1("doi:" + lower DOI)[:16]
  doi TEXT UNIQUE,
  journal TEXT,                        -- abbr from _data/feeds.yml
  journal_name TEXT,
  journal_column TEXT,
  pool TEXT,
  title TEXT NOT NULL,
  authors_json TEXT,
  published TEXT,                      -- YYYY-MM-DD (Crossref registration date, as the old digest used)
  abstract TEXT,
  zh_title TEXT,
  zh_abstract TEXT,                    -- full Chinese abstract (old digests)
  zh_summary TEXT,                     -- one-sentence Chinese summary (assistant)
  direction TEXT,
  direction_source TEXT,               -- keyword | ai
  score INTEGER,                       -- relevance 0-5 from the assistant
  radar INTEGER NOT NULL DEFAULT 0,
  radar_json TEXT,
  is_oa INTEGER,                       -- 1 open access, 0 closed, NULL unknown
  oa_pdf_url TEXT,
  oa_license TEXT,
  origin TEXT NOT NULL DEFAULT 'crossref',
  first_seen TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'saved', 'dismissed')),
  status_at TEXT,
  paper_id TEXT,
  enrich_batch TEXT,
  enriched_at TEXT,
  ai_run_id INTEGER,
  updated_at TEXT
);
CREATE INDEX feed_papers_published ON feed_papers(published);
CREATE INDEX feed_papers_status ON feed_papers(status, published);
CREATE INDEX feed_papers_journal ON feed_papers(journal);
CREATE INDEX feed_papers_batch ON feed_papers(enrich_batch);

CREATE TABLE feed_news (
  id TEXT PRIMARY KEY,                 -- sha1 of the canonical link
  source TEXT NOT NULL,
  title TEXT NOT NULL,
  link TEXT NOT NULL,
  published TEXT,                      -- ISO date or date-time (UTC)
  summary TEXT,
  image TEXT,
  zh_title TEXT,
  zh_summary TEXT,
  angle TEXT,                          -- why it matters for Leo's research
  category TEXT,
  score INTEGER,
  direction TEXT,
  direction_source TEXT,
  origin TEXT NOT NULL DEFAULT 'rss',
  first_seen TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'saved', 'dismissed')),
  status_at TEXT,
  enrich_batch TEXT,
  enriched_at TEXT,
  ai_run_id INTEGER,
  updated_at TEXT
);
CREATE INDEX feed_news_published ON feed_news(published);
CREATE INDEX feed_news_status ON feed_news(status, published);
CREATE INDEX feed_news_source ON feed_news(source);
CREATE INDEX feed_news_batch ON feed_news(enrich_batch);
