---
key: ordered
name: 有序与多分类因变量
english: Ordered and Multinomial Outcome Models
summary: 因变量是几个类别时，有序模型假设类别背后有一条连续的倾向、被几道门槛切开，多项模型让每个类别各有一套系数；两者的系数都不能直接读成概率的变化，要换算成每个类别的预测概率。
---

## 适用的问题

因变量是有限的几个类别，而不是一个连续的数。分两种情况。

**类别有先后，但相邻类别之间的距离说不清**：

- 军事化国家间争端的敌对等级：没有军事行动、威胁使用武力、展示武力、使用武力、战争；
- 问卷里的态度题，比如是否支持对某国动武，从坚决反对到坚决支持分五档；
- 按年死亡人数分档的冲突强度；
- 信用评级、民主程度的分档。

**类别没有先后**：

- 一国面对危机的选择：谈判、制裁、军事行动；
- 内战的结局：政府获胜、反政府方获胜、谈判解决、低烈度拖延；
- 领导人下台后的去向：正常退休、流亡、入狱、死亡。

有些变量归哪一类本身需要论证。联合国大会投票的赞成、弃权、反对，常被当作有序，弃权居中；但弃权也可能是一种独立的策略，比如不想得罪任何一方，那它就不在赞成与反对之间的那条线上。

为什么不直接用最小二乘：把 1 到 5 当作等距的数，等于假设“威胁使用武力”到“展示武力”的距离，和“使用武力”到“战争”一样远；预测值还会落到 1 以下或 5 以上。为什么不拆成几个二元变量各跑一次：每次只用了一部分信息，几次的结果还可能互相矛盾。

## 核心思想

**有序模型**：设想每个观测背后有一条看不见的连续倾向 y*，比如两国关系的敌对程度。它由自变量决定，再加一个随机扰动。研究者看不到 y*，只看到它落在哪一段：越过第一道门槛，从“没有军事行动”升到“威胁使用武力”；越过第二道，升到“展示武力”，依此类推。门槛的位置由数据估计，不要求等距。

自变量只推动 y* 整体左右移动，门槛本身不动。所以一个自变量对每一道门槛的作用都相同，这就是平行回归假设，在有序 logit 里也叫比例优势假设。

一个数字例子。三个类别：反对、中立、支持；两道门槛 τ₁ = −1、τ₂ = 1；扰动服从 logistic 分布。计算方法是：

```text
P(反对)         = Λ(τ₁ − xβ)
P(反对或中立)   = Λ(τ₂ − xβ)
P(中立)         = Λ(τ₂ − xβ) − Λ(τ₁ − xβ)
P(支持)         = 1 − Λ(τ₂ − xβ)
Λ(z)            = 1 / (1 + e^(−z))
```

- `xβ` 是线性预测值，即自变量把这个观测的倾向推到了哪里；`Λ` 是 logistic 分布函数。

不同 xβ 下三类的概率：

| `xβ` | `P(反对)` | `P(中立)` | `P(支持)` |
|---|---|---|---|
| −2 | 0.731 | 0.222 | 0.047 |
| −1 | 0.500 | 0.381 | 0.119 |
| 0 | 0.269 | 0.462 | 0.269 |
| 1 | 0.119 | 0.381 | 0.500 |

xβ 每增加 1，“反对”一路下降，“支持”一路上升，两头的方向和系数符号一致。但“中立”先升后降：从 −2 到 −1 增加 0.159，从 0 到 1 减少 0.081。中间类别往哪个方向变，取决于起点在哪里，系数的符号说明不了它。这是读有序模型最重要的一点：必须换算成每个类别的预测概率。

**多项模型**：不假设共同的倾向，也没有门槛。选一个基准类别，其余每个类别各有一套系数，表示相对于基准的对数几率。它不要求类别有序，也就没有平行回归的约束；代价是参数多得多，J 个类别、p 个自变量要估 `(J − 1) × p` 个系数。

它自己的关键假设是无关选项的独立性，简称 IIA：任意两个类别的概率之比，不受其他类别存在与否的影响。一个例子：某国面对危机，谈判、制裁、军事行动的概率是 0.5、0.3、0.2。现在多出一个“有限空袭”的选项，拿走了 0.2 的概率。现实中它主要分流军事行动，谈判和制裁大致不变；多项 logit 却只能让三个旧选项按比例缩水，变成 0.4、0.24、0.16，谈判与军事行动之比始终是 2.5 比 1。

