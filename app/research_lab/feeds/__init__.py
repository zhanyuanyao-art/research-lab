"""Local feed engine behind 首页 · 推送: journal articles (Crossref + OpenAlex) and think-tank commentary
(RSS / Atom), stored in feed_papers / feed_news, refreshed in the background every six hours.

Modules: sources (the _data/feeds.yml contract), papers and news (fetchers), store (reads and status
changes), enrich (the feeds.enrich assistant job), scheduler (background refresh), history (one-time
import of the old newsletter), importer (一键导入 into the library).

Importing this package registers the feeds.enrich job with the assistant queue.
"""
from __future__ import annotations

from . import enrich  # noqa: F401 - registers feeds.enrich
