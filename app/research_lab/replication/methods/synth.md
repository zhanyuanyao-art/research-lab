---
key: synth
name: 合成控制
english: Synthetic Control Method, SCM
summary: 用一组未受处理单位的加权平均，拼出一个事前走势与处理单位几乎重合的合成单位，事后两者之间的差距就是处理的效果。
---

## 适用的问题

只有一个或少数几个单位受到处理，结果是这个单位在总体层面上的时间序列，研究关心的正是这个案例本身。比如：

- 一个国家经历民主化、政变或政权更替之后，经济增长和政治暴力怎么变化；
- 一个国家加入货币联盟、退出某个区域组织，或者单独受到一轮制裁之后，贸易和产出怎么变化；
- 一个地区长期遭受恐怖袭击或武装冲突，经济上损失了多少。

比较政治里有两个经典用法：Abadie 和 Gardeazabal 估计巴斯克地区恐怖主义的经济代价；Abadie、Diamond 和 Hainmueller 估计两德统一对西德人均 GDP 的影响。

这类问题过去靠比较案例研究：挑一两个看起来相似的国家做对照，挑哪个全凭研究者判断。双重差分可以用很多对照单位，但要求处理单位与对照组的平均走势平行，而单个国家的走势往往和其他国家的平均并不平行。合成控制把挑对照这一步，变成一个有明确标准、可以复核的优化问题。

## 核心思想

没有哪一个国家能单独代表处理国的反事实，但几个国家按一定比例混合起来也许可以。合成控制在处理之前的数据上找一组权重，让供体池里各单位的加权平均，在事前的结果走势和主要特征上尽量贴近处理单位。如果事前很长一段时间都贴得很紧，就有理由相信：没有处理的话，事后两者也会继续贴着走。事后两条线分开的部分，就是处理的效果。

一个数字例子，处理发生在第 3 年之后，供体池里其余国家的权重都是 0：

| 年份 | 处理国 A | 供体 B | 供体 C | 合成 A = 0.6·B + 0.4·C | 差距 |
|---|---|---|---|---|---|
| 1 | 10.0 | 9.0 | 11.5 | 10.0 | 0 |
| 2 | 11.0 | 10.0 | 12.5 | 11.0 | 0 |
| 3 | 12.0 | 11.0 | 13.5 | 12.0 | 0 |
| 4 | 12.5 | 12.0 | 14.5 | 13.0 | −0.5 |
| 5 | 13.0 | 13.0 | 15.5 | 14.0 | −1.0 |

B 总比 A 低 1，C 总比 A 高 1.5，单独看谁都不是好对照；按 6 比 4 混合后，前三年与 A 完全重合。处理之后，合成 A 每年涨 1，A 只涨 0.5，效果在第 4 年是 −0.5，第 5 年扩大到 −1.0。真实数据里事前拟合不会这么完美，拟合到什么程度才可信，是后面检验的重点。

## 模型设定

**选权重**：

```text
W* = argmin_W  (X₁ − X₀·W)′ · V · (X₁ − X₀·W)
约束：w_j ≥ 0，w₂ + w₃ + … + w_{J+1} = 1

τ̂_1t = Y_1t − Σ_{j=2}^{J+1} w_j* · Y_jt，   t > T₀
```

- 单位 1 是处理单位，单位 2 到 J+1 是 J 个供体，处理发生在第 T₀ 期之后；
- `X₁`：k × 1 向量，处理单位的 k 个事前预测变量，比如投资率、贸易开放度的事前均值，以及若干事前年份的结果值；
- `X₀`：k × J 矩阵，各供体相同的预测变量；
- `W`：J 个供体的权重；
- `V`：k × k 对角矩阵，代表各预测变量的重要性。通常选 V 让事前各期结果的拟合误差最小，或者把事前时期分成训练段和验证段做交叉验证；
- `Y_1t`、`Y_jt`：处理单位和供体 j 在第 t 期的结果；
- `τ̂_1t`：第 t 期的效果估计，每个事后时期各有一个。

