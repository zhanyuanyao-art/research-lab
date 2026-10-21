---
key: binary
name: Logit 与 Probit
english: Logit and Probit Models
summary: 结果只有发生和没发生两种时，把解释变量的线性组合压进 0 到 1 之间当作概率；系数只说明方向，大小要换算成概率的变化才能读。
---

## 适用的问题

因变量只有两个取值：发生或没发生，是或否。比如：

- 一对国家在某一年是否爆发军事化争端；
- 一个国家是否批准某项条约，是否在联合国大会的某次表决中投赞成票；
- 一个国家某一年是否发生政变；
- 一位受访者是否支持增加对外援助。

我们关心的是给定解释变量时事件发生的概率 `Pr(Y = 1 | X)`，以及各个变量怎样改变这个概率。

直接用最小二乘，也就是线性概率模型，有两个毛病：预测的概率可能小于 0 或大于 1；它假定 X 每增加一单位，概率总是变化同样多。而一个几乎不可能开战的国家对，和一个处在战争边缘的国家对，受到同样的冲击，概率的变化不会相同。Logit 和 Probit 用一条 S 形曲线描述概率，解决这两个问题。

## 核心思想

**潜变量的想法**。设想每个观测背后有一个看不到的连续倾向 Y*，比如一国领导层诉诸武力的倾向，它由解释变量和随机因素共同决定；倾向超过某个门槛，事件就发生。我们只看到发生与否，看不到倾向有多强。随机因素服从标准正态分布，得到 Probit；服从 logistic 分布，得到 Logit。两种分布形状很像，Logit 的尾部稍厚。

因为 Y* 看不到，它的尺度无法确定：把 Y* 和门槛同时放大两倍，观测到的结果完全不变。所以模型必须把随机因素的方差固定下来：Probit 固定为 1，Logit 固定为 π²/3，标准差约 1.8。这带来两个后果。第一，系数只在这个人为的尺度上有意义，Logit 系数大约是 Probit 系数的 1.6 到 1.8 倍，两者不能直接比。第二，凡是改变剩余方差的东西都会改变系数的大小，后面讲的跨模型比较问题就源于此。

**为什么系数不是边际效应**。在 S 形曲线上，X 增加一单位，概率变化多少取决于处在曲线的哪一段：概率在 0.5 附近时曲线最陡；接近 0 或 1 时曲线平坦，同样的 X 变化几乎不改变概率。

一个数字例子。设 Logit 模型的线性部分是 `−3 + 1·X`：

| X | 线性部分 | 概率 | 几率 |
|---|---|---|---|
| 0 | −3 | 0.047 | 0.050 |
| 1 | −2 | 0.119 | 0.135 |
| 3 | 0 | 0.500 | 1.000 |
| 4 | 1 | 0.731 | 2.718 |

系数都是 1，但 X 从 0 到 1，概率只增加 7.2 个百分点；从 3 到 4，增加 23.1 个百分点。不变的是几率之比：X 每增加一单位，几率都乘以 `exp(1) ≈ 2.72`。几率是发生概率和不发生概率之比，概率为 0.5 时几率为 1。所以 Logit 系数有两种读法：在对数几率尺度上它是常数；在概率尺度上它随位置变化。

Logit 还有一个方便的上限：概率尺度上的边际效应最大是 β/4，出现在概率为 0.5 的地方。上例中 β = 1，所以 X 增加一单位，概率最多增加约 25 个百分点。

## 模型设定

```text
Y*_i = X_i′β + ε_i
Y_i = 1，若 Y*_i > 0；否则 Y_i = 0
Pr(Y_i = 1 | X_i) = F(X_i′β)
```

- `Y*_i`：看不到的潜在倾向；
- `X_i′β`：解释变量的线性组合，也叫线性预测值；常数项吸收了门槛的位置，所以门槛可以设为 0；
- `ε_i`：随机因素；
- `F`：ε 的累积分布函数，把任意实数映射到 0 到 1 之间。

两种连接函数：

```text
Logit：Pr(Y_i = 1 | X_i) = Λ(X_i′β) = exp(X_i′β) / [1 + exp(X_i′β)]
       等价于 log[p_i / (1 − p_i)] = X_i′β
Probit：Pr(Y_i = 1 | X_i) = Φ(X_i′β)
       等价于 Φ⁻¹(p_i) = X_i′β
```

- `Λ`：logistic 分布函数；
- `Φ`：标准正态分布函数，`Φ⁻¹` 是它的反函数；
- `p_i`：第 i 个观测 Y = 1 的概率；
- `p_i / (1 − p_i)`：几率；取对数后叫对数几率，Logit 模型里它是 X 的线性函数；
- 由此 `exp(β_k)` 是几率比：X_k 增加一单位，几率变成原来的几倍。

