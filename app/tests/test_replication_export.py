"""Card 0191 (REPLICATION-v4-DCL.md §3E): the data folder export. A plan's ticked 复现清单 items become one directory per group
(code / data / output), a README that ties each item to its script, output and verdict, and a zip saved in the paper's 课题
drafts/ and attached to the issue that names the paper. Nothing is written unless Leo posts; the author's text is untrusted."""
from __future__ import annotations

import json
import os
import re
import time
import zipfile
from datetime import datetime
from pathlib import Path

import pytest

from research_lab.config import get_paths
from research_lab.files import issues as I
from research_lab.replication import datasets, export, plans, workspace as W
from ._timing import SLOW

SLUG = "us-rok-reassurance"                 # direction alliance-us (the fixture's projects.yml)
OTHER_SLUG = "asean-centrality"             # direction sea-asean
CSV = b"country,year,y\nA,2010,1.5\nB,2011,2.5\nC,2012,0.5\n"
SECRET = "TOP-SECRET-NOT-IN-THE-PACKAGE"

MAIN_R = '''
source("./code/helper.R")
d <- fread("./data/meetingData.csv")
fit <- lm(y ~ x, data = d)
ggsave(filename="./figures/coefPlotMain.pdf")
'''
HELPER_R = 'norm <- function(x) x / sum(x)\n'
ROBUST_R = 'p <- read.csv("./data/panel.csv")\nfit <- lm(y ~ x, data = p)\n'
ROBUST2_R = 'r <- read_dta("./data/restricted_survey.dta")\nfit <- lm(y ~ x, data = r)\n'

ROWS = [   # kind, label, caption, group, scripts, outputs, ticked
    ("table", "Table 3", "Effect of participant hawkishness", "main", ["code/main.R"], [], 1),
    ("figure", "Figure 7", "Summary of the three models", "main", ["code/main.R"], ["figures/coefPlotMain.pdf"], 1),
    ("table", "Table 9", "A table without any script", "main", [], [], 1),
    ("table", "Table A1", "Robustness: dropping the Soviet Union", "robust", ["code/robust.R"], [], 1),
    ("table", "Table A2", "Using the restricted survey instead", "robust", ["code/robust2.R"], [], 1),
    ("table", "Table A3", "Other appendix table nobody ticked", "other", ["code/robust.R"], [], 0),
]


def _paper(conn, pid="jost2024", title="Advisers and Aggregation", slug=SLUG):
    now = datetime.now().isoformat()
    conn.execute("INSERT INTO papers(id, title, title_hash, authors_json, year, venue, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                 (pid, title, pid, '["Jost, Tyler"]', 2024, "IO", now, now))
    if slug:
        conn.execute("INSERT INTO paper_projects(paper_id, project_slug) VALUES (?,?)", (pid, slug))
    conn.commit()
    return pid


def _put(path: Path, text: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))


def _dataset(conn, plan_id, tmp_path, name):
    src = tmp_path / "src" / name
    _put(src, CSV)
    ds = datasets.import_file(conn, src, original_name=name)
    plans.attach_dataset(conn, plan_id, ds)
    return ds


def _rows(conn, plan_id, rows=ROWS):
    now = datetime.now().isoformat()
    for pos, (kind, label, caption, group, scripts, outputs, ticked) in enumerate(rows):
        conn.execute("INSERT INTO replication_coverage(plan_id, item_key, source, kind, label, caption, page, section, grp, position, "
                     "scripts_json, outputs_json, ticked, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (plan_id, f"{'main' if group == 'main' else 'appendix'}:{label}", "main" if group == "main" else "appendix", kind,
                      label, caption, "5", "", group, pos,
                      json.dumps([{"path": Path(s).name, "full": s, "how": "comment", "ref": "x", "via": "", "have": True} for s in scripts]),
                      json.dumps(outputs), ticked, now, now))
    conn.commit()


