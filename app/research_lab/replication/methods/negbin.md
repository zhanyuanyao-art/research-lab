---
key: negbin
name: 负二项回归
english: Negative Binomial Regression
summary: 次数的起伏比泊松回归允许的大时，多估一个离散参数，让方差可以大于均值；系数的读法和泊松回归相同，取指数是倍数。
---

## 适用的问题

和泊松回归一样，因变量是次数；不同的是数据明显过度离散，方差远大于均值。政治和国际关系的事件数据几乎都是这样：

- 各国每年的恐怖袭击次数：多数国家多数年份是 0，少数国家一年几百起；
- 各国每年的抗议次数：平时寥寥，危机年份集中爆发；
- 国家对之间每年的外交冲突事件数。

过度离散有两个来源。一是**未观测到的异质性**：解释变量相同的单位，潜在的发生率也不同，有的国家就是更容易发生抗议。二是**传染**：一起事件让下一起更可能发生，事件扎堆。负二项回归用一个额外的参数描述这种多出来的起伏。

使用之前要想清楚它解决的是什么问题。泊松回归的系数在过度离散时仍然一致，配上稳健标准误就能正确推断；负二项多给的是效率，以及一个完整的次数分布，可以预测出现 0 次、1 次……的概率。它解决不了遗漏变量问题。

## 核心思想

**泊松–伽马混合**。设想每个单位的发生率都乘了一个看不到的随机因子 ν_i，它的平均值是 1，有的单位大于 1，有的小于 1。给定 ν_i，次数服从泊松分布；如果 ν_i 服从伽马分布，把它积分掉，得到的次数分布就是负二项分布。

一个数字例子说明异质性怎样制造过度离散。一半国家的抗议发生率是每年 1 起，另一半是每年 5 起，每个国家内部都严格服从泊松分布：

- 全体的平均次数是 3；
- 全体的方差等于组内方差的平均加上组间均值的方差：3 + 4 = 7，是均值的两倍多；
- 出现 0 次的比例是 0.5 × e⁻¹ + 0.5 × e⁻⁵ ≈ 0.184 + 0.003，约 18.7%；而均值为 3 的泊松分布只预测 5.0% 的 0。

这里没有任何单独的零过程，多出来的 0 完全来自各国发生率的差别。讨论零膨胀模型时还会用到这个例子。用均值 3、方差 7 的负二项分布来拟合，预测的 0 约为 14.9%，比泊松分布接近得多。

## 模型设定

```text
Y_i | X_i, ν_i ~ Poisson(μ_i·ν_i)
ν_i ~ Gamma，E(ν_i) = 1，Var(ν_i) = α
log μ_i = X_i′β
E(Y_i | X_i) = μ_i
NB2：Var(Y_i | X_i) = μ_i + α·μ_i²
```

- `Y_i`：第 i 个观测的次数；`X_i` 是解释变量；
- `μ_i`：期望次数，和泊松回归一样由对数连接决定；
- `ν_i`：未观测到的乘法异质性，平均为 1；
- `α`：离散参数，等于 ν_i 的方差；α = 0 时退化为泊松回归，α 越大过度离散越严重；
- 最后一行说明方差随均值的平方增长：均值大的观测，额外的起伏也大得多。

NB2 的概率函数：

```text
Pr(Y_i = k | X_i) = Γ(k + 1/α) / [Γ(1/α)·k!] · [1/(1 + α·μ_i)]^(1/α) · [α·μ_i/(1 + α·μ_i)]^k
Pr(Y_i = 0 | X_i) = (1 + α·μ_i)^(−1/α)
```

- `Γ`：伽马函数，在正整数上 `Γ(n) = (n − 1)!`；
- `k!`：k 的阶乘；
- 第二行是 k = 0 的特例。上面例子里的 14.9% 就是这样算的：均值 3、方差 7 意味着 α·3² = 4，α = 4/9，于是 `(1 + 4/3)^(−9/4) ≈ 0.149`。

**NB1 与 NB2**：

