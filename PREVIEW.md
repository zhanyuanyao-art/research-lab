# Source preview

This source candidate combines the research workspace with a first-use Coach. It is for local evaluation under AGPL-3.0-or-later and is published at [github.com/zhanyuanyao-art/research-lab](https://github.com/zhanyuanyao-art/research-lab). The exact source revision and file hashes are in `SOURCE-MANIFEST.json`.

## Included

- Create an empty workspace, choose a research direction and project, read a PDF, and save a note with a next step. This loop works without a model or API key.
- New workspaces have their own owner label and automatic direction-based issue numbers. Existing owner aliases remain compatible.
- Search papers, projects, issue numbers, and PDF text from the global Command/Ctrl+K panel.
- Organize issues and sub-issues, reorder work, and delete with Undo.
- Drop documents onto issue descriptions: Office documents become outputs; ebooks and images become attachments; PDFs can link to library records or wait for an explicit import decision.
- New workspaces start with AI paused, empty feed sources, and maintainer autofix disabled.

## Try this without help

1. Follow the README in a new folder, with no existing research workspace.
2. Create a direction and project, import a harmless sample PDF, and open the reader.
3. Return to the Coach, save a note and next task, then find them under the project.
4. Use Command/Ctrl+K to find the new issue by its number or title.
5. Close and restart the app with the same workspace paths. Confirm the project, paper, and task remain.
6. Check that skipping or resuming the guide works, and that missing model access does not prevent reading or organizing work.

Report where you got stuck, what you expected, and what actually happened. Use the bug form with an invented example; never attach your library database or environment file.

## Evaluation limits

The UI is primarily Simplified Chinese. Data analysis is experimental. Scanned PDFs need an existing text layer; OCR is not included. This preview is for one person on a local computer, not a hosted multi-user service.

The implementation was checked on macOS using synthetic workspaces, automated tests, and light/dark/390px browser checks. This is not evidence of testing by an independent user or on another physical machine. GitHub CI runs the test suite on the public repository. Source installation is the primary route. An isolated Mac trial has also been built, but is ad-hoc signed and not notarized; see MAC-TRIAL.md.

The project license, dependency provenance, public repository and private reporting channel are in place. The final native UI check and independent-user verification remain release tasks. See [REVIEW.md](REVIEW.md).

## 选择推送内容

在上手引导、推送页的「Subscriptions（订阅）」或设置的「学科与兴趣」选择论文和新闻。初次打开推荐国际关系，保存前不会添加来源；可以跳过，也可以单独开启一个频道。

首批提供国际关系、政治学、区域研究及六项兴趣：联盟与安全、东亚与东南亚、全球治理、灾害管理、国际政治经济、国际关系理论。共九本期刊、三个新闻源，重叠来源去重；每项可展开查看来源。论文关键词用于现有推荐排序，不是严格过滤，也不会替你新建研究方向。

预设期刊通过已有 Crossref/OpenAlex 流程获取书目信息，全文权限不因订阅改变。新闻源是 BBC World、UN News 和 Fulcrum 的公开 RSS。2026-10-12 已验证三个 RSS 返回有效条目；期刊 ISSN 依据出版方页面核对，地址保存在 `app/research_lab/feeds/presets.py`，没有打包私人订阅配置或新闻全文。

修改在下次刷新生效。既有自定义来源、停用设置和历史条目保留；取消所有选择只移除预设订阅。额外抓取由现有刷新机制执行，保存页面本身不调用模型或网络。第三方来源今后可能更换地址或暂停服务。
