---
key: fixed_effects
name: 固定效应与面板数据
english: Fixed Effects and Panel Data
summary: 给每个单位、每个时期各配一个截距，只用同一单位内部随时间的变化来估计，把单位之间不随时间变化的差别整个扣掉。
---

## 适用的问题

同一批单位被反复观察，形成面板数据：国家—年份、二元组—年份、议员—会期、每届政府下的历次会议。我们担心单位之间有一些说不清、量不到的差别，而这些差别同时影响自变量和结果。比如：

- 研究对外援助和受援国治理：受援国的殖民历史、官僚传统、地理位置，既影响它拿到多少援助，也影响它的治理水平；
- 研究贸易和冲突：两国之间的距离、共同语言、历史宿怨，既影响贸易量，也影响打仗的可能；
- 研究某届政府的决策：领导人的个人风格和执政理念，影响这一届里的每一次会议。

这类差别大多长期稳定，难以穷举，更难测量。固定效应回答的是一个收窄了的问题：同一个国家，在 X 比自己平时高的年份，Y 是否也比自己平时高？它不再拿瑞典和苏丹比，而是拿瑞典和过去的瑞典比。

## 核心思想

混合回归把所有观测堆在一起，系数里既有单位内部的关系，也有单位之间的差别。只要那些稳定的差别和 X 相关，系数就有偏。

一个数字例子，两个国家各观察三年：

| | 第 1 年 | 第 2 年 | 第 3 年 | 本国均值 |
|---|---|---|---|---|
| 甲国 X | 1 | 2 | 3 | 2 |
| 甲国 Y | 10 | 11 | 12 | 11 |
| 乙国 X | 5 | 6 | 7 | 6 |
| 乙国 Y | 2 | 3 | 4 | 3 |

把六个点放在一起做回归，斜率约为 −1.57：X 越高，Y 越低。可是在每个国家内部，X 每增加 1，Y 都增加 1。负号完全来自两国之间的差别：甲国有某种稳定的特征，让它 X 低而 Y 高。

固定效应的做法是先把每个观测减去本国的均值，叫作组内变换：

| | 第 1 年 | 第 2 年 | 第 3 年 |
|---|---|---|---|
| 甲国 X − 本国均值 | −1 | 0 | 1 |
| 甲国 Y − 本国均值 | −1 | 0 | 1 |
| 乙国 X − 本国均值 | −1 | 0 | 1 |
| 乙国 Y − 本国均值 | −1 | 0 | 1 |

减完之后再回归，斜率是 1。凡是在一个国家内部不随时间变化的东西，减去本国均值后都变成 0；不管它有没有被测量，都不会再影响估计。

这也说明了固定效应用的是哪部分变化：每个观测偏离本单位平均水平的部分。单位之间平均水平的差异全部丢掉了。由此有两个直接后果，后面会反复出现：

- 一个单位内部 X 从不变化，这个单位对估计 β 没有任何贡献；
- 一个变量在单位内部从不变化，比如国土面积、是否曾是殖民地，减去均值后处处为 0，它的系数根本估不出来。

## 模型设定

**单位固定效应**：

```text
Y_it = α_i + β·X_it + ε_it
```

- `i` 是单位，`t` 是时期；
- `α_i` 是单位 i 自己的截距，吸收它一切不随时间变化的特征，测量了的和没测量的都在里面；
- `β` 是要估计的组内效应；
- `ε_it` 是剩下的误差。

把每个变量减去本单位的时间均值，α_i 就消掉了：

```text
(Y_it − Ȳ_i) = β·(X_it − X̄_i) + (ε_it − ε̄_i)
```

- `Ȳ_i`、`X̄_i`、`ε̄_i` 是单位 i 各期的平均值。

给每个单位放一个哑变量再做普通回归，叫最小二乘虚拟变量法，得到的 β 和组内变换完全相同，只是标准误的自由度修正可能不同。

**时间固定效应**：

```text
Y_it = λ_t + β·X_it + ε_it
```

