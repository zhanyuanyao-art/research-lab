---
key: topic
name: 主题模型
english: Topic Models, LDA / STM / keyATM
summary: 把每篇文档看成几个主题按比例混合、每个主题看成一组常一起出现的词，从大量文本里同时估出这两样东西；它是一件测量工具，主题的含义和个数都要靠阅读和验证来确立。
---

## 适用的问题

手里有大量文本，想知道里面在谈什么、各谈多少，以及谈论的重心怎样随时间、随说话者变化，但事先没有完整的分类表。比如：

- 各国在联合国大会一般性辩论中的发言，冷战结束后安全议题和发展议题的比重怎样变化；
- 外交部例行记者会上，涉及某个邻国的答问更常谈主权、经济合作还是历史问题；
- 议会关于对外援助的辩论，执政党和反对党的关注点有什么不同；
- 开放式问卷里，受访者怎样解释自己支持或反对出兵。

主题模型在这里承担两种角色。一是发现：读不完的文本里有哪些反复出现的话题，研究者事先未必想得到。二是测量：给每篇文档算出它在各主题上的比重，再把比重当作变量，描述趋势或放进回归。前一种用途容忍一定的随意，后一种要求主题含义稳定、经得起验证。

它和另外两类文本方法的分工是：类别和关键词事先清楚，用词典法；类别清楚、又有人工编码好的训练样本，用有监督分类；类别本身不清楚，才轮到主题模型。

## 核心思想

主题模型先做一个大胆的简化：只看每篇文档里每个词出现了几次，不管词序，这叫词袋。整个语料于是变成一张文档-词频矩阵，每行一篇文档，每列一个词。

在此基础上，模型假设文档是这样写出来的：

- 每个主题是词表上的一个概率分布，比如“安全”主题里，导弹、军事这些词的概率高；
- 每篇文档有自己的一组主题比例；
- 写下每个词时，先按这篇文档的主题比例抽一个主题，再按这个主题的词分布抽一个词。

估计的过程反过来：只看到词频矩阵，推断各主题的词分布和各文档的主题比例。总在同一批文档里一起出现的词，会被归进同一个主题。

一个数字例子。词表只有五个词，两个主题：

| 词 | 主题 1 | 主题 2 |
|---|---|---|
| 导弹 | 0.40 | 0.00 |
| 军事 | 0.40 | 0.00 |
| 合作 | 0.20 | 0.20 |
| 贸易 | 0.00 | 0.40 |
| 关税 | 0.00 | 0.40 |

一篇主题比例为 0.75 和 0.25 的文档，每 100 个词里期望有导弹 30 个、军事 30 个、合作 20 个、贸易 10 个、关税 10 个。以合作为例：

```text
0.75 × 0.20 + 0.25 × 0.20 = 0.20
```

模型看到的只是这组词频，要反推出 0.75 和 0.25。例子里有三点值得记住：

- 模型不会给主题起名字。“主题 1”叫“安全”，是研究者读了高频词和代表性文档之后的解释，这个解释需要验证；
- 同一个词可以属于多个主题。“合作”在某篇文档里归哪个主题，取决于同一篇里的其他词；
- 主题比例是估计值。短文档词少，比例估得很不准，后面把它当变量用时会成为问题。

## 模型设定

**LDA**，潜在狄利克雷分配：

```text
对每个主题 k = 1, …, K：     β_k ~ Dirichlet(η)
对每篇文档 d = 1, …, D：     θ_d ~ Dirichlet(α)
  对文档 d 的第 n 个词：     z_dn ~ Categorical(θ_d)
                             w_dn ~ Categorical(β_{z_dn})
```

- `K` 是主题个数，由研究者指定；`D` 是文档数；`Categorical` 表示按给定的概率抽出一个类别；
- `β_k` 是主题 k 在整个词表上的概率分布，就是例子表里的一列；
- `θ_d` 是文档 d 的主题比例，K 个非负数，加起来等于 1；
- `z_dn` 是第 n 个词所属的主题，看不到，要推断；`w_dn` 是实际观察到的词；
- `α`、`η` 是狄利克雷先验的参数。α 越小，每篇文档越集中在少数主题；η 越小，每个主题越集中在少数词。

**结构主题模型 STM**，让文档的元数据进入模型：

