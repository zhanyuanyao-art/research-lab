# Third-party dependencies

The source preview and Mac candidate preserve upstream notices. Application code uses AGPL-3.0-or-later as recorded in `LICENSE-DECISION.md`; `LICENSE` contains the full text. Upstream components retain their own terms.

## Frontend

`THIRD-PARTY-NOTICES.txt` preserves the original notices for htmx 2.0.11 (0BSD), Toast UI Editor 3.2.2 (MIT), Cytoscape.js 3.34.3 (MIT), DOMPurify 2.3.3 (Apache-2.0/MPL-2.0 choice), and Lucide/Feather (ISC/MIT). `third-party-sources.json` binds those notices and assets by SHA-256.

`MAC-THIRD-PARTY-NOTICES.txt` adds the editor's 15 embedded dependencies. The official 3.2.2 CDN bundle is byte-identical to the vendored minified file. Its readable bundle identifies the packages; the same release tag's package lock identifies the versions. The original npm package license texts were obtained with lockfile integrity verification. Sources, versions and notice byte ranges are recorded in `mac-third-party-sources.json`. The existing Microsoft helper banner remains intact. This closes the notice inventory; it is not a vulnerability audit of the archived editor.

The 62-symbol Lucide sprite is rebuilt from individually pinned upstream SVGs, preserving existing symbol IDs and application styling. Exact sources and hashes are recorded in the Mac supplement, including the older `trash-2` name. Nine symbols have upstream geometry updates; no new UI destinations are introduced.

Also retained:

- PDF.js: its root, cmaps, standard-font and wasm licenses under `app/research_lab/web/static/pdfjs/`.
- Tiptap: `app/research_lab/web/static/vendor/tiptap-editor.LICENSES.txt`; source and package lock under `scripts/vendor/editor/`.

## Mac / Python

The trial builder checks installed distribution versions against `uv.lock`, then collects original license files, including `LICENCE` spellings, nested vendor notices and Matplotlib font notices. Full relative paths prevent same-named licenses from overwriting each other. The generated `licenses/inventory.json` is a build-environment superset; `licenses/bundle-modules.json` lists the actual Python and native payload. Neither includes absolute developer paths.

The supplement fills packaging omissions with verified upstream sources:

- PyMuPDF 1.28.2's complete AGPL text; the wheel's short license summary remains preserved.
- PyObjC core, Security and UniformTypeIdentifiers 12.2.2 notices extracted from the exact source archives in `uv.lock`.
- proxy_tools 0.1.0: its metadata says MIT, but the byte-identical code at the release-era commit accompanies BSD-2-Clause text. Preserve the actual upstream text, not a generated MIT notice.
- CPython 3.14.3 standalone build 20260325 and its upstream native notices. The build refuses a different interpreter build until this inventory is reviewed.

NumPy, SciPy, Pillow, Matplotlib, pyreadstat and other distributions retain their complete supplied notices, including native dependencies and runtime exceptions. PyInstaller's bootloader exception and runtime license texts are also retained. The Mac application places these materials at `Contents/Resources/licenses/`.

## Remaining release decisions

The DMG contains the official PyMuPDF and MuPDF 1.28.2 source archives, verified by recorded hashes, alongside application source and build instructions. MuPDF source notices, including optional components and fonts, are included as a superset. The selected application license is AGPL-3.0-or-later. Review the complete corresponding-source delivery for each release; including AGPL text alone is insufficient. The private candidate must not be presented as a permissively licensed or notarized public release. See `LICENSE-DECISION.md` and `MAC-TRIAL.md`.
