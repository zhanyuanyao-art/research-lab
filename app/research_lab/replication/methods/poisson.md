---
key: poisson
name: 泊松回归
english: Poisson Regression
summary: 结果是事件次数时，让期望次数的对数成为解释变量的线性函数；系数取指数，读作这个变量多一单位，次数变成原来的几倍。
---

## 适用的问题

因变量是次数：0、1、2……，不能为负，常常集中在很小的数上，偶尔有很大的值。比如：

- 一个国家一年内卷入几起军事化争端；
- 一个国家一年内发生多少起恐怖袭击或抗议事件；
- 一国一年内对外签订几项双边投资协定；
- 联合国安理会一年内通过多少项涉及某一地区的决议。

这样的数据直接用最小二乘有几个问题：预测值可能为负；大量的 0 和右偏的长尾让误差严重异方差；先取对数又遇到 0 无法取对数。泊松回归直接为次数的期望值建模，避开这些问题。

它的用途不止于次数。只要因变量非负、期望值和解释变量是乘法关系，泊松回归就适用，哪怕因变量不是整数。国际贸易的引力模型里，双边贸易额有大量的 0，Santos Silva 和 Tenreyro 主张用泊松伪极大似然来估计，现在这已是引力模型的标准做法，常写作 PPML。

## 核心思想

泊松回归有两层，要分开理解。

**第一层是均值**：期望次数是解释变量的指数函数，`E(Y | X) = exp(X′β)`。取指数保证预测值永远为正；它也意味着各个因素的作用是乘法的：X 增加一单位，期望次数乘以 `exp(β)`，而不是加上一个固定的数。

一个数字例子：某类国家的抗议次数基线是每年 2 起，系数 β = 0.405，`exp(0.405) ≈ 1.5`。X 每增加一单位，期望次数乘以 1.5：

| X | 期望次数 | 比上一行多 |
|---|---|---|
| 0 | 2.00 | |
| 1 | 3.00 | +1.00 |
| 2 | 4.50 | +1.50 |
| 3 | 6.75 | +2.25 |

百分比变化始终是 50%，次数的绝对增量却随基线增大。所以泊松回归的系数读作比例变化最自然。

**第二层是分布**：假定次数服从泊松分布。这个分布只有一个参数，均值等于方差。这一条在政治事件数据里几乎从不成立：事件往往扎堆出现，方差远大于均值。

关键在于，第二层错了不会连累第一层。泊松回归求解的方程是：

```text
Σ_i [Y_i − exp(X_i′β̂)]·X_i = 0
```

- `Y_i`：第 i 个观测的实际次数；
- `X_i`：解释变量，含常数项；
- `exp(X_i′β̂)`：模型预测的期望次数，记作 μ̂_i；
- 整个方程的意思是：残差和每个解释变量都不相关。

这个方程里只有期望值，没有方差。只要 `E(Y | X) = exp(X′β)` 设定正确，方程在总体上就成立，解出的 β̂ 就一致，不管 Y 是否真的服从泊松分布、方差是否等于均值、甚至 Y 是不是整数。这是 Gourieroux、Monfort 和 Trognon 的结果，Wooldridge 反复强调它的实际意义：泊松回归是一个只依赖均值设定的稳健估计量。分布假设错了，代价只落在标准误上，换成稳健标准误就行。

## 模型设定

```text
Pr(Y_i = k | X_i) = exp(−μ_i)·μ_i^k / k!，k = 0, 1, 2, …
log μ_i = X_i′β
E(Y_i | X_i) = Var(Y_i | X_i) = μ_i
```

- `μ_i`：第 i 个观测的期望次数；
- `k!`：k 的阶乘；
- `log μ_i = X_i′β`：对数连接，期望次数的对数是解释变量的线性函数；
- 第三行是泊松分布的等离散性质：均值等于方差。

**发生率比和边际效应**：

```text
IRR_k = exp(β_k)
百分比变化 = 100·[exp(β_k) − 1]%
∂μ_i / ∂X_ki = μ_i·β_k
```

- `IRR_k`：发生率比，X_k 增加一单位，期望次数变成原来的几倍；
- 第三行是次数尺度上的边际效应，等于期望次数乘以系数，所以基线高的观测，效应的绝对值大。

模型有常数项时，`Σ_i μ̂_i = Σ_i Y_i`：预测次数的平均正好等于实际次数的平均。因此 X_k 的平均边际效应就是 `β̂_k · Ȳ`，Ȳ 是样本的平均次数。

**暴露量与偏移项**。各单位发生事件的机会不同时，次数不能直接比：人口五千万和五百万的国家各发生 10 起袭击，含义完全不同；一个国家对被观察了 10 年，另一个只有 2 年，也是如此。办法是对比率建模：