```text
NB1：Var(Y_i | X_i) = μ_i + δ·μ_i = (1 + δ)·μ_i
NB2 求解 β 的方程：Σ_i [(Y_i − μ_i) / (1 + α·μ_i)]·X_i = 0
```

- `δ`：NB1 的离散参数，方差是均值的固定倍数；
- 第二行是 NB2 对 β 的一阶条件，`1/(1 + α·μ_i)` 是每个观测的权重。

两者的区别在于方差怎样随均值增长：NB2 是二次的，NB1 是线性的。软件默认的通常是 NB2，Stata 的 `nbreg` 和 R 的 `glm.nb` 都是。两者没有嵌套关系，可以用 AIC 或 BIC 比较。还有一个实质差别：NB2 的一阶条件里，只要均值设定正确，`Y_i − μ_i` 的期望就是 0，不管方差形式对不对，所以 NB2 的系数在均值设定正确时仍然一致；NB1 没有这个保证。从权重也能看出 NB2 和泊松的差别：泊松对每个观测的残差一视同仁，NB2 给期望次数大的观测降权。

**和泊松回归的似然比检验**：

```text
LR = 2·(log L_NB − log L_Poisson)
H0：α = 0
```

- `log L_NB`、`log L_Poisson`：两个模型的对数似然；
- `H0`：原假设，不存在过度离散。

α 不能小于 0，原假设正好落在参数空间的边界上，LR 在原假设下服从 0 和 `χ²(1)` 各占一半的混合分布。实际做法是把 `χ²(1)` 的 p 值减半，5% 水平的临界值是 2.71 而不是 3.84。Stata 输出里的 `chibar2(01)` 就是这个检验。

**零膨胀模型**：总体里混着两类单位，一类永远是 0，另一类服从普通的计数分布，也可能碰巧为 0。

```text
Pr(Y_i = 0) = π_i + (1 − π_i)·f(0 | μ_i)
Pr(Y_i = k) = (1 − π_i)·f(k | μ_i)，k ≥ 1
logit(π_i) = Z_i′γ
E(Y_i | X_i, Z_i) = (1 − π_i)·μ_i
```

- `π_i`：属于永远为 0 那一类的概率；
- `f(k | μ_i)`：泊松或负二项分布给出的概率；
- `Z_i`、`γ`：零部分的解释变量和系数，用 Logit 描述 π_i；
- 最后一行是期望次数，同时受两部分影响。

**障碍模型**：所有的 0 来自同一个决定，先决定是否越过 0，越过之后次数服从一个去掉了 0 的计数分布。

```text
Pr(Y_i = 0) = 1 − p_i
Pr(Y_i = k) = p_i · f(k | μ_i) / [1 − f(0 | μ_i)]，k ≥ 1
```

- `p_i`：越过障碍、次数为正的概率，通常用 Logit 描述；
- `f(k | μ_i) / [1 − f(0 | μ_i)]`：截掉 0 之后的计数分布。

两者的区别在于 0 的含义。零膨胀模型说有两种 0：根本不可能发生的结构性 0，和可能发生但碰巧没发生的 0；比如彼此没有任何往来的国家对不可能发生外交冲突，有往来的国家对在某一年也可能恰好没有冲突。障碍模型说只有一种 0，来自一个是否行动的决定；比如一国先决定要不要对另一国实施制裁，决定之后再决定实施几项措施。

## 识别假设

负二项回归本身不是因果识别设计。下面是估计量需要的条件和每条失败的代价。

