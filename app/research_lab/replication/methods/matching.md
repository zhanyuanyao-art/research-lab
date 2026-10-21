---
key: matching
name: 匹配与倾向得分
english: Matching and Propensity Scores
summary: 为受处理单位找出在可观测特征上相似的未受处理单位来比较，减少结论对模型设定的依赖；但它只能处理看得见的差别，因果结论仍取决于没有遗漏重要的混杂因素。
---

## 适用的问题

处理不是随机分配的，研究者手里只有观察数据，而且有理由相信决定谁受处理的主要因素都已测量。比如：

- 联合国维和行动派往哪里，与冲突烈度、伤亡规模、地理位置有关，想知道维和是否延长了战后和平；
- 接受 IMF 贷款计划的国家本来就深陷危机，想知道贷款计划本身对增长的影响；
- 签署人权条约的国家在民主程度和收入上与不签署的不同，想知道签署是否改善了人权状况。

直接比较受处理和未受处理的单位，差别里混着两组原本的不同。匹配的思路很朴素：只拿在这些特征上相似的单位来比。

## 核心思想

一个数字例子。设想维和部队更常被派往高烈度冲突，而高烈度冲突之后的和平本来就短。结果是和平持续的年数：

| 冲突烈度 | 有维和：个数 | 有维和：平均年数 | 无维和：个数 | 无维和：平均年数 | 层内差 |
|---|---|---|---|---|---|
| 高 | 30 | 4 | 10 | 2 | +2 |
| 低 | 10 | 9 | 30 | 8 | +1 |
| 合计 | 40 | 5.25 | 40 | 6.5 | −1.25 |

直接比，有维和的和平平均短 1.25 年，好像维和适得其反。但在每个烈度层内部，有维和的都更长。按受处理案例的构成加权，即 30 个高烈度、10 个低烈度：

```text
ATT = (30 × 2 + 10 × 1) / 40 = 1.75
```

维和让受维和的案例的和平平均延长 1.75 年。直接比较出错，是因为维和专挑了难的案例。在烈度上分层比较，就是最简单的匹配：精确匹配。

现实中协变量多，而且多是连续的，几乎找不到所有特征都一模一样的对照，于是有了各种退一步的办法：按距离找最近的，把连续变量粗化后再精确匹配，把多个协变量压缩成一个倾向得分，或者直接给对照组加权。Ho、Imai、King 和 Stuart 把匹配定位为一种数据预处理：先把两组修剪、加权到可比，再做原本打算做的分析，这样结论就不会因为回归设定的细微改动而大幅变化。

## 模型设定

**估计对象与识别**：

```text
τ_ATT = E[Y_i(1) − Y_i(0) | D_i = 1]

若 Y_i(0) ⊥ D_i | X_i，则
τ_ATT = E[ E[Y_i | D_i = 1, X_i] − E[Y_i | D_i = 0, X_i] | D_i = 1 ]
```

- `D_i`：单位 i 是否受处理；
- `Y_i(1)`、`Y_i(0)`：受处理和不受处理时的潜在结果；
- `X_i`：处理之前就确定的协变量；
- `⊥`：条件独立，即给定 X 之后，谁受处理与潜在结果无关；
- `τ_ATT`：受处理单位的平均处理效应；条件换成全体单位就是 ATE，换成未受处理单位就是 ATC；
- 第二个式子的意思：在 X 相同的单位内部比较两组，再按受处理单位的 X 分布加权平均，正是上面数字例子的算法。

**最近邻匹配**：

```text
τ̂_ATT = (1/N₁) · Σ_{i: D_i = 1} [ Y_i − (1/M) · Σ_{j ∈ J_M(i)} Y_j ]
马氏距离：d(i, j) = √[ (X_i − X_j)′ · S⁻¹ · (X_i − X_j) ]
```

- `N₁`：受处理单位的个数；
- `J_M(i)`：与单位 i 距离最近的 M 个对照单位；
- `S`：协变量的样本协方差矩阵，马氏距离靠它把单位和尺度不同的变量放到一起比较；
- **卡尺**：只接受距离小于某个上限的匹配，常见的是倾向得分 logit 值标准差的 0.2 倍；找不到合格对照的受处理单位被丢掉；
- **有放回与无放回**：有放回时一个对照可以被多次使用，匹配更近，但方差更大。

