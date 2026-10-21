#!/usr/bin/env bash
# Replace /Applications/Research Lab.app with the release build in packaging/macos/dist.noindex,
# quitting the running copy first, then launch it. Explicit on purpose: it deletes the installed app.
# Refuses development builds (--worktree / dirty) unless --force. Afterwards every other copy is
# unregistered from Launch Services, so Launchpad and Spotlight show exactly one Research Lab.
set -euo pipefail
cd "$(dirname "$0")/../.."
SRC="packaging/macos/dist.noindex/Research Lab.app"
[[ -d "$SRC" ]] || SRC="packaging/macos/dist/Research Lab.app"   # builds made before the .noindex move
DEST="/Applications/Research Lab.app"
FORCE=0; [[ ${1:-} == --force ]] && FORCE=1
[[ -d "$SRC" ]] || { echo "no release build — run packaging/macos/build.sh first" >&2; exit 1; }
STAMP=$(cat "$SRC/Contents/Resources/research_lab/web/static/build.json" 2>/dev/null || echo '{}')
if [[ $FORCE == 0 ]] && { [[ $STAMP != *'"mode": "release"'* ]] || [[ $STAMP == *'"dirty": true'* ]]; }; then
  echo "refusing to install a development build ($STAMP); build a release (build.sh [--ref vX.Y.Z]) or pass --force" >&2; exit 1
fi
NEW=$(plutil -extract CFBundleShortVersionString raw "$SRC/Contents/Info.plist")
OLD=$([[ -d "$DEST" ]] && plutil -extract CFBundleShortVersionString raw "$DEST/Contents/Info.plist" || echo none)
osascript -e 'tell application "Research Lab" to quit' >/dev/null 2>&1 || true
for _ in 1 2 3 4 5 6 7 8 9 10; do pgrep -f "Research Lab.app/Contents/MacOS" >/dev/null || break; sleep 1; done
rm -rf "$DEST"
cp -R "$SRC" "$DEST"
echo "installed $NEW over $OLD → $DEST ($(sed -n 's/.*"commit": "\([^"]*\)".*/\1/p' "$DEST/Contents/Resources/research_lab/web/static/build.json" 2>/dev/null || echo 'no stamp'))"
"$(dirname "$0")/../../scripts/app-copies.sh" clean >/dev/null || true
open "$DEST"
