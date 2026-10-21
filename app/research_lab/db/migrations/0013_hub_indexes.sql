-- Columns the 课题 hub, the direction folders and the AI queue filter on. All were full scans before.
CREATE INDEX IF NOT EXISTS excerpts_project ON excerpts(project_slug);
CREATE INDEX IF NOT EXISTS excerpts_status ON excerpts(status);
CREATE INDEX IF NOT EXISTS artifacts_project ON artifacts(project);
CREATE INDEX IF NOT EXISTS paper_projects_project ON paper_projects(project_slug);
CREATE INDEX IF NOT EXISTS papers_direction ON papers(direction);
CREATE INDEX IF NOT EXISTS ai_jobs_status_created ON ai_jobs(status, created_at);
