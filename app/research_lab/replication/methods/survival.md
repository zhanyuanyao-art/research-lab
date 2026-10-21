---
key: survival
name: 生存分析
english: Survival Analysis / Event History Analysis
summary: 研究一件事要过多久才发生，以风险率为核心，在部分单位到观察结束时事件仍未发生的情况下，估计各种因素如何改变事件在任一时刻发生的风险。
---

## 适用的问题

结果不是一个数值，也不是一个是否，而是一段时间：从某个起点开始，过了多久某件事才发生。比如：

- 一场战争打多久才结束；
- 一位领导人在任多久才下台，以及是因选举、政变还是死亡下台；
- 内战结束后的和平能维持多久才再次破裂；
- 一个国家签署条约后过多久批准，一个联盟能维持多久。

这类数据有一个共同的麻烦：观察常常在事件发生之前就结束了。数据截止时还在打的仗、还在任的领导人、还没破裂的和平，我们只知道它们的真实持续时间至少有这么长。生存分析就是为这种数据设计的。

## 核心思想

核心概念是**风险率**：在已经撑到某一时刻的单位中，事件在这一刻发生的可能性。问的不是和平平均维持几年，而是和平已经维持了 5 年，第 6 年破裂的概率有多大。这样提问，自然地处理了两件事：只有仍处在风险中的单位才进入计算；每一刻的风险可以随时间变化，也可以随当时的协变量变化。

观察结束时事件仍未发生，叫**右删失**。删失的单位不是没有信息，它们告诉我们至少撑到了这里。对持续时间直接做 OLS 有几个问题：

- 删掉删失的单位，只剩已经结束的、往往更短的持续期，持续时间被系统性低估；
- 把删失时的时长当成真实时长，同样低估，低估多少还取决于数据何时截止；
- 持续时间非负、严重右偏，误差不可能近似正态；
- 协变量在持续期内会变，比如经济增长逐年不同，OLS 只能给每个单位一个值。

一个数字例子：五段战后和平分别维持了 2、4、5、6、8 年，其中 5 年和 8 年那两段到数据截止时还没破裂，是删失。用 Kaplan–Meier 方法逐个时点计算：

| 时间 t | 期初仍在风险中 | 当期破裂 | 当期删失 | 当期存活比例 | 累计存活 S(t) |
|---|---|---|---|---|---|
| 2 | 5 | 1 | 0 | 4/5 | 0.80 |
| 4 | 4 | 1 | 0 | 3/4 | 0.60 |
| 5 | 3 | 0 | 1 | 1 | 0.60 |
| 6 | 2 | 1 | 0 | 1/2 | 0.30 |
| 8 | 1 | 0 | 1 | 1 | 0.30 |

存活比例在第 6 年跌到 0.5 以下，中位持续时间是 6 年。如果删掉两段删失的和平，只看 2、4、6，中位数是 4 年。删失的单位虽然没告诉我们终点，却让估计不再偏短。还要注意，第 5 年删失的那段和平在第 6 年已退出风险集，所以第 6 年的分母是 2。

## 模型设定

**基本量**：

```text
S(t) = Pr(T > t)
h(t) = lim_{Δt→0} Pr(t ≤ T < t + Δt | T ≥ t) / Δt
H(t) = ∫₀ᵗ h(u) du，   S(t) = exp(−H(t))
```

- `T`：持续时间，即事件发生的时刻；
- `S(t)`：存活函数，撑过 t 的概率；
- `h(t)`：风险率，已经撑到 t 的单位在下一瞬间发生事件的速率；离散时间下就是 `Pr(T = t | T ≥ t)`；
- `H(t)`：累积风险；三者知道其一，就能推出另外两个。

**Kaplan–Meier 估计**：

```text
Ŝ(t) = Π_{t_j ≤ t} (1 − d_j / n_j)
```

- `t_j`：第 j 个有事件发生的时点；
- `d_j`：该时点发生事件的个数；
- `n_j`：该时点之前仍在风险集中的个数，删失的单位在删失之后退出。

