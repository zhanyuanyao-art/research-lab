-- Book reviews in 推送 · 论文 (card 0154): journals publish them as journal-articles, so they arrive with the
-- papers. kind tells them apart; the reviewed book is parsed from the review's title (feeds/papers.py
-- parse_review) and any part may be missing, in which case the page shows the raw title. The Crossref
-- authors (authors_json) are the reviewer.
ALTER TABLE feed_papers ADD COLUMN kind TEXT NOT NULL DEFAULT 'article' CHECK (kind IN ('article', 'review'));
ALTER TABLE feed_papers ADD COLUMN book_title TEXT;
ALTER TABLE feed_papers ADD COLUMN book_authors TEXT;
ALTER TABLE feed_papers ADD COLUMN book_imprint TEXT;
CREATE INDEX feed_papers_kind ON feed_papers(kind, status, published);
