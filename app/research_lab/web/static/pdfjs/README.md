# PDF.js 6.4.299 (legacy build)

Upstream: https://github.com/mozilla/pdf.js/releases/tag/v6.4.299

Source: https://registry.npmjs.org/pdfjs-dist/-/pdfjs-dist-6.4.299.tgz

Integrity: `sha512-AVl138zALtfaAPvADulE0PZThbYzCBS79nL4pOSL/6Sm/4AH5A21BD9VHt97OlCuzJuCpmeZtAtkinisF4Vb1g==`

Apache-2.0; see LICENSE and asset-specific license files. Copied from the npm distribution (minified build files renamed). The viewer CSS is wrapped in `@scope (#pdf-container)` and its `:root` selectors changed to `:scope` to isolate upstream global sidebar/toolbar rules from the app. All other assets are unmodified. Core, worker and viewer must be updated together. Fonts, CMaps and WASM stay local for offline reading. No PDF scripting manager or annotation editor is enabled. This directory is separate from the editor vendor bundle to keep ownership independent.