**Cox 比例风险模型**：

```text
h_i(t) = h₀(t) · exp(X_i′β)
L(β) = Π_{事件 j} [ exp(X_j′β) / Σ_{k ∈ R(t_j)} exp(X_k′β) ]
```

- `h₀(t)`：基准风险，即协变量都为 0 时风险随时间的形状，不做任何设定；
- `X_i`：协变量；`β`：系数；
- `L(β)`：偏似然。每个事件发生时，它问的是：在当时仍处于风险中的单位里，为什么偏偏是这一个。`R(t_j)` 是第 j 个事件时点的风险集；基准风险在分子分母中约掉，所以不用设定；
- 同一时点有多个事件时要做近似：R 默认 Efron 方法，Stata 默认 Breslow 方法。按年记录的数据同时事件很多，两者可能有差别。

**风险比**：

```text
HR_k = exp(β_k)
S₁(t) = S₀(t)^HR
```

- `HR_k`：协变量 k 增加一个单位，任一时刻的风险乘以 `exp(β_k)`；HR = 1.25 表示风险高 25%，HR = 0.6 表示低 40%；
- 第二行：在比例风险下，一组的存活曲线等于另一组存活曲线的 HR 次方，可以用来把风险比翻译成存活概率。

**比例风险假设与补救**。Cox 模型要求两个单位的风险之比不随时间变化。检验用 Schoenfeld 残差：每次事件发生时，发生事件那个单位的协变量值，减去风险集内按风险加权的平均值；比例风险成立时，标准化残差与时间无关，Grambsch 和 Therneau 据此给出了检验。违反时，常用的补救是让效果随时间变化：

```text
h_i(t) = h₀(t) · exp(β₁·X_i + β₂·X_i·log t)
```

- `β₂`：效果随时间的变化，t 时刻的风险比为 `exp(β₁ + β₂·log t)`；
- 如果违反比例风险的只是控制变量，也可以按它分层，让每一层有自己的基准风险。

**Weibull 参数模型**：

```text
h_i(t) = p · t^(p−1) · exp(X_i′β)
log T_i = X_i′γ + σ·ε_i，   p = 1/σ，  β = −γ/σ
```

- `p`：形状参数。p > 1 风险随时间上升，p < 1 随时间下降，p = 1 是风险恒定的指数模型；
- 第一行是比例风险写法，第二行是加速失效时间写法，两套系数符号相反：前者正号表示风险更高、持续更短，后者正号表示持续更长；
- 参数模型能直接预测持续时间，代价是要求分布设定正确。

**时变协变量与计数过程格式**。把每段持续期按协变量变化的时点切成多行，每行记录区间起点、终点和区间末是否发生事件：

| 领导人 | start | stop | event | 当年经济增长 |
|---|---|---|---|---|
| A | 0 | 1 | 0 | 2.0 |
| A | 1 | 2 | 1 | −3.0 |
| B | 0 | 1 | 0 | 4.0 |
| B | 1 | 2 | 0 | 3.5 |
| B | 2 | 3 | 0 | 3.0 |

A 在第 2 年下台；B 到第 3 年数据截止时仍在任，是删失。每一行只在自己的区间里贡献风险，模型用的是事件发生那一刻的协变量值。数据开始时已经在任的领导人，把 start 设为进入数据时的任期，即左截断，也用这种格式处理。

**离散时间 logit**。国际关系常见的国家年、双边年数据，因变量是当年是否爆发冲突，本身就是按年分组的持续时间数据。Beck、Katz 和 Tucker 指出，对它直接跑 logit，等于假设风险不随距上次事件的时间变化；协变量和这个时间相关时，系数就会有偏。做法是加入时间依赖项：

```text
Pr(Y_it = 1 | 此前尚未发生) = logit⁻¹( X_it′β + g(t_i) )
```

- `Y_it`：单位 i 在第 t 年是否发生事件；
- `t_i`：距上次事件或进入风险集的年数，比如和平年数；
- `g(t)`：时间依赖项。Beck、Katz 和 Tucker 建议用时间哑变量或三次样条；Carter 和 Signorino 建议用 t、t²、t³，更简单，也便于画出风险随时间的形状；
- 把 logit 换成互补双对数链接，就与分组时间下的比例风险模型严格对应；事件罕见时两者差别不大。