## 模型设定

**有序 logit 与有序 probit**：

```text
y_i* = x_i′β + ε_i
y_i = j    当 τ_{j−1} < y_i* ≤ τ_j ,   j = 1, …, J ,   τ_0 = −∞ ,  τ_J = +∞
P(y_i ≤ j) = F(τ_j − x_i′β)
P(y_i = j) = F(τ_j − x_i′β) − F(τ_{j−1} − x_i′β)
```

- `y_i*` 是看不见的倾向；`x_i` 是自变量，不含截距，截距的作用由门槛承担；
- `β` 只有一套，对所有类别通用；
- `τ_j` 是门槛，也叫切点，共 J − 1 个，从小到大排列；
- `F` 在有序 logit 里是 logistic 分布函数 Λ，在有序 probit 里是标准正态分布函数 Φ；
- 扰动的方差被固定为常数，logit 是 π²/3，probit 是 1，所以 β 只在这个尺度下有意义。logit 的系数大约是 probit 的 1.6 到 1.8 倍；同一个变量的系数，在加入其他变量后也会因为扰动方差的尺度变化而改变，不能直接拿来比；
- 按 R 的 `polr`、`clm` 和 Stata 的 `ologit` 的写法，β 为正表示倾向更高的类别；有的软件写成 `F(τ_j + x_i′β)`，符号正好相反，读代码时要确认。

**比例优势**：

```text
log[ P(y_i > j) / P(y_i ≤ j) ] = x_i′β − τ_j ,   所有 j 共用同一个 β
```

- `exp(β_k)` 读作：x_k 增加一个单位，“高于第 j 类”相对“不高于第 j 类”的几率乘以 `exp(β_k)`；无论在哪一道门槛切开，这个倍数都一样。

**Brant 检验**：

```text
对每道门槛 j 单独跑一个二元 logit，因变量是 1[y_i > j]：
    P(y_i > j) = Λ(α_j + x_i′β_j) ,   j = 1, …, J − 1
H₀: β_1 = β_2 = … = β_{J−1}
```

- `α_j` 是第 j 个二元 logit 的截距，对应门槛 τ_j 的位置，`β_j` 是它的系数；
- 比较 J − 1 组系数是否相等的 Wald 检验，考虑了它们之间的协方差；既有整体检验，也有逐个变量的检验；
- 样本很大时，微不足道的差异也会被判为显著，所以要同时看各组系数差多少、差异是否改变实质结论。

**广义有序 logit 与部分比例优势**：

```text
P(y_i > j) = Λ(α_j + x_1i′β + x_2i′β_j)
```

- `α_j` 是各门槛自己的截距；`x_1` 是满足平行回归的变量，系数在各门槛相同；`x_2` 是不满足的变量，系数 `β_j` 随门槛变化；
- 全部变量都放开，就是广义有序 logit；只放开一部分，就是部分比例优势模型；
- 代价是参数变多，而且累积概率曲线可能交叉，在某些自变量取值下算出负的类别概率，估计后必须检查。

**多项 logit**：

```text
P(y_i = j) = exp(x_i′β_j) / Σ_{m=1}^{J} exp(x_i′β_m) ,   β_1 = 0
log[ P(y_i = j) / P(y_i = 1) ] = x_i′β_j
∂P(y_i = j) / ∂x_ik = P(y_i = j) · ( β_jk − Σ_m P(y_i = m) · β_mk )
```

- 第 1 类是基准，它的系数固定为 0；`β_j` 是第 j 类相对基准的对数几率；`exp(β_jk)` 叫相对风险比；
- 第三行是边际效应：x_k 对第 j 类概率的影响，取决于 β_jk 和所有类别系数按概率加权的平均之差。所以 β_jk 为正，第 j 类的概率也可能下降，只要别的类别涨得更多；
- 换基准类别，系数全变，但预测概率完全不变。

**IIA**：

```text
P(y_i = j) / P(y_i = k) = exp( x_i′(β_j − β_k) )
```