def _workspace(plan_id):
    ws = W.workspace_dir(plan_id)
    for rel, text in {"code/main.R": MAIN_R, "code/helper.R": HELPER_R, "code/robust.R": ROBUST_R, "code/robust2.R": ROBUST2_R}.items():
        _put(ws / "author" / rel, text)
        _put(ws / "rrun" / rel, text)
    _put(ws / "rrun" / "code" / "main.R", MAIN_R + 'cat("patched path\\n")\n')            # the copy that ran differs
    tables = [{"label": "Table 3", "model": "(1)", "rows": [{"term": "x", "estimate": 1.0}]},
              {"label": "Table A1", "model": "(1)", "rows": [{"term": "x", "estimate": 2.0}]},
              {"label": "Table A3", "model": "(1)", "rows": [{"term": "x", "estimate": 3.0}]}]
    _put(ws / "outputs" / "results.json", json.dumps({"tables": tables}))
    _put(ws / "outputs" / "tables" / "main-1-Table_3.tex", "\\begin{table}3\\end{table}")
    _put(ws / "outputs" / "tables" / "robust-1-Table_A1.tex", "\\begin{table}A1\\end{table}")
    _put(ws / "outputs" / "tables" / "robust-2-Table_A3.tex", "\\begin{table}A3\\end{table}")
    _put(ws / "outputs" / "figures" / "coefPlotMain.pdf", b"%PDF-1.4 main")
    _put(ws / "outputs" / "figures" / "stray.png", b"png")
    _put(ws / "outputs" / "Rplots.pdf", b"%PDF-1.4 junk")
    _put(ws / "outputs" / "changes.md", "# 对作者代码的改动\n\n在 /Users/someone/work 里改了路径。\n")
    _put(ws / "logs" / "rinstall.json", json.dumps({"r": "4.6.1", "wanted": ["dplyr", "lme4"], "failed": {"lme4": "需要 cmake"},
                                                   "notes": ["stargazer 改过两行"]}))
    _put(ws / "rlib" / "dplyr" / "DESCRIPTION", "Package: dplyr\nVersion: 1.2.1\n")
    _put(ws / "rlib" / "R6" / "DESCRIPTION", "Package: R6\nVersion: 2.6.1\n")
    return ws


def _comparison(conn, plan_id):
    """A real compare.compare() of three targets against the run's tables, kept with the run as step 4 keeps it."""
    from research_lab.replication import compare
    tables = [{"label": "Table 3", "model": "(1)", "rows": [{"term": "x", "estimate": 1.0, "se": 0.1}, {"term": "z", "estimate": 5.0, "se": 0.1}]},
              {"label": "Table A1", "model": "(1)", "rows": [{"term": "x", "estimate": 2.0, "se": 0.1}]}]
    targets = [{"id": "T1", "table": "Table 3", "model": "(1)", "term": "x", "value_text": "1.00"},
               {"id": "T2", "table": "Table 3", "model": "(1)", "term": "z", "value_text": "-5.00"},
               {"id": "T3", "table": "Table A1", "model": "(1)", "term": "x", "value_text": "-9.00"}]
    cmp = {"run_id": "RUN-2026-0001", "at": "2026-10-18T10:00:00", **compare.compare(targets, {"tables": tables})}
    conn.execute("INSERT INTO analysis_runs(id, plan_id, method, params_json, result_json, status, workspace_json, tokens, created_at) "
                 "VALUES (?,?,?,?,?,?,?,?,?)", ("RUN-2026-0001", plan_id, "workspace", "{}", "{}", "done",
                                               json.dumps({"comparison": cmp}), 0, datetime.now().isoformat()))
    conn.commit()
    return {x["id"]: x["verdict"] for x in cmp["items"]}


verdicts: dict[str, dict[str, str]] = {}


@pytest.fixture
def plan(conn, tmp_path):
    paper = _paper(conn)
    plan_id = plans.create_blank(conn, paper, "alliance-us")
    _dataset(conn, plan_id, tmp_path, "meetingData.csv")
    _dataset(conn, plan_id, tmp_path, "panel.csv")
    _rows(conn, plan_id)
    _workspace(plan_id)
    verdicts[plan_id] = _comparison(conn, plan_id)
    return plan_id


def _tree(root: Path) -> dict[str, int]:
    return {p.relative_to(root).as_posix(): p.stat().st_size for p in sorted(root.rglob("*")) if p.is_file()}


def _issue(conn, slug=SLUG, paper="jost2024", title="复现 Jost"):
    ref = I.create(title, slug, "", I.revision())
    I.set_material(conn, ref, f"paper:{paper}", True, I.revision(ref))
    return ref


def _attachments(ref):
    return [x["rel"] for x in I.attachments(ref)]


