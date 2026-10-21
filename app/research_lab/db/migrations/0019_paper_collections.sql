-- User-named library folders are independent from research directions and projects.
CREATE TABLE paper_collections (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL COLLATE NOCASE UNIQUE,
  version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);
CREATE TABLE paper_collection_items (
  collection_id TEXT NOT NULL REFERENCES paper_collections(id) ON DELETE CASCADE,
  paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
  PRIMARY KEY (collection_id, paper_id)
);
CREATE INDEX paper_collection_items_paper ON paper_collection_items(paper_id);
