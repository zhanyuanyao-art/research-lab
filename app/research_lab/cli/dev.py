"""rl dev — the development workflow (see dev/README.md).

  rl dev status                          versions, worktrees, servers, installed app, board
  rl dev card new "标题" --files a,b      planner: a card in dev/cards/
  rl dev claim <id> --as <session>       coder: take a card, get its worktree (refuses file clashes)
  rl dev check <id>                      tester: tests scaled to the change level (L0 小改 → pages only)
  rl dev review <id> pass|changes --as <session> [--notes …]   reviewer (never the card's owner)
  rl dev integrate <id>                  integrator: fast-forward main, push, card → done
  rl dev release 0.5.0                   integrator: bump, tag, build from the tag, install
  rl dev commit "message"                commit dev/ only (cards, reports, RELEASES.md)
  rl dev autofix <id> [--thread T] [--detach]   small bug: fix, check, review, merge, install the next patch
"""
from __future__ import annotations

import os

import typer

from ..devflow import cards, flow, status

app = typer.Typer(help="开发流程：卡片、领取、测试、评审、合并、发版", no_args_is_help=True)
card_app = typer.Typer(help="卡片", no_args_is_help=True)
app.add_typer(card_app, name="card")


def _session(value: str | None) -> str:
    s = value or os.environ.get("RL_SESSION")
    if not s:
        raise typer.BadParameter("用 --as 写明你是哪个会话（或设置 RL_SESSION）")
    return s


def _fail(e: Exception) -> None:
    typer.echo(f"✗ {e}", err=True)
    raise typer.Exit(1)


@app.command("status")
def status_cmd(copies: bool = typer.Option(True, help="检查系统里可启动的程序副本")) -> None:
    """总览：版本、worktree、运行中的服务、已安装的应用、看板。"""
    o = status.overview(include_copies=copies)
    v = o["versions"]
    inst = v["installed"] or {}
    typer.echo(f"版本 {v['version']} · main {v['head']} · 领先 origin {v['ahead_of_origin']} / 落后 {v['behind_origin']} · 最新标签 {v['tag'] or '无'}")
    typer.echo(f"已安装 /Applications：{inst.get('version', '未安装')} {inst.get('commit', '')}"
               + (" · 与 main 一致" if v["installed_current"] else " · 不是 main 的最新版本" if inst else ""))
    if o["lock"]:
        typer.echo(f"集成锁：{o['lock']['holder']} · {o['lock']['purpose']} · {o['lock']['since']}")
    typer.echo("\nworktree：")
    for w in o["worktrees"]:
        d = w["dirty"]
        typer.echo(f"  {w['name']:<28} {w.get('branch', '?'):<28} 端口 {w['port']}"
                   + ("" if w["is_main"] else f" · 领先 main {w['ahead']} / 落后 {w['behind']}")
                   + (" · 未提交：没查到" if d.get("unknown") else "")    # git timed out or errored: not clean
                   + (f" · 未提交代码 {len(d['code'])}" if d["code"] else "") + (f" · 未提交内容 {len(d['content'])}" if d["content"] else ""))
    typer.echo("\n运行中：")
    for s in o["servers"] or [{"port": "-", "kind": "无", "cwd": "", "pid": ""}]:
        typer.echo(f"  {s['port']} {s['kind']} pid {s['pid']} {s['cwd']}")
    if o["copies"] is not None:
        extra = [c for c in o["copies"] if c != str(status.INSTALLED)]
        typer.echo(f"\n可启动的 Research Lab：{len(o['copies'])} 个" + (f"（多余：{', '.join(extra)}；scripts/app-copies.sh clean）" if extra else ""))
    typer.echo("\n看板：")
    for st, items in cards.board().items():
        if items and st not in ("done", "dropped"):
            typer.echo(f"  {cards.LABEL[st]}：" + "；".join(f"{c.id} {c.get('title')}" + (f"（{c.get('owner')}）" if c.get("owner") else "") for c in items))


@card_app.command("new")
def card_new(title: str, files: str = typer.Option("", help="逗号分隔，可用 dir/**"), tier: str = "standard",
             priority: str = "P2", source: str = "", why: str = "", done: str = "",
             level: str = typer.Option(None, help="L0 小改 / L1 普通 / L2 重要；只能比自动判定更高")) -> None:
    """planner：新建卡片。"""
    try:
        cards.check_level(level)
    except cards.CardError as e:
        _fail(e)
    c = cards.new(title, files=[f.strip() for f in files.split(",") if f.strip()], tier=tier, priority=priority,
                  source=source, why=why, done=done, level=level)
    typer.echo(f"卡片 {c.id} · {c.path}")


@card_app.command("list")
def card_list(all_: bool = typer.Option(False, "--all", help="包括已完成和已放弃")) -> None:
    for c in cards.all_cards():
        if all_ or c.status not in ("done", "dropped"):
            typer.echo(f"{c.id} [{cards.LABEL.get(c.status, c.status)}] {c.get('priority')} {c.get('title')}"
                       + (f" · {c.get('owner')}" if c.get("owner") else ""))