def _drafts(slug=SLUG) -> list[str]:
    d = get_paths().projects / slug / "drafts"
    return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


# ---------------------------------------------------------------- the folder

def test_one_directory_per_group_with_ticked_items_and_code_data_output_in_each(conn, plan, tmp_path):
    built = export.build_folder(conn, plan, tmp_path / "out" / f"{plan}-replication")
    tree = _tree(built.root)
    assert set(built.groups) == {"正文结果", "附录稳健性"}                    # 附录其他 has nothing ticked: no directory
    assert not (built.root / "附录其他").exists()
    top = {p.split("/")[0] for p in tree}
    assert top == {"正文结果", "附录稳健性", "README.md", "R-packages.txt", "其他输出"}
    # code: the authors' scripts as saved, the helper they source, the copy that ran where it differs, changes.md
    assert {"正文结果/code/main.R", "正文结果/code/helper.R", "正文结果/code/modified/main.R", "正文结果/code/changes.md"} <= set(tree)
    assert "正文结果/code/modified/helper.R" not in tree                       # identical to the author's file
    assert (built.root / "正文结果/code/main.R").read_text() == MAIN_R and "patched path" in (built.root / "正文结果/code/modified/main.R").read_text()
    assert {"附录稳健性/code/robust.R", "附录稳健性/code/robust2.R"} <= set(tree)
    assert "附录稳健性/code/main.R" not in tree
    # data: what each group's scripts read, as imported
    assert (built.root / "正文结果/data/meetingData.csv").read_bytes() == CSV
    assert (built.root / "附录稳健性/data/panel.csv").read_bytes() == CSV
    assert "附录稳健性/data/meetingData.csv" not in tree and "正文结果/data/panel.csv" not in tree
    # output: results.json slices, the stargazer text and the figure of each group's items only
    assert [t["label"] for t in json.loads((built.root / "正文结果/output/results.json").read_text())["tables"]] == ["Table 3"]
    assert [t["label"] for t in json.loads((built.root / "附录稳健性/output/results.json").read_text())["tables"]] == ["Table A1"]
    assert "正文结果/output/tables/main-1-Table_3.tex" in tree and "附录稳健性/output/tables/robust-1-Table_A1.tex" in tree
    assert "正文结果/output/figures/coefPlotMain.pdf" in tree
    assert not [p for p in tree if "Table_A3" in p or "Rplots" in p]            # an unticked item's table and R's junk stay out
    assert "其他输出/figures/stray.png" in tree                                 # no group claims it: kept, apart
    assert built.groups["正文结果"] == {"code": 4, "data": 1, "output": 3} and built.groups["附录稳健性"] == {"code": 3, "data": 1, "output": 2}


def test_the_readme_ties_items_to_scripts_outputs_verdicts_and_says_what_is_open(conn, plan, tmp_path):
    built = export.build_folder(conn, plan, tmp_path / "out" / "f")
    text = (built.root / "README.md").read_text()
    row = next(line for line in text.splitlines() if line.startswith("| Table 3 "))
    assert "正文结果/code/main.R" in row and "正文结果/output/results.json" in row and "main-1-Table_3.tex" in row
    v = verdicts[plan]
    labels = {"exact": "完全一致", "within": "在误差范围内", "direction": "方向一致但大小或显著性不同", "mismatch": "对不上", "missing": "没找到对应输出"}
    assert len(set(v.values())) >= 2                                              # the fixture compares to different verdicts
    assert all(f"{labels[v[t]]} 1" in row or f"{labels[v[t]]} 2" in row for t in ("T1", "T2")) and row.rstrip().endswith("已复现 |")
    fig = next(line for line in text.splitlines() if line.startswith("| Figure 7 "))
    assert "coefPlotMain.pdf" in fig and "未对照" in fig                          # no comparison row for a figure
    assert f"{labels[v['T3']]} 1" in next(line for line in text.splitlines() if line.startswith("| Table A1 "))
    assert "### 尚未复现" in text and "Table 9" in text and "没有找到对应的作者脚本" in text
    assert "### 等数据" in text and "Table A2" in text and "restricted_survey.dta" in text      # its script reads a file nobody holds
    assert "待补数据文件" in text and "`data/restricted_survey.dta`" in text
    assert "R 版本：4.6.1" in text and "| dplyr | 1.2.1 |" in text and "| lme4 | 没装上：需要 cmake |" in text
    assert "stargazer 改过两行" in text
    assert "cd 正文结果" in text and "Rscript code/main.R" in text and "Install R packages" in text
    assert "Table A3" not in text.split("## 还没有复现的")[0]                    # unticked: counted, not listed
    assert "另有 1 项没有勾选" in text
    assert "/Users/someone" not in text and "/Users/someone" not in (built.root / "正文结果/code/changes.md").read_text()
    assert (built.root / "R-packages.txt").read_text().splitlines() == ["R 4.6.1", "R6==2.6.1", "dplyr==1.2.1"]
    assert built.status == {"已复现": 3, "等数据": 1, "尚未复现": 1}