- 两个类别的概率之比只取决于这两类自己的系数，和其他类别无关；
- Hausman 和 McFadden 的检验：去掉一个类别重新估计，比较剩下类别的系数有没有显著变化；Small 和 Hsiao 的检验思路类似。Cheng 和 Long 的模拟研究发现这些检验的表现很差，不同检验的结论常互相矛盾，因此更重要的是从实质上论证各选项是否彼此独立；
- IIA 不可信时，可以用嵌套 logit，把相近的选项放进同一组；或用多项 probit，允许各选项的扰动相关。

## 识别假设

有序和多项模型本身不是因果识别设计，下面是估计量需要的条件，以及每条不成立的代价。

1. **类别排在同一条维度上**：仅对有序模型。如果弃权不在赞成与反对之间，或者“展示武力”和“威胁使用武力”的先后因情境而异，有序模型就套错了结构。代价：系数混合了不同维度的作用，预测概率系统性出错。
2. **平行回归**：一个变量对每道门槛的作用相同。民主可能显著降低从威胁升到展示武力的可能，却几乎不影响冲突升级为战争；有序 logit 只能给出一个数，把两种作用平均掉。代价：两端类别的预测概率出错，真正有意思的异质作用被掩盖。
3. **扰动分布正确且方差相同**：如果某类国家的行为更难预测，扰动方差更大，β 就和尺度纠缠在一起，组间比较会出错。代价：系数的大小乃至概率效应的方向都可能错，异方差有序模型可以放宽这一条。
4. **IIA**：仅对多项 logit。代价：增减选项时的替代关系被强行设成按比例，系数也随之有偏。
5. **观测相互独立**：同一对国家年复一年地出现，误差高度相关。代价：标准误偏小，要聚类。若用单位固定效应，直接加一长串虚拟变量会带来伴生参数问题，系数有偏；有序 logit 可以用 Baetschmann 等人的估计量，Stata 里有社区命令 `feologit`。
6. **每个类别有足够的观测，没有完全分离**：战争这样的类别往往很稀少。代价：门槛和系数不稳定，某个自变量完全预测某一类时估计值发散到无穷。

## 稳健性检验：做什么，为什么

| 检验 | 检查哪条假设 | 怎么做，怎么看 |
|---|---|---|
| Brant 检验与分门槛二元 logit | 平行回归 | 在每道门槛跑一个二元 logit，把各组系数并排画出来，同时看 Brant 的整体和逐变量检验。大样本几乎总会拒绝，所以重点是差异的大小，以及它是否改变实质结论 |
| 部分比例优势或广义有序 logit | 平行回归 | 只放开违反假设的变量，比较各类别的预测概率与原模型有多大差别，并检查有没有负概率。差别小，说明平行回归的违反无关紧要；差别大，就该报告放开后的结果 |
| 有序 probit 与有序 logit 对照 | 扰动分布 | 两者的系数尺度不同，但预测概率应当几乎一样。差别明显，往往是某些类别太稀少、结果依赖分布尾部的形状 |
| 用多项 logit 重估 | 类别排在同一条维度上 | 不加有序约束，看各类别的预测概率是否大致按顺序变化。如果某个自变量把中间类别推向一边、却不影响两端，有序结构就值得怀疑 |
| 合并稀疏类别 | 稀疏类别、局部违反平行回归 | 比如把战争并入使用武力，重新估计。结论不变，说明它不靠少数极端观测撑着；但合并会丢信息、改变含义，两种结果都应报告，合并方式不能事后挑 |
| 异方差有序模型 | 方差相同 | 让扰动方差随组别变化，比如政体，看主要系数是否改变。组间比较是论文核心时尤其要做，因为尺度差异会伪装成效果差异 |
| 最小二乘作参照 | 结论不依赖分布假设 | 把类别当作等距数值跑线性回归，作为粗略参照。方向和显著性一致，说明结论不是有序模型的函数形式造出来的；不一致时要追问原因 |
| 去掉一个类别重估 | IIA | 多项 logit 去掉某个选项重新估计，看剩余类别的系数是否稳定；正式检验不可靠，把它当作诊断而不是判决。不稳定时改用嵌套 logit 或多项 probit |
| 聚类标准误与固定效应 | 观测独立、单位异质 | 标准误聚类在国家或国家对；有单位固定效应需求时，用一致的固定效应有序 logit，或以线性固定效应模型作对照 |

