"""Card 0167 (from card 0164's review): integrate and release first check that the main checkout has no
uncommitted code. A git error there used to read as clean; it now refuses, with the reason in Chinese."""
from __future__ import annotations

import pytest

from research_lab.devflow import cards, flow

UNKNOWN = {"code": [], "content": [], "unknown": True}


def _ready_card(monkeypatch):
    monkeypatch.setattr(flow, "git", lambda *a, **k: "head1" if a[:1] == ("rev-parse",) else "")
    card = cards.new("K", files=["k.py"])
    cards.update(card, status="review", owner="writer", branch="agent/k",
                 test={"result": "pass", "level": "L1", "commit": "head1"})
    return card


def test_a_main_checkout_git_cannot_read_stops_the_merge(repo, monkeypatch):
    card = _ready_card(monkeypatch)
    # the repo fixture is not a git checkout, so git status on it errors: the real failure, not a stub
    with pytest.raises(flow.FlowError, match="读不到主仓库的 git 状态.*不能合并"):
        flow.integrate(card.id, "integrator", do_push=False)
    assert flow.read_lock() is None                                    # refused before taking the lock


@pytest.mark.parametrize("state,refused", [
    (UNKNOWN, "读不到主仓库的 git 状态"),                              # a timeout reads the same as an error
    ({"code": ["app/x.py"], "content": []}, "主仓库里有未提交的代码改动"),
    ({"code": [], "content": ["_data/a.md"]}, None),                  # research content never blocks
])
def test_the_merge_gate_on_the_main_checkout(repo, monkeypatch, state, refused):
    card = _ready_card(monkeypatch)
    monkeypatch.setattr(flow, "dirty", lambda path, timeout=None: state)
    with pytest.raises(Exception) as err:            # past the gate it fails on git in a tmp dir, like test_devflow
        flow.integrate(card.id, "integrator", do_push=False)
    if refused:
        assert err.type is flow.FlowError and refused in str(err.value)
    else:
        assert "读不到" not in str(err.value) and "未提交的代码" not in str(err.value)


def test_a_main_checkout_git_cannot_read_stops_the_release(repo, monkeypatch):
    with pytest.raises(flow.FlowError, match="读不到主仓库的 git 状态.*不能发版"):
        flow.release("9.9.9", "integrator", install=False, do_push=False)
    monkeypatch.setattr(flow, "dirty", lambda path, timeout=None: {"code": [], "content": []})
    with pytest.raises(flow.FlowError, match="集成锁不在"):             # clean: past the gate, stopped by the lock
        flow.release("9.9.9", "integrator", lock_held=True)
