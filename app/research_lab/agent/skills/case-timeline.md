---
name: case-timeline
title: 案例时间轴
description: Build and iteratively revise source-grounded case chronologies, then submit for human review.
summary: 按事件与行动阶段核读案例来源，整理时间、冲突与缺口，审阅后归入研究档案。
mode: task
tier: standard
max_output_tokens: 5500
max_tool_calls: 26
tool_output_chars: 11000
tools: [research_state, search_research, read_research_source, list_papers, list_artifacts, save_research_checkpoint, save_research_draft, ask_user]
triggers: [案例时间轴, 案例时间线, 时间轴, 时间线, 事件年表, 案例检索, 检索案例, 案例梳理, timeline, chronology]
---
Build a source-grounded event chronology in Simplified Chinese through lab tools. It remains a suggested output until the user reviews and adopts it. The timeline is descriptive evidence; causal conclusions and evidence status remain the researcher's decisions.

Restore research_state and continue by next_offset to recover prior scope, exclusions and corrections across engines. Define case boundaries, actors, locations, dates and the event unit. If key scope is missing, ask one focused question after preserving the current checkpoint. Otherwise state a provisional scope and begin.

Iterate according to what is unresolved:
- Search local material, original public documents and configured web search with Chinese/English names and aliases. If web search is unavailable, read known public URLs from existing source registers and clearly log remaining search gaps. Search-service snippets are discovery leads, not full-page reading or independent evidence.
- Read and snapshot original passages with read_research_source; page through long sources. For each event retain actor/recipient, location, task, action stage, event date or range and precision, publication date, reported-by, source and locator. Use unknown when a date or stage is unsupported; never infer a publication year from an upload folder.
- Separate offers, permission, arrival, operational activity and completed delivery. Record announcements as announcements until execution evidence is found. Distinguish firsthand documents, official retrospective accounts, participant reports, journalism and quoted allegations. A source's self-report supports what it reported, not private motivations or an independent impact estimate.
- Reconcile conflicts without silently choosing one date or actor. Group mirrors and repeated accounts into one source lineage. Do not merge different tasks, disasters, sites or funding streams. Preserve excluded material and the specific reason for exclusion.
- Target new retrieval at missing links in the chronology. Present rival sequences and the documents that would distinguish them. Chronological order alone does not prove causation; missing records do not establish refusal or nonparticipation.

Save a Markdown draft with scope; event table (event time and precision, actor/task/stage, observation, publication time, uncertainty); source limitations and conflicts; missing documents; next actions and a short revision account. Citations must refer to actual read_id values and explain which event statement they support. Keep tool ids out of prose; the review page shows the source ledger. Do not invent quotations, fill Claim/personal insights, or change evidence status.

Save checkpoints early and reserve two tool calls for saving. Save working when evidence is insufficient; save review when the source-cited timeline and its limits can be reviewed. End with the returned 审阅产出 link. Budget exhaustion, missing tools or inaccessible sources must remain explicit in the checkpoint.

Apply these reminders only to the corresponding cases and verify them against sources: US–Iran proposal dates and later hearing/publication dates are different fields. In disaster assistance, different grants under one programme are not automatically the same funder; CERF and Chinese funding require separate provenance. Eleven analytical records do not necessarily represent eleven independent cases. Distinguish a bulletin's cover date, a parliamentary answer date, printed page and physical PDF position. Do not carry these examples into unrelated cases as assumed facts.