```text
log μ_i = log(t_i) + X_i′β
μ_i / t_i = exp(X_i′β)
```

- `t_i`：暴露量，比如人口、观察年数、两国的边界长度；
- `log(t_i)`：偏移项，系数固定为 1，不估计；
- 第二行说明，这时 `exp(β)` 读作 X 对发生率的倍数作用。

把 `log(t_i)` 当作普通解释变量放进去、让它的系数自由估计，就不再假定次数和暴露量严格成比例。这可以作为一项检验：估出来的系数接近 1，比例假定就是合理的。

**过度离散检验**。把方差写成更一般的形式：

```text
Var(Y_i | X_i) = μ_i + α·g(μ_i)
H0：α = 0
辅助回归：[(Y_i − μ̂_i)² − Y_i] / μ̂_i = α·g(μ̂_i) / μ̂_i + u_i
离散统计量 = [Σ_i (Y_i − μ̂_i)² / μ̂_i] / (N − K)
```

- `α`：离散参数，α > 0 表示过度离散；
- `g(μ_i)`：方差多出来那部分的形式，取 μ_i² 对应负二项回归的 NB2，取 μ_i 对应方差与均值成比例；
- `H0`：原假设，等离散；
- 辅助回归是 Cameron 和 Trivedi 的检验：用最小二乘、不加常数项，看 α 的 t 值；`u_i` 是这个辅助回归的误差；
- 离散统计量是 Pearson χ² 除以自由度，N 是观测数，K 是参数个数；接近 1 说明大致等离散，明显大于 1 就是过度离散。

**准泊松和稳健标准误**：

```text
准泊松：Var(Y_i | X_i) = φ·μ_i
SE_准泊松 = √φ̂ · SE_泊松
```

- `φ`：比例因子，用上面的离散统计量估计，记作 φ̂；
- `SE`：标准误。准泊松的系数和泊松回归完全相同，只是标准误放大 √φ̂ 倍。

离散统计量是 4 时，标准误翻倍，原来 z = 3 的系数变成 z = 1.5，不再显著。三明治式的稳健标准误不需要假定方差的具体形式，比准泊松更一般；面板数据里再按单位聚类。

**固定效应泊松**：

```text
log μ_it = α_i + λ_t + X_it′β
```

- `α_i`：单位固定效应，吸收单位不随时间变化的特征；
- `λ_t`：时期固定效应；
- `X_it`：随时间变化的解释变量。

泊松在这里有两个其他非线性模型少有的好性质。第一，直接放单位哑变量估计，和用 Hausman、Hall 与 Griliches 的条件似然估计，得到的 β 完全相同，没有偶发参数问题，期数短也不怕。第二，Wooldridge 证明，只要条件均值设定正确、解释变量严格外生，固定效应泊松就是一致的，不需要等离散，也不需要观测在时间上独立，配上聚类标准误即可。代价是：所有期都为 0 的单位不提供任何信息，会被删掉。

## 识别假设

泊松回归本身不是因果识别设计。下面是估计量需要的条件和每条失败的代价。

1. **条件均值设定正确**：`E(Y | X) = exp(X′β)`，包括指数的函数形式，以及该放的变量都放了。这是泊松回归唯一真正离不开的假设。效应其实不是乘法的，系数就只是某种近似。
2. **外生性**：没观测到的因素和 X 无关。遗漏变量、反向因果的后果和最小二乘相同，系数有偏。固定效应泊松需要严格外生：过去的 Y 不能影响今天的 X。前几年的冲突次数影响了今年的军购，军购就不是严格外生的。
3. **等离散**：方差等于均值。只有在使用模型默认标准误时才需要。违反时系数照样一致，但默认标准误偏小，显著性被夸大。政治事件数据几乎总是违反，所以应当默认使用稳健或聚类标准误。
4. **观测独立**：事件之间不互相引发。一次袭击引来报复，一场抗议点燃下一场，事件在时间和空间上传染，既造成过度离散，也让观测之间相关。推断上用聚类标准误；如果传染本身就是实质问题，就把上一期的次数放进模型。
5. **零和正数来自同一个过程**：泊松回归把 0 看作期望次数小时的自然结果。如果有一部分单位根本不可能发生事件，比如彼此没有任何接触的遥远国家对，均值设定就可能错了。但观测到的 0 比泊松分布预测的多，本身并不说明存在这样一个单独的过程；过度离散同样会制造大量的 0，见负二项一章。
6. **估计值存在**：在解释变量或固定效应的某种组合下 Y 全为 0 时，对应参数会发散，这是泊松版本的分离问题。高维固定效应下尤其常见，Stata 的 ppmlhdfe 会自动识别并剔除这些观测。

