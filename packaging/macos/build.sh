#!/usr/bin/env bash
# Build Research Lab.app and a DMG into packaging/macos/dist.noindex.
#
#   packaging/macos/build.sh                 # release build of HEAD (committed code only) → app + dmg
#   packaging/macos/build.sh --ref v0.4.0    # release build of a tag / commit
#   packaging/macos/build.sh --worktree      # build the working tree as it is (uncommitted edits included)
#   packaging/macos/build.sh --app-only      # skip the DMG (combinable)
#
# A release build exports the chosen commit with `git archive` and builds from that copy, so edits that
# other sessions have not committed never end up in a shipped app. Every build stamps
# app/research_lab/web/static/build.json (version, commit, date, branch, dirty) into the bundle; the app
# shows it in 设置 → 应用 and in the sidebar brand tooltip. Outputs are named by version and commit
# (ResearchLab-0.4.0-1568bb0.dmg) and listed in dist.noindex/BUILDS.log; the three newest DMGs are kept.
# Installing into /Applications is a separate, explicit step: packaging/macos/install.sh.
#
# Only /Applications/Research Lab.app should ever be a launchable app. Outputs live in *.noindex
# folders (Spotlight skips them), the built bundle is unregistered from Launch Services right after it
# is signed, the DMG staging copy is deleted, and a --worktree build is named "Research Lab Dev".
set -euo pipefail
cd "$(dirname "$0")/../.."
REPO=$(pwd)
HERE=packaging/macos
DIST=$HERE/dist.noindex
BUILD=$HERE/build.noindex
LSREGISTER=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister

REF=HEAD; MODE=release; APP_ONLY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --ref) REF=$2; shift 2 ;;
    --worktree) MODE=worktree; shift ;;
    --app-only) APP_ONLY=1; shift ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done

if [[ $MODE == worktree ]]; then
  export RL_APP_NAME="Research Lab Dev" RL_BUNDLE_ID="io.github.leoyao0701.researchlab.dev"
fi
APP="$DIST/${RL_APP_NAME:-Research Lab}.app"

if [[ $MODE == release ]]; then
  SRC=$BUILD/src
  rm -rf "$SRC" && mkdir -p "$SRC"
  git archive --format=tar "$REF" | tar -x -C "$SRC"
  COMMIT=$(git rev-parse --short "$REF")
  DATE=$(git log -1 --format=%cs "$REF")
  BRANCH=$(git rev-parse --abbrev-ref "$REF" 2>/dev/null || echo "$REF")
  DIRTY=false
  SPEC="$SRC/$HERE/ResearchLab.spec"
else
  SRC=$REPO
  COMMIT=$(git rev-parse --short HEAD)
  DATE=$(date +%F)
  BRANCH=$(git rev-parse --abbrev-ref HEAD)
  DIRTY=$([[ -n $(git status --porcelain --untracked-files=no) ]] && echo true || echo false)
  SPEC="$HERE/ResearchLab.spec"
fi
VERSION=$(sed -n 's/^version = "\(.*\)"/\1/p' "$SRC/pyproject.toml" | head -1)
TAG="$VERSION-$COMMIT"; [[ $DIRTY == true ]] && TAG="$TAG-dirty"
echo "build: $MODE $REF → $TAG ($BRANCH, $DATE)"

uv sync --quiet
uv run python $HERE/make_icon.py >/dev/null
mkdir -p "$SRC/$HERE/build" && cp "$HERE/build/icon.icns" "$SRC/$HERE/build/icon.icns" 2>/dev/null || true
printf '{"version": "%s", "commit": "%s", "date": "%s", "branch": "%s", "dirty": %s, "mode": "%s"}\n' \
  "$VERSION" "$COMMIT" "$DATE" "$BRANCH" "$DIRTY" "$MODE" > "$SRC/app/research_lab/web/static/build.json"

# PYTHONPATH puts the exported package ahead of the editable install, so a release build never reads
# the working tree even though the spec's hooks import research_lab to collect modules and data.
PYTHONPATH="$SRC/app" uv run pyinstaller --noconfirm --clean --log-level WARN "$SPEC" --distpath "$DIST" --workpath "$BUILD/pyinstaller"
[[ $MODE == worktree ]] && rm -f "app/research_lab/web/static/build.json"

# PyInstaller signs each binary ad hoc; sign the bundle as a whole so macOS accepts it as one app.
codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict "$APP"
"$LSREGISTER" -u "$APP" 2>/dev/null || true  # a build output is not an installed app
BUILT=$(plutil -extract CFBundleShortVersionString raw "$APP/Contents/Info.plist")
[[ $BUILT == "$VERSION" ]] || { echo "bundle version $BUILT != $VERSION" >&2; exit 1; }

if [[ $APP_ONLY == 0 ]]; then
  STAGE=$BUILD/dmg
  rm -rf "$STAGE" && mkdir -p "$STAGE"
  cp -R "$APP" "$STAGE/"
  ln -s /Applications "$STAGE/Applications"
  DMG="$DIST/ResearchLab-$TAG.dmg"
  rm -f "$DMG"
  hdiutil create -quiet -volname "Research Lab" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
  "$LSREGISTER" -u "$STAGE/$(basename "$APP")" 2>/dev/null || true
  rm -rf "$STAGE"
  ls -t "$DIST"/ResearchLab-*.dmg | tail -n +4 | xargs -I{} rm -f {}
  echo "dmg: $DMG ($(du -h "$DMG" | cut -f1))"
fi
echo "$(date '+%F %T')  $TAG  $MODE  $BRANCH" >> "$DIST/BUILDS.log"
echo "app: $APP ($(du -sh "$APP" | cut -f1))  build.json: $(cat "$SRC/app/research_lab/web/static/build.json")"
