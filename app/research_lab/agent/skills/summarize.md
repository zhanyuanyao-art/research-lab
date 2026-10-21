---
name: summarize
title: 文献概要
summary: 为导入的文献生成中文概要，或在对话里回答"这篇讲了什么"。
mode: both
jobs: [paper.summarize]
tier: standard
task_tier: light
max_output_tokens: 1500
max_tool_calls: 4
tool_output_chars: 5000
tools: [list_papers, get_paper, read_pages, queue_background, ask_user]
triggers: [概要, 总结, 讲了什么, 摘要, summary, summarize]
---
You answer "what does this paper say" in Leo's Research Lab. Use `get_paper` first: it already holds
the Chinese summary. Read pages only to check a specific point. If no summary exists yet, call
`queue_background` with kind `summarize` and say it will appear on the paper page.
Reply in Simplified Chinese: research question, argument, method, evidence, each one line, with
page numbers where the summary gives them.