## 稳健性检验：做什么，为什么

| 检验 | 检查哪条假设 | 怎么做，怎么看 |
|---|---|---|
| 稳健或聚类标准误 | 等离散、观测独立 | 和默认标准误并列。稳健标准误大很多，说明存在过度离散或观测相关，默认标准误下的显著性不可信。这一步应当是默认做法，而不是额外的检验 |
| 过度离散检验 | 等离散 | 看离散统计量、Cameron–Trivedi 检验，或者和负二项回归的似然比检验。它回答的是默认标准误能不能用，不是泊松系数能不能用 |
| 改用负二项回归 | 方差形式与观测权重 | 系数和泊松接近，说明结果不依赖对方差形式的假定；差别很大，通常说明均值设定有问题，或者少数次数很大的观测在主导结果，这时更该相信只依赖均值设定的泊松，理由见负二项一章 |
| 比较预测和实际的 0 的比例 | 零的生成过程 | 预测的 0 比例是 `(1/N)·Σ_i exp(−μ̂_i)`，和实际比例对照；也可以画 rootogram，比较每个次数的预测频数和实际频数。差距大时先考虑过度离散，再考虑零膨胀 |
| 偏移项改为自由系数 | 次数和暴露量成比例 | 把 log 暴露量从偏移项换成普通解释变量。系数远离 1 时，次数和暴露量不成比例，偏移项的设定过强 |
| 加入单位固定效应 | 外生性：不随时间变化的遗漏变量 | 用固定效应泊松，看系数是否保持；同时报告因全为 0 而被删掉的单位数，说明结论适用于哪些单位 |
| 剔除极端值或截尾 | 结论不由少数大值主导 | 泊松的估计方程里，期望次数大的观测残差也大，对估计的影响更大。去掉伤亡最多的几起事件或最极端的几个国家年份，或者把次数截在某个上限，再重估 |
| 动态设定 | 观测独立、事件传染 | 加入上一期的次数。系数大幅变化，说明原来的效果部分来自事件的自我延续；但短面板里同时放固定效应和滞后因变量会产生偏误，要谨慎解读 |

## 怎么读结果

- **系数**：X 增加一单位，期望次数的对数变化 β。β 较小时约等于百分比变化：β = 0.1 约是 10%，精确值是 10.5%。
- **发生率比**：`exp(β)`。IRR = 1.5 是增加 50%；IRR = 0.6 是减少 40%；β = −0.7 时 IRR ≈ 0.50，次数减半。表格报告 IRR 时，检验的原假设是 1 而不是 0。
- **次数尺度上的效应**：用平均边际效应或典型取值下的预测次数来报告，比如盟国数量从 0 增加到 2 时，预测争端次数从每年 0.8 起变成 1.3 起。
- **解释变量取对数**：系数是弹性，X 增加 1%，期望次数约增加 β%。
- **有偏移项时**：读作发生率的变化，比如每百万人口的袭击次数。
- **交互项**：在对数尺度上，乘积项系数取指数是两个发生率比之比；在次数尺度上，交互效应还取决于基线，和 Logit 的情形类似，要用边际效应来读。
- **表格里要找的信息**：标准误类型、离散统计量或过度离散检验、有没有偏移项、固定效应以及被删掉的单位数。

读的时候可以这样说：控制人口和年份固定效应后，选举年的抗议次数平均是非选举年的 1.4 倍；标准误按国家聚类；改用负二项回归，发生率比为 1.37，基本不变。

## 常见陷阱

- **用默认标准误**：过度离散时显著性被夸大，这是计数模型里最常见的问题。
- **一见过度离散就放弃泊松**：过度离散只影响标准误，泊松的系数照样一致，换稳健标准误就够了。
- **把 IRR 读成次数的增加量**：IRR = 1.5 是增加 50%，不是多 1.5 次。
- **暴露量不同却没有偏移项**：人口大国自然事件多，结果混进了规模效应。
- **用 `log(Y + 1)` 的最小二乘代替**：加 1 是任意的，换成加 0.1 结论可能改变，计量单位一变结果也跟着变。
- **没注意固定效应泊松删掉了全零单位**：样本变成了至少发生过一次事件的单位，结论的适用范围要随之收窄。
- **看到很多 0 就上零膨胀模型**：大量的 0 往往只是过度离散的表现。
- **把对数尺度上的交互项系数当作次数尺度上的交互效应**。

## 代码写法

R，基本模型用 glm，面板和高维固定效应用 fixest：