- `λ_t` 是时期 t 自己的截距，吸收同一时期打在所有单位身上的冲击，比如全球金融危机、冷战结束、大宗商品价格的起落；
- 只放时间固定效应，比较的是同一年里不同单位之间的差别，单位间稳定的差别仍然留在里面。

**双向固定效应**：

```text
Y_it = α_i + λ_t + β·X_it + ε_it
```

在平衡面板里，它等价于把每个变量做双重去均值：

```text
Ẍ_it = X_it − X̄_i − X̄_t + X̄
```

- `X̄_t` 是时期 t 所有单位的均值，`X̄` 是总均值；
- 剩下的变化是：这个单位这一年的 X，既偏离它自己的常态、又偏离这一年所有单位的常态的那部分；
- 面板不平衡时这个公式不再精确，软件会反复交替去均值直到收敛，结果仍然等价于放两套哑变量。

**更细的固定效应**。论文里写「某某固定效应」，意思就是给某某的每一个取值一个截距，于是所有比较都只在同一个取值的内部进行：

- **administration fixed effects，政府届别固定效应**：每届政府一个截距。比较限于同一届政府之内，领导人的个性、执政党、任期内不变的政策路线都被吸收；只在届与届之间变化的变量估不出来。
- **country-year fixed effects，国家—年份固定效应**：出现在比国家—年份更细的数据里，比如省份—年份、二元组—年份、个人调查。它吸收一切在某国某年取同一个值的东西，GDP、政体、是否选举年全部被吸收，剩下的只是同一国家同一年内部的差别，比如不同省份之间、不同贸易伙伴之间的差别。
- **dyad fixed effects，二元组固定效应**：每一对国家一个截距，吸收两国之间的距离、是否接壤、历史关系等。
- **exporter-year 加 importer-year 固定效应**：引力模型里常见，分别吸收每个国家每年的整体出口能力和进口需求。
- **region × year fixed effects，地区 × 年份固定效应**：允许每个地区有自己的年度冲击，只在同一地区同一年里比较。

读到这些说法时问两个问题：比较是在什么范围内进行的？哪些变量因此被吸收、不可能再有系数？

**随机效应**，作为对照：

```text
Y_it = μ + β·X_it + u_i + ε_it,   u_i ~ (0, σ_u²),   Cov(u_i, X_it) = 0
```

- `μ` 是总截距；
- `u_i` 是单位的随机偏离，和固定效应里的 α_i 是同一样东西，区别在于这里假定它和 X 不相关；
- `σ_u²` 是单位间的方差，`σ_ε²` 是单位内的方差。

随机效应的估计相当于只减去均值的一部分：

```text
Y_it − θ·Ȳ_i = (1 − θ)·μ + β·(X_it − θ·X̄_i) + 误差
θ = 1 − √( σ_ε² / (σ_ε² + T·σ_u²) )
```

- `T` 是每个单位的期数；
- θ = 0 时就是混合回归，θ = 1 时就是固定效应。单位间差异越大、期数越多，θ 越接近 1，随机效应就越接近固定效应。

**Hausman 检验**比较两者：

```text
H = (β̂_FE − β̂_RE)′ [Var(β̂_FE) − Var(β̂_RE)]⁻¹ (β̂_FE − β̂_RE)
```

- `β̂_FE`、`β̂_RE` 是两种方法对同一组系数的估计；
- 原假设是 u_i 和 X 不相关。此时两种估计都一致，随机效应更有效，两者应当接近；
- 备择假设下只有固定效应一致，两者会拉开；
- 原假设下 H 服从卡方分布，自由度是比较的系数个数。

**动态面板**，即加入滞后因变量：

```text
Y_it = α_i + ρ·Y_i,t−1 + β·X_it + ε_it
```

- `ρ` 是持续性，表示今年的 Y 有多少延续自去年。它和固定效应放在一起会产生 Nickell 偏误，见常见陷阱。

## 识别假设

