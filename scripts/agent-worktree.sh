#!/usr/bin/env bash
# One checkout per agent session, so parallel sessions never edit the same working tree.
#
#   scripts/agent-worktree.sh new <name>      # ../research-lab.<name>, branch agent/<name> off main, own .venv
#   scripts/agent-worktree.sh list            # worktrees, their branches and ports
#   scripts/agent-worktree.sh remove <name>   # once agent/<name> is merged (refuses if it has unmerged commits)
#
# What is shared and what is not:
#   code, templates, static, migrations  → the worktree (the branch under development)
#   research content, app/data, app/.env → the main checkout, through the .rl-content-root marker that
#                                           config.find_repo_root honours (one library, one SQLite)
#   port                                  → 4101 + N, printed by `new` and `list`
# Content written by the app (new artifacts, projects.yml edits) therefore lands in the main checkout's
# working tree; commit content there, commit code on the branch, merge the branch into main.
set -euo pipefail
MAIN=$(cd "$(dirname "$0")/.." && pwd)
PARENT=$(dirname "$MAIN")
cmd=${1:-}; name=${2:-}

port_of() { # stable port per name: 4101 + (crc of name mod 80); 4180 is the Mac app's, so that one becomes 4181
  local n p; n=$(printf '%s' "$1" | cksum | cut -d' ' -f1); p=$((4101 + n % 80)); [[ $p == 4180 ]] && p=4181; echo $p; }

case "$cmd" in
  new)
    [[ -n $name && $name =~ ^[a-z0-9][a-z0-9-]*$ ]] || { echo "usage: $0 new <name>  (lowercase, digits, dashes)" >&2; exit 2; }
    WT="$PARENT/research-lab.$name"; BR="agent/$name"
    [[ -e $WT ]] && { echo "$WT already exists" >&2; exit 1; }
    git -C "$MAIN" fetch -q origin main 2>/dev/null || true
    if git -C "$MAIN" show-ref --verify --quiet "refs/heads/$BR"; then
      git -C "$MAIN" worktree add -q "$WT" "$BR"
    else
      git -C "$MAIN" worktree add -q -b "$BR" "$WT" main
    fi
    printf '%s\n' "$MAIN" > "$WT/.rl-content-root"
    (cd "$WT" && uv sync --quiet)
    echo "worktree: $WT  branch: $BR  content: $MAIN"
    echo "run:      cd $WT && uv run rl serve --no-open --port $(port_of "$name")"
    echo "finish:   commit on $BR, then in $MAIN: git merge --ff-only $BR  (or rebase $BR onto main first)"
    ;;
  list)
    git -C "$MAIN" worktree list | while read -r path sha rest; do
      n=$(basename "$path"); n=${n#research-lab.}
      [[ $path == "$MAIN" ]] && echo "$path  $sha $rest  (main checkout, content root, port 4100)" || echo "$path  $sha $rest  port $(port_of "$n")"
    done
    ;;
  remove)
    [[ -n $name ]] || { echo "usage: $0 remove <name>" >&2; exit 2; }
    WT="$PARENT/research-lab.$name"; BR="agent/$name"
    if [[ -n $(git -C "$WT" status --porcelain 2>/dev/null | grep -v " \.rl-content-root$") ]]; then echo "$WT has uncommitted changes; commit or discard them first" >&2; exit 1; fi
    rm -f "$WT/.rl-content-root"
    if ! git -C "$MAIN" merge-base --is-ancestor "$BR" main 2>/dev/null; then echo "$BR has commits not in main; merge it first (or delete the branch yourself)" >&2; exit 1; fi
    git -C "$MAIN" worktree remove "$WT"
    git -C "$MAIN" branch -d "$BR"
    echo "removed $WT and $BR"
    ;;
  *) sed -n '2,16p' "$0"; exit 2 ;;
esac
