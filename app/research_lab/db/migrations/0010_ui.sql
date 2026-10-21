-- V2 workspace UI: artifacts carry a research direction (front matter `direction`, defaulting from the
-- project's direction in _data/projects.yml). direction_source records which one the index used, so a
-- project's direction change can re-default its artifacts without re-reading every file.
ALTER TABLE artifacts ADD COLUMN direction TEXT;
ALTER TABLE artifacts ADD COLUMN direction_source TEXT;
CREATE INDEX IF NOT EXISTS artifacts_direction ON artifacts(direction);
-- Force the next index pass to re-read every file so the new columns fill in.
UPDATE artifacts SET mtime = 0;
