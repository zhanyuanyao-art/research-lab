"""Card 0203 (Leo 10-21: 「改成一个框，像对话框里的 checking、运行中，一直弹动，包含时间和箭头，点击箭头弹出完整的」):
一键跑完 is one line in a .jobline box. Running, it pulses and carries the start time for the live clock; finished, it
says what came of it and how long it took; the arrow opens the nine steps; 需要你 stays outside the box."""
from __future__ import annotations

import time

from research_lab.replication import pipeline
from research_lab.web.routes.run_all import timing, took

from .test_pipeline import fakes, plan  # noqa: F401


def test_the_clock_reads_like_speech():
    assert [took(s) for s in (0, 12, 60, 185, 3600, 3725)] == ["0 秒", "12 秒", "1 分", "3 分 5 秒", "1 小时 0 分", "1 小时 2 分"]
    assert took(None) == "" and took(-3) == ""


def test_timing_gives_the_run_and_every_step_a_duration():
    stamp = lambda t: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))  # noqa: E731
    t0 = time.time() - 600
    run = timing({"started": stamp(t0), "ended": stamp(t0 + 185), "running": False, "current": None, "steps": [
        {"key": "fetch", "state": "done", "started": stamp(t0), "ended": stamp(t0 + 12)},
        {"key": "scan", "state": "skipped", "started": None, "ended": stamp(t0 + 12)},
        {"key": "run", "state": "running", "started": stamp(t0 + 12), "ended": None}]})
    assert run["took"] == "3 分 5 秒" and run["since"] == int(t0) and run["at"] is None
    assert [s["took"] for s in run["steps"]][:2] == ["12 秒", ""] and run["steps"][2]["took"].endswith("秒")
    assert timing({"started": "garbled", "steps": [], "current": None})["took"] == ""


def test_a_finished_run_is_one_line_that_opens_to_the_steps(client, conn, plan, fakes):  # noqa: F811
    pipeline.run_now(conn, plan)
    html = client.get(f"/data/plans/{plan}/pipeline").text
    box = html.split('<details class="jobline" id="run-all-box"')[1].split("</details>")[0]
    assert 'data-tone="warn"' in box and "件事需要你" in box and "jobline-chev" in box and "jobline-time" in box
    assert box.index("</summary>") < box.index('<ol class="jobline-steps">') and box.count("data-step=") == 9
    assert "需要你</h4>" in html.split("</details>")[1] and "brew install cmake" in html.split("</details>")[1]
    assert "every 3s" not in html and "data-since" not in html


def test_a_running_box_pulses_and_carries_its_clock(client, conn, plan):  # noqa: F811
    run = pipeline._Run(plan, pipeline._root(plan))
    hold = pipeline._Hold(pipeline._root(plan))
    assert hold.take()
    try:
        run.begin("install")
        html = client.get(f"/data/plans/{plan}/pipeline?poll=1").text
    finally:
        hold.release()
        run.finish()
    head = html.split("</summary>")[0]
    assert 'data-tone="running"' in head and "准备环境 · 第 4/9 步" in head and "data-since=" in head
    assert 'data-step="install" data-state="running"' in html and "data-step-since=" in html
