"""Card 0193: methods read from author code by rules, and a teaching chapter for each method."""
from __future__ import annotations

import re
import time

import pytest

from research_lab.replication import method_notes as M
from ._timing import SLOW


def _keys(scripts: dict[str, str]) -> list[str]:
    return [f.key for f in M.detect(scripts)]


def test_r_calls_and_formula_terms():
    code = '''
library(fixest)
controls = " + nAttend + numDef +
              cinc + factor(admin)"
fit1 = glm(paste(outcome, "~ meanHawk", controls), data=d, family="poisson")
fit2 = lm(y ~ x, data=d)
fit3 = glm(win ~ x, family = binomial(link = "logit"), data = d)
fit4 = glmer.nb(n ~ x + (1|admin), data = d)
es = feols(y ~ sunab(first_treat, year) | unit + year, data = d)
# glm(should_not_count ~ x, family = poisson)
s = "coxph(Surv(t, e) ~ x)"
'''
    found = {f.key: f for f in M.detect({"code/main.R": code})}
    assert set(found) == {"did", "fixed_effects", "multilevel", "ols", "binary", "poisson", "negbin"}
    assert found["poisson"].count == 1                       # the commented call and the string do not count
    assert found["did"].snippets[0].line == 9
    assert found["fixed_effects"].snippets[0].line == 9      # the feols `|` part is read first


def test_a_fixed_effect_term_is_shown_from_the_start_of_its_statement():
    code = 'controls = " + nAttend + numDef +\n              cinc + factor(admin)"\nfit = lm(y ~ x, data = d)\n'
    fe = {f.key: f for f in M.detect({"a.R": code})}["fixed_effects"].snippets[0]
    assert fe.code.startswith("controls = ") and fe.code.endswith('factor(admin)"')


def test_data_prep_factor_is_not_a_fixed_effect():
    code = 'd$admin = factor(d$admin)\nfit = lm(y ~ x, data = d)\n'
    assert _keys({"a.R": code}) == ["ols"]


def test_did_from_an_interaction_term():
    code = 'm = feols(y ~ treated:post | unit + year, data = df, cluster = ~unit)\n'
    keys = _keys({"a.R": code})
    assert "did" in keys and "fixed_effects" in keys and "ols" in keys


def test_stata_commands_and_prefixes():
    code = '''
use "data.dta", clear
eststo: quietly reg y x, robust
xtreg y x, fe vce(cluster id)
nbreg deaths x
bootstrap, reps(500): reg y x
csdid y, ivar(id) time(year) gvar(g)
* logit y x
'''
    keys = _keys({"do/main.do": code})
    assert keys == ["did", "fixed_effects", "ols", "negbin", "bootstrap"]


def test_stata_commands_behind_prefixes_are_read():
    code = "quietly reg y x\nbysort id: logit w x\neststo m1: qui nbreg n x\ncap noi poisson n x\nby id year: xtreg y x, fe\n"
    found = {f.key: f.count for f in M.detect({"a.do": code})}
    assert found == {"fixed_effects": 1, "ols": 2, "binary": 1, "poisson": 1, "negbin": 1}


def test_python_class_calls_and_r_robust_and_negative_binomial():
    py = 'm = sm.OLS(y, X).fit()\ng = sm.GLM(y, X, family=sm.families.Binomial()).fit()\nl = LogisticRegression().fit(X, y)\nlm(x)\n'
    assert {f.key for f in M.detect({"a.py": py})} == {"ols", "binary"}
    assert _keys({"a.R": "m = lm_robust(y ~ x, data = d)\n"}) == ["ols"]
    assert _keys({"a.R": "m = glm(n ~ x, family = negative.binomial(2), data = d)\n"}) == ["negbin"]


def test_python_calls():
    code = '''
import statsmodels.formula.api as smf
m = smf.ols("y ~ x + C(country)", data=df).fit()
p = smf.poisson("n ~ x", data=df).fit()
'''
    assert _keys({"py/main.py": code}) == ["fixed_effects", "ols", "poisson"]


def test_r_has_no_python_only_names():
    assert _keys({"a.R": "x = logit(p)\ny = ols(z)\n"}) == []


def test_one_snippet_per_script_up_to_the_cap():
    code = "\n".join(f"f{i} = lm(y ~ x{i}, data = d)" for i in range(20))
    (f,) = M.detect({f"s{n}.R": code for n in range(5)})
    assert f.count == 100 and len(f.snippets) == M.SNIPPETS_PER_METHOD
    assert [h.script for h in f.snippets] == ["s0.R", "s1.R", "s2.R"] and f.snippets[0].line == 1


@pytest.mark.parametrize("hostile", [
    "glm(" * 300_000,                                                  # one long line of calls
    ("glm(" + "x" * 900 + "\n") * 2_000,                               # many lines, each opening a call
    ("treatment" + "_" * 20 + " " * 4 + "*" + " " * 4 + "post" + "a" * 19 + " ") * 40_000,
    ("+ factor (" * 100 + "\n") * 10_000,
    "(" * 1_000_000,
    ("quietly: capture: noisily: reg y x\n") * 30_000,
    "xtreg y x" + "," * 590_000,
    ("bysort " + "a " * 60 + ": " * 30 + "reg y x\n") * 3_000,
], ids=["calls-one-line", "calls-many-lines", "did-terms", "factor-terms", "open-brackets", "stata-prefixes",
        "xt-commas", "by-prefixes"])
def test_hostile_code_finishes_fast(hostile):
    for name in ("big.R", "big.do", "big.py"):
        t = time.perf_counter()
        M.detect({name: hostile})
        assert time.perf_counter() - t < 1.0 * SLOW, name


# ---------------------------------------------------------------- the chapters

_CODE = re.compile(r"```.*?```", re.S)


def test_every_detected_method_has_a_chapter_in_order():
    chapters = M.chapters()
    assert set(M.ORDER) <= set(chapters), set(M.ORDER) - set(chapters)
    for key, ch in chapters.items():
        assert ch.key == key and ch.name and ch.summary
        assert tuple(ch.sections) == M.SECTIONS, key
        assert all(ch.sections[s] for s in M.SECTIONS), key


def test_chapters_follow_the_writing_rules():
    for key, ch in M.chapters().items():
        prose = _CODE.sub("", ch.markdown)
        assert "（" not in prose and "）" not in prose, key          # Leo: no full-width parenthetical asides
        table = ch.sections["稳健性检验：做什么，为什么"]
        assert "| 检验 | 检查哪条假设 | 怎么做，怎么看 |" in table, key
        assert "在作者代码里认" in ch.sections["代码写法"], key
