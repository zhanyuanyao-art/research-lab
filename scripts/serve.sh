#!/usr/bin/env bash
# Preview the site locally at http://127.0.0.1:4000/research-lab/
# Uses Jekyll 3.9.5 installed into the user gem dir (see README → Local preview).
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$HOME/.gem/ruby/2.6.0/bin:$PATH"
command -v jekyll >/dev/null || { echo "jekyll not found; see README → Local preview" >&2; exit 1; }
exec jekyll serve --host 127.0.0.1 --port 4000 "$@"