## 怎么读结果

- **有序模型的系数**：符号表示对倾向 y* 的作用方向；对最低和最高类别，方向可以直接读，对中间类别不行。
- **累积几率比**：`exp(β)` 是高于任意一道门槛的几率的倍数，不是概率的倍数，更不是某一类概率的变化。
- **门槛**：Stata 输出里的 `/cut1`、`/cut2`，R 的 `polr` 输出里的 `1|2`、`2|3`。它们本身很少有实质含义，间距反映各类别在倾向上占多宽。
- **预测概率和边际效应**：在有代表性的自变量取值下，算出每个类别的预测概率；或算每个类别的平均边际效应。同一个变量对各类别概率的边际效应加起来等于 0，一类涨了，别的类总共跌同样多。
- **多项模型的系数**：每一组都相对基准类别来读；看论文时先找基准是谁。实质结论应当用预测概率或边际效应来表述。
- **拟合指标**：伪 R² 只能在同一数据上比较模型；正确预测的比例要和全猜众数类别的基准比较。

读的时候可以这样说：控制实力对比和同盟关系后，两国都是民主国家时，争端升级到使用武力的概率从 0.18 降到 0.11，停在威胁阶段的概率相应上升；Brant 检验显示民主变量违反平行回归，放开它之后，民主对升级为战争的作用不再显著。

## 常见陷阱

- **直接读中间类别的系数符号**：中间类别的变化方向取决于起点，见核心思想里的例子。
- **把几率比当成概率的变化**：几率比 2 不是概率翻倍。
- **跨模型比较 logit 系数**：扰动尺度不同，系数大小不可比，比较预测概率或边际效应。
- **Brant 检验显著就改用多项 logit**：丢掉了有序信息，参数翻倍；先试部分比例优势模型。
- **广义有序模型出现负概率而不检查**。
- **忘了多项模型的基准类别**：同一组数据换个基准，系数面目全非。
- **类别太稀少**：几十个观测分到五个类别，有的类别只有几个观测，门槛估计极不稳定。
- **事后挑选合并方式**：看哪种合并显著就报哪种，等于在设定上做选择性报告。
- **在有序 logit 里直接加单位虚拟变量**：伴生参数问题使系数有偏，单位多而期数少时尤其严重。
- **交互项**：非线性模型里，交互项系数不等于交互作用对概率的影响，后者要在概率尺度上计算。

## 代码写法

R：

```r
library(MASS)
library(marginaleffects)

# 因变量必须是有序因子；polr 里 β 为正表示倾向更高的类别
df$hostility <- factor(df$hostility, levels = 1:5, ordered = TRUE)
m_ol <- polr(hostility ~ democracy + cap_ratio + alliance, data = df,
             method = "logistic", Hess = TRUE)   # method = "probit" 即有序 probit
summary(m_ol)                                     # Intercepts 一栏的 1|2、2|3 …… 是门槛

# 平行回归：Brant 检验，给出整体和逐个变量的结果
library(brant)
brant(m_ol)

# 每个类别的预测概率：把所有观测的 democracy 分别设为 0 和 1 再平均
avg_predictions(m_ol, variables = list(democracy = c(0, 1)))
avg_slopes(m_ol, variables = "democracy")         # 每个类别一行，加起来为 0

# 部分比例优势：nominal 里的变量在各门槛上有不同系数，不再出现在主公式里
library(ordinal)
m_pp <- clm(hostility ~ cap_ratio + alliance, nominal = ~ democracy, data = df)

# 多项 logit：先指定基准类别
library(nnet)
df$response <- relevel(factor(df$response), ref = "negotiate")
m_mn <- multinom(response ~ democracy + cap_ratio + alliance, data = df)
exp(coef(m_mn))                                   # 相对风险比，每行相对 negotiate
nd <- data.frame(democracy = c(0, 1), cap_ratio = mean(df$cap_ratio), alliance = 0)
predict(m_mn, newdata = nd, type = "probs")
```

