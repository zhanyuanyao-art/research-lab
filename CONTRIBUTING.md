# Contributing

Contributions are accepted under AGPL-3.0-or-later at [github.com/zhanyuanyao-art/research-lab](https://github.com/zhanyuanyao-art/research-lab). Submit only code you have the right to contribute under the same application license, and preserve third-party notices. Open an issue with the bug or workflow form, or a pull request against `main`.

Use a separate branch or worktree. Keep changes small, preserve existing research-file formats, and add synthetic regression data rather than copying your workspace. Run the full tests before proposing behavior changes, and check light/dark themes and a 390px viewport for UI changes. Front-end components and color tokens live in `app/research_lab/web/static/app.css`; module stylesheets are for layout.

AI must not fabricate quotations, overwrite a researcher's Claim, set evidence strength, or adopt a draft without an explicit review action. Tests must not call paid model services or a real Claude Code account.

Do not attach private PDFs, conversations, databases, `.env`, credentials, or unsanitized screenshots to an issue or patch. Describe reproducible steps using an invented paper. Keep comments concise and explain the reason for non-obvious behavior.

The `rl dev` release tools in this snapshot were built for the original maintainer workflow. Contributors should not run them to publish, install, or modify a remote repository. Normal branches, commits, and local tests are sufficient for candidate review.

Follow [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). The bug and workflow forms ask for reproducible, invented examples. Report security problems privately as described in [SECURITY.md](SECURITY.md).