**竞争风险**。领导人可能因选举、政变或死亡下台，发生了一种，其余的就不会再发生。把其他结局当作删失，估计的是特定原因的风险，适合回答哪些因素提高政变风险；但这时 1 − Kaplan–Meier 会高估到某一时刻被政变推翻的概率，因为已因选举下台的人不可能再被推翻。这个概率要用累积发生函数，或 Fine 和 Gray 的子分布风险模型。

## 识别假设

生存分析是描述时间过程的一类模型，本身不是因果识别设计。下面是估计量成立需要的条件，以及每一条不成立时的代价：

1. **删失与风险无关**：给定协变量，单位是否被删失与它发生事件的风险无关。代价：最不稳定的政权如果恰好因为数据缺失提前退出样本，风险被低估，持续时间被高估。
2. **比例风险**，针对 Cox 模型和比例风险形式的参数模型。代价：估计出的风险比是对整个时段的某种平均，可能掩盖一个前期为正、后期为负的效果，标准误也不再可信。
3. **协变量的函数形式正确**：Cox 模型假设协变量以对数线性的方式影响风险。代价：效果估计有偏；而且 Keele 指出，函数形式设错常常表现为假的比例风险违反，所以要先查函数形式，再查比例风险。
4. **风险集和时间起点定义正确**：谁从什么时候开始处于风险中。代价：冲突爆发数据里保留了战争正在进行的年份，或者数据开始前就已在任的领导人从上任起算却不做左截断处理，都会让样本偏向长寿的单位。
5. **观测之间独立，或已处理相依**：同一国家的多段和平、同一领导人的多次任期彼此相关。代价：标准误偏小。应按单位聚类，或加入脆弱性项，即单位层面的随机效应。
6. **协变量在风险时点之前确定**：代价：用战争最后一年的伤亡、或领导人最终的下台方式作协变量，就是用结果去解释结果。
7. **参数模型的分布、离散时间模型的时间依赖设定正确**：代价：系数会把设错的时间依赖吸收进去。

这些都满足，风险比也只是关联。要说维和降低了复发风险，还得说明维和不是挑着案例去的，这是研究设计的事。

## 稳健性检验：做什么，为什么

每一项检验都对应上面的某条假设。读论文时，可以对照着看作者做了哪些、漏了哪些。

| 检验 | 检查哪条假设 | 怎么做，怎么看 |
|---|---|---|
| Kaplan–Meier 曲线与对数秩检验 | 比例风险的直观检查，模型方向是否有数据支撑 | 按关键变量分组画存活曲线。曲线交叉，提示风险比随时间改变；模型结果与曲线方向相反，要回头查设定 |
| Schoenfeld 残差检验 | 比例风险 | 逐个协变量和整体检验，并画标准化残差随时间的走势。显著或有明显趋势，说明该变量的效果随时间变化，单一风险比会误导 |
| 函数形式检查 | 函数形式 | 用鞅残差或样条检查连续协变量是否需要取对数、加平方或分段。先做这一步再做比例风险检验，否则会把函数形式问题误判为比例风险问题 |
| 时间交互或分层 | 比例风险 | 对违反的变量加入与 log t 的交互，报告风险比随时间的变化；违反的若是控制变量，就按它分层。核心结论应当不变 |
| 离散时间模型对照 | 连续时间的设定，同时事件的近似处理 | 用 logit 或互补双对数模型，加时间样条或 t、t²、t³ 重估。年度数据同时事件多，Efron 和 Breslow 的近似可能失准，两类模型的结论应当一致 |
| 聚类标准误或脆弱性模型 | 观测独立 | 按国家或领导人聚类，或加入单位随机效应。显著性大幅下降，说明原来的推断依赖于把相关的观测当成独立 |
| 改变删失的定义 | 删失与风险无关 | 对结局不清楚的持续期换一种编码，或去掉数据截止前不久才开始的持续期。结论稳定，说明删失方式不是关键 |
| 竞争风险模型 | 删失与风险无关 | 把其他结局当作竞争事件而不是普通删失，看关心的那种结局的结论是否还在 |
| 协变量滞后一期 | 协变量先于风险 | 时变协变量改用上一期的值。结果变化很大，提示存在反向因果 |