Stata，`brant` 来自 Long 和 Freese 的 SPost13，`gologit2` 是 Williams 的社区命令：

```stata
* 有序 logit，标准误聚类在国家对
ologit hostility democracy cap_ratio alliance, vce(cluster dyad)

* 每个类别的预测概率和边际效应：outcome(5) 指 hostility = 5 这一类
margins, at(democracy = (0 1)) predict(outcome(5))
margins, dydx(democracy) predict(outcome(5))

* 平行回归：Brant 检验在普通标准误的模型上做
quietly ologit hostility democracy cap_ratio alliance
brant

* 部分比例优势：autofit 逐个放开违反平行回归的变量
gologit2 hostility democracy cap_ratio alliance, autofit

* 多项 logit，以 1 类为基准，报告相对风险比
mlogit response democracy cap_ratio alliance, baseoutcome(1) rrr vce(cluster country)

* IIA 的 Hausman 检验：去掉第 3 类重估，再与完整模型比较
quietly mlogit response democracy cap_ratio alliance, baseoutcome(1)
estimates store full
quietly mlogit response democracy cap_ratio alliance if response != 3, baseoutcome(1)
estimates store drop3
hausman drop3 full, alleqs constant
```

Python，statsmodels：

```python
import pandas as pd
import statsmodels.api as sm
from statsmodels.miscmodels.ordinal_model import OrderedModel

X_cols = ["democracy", "cap_ratio", "alliance"]
nd = pd.DataFrame({"democracy": [0, 1], "cap_ratio": df["cap_ratio"].mean(), "alliance": 0})

# 有序 logit：因变量设为有序分类；自变量里不能有常数项，门槛承担截距的作用
y = df["hostility"].astype(pd.CategoricalDtype(categories=[1, 2, 3, 4, 5], ordered=True))
m_ol = OrderedModel(y, df[X_cols], distr="logit").fit(method="bfgs", disp=False)
print(m_ol.summary())
probs = m_ol.model.predict(m_ol.params, exog=nd[X_cols])   # 每行一个观测，每列一个类别

# 多项 logit：排序最前的类别是基准
m_mn = sm.MNLogit(df["response"], sm.add_constant(df[X_cols])).fit(disp=False)
print(m_mn.summary())
print(m_mn.get_margeff().summary())             # 每个类别的平均边际效应
```

在作者代码里认有序与多分类模型，看这几处：R 的 `polr`、`clm`、`clmm`、`oglmx`，VGAM 里 `vglm` 配 `cumulative(parallel = ...)`，`multinom` 和 `mlogit` 包；Stata 的 `ologit`、`oprobit`、`gologit2`、`oglm`、`brant`、`mlogit`、`mprobit`、`nlogit`，以及 `margins` 后面跟 `predict(outcome(...))`；Python 的 `OrderedModel` 和 `MNLogit`。输出里也有记号：有序模型有一串切点，写作 `/cut1` 或 `1|2`；多项模型的系数表按类别分成几段，每段对应一个非基准类别。

## 延伸阅读

- Long, J. Scott. 1997. *Regression Models for Categorical and Limited Dependent Variables*. Sage. 有序因变量和名义因变量两章。
- Long, J. Scott, and Jeremy Freese. 2014. *Regression Models for Categorical Dependent Variables Using Stata*. 3rd ed. Stata Press.
- Agresti, Alan. 2010. *Analysis of Ordinal Categorical Data*. 2nd ed. Wiley.
- McCullagh, Peter. 1980. "Regression Models for Ordinal Data." *Journal of the Royal Statistical Society, Series B* 42 (2).
- Brant, Rollin. 1990. "Assessing Proportionality in the Proportional Odds Model for Ordinal Logistic Regression." *Biometrics* 46 (4).
- Williams, Richard. 2006. "Generalized Ordered Logit/Partial Proportional Odds Models for Ordinal Dependent Variables." *Stata Journal* 6 (1).
- Hausman, Jerry, and Daniel McFadden. 1984. "Specification Tests for the Multinomial Logit Model." *Econometrica* 52 (5).
- Train, Kenneth E. 2009. *Discrete Choice Methods with Simulation*. 2nd ed. Cambridge University Press.
