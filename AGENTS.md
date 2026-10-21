# Research Lab contributor instructions

This snapshot is a private review candidate. Read README.md and REVIEW.md before changing it. Never publish it or choose a license without the owner's explicit approval.

Code is in app/research_lab; synthetic tests are in app/tests. Work in a separate branch. Do not read a user's workspace, .env, library database, research files, or private history. Use synthetic fixtures and temporary workspaces. Run tests with model workers and network jobs disabled.

Use the shared UI components and CSS variables. Preserve human review before adoption of research drafts. Never fabricate quotations, write the researcher's Claim, or set evidence status. Keep code comments concise and in English.

Maintainer-only development and release commands are outside the supported candidate workflow. Do not run a command that pushes a repository, installs an app, or starts an autofix worker unless explicitly requested.