估计用极大似然，而不是最小二乘：

```text
log L(β) = Σ_i { Y_i·log F(X_i′β) + (1 − Y_i)·log[1 − F(X_i′β)] }
```

- `log L(β)`：对数似然，即在给定 β 时观察到这组数据的概率取对数，软件寻找让它最大的 β；
- 发生的观测贡献 `log F`，没发生的观测贡献 `log(1 − F)`。

边际效应：

```text
∂Pr(Y_i = 1 | X_i) / ∂X_ki = f(X_i′β)·β_k
Logit：f(z) = Λ(z)·[1 − Λ(z)]；Probit：f(z) = φ(z)
AME_k = (1/N)·Σ_i f(X_i′β̂)·β̂_k
MEM_k = f(X̄′β̂)·β̂_k
```

- `f`：F 的导数，即密度函数；`φ` 是标准正态密度；
- 第一行说明，概率对 X_k 的边际效应等于系数乘以一个随观测而变的比例因子，所以每个观测的边际效应大小不同，符号都和 β_k 相同；
- `AME_k`：平均边际效应，先对每个观测算边际效应，再取平均；N 是观测数；
- `MEM_k`：均值处的边际效应，先把所有 X 取样本均值，再算一个边际效应；`X̄` 是解释变量的均值向量；
- X_k 是 0/1 变量时不用导数，而是算每个观测在 X_k = 1 和 X_k = 0 时的预测概率之差，再取平均。

## 识别假设

Logit 和 Probit 不是因果识别设计。下面是估计量需要的条件和每条失败的代价。和最小二乘不同，非线性模型里有几条失败会直接让系数有偏，而不只是让标准误错。

1. **外生性**：ε 与 X 无关。遗漏混杂因素、反向因果、测量误差的后果和最小二乘一样：系数有偏，不一致。
2. **潜变量误差的分布设定正确，包括同方差**：在最小二乘里，异方差只让标准误错；在这里，ε 的方差如果随某个变量变化，系数和预测概率都会错，因为系数本来就是以那个被固定的标准差为单位的。所以 Logit 加稳健标准误救不了异方差，系数本身已经错了，Freedman 专门批评过这种做法。Logit 和 Probit 之间的选择通常影响不大，两者只在极端概率上有差别。
3. **不存在被忽略的异质性**：这是非线性模型特有的问题。在最小二乘里，漏掉一个和 X 无关的变量不造成偏误；在 Logit 和 Probit 里，它会增大剩余方差，所有系数都向 0 收缩。平均边际效应受的影响小得多。这就是不能跨模型比较系数的原因。
4. **观测独立**：国家对–年份数据里，同一对国家的各年高度相关；冲突一旦开始常持续多年，结束后一段时间内又不太可能再爆发。不处理这种时间依赖，标准误偏小，系数也可能偏。Beck、Katz 和 Tucker 建议加入距上次事件的年数及其样条，Carter 和 Signorino 建议用这个年数的一次、二次、三次项。
5. **极大似然估计存在而且可靠**：不能有完全分离，事件也不能太少。完全分离指某个变量或变量组合完美地预测了结果，比如样本里某类国家对从来没有发生过冲突；这时对应系数的估计值趋于无穷，软件要么不收敛，要么报出巨大的系数和标准误，Stata 则直接删掉变量和相关观测。事件很少时，极大似然在有限样本里会系统性地低估事件概率。
6. **没有完全共线**：同最小二乘。

## 稳健性检验：做什么，为什么