## 怎么读结果

- **先看事件是什么**：研究战争持续时间时，事件是战争结束，HR > 1 表示战争结束得更快；研究和平持续时间时，事件是战争复发，HR > 1 表示和平更脆弱。同样的符号，含义取决于事件的定义。
- **再看参数化**：Cox 和比例风险形式报的是风险比；R 的 `survreg`、Stata 加了 `time` 选项的 `streg` 报的是加速失效时间系数，正号表示持续更久，`exp(γ)` 是时间比，1.5 表示持续时间长 50%。
- **风险比不是持续时间的比例**：维和的 HR = 0.6，意思是在和平的任一时刻，有维和时复发的风险比没有维和时低 40%，不是和平长 40%。换成存活概率：没有维和时五年后仍保持和平的比例若为 0.5，有维和时约为 0.5^0.6 ≈ 0.66。
- **形状参数要谨慎**：Weibull 估出 p < 1，即总体风险随时间下降，未必说明每一段和平都越来越稳；也可能是不稳定的案例早早破裂、退出样本，剩下的本来就稳。未观测异质性和真正的时间依赖，从数据上很难分开。
- **表格里要找的信息**：单位数、持续期数、事件数；同时事件的处理方法；标准误聚类的层级；比例风险检验的结果。

读的时候可以这样说：控制冲突烈度和经济水平后，有维和的战后和平在任一时刻复发战争的风险约为没有维和时的 0.6 倍；Schoenfeld 残差检验没有发现维和变量违反比例风险；换成按国家聚类的标准误和离散时间 logit，结论不变。

## 常见陷阱

- **把系数方向读反**：比例风险系数和加速失效时间系数符号相反；事件定义不同，同一符号的含义也相反。
- **对持续时间跑 OLS，或者删掉删失的单位**：持续时间被低估。
- **不做比例风险检验，或者不查函数形式就检验**。
- **用事后的信息当协变量**：战争的总伤亡、领导人最终的下台方式，都要到持续期结束才知道。
- **风险集和起点出错**：忽略左截断，或在冲突爆发数据里保留冲突正在进行的年份。
- **二元面板 logit 不控制时间依赖**：系数会吸收距上次事件的时间的影响。
- **有竞争风险时把 1 − Kaplan–Meier 当作某种结局的发生概率**：会高估。
- **把风险比当成因果效应**：面对选择性分配，生存模型和普通回归一样无能为力。

## 代码写法

R，核心是 survival 包：

```r
library(survival)
# duration 为持续时间，event 为 1 表示事件发生、0 表示删失

# Kaplan–Meier 曲线和对数秩检验
plot(survfit(Surv(duration, event) ~ pko, data = df))
survdiff(Surv(duration, event) ~ pko, data = df)

# Cox 模型：默认用 Efron 方法处理同时事件，按国家聚类；exp(coef) 一列是风险比
cox <- coxph(Surv(duration, event) ~ pko + gdp + intensity, data = df, cluster = country)
summary(cox)

# 比例风险检验：Schoenfeld 残差
zph <- cox.zph(cox)
zph
plot(zph)

# 让 pko 的效果随 log(t) 变化
cox_tt <- coxph(Surv(duration, event) ~ pko + gdp + intensity + tt(pko), data = df,
                tt = function(x, t, ...) x * log(t))

# 时变协变量：计数过程格式，每行是一个 (start, stop] 区间
cox_tv <- coxph(Surv(start, stop, event) ~ pko + gdp_lag, data = df_long, cluster = country)

# Weibull：survreg 报告加速失效时间系数，正号表示持续更久
wb <- survreg(Surv(duration, event) ~ pko + gdp, data = df, dist = "weibull")

# 竞争风险：status 为因子，第一个水平是删失，其余水平是各类结局，其中一类叫 coup
fg <- finegray(Surv(duration, status) ~ ., data = df, etype = "coup")
fit_fg <- coxph(Surv(fgstart, fgstop, fgstatus) ~ gdp + intensity, data = fg, weights = fgwt)

# 离散时间 logit：国家年数据，peace_years 为和平已持续的年数
lg <- glm(onset ~ x1 + x2 + peace_years + I(peace_years^2) + I(peace_years^3),
          family = binomial(link = "logit"), data = cy)
```

