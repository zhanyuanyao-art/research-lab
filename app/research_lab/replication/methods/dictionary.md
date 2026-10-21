---
key: dictionary
name: 词典法与文本计量
english: Dictionary Methods and Text-Based Measurement
summary: 事先为每个类别列一张词表，数文本里命中多少词，换算成分数再放进回归；词表在别的领域好用，不等于在你的文本里好用，所以必须拿人工编码去核对。
---

## 适用的问题

要测的概念事先清楚，而且有一批辨认得出的用词。比如：

- 外交声明的语气是强硬还是缓和；
- 领导人讲话里威胁性语言出现得多频繁；
- 各国联大发言提到主权、不干涉内政的程度；
- 新闻报道对某国的正面与负面措辞。

词典法的长处是透明、便宜、可复制：不需要训练样本，语料再小也能用，每一个分数都能追溯到具体是哪些词。短处是它只认得词表里的词。

它和另外两类文本方法的分工是：概念清楚、用词也清楚，用词典法；概念清楚、但用词说不全，而有人工编码好的样本，用有监督分类；连类别都不清楚，用主题模型。

## 核心思想

词典法分三步：定词表，数命中，按长度标准化。测量的质量完全取决于词表，误差有三个来源：

- **漏判**：概念出现了，但用的词不在表里。中文官方表态里的“勿谓言之不预”，是分量很重的警告，却不含任何“威胁”字样；all options are on the table 也是。
- **误中**：表里的词出现了，说的却不是这个概念。通配符 `arm*` 本想抓 army、armed，同时会命中 armistice 和 Armenia；“我们不会使用武力”被否定了，却照样计入；一份谴责他国威胁的声明，因为引用了对方的威胁而得高分。
- **领域错配**：词表在别的语料上编成。Loughran 和 McDonald 发现，在美国上市公司年报里，通用的哈佛词典标为负面的词，约四分之三在财务语境下并不负面，比如 tax、cost、liability。外交文本也一样，“危机”“冲突”“安全”常以中性甚至积极的意思出现，比如“加强安全合作”。

所以词典法的核心工作不是数词，而是验证：随机抽一批文本请人编码，看词典和人工判断差多少、差在哪里。

一个数字例子。从全部声明里随机抽 200 句，人工判断是否含威胁，再和词典对照：

| | 人工判为威胁 | 人工判为非威胁 | 合计 |
|---|---|---|---|
| 词典命中 | 30 | 20 | 50 |
| 词典未命中 | 10 | 140 | 150 |
| 合计 | 40 | 160 | 200 |

```text
准确率 = (30 + 140) / 200 = 0.85
精确率 = 30 / 50 = 0.60
召回率 = 30 / 40 = 0.75
```

准确率 85% 看着不错，但那主要是因为大部分句子本来就没有威胁。精确率说明词典判为威胁的句子里有四成是错的，召回率说明真有威胁的句子漏掉了四分之一。更要紧的是漏在哪里：如果漏掉的十句集中在某一类国家，词典就会系统性地低估这类国家的强硬程度，而这正是研究要比较的东西。

## 模型设定

**分数**：

```text
S_d^c = Σ_{w ∈ L_c} n_dw / N_d
S_d^c = Σ_{w ∈ L_c} s_w · n_dw / N_d
```

- `L_c` 是类别 c 的词表，`n_dw` 是词条 w 在文档 d 里出现的次数，`N_d` 是文档 d 经过同样预处理后的总词数；
- 第二行是加权版本，`s_w` 是每个词的强度权重，比如“威胁”权重低于“最后通牒”。

**两极语气**：

```text
Tone_d    = (Pos_d − Neg_d) / (Pos_d + Neg_d)
LogTone_d = log[ (Pos_d + 0.5) / (Neg_d + 0.5) ]
```

- `Pos_d`、`Neg_d` 是积极词和消极词的命中数；
- 第一行取值在 −1 到 1 之间，但只看比例不看数量：两份都没有消极词的声明，一份命中一个积极词，一份命中十个，都得 1；一个都没命中时无定义；
- 第二行是 Lowe 等人的对数比，加 0.5 避免对零取对数，上面两份声明分别得 1.10 和 3.04，能区分证据的多少。

**验证指标**：

```text
精确率 P = TP / (TP + FP)
召回率 R = TP / (TP + FN)
F1 = 2 · P · R / (P + R)
κ = (p_o − p_e) / (1 − p_e)
α = 1 − D_o / D_e ,   二分类、两名编码员时  D_e = 2 · n_1 · n_0 / [ n · (n − 1) ]
```

