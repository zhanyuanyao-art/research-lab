---
key: did
name: 双重差分
english: Difference-in-Differences, DiD
summary: 用处理组前后的变化，减去对照组同期的变化，扣掉大家共同经历的趋势，剩下的就是政策的效果。
---

## 适用的问题

一项政策、冲击或事件只落在一部分单位身上，而且这些单位在事件前后都被观察到。比如：

- 一部分国家加入了某个条约，另一部分没有；
- 几个省份先推行了改革，其余省份晚几年或者一直没有推行；
- 一场危机只波及部分地区。

我们真正想问的是一个看不到的反事实：如果没有这项政策，受影响的单位现在会是什么样子？双重差分的办法，是用没受影响的单位同期的变化，来代替这个看不到的反事实。

## 核心思想

先看两种只做一次差分的比较，各自错在哪里：

- **只比处理组的前后**：前后的差里，既有政策的作用，也有这段时间里本来就会发生的变化，比如经济周期、全球局势。两者分不开。
- **只比事后的处理组和对照组**：两组的差里，既有政策的作用，也有两组原本就存在的差别。被选中推行改革的省份，本来就可能更富。

双重差分把两次差分叠在一起：

```text
效果 = (处理组事后 − 处理组事前) − (对照组事后 − 对照组事前)
```

第一次差分，组内前后相减，消掉了两组之间不随时间变化的差别；第二次差分，两组的变化相减，消掉了两组共同经历的时间冲击。

一个数字例子：

| | 事前 | 事后 | 变化 |
|---|---|---|---|
| 处理组 | 10 | 16 | +6 |
| 对照组 | 8 | 11 | +3 |
| 双重差分 | | | +3 |

处理组涨了 6，但其中 3 是对照组也涨了的部分，也就是没有政策也会发生的变化；政策本身的效果是 3。

这里有一个关键前提：对照组的变化，确实能代表处理组在没有政策时的变化。这就是下面的平行趋势假设，双重差分的一切检验都围绕它展开。

## 模型设定

**两组两期**，用回归写出来：

```text
Y_it = α + β·Treat_i + γ·Post_t + δ·(Treat_i × Post_t) + ε_it
```

- `Treat_i`：单位 i 属于处理组取 1，否则取 0；β 是两组事前就有的差别。
- `Post_t`：时期 t 在政策之后取 1；γ 是对照组前后的变化，代表共同趋势。
- `Treat_i × Post_t`：处理组在政策之后取 1；它的系数 δ 就是双重差分估计值，和上面数字例子算出来的 3 完全相同。

**多期面板**，用双向固定效应：

```text
Y_it = α_i + λ_t + δ·D_it + X_it′θ + ε_it
```

- `α_i` 是每个单位自己的截距，吸收了 Treat_i 以及一切不随时间变化的单位特征；
- `λ_t` 是每个时期自己的截距，吸收了 Post_t 以及一切同时影响所有单位的冲击；
- `D_it` 在单位 i 于时期 t 已经受到处理时取 1；
- `X_it` 是控制变量，只应放政策之前就确定的变量，原因见常见陷阱。

**事件研究**，把效果按距离政策的时间展开：

```text
Y_it = α_i + λ_t + Σ_{k ≠ −1} δ_k · 1[t − E_i = k] + ε_it
```

- `E_i` 是单位 i 开始受处理的时期，`t − E_i` 是相对时间；
- 以政策前一期 k = −1 为基准，不放进回归；
- k < 0 的 δ_k 检查事前趋势，应当接近 0；k ≥ 0 的 δ_k 描述效果随时间怎么变化。

## 识别假设

1. **平行趋势**：如果没有政策，处理组和对照组的结果会平行变化。这条假设说的是事后看不到的反事实，所以永远无法直接检验；事前的趋势是否平行只是间接证据。
2. **没有预期效应**：单位不会因为预料到政策，在政策生效前就提前改变行为。如果政策提前宣布，事前就已经有反应，基准期就被污染了。
3. **不外溢**：一个单位受处理，不影响对照组的结果。邻国加入条约后，没加入的国家也可能跟着调整，这时对照组就不再是干净的反事实。
4. **没有同时发生、只打在处理组身上的其他冲击**：如果改革省份同年还换了领导人，双重差分分不清是哪件事起的作用。
5. **处理时间交错时，效果在不同批次和不同时期之间大致相同**：这是双向固定效应的额外要求。各单位在不同年份陆续受处理时，双向固定效应会把已经受处理的单位当作后来者的对照；如果效果随时间增长，这些比较会带进错误的符号，加权平均甚至可能和每个真实效果的方向都相反。

