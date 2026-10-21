-- Method module: "where to look" per evidence cell, competing usages per concept,
-- and which projects a concept ledger belongs to (concepts are shared across projects).
ALTER TABLE evidence_cells ADD COLUMN hint TEXT;
ALTER TABLE evidence_cells ADD COLUMN hint_origin TEXT NOT NULL DEFAULT 'user';
ALTER TABLE concepts ADD COLUMN competing_json TEXT NOT NULL DEFAULT '[]';
CREATE TABLE concept_projects (
  concept_id TEXT NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
  project_slug TEXT NOT NULL, sort INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (concept_id, project_slug));
CREATE INDEX evidence_cells_case ON evidence_cells(case_id);
CREATE INDEX search_log_cell ON search_log(cell_id);