1. **条件均值设定正确**：和泊松回归相同，`E(Y | X) = exp(X′β)`。错了，系数就错。
2. **外生性，包括异质性与 X 无关**：这一条最容易被误解。负二项把未观测到的异质性 ν_i 当作和 X 无关的随机因子处理，相当于一种随机效应假定。如果一国更容易发生抗议的潜在倾向和它的政体类型相关，负二项回归并不能把这种倾向剔除，系数照样有偏。常说的“负二项控制了未观测到的异质性”，只在这种异质性和解释变量无关时成立，而那时它本来就不威胁系数。
3. **方差形式正确**：NB2 假定方差是均值的二次函数。错了的代价：NB2 的系数在均值正确时仍然一致，但模型给出的标准误不对，效率上的好处也打折扣；预测某个次数的概率，比如 0 次的概率，会出错。用稳健标准误可以保住推断。
4. **观测独立**：同一单位多年的观测相关时，标准误偏小，要聚类。
5. **零膨胀和障碍模型的两部分有理论依据**：零膨胀模型如果零部分和计数部分用同一组解释变量，两部分主要靠分布形状来区分，识别很脆弱，系数可能很不稳定。
6. **固定效应要真的是固定效应**：Hausman、Hall 和 Griliches 提出的条件固定效应负二项，也就是 Stata 的 `xtnbreg, fe`，它的单位效应进入的是离散参数，而不是均值。Allison 和 Waterman 指出，它并不能像通常的固定效应那样控制所有不随时间变化的单位特征；一个明显的迹象是，这个模型仍然能估计出不随时间变化变量的系数，比如一国是否内陆国。替代办法有两个：用固定效应泊松加聚类标准误，它在均值设定正确时一致，这是 Wooldridge 推荐的做法；或者直接放单位哑变量估计无条件负二项，Allison 和 Waterman 的模拟显示它的系数偏误不大。

## 稳健性检验：做什么，为什么

| 检验 | 检查哪条假设 | 怎么做，怎么看 |
|---|---|---|
| α = 0 的似然比检验 | 是否存在过度离散 | 按边界情形的 `chibar2(01)` 判断。显著说明泊松回归的默认标准误不能用；但它证明不了负二项是对的，只说明负二项分布比泊松分布更贴近数据 |
| 改用泊松回归加稳健标准误 | 方差形式、观测权重 | 这是最常见的对照，也是作者在泊松和负二项之间来回切换的原因。两者对方差和观测权重的假定不同：泊松平等对待每个观测的残差，NB2 按 `1/(1 + α·μ_i)` 给期望次数大的观测降权。均值设定正确时两者的系数都一致，理应接近；接近，说明结论不取决于方差假定和权重；差别很大，说明均值设定有问题，或者结论由少数大次数观测撑着，这时更该相信只依赖均值的泊松回归 |
| 负二项加稳健标准误 | 方差形式 | 和模型标准误比较。差别大说明 NB2 的二次方差不符合数据 |
| NB1 与 NB2 比较 | 方差随均值增长的方式 | 比较 AIC、BIC 和系数。系数差别大时，以 NB2 或泊松为准，因为 NB1 的系数在方差设错时没有一致性保证 |
| 预测和实际的次数分布 | 整体分布设定 | 比较 0、1、2……每个次数的预测频数和实际频数，或画 rootogram。负二项通常能把 0 的比例拟合得很好；仍有系统性差距，再考虑零膨胀或障碍模型 |
| 零膨胀或障碍模型 | 零的生成过程 | 只有理论上存在一类不可能发生事件的单位，或存在一个先行的是否行动的决定时才值得做。看核心变量在两部分里的系数，以及对总期望次数的边际效应。拿 Vuong 检验在零膨胀和普通模型之间做选择是一种误用，Wilson 专门讨论过 |
| 换掉 `xtnbreg, fe` | 外生性：不随时间变化的遗漏变量 | 用固定效应泊松或带单位哑变量的负二项重估。如果 `xtnbreg, fe` 还能估出不随时间变化变量的系数，说明它没有吸收单位效应 |
| 随机效应或多层负二项 | 异质性的结构 | 给单位一个随机截距，把单位层面的异质性和观测层面的过度离散分开。和固定效应结果差别大，说明单位效应和解释变量相关，随机效应假定不成立 |
| 剔除极端值 | 少数大值的影响 | 去掉最极端的几个单位或年份，看泊松和负二项的结果是否趋于一致。两者的分歧如果主要来自这几个观测，结论就依赖于怎样对待它们 |

## 怎么读结果