| 检验 | 检查哪条假设 | 怎么做，怎么看 |
|---|---|---|
| 换成线性概率模型 | 连接函数和分布设定 | 用最小二乘加稳健标准误重估，把系数和 Logit 的平均边际效应放在一起比。两者接近，说明结论不依赖 S 形曲线的具体形状；差别很大时，通常是很多观测处在概率的极端区域，或者存在分离 |
| Logit 与 Probit 互换 | 误差分布的形状 | 比平均边际效应，不比系数。两者几乎总是很接近；不接近时，检查极端观测 |
| 控制时间依赖 | 观测独立 | 在国家对–年份或国家–年份的事件数据里，加入距上次事件的年数及其样条或三次多项式。核心系数随之大幅变化，说明原来的结果混进了事件在时间上的聚集 |
| 聚类标准误 | 观测独立 | 按国家对或国家聚类。显著性比不聚类时下降很多，说明原来的推断过于乐观 |
| Firth 惩罚似然 | 分离、小样本偏误 | 在似然函数上加一个惩罚项，分离时也能得到有限的估计值，同时减小小样本偏误；Zorn 把它引入政治学。结果和普通 Logit 接近，说明原来的估计不受这些问题影响 |
| 罕见事件校正 | 事件很少时的有限样本偏误、按结果抽样 | King 和 Zeng 的方法修正系数和预测概率的偏误；如果数据是把全部事件和一部分非事件抽出来组成的，还要按总体中的事件比例做先验校正或加权。事件数以千计时，校正通常几乎不改变结果；只有几十个事件时差别可能很大 |
| 检查分离和被删掉的观测 | 极大似然估计存在 | 看有没有异常巨大的系数和标准误，看 Stata 是否提示某个变量完美预测并删掉了观测。被删掉的往往正是信息最多的观测 |
| 固定效应的处理方式 | 外生性：不随时间变化的遗漏变量 | 条件 Logit 只使用 Y 在组内有变化的单位，从来没发生过事件的国家对全部被删掉；关于国家对数据要不要加固定效应，Green、Kim 和 Yoon 与 Beck 和 Katz 的争论正围绕这一点。直接加单位哑变量的 Probit，在每个单位期数少时有偶发参数问题，估计有偏。可以用带固定效应的线性概率模型做对照 |
| 换事件的编码门槛 | 测量 | 战争门槛在每年 25 人和 1000 人死亡之间切换，政变换用不同数据集。二分变量常常是从连续量切出来的，门槛不同，事件集合就不同 |
| 分离图与样本外预测 | 整体设定 | Greenhill、Ward 和 Sacks 的分离图，或交叉验证下的预测表现，用来看模型能否把发生和不发生的观测区分开。拟合很差时，对系数的解读也要更谨慎 |

## 怎么读结果

- **系数的符号**：可以直接读，正号表示 X 增加时事件更可能发生。
- **系数的大小**：Logit 系数是对数几率的变化，Probit 系数是潜在倾向以其标准差为单位的变化，两者都不是概率的变化。
- **几率比**：`exp(β)`。几率比为 2 是几率翻倍，不是概率翻倍。概率小时两者接近：基线概率 2%，几率翻倍后概率约 3.9%；基线概率 40%，几率翻倍后概率是 57%，远没有翻倍。
- **平均边际效应还是均值处的边际效应**：优先报告平均边际效应。均值处的边际效应算的是一个平均观测，而这个观测可能根本不存在：民主哑变量的均值是 0.4，没有哪个国家是 40% 民主的。而且曲线是弯的，均值处的效应一般不等于效应的平均。
- **预测概率**：最直观的报告方式。选几个有实际意义的情形，比如两个民主国家组成的国家对，和一个民主、一个非民主的国家对，其他变量取典型值，报告预测概率及其置信区间；置信区间用 delta 方法或模拟得到，King、Tomz 和 Wittenberg 系统地讲过这种做法。
- **交互项**：概率尺度上的交互效应不等于乘积项的系数，两者连符号都可能不同；而且即使模型里没有乘积项，由于曲线是弯的，一个变量的边际效应也会随其他变量变化。Ai 和 Norton 讨论过这一点。要用不同取值下边际效应的差来读交互。
- **伪 R²**：McFadden 伪 R² 之类，和最小二乘的 R² 不是一回事，数值通常小得多，只适合在同一数据的不同模型之间比较。
- **表格里要找的信息**：观测数、事件数、标准误类型、有没有控制时间依赖、边际效应在哪里取值。

读的时候可以这样说：其他变量取样本观测值时，同盟关系使一对国家爆发争端的概率平均降低 1.2 个百分点；基线概率约 3%，相当于降低四成；按国家对聚类后仍在 5% 水平显著。

## 常见陷阱

- **把系数当成概率变化**：系数 0.5 既不是概率增加 50%，也不是增加 0.5 个百分点。
- **跨模型比较系数**：加入一个控制变量，即使它和 X 无关，X 的系数也会变大，因为剩余方差变了。所以“加入控制变量后系数从 0.8 变成 1.1”不能读成混杂因素原本压低了效果。跨模型、跨子样本比较应当用平均边际效应或预测概率，Mood 对此有详细讨论。在民主和非民主子样本里分别估计、再比较两个 Logit 系数，也受剩余方差不同的干扰。
- **把几率比读成风险比**：几率比为 3 不是可能性的三倍，只有事件罕见时两者才接近。
- **交互项只看乘积项的系数和星号**。
- **没注意 Stata 因分离自动删掉了观测**：样本已经悄悄变了。
- **用稳健标准误应对异方差**：潜变量的异方差让系数本身错了，换标准误不解决问题。
- **事件很少却放很多解释变量**：每个变量分到的事件太少，估计不稳定，分离也更常见。
- **忘了固定效应 Logit 删掉了谁**：从没发生过事件的单位不参与估计，结论只适用于至少发生过一次事件的单位。

## 代码写法

R，模型用 glm，边际效应用 marginaleffects：