@card_app.command("set")
def card_set(card_id: str, status_: str = typer.Option(None, "--status"), files: str = typer.Option(None),
             priority: str = typer.Option(None), owner: str = typer.Option(None), level: str = typer.Option(None)) -> None:
    """改卡片字段（planner/integrator 用）。"""
    try:
        c = cards.get(card_id)
        fields = {k: v for k, v in {"status": status_, "priority": priority, "owner": owner, "level": level}.items() if v is not None}
        if files is not None:
            fields["files"] = [f.strip() for f in files.split(",") if f.strip()]
        cards.update(c, **fields)
        typer.echo(f"卡片 {c.id} 已更新")
    except cards.CardError as e:
        _fail(e)


@app.command("claim")
def claim_cmd(card_id: str, as_: str = typer.Option(None, "--as")) -> None:
    """coder：领取卡片并建好 worktree。"""
    try:
        c = flow.claim(card_id, _session(as_))
        typer.echo(f"卡片 {c.id} → {c.get('owner')} · 分支 {c.get('branch')}\n  cd {c.get('worktree')}")
    except (flow.FlowError, cards.CardError) as e:
        _fail(e)


@app.command("check")
def check_cmd(card_id: str, smoke: bool = typer.Option(True, help="启动该 worktree 的服务检查页面"),
              full: bool = typer.Option(False, help="小改也跑全部单元测试")) -> None:
    """tester：按改动等级测试（小改只查页面），结果写进卡片和 dev/reports/。"""
    try:
        r = flow.check(card_id, smoke=smoke, full=full)
    except (flow.FlowError, cards.CardError) as e:
        _fail(e)
    typer.echo(f"等级：{flow.LEVEL_LABEL[r['level']]}（{'；'.join(r['reasons'])}）"
               + ("，合并前需要另一个会话评审并写清单结论" if r["level"] == "L2" else "，测试通过即可合并，不需要评审"))
    typer.echo(f"{'✓ 通过' if r['ok'] else '✗ 未通过'} · {r['summary']}")
    for line in r["failed"]:
        typer.echo(f"  {line}")
    bad = [f"{p} {c}" for p, c in r["pages"] if c != 200]
    if bad:
        typer.echo("  页面异常：" + "，".join(bad))
    if r["outside"]:
        typer.echo("  超出声明范围：" + ", ".join(r["outside"]))
    typer.echo(f"  报告：{r['report']}")
    if not r["ok"]:
        raise typer.Exit(1)


@app.command("review")
def review_cmd(card_id: str, verdict: str, as_: str = typer.Option(None, "--as"), notes: str = "") -> None:
    """reviewer：记录评审结论（pass / changes）。"""
    try:
        c = flow.review(card_id, verdict, _session(as_), notes)
        typer.echo(f"卡片 {c.id} 评审：{verdict}")
    except (flow.FlowError, cards.CardError) as e:
        _fail(e)


@app.command("integrate")
def integrate_cmd(card_id: str, as_: str = typer.Option("integrator", "--as"), force: bool = False,
                  push: bool = typer.Option(True)) -> None:
    """integrator：快进合并到 main 并推送。"""
    try:
        typer.echo(flow.integrate(card_id, as_, force=force, do_push=push))
        typer.echo(flow.commit_dev(f"dev: card {card_id.zfill(4)} integrated"))
    except (flow.FlowError, cards.CardError, RuntimeError) as e:
        _fail(e)


@app.command("release")
def release_cmd(version: str, as_: str = typer.Option("integrator", "--as"), install: bool = typer.Option(True),
                push: bool = typer.Option(True)) -> None:
    """integrator：改版本号、打标签、从标签打包、安装到 /Applications。"""
    try:
        for step in flow.release(version, as_, install=install, do_push=push):
            typer.echo(f"  {step}")
    except (flow.FlowError, RuntimeError) as e:
        _fail(e)


@app.command("commit")
def commit_cmd(message: str) -> None:
    """只提交 dev/（卡片、报告、版本记录）。"""
    try:
        typer.echo(flow.commit_dev(message))
    except RuntimeError as e:
        _fail(e)


@app.command("autofix")
def autofix_cmd(card_id: str, thread: str = typer.Option(None, "--thread", help="报 bug 的对话，进度写回这里"),
                detach: bool = typer.Option(False, "--detach", help="在独立进程里跑并立刻返回（应用里触发时用）")) -> None:
    """小 bug 直修：在单独的 worktree 里修、硬性检查、全套测试、只读评审，都通过就合并并装下一个补丁版。"""
    from ..devflow import autofix
    try:
        if detach:
            pid, log = autofix.spawn(card_id, thread)
            typer.echo(f"已在独立进程里开始（pid {pid}）· 输出 {log}")
            return
        autofix.stop_on_sigterm()
        r = autofix.run(card_id, thread=thread)
    except (autofix.Disabled, flow.FlowError, cards.CardError) as e:
        _fail(e)
    label = {"installed": f"✓ 已合并并装好 v{r.version}", "handed_off": "✗ 已转交集成会话", "refused": "✗ 没有运行"}[r.status]
    typer.echo(label + (f"：{r.reason}" if r.reason else ""))
    if r.report:
        typer.echo(f"  记录：{r.report}")
    if r.status != "installed":
        raise typer.Exit(1)