- **系数与发生率比**：和泊松回归完全一样，`exp(β)` 是倍数。
- **离散参数**：Stata 报告 alpha，有时还报告它的对数 lnalpha；R 的 `glm.nb` 报告 theta，`θ = 1/α`。θ 越大越接近泊松，θ = 2 就是 α = 0.5。方差和均值之比是 `1 + α·μ`：α = 0.5、μ = 4 时，方差是 4 + 0.5 × 16 = 12，是均值的 3 倍。
- **零膨胀模型的两张表**：计数部分的系数照常读作倍数；零部分是一个 Logit，预测的是属于永远为 0 那一类的概率，所以正号意味着更可能是结构性的 0，也就是事件更少。同一个变量在两部分的符号很容易读反。变量对总期望次数的作用要用边际效应算，因为它同时通过 π 和 μ 起作用。
- **障碍模型的两张表**：第一部分是越过 0 的概率，正号表示更可能至少发生一次；第二部分是已经发生的前提下次数有多少。
- **表格里要找的信息**：α 或 θ 及其检验、标准误类型、NB1 还是 NB2、零部分用了哪些变量、固定效应是怎样实现的。

读的时候可以这样说：负二项回归显示，邻国发生内战使一国的边境冲突事件增加约 60%，发生率比为 1.6；离散参数 α = 1.8，明显大于 0，泊松分布不适合；改用泊松回归加聚类标准误，发生率比为 1.55，结论不变。

## 常见陷阱

- **以为负二项能控制未观测的单位特征**：它只吸收和 X 无关的异质性。
- **只因为方差大于均值就换负二项，却不说为什么不用泊松加稳健标准误**：两者针对同一个问题，各有代价，要说清选择的理由。
- **把 `xtnbreg, fe` 当作真正的固定效应模型**。
- **看到很多 0 就上零膨胀**：异质性本身就能制造大量的 0，上面的例子里 0 的比例从 5% 升到将近 19%，却不存在任何单独的零过程。零膨胀模型参数更多、解释更复杂，没有理论依据时只是在拟合噪音。
- **读反 theta 和 alpha**：R 的 theta 越小，过度离散越严重。
- **用 `χ²(1)` 的临界值检验 α = 0**：没考虑边界问题，检验偏保守。
- **在 Python 的广义线性模型里直接用负二项族**：statsmodels 的 `sm.families.NegativeBinomial` 默认把 α 固定为 1，不估计它，结果和真正的负二项回归不同。
- **零膨胀模型两部分放同样的变量**：两部分几乎只靠分布形状区分，系数可能很不稳定。

## 代码写法

R，负二项用 MASS，零膨胀和障碍模型用 pscl，固定效应用 fixest：

```r
library(MASS)
m_nb <- glm.nb(y ~ x + z, data = df)
summary(m_nb)                    # 输出里的 theta = 1/α，theta 越大越接近泊松
exp(coef(m_nb))                  # 发生率比

# 与泊松回归的似然比检验；α 不能小于 0，p 值按边界情形减半
m_po <- glm(y ~ x + z, family = poisson, data = df)
lr <- as.numeric(2 * (logLik(m_nb) - logLik(m_po)))
pchisq(lr, df = 1, lower.tail = FALSE) / 2

# 聚类标准误
library(sandwich)
library(lmtest)
coeftest(m_nb, vcov = vcovCL(m_nb, cluster = ~country))

# 零膨胀与障碍模型：竖线后面是零部分的解释变量
library(pscl)
m_zinb <- zeroinfl(y ~ x + z | w, data = df, dist = "negbin")
m_hnb  <- hurdle(y ~ x + z | w, data = df, dist = "negbin", zero.dist = "binomial")

# 固定效应：带固定效应的负二项，以及作为对照的固定效应泊松
library(fixest)
m_fnb <- fenegbin(y ~ x + z | country + year, data = df, cluster = ~country)
m_fpo <- fepois(y ~ x + z | country + year, data = df, cluster = ~country)

# 多层负二项：国家随机截距
library(lme4)
m_re <- glmer.nb(y ~ x + z + (1 | country), data = df)
```

