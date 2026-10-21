# License and distribution decision

Decision recorded on 2026-10-12 after the owner delegated the choice: **AGPL-3.0-or-later**, with source-first local installation. Apple signing is a later binary-distribution step, not a prerequisite for publishing source. The source was published on 2026-10-21 at [github.com/zhanyuanyao-art/research-lab](https://github.com/zhanyuanyao-art/research-lab) as a fresh-history export; the original private repository and its history stay private.

## Application license

Research Lab application code in this source snapshot — Copyright (C) 2026 Leo Yao.

This program is free software: you can redistribute it and/or modify it under the terms of the GNU Affero General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See [LICENSE](LICENSE) for the full terms.

Third-party code, assets and fonts retain their own notices and license terms; see [THIRD-PARTY.md](THIRD-PARTY.md). This grant does not license user workspaces, research files, imported papers, datasets, conversations or credentials. Those are excluded from the source export and remain subject to their owners' rights. Existing grants for the original research site or older code are not revoked or changed by this snapshot.

## Why this route

The existing PDF engine uses the AGPL route offered by [PyMuPDF](https://pymupdf.io/licensing). Retaining it avoids a reader/extraction rewrite solely for licensing. AGPL permits commercial use and charging for copies or services; applicable corresponding-source and notice obligations still apply. In particular, section 13 addresses offering corresponding source to remote users of modified versions. It does not require researchers to publish their private papers or notes merely to use the app locally.

The LICENSE text is the verbatim GNU AGPL v3 text preserved in the pinned PyMuPDF 1.28.2 COPYING file; its provenance and SHA-256 are in mac-third-party-sources.json. The application grant above selects version 3 or later; it does not relicense third-party components.

## Release sequence

1. Publish only a reviewed clean source snapshot, with LICENSE, third-party notices, installation instructions and synthetic tests. Keep a new workspace separate from the source checkout.
2. Let a small group validate the Coach, PDF reader, notes and restart locally. No Apple membership is needed for this source workflow.
3. Prepare a separately verified Mac binary release. Offer application source and the required dependency source with each binary; preserve build instructions and notices.
4. Add Developer ID signing and notarization when a distributable Mac installer is needed. This does not require an App Store listing. See [Apple's Developer ID overview](https://developer.apple.com/developer-id/).

The current Mac candidate is ad-hoc signed and not notarized. Source licensing and source publication are done, with private vulnerability reporting enabled. Independent-machine validation and a signed Mac binary remain open.