**精确匹配与粗化精确匹配**。精确匹配要求所有协变量完全相同。粗化精确匹配，即 CEM，先把连续变量粗化成区间，比如人均收入分成五档、政体分成三类，再在粗化后的格子里精确匹配，丢掉只有一组单位的格子，最后按格子里两组的数量加权。两组的不平衡程度由研究者事先选定的粗化程度直接控制。代价是协变量一多，格子数爆炸，大量受处理单位找不到对照。

**倾向得分**：

```text
e(X_i) = Pr(D_i = 1 | X_i)
若 Y_i(0), Y_i(1) ⊥ D_i | X_i，则 Y_i(0), Y_i(1) ⊥ D_i | e(X_i)
```

- `e(X_i)`：给定协变量时受处理的概率，通常用 logit 估计；
- 第二行是 Rosenbaum 和 Rubin 的结论：条件独立成立时，只按倾向得分这一个数来匹配或分层就够了，多维问题被压成一维。

**加权**：

```text
IPW，ATE：  w_i = D_i / ê(X_i) + (1 − D_i) / (1 − ê(X_i))
IPW，ATT：  受处理单位 w_i = 1；对照单位 w_i = ê(X_i) / (1 − ê(X_i))
熵平衡：    最小化 Σ_{D_i = 0} w_i · log(w_i / q_i)
            约束 Σ_{D_i = 0} w_i · c_r(X_i) = 受处理组 c_r(X) 的均值，Σ w_i = 1，w_i > 0
```

- `ê(X_i)`：估计出的倾向得分。估计 ATT 时，越像受处理单位的对照，即 ê 越大，权重越大；ê 接近 0 或 1 时权重会爆炸，少数几个单位主导结果；
- `q_i`：基准权重，通常取均等；
- `c_r(X_i)`：要平衡的矩，比如各协变量的均值、方差、偏度；
- 熵平衡由 Hainmueller 提出：在指定的矩上让对照组与受处理组精确相等，同时让权重尽量接近均等。指定矩上的平衡是构造出来的，没指定的矩和交互项要另查。

**平衡诊断**：

```text
SMD_k = (x̄_k,T − x̄_k,C) / √[ (s²_k,T + s²_k,C) / 2 ]
```

- `x̄_k,T`、`x̄_k,C`：协变量 k 在匹配或加权后受处理组和对照组的均值；
- `s²_k,T`、`s²_k,C`：两组的方差。分母通常用匹配前样本的值，在匹配前后保持不变，这样 SMD 的变化只反映均值差的变化；估计 ATT 时也常只用受处理组的标准差；
- 常用标准是 `|SMD| < 0.1`，同时看方差比是否接近 1。

**为什么不看平衡检验的 p 值**。Imai、King 和 Stuart 指出了三点：平衡是手头这个样本的性质，不是对总体的推断，用不着假设检验；t 检验的 p 值随样本量变化，匹配丢掉一部分观测后，即使不平衡一点没变甚至变差，p 值也会因为样本变小而变大，看起来更平衡了；不显著也不等于平衡。应当直接看 SMD 这类不随样本量变化的指标。

**对倾向得分匹配的批评**。King 和 Nielsen 指出，倾向得分匹配模拟的是完全随机化的实验，而马氏距离匹配和粗化精确匹配模拟的是按协变量分区组的随机实验，后者效率更高。更麻烦的是，倾向得分相同的两个单位，协变量可以差得很远；样本接近随机实验的平衡程度之后，继续按倾向得分修剪，等于在协变量上随机丢观测，不平衡、模型依赖和偏差反而上升，他们称之为倾向得分悖论。所以用倾向得分匹配时，平衡必须直接在协变量上检查，最好再和马氏距离、粗化精确匹配的结果对照。倾向得分在判断共同支撑、构造加权估计量时仍然有用。

## 识别假设