Stata：

```stata
* NB2；默认标准误下，输出末尾报告 alpha = 0 的似然比检验 chibar2(01)
nbreg y x z
nbreg y x z, irr vce(cluster country)       // 发生率比，按国家聚类
nbreg y x z, dispersion(constant)           // NB1
gnbreg y x z, lnalpha(w)                    // 让离散参数随变量 w 变化

* 零膨胀负二项：inflate() 里是零部分的解释变量
zinb y x z, inflate(w) vce(cluster country)

* 障碍模型分两步：先估是否越过 0，再对正数部分估截断负二项
gen any = (y > 0) if !missing(y)
logit any x z, vce(cluster country)
tnbreg y x z if y > 0, ll(0) vce(cluster country)

* 固定效应：用泊松加聚类标准误，或负二项加单位哑变量，而不是 xtnbreg, fe
ppmlhdfe y x z, absorb(country year) vce(cluster country)   // 需先 ssc install ppmlhdfe
nbreg y x z i.country i.year, vce(cluster country)
```

Python：

```python
import numpy as np
import statsmodels.formula.api as smf
from scipy.stats import chi2

# NB2：参数里的 alpha 就是离散参数
m_nb2 = smf.negativebinomial("y ~ x + z", data=df).fit()
np.exp(m_nb2.params.drop("alpha"))          # 发生率比

# NB1
m_nb1 = smf.negativebinomial("y ~ x + z", data=df, loglike_method="nb1").fit()

# 与泊松回归的似然比检验，p 值减半
m_po = smf.poisson("y ~ x + z", data=df).fit()
lr = 2 * (m_nb2.llf - m_po.llf)
p = chi2.sf(lr, 1) / 2

# 按国家聚类；df 需先去掉缺失值，groups 才能和样本对齐
m_cl = smf.negativebinomial("y ~ x + z", data=df).fit(cov_type="cluster", cov_kwds={"groups": df["country"]})

# 注意：smf.glm 配 sm.families.NegativeBinomial() 时，alpha 默认固定为 1，不会被估计
```

在作者代码里认负二项回归，看这几处：R 的 `glm.nb(`、`glmer.nb(`、`fenegbin(`，pscl 包的 `zeroinfl(` 和 `hurdle(` 配 `dist = "negbin"`，`glmmTMB(` 配 `family = nbinom2`；Stata 的 `nbreg`、`gnbreg`、`xtnbreg`、`menbreg`、`zinb`、`tnbreg`；Python 的 `smf.negativebinomial`、`NegativeBinomialP`、`ZeroInflatedNegativeBinomialP`，以及 `sm.families.NegativeBinomial`，后者要检查 alpha 是否被固定。输出里出现 theta、alpha、lnalpha 或 `LR test of alpha=0`，就是负二项。

## 延伸阅读

- Cameron, A. Colin, and Pravin K. Trivedi. 2013. *Regression Analysis of Count Data*. 2nd ed. Cambridge University Press.
- Hilbe, Joseph M. 2011. *Negative Binomial Regression*. 2nd ed. Cambridge University Press.
- Long, J. Scott. 1997. *Regression Models for Categorical and Limited Dependent Variables*. Sage. 计数结果一章。
- Hausman, Jerry, Bronwyn H. Hall, and Zvi Griliches. 1984. "Econometric Models for Count Data with an Application to the Patents–R&D Relationship." *Econometrica* 52 (4).
- Allison, Paul D., and Richard P. Waterman. 2002. "Fixed-Effects Negative Binomial Regression Models." *Sociological Methodology* 32.
- Lambert, Diane. 1992. "Zero-Inflated Poisson Regression, with an Application to Defects in Manufacturing." *Technometrics* 34 (1).
- Mullahy, John. 1986. "Specification and Testing of Some Modified Count Data Models." *Journal of Econometrics* 33 (3).
- Zeileis, Achim, Christian Kleiber, and Simon Jackman. 2008. "Regression Models for Count Data in R." *Journal of Statistical Software* 27 (8).