- `TP` 是词典和人工都判为是，`FP` 是词典判是而人工判否，`FN` 是词典判否而人工判是；
- `κ` 是 Cohen 的 kappa：`p_o` 是实际一致的比例，`p_e` 是按两方各自的判是比例、纯靠运气会达到的一致比例；
- `α` 是 Krippendorff 的 alpha：`D_o` 是实际不一致的比例，`D_e` 是把所有编码混在一起随机配对时的期望不一致；`n` 是编码总数，`n_1`、`n_0` 是其中判是和判否的个数。α 可以处理多名编码员、缺失编码和有序或连续的编码；
- Krippendorff 建议 α 至少达到 0.8 才用来下结论，0.667 到 0.8 之间只能下试探性的结论。

把上面的例子当作两方编码：

```text
κ:  p_o = 0.85 ,  p_e = 0.25 × 0.20 + 0.75 × 0.80 = 0.65 ,  κ = 0.20 / 0.35 ≈ 0.57
α:  n = 400 ,  n_1 = 50 + 40 = 90 ,  n_0 = 310 ,  D_o = 30 / 200 = 0.15
    D_e = 2 × 90 × 310 / (400 × 399) ≈ 0.350 ,  α = 1 − 0.15 / 0.350 ≈ 0.57
```

两个指标都只有 0.57，远低于 0.8，这部词典需要修改。α 通常先用来检验人工编码员之间的一致，确认人工标准本身可靠，再拿人工标准去评价词典。

**分数进入回归**：

```text
作自变量：  Y_it = β_0 + β · S_it + X_it′γ + ε_it
作结果：    log E[C_d] = log N_d + X_d′γ
```

- `X_it` 是控制变量，`C_d` 是文档 d 的命中次数；
- 作自变量时，`β` 读作分数每变化一个单位，结果平均变化多少；分数常先标准化；
- 作结果时，与其对比例做最小二乘，不如对命中次数 `C_d` 用泊松或负二项回归，把 `log N_d` 作为偏移量。这等于建模每个词命中的概率，自然处理了大量的零和长度带来的方差差异；`exp(γ)` 是命中率的倍数。

**词嵌入作为延伸**：

```text
cos(u, v) = u · v / ( ‖u‖ · ‖v‖ )
```

- 词嵌入用每个词周围的词训练出一个几百维的向量，用法相近的词向量方向相近，`cos` 接近 1；
- 用途一是扩充词表：找出种子词的近邻作候选，人工审读后再加入；用途二是给文档打分：文档向量与种子词平均向量的相似度；
- 要小心：反义词常出现在相同的语境里，war 和 peace 的向量可能很近；在新闻或网页上预训练的向量带着那些语料的用法，在本语料上训练又需要足够大的语料。近邻只能当候选，不能直接入表。

## 识别假设

词典法是测量工具，不是因果识别设计。这里列的是分数可以当作概念测量的条件，以及每条不成立的代价。

1. **词与概念在这份语料里对应**：代价是系统性的漏判和误中，分数测到的是别的东西。
2. **命中越多，概念越强**：仪式性套话、重复、引用他人原话都会破坏这一条。代价：例行公事的长篇声明得分虚高，引用对方威胁的谴责被算成威胁。
3. **测量误差与研究变量无关**：词典对各组漏判和误中的程度相同。代价最隐蔽：如果非民主国家惯用含蓄的固定表达，词典对它们漏判更多，民主与非民主国家的比较就有偏差，方向无法事先判断。
4. **跨时期、跨语言可比**：词义会漂移，冷战时期和反恐时期的 security 指向不同；翻译文本的用词取决于译者。代价：分数的趋势有一部分只是用词习惯的变化。
5. **预处理不制造或抹掉命中**：合作词典里的 `unit*` 本想抓 unity 和 united，在国际关系文本里会命中每一次 United Nations 和 United States；中文分词之后，“不使用武力”里的“使用武力”照样命中，否定就丢了。代价：大批误中，而且往往集中在特定文本里。
6. **词表独立于结果确定**：看着哪些词和结果相关再挑进词表，等于在测量里预先写进结论。代价：显著性虚高，换一份样本就不成立。

## 稳健性检验：做什么，为什么