## 稳健性检验：做什么，为什么

每一项检验都对应上面的某条假设。读论文时，可以对照着看作者做了哪些、漏了哪些。

| 检验 | 检查哪条假设 | 怎么做，怎么看 |
|---|---|---|
| 事件研究图 | 平行趋势、没有预期效应 | 画出各期的 δ_k。政策前各期应当在 0 附近、没有明显的上升或下降趋势；如果政策前已经在涨，结果很可能只是延续了原有趋势 |
| 安慰剂检验 | 结果是否只是巧合或设定造成的 | 把政策时间人为提前，或者随机指定一批假的处理单位，重新估计。效果应当消失；如果假政策也有效果，说明模型在捕捉别的东西 |
| 换对照组、剔除部分样本 | 不外溢 | 去掉处理组的邻居，或者改用尚未受处理的单位做对照。结果稳定，说明外溢不是主要问题 |
| 加入单位各自的线性趋势 | 放宽平行趋势 | 允许每个单位有自己的线性趋势，只要求偏离各自趋势的部分平行。估计值变化很大时要警惕；但这种做法也可能把政策的动态效果一起吸收掉 |
| 三重差分 | 同期其他冲击 | 在处理地区内部再找一个不受政策影响的群体，多减一次，扣掉只打在处理地区的同期冲击 |
| 交错处理的新估计量 | 效果同质 | 用 Callaway 和 Sant'Anna、Sun 和 Abraham 等人的方法，只拿干净的对照组来比；也可以用 Goodman-Bacon 分解，看双向固定效应的结果靠哪些比较撑起来 |
| 聚类标准误 | 误差序列相关 | 同一单位各年的误差高度相关，普通标准误会严重低估不确定性。标准误应当聚类在分配处理的层级，比如省份；聚类很少时，用野自助法 |
| 敏感性分析 | 平行趋势被违反到什么程度结论才会变 | Rambachan 和 Roth 的方法给出一个界限：事后偏离平行趋势的幅度要多大，才能推翻结论 |
| 协变量平衡、加权或匹配 | 两组在影响趋势的特征上不同 | 如果处理组和对照组在一些会影响变化速度的特征上差别很大，就在这些事前特征上加权或匹配，让平行趋势在条件上更可信 |

需要强调一点：事前趋势检验通过，不等于平行趋势成立。事前样本少的时候，检验的统计功效很低，看不出问题不代表没有问题；而且只挑通过检验的设定来报告，本身也会让结果产生偏差。

## 怎么读结果

- **δ 的含义**：处理组受政策影响的平均效果，单位和 Y 相同。Y 取对数时，δ 约等于百分比变化，δ = 0.05 大约是 5%。
- **它估计的是谁的效果**：处理组的平均处理效应，不是任意一个单位被处理后的效果。处理组如果是自己选择加入的，结论不能直接外推到没加入的单位。
- **事件研究图**：横轴是距离政策的期数，纵轴是 δ_k 和置信区间。理想的图形是：政策前贴着 0 走，政策后开始偏离；效果是一下跳上去，还是逐年累积，也能从图上看出来。
- **表格里要找的信息**：观测数、单位数、期数、处理组单位数、固定效应、标准误聚类的层级。缺了聚类层级，就无法判断显著性是否可信。

读的时候可以这样说：控制单位和年份固定效应后，改革使处理省份的指标平均上升 δ 个单位；事前各期系数都接近 0，说明改革前两组走势相近。

## 常见陷阱

