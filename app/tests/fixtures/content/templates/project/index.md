---
title: "Project: <Name>"
---

# <Project name>

<p class="kicker">stage: <stage> · since <YYYY-MM></p>

## Question

...

## Why it is interesting

...

## Current answer (one paragraph, revised as artifacts accumulate)

...

## Files

- [literature](literature/) — the reading, by camp
- [evidence](evidence/) — cases, events, data, with sources
- `drafts/` — paper-ready artifacts assembled into sections (full manuscripts go to a private repo)

## Artifacts

{% assign items = site.artifacts | where: "project", page.slug | sort: "ref" | reverse %}
{% include artifact-list.html items=items %}