1. **严格外生**：扣除单位固定效应之后，任何一期的误差都和任何一期的 X 不相关，写作 `E(ε_it | X_i1, …, X_iT, α_i) = 0`。它比看上去严格：不仅要求没有同期的时变混杂，还要求过去的 Y 不会反过来影响后来的 X。如果今年冲突加剧导致明年援助减少，就违反了这一条。
2. **混杂只存在于固定效应所在的层级**：固定效应只扣掉在它那一层恒定的东西。单位固定效应扣不掉随时间变化的混杂，比如一场国内经济危机同时改变援助和治理；国家—年份固定效应扣掉了国家层面的一切时变因素，却扣不掉省份层面的。
3. **单位内部有足够的变化**：β 只靠 X 在单位内部的变化来识别。变化少，估计就由少数单位决定，而且噪声很大。
4. **效应同质，或者接受一个加权平均**：各单位的真实效应不同时，固定效应估计的是一个加权平均，X 在内部变化大的单位权重更大，并不是总体的平均效应。处理是二值变量、各单位先后在不同时间受处理时，双向固定效应还会出现负权重，详见双重差分一章。
5. **随机效应额外需要 u_i 和 X 不相关**：这条一旦违反，随机效应的 β 就同时混有组内和组间的关系，偏误的方向和大小都不可知。

政治学和国际关系研究多用固定效应，原因正在第 5 条：研究者担心的恰恰是那些看不见的单位特征和 X 相关，而随机效应要假定它们不相关。在大样本里，Hausman 检验也常常拒绝随机效应。

但固定效应不是免费的：它丢掉了全部组间变化，估不出不随时间变化的变量，每个单位期数很少时估计很不精确。单位多、期数少、组间差异本身就是研究兴趣时，随机效应或多层模型可能更合适。一个折中是 Mundlak 设定：在随机效应模型里加入各单位 X 的均值，X 本身的系数就等于固定效应估计，均值的系数显示组间关系，均值项是否为 0 本身就是一个 Hausman 式的检验。

## 稳健性检验：做什么，为什么

每一项检验都对应上面的某条假设。读论文时，可以对照着看作者做了哪些、漏了哪些。

| 检验 | 检查哪条假设 | 怎么做，怎么看 |
|---|---|---|
| 混合回归、单向、双向固定效应并排报告 | 不随时间变化的混杂有多重 | 加入单位固定效应后系数大幅缩小甚至变号，说明组间差别曾在起作用，混合回归不可信；系数基本不变，说明稳定的单位特征不是主要问题 |
| 加入时间固定效应或地区 × 年份固定效应 | 共同冲击和地区性冲击 | 某一时期所有单位的 X 和 Y 一起变动时，只放单位固定效应会把这种共同波动当成 X 的效果。加入后系数稳定，说明结果不是全球或地区的同步波动造成的 |
| 加入单位各自的线性趋势 | 时变混杂中缓慢、平滑的那部分 | 允许每个国家沿自己的直线发展，只用偏离自身趋势的变化来识别。系数稳定是好消息；系数消失说明原结果可能来自各国长期走势的差异。但趋势也可能吸收掉逐年累积的真实效应 |
| 加入下一期的 X | 严格外生、反向因果 | 未来的 X 不可能影响今天的 Y，它的系数应当接近 0。显著说明 Y 在推动 X，或者两者共享一个固定效应扣不掉的时变趋势 |
| 加入时变控制变量 | 时变混杂 | 只加在 X 之前就确定、本身不受 X 影响的变量。主系数变化越小越好；加入 X 的结果会挡掉一部分效应，是坏控制 |
| 统计内部变化，逐个剔除单位 | 单位内部有足够的变化 | 报告有多少个单位的 X 发生过变化，以及扣掉固定效应后 X 的标准差；每次去掉一个单位重估一遍。结果只靠两三个国家撑着时，结论其实是关于这几个国家的 |
| Hausman 检验或 Mundlak 检验 | 随机效应的无相关假设 | 拒绝原假设说明随机效应有偏，应当用固定效应。经典 Hausman 检验要求随机效应在原假设下有效，即误差同方差、无序列相关；用了聚类标准误时，改用 Mundlak 回归，检验均值项是否联合为 0 |
| 聚类标准误 | 误差的序列相关 | 固定效应只扣掉误差里恒定的部分，同一国家各年的误差仍然相关，不聚类时标准误会被严重低估。聚类在单位层级，或在 X 被分配的层级；只有十几二十个聚类时，用野聚类自助法 |
| 固定效应和滞后因变量两种设定对比 | 动态设定是否正确 | Angrist 和 Pischke 指出，在常见的设定下，这两种估计从两侧夹住真值。两种结果方向一致、大小接近，结论更可信 |
| 换成条件 logit 或固定效应泊松 | 二值或计数结果的函数形式 | 线性概率模型加固定效应简单透明，但预测值可能越出 0 到 1。换成非线性固定效应模型后符号和显著性不变，说明结论不是函数形式造成的 |