| 检验 | 检查哪条假设 | 怎么做，怎么看 |
|---|---|---|
| 人工编码验证 | 词与概念对应 | 从全语料随机抽样，编码员不看词典结果，报告精确率、召回率和 F1。必须从全语料抽，因为只从命中文本里抽只能算精确率，漏判完全看不到 |
| 编码员一致性 | 人工标准本身可靠 | 两名以上编码员独立编码同一批文本，算 Krippendorff α，一般要求 0.8 以上。人工编码是验证的标尺，标尺不可靠，算出的精确率和召回率也没有意义 |
| 分组验证 | 误差与研究变量无关 | 按研究要比较的组，比如政体、时期、语言，分别算精确率和召回率。各组差别大，说明误差是差异性的，组间差异可能完全由词典造成 |
| 关键词上下文 | 词与概念对应，命中越多概念越强 | 列出高频命中词的原句，看有没有否定、引用和一词多义。成本最低，常能一眼发现 armistice、United Nations 这类误中 |
| 逐词剔除 | 结果不由个别词驱动 | 每次去掉一个词重算分数、重做回归。少数高频词常贡献大部分命中；去掉一个词结论就变，测到的其实是这个词的用法变化 |
| 换词典 | 结果不依赖某一张词表 | 用另一部成熟词典，或另一位研究者独立编的词表重做。不同词表的漏判和误中不同，都成立才说明结论来自概念 |
| 用人工编码重做回归 | 测量误差没有制造或掩盖效果 | 人工编码样本够大时，直接用人工编码作变量重做主回归，比较方向和大小。这是对词典误差影响最直接的检查 |
| 改变预处理 | 预处理不制造或抹掉命中 | 词干化与否、处理否定与否、多词短语是否作为整体词条、中文换分词工具，各做一遍。这些选择直接改变命中数 |
| 换标准化方式 | 命中越多概念越强 | 比例、对数比、带长度偏移的计数模型各做一遍，并剔除极短的文档。长短悬殊的文档，在不同标准化下排序可能颠倒 |
| 安慰剂词表 | 结果不是文本整体变化造成的 | 随机抽一组词频相近、和概念无关的词当词表重做。安慰剂词表也有效果，说明结果反映的是篇幅、文体或格式的变化 |
| 分时期看命中词 | 跨时期可比 | 按时期列出贡献最大的命中词。不同时期靠不同的词撑起分数，就要追问测的是否还是同一个概念 |

## 怎么读结果

- **分数的单位**：比例形式下，0.008 是每千词 8 个命中词。这类分数通常右偏、零很多，作者常做标准化，β 就是一个标准差的效果。
- **分数由什么撑起**：好的论文会列出贡献最大的命中词和几条原句；没有列，就无法判断测到的是什么。
- **验证结果**：找精确率、召回率、α，并看它们是在全样本上算的，还是分组算的；只有准确率时要追问类别比例。
- **作结果的计数模型**：`exp(γ)` 是命中率的倍数，比如 1.3 读作威胁性用语的频率高 30%。

读的时候可以这样说：作者用自编的威胁词典给每份声明打分，在 300 句随机人工编码样本上精确率 0.8、召回率 0.7，两类政体的召回率相近；控制年份后，危机期间威胁用语的频率高出 30%，换用另一部词典和去掉最高频的命中词后结论不变。

## 常见陷阱

- **借用通用词典**：情感词典多在影评、社交媒体或新闻上编成，搬到外交文本前必须重新验证。
- **否定、引用和反讽**：词袋看不出“我们不会”“对方威胁说”。
- **通配符过度匹配**：`arm*`、`unit*` 之外，`war*` 会命中 warm、warrant 和 Warsaw，华沙条约也被算成了战争。
- **验证样本只从命中文本里抽**：只能看到精确率，看不到召回率。
- **只报准确率**：概念稀少时，全判为否也有很高的准确率。
- **长度没有标准化**：长讲话机械地得到更多命中。
- **看着结果编词表**：词表应在看结果之前固定，或者在一半样本上编、另一半上检验。
- **翻译**：把英文词典直译成中文，或用机器翻译后的文本，词义和用法都会走样。
- **把分数当成没有误差的变量**：误差大致随机时，作自变量的系数被往 0 拉；误差和组别相关时，偏差方向无法判断。

## 代码写法

R，常用 quanteda：

```r
library(quanteda)

# 词典：* 是通配符；带空格的条目是多词短语，要在 tokens 上匹配
dict <- dictionary(list(
  threat      = c("threat*", "ultimatum*", "retaliat*", "red line", "grave consequences"),
  cooperation = c("cooperat*", "partnership*", "dialogue*")
))

toks   <- tokens(corpus(statements, text_field = "text"), remove_punct = TRUE)
hits   <- dfm(tokens_lookup(toks, dictionary = dict))   # 默认不分大小写
scores <- convert(hits, to = "data.frame")
statements$threat_n     <- scores$threat
statements$n_tokens     <- ntoken(toks)
statements$threat_share <- statements$threat_n / statements$n_tokens

# 关键词上下文：逐条读命中的原句，找否定、引用和一词多义
kwic(toks, pattern = "threat*", window = 6)
kwic(toks, pattern = phrase("red line"), window = 6)

# 和人工编码比：hand 是从全语料随机抽的句子，dict_hit 和 human 都是 0/1
tab <- table(dict = hand$dict_hit, human = hand$human)
precision <- tab["1", "1"] / sum(tab["1", ])
recall    <- tab["1", "1"] / sum(tab[, "1"])

# 编码员一致性：kripp.alpha 要求编码员在行、文本在列
library(irr)
kripp.alpha(t(as.matrix(hand[, c("coder1", "coder2")])), method = "nominal")

# 作结果：命中次数用泊松回归，文档长度取对数作偏移
library(fixest)
fepois(threat_n ~ crisis + democracy | year, offset = ~log(n_tokens),
       data = statements, cluster = ~country)
```