**拟合质量**：

```text
RMSPE_pre  = √[ (1/T₀) · Σ_{t ≤ T₀} (Y_1t − Σ_j w_j*·Y_jt)² ]
RMSPE_post = √[ (1/(T − T₀)) · Σ_{t > T₀} (Y_1t − Σ_j w_j*·Y_jt)² ]
r = RMSPE_post / RMSPE_pre
```

- `T`：总期数，`T − T₀` 是事后期数；
- `RMSPE_pre`：事前的均方根预测误差，衡量事前贴得多紧；
- `RMSPE_post`：事后的同一指标，衡量事后分开多远；
- `r`：两者之比，越大说明事后的偏离相对于事前的拟合噪声越突出。

**为什么权重非负、加总为 1**。这两条约束让合成单位落在供体的凸包之内，也就是说，它只能是已有单位之间的插值：

- **不外推**：普通回归也可以改写成对照单位的加权平均，权重同样加总为 1，但可以为负，也可以大于 1，于是能拼出一个现实中不存在的组合，在数据支撑之外外推，而且读者看不出来；
- **透明**：权重通常只落在少数几个供体上，读者可以直接看合成单位由哪些国家组成，用案例知识判断这个组合说不说得通；
- **防过拟合**：约束本身是一种正则化，减少了用许多单位的噪声凑出完美拟合的可能。

代价是：处理单位如果在某个维度上是极端值，比如收入最高的国家，任何非负加权平均都够不到它，事前拟合必然很差。这时不应硬用合成控制，可以先对结果去均值再拟合，或改用下文的增强合成控制。

**插值偏差**。待在凸包之内也有风险。如果结果与特征之间的关系是非线性的，把一个很富的国家和一个很穷的国家平均起来，并不等于一个中等收入国家。应对办法是把供体池限制在与处理单位本来就相近的单位里，或者用 Abadie 和 L'Hour 的惩罚性合成控制，让权重偏向与处理单位距离近的供体。

**两种扩展**：

- **合成双重差分**：Arkhangelsky 等人同时给单位和时期加权。单位权重像合成控制一样，让供体的事前走势接近处理单位，但允许两者之间有一个固定的水平差，这一点借自双重差分；时期权重让与事后更相似的那些事前年份分量更重；最后做一次加权的双向固定效应回归。它适合多个单位同时受处理的情形，也不要求事前水平完全重合。
- **增强合成控制**：Ben-Michael、Feller 和 Rothstein 的出发点是，事前拟合不完美时，合成控制的估计有偏。他们先用一个结果模型，常用岭回归，估计剩下的不平衡会造成多大偏差，再把它从估计值里扣掉。代价是允许少量负权重，即有节制的外推，外推的程度由正则化控制。

此外，Xu 的广义合成控制用交互固定效应模型，从全部对照单位中估计潜在因子，处理单位多、处理时间交错时用起来更方便。

## 识别假设

合成控制背后的统计模型是一个因子模型：

```text
Y_it(0) = δ_t + θ_t′·Z_i + λ_t′·μ_i + ε_it
```

- `Y_it(0)`：单位 i 在第 t 期不受处理时的潜在结果；
- `δ_t`：所有单位共同的时间冲击；
- `Z_i`：观测到的特征，`θ_t` 是它们随时间变化的作用；
- `μ_i`：看不见的单位特征，即因子载荷；`λ_t` 是随时间变化的共同因子；
- `ε_it`：期望为零的随机扰动。

当 `λ_t` 不随时间变化时，`λ_t′·μ_i` 就只是单位固定效应，模型退化为双重差分的世界；合成控制允许看不见的特征以随时间变化的方式起作用，这是它比双重差分宽松的地方。它需要的条件是：