需要强调一点：这些检验只能间接说明时变混杂不严重，全部通过也不等于 β 有因果解释。要做因果推断，还得说清楚 X 在单位内部为什么会变化，以及这种变化为什么可以看作外生。

## 怎么读结果

- **β 的含义**：同一单位内，X 比它自己的平均水平高一个单位时，Y 平均比它自己的平均水平高多少。这是组内效应，不能读成「X 高的国家比 X 低的国家 Y 高多少」。
- **「一个单位」是否现实**：先看 X 在单位内部的标准差。比如某个政体指标全样本标准差是 7，扣掉国家固定效应后只剩 1.5，那么按「上升 7 分」来解读效应，就远远超出了绝大多数国家实际经历过的变化。合理的做法是用组内标准差来换算效应大小。
- **两个 R²**：带固定效应的模型的总 R² 往往很高，主要来自那一堆单位截距；组内 R² 才说明 X 解释了多少单位内部的变化。fixest 报 Within R2，Stata 的 xtreg 分别报 within、between、overall，areg 报的 R² 含有哑变量的贡献。
- **表格底部**：固定效应通常不出现在系数表里，只在底部写「Country FE: Yes」「Year FE: Yes」。要对照正文看清放了哪几套，以及标准误聚类在哪一层。
- **消失的变量**：不随时间变化的控制变量在固定效应模型里没有系数，软件会提示因共线而删除。这不是作者漏报。
- **观测数**：只有一个观测的组对估计没有贡献，reghdfe 默认把它们去掉；条件 logit 去掉结果从不变化的单位，固定效应泊松去掉结果全为 0 的单位。所以同一份数据在不同模型里观测数不同，复现时要先对这个数。

读的时候可以这样说：控制国家和年份固定效应后，同一个国家在 X 比自身平均水平高一个组内标准差的年份，Y 平均高出 β 乘以这个标准差；这一估计来自样本中 X 发生过变化的那些国家。

## 常见陷阱

- **以为固定效应控制了一切**：它只扣掉在其层级上恒定的东西。随时间变化的混杂、反向因果，都原封不动。
- **想估计不随时间变化的变量**：比如在国家固定效应模型里报告「是否为前殖民地」的系数。真正不变的变量会被删掉；变化极少的变量，比如几十年里只变过一两次的制度变量，系数靠极少数变化估出来，标准误巨大，结果很不稳定。
- **测量误差被放大**：X 很稳定时，去均值去掉的大多是真实信号，留下的组内变化里噪声占比更高，系数向 0 的衰减比混合回归更严重。加入固定效应后系数变小，不一定是混杂被扣掉了，也可能是测量误差。
- **二值结果加单位固定效应**：条件 logit 会扔掉结果从不变化的单位。在二元组—年份的冲突数据里，绝大多数国家对从未发生过冲突，加上二元组固定效应后它们全部退出样本，估计只剩下那些发生过冲突的国家对。国际关系方法论里为此有过一场著名的争论：一方认为这样才控制了二元组之间看不见的差别，另一方认为这等于扔掉了和平本身的信息，也让距离、接壤这些不变的变量无从估计。两种设定都要读，结论不同时要说清楚各自代表什么样本。
- **非线性模型直接加大量哑变量**：probit 加单位哑变量，在每个单位期数少时估计不一致，这叫附带参数问题。logit 应改用条件 logit；泊松模型没有这个问题。
- **聚类与自由度修正**：areg、xtreg、reghdfe 在小样本修正中如何计入被吸收的固定效应，处理方式不同，聚类标准误会有差别。复现时标准误在小数点后第二三位对不上，常常就是这个原因，不一定是数据出错。另外，作者没有写明标准误类型时，用的是软件的默认设置，而默认值可能随版本变化，复现前要先确认这一点。
- **交错的二值处理用双向固定效应**：效果随时间变化时会有负权重，详见双重差分一章。