1. **条件独立**，也叫可忽略性或基于可观测变量的选择：给定 X，谁受处理与潜在结果无关。这是核心，而且不可检验。代价：任何一个没测量的混杂因素都会带来偏差，匹配对它无能为力。
2. **共同支撑**：对每一种 X，受处理和不受处理的概率都严格大于 0；估计 ATT 时，只要求受处理单位的 X 在对照组里找得到。代价：没有对照的区域只能靠外推，IPW 的权重会爆炸；修剪掉这些单位，估计对象随之改变。
3. **协变量都是处理前变量**：代价：匹配了被处理影响的变量，会把处理效果的一部分挡掉，或者打开新的偏差路径。
4. **没有干扰，处理只有一个版本**：一个单位受处理不影响其他单位的结果，处理在各单位之间含义相同。代价：维和对邻国的外溢会污染对照组；规模悬殊的维和行动混成一个哑变量，估计出的是一个难以解释的平均。
5. **倾向得分模型设定正确**，只对用倾向得分的方法而言。代价：协变量上残留不平衡；好在可以直接检查平衡来发现。

**匹配本身不是识别策略**。断点回归、双重差分、工具变量的可信度，来自研究设计之外的某种变化：一条门槛、一次时间上的冲击、一个外生的推动。匹配没有这样的来源，它的因果解释完全建立在第 1 条假设上，而这条假设讲的是世界，不是算法。要论证决定处理的因素都已测量，靠的是对分配过程的实质了解，比如维和由安理会决定，决定时看的是哪些因素。匹配做得再好，也只是让两组在看得见的变量上可比；它真正的贡献是减少模型依赖、把比较限制在有共同支撑的范围内。所以它常与其他设计结合，比如先匹配再做双重差分；单独使用时，必须配上敏感性分析。

## 稳健性检验：做什么，为什么

每一项检验都对应上面的某条假设。读论文时，可以对照着看作者做了哪些、漏了哪些。

| 检验 | 检查哪条假设 | 怎么做，怎么看 |
|---|---|---|
| 共同支撑图 | 共同支撑 | 画两组倾向得分的分布。受处理组有一段对照组几乎没有的范围，那段单位找不到真正的对照，估计靠外推；修剪之后要说明估计对象变成了谁 |
| 平衡表和 love plot | 匹配是否真把 X 拉平，这是条件独立能发挥作用的前提 | 列出匹配前后各协变量的 SMD 和方差比，匹配后 SMD 应在 0.1 以内；还要查平方项和交互项。检查对象是协变量本身，不是倾向得分 |
| 换匹配方法 | 模型依赖 | 倾向得分最近邻、马氏距离、粗化精确匹配、熵平衡各做一遍。结论一致，说明不依赖特定算法；结论随方法摇摆，说明数据撑不起这么细的比较 |
| 匹配后回归调整或双重稳健估计 | 残余不平衡、模型设定 | 在匹配样本上再对协变量做回归调整，或用同时建处理模型和结果模型的双重稳健估计量；两个模型只要有一个设定对，估计就一致 |
| Rosenbaum 界 | 条件独立 | 假设一个未观测因素能让 X 相同的两个单位受处理的胜算相差 Γ 倍，逐步加大 Γ，看结论在多大的 Γ 下失去显著。Γ 越大，结论越经得起隐藏偏差 |
| sensemakr 敏感性分析 | 条件独立 | 在回归框架下计算稳健值：遗漏变量要同时解释处理和结果多大比例的残余方差，才能把估计值压到 0；再拿已观测协变量的解释力做对照 |
| 负对照结果 | 条件独立 | 选一个处理不可能影响的结果，比如处理之前的结果。匹配后它上面也出现了效果，说明还有没控制住的混杂 |
| 改变协变量集合 | 处理前变量 | 去掉可能被处理影响的变量，以及只影响处理、不影响结果的变量。后一类变量会加大方差，还可能放大未观测混杂造成的偏差 |
| 报告被丢掉的受处理单位 | 估计对象 | 卡尺和粗化精确匹配会丢掉受处理单位，估计对象从全部受处理单位变成匹配上的那部分。要报告丢了多少、丢的是哪类单位 |

