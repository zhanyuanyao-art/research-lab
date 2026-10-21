-- 新闻两条线 (docs/design/NEWS-v2.md, card 0061): 世界要闻 (wires and general outlets, ranked by importance) and
-- 研究相关 (think-tank commentary, ranked by relevance as before). lane comes from the source in _data/feeds.yml;
-- importance (0-5, weight in world politics, whatever the region) and region come from the scoring job.
ALTER TABLE feed_news ADD COLUMN lane TEXT NOT NULL DEFAULT 'research';
ALTER TABLE feed_news ADD COLUMN importance INTEGER;
ALTER TABLE feed_news ADD COLUMN region TEXT;
CREATE INDEX IF NOT EXISTS idx_feed_news_lane ON feed_news(lane, first_seen);