**滞后因变量与 Nickell 偏误**。在固定效应模型里加入 `Y_i,t−1`，去均值后的滞后项 `Y_i,t−1 − Ȳ_i` 里含有各期误差的平均，而当期误差 ε_it 正是这个平均的一部分，于是解释变量和误差相关。偏误的近似大小是：

```text
plim(ρ̂ − ρ) ≈ −(1 + ρ) / (T − 1)
```

- `ρ` 是真实的持续性参数，`ρ̂` 是固定效应估计，`T` 是期数；
- 偏误为负，随期数增多而缩小，量级约为 1/T。

算一下：ρ = 0.5、T = 10 时，偏误约为 −1.5 / 9 ≈ −0.17，估出来的 ρ 只有 0.33 左右；T = 40 时，偏误约为 −0.04。滞后项的偏误还会通过它和 X 的相关传给 β。所以在几十年的国家—年份面板里，这个问题通常不大；在只有五到十期的面板里，必须当真。常见的对策是 Arellano–Bond 差分 GMM 和系统 GMM，用更早的滞后值作工具；它们本身对工具的个数和误差的序列相关很敏感，工具太多时过度识别检验会失去作用。另一个办法是像上表那样，同时报告固定效应和滞后因变量两种设定。

## 代码写法

R，常用 fixest，随机效应和动态面板用 plm：

```r
library(fixest)
# 单位固定效应：竖线后面是固定效应，标准误聚类在国家
m1 <- feols(y ~ x + z | country, data = df, cluster = ~country)

# 双向固定效应
m2 <- feols(y ~ x + z | country + year, data = df, cluster = ~country)

# 省份—年份数据里的国家—年份固定效应：^ 把两个变量交叉成一套截距
m3 <- feols(y ~ x | province + country^year, data = df, cluster = ~province)

# 引力模型：二元组固定效应加出口国—年份、进口国—年份固定效应
m4 <- feols(trade ~ x | dyad + exporter^year + importer^year, data = df, cluster = ~dyad)

# 国家各自的线性趋势：country[year] 只给每个国家一个专属的年份斜率，不含国家截距，所以要和 country 一起写
m5 <- feols(y ~ x | country + country[year] + year, data = df, cluster = ~country)

# 加入下一期的 x 作检验；f() 取领先值，需要声明面板结构
m6 <- feols(y ~ x + f(x) | country + year, data = df,
            panel.id = ~country + year, cluster = ~country)

etable(m1, m2, m5, m6)
fitstat(m2, ~ wr2)   # 组内 R²

# Mundlak 检验：把每个国家的均值放进不带固定效应的回归，检验均值项是否联合为 0
df$x_bar <- ave(df$x, df$country)
df$z_bar <- ave(df$z, df$country)
mk <- feols(y ~ x + z + x_bar + z_bar, data = df, cluster = ~country)
wald(mk, keep = "_bar$")

# 随机效应与经典 Hausman 检验
library(plm)
pdf <- pdata.frame(df, index = c("country", "year"))
fe <- plm(y ~ x + z, data = pdf, model = "within")
re <- plm(y ~ x + z, data = pdf, model = "random")
phtest(fe, re)

# 短面板的动态模型：差分 GMM，用 y 的二阶及更早滞后作工具
gmm <- pgmm(y ~ lag(y, 1) + x | lag(y, 2:99), data = pdf,
            effect = "individual", model = "twosteps")
summary(gmm, robust = TRUE)
```