敏感性分析的读法，举两个数字。Γ = 1.5 时 p 值仍小于 0.05，到 Γ = 2 才超过：未观测因素要让特征相同的两个单位受处理的胜算相差 2 倍，才推得翻结论。sensemakr 报告稳健值 RV = 0.15：遗漏变量要同时解释处理和结果各 15% 的残余方差，才能把估计值压到 0；如果最重要的已观测协变量只解释了 2%，就需要一个比它强好几倍的遗漏变量。

## 怎么读结果

- **估计对象**：先看作者估计的是 ATT、ATE 还是 ATC。为受处理单位找对照，得到的是 ATT；用了卡尺之后，是匹配上的那部分受处理单位的 ATT。
- **平衡表**：匹配前后各协变量的 SMD，最好配一张 love plot；匹配前不平衡、匹配后在 0.1 以内，说明在这些变量上可比了。
- **样本量**：匹配后各组还剩多少单位，有多少受处理单位因为找不到对照被丢掉。
- **标准误**：有放回的最近邻匹配不能用普通自助法，应当用 Abadie 和 Imbens 的标准误；在匹配样本上跑回归时，按匹配对聚类；加权估计要考虑权重本身是估计出来的。
- **敏感性分析**：Γ 或 RV 是多少，作者拿哪些已观测协变量做对照。

读的时候可以这样说：在冲突烈度、伤亡、持续时间、地形等协变量上做一对一最近邻匹配后，所有协变量的标准化均值差都在 0.1 以内；在匹配样本中，维和使和平平均延长 x 年；敏感性分析显示，未观测因素要让受维和的胜算相差 Γ 倍，才能推翻这一结论。

## 常见陷阱

- **把匹配当成解决内生性的办法**：它只处理看得见的差别。
- **用 p 值判断平衡，或只查倾向得分的平衡**：前者随样本量变化，后者不等于各协变量平衡。
- **匹配了处理之后的变量**：比如维和研究里匹配了维和部署之后的伤亡人数。
- **卡尺丢掉大量受处理单位却不说明**：估计对象悄悄变了。
- **试很多种匹配设定，只报最好的那个**：这和挑回归设定是一回事。
- **用普通自助法算有放回最近邻匹配的标准误**：Abadie 和 Imbens 证明这种自助法不成立。
- **以为协变量放得越多越好**：只影响处理的变量和处理后变量，放进去都会帮倒忙。

## 代码写法

R，常用 MatchIt 做匹配、WeightIt 做加权、cobalt 查平衡、sensemakr 做敏感性分析：

```r
library(MatchIt)
library(cobalt)
library(lmtest)
library(sandwich)

# 一对一最近邻，在倾向得分的 logit 尺度上匹配，卡尺为 logit 倾向得分标准差的 0.2 倍，估计 ATT
m_nn <- matchit(d ~ x1 + x2 + x3, data = df, method = "nearest",
                distance = "glm", link = "linear.logit", caliper = 0.2, estimand = "ATT")
# 马氏距离最近邻
m_mah <- matchit(d ~ x1 + x2 + x3, data = df, method = "nearest", distance = "mahalanobis")
# 粗化精确匹配，默认自动分箱
m_cem <- matchit(d ~ x1 + x2 + x3, data = df, method = "cem")

# 平衡诊断：标准化均值差，阈值 0.1
bal.tab(m_nn, un = TRUE, thresholds = c(m = 0.1))
love.plot(m_nn, thresholds = c(m = 0.1))

# 在匹配样本上估计：带匹配权重，标准误按匹配对聚类
md <- match.data(m_nn)
fit <- lm(y ~ d + x1 + x2 + x3, data = md, weights = weights)
coeftest(fit, vcov. = vcovCL, cluster = ~subclass)

# 加权：逆概率加权与熵平衡
library(WeightIt)
w_ipw <- weightit(d ~ x1 + x2 + x3, data = df, method = "glm", estimand = "ATT")
w_eb  <- weightit(d ~ x1 + x2 + x3, data = df, method = "ebal", estimand = "ATT")
bal.tab(w_eb)

# 敏感性分析：以 x1 为基准，看强度为它 1 到 3 倍的遗漏变量会怎样
library(sensemakr)
ols <- lm(y ~ d + x1 + x2 + x3, data = df)
sens <- sensemakr(model = ols, treatment = "d", benchmark_covariates = "x1", kd = 1:3)
summary(sens)
plot(sens)
```