```text
η_d ~ Normal(Γ′x_d, Σ) ,   θ_dk = exp(η_dk) / Σ_j exp(η_dj) ,   η_dK = 0
β_{d,k,v} ∝ exp( m_v + κ^主题_{k,v} + κ^内容_{g_d,v} + κ^交互_{g_d,k,v} )
```

- `η_d` 是文档 d 未经变换的一组主题权重，只有 K − 1 个自由取值，第 K 个固定为 0；经第一行的变换后变成加起来等于 1 的比例 `θ_d`；
- `x_d` 是主题流行度的协变量，比如发言国的政体、年份；`Γ` 是它们对主题比例的作用。“民主国家更多谈人权”这类问题由此直接进入模型；
- `Σ` 是主题之间的协方差，允许主题相关，比如谈核问题的文档也常谈制裁，LDA 的狄利克雷分布做不到这一点；
- `β_{d,k,v}` 是文档 d 里主题 k 选中词 v 的概率，比 LDA 多了下标 d，因为用词可以随文档所属的组变化；`g_d` 是内容协变量，即文档所属的组；`m_v` 是词 v 在全语料的基准对数频率，各个 `κ` 是相对基准的偏离。内容协变量让同一主题在不同组里用词不同，比如同样谈人权，不同政体的国家选用的词不一样；
- 没有任何协变量时，STM 退化为相关主题模型。

**keyATM**，用关键词给主题打种子：

```text
关键词主题 k 中的每个词：  s_dn ~ Bernoulli(π_k)
  s_dn = 1：  w_dn ~ Categorical(β̃_k)，只在主题 k 的关键词里选
  s_dn = 0：  w_dn ~ Categorical(β_k)，在整个词表里选
```

- 研究者事先给部分主题各列几个关键词，比如“核”主题列 nuclear、warhead、enrichment；另留若干个没有关键词的主题，吸收其余内容；
- `s_dn` 是一个开关，决定这个词从关键词分布还是从普通分布里抽；`β̃_k` 只在关键词上取正值；`π_k` 是主题 k 的词来自关键词分布的概率，由数据估计；
- 关键词只是把主题锚住，主题仍会吸收大量没列出的词，比如 centrifuge 可能被学进“核”主题；
- 好处是主题从一开始就和研究者的概念对上号，不必事后命名，多次估计的结果也更稳定；代价是关键词的选择本身成了需要论证和检验的决定。

**选主题个数 K**。常用的指标之一是语义一致性：

```text
C(k) = Σ_{i=2}^{M} Σ_{j=1}^{i−1} log[ (D(v_i, v_j) + 1) / D(v_j) ]
```

- `v_1, …, v_M` 是主题 k 概率最高的 M 个词，按概率从高到低排；
- `D(v)` 是含有词 v 的文档数，`D(v_i, v_j)` 是同时含有两个词的文档数；
- 高频词确实常在同一批文档里出现，C 就接近 0；越负，主题越像几个不相干的词拼在一起。

另一个指标是排他性：一个主题的高频词，在其他主题里是否也高频。一致性偏爱少而宽的主题，排他性偏爱多而窄的主题，两者此消彼长。此外还有留出似然，即模型对没参与估计的词预测得多好。

K 没有唯一正确的值。它更像地图的比例尺：K 取 10，“安全”是一个主题；K 取 40，它会分成核不扩散、反恐、维和、领土争端。两张地图都没有错，只是粒度不同。统计拟合常随 K 增大而一直改善，人能读懂的程度却不跟着改善，Chang 等人的实验发现，留出似然更好的模型，主题往往反而更难被人理解。所以 K 要按研究问题需要的粒度来定，用一致性、排他性和阅读来辅助，并报告结论在相邻几个 K 下是否成立。

**把主题比例当变量用**：

```text
作结果：    θ̂_dk = λ_0 + x_d′λ + u_d
作自变量：  Y_d = α + Σ_{k≠r} γ_k · θ̂_dk + W_d′δ + ε_d
```

- 作结果时，STM 的 `estimateEffect` 从主题比例的近似后验里反复抽样，每次跑一遍回归再合并，把测量的不确定性带进标准误；
- 作自变量时，各主题比例加起来等于 1，必须留出一个参照主题 `r`，否则和截距完全共线。`γ_k` 读作把文本比重从参照主题挪到主题 k 的效果，比例变化 0.1 就是 10 个百分点；
- `θ̂` 是估计值。短文档的比例噪声大，作自变量时会把系数往 0 拉；
- 只在 `estimateEffect` 里出现、却没进流行度模型的变量，主题比例已被先验往不含它的方向拉过，它和主题的关系容易被低估，所以一般让两处的协变量保持一致。

