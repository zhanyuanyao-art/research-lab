-- Papers V2: language and research direction per paper, direction per excerpt, per-page translations,
-- and trigram full-text tables so Chinese/Japanese/Korean text is searchable without word segmentation
-- (unicode61 treats a run of CJK characters as one token). Search merges both indexes per row.
ALTER TABLE papers ADD COLUMN language TEXT;      -- zh | en | ja | ko | other; NULL = unknown (no text layer)
ALTER TABLE papers ADD COLUMN direction TEXT;     -- a slug from _data/directions.yml, user-set
ALTER TABLE excerpts ADD COLUMN direction TEXT;

-- Display-only machine translation of a file's pages; excerpts always quote paper_pages.
CREATE TABLE paper_translations (
  paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
  file_id INTEGER NOT NULL REFERENCES paper_files(id) ON DELETE CASCADE,
  page_index INTEGER NOT NULL,
  lang_from TEXT, lang_to TEXT NOT NULL,
  text TEXT NOT NULL,
  ai_run_id INTEGER,
  created_at TEXT NOT NULL,
  PRIMARY KEY (file_id, page_index, lang_to));
CREATE INDEX paper_translations_paper ON paper_translations(paper_id, lang_to);

CREATE VIRTUAL TABLE pages_fts_cjk USING fts5(text, content='paper_pages', content_rowid='rowid', tokenize='trigram');
CREATE TRIGGER pages_cjk_ai AFTER INSERT ON paper_pages BEGIN
  INSERT INTO pages_fts_cjk(rowid, text) VALUES (new.rowid, new.text); END;
CREATE TRIGGER pages_cjk_ad AFTER DELETE ON paper_pages BEGIN
  INSERT INTO pages_fts_cjk(pages_fts_cjk, rowid, text) VALUES ('delete', old.rowid, old.text); END;
CREATE TRIGGER pages_cjk_au AFTER UPDATE ON paper_pages BEGIN
  INSERT INTO pages_fts_cjk(pages_fts_cjk, rowid, text) VALUES ('delete', old.rowid, old.text);
  INSERT INTO pages_fts_cjk(rowid, text) VALUES (new.rowid, new.text); END;
INSERT INTO pages_fts_cjk(pages_fts_cjk) VALUES ('rebuild');

CREATE VIRTUAL TABLE excerpts_fts_cjk USING fts5(quote, my_note, content='excerpts', content_rowid='rowid', tokenize='trigram');
CREATE TRIGGER excerpts_cjk_ai AFTER INSERT ON excerpts BEGIN
  INSERT INTO excerpts_fts_cjk(rowid, quote, my_note) VALUES (new.rowid, new.quote, new.my_note); END;
CREATE TRIGGER excerpts_cjk_ad AFTER DELETE ON excerpts BEGIN
  INSERT INTO excerpts_fts_cjk(excerpts_fts_cjk, rowid, quote, my_note) VALUES ('delete', old.rowid, old.quote, old.my_note); END;
CREATE TRIGGER excerpts_cjk_au AFTER UPDATE ON excerpts BEGIN
  INSERT INTO excerpts_fts_cjk(excerpts_fts_cjk, rowid, quote, my_note) VALUES ('delete', old.rowid, old.quote, old.my_note);
  INSERT INTO excerpts_fts_cjk(rowid, quote, my_note) VALUES (new.rowid, new.quote, new.my_note); END;
INSERT INTO excerpts_fts_cjk(excerpts_fts_cjk) VALUES ('rebuild');