1. **事前长期贴合**：合成单位在很长的事前时期里都贴着处理单位，它在看不见的 `μ_i` 上也就近似一致。事前期数短、或者结果噪声大时，贴合可能只是拟合了噪声，事后不再成立。
2. **处理单位在凸包之内或附近**：否则事前拟合做不好，偏差无从控制。
3. **没有外溢**：处理不影响供体。邻国、贸易伙伴往往会受波及，合成单位的轨迹随之被带动，效果被低估或高估。
4. **供体没有自己的大冲击**：事后某个高权重供体遭遇战争、危机，或者也采取了同类政策，合成单位就不再代表没有处理时的反事实。
5. **没有预期效应**：政策提前宣布时，T₀ 应设在宣布之时；否则事前拟合会把预期带来的反应吸收进去，效果被低估。
6. **关系稳定**：让权重在事前成立的那种结构关系，在事后继续成立。

## 稳健性检验：做什么，为什么

每一项检验都对应上面的某条假设。读论文时，可以对照着看作者做了哪些、漏了哪些。

| 检验 | 检查哪条假设 | 怎么做，怎么看 |
|---|---|---|
| 事前拟合 | 事前长期贴合、凸包 | 画处理单位和合成单位的走势图，报告事前 RMSPE 和预测变量平衡表。事前轨迹应当紧贴；事前就分得开，事后的差距没有意义 |
| 空间安慰剂 | 效果是否只是偶然 | 依次把每个供体当作处理单位，用其余单位去合成它，得到一组安慰剂差距曲线。处理单位的事后差距应当在这组曲线里显得极端；很多供体都有同样大的偏离，说明这种幅度的偏离本来就常见 |
| RMSPE 比值与排序 p 值 | 同上，并校正拟合好坏的差别 | 算每个单位事后与事前 RMSPE 之比，看处理单位排第几，排第一时 `p = 1/(J+1)`。比值优于只看事后差距，因为事前就拟合不好的安慰剂单位，事后差距大也说明不了什么 |
| 时间安慰剂 | 没有预期效应、没有过拟合 | 把处理时间人为提前几年，只用这之前的数据选权重。假处理时点到真处理时点之间不该出现差距；出现了，说明要么有预期，要么合成单位只是在拟合噪声，到样本外就失效 |
| 留一法 | 结果不依赖单个供体 | 依次去掉每个权重为正的供体重新合成。估计应当大体不变；去掉某一国效果就消失，要去查那个国家事后是否有自己的冲击 |
| 调整供体池 | 没有外溢、供体没有自己的冲击、插值偏差 | 去掉邻国、主要贸易伙伴、同期遭遇类似冲击的国家，或者只保留与处理单位相近的国家。结论应当稳定 |
| 换预测变量设定 | 设定搜索 | 只用事前结果、加入协变量、换用不同的事前年份组合。合成控制的设定自由度很大，只在某一种设定下显著，就要怀疑是挑出来的 |
| 增强合成控制、合成双重差分 | 事前拟合不完美带来的偏差 | 用做偏差校正或允许水平差的方法重估。结论一致，说明效果不是拟合缺口造成的 |

一个排序 p 值的例子：处理国事前 RMSPE 为 0.2，事后为 1.0，比值为 5；19 个供体各自做安慰剂，比值都小于 5。处理国在 20 个单位中排第一，`p = 1/20 = 0.05`。

关于推断要说清一点：这个 p 值不是抽样意义上的 p 值，它回答的是，假如处理被随机分给了某一个单位，看到这么极端的结果有多大可能。供体池只有 15 个单位时，最小可能的 p 值是 1/16 ≈ 0.06，永远到不了 0.05。这不是方法的缺陷：只有一个处理单位，能做的统计推断本来就有限。

## 怎么读结果