def test_a_plan_that_never_ran_r_lists_the_python_environment(conn, tmp_path):
    paper = _paper(conn)
    plan_id = plans.create_blank(conn, paper, "alliance-us")
    _dataset(conn, plan_id, tmp_path, "meetingData.csv")
    _rows(conn, plan_id, [("table", "Table 1", "Main table", "main", [], [], 1)])
    ws = W.workspace_dir(plan_id)
    _put(ws / "scripts" / "analysis.py", "import pandas\n")
    _put(ws / "outputs" / "results.json", json.dumps({"tables": [{"label": "Table 1", "model": "(1)", "rows": []}]}))
    _put(ws / "outputs" / "figures" / "fig1.png", b"png")
    built = export.build_folder(conn, plan_id, tmp_path / "out" / "py")
    tree = _tree(built.root)
    assert "R-packages.txt" not in tree and "正文结果/code/python/analysis.py" in tree
    assert "正文结果/output/figures/fig1.png" in tree and "正文结果/data/meetingData.csv" in tree     # the AI's files go to the first group
    text = (built.root / "README.md").read_text()
    assert "复现用的是 Python 3." in text and "pandas" in text and "Re-run" in text


def test_a_dataset_without_its_original_file_is_exported_as_csv(conn, plan, tmp_path):
    ds = plans.get(conn, plan)["plan"]["datasets"][0]
    datasets.original_path(datasets.get(conn, ds)).unlink()
    built = export.build_folder(conn, plan, tmp_path / "out" / "csv")
    assert "正文结果/data/meetingData.csv" in _tree(built.root)
    assert b"country,year,y" in (built.root / "正文结果/data/meetingData.csv").read_bytes()


def test_nothing_ticked_or_an_existing_destination_is_refused(conn, plan, tmp_path):
    conn.execute("UPDATE replication_coverage SET ticked = 0")
    conn.commit()
    with pytest.raises(export.ExportError, match="还没有勾选"):
        export.build_folder(conn, plan, tmp_path / "out" / "none")
    assert not (tmp_path / "out" / "none").exists()
    conn.execute("UPDATE replication_coverage SET ticked = 1 WHERE grp = 'main'")
    conn.commit()
    taken = tmp_path / "taken"
    _put(taken / "mine.txt", "keep")
    with pytest.raises(export.ExportError, match="已经存在"):
        export.build_folder(conn, plan, taken)
    assert (taken / "mine.txt").read_text() == "keep"


# ---------------------------------------------------------------- links, odd names, caps

