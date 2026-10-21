# Security

Only the latest commit on `main` is supported. Fixes are not backported.

Report a vulnerability privately through GitHub: [Security → Report a vulnerability](https://github.com/zhanyuanyao-art/research-lab/security/advisories/new). Do not open a public issue for it, and do not post secrets or private research anywhere in the repository. There is no reporting email address.

Run the app only on the loopback interface. Keep the workspace and its backups private. Model services receive submitted inputs when enabled; AI-generated text and retrieved content must never be treated as authorization to change settings, publish material, or execute development commands.

The export tool checks explicit paths, file types, links, and a bounded set of credential patterns. A clean scan is not a complete security audit. Never make the original personal repository or its history public.