```r
# Logit 与 Probit
m_logit  <- glm(y ~ x + z, family = binomial(link = "logit"),  data = df)
m_probit <- glm(y ~ x + z, family = binomial(link = "probit"), data = df)
exp(coef(m_logit))                                     # 几率比

# 边际效应与预测概率
library(marginaleffects)
avg_slopes(m_logit)                                    # 平均边际效应 AME
slopes(m_logit, newdata = "mean")                      # 均值处的边际效应 MEM
predictions(m_logit, newdata = datagrid(x = c(0, 1)))  # 其他变量取均值或众数时的预测概率
avg_slopes(m_logit, vcov = ~country)                   # 按国家聚类的标准误

# 时间依赖：peace_yrs 是距上次事件的年数
m_t <- glm(y ~ x + z + peace_yrs + I(peace_yrs^2) + I(peace_yrs^3),
           family = binomial, data = df)

# 线性概率模型对照，带固定效应
library(fixest)
m_lpm <- feols(y ~ x + z | country + year, data = df, cluster = ~country)

# 条件 Logit：组内 y 没有变化的国家自动被剔除
library(survival)
m_cl <- clogit(y ~ x + z + strata(country), data = df)

# Firth 惩罚似然
library(logistf)
m_firth <- logistf(y ~ x + z, data = df)
```

Stata：

```stata
* Logit，按国家聚类
logit y x z, vce(cluster country)
margins, dydx(*)                            // 平均边际效应 AME
margins, dydx(*) atmeans                    // 均值处的边际效应 MEM
margins, at(x = (0 1))                      // x 取 0 和 1 时的平均预测概率
logit y x z, or vce(cluster country)        // 报告几率比

* Probit
probit y x z, vce(cluster country)
margins, dydx(*)

* 交互项：c. 表示连续变量，i. 表示分类变量，## 同时放入主效应和乘积项
logit y c.x##i.d z, vce(cluster country)
margins d, dydx(x)                          // d 的每个取值上 x 的平均边际效应

* 固定效应：条件 Logit
clogit y x z, group(country)

* Firth 惩罚似然，需先 ssc install firthlogit
firthlogit y x z
```

Python：

```python
import numpy as np
import statsmodels.formula.api as smf

# df 需先去掉相关变量的缺失值，groups 才能和样本对齐
m_logit = smf.logit("y ~ x + z", data=df).fit(cov_type="cluster", cov_kwds={"groups": df["country"]})
np.exp(m_logit.params)                               # 几率比
print(m_logit.get_margeff(at="overall").summary())   # 平均边际效应 AME
print(m_logit.get_margeff(at="mean").summary())      # 均值处的边际效应 MEM

m_probit = smf.probit("y ~ x + z", data=df).fit()
```

在作者代码里认 Logit 和 Probit，看这几处：R 的 `glm(` 里写着 `family = binomial` 或 `binomial(link = "probit")`，`feglm(` 配 binomial 族，以及 `clogit(`、`logistf(`；Stata 的 `logit`、`logistic`、`probit`、`xtlogit`、`xtprobit`、`clogit`、`relogit`、`firthlogit`，后面常跟 `margins`；Python 的 `smf.logit`、`smf.probit`、`sm.Logit`、`sm.Probit`。另外，一组 `peace_yrs`、`t`、`t2`、`t3` 或样条变量通常是在控制时间依赖；`, or` 选项、`logistic` 命令或 `exp(coef(...))` 说明作者报告的是几率比。

## 延伸阅读

- Long, J. Scott. 1997. *Regression Models for Categorical and Limited Dependent Variables*. Sage. 二分类结果一章。
- King, Gary, Michael Tomz, and Jason Wittenberg. 2000. "Making the Most of Statistical Analyses: Improving Interpretation and Presentation." *American Journal of Political Science* 44 (2).
- Ai, Chunrong, and Edward C. Norton. 2003. "Interaction Terms in Logit and Probit Models." *Economics Letters* 80 (1).
- Mood, Carina. 2010. "Logistic Regression: Why We Cannot Do What We Think We Can Do, and What We Can Do About It." *European Sociological Review* 26 (1).
- King, Gary, and Langche Zeng. 2001. "Logistic Regression in Rare Events Data." *Political Analysis* 9 (2).
- Zorn, Christopher. 2005. "A Solution to Separation in Binary Response Models." *Political Analysis* 13 (2).
- Beck, Nathaniel, Jonathan N. Katz, and Richard Tucker. 1998. "Taking Time Seriously: Time-Series-Cross-Section Analysis with a Binary Dependent Variable." *American Journal of Political Science* 42 (4).
- Carter, David B., and Curtis S. Signorino. 2010. "Back to the Future: Modeling Time Dependence in Binary Data." *Political Analysis* 18 (3).
