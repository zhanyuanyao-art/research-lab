# Research Lab — notes for coding agents

Research Lab is published under AGPL-3.0-or-later. You may change your own copy. These notes help an agent such as Claude Code or Codex do that without touching the user's research.

## Layout

- `app/research_lab` is the FastAPI + Jinja + HTMX app. Pages live in `web/routes` and `web/templates`; shared components and color tokens are in `web/static/app.css`.
- `agent/` is the assistant Theo. Its prompts are `agent/skills/*.md`, whose front matter lists each skill's tools and trigger words; the tools are in `agent/tools.py` and the modules it registers.
- `feeds/` builds the daily push (journal presets in `feeds/presets.py`), `replication/` replicates papers, `files/` reads and writes the workspace's YAML files, `ai/` holds model providers and background jobs.
- `app/tests` holds synthetic tests.
- A workspace is a separate folder named by `RL_REPO_ROOT` and `RL_DATA_DIR`: papers, notes, `_data/*.yml`, `app/data/lab.sqlite` and `app/.env`. It is the user's data, not part of this repository.

## Run and test

```sh
uv sync --locked
TZ=Asia/Shanghai uv run pytest -q app/tests
RL_DISTRIBUTION=trial RL_REPO_ROOT=<workspace> RL_DATA_DIR=<workspace>/app/data RL_AUTOFIX=0 uv run rl serve
```

Restart `rl serve` after Python changes; templates and static files reload on their own.

## Rules

- Do not read or change the user's workspace, database, `.env` or research files unless the user asks. Test with synthetic fixtures and temporary workspaces. Tests must not call paid model services or a real Claude Code account.
- AI never fabricates quotations, writes the researcher's Claim, or sets evidence status, and AI drafts wait for the user to adopt them. Keep it that way.
- Use the shared UI components and CSS variables. Interface text is Simplified Chinese.
- Keep code comments concise and in English.
- `rl dev` (cards, autofix, release) and `rl launchd` belong to the original maintainer's setup and are hidden in trial mode. Use ordinary branches and commits instead, and merge or rebase onto `main` to take later updates.