```r
# 泊松回归与发生率比
m1 <- glm(y ~ x + z, family = poisson(link = "log"), data = df)
exp(coef(m1))

# 稳健标准误与聚类标准误
library(sandwich)
library(lmtest)
coeftest(m1, vcov = vcovHC(m1, type = "HC0"))
coeftest(m1, vcov = vcovCL(m1, cluster = ~country))

# 准泊松：系数相同，标准误乘以离散参数的平方根
m2 <- glm(y ~ x + z, family = quasipoisson, data = df)

# 暴露量：offset 的系数固定为 1
m3 <- glm(y ~ x + z + offset(log(pop)), family = poisson, data = df)

# 过度离散检验
library(AER)
dispersiontest(m1)              # 检验 Var = φ·μ 中 φ 是否大于 1
dispersiontest(m1, trafo = 2)   # 检验 Var = μ + α·μ² 中 α 是否大于 0

# 次数尺度上的平均边际效应
library(marginaleffects)
avg_slopes(m1)

# 固定效应泊松：所有年份都为 0 的国家会被自动剔除
library(fixest)
m4 <- fepois(y ~ x + z | country + year, data = df, cluster = ~country)
m5 <- fepois(y ~ x + z | country + year, offset = ~log(pop), data = df, cluster = ~country)
```

Stata：

```stata
* 默认标准误下可以做拟合优度检验
poisson y x z
estat gof

* 稳健标准误，报告发生率比
poisson y x z, irr vce(robust)

* 暴露量：exposure() 自动取对数作偏移项
poisson y x z, exposure(pop) vce(cluster country)
margins, dydx(*)                                  // 次数尺度上的平均边际效应

* 固定效应泊松
xtset country year
xtpoisson y x z, fe vce(robust)

* 高维固定效应，并自动处理分离问题，需先 ssc install ppmlhdfe
ppmlhdfe y x z, absorb(country year) vce(cluster country)
```

Python：

```python
import numpy as np
import statsmodels.api as sm
import statsmodels.formula.api as smf
import pyfixest as pf

# 泊松回归，稳健标准误
m1 = smf.poisson("y ~ x + z", data=df).fit(cov_type="HC0")
np.exp(m1.params)                                   # 发生率比

# 带偏移项、按国家聚类；df 需先去掉缺失值，groups 才能和样本对齐
m2 = smf.glm("y ~ x + z", data=df, family=sm.families.Poisson(),
             offset=np.log(df["pop"])).fit(cov_type="cluster", cov_kwds={"groups": df["country"]})

# 准泊松：用 Pearson χ² 估计比例因子
m3 = smf.glm("y ~ x + z", data=df, family=sm.families.Poisson()).fit(scale="X2")

# 固定效应泊松
m4 = pf.fepois("y ~ x + z | country + year", data=df, vcov={"CRV1": "country"})
```

在作者代码里认泊松回归，看这几处：R 的 `glm(` 配 `family = poisson` 或 `quasipoisson`，`fepois(`，`feglm(` 配 poisson 族，公式里的 `offset(log(...))`；Stata 的 `poisson`、`xtpoisson`、`ppmlhdfe`、`ppml`，以及 `irr`、`exposure(`、`offset(` 这些选项；Python 的 `smf.poisson`、`sm.families.Poisson`、`pf.fepois`。贸易或引力模型的论文里出现 PPML，指的就是泊松伪极大似然。

## 延伸阅读

- Cameron, A. Colin, and Pravin K. Trivedi. 2013. *Regression Analysis of Count Data*. 2nd ed. Cambridge University Press.
- Wooldridge, Jeffrey M. 2010. *Econometric Analysis of Cross Section and Panel Data*. 2nd ed. MIT Press. 第 18 章。
- Gourieroux, Christian, Alain Monfort, and Alain Trognon. 1984. "Pseudo Maximum Likelihood Methods: Applications to Poisson Models." *Econometrica* 52 (3).
- Hausman, Jerry, Bronwyn H. Hall, and Zvi Griliches. 1984. "Econometric Models for Count Data with an Application to the Patents–R&D Relationship." *Econometrica* 52 (4).
- Cameron, A. Colin, and Pravin K. Trivedi. 1990. "Regression-Based Tests for Overdispersion in the Poisson Model." *Journal of Econometrics* 46 (3).
- Wooldridge, Jeffrey M. 1999. "Distribution-Free Estimation of Some Nonlinear Panel Data Models." *Journal of Econometrics* 90 (1).
- Santos Silva, J. M. C., and Silvana Tenreyro. 2006. "The Log of Gravity." *Review of Economics and Statistics* 88 (4).
- King, Gary. 1988. "Statistical Models for Political Science Event Counts: Bias in Conventional Procedures and Evidence for the Exponential Poisson Regression Model." *American Journal of Political Science* 32 (3).