def test_links_are_never_followed_and_nothing_outside_the_workspace_lands_in_the_folder(conn, plan, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text(SECRET)
    outside = tmp_path / "outside"
    _put(outside / "evil.R", SECRET)
    ws = W.workspace_dir(plan)
    os.symlink(secret, ws / "author" / "code" / "linked.R")                 # a script that is a link
    os.symlink(outside, ws / "author" / "linkdir")                          # a folder that is a link
    os.symlink(secret, ws / "outputs" / "leak.pdf")
    os.symlink(secret, ws / "outputs" / "tables" / "main-9-Table_3.tex")
    os.link(secret, ws / "outputs" / "figures" / "hard.png")                 # a second hard link
    ds = datasets.get(conn, plans.get(conn, plan)["plan"]["datasets"][0])
    orig = datasets.original_path(ds)
    orig.unlink()
    os.symlink(secret, orig)                                                # the dataset's original file is a link
    _rows(conn, plan, [("table", "Table 11", "Linked script", "main", ["code/linked.R", "linkdir/evil.R"], ["figures/hard.png"], 1)])
    built = export.build_folder(conn, plan, tmp_path / "out" / "links")
    for p in built.root.rglob("*"):
        assert not p.is_symlink()
        if p.is_file():
            assert SECRET not in p.read_text(errors="ignore"), p
    assert "正文结果/data/meetingData.csv" in _tree(built.root)              # fell back to the library's CSV, not the link
    text = (built.root / "README.md").read_text()
    assert "linked.R（未保存）" in text and "evil.R（未保存）" in text         # a script that is a link counts as not saved


def test_names_from_the_authors_text_are_cleaned_before_they_are_written(conn, plan, tmp_path):
    ws = W.workspace_dir(plan)
    nasty = "evil‮txt\n.R"
    _put(ws / "author" / "code" / nasty, "x <- 1\n")
    _put(ws / "outputs" / "figures" / "a‮fdp.png", b"png")
    _rows(conn, plan, [("table", "Table 12", "| inject |\n| a | b |\x00" + "z" * 5000, "main", [f"code/{nasty}"], [], 1)])
    built = export.build_folder(conn, plan, tmp_path / "out" / "names")
    for rel in _tree(built.root):
        assert not re.search("[\x00-\x1f‪-‮]", rel), repr(rel)
    text = (built.root / "README.md").read_text()
    assert "‮" not in text and "\x00" not in text
    row = next(line for line in text.splitlines() if line.startswith("| Table 12 "))
    assert row.count("|") == 7 and len(row) < 500                          # one table row: no injected cells, a bounded caption


def test_a_malicious_save_path_in_an_author_script_never_reaches_the_readme_or_a_command(conn, plan, tmp_path):
    ws = W.workspace_dir(plan)
    evil = "./fig\x1b\u202e$(touch PWNED)`id`;rm/out.pdf"
    _put(ws / "author" / "code" / "main.R", MAIN_R + f'ggsave(filename="{evil}")\nggsave(filename="./plots/ok.pdf")\n'
                                                    'ggsave(filename="./my plots/x.pdf")\n')
    text = (export.build_folder(conn, plan, tmp_path / "out" / "evil").root / "README.md").read_text()
    assert not re.search("[\x00-\x08\x0b-\x1f\u202a-\u202e]", text) and "PWNED" not in text and "$(" not in text
    assert "`mkdir -p figures plots`" in text                                        # the plain ones still get a hint …
    assert "`my plots/`" in text and "名字里有空格的文件夹没有放进命令" in text            # … one with a space is named, never in a command
    assert "另有 1 个存图文件夹的名字含特殊字符，没有列出" in text                      # … and the hostile one is only counted


def test_a_script_name_with_shell_characters_never_lands_in_a_copyable_command(conn, plan, tmp_path):
    name = "x;touch PWNED $(id) `id`.R"
    ws = W.workspace_dir(plan)
    _put(ws / "author" / "code" / name, "y <- 1\n")
    conn.execute("DELETE FROM replication_coverage")
    conn.commit()
    _rows(conn, plan, [("table", "Table 13", "Named like a command", "main", [f"code/{name}"], [], 1),
                       ("table", "Table 14", "A tame one", "main", ["code/main.R"], [], 1)])
    built = export.build_folder(conn, plan, tmp_path / "out" / "cmd")
    text = (built.root / "README.md").read_text()
    how = text.split("## 怎么重跑", 1)[1].split("**在 Research Lab 里**", 1)[0]
    assert "PWNED" not in how and "$(" not in how and "`id`" not in how
    fence = how.split("```")[1]
    assert fence.strip().splitlines() == ["cd 正文结果", export.NO_CMD]                # no Rscript line for a name that is not plain
    assert export.NO_CMD in how.split("各组的入口脚本")[1] and "`code/main.R`" in how
    assert any(p.endswith(name) for p in _tree(built.root))                          # the file itself is still in the folder
    _issue(conn)
    done = export.export_folder(conn, plan)                                          # and a zip with such a name still works
    with zipfile.ZipFile(get_paths().projects / SLUG / "drafts" / done.rel.split("/", 1)[1]) as zf:
        assert any(n.endswith(name) for n in zf.namelist())


def test_a_file_over_the_cap_is_left_out_and_the_readme_says_so(conn, plan, tmp_path, monkeypatch):
    monkeypatch.setattr(export, "MAX_FILE", 60)                                 # the CSVs are 51 bytes, main.R is not
    built = export.build_folder(conn, plan, tmp_path / "out" / "cap")
    tree = _tree(built.root)
    assert "正文结果/code/main.R" not in tree and "正文结果/data/meetingData.csv" in tree
    assert built.skipped >= 1
    text = (built.root / "README.md").read_text()
    assert "## 没有放进来的文件" in text and "`code/main.R`：超过大小上限" in text


def test_the_folder_and_file_count_caps_hold_too(conn, plan, tmp_path, monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(export, "MAX_FILES", 3)
        built = export.build_folder(conn, plan, tmp_path / "out" / "few")
    generated = ("README.md", "R-packages.txt", "changes.md", "results.json")
    assert len([k for k in _tree(built.root) if not k.endswith(generated)]) <= 3 and built.skipped >= 5
    monkeypatch.setattr(export, "MAX_FOLDER", 100)
    small = export.build_folder(conn, plan, tmp_path / "out" / "small")
    assert sum(v for k, v in _tree(small.root).items() if "/code/" in k and not k.endswith("changes.md")) <= 100 and small.skipped >= 5


def test_the_zip_has_a_size_cap_and_a_refused_export_saves_nothing(conn, plan, monkeypatch):
    issue = _issue(conn)
    monkeypatch.setattr(export, "ZIP_CAP", 300)
    with pytest.raises(export.ExportError, match="压缩后超过"):
        export.export_folder(conn, plan)
    assert _drafts() == [] and _attachments(issue) == []
    assert not [p for p in get_paths().cache.glob("rl-export-*")]                  # the scratch folder is gone too


def test_the_zip_cap_is_enforced_while_a_member_is_written_not_after(tmp_path, monkeypatch):
    import zipfile as zf_mod
    folder = tmp_path / "pkg"
    for i in range(8):
        _put(folder / f"big{i}.bin", os.urandom(1024 * 1024))                      # incompressible: 8 MB in all
    reached: list[int] = []

    class Spy(zf_mod.ZipFile):
        def close(self):
            if self.fp is not None:
                reached.append(self.fp.tell())                                       # what was on disk when the zip was abandoned
            super().close()
    monkeypatch.setattr(zf_mod, "ZipFile", Spy)
    out = tmp_path / "pkg.zip"
    with pytest.raises(export.ExportError, match="压缩后超过"):
        export.zip_folder(folder, out, cap=1_500_000)
    assert reached and reached[0] < 1_500_000 + 1_200_000                           # stopped within a chunk of the cap, not at 8 MB
    assert not out.exists()                                                          # a refused zip is not left behind


def test_a_failed_copy_into_drafts_leaves_no_half_zip_and_never_touches_an_existing_one(conn, plan, tmp_path, monkeypatch):
    src = tmp_path / "x.zip"
    src.write_bytes(b"PK-whole-zip")
    drafts = get_paths().projects / SLUG / "drafts"
    drafts.mkdir(parents=True, exist_ok=True)
    (drafts / "a.zip").write_bytes(b"Leo's own")

    def boom(fsrc, fdst, length=0):
        fdst.write(b"half")
        raise OSError("disk full")
    with monkeypatch.context() as m:
        m.setattr(export.shutil, "copyfileobj", boom)
        with pytest.raises(OSError):
            export._store(SLUG, src, "b.zip")
    assert sorted(os.listdir(drafts)) == ["a.zip"]                                   # no b.zip, and no temporary file either
    assert export._store(SLUG, src, "a.zip") == "drafts/a-2.zip"                     # a taken name is skipped, atomically
    assert (drafts / "a.zip").read_bytes() == b"Leo's own" and (drafts / "a-2.zip").read_bytes() == b"PK-whole-zip"
    assert sorted(os.listdir(drafts)) == ["a-2.zip", "a.zip"]


def test_the_time_limit_stops_the_build_and_saves_nothing(conn, plan):
    issue = _issue(conn)
    with pytest.raises(export.ExportError, match="超过"):
        export.export_folder(conn, plan, seconds=-1)
    assert _drafts() == [] and _attachments(issue) == []


def test_a_running_plan_and_a_second_click_are_refused(conn, plan, monkeypatch):
    from research_lab.replication import rrun
    _issue(conn)
    with monkeypatch.context() as m:
        m.setattr(rrun, "busy", lambda _plan: True)
        with pytest.raises(export.ExportError, match="正在运行"):
            export.export_folder(conn, plan)
    assert export._LOCK.acquire(blocking=False)
    try:
        with pytest.raises(export.ExportError, match="正在导出"):
            export.export_folder(conn, plan)
    finally:
        export._LOCK.release()
    assert _drafts() == []


def test_hostile_megabyte_input_stays_fast(conn, plan, tmp_path, monkeypatch):
    from research_lab.replication import missing
    # missing.report (card 0188) reads the same author text and has its own timing test; this one times the export's own passes.
    monkeypatch.setattr(missing, "report", lambda *a, **k: missing.Report("x"))

    def took(fn):
        t = time.perf_counter()
        fn()
        return time.perf_counter() - t
    big = "(" * 700_000 + "'" * 700_000 + "source(" * 100_000
    assert took(lambda: export._text(big * 3, 200)) < 1 * SLOW and took(lambda: export._td("|" * 5_000_000, 200)) < 1 * SLOW
    assert took(lambda: export._part("..‮" * 2_000_000)) < 1 * SLOW and len(export._part("x" * 5_000_000 + ".R")) <= 120
    assert took(lambda: export._keys_of("Table " * 500_000)) < 1 * SLOW
    ws = W.workspace_dir(plan)
    _put(ws / "author" / "code" / "main.R", 'source("./code/helper.R")\nfread("./data/meetingData.csv")\n' + big[:1_900_000])
    for i in range(250):
        _put(ws / "author" / "code" / f"s{i}.R", 'x <- read.csv("./data/panel.csv")\n' + "a(" * 20_000)
    rows = [{"kind": "table", "label": f"Table {i}", "caption": "c" * 1_000_000, "page": "1", "section": "", "group": "main",
             "scripts": [{"path": "main.R", "full": "code/main.R", "have": True}] * 50, "outputs": ["o" * 100_000] * 20, "ticked": True}
            for i in range(1, 60)]
    assert took(lambda: export.build_folder(conn, plan, tmp_path / "out" / "hostile", items=rows)) < 1 * SLOW
    readme = (tmp_path / "out" / "hostile" / "README.md").read_text()
    assert len(readme) < 400_000


# ---------------------------------------------------------------- the zip, the 课题, the issue

def test_the_zip_is_saved_in_the_papers_kt_drafts_and_attached_to_the_issue_that_names_the_paper(conn, plan):
    issue = _issue(conn)
    done = export.export_folder(conn, plan)
    name = f"{plan}-replication.zip"
    assert done.rel == f"drafts/{name}" and done.attached and done.issue_key == "复现 Jost"
    assert _drafts() == [name] and _attachments(issue) == [f"drafts/{name}"]
    with zipfile.ZipFile(get_paths().projects / SLUG / "drafts" / name) as zf:
        names = zf.namelist()
    assert all(n.startswith(f"{plan}-replication/") for n in names)
    assert f"{plan}-replication/README.md" in names and f"{plan}-replication/正文结果/code/main.R" in names
    assert not [n for n in names if ".." in n.split("/") or n.startswith("/")]
    assert "已导出" in done.message and "复现 Jost" in done.message
    assert not list(get_paths().cache.glob("rl-export-*"))
    again = export.export_folder(conn, plan)                                      # never over the first one
    assert again.rel == f"drafts/{plan}-replication-2.zip"
    assert _attachments(issue) == [f"drafts/{name}", again.rel]


def test_with_several_issues_the_one_in_the_plans_direction_gets_it(conn, plan):
    other = _issue(conn, OTHER_SLUG, title="东盟那边也用了这篇")                  # direction sea-asean
    mine = _issue(conn, SLUG, title="复现 Jost")                                  # direction alliance-us, the plan's
    done = export.export_folder(conn, plan)
    assert done.attached and done.issue_key == "复现 Jost"
    assert _attachments(mine) == [done.rel] and _attachments(other) == []
    assert _drafts(SLUG) == [f"{plan}-replication.zip"] and _drafts(OTHER_SLUG) == []


def test_with_no_issue_in_the_plans_direction_or_none_at_all_the_zip_is_only_saved(conn, plan):
    assert export.pick_target(conn, plans.get(conn, plan)).issue_ref == ""
    done = export.export_folder(conn, plan)                                       # no issue names the paper
    assert not done.attached and done.project == SLUG and "没有议题把这篇论文列为材料" in done.message
    assert _drafts(SLUG) == [f"{plan}-replication.zip"]
    elsewhere = _issue(conn, OTHER_SLUG)
    also = _issue(conn, "institutional-reassurance")                              # strategy-signaling: neither is alliance-us
    again = export.export_folder(conn, plan)
    assert not again.attached and "没有一个在这份方案的研究方向里" in again.message
    assert _attachments(elsewhere) == [] and _attachments(also) == []
    assert again.project == SLUG                                                  # the paper's own 课题


def test_two_issues_in_the_same_direction_are_not_guessed_between(conn, plan):
    a = _issue(conn, SLUG, title="一")
    b = _issue(conn, "us-iran-talks", title="二")
    conn.execute("UPDATE replication_plans SET direction = 'strategy-signaling' WHERE id = ?", (plan,))
    conn.commit()
    c = _issue(conn, "institutional-reassurance", title="三")                      # strategy-signaling too, with b
    done = export.export_folder(conn, plan)
    assert not done.attached and "分不出该挂哪一个" in done.message
    assert [_attachments(x) for x in (a, b, c)] == [[], [], []]


def test_a_paper_in_no_kt_and_named_by_no_issue_is_refused(conn, tmp_path):
    paper = _paper(conn, "loose2024", "Loose", slug=None)
    plan_id = plans.create_blank(conn, paper, "alliance-us")
    _rows(conn, plan_id, [("table", "Table 1", "T", "main", [], [], 1)])
    with pytest.raises(export.ExportError, match="还没有加入任何课题"):
        export.export_folder(conn, plan_id)
    blank = plans.create_blank(conn)
    with pytest.raises(export.ExportError, match="没有关联论文"):
        export.export_folder(conn, blank)


# ---------------------------------------------------------------- only on the click

def test_the_page_shows_the_card_but_reading_it_writes_nothing(client, conn, plan):
    _issue(conn)
    before = set(get_paths().cache.glob("*"))
    html = client.get(f"/replicate/{plan}").text
    assert 'data-replicate-export-button' in html and "Export folder" in html and "勾选了 5 项" in html
    assert "disabled" not in html.split("data-replicate-export-button", 1)[1].split(">", 1)[0]
    assert _drafts() == [] and set(get_paths().cache.glob("*")) == before
    assert client.get(f"/replicate/{plan}/export-folder").status_code == 405       # there is no GET that exports


def test_the_card_is_disabled_when_nothing_is_ticked(client, conn, plan):
    conn.execute("UPDATE replication_coverage SET ticked = 0")
    conn.commit()
    html = client.get(f"/replicate/{plan}").text
    assert "data-replicate-export-empty" in html
    assert "disabled" in html.split("data-replicate-export-button", 1)[1].split(">", 1)[0]


def test_only_a_post_from_the_app_exports(client, conn, plan):
    issue = _issue(conn)
    url = f"/replicate/{plan}/export-folder"
    assert client.post(url, headers={"HX-Request": ""}).status_code == 403                                   # no Origin, no HX header
    assert client.post(url, headers={"HX-Request": "", "Origin": "http://evil.example"}).status_code == 403   # another site
    assert _drafts() == [] and _attachments(issue) == []
    r = client.post(url, follow_redirects=False)
    assert r.status_code == 204 and "msg=" in r.headers["HX-Redirect"]
    assert _drafts() == [f"{plan}-replication.zip"] and _attachments(issue) == [f"drafts/{plan}-replication.zip"]
    plain = client.post(url, headers={"HX-Request": "", "Origin": "http://127.0.0.1:4100"}, follow_redirects=False)
    assert plain.status_code == 303 and plain.headers["location"].startswith(f"/replicate/{plan}?msg=")


def test_a_refused_post_says_why_and_a_missing_plan_is_404(client, conn, plan):
    conn.execute("UPDATE replication_coverage SET ticked = 0")
    conn.commit()
    r = client.post(f"/replicate/{plan}/export-folder", follow_redirects=False)
    assert r.status_code == 204 and "err=" in r.headers["HX-Redirect"] and _drafts() == []
    assert client.post("/replicate/RP-2026-9999/export-folder").status_code == 404