## 识别假设

主题模型是测量工具，不是因果识别设计。这里列的是把主题当作测量所需的条件，以及每条不成立的代价。

1. **词袋足以表达要测的概念**：模型不看词序，也不分谁对谁做了什么。它能看出一篇文档在谈制裁，看不出是谁制裁谁，也看不出赞成还是反对。代价：主题测的是关注什么，不是立场；拿它当立场用，结论就错了对象。
2. **主题的含义在语料中保持不变**：没有内容协变量时，同一主题在所有年份、所有说话者那里共用一套词分布。如果“安全”的词汇在 2001 年后转向反恐，固定的主题要么分裂，要么混杂。代价：比重的变化有一部分只是用词的变化。
3. **研究者给的标签和主题内容一致**：代价最大也最常见。叫“人权”的主题实际可能是更宽的“国际规范”，于是关于人权的结论落在了另一个概念上。
4. **结果不依赖初始值**：主题模型的似然函数有许多局部最优，不同随机种子可能得到不同的主题。代价：某个发现可能只存在于某一次估计里。
5. **预处理不改变实质结论**：词干化、停用词、低频词、二元词组这些选择会改变词频矩阵。代价：主题和下游结果随之改变，而读者看不到这些选择。
6. **作变量时的测量误差已被处理**：代价是系数衰减和过窄的标准误。还有一种循环：研究者在全样本上反复调整 K 和标签，直到主题和结果出现想要的关系，再在同一批文档上检验它。
7. **keyATM 的关键词独立于结果选定，而且确实刻画概念**：太宽的关键词，比如 security，会把不相干的内容拉进主题；看过结果再挑关键词，等于把结论预先写进了测量。

## 稳健性检验：做什么，为什么

| 检验 | 检查哪条假设 | 怎么做，怎么看 |
|---|---|---|
| 通读代表性文档 | 标签和内容一致 | 每个主题抽出比例最高的 10 到 20 篇通读，再随机抽几篇比例中等的。要做这一步，是因为高频词只是主题的剪影，标签对不对要看文档实际在说什么；代表性文档讲的是另一回事，标签就得改 |
| 一致性与排他性 | K 的选择、主题质量 | 对一组候选 K 画出各主题的一致性和排他性，挑位于两者前沿、粒度又合乎研究问题的 K。两者此消彼长，只看一个会系统地偏向过少或过多的主题 |
| 在多个 K 下重做主分析 | 结论不依赖 K | 主结果在相邻几个 K 下都应成立。必须做，是因为 K 没有唯一正确的值；只在某个 K 下成立的结论，更可能是切分方式的产物 |
| 词语入侵与主题入侵 | 主题对人可解释 | 把别的主题的一个高频词混进某主题的前五个词，看编码员能否挑出；或给一篇文档配几个主题，其中一个是混进来的。挑得出，说明主题对人有清晰含义；统计拟合好不保证这一点 |
| 与人工编码比较 | 测量效度 | 随机抽一批文档，请不知道模型结果的编码员按研究概念编码，算人工编码和主题比例的相关，或以人工编码为准算精确率、召回率。主题要当测量用，这是最直接的证据 |
| 外部事件检验 | 预测效度 | 主题比例应在已知事件后变化，比如核试验之后“核”主题的比重上升。对不上已知事件的主题，很难让人相信它测到了想测的东西 |
| 多个随机种子 | 不是局部最优的偶然 | 换种子或初始化方法重估几次，比较主题能否一一对应、主结论是否保持。STM 默认的谱初始化结果可重复，但重复的只是众多局部最优中的一个 |
| 换预处理 | 预处理不改变结论 | 是否词干化、去停用词、去低频词、加二元词组，各做一遍；Denny 和 Spirling 的 preText 可先找出最可能改变结果的步骤。中文语料还要换分词工具或自定义词典再做一遍 |
| 拆样本：一半发现、一半检验 | 结论没有被看过的数据塑造 | 在一半文档上选 K、命名、定关键词，固定下来，再在另一半上估计和检验。这样研究者的反复调整就碰不到检验用的数据 |
| 传递测量不确定性，剔除短文档 | 作变量时的测量误差 | 用 `estimateEffect` 的抽样，或对多次抽取的主题比例分别回归再合并；再剔除很短的文档重做。结果变化大，说明结论倚重估得最不准的那部分比例 |

