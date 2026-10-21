#!/usr/bin/env bash
# Keep exactly one launchable Research Lab: /Applications/Research Lab.app.
#
#   scripts/app-copies.sh          # list registered copies and bundles found on disk
#   scripts/app-copies.sh clean    # unregister every copy except /Applications from Launch Services
#
# Unregistering is reversible (opening a copy registers it again) and deletes nothing. Build outputs,
# DMG volumes, worktree builds and other tools' builds (e.g. a Codex workspace) are what pile up.
set -euo pipefail
LSREGISTER=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
KEEP="/Applications/Research Lab.app"
registered() { "$LSREGISTER" -dump 2>/dev/null | sed -n 's/^path: *\(.*Research Lab[^/]*\.app\) (0x[0-9a-f]*)$/\1/p' | sort -u; }
case "${1:-list}" in
  list)
    echo "registered with Launch Services:"; registered | sed 's/^/  /'
    echo "bundles Spotlight knows:"; mdfind 'kMDItemCFBundleIdentifier == "io.github.leoyao0701.researchlab*"' 2>/dev/null | sed 's/^/  /'
    ;;
  clean)
    registered | while IFS= read -r p; do
      [[ $p == "$KEEP" ]] && continue
      for _ in 1 2 3; do "$LSREGISTER" -u "$p" 2>/dev/null || true; done
      echo "unregistered: $p"
    done
    [[ -d $KEEP ]] && "$LSREGISTER" -f "$KEEP"
    ;;
  *) sed -n '2,9p' "$0"; exit 2 ;;
esac