- **估计的是什么**：这一个单位在每一个事后时期的效果。可以把事后各期平均起来，但仍然只是这个案例的效果，不能推广成一般规律。
- **走势图**：横轴是时间，两条线分别是处理单位和合成单位，处理时点画一条竖线。先看事前贴不贴，再看事后分不分。
- **差距图与安慰剂图**：处理单位的差距曲线叠在所有安慰剂曲线上，事后应当明显跑出这一片曲线。
- **权重表**：合成单位由哪几个国家组成、各占多少；用案例知识判断这个组合是否合理。
- **预测变量平衡表**：处理单位、合成单位、全部供体平均三列并排，合成单位应当比简单平均更接近处理单位。

读的时候可以这样说：以 J 个国家为供体池，合成单位主要由 B 和 C 构成，事前 RMSPE 为 0.2；处理后十年，处理国人均 GDP 平均比合成单位低 x；处理国事后与事前 RMSPE 之比在全部 J+1 个单位中排第一，排序 p 值为 `1/(J+1)`；去掉任何一个正权重供体，结论都不变。

## 常见陷阱

- **事前拟合不好仍然报结果**：事前就分开的两条线，事后的差距不能解读为效果。
- **把每个事前年份的结果都单独当作预测变量**：这时其他协变量的权重会被压到接近 0，加了等于没加；反过来，只挑几个事前年份，又给了研究者选择空间。Ferman、Pinto 和 Possebom 指出，不同设定可能给出差别很大的结果，应当报告多种设定。
- **事前期太短**：几年的贴合说明不了看不见的特征也一致。
- **供体池被污染**：供体受到外溢，或者事后发生了自己的大事。
- **把排序 p 值当成普通 p 值**，或者在供体很少时仍然追求 0.05。
- **事后窗口拉得太长**：越往后，别的事件累积得越多，差距越难全部归于处理。
- **用处理之后才测量的变量做预测变量**：它们可能已经被处理影响。
- **处理时间定错**：宣布和实施之间隔了很久，以实施为准会漏掉预期效应。
- **只给一张走势图**：不报安慰剂分布、权重表和留一法，读者无从判断结果有多特别。

## 代码写法

R，经典的 Synth 包，包装了安慰剂检验的 tidysynth，以及合成双重差分的 synthdid。synthdid 和 augsynth 不在 CRAN，需用 remotes::install_github("synth-inference/synthdid") 这样从 GitHub 安装：

```r
library(Synth)
# 数据须为 data.frame；unit_id 为数值，unit_name 为字符
dp <- dataprep(
  foo = df,
  predictors = c("invest", "trade"),            # 取事前均值的预测变量
  predictors.op = "mean",
  time.predictors.prior = 1975:1989,
  special.predictors = list(                    # 若干事前年份的结果
    list("gdp", 1980, "mean"),
    list("gdp", 1985, "mean")
  ),
  dependent = "gdp",
  unit.variable = "unit_id",
  unit.names.variable = "unit_name",
  time.variable = "year",
  treatment.identifier = 7,
  controls.identifier = c(1:6, 8:20),
  time.optimize.ssr = 1975:1989,                # 在这些年份上最小化结果的拟合误差，借此选 V
  time.plot = 1975:2005
)
so <- synth(data.prep.obj = dp)
synth.tab(dataprep.res = dp, synth.res = so)    # 权重表与预测变量平衡表
path.plot(synth.res = so, dataprep.res = dp)    # 走势图
gaps.plot(synth.res = so, dataprep.res = dp)    # 差距图

# tidysynth：顺带生成全部空间安慰剂
library(tidysynth)
out <- df |>
  synthetic_control(outcome = gdp, unit = country, time = year,
                    i_unit = "A", i_time = 1989, generate_placebos = TRUE) |>   # tidysynth 的 i_time 是处理前最后一年，与 Stata 的 trperiod 不同
  generate_predictor(time_window = 1975:1989, invest = mean(invest, na.rm = TRUE)) |>
  generate_predictor(time_window = 1985, gdp_1985 = gdp) |>
  generate_weights(optimization_window = 1975:1989) |>
  generate_control()
out |> plot_placebos()        # 空间安慰剂
out |> plot_mspe_ratio()      # 事后与事前 MSPE 之比，排序与 RMSPE 之比相同
out |> grab_significance()    # 排序 p 值

# 合成双重差分：treated 在处理单位的处理后时期取 1
library(synthdid)
setup <- panel.matrices(df, unit = "country", time = "year", outcome = "gdp", treatment = "treated")
tau <- synthdid_estimate(setup$Y, setup$N0, setup$T0)
sqrt(vcov(tau, method = "placebo"))
```