## 怎么读结果

- **主题的词表**：STM 的 `labelTopics` 给出几种词表。最高概率词常混着各主题都多的通用词；FREX 兼顾频率和排他性，通常最能区分主题，命名时主要看它，再配合代表性文档。
- **主题比例**：文档层面是每篇的 θ；语料层面看各主题的平均比例，以及按年份画出的变化。
- **协变量效应**：`estimateEffect` 给出的差，比如民主国家在人权主题上的平均比例比非民主国家高 0.03，读作 3 个百分点；还要对照基准水平，基准是 0.05 时，这是六成的提升。
- **作自变量的系数**：永远相对参照主题来读。
- **表格和附录里要找的信息**：K 是怎么选的，预处理做了哪些步骤，主题怎么命名，有没有人工验证，主结果在其他 K 下是否成立。

读的时候可以这样说：控制年份后，民主国家发言中人权主题的比重平均高出 3 个百分点，相当于该主题平均比重的一半以上；这一差异在 K 取 20 到 40 时都存在，抽读的代表性文档确实在讨论人权问题。

## 常见陷阱

- **只看高频词就命名**：最常见也最危险，名字一旦定下，读者会默认它测的就是这个概念。
- **把某个 K 的结果当作语料的真实结构**：它只是一种分辨率下的切分。
- **只用留出似然或困惑度挑 K**：统计上最优的 K 往往过大，主题难以解释。
- **把关注当立场**：主题比重高，只说明谈得多，不说明支持。
- **在同一份数据上反复调参直到出结果**：K、关键词、标签、预处理都可以调，调到满意再检验，显著性就失去了意义。
- **短文本直接套用**：推文、标题每篇只有几个词，θ 估得很差，可以按说话者或日期合并文档，或改用为短文本设计的模型。
- **全部主题比例放进回归**：加起来等于 1，和截距完全共线。
- **中文分词不当**：分词工具和自定义词典会改变词表，“一带一路”被切开，这个概念就散到了几个词里。
- **多语种语料混在一起**：主题会先按语言分开，而不是按议题。
- **keyATM 关键词和词表对不上**：语料做了词干化或小写化，关键词也要用同样处理后的形式，否则会被静默忽略或报错。

## 代码写法

R，预处理用 quanteda，模型用 stm 和 keyATM：

```r
library(quanteda)
library(stm)

# 预处理：每一步都是会影响结果的选择，稳健性检验要逐一改动
corp  <- corpus(speeches, text_field = "text")
toks  <- tokens(corp, remove_punct = TRUE, remove_numbers = TRUE)
toks  <- tokens_remove(tokens_tolower(toks), stopwords("en"))
toks  <- tokens_wordstem(toks)
dfmat <- dfm_trim(dfm(toks), min_docfreq = 5)

# 转成 stm 的格式；元数据 meta 来自语料的文档变量
out <- convert(dfmat, to = "stm")

# 比较一组候选 K：留出似然、语义一致性、排他性、残差
ks <- searchK(out$documents, out$vocab, K = c(10, 20, 30, 40),
              prevalence = ~ regime + s(year), data = out$meta)
plot(ks)

# 估计 STM：政体和年份进入主题流行度
fit <- stm(out$documents, out$vocab, K = 20, prevalence = ~ regime + s(year),
           data = out$meta, init.type = "Spectral")
labelTopics(fit, n = 10)                         # 最高概率词、FREX 等
findThoughts(fit, texts = speeches$text, topics = 3, n = 10)   # 读代表性文档；texts 要和进入模型的文档一一对应
semanticCoherence(fit, out$documents)
exclusivity(fit)

# 协变量效应：在主题比例的近似后验上抽样，传递测量不确定性
eff <- estimateEffect(1:20 ~ regime + s(year), fit, metadata = out$meta, uncertainty = "Global")
summary(eff, topics = 3)
plot(eff, covariate = "regime", topics = 3, method = "difference",
     cov.value1 = "democracy", cov.value2 = "autocracy")

# 主题比例矩阵，每行一篇文档、每列一个主题，可以并回数据做下游回归
theta <- fit$theta

# keyATM：关键词要用和词表同样处理后的形式，这里是词干
library(keyATM)
docs <- keyATM_read(texts = dfmat)
keys <- list(nuclear = c("nuclear", "warhead", "enrich"),
             trade   = c("trade", "tariff", "export"))
visualize_keywords(docs = docs, keywords = keys)   # 先看关键词在语料里够不够常见
fit_k <- keyATM(docs = docs, no_keyword_topics = 10, keywords = keys,
                model = "base", options = list(seed = 250))
top_words(fit_k)
```

