# 开发

这里是 Research Lab 的源码，以 AGPL-3.0-or-later 公开。想改自己的副本，用普通的 git 分支和提交就行，也可以让 Claude Code、Codex 这类工具按 [AGENTS.md](../AGENTS.md) 在本地改。

- 改完运行 `TZ=Asia/Shanghai uv run pytest -q app/tests`；界面改动检查浅色、深色和手机宽度。
- 测试只用合成数据，不读你的工作区、数据库或 .env。
- AI 写的东西保持先审阅、后采纳。
- `rl dev` 的卡片、自动修复、发版和安装命令是原维护者的环境用的，按 README 的方式启动时会隐藏。