Stata：

```stata
* 倾向得分最近邻匹配，估计 ATT
teffects psmatch (y) (d x1 x2 x3, logit), atet nneighbor(1)
* 马氏距离最近邻匹配，并做偏差校正
teffects nnmatch (y x1 x2 x3) (d), atet nneighbor(1) metric(mahalanobis) biasadj(x1 x2 x3)
* 匹配后的平衡诊断
tebalance summarize
* 逆概率加权
teffects ipw (y) (d x1 x2 x3, logit), atet
* 熵平衡：生成权重变量 _webal
ebalance d x1 x2 x3, targets(1)
regress y d [pweight = _webal]
* 粗化精确匹配：生成权重变量 cem_weights
cem x1 x2 x3, treatment(d)
regress y d [iweight = cem_weights]
```

Python，用 statsmodels 估计倾向得分、scikit-learn 找最近邻：

```python
import numpy as np
import statsmodels.formula.api as smf
from sklearn.neighbors import NearestNeighbors

# 倾向得分，换成 logit 尺度
p = smf.logit("d ~ x1 + x2 + x3", data=df).fit().predict(df)
df["lps"] = np.log(p / (1 - p))
treated, control = df[df["d"] == 1], df[df["d"] == 0]

# 一对一有放回最近邻，卡尺为 logit 倾向得分标准差的 0.2 倍
nn = NearestNeighbors(n_neighbors=1).fit(control[["lps"]])
dist, idx = nn.kneighbors(treated[["lps"]])
keep = dist[:, 0] <= 0.2 * df["lps"].std()
matched_c = control.iloc[idx[keep, 0]]
# ATT 点估计；标准误不能用普通自助法
att = treated["y"].to_numpy()[keep].mean() - matched_c["y"].mean()
```

在作者代码里认匹配，看这几处：`matchit`、`match.data`、`Match`、`GenMatch`、`cem`、`weightit`、`ebalance`；Stata 的 `teffects psmatch`、`teffects nnmatch`、`teffects ipw`、`psmatch2`、`kmatch`；一个以处理变量为因变量的 logit 或 probit，随后把预测概率存成新变量；回归里的 `weights = weights` 或 `[pweight = ...]`；平衡检查的 `bal.tab`、`love.plot`、`tebalance`、`pstest`；敏感性分析的 `sensemakr`、`psens`、`rbounds`、`mhbounds`。

## 延伸阅读

- Rosenbaum, Paul R., and Donald B. Rubin. 1983. "The Central Role of the Propensity Score in Observational Studies for Causal Effects." *Biometrika* 70 (1).
- Ho, Daniel E., Kosuke Imai, Gary King, and Elizabeth A. Stuart. 2007. "Matching as Nonparametric Preprocessing for Reducing Model Dependence in Parametric Causal Inference." *Political Analysis* 15 (3).
- Stuart, Elizabeth A. 2010. "Matching Methods for Causal Inference: A Review and a Look Forward." *Statistical Science* 25 (1).
- Iacus, Stefano M., Gary King, and Giuseppe Porro. 2012. "Causal Inference without Balance Checking: Coarsened Exact Matching." *Political Analysis* 20 (1).
- Hainmueller, Jens. 2012. "Entropy Balancing for Causal Effects: A Multivariate Reweighting Method to Produce Balanced Samples in Observational Studies." *Political Analysis* 20 (1).
- King, Gary, and Richard Nielsen. 2019. "Why Propensity Scores Should Not Be Used for Matching." *Political Analysis* 27 (4).
- Cinelli, Carlos, and Chad Hazlett. 2020. "Making Sense of Sensitivity: Extending Omitted Variable Bias." *Journal of the Royal Statistical Society: Series B* 82 (1).
- Rosenbaum, Paul R. 2002. *Observational Studies*. 2nd ed. Springer.