Stata 没有官方的主题模型命令，社区命令 `ldagibbs` 可以估 LDA，但多数作者在 R 或 Python 里估好主题比例，再读进 Stata 做回归：

```stata
* 读入每篇文档的主题比例 topic1-topic20，按文档编号并上元数据
import delimited "theta.csv", clear
merge 1:1 doc_id using "meta.dta", keep(match) nogenerate

* 主题比例作结果
regress topic3 i.democracy i.year, vce(cluster country)

* 主题比例作自变量：比例加起来等于 1，留 topic20 作参照不放进回归
regress y topic1-topic19 i.year, vce(cluster country)
```

Python，常用 gensim：

```python
from gensim.corpora import Dictionary
from gensim.models import CoherenceModel, LdaModel

# tokenized_docs 是分好词、处理好的文档列表，每篇是一个词的列表
dictionary = Dictionary(tokenized_docs)
dictionary.filter_extremes(no_below=5, no_above=0.5)   # 去掉太罕见和太普遍的词
bow = [dictionary.doc2bow(doc) for doc in tokenized_docs]

lda = LdaModel(bow, num_topics=20, id2word=dictionary, passes=10, random_state=1)
print(lda.show_topics(num_topics=20, num_words=10))

# u_mass 是上面那种基于文档共现的语义一致性，gensim 的实现在平滑和汇总方式上略有不同
cm = CoherenceModel(model=lda, corpus=bow, dictionary=dictionary, coherence="u_mass")
print(cm.get_coherence())

# 每篇文档的主题比例
theta = [lda.get_document_topics(b, minimum_probability=0) for b in bow]
```

在作者代码里认主题模型，看这几处：R 的 `stm(..., K = , prevalence = )`、`searchK`、`estimateEffect`、`labelTopics`、`findThoughts`，`keyATM(..., keywords = , no_keyword_topics = )` 和 `keyATM_read`，`topicmodels::LDA`、`seededlda` 的 `textmodel_lda` 与 `textmodel_seededlda`；Python 的 `LdaModel`、`LatentDirichletAllocation`；Stata 的 `ldagibbs`。之后通常有一个文档乘主题的矩阵，比如 `fit$theta`，被并回数据框，某几列出现在回归式里。还要看前面的预处理：`tokens_wordstem`、`stopwords`、`dfm_trim` 的门槛，以及中文语料用了什么分词工具。

## 延伸阅读

- Blei, David M., Andrew Y. Ng, and Michael I. Jordan. 2003. "Latent Dirichlet Allocation." *Journal of Machine Learning Research* 3.
- Grimmer, Justin, and Brandon M. Stewart. 2013. "Text as Data: The Promise and Pitfalls of Automatic Content Analysis Methods for Political Texts." *Political Analysis* 21 (3).
- Grimmer, Justin, Margaret E. Roberts, and Brandon M. Stewart. 2022. *Text as Data: A New Framework for Machine Learning and the Social Sciences*. Princeton University Press.
- Roberts, Margaret E., Brandon M. Stewart, Dustin Tingley, Christopher Lucas, Jetson Leder-Luis, Shana Kushner Gadarian, Bethany Albertson, and David G. Rand. 2014. "Structural Topic Models for Open-Ended Survey Responses." *American Journal of Political Science* 58 (4).
- Roberts, Margaret E., Brandon M. Stewart, and Dustin Tingley. 2019. "stm: An R Package for Structural Topic Models." *Journal of Statistical Software* 91 (2).
- Eshima, Shusei, Kosuke Imai, and Tomoya Sasaki. 2024. "Keyword-Assisted Topic Models." *American Journal of Political Science* 68 (2).
- Chang, Jonathan, Jordan Boyd-Graber, Sean Gerrish, Chong Wang, and David M. Blei. 2009. "Reading Tea Leaves: How Humans Interpret Topic Models." *Advances in Neural Information Processing Systems* 22.
- Denny, Matthew J., and Arthur Spirling. 2018. "Text Preprocessing for Unsupervised Learning: Why It Matters, When It Misleads, and What to Do about It." *Political Analysis* 26 (2).