Stata：

```stata
* 声明生存数据，之后的 st 命令都依赖这一步
stset duration, failure(event) id(id)
sts graph, by(pko)
sts test pko
* Cox 模型：默认报告风险比，用 Breslow 方法处理同时事件
stcox pko gdp intensity, vce(cluster country)
estat phtest, detail
* 让 pko 的效果随 ln(t) 变化
stcox pko gdp intensity, tvc(pko) texp(ln(_t))
* Weibull：默认比例风险参数化；加 time 选项改为加速失效时间参数化
streg pko gdp intensity, distribution(weibull)
* 计数过程格式：每行一个区间，start 为区间起点
stset stop, id(id) failure(event) time0(start)
* 竞争风险：event 取 1 为政变、取 2 为其他方式下台
stset duration, failure(event == 1)
stcrreg gdp intensity, compete(event == 2)
* 离散时间 logit，和平年数的一次、二次、三次项
logit onset x1 x2 c.peace_years##c.peace_years##c.peace_years, vce(cluster country)
```

Python，常用 lifelines：

```python
from lifelines import CoxPHFitter

# 传入的数据框只放持续时间、事件、协变量和聚类变量
cols = ["duration", "event", "pko", "gdp", "intensity", "country"]
cph = CoxPHFitter()
cph.fit(df[cols], duration_col="duration", event_col="event", cluster_col="country")
cph.print_summary()
cph.check_assumptions(df[cols], p_value_threshold=0.05)   # 比例风险检验
```

在作者代码里认生存分析，看这几处：`Surv(`、`survfit`、`coxph`、`cox.zph`、`survreg`、`tt(`、`finegray`；Stata 里先有一行 `stset`，之后是 `sts graph`、`stcox`、`streg`、`estat phtest`、`stsplit`、`tvc()`、`stcrreg`；Python 的 `lifelines`；离散时间的写法则是二元事件变量配上和平年数及其平方、立方，或 `btscs` 生成的样条项，回归命令是 `logit` 或 `cloglog`。

## 延伸阅读

- Box-Steffensmeier, Janet M., and Bradford S. Jones. 2004. *Event History Modeling: A Guide for Social Scientists*. Cambridge University Press.
- Therneau, Terry M., and Patricia M. Grambsch. 2000. *Modeling Survival Data: Extending the Cox Model*. Springer.
- Cox, D. R. 1972. "Regression Models and Life-Tables." *Journal of the Royal Statistical Society: Series B* 34 (2).
- Box-Steffensmeier, Janet M., and Christopher J. W. Zorn. 2001. "Duration Models and Proportional Hazards in Political Science." *American Journal of Political Science* 45 (4).
- Grambsch, Patricia M., and Terry M. Therneau. 1994. "Proportional Hazards Tests and Diagnostics Based on Weighted Residuals." *Biometrika* 81 (3).
- Keele, Luke. 2010. "Proportionally Difficult: Testing for Nonproportional Hazards in Cox Models." *Political Analysis* 18 (2).
- Beck, Nathaniel, Jonathan N. Katz, and Richard Tucker. 1998. "Taking Time Seriously: Time-Series-Cross-Section Analysis with a Binary Dependent Variable." *American Journal of Political Science* 42 (4).
- Carter, David B., and Curtis S. Signorino. 2010. "Back to the Future: Modeling Time Dependence in Binary Data." *Political Analysis* 18 (3).
