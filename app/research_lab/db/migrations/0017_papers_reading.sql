-- 阅读状态 (docs/design/PAPERS.md §A, card 0034): when a paper was last opened in the reader. The first
-- open moves an unread paper to 在读; the archive can sort by it. The three states themselves map onto
-- the existing read_depth values (未读 = NULL / abstract, 在读 = skimmed, 读完 = read / reread).
ALTER TABLE papers ADD COLUMN last_opened_at TEXT;