Stata：

```stata
xtset country year

* 单位固定效应，聚类在国家
xtreg y x z, fe vce(cluster country)

* 双向固定效应
xtreg y x z i.year, fe vce(cluster country)

* 多套固定效应用 reghdfe，需先 ssc install reghdfe
reghdfe y x z, absorb(country year) vce(cluster country)

* 国家—年份固定效应：# 把两个变量交叉
reghdfe y x, absorb(province country#year) vce(cluster province)

* 国家各自的线性趋势
reghdfe y x, absorb(country##c.year) vce(cluster country)

* 加入下一期的 x
reghdfe y x F.x, absorb(country year) vce(cluster country)

* 经典 Hausman 检验：要用不带稳健标准误的估计，先放一致的，再放有效的
xtreg y x z, fe
estimates store fe
xtreg y x z, re
estimates store re
hausman fe re

* Mundlak 检验，可以配聚类标准误
bysort country: egen x_bar = mean(x)
bysort country: egen z_bar = mean(z)
xtreg y x z x_bar z_bar, re vce(cluster country)
test x_bar z_bar

* 动态面板：Arellano–Bond 差分 GMM
xtabond y x, lags(1) vce(robust)
```

Python：

```python
import pyfixest as pf
# 双向固定效应，标准误聚类在国家
m = pf.feols("y ~ x + z | country + year", data=df, vcov={"CRV1": "country"})
m.summary()

from linearmodels.panel import PanelOLS, RandomEffects
# linearmodels 要求两层索引：先单位，后时间
panel = df.set_index(["country", "year"])
fe = PanelOLS.from_formula("y ~ x + z + EntityEffects + TimeEffects", data=panel)
fe_res = fe.fit(cov_type="clustered", cluster_entity=True)
re_res = RandomEffects.from_formula("y ~ 1 + x + z", data=panel).fit()
```

在作者代码里认固定效应，看这几处：fixest 和 pyfixest 公式里竖线 `|` 后面的变量，以及 `^` 交叉和 `country[year]` 这样的单位趋势；公式里直接加的 `factor(country)`、`as.factor(year)`，Python 的 `C(country)`；`plm(..., model = "within")`，`felm` 公式的第二段；Stata 的 `xtreg, fe`、`areg, absorb()`、`reghdfe, absorb()`、`i.country`、`country#year`；linearmodels 的 `EntityEffects`、`TimeEffects`。出现 `hausman`、`phtest`，说明作者比较过随机效应；出现 `xtabond`、`xtabond2`、`pgmm`，说明是动态面板。

## 延伸阅读

- Wooldridge, Jeffrey M. 2010. *Econometric Analysis of Cross Section and Panel Data*. 2nd ed. MIT Press. 第 10 章。
- Angrist, Joshua D., and Jörn-Steffen Pischke. 2009. *Mostly Harmless Econometrics*. Princeton University Press. 第 5 章。
- Nickell, Stephen. 1981. "Biases in Dynamic Models with Fixed Effects." *Econometrica* 49 (6).
- Green, Donald P., Soo Yeon Kim, and David H. Yoon. 2001. "Dirty Pool." *International Organization* 55 (2).
- Clark, Tom S., and Drew A. Linzer. 2015. "Should I Use Fixed or Random Effects?" *Political Science Research and Methods* 3 (2).
- Mummolo, Jonathan, and Erik Peterson. 2018. "Improving the Interpretation of Fixed Effects Regression Results." *Political Science Research and Methods* 6.
- Imai, Kosuke, and In Song Kim. 2019. "When Should We Use Unit Fixed Effects Regression Models for Causal Inference with Longitudinal Data?" *American Journal of Political Science* 63 (2).
- Cameron, A. Colin, and Douglas L. Miller. 2015. "A Practitioner's Guide to Cluster-Robust Inference." *Journal of Human Resources* 50 (2).
