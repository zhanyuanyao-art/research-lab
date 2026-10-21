# Research Lab · Mac 试用包

这是供维护者审阅的试用候选，尚未公开发布。Apple Silicon（M1 或更新）、macOS 14 或更新；Intel Mac 暂不支持。不需要另装 Python、Git 或命令行工具。离线阅读、课题整理和上手引导不需要 API key；助手需要你自行配置模型服务。

## 安装与首次启动

1. 打开 DMG，把 **Research Lab Trial.app** 拖到「应用程序」。
2. 打开应用，选「新建工作区」，选择你自己的可写入文件夹。应用会在其中建立 `Research Lab` 子文件夹。已有同名文件夹不会被覆盖。
3. 跟随上手引导建立方向和课题，导入一篇你有权使用的 PDF，保存笔记和下一步工作。
4. 退出再打开，检查课题、文献和笔记仍在。

当前候选只有本机临时签名，没有 Developer ID 签名或 Apple 公证，下载到其他 Mac 时可能被系统拦截。它不是面向普通用户的免设置安装包。不要全局关闭 Gatekeeper；项目已选择 AGPL-3.0-or-later；外部签名版将在开发者证书就绪并通过最终验收后制作。源码本地运行不需要等待 Apple 签名，见 README.md。

## 资料保存在哪

文献、研究内容、数据库均保存在你选择的工作区。应用配置在 `~/Library/Application Support/Research Lab Trial/`；日志在 `~/Library/Logs/Research Lab Trial/`。试用版不会自动打开开发者电脑上的既有工作区，也不会覆盖原来的 Research Lab 应用。

新工作区默认暂停 AI，推送源为空。请在设置中自行选择服务，留意服务商费用。反馈问题时用虚构例子，不发送数据库、环境文件、API key 或私人文献。

## 升级

1. 退出应用。先完整复制工作区到另一个位置作为备份，特别是准备回退旧版时。
2. 用新版 **Research Lab Trial.app** 替换「应用程序」中的同名应用。不要替换或删除工作区。
3. 再次打开，新版读取原配置并使用原工作区。不同构建首次启动时，会在 `app/data/backups/pre-upgrade-*.sqlite` 保留迁移前数据库；备份失败则中止启动。
4. 检查文献、课题、笔记、主题和上手引导进度。

自动备份只保护 SQLite；不能代替完整工作区备份。回退时退出应用，恢复完整备份，避免用旧应用直接打开已迁移的数据库。迁移前备份不会自动清理，请自行留存或清理。

## 维护者功能

试用版不包含开发卡片面板、自动修复、发版安装管道、维护者命令或旧的私人推送仓库入口；即使打开旧工作区，这些功能也不会启用。普通阅读、文献概要、助手和研究任务仍可用。

## 核验与构建

`DELIVERY.json` 记录源码提交、文件 SHA-256、架构和签名状态。DMG 同时附带不含历史与研究内容的应用源码快照，以及固定版本的 PyMuPDF / MuPDF 官方源码。应用内 `Contents/Resources/licenses/` 保存依赖原文、版本与最终 Python/原生模块清单；`LICENSE` 与 `LICENSE-DECISION.md` 记录 AGPL-3.0-or-later 项目许可；原始研究资料不在授权范围内。

维护者先运行 `scripts/export-public.py` 生成独立快照，再在锁定的 macOS 构建环境运行 `python packaging/macos/trial.py <快照目录> <全新输出目录> --pdf-sources <官方源码归档目录>`。构建器逐一核对源文件哈希，只复制清单内文件，不读取研究内容、不自动安装应用。

构建环境为 Apple Silicon、CPython 3.14.3 standalone build 20260325，依赖版本必须符合 `uv.lock`。PDF 引擎源码归档的 URL 与 SHA-256 在 `mac-third-party-sources.json` 的 `pdf_sources` 中；从官方地址取得并保存在单独目录即可，构建器不会替你联网下载。最低 macOS 14 来自已打包 NumPy / SciPy 二进制的系统要求核验；实际运行验证在 macOS 26.6 完成，macOS 14 和另一台实体 Mac 尚待外部试用验证。