- **把事前趋势检验通过当成证明**：它只是必要条件的一个弱证据。
- **交错处理直接用双向固定效应**：效果随时间变化时，估计值可能严重偏，甚至符号反了。
- **控制了被政策影响的变量**：比如研究改革对增长的影响，却控制了改革之后的投资。这些坏控制会把政策效果的一部分挡掉。
- **标准误没聚类，或者聚类层级不对**：显著性会被夸大，这是早期双重差分研究里最常见的问题。
- **处理时间定错**：政策宣布和生效之间隔了很久，以生效日为准就会漏掉预期效应。
- **函数形式**：水平值平行和对数值平行一般不能同时成立；换一种取值方式，结论可能变化。
- **重复截面样本的构成变了**：每期调查的不是同一批人，处理组的人员构成在政策前后发生变化，就会混进构成效应。

## 代码写法

R，常用 fixest 和 did 两个包：

```r
library(fixest)
# 两组两期或多期面板：处理哑变量 × 事后哑变量，单位和年份固定效应，标准误聚类在单位
m1 <- feols(y ~ treat:post | unit + year, data = df, cluster = ~unit)

# 事件研究：rel_year 是距处理的年数，-1 作基期；从未受处理的单位把 rel_year 设成 -1000，一并作基期
m2 <- feols(y ~ i(rel_year, ref = c(-1, -1000)) | unit + year, data = df, cluster = ~unit)
iplot(m2)

# 交错处理：Sun 和 Abraham 的估计量，first_treat 是开始受处理的年份
# 从未受处理的单位 first_treat 记成样本期之外的数，如 10000；下面的 did 包则要记 0
m3 <- feols(y ~ sunab(first_treat, year) | unit + year, data = df, cluster = ~unit)

# 交错处理：Callaway 和 Sant'Anna，从未受处理的单位 first_treat 记为 0
library(did)
cs <- att_gt(yname = "y", tname = "year", idname = "unit", gname = "first_treat", data = df)
aggte(cs, type = "dynamic")
```

Stata：

```stata
* 双向固定效应
reghdfe y c.treat#c.post, absorb(unit year) vce(cluster unit)

* Stata 17 起的官方命令
xtset unit year
xtdidregress (y) (treated), group(unit) time(year)

* 交错处理：Callaway 和 Sant'Anna
csdid y, ivar(unit) time(year) gvar(first_treat)
```

Python：

```python
import pyfixest as pf
m = pf.feols("y ~ treat:post | unit + year", data=df, vcov={"CRV1": "unit"})
```

在作者代码里认双重差分，看这几处：处理变量和事后变量的交互项，比如 `treat:post`、`treat*post`、Stata 的 `##`；单位和时间两套固定效应；`att_gt`、`sunab`、`csdid`、`did_multiplegt` 这些专门的命令。

## 延伸阅读

- Angrist, Joshua D., and Jörn-Steffen Pischke. 2009. *Mostly Harmless Econometrics*. Princeton University Press. 第 5 章。
- Cunningham, Scott. 2021. *Causal Inference: The Mixtape*. Yale University Press. 双重差分一章。
- Bertrand, Marianne, Esther Duflo, and Sendhil Mullainathan. 2004. "How Much Should We Trust Differences-in-Differences Estimates?" *Quarterly Journal of Economics* 119 (1).
- Goodman-Bacon, Andrew. 2021. "Difference-in-Differences with Variation in Treatment Timing." *Journal of Econometrics* 225 (2).
- Callaway, Brantly, and Pedro H. C. Sant'Anna. 2021. "Difference-in-Differences with Multiple Time Periods." *Journal of Econometrics* 225 (2).
- Sun, Liyang, and Sarah Abraham. 2021. "Estimating Dynamic Treatment Effects in Event Studies with Heterogeneous Treatment Effects." *Journal of Econometrics* 225 (2).
- Rambachan, Ashesh, and Jonathan Roth. 2023. "A More Credible Approach to Parallel Trends." *Review of Economic Studies* 90 (5).
- Roth, Jonathan, Pedro H. C. Sant'Anna, Alyssa Bilinski, and John Poe. 2023. "What's Trending in Difference-in-Differences? A Synthesis of the Recent Econometrics Literature." *Journal of Econometrics* 235 (2).
