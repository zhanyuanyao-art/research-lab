#!/usr/bin/env bash
# Create this week's artifact from the template.
#   scripts/new-artifact.sh <type> <slug> [project]
#   scripts/new-artifact.sh puzzle asean-centrality-persistence asean-centrality
# Computes the ISO week and the next sequence number within that week.
set -euo pipefail
cd "$(dirname "$0")/.."
type="${1:?type (see _data/types.yml)}"
slug="${2:?slug for the filename}"
project="${3:-$(ruby -ryaml -e 'puts YAML.load_file("_data/now.yml")["project"]')}"
year=$(date +%G); week=$(date +%V); today=$(date +%F)
n=$(ls _artifacts/${year}-W${week}-*.md 2>/dev/null | wc -l | tr -d ' ')
seq=$(printf '%02d' $((n + 1)))
id="ART-${year}-${week}${seq}"
file="_artifacts/${year}-W${week}-${slug}.md"
[ -e "$file" ] && { echo "exists: $file" >&2; exit 1; }
sed -e "s/^ref: .*/ref: ${id}/" \
    -e "s/^week: .*/week: ${year}-W${week}/" \
    -e "s/^type: .*/type: ${type}/" \
    -e "s/^project: .*/project: ${project}/" \
    -e "s/^created: .*/created: ${today}/" \
    templates/artifact.md > "$file"
echo "$file  ($id)"