Stata，用 Unicode 正则数命中次数：

```stata
* 统一小写，先去掉文本里原有的 @，再把每个命中替换成 @，数 @ 的个数
gen text_l   = subinstr(ustrlower(text), "@", "", .)
gen tmp      = ustrregexra(text_l, "\bthreat\w*|\bultimatum\w*|\bretaliat\w*|\bred line\b", "@")
gen threat_n = ustrlen(tmp) - ustrlen(subinstr(tmp, "@", "", .))
gen n_tokens = wordcount(text_l)
gen threat_share = threat_n / n_tokens
drop tmp

* 验证：行百分比里词典命中一行的一致比例是精确率，列百分比里人工判是一列的是召回率
tab dict_hit human, row col
kap coder1 coder2

* 作结果：泊松回归，文档长度作暴露量
poisson threat_n i.crisis i.democracy i.year, exposure(n_tokens) vce(cluster country)
```

Python：

```python
import re
from sklearn.metrics import cohen_kappa_score, f1_score, precision_score, recall_score

THREAT = re.compile(r"\bthreat\w*|\bultimatum\w*|\bretaliat\w*|\bred line\b", re.IGNORECASE)
WORD = re.compile(r"\w+")

statements["threat_n"] = statements["text"].apply(lambda t: len(THREAT.findall(t)))
statements["n_tokens"] = statements["text"].apply(lambda t: len(WORD.findall(t)))
statements["threat_share"] = statements["threat_n"] / statements["n_tokens"]

# 以人工编码为准评价词典；编码员之间用 kappa
print(precision_score(hand["human"], hand["dict_hit"]),
      recall_score(hand["human"], hand["dict_hit"]),
      f1_score(hand["human"], hand["dict_hit"]),
      cohen_kappa_score(hand["coder1"], hand["coder2"]))

# 词嵌入扩充词表：在本语料上训练，列出种子词的近邻，人工审读后再决定是否入表
from gensim.models import Word2Vec
w2v = Word2Vec(sentences=tokenized_docs, vector_size=300, window=5, min_count=10, seed=1)
print(w2v.wv.most_similar(positive=["threat", "ultimatum"], topn=20))
```

在作者代码里认词典法，看这几处：quanteda 的 `dictionary(`、`tokens_lookup`、`dfm_lookup`、自带的 `data_dictionary_LSD2015`，`liwcalike`，tidytext 的 `get_sentiments` 加 `inner_join`，`str_count` 或 `grepl` 里一长串用 `|` 连起来的词；Stata 的 `ustrregexm`、`ustrregexra`、`regexm`，或在一串词上循环的 `strpos`；Python 的 `re.findall` 和 VADER 的 `SentimentIntensityAnalyzer`。随后通常是命中数除以总词数，或者计数模型里出现文档长度的 `offset` 和 `exposure`。

## 延伸阅读

- Grimmer, Justin, and Brandon M. Stewart. 2013. "Text as Data: The Promise and Pitfalls of Automatic Content Analysis Methods for Political Texts." *Political Analysis* 21 (3).
- Grimmer, Justin, Margaret E. Roberts, and Brandon M. Stewart. 2022. *Text as Data: A New Framework for Machine Learning and the Social Sciences*. Princeton University Press.
- Krippendorff, Klaus. 2013. *Content Analysis: An Introduction to Its Methodology*. 3rd ed. SAGE.
- Loughran, Tim, and Bill McDonald. 2011. "When Is a Liability Not a Liability? Textual Analysis, Dictionaries, and 10-Ks." *Journal of Finance* 66 (1).
- Young, Lori, and Stuart Soroka. 2012. "Affective News: The Automated Coding of Sentiment in Political Texts." *Political Communication* 29 (2).
- Lowe, Will, Kenneth Benoit, Slava Mikhaylov, and Michael Laver. 2011. "Scaling Policy Preferences from Coded Political Texts." *Legislative Studies Quarterly* 36 (1).
- Denny, Matthew J., and Arthur Spirling. 2018. "Text Preprocessing for Unsupervised Learning: Why It Matters, When It Misleads, and What to Do about It." *Political Analysis* 26 (2).
- Rodriguez, Pedro L., and Arthur Spirling. 2022. "Word Embeddings: What Works, What Doesn't, and How to Tell the Difference for Applied Research." *Journal of Politics* 84.