Stata：

```stata
tsset unit_id year
* 预测变量：投资与贸易的事前均值，加上 1980 和 1985 年的结果
synth gdp invest trade gdp(1980) gdp(1985), trunit(7) trperiod(1990) figure
* 空间安慰剂与基于 RMSPE 比值的 p 值，需另装 synth_runner
synth_runner gdp invest trade gdp(1980) gdp(1985), trunit(7) trperiod(1990)
* 合成双重差分
sdid gdp unit_id year treated, vce(placebo)
```

Python，手写核心优化，便于看清方法本身：

```python
import numpy as np
from scipy.optimize import minimize

# Y0_pre：事前各期 × 供体的矩阵；y1_pre：处理单位的事前序列；只用事前结果做拟合
def loss(w):
    return np.sum((y1_pre - Y0_pre @ w) ** 2)

J = Y0_pre.shape[1]
res = minimize(loss, x0=np.full(J, 1 / J), method="SLSQP",
               bounds=[(0, 1)] * J,
               constraints=[{"type": "eq", "fun": lambda w: np.sum(w) - 1}])
w = res.x                       # 非负、加总为 1 的权重
gap = y1_post - Y0_post @ w     # 事后各期的效果估计
```

在作者代码里认合成控制，看这几处：`dataprep`、`synth`、`synth.tab`、`path.plot`、`gaps.plot`；`synthetic_control`、`generate_weights`；Stata 的 `synth ..., trunit() trperiod()` 和 `synth_runner`；`synthdid_estimate`、`sdid`、`augsynth`、`gsynth`；手写时则是一个权重非负、加总为 1 的约束优化，外加一个对每个供体循环重跑、计算事前事后 RMSPE 的安慰剂循环。

## 延伸阅读

- Abadie, Alberto, and Javier Gardeazabal. 2003. "The Economic Costs of Conflict: A Case Study of the Basque Country." *American Economic Review* 93 (1).
- Abadie, Alberto, Alexis Diamond, and Jens Hainmueller. 2010. "Synthetic Control Methods for Comparative Case Studies: Estimating the Effect of California's Tobacco Control Program." *Journal of the American Statistical Association* 105 (490).
- Abadie, Alberto, Alexis Diamond, and Jens Hainmueller. 2015. "Comparative Politics and the Synthetic Control Method." *American Journal of Political Science* 59 (2).
- Abadie, Alberto. 2021. "Using Synthetic Controls: Feasibility, Data Requirements, and Methodological Aspects." *Journal of Economic Literature* 59 (2).
- Arkhangelsky, Dmitry, Susan Athey, David A. Hirshberg, Guido W. Imbens, and Stefan Wager. 2021. "Synthetic Difference-in-Differences." *American Economic Review* 111 (12).
- Ben-Michael, Eli, Avi Feller, and Jesse Rothstein. 2021. "The Augmented Synthetic Control Method." *Journal of the American Statistical Association* 116 (536).
- Xu, Yiqing. 2017. "Generalized Synthetic Control Method: Causal Inference with Interactive Fixed Effects Models." *Political Analysis* 25 (1).
- Cunningham, Scott. 2021. *Causal Inference: The Mixtape*. Yale University Press. 合成控制一章。
