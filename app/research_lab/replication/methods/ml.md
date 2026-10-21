---
key: ml
name: 机器学习测量
english: Supervised Machine Learning for Measurement
summary: 在有真值的样本上训练预测模型，给主分析里缺这个变量的对象打分，再把分数当作自变量放进回归；可信与否取决于模型能不能搬到新的人群，以及预测误差有没有被带进第二步。
---

## 适用的问题

研究要用的一个变量，在主分析的样本里观察不到，却能在另一批对象身上观察到，而且两批对象共有一组都查得到的特征。比如：

- 想知道冷战时期各国决策者的鹰派程度，但这些人从未做过问卷；今天的精英问卷里，受访者既回答了测量鹰派程度的题目，也报告了从军经历、教育、出生年代等履历，而这些履历在历史人物身上也查得到；
- 十万份外交电报里，只有两千份被人工判断过是否含有威胁，其余的需要自动编码；
- 一些地区没有可靠的经济统计，但有夜间灯光等遥感特征。

做法分两步。第一步是测量：在有真值的训练样本上学一个从特征到目标变量的映射，再给目标样本里的每个对象打分。第二步是分析：把分数当作自变量放进回归，回答真正的研究问题。

两步的目标不同。第一步只关心预测准不准，哪个特征权重大并不重要，也不能读成因果；第二步关心一个系数，它的偏差和标准误都受第一步误差的影响。Mullainathan 和 Spiess 把这种区别概括为：机器学习擅长回答“预测值是多少”，回归要回答“系数是多少”，拿前者的工具做后者的事，要格外小心。

## 核心思想

**一、只用没见过的数据评价模型。** 足够灵活的模型能把训练样本拟合得几乎没有误差，连纯属偶然的波动也一起学进去，这叫过拟合。所以模型好不好，只能看它在训练时没见过的数据上表现如何。标准做法是先留出一部分有真值的样本作测试集，在其余部分里调参、选模型，最后在测试集上只评估一次。

**二、用交叉验证控制复杂度。** 模型太简单会漏掉真实规律，太复杂会把噪声当规律。正则化回归和提升法都有控制复杂度的旋钮，拧到哪里由 k 折交叉验证决定：把训练集分成 k 份，轮流用 k − 1 份拟合、剩下 1 份评估，取 k 次误差的平均，选误差最小的设置。

- 正则化回归，比如 lasso 和岭回归，在最小二乘之外对系数大小加一项惩罚，惩罚越重，系数越被压向 0。lasso 会把一部分系数压成恰好为 0，等于自动挑变量；岭回归只压小，不删除。
- 提升法从一个很粗的预测出发，每一轮拟合一棵小树去修正上一轮的残差，再只把这棵树的一小部分加进来。它擅长捕捉交互和非线性，比如“从军经历只在冷战一代身上和鹰派程度相关”。

**三、预测值不是真值，误差会传到第二步。** 最简单的情形是只有一个自变量，预测误差和结果的扰动项无关，这时有一个干净的结论：

```text
plim β̂ = β · Cov(Z, Ẑ) / Var(Ẑ)
```

- `Z` 是真值，`Ẑ` 是预测值，`β` 是真实效果，`β̂` 是用预测值估出的系数；
- 右边的比值，正是在有真值的数据上把 Z 对 Ẑ 回归得到的斜率，叫校准斜率，在测试集上就能算出来。

设真实效果 β = 0.5：

| 预测值的形态 | 校准斜率 | 第二步估出的 β |
|---|---|---|
| 真值加独立噪声，噪声方差是真值方差的一半 | 1 / 1.5 ≈ 0.67 | 0.33，被衰减 |
| 给定特征时真值的条件均值，校准良好 | 1 | 0.5，无偏，但标准误更大 |
| 只有条件均值一半的波动幅度，压缩过度 | 2 | 1.0，被放大 |

第一行是经典测量误差：噪声让预测值比真值更分散，系数被拉向 0，这就是衰减偏差，过拟合的模型在新数据上接近这一行。第三行是惩罚过重的模型，预测值挤在均值附近，系数反被放大。第二行说明，好的预测不必和真值一样分散，只要给定特征时平均说对，第二步就不偏，损失的只是精度。

这张表还需要一个前提：校准斜率在目标人群里和在训练人群里一样。这是机器学习测量最难的地方，下面的假设大多围绕它展开。

## 模型设定

**第一步：训练与打分**

```text
Z_j = f(X_j) + e_j ,                              j ∈ S
f̂ = argmin_f  Σ_{j∈S} L(Z_j, f(X_j)) + λ · Ω(f)
Ẑ_i = f̂(X_i) ,                                    i ∈ T
```

- `S` 是有真值的训练样本，比如问卷受访者；`T` 是目标样本，比如历史决策者；
- `X` 是两批对象都有的特征；`e_j` 是特征解释不了的部分；`f` 是从特征到目标的映射，可以是线性的，也可以是一组树；
- `L` 是损失函数，连续目标常用平方误差，二分类常用对数损失；
- `Ω(f)` 衡量模型复杂度，`λ` 是惩罚强度，由交叉验证确定。

**正则化回归**，按 glmnet 的弹性网写法：

```text
β̂ = argmin_β  Σ_j (Z_j − β_0 − X_j′β)² / (2n) + λ · [ (1 − α)/2 · Σ_k β_k² + α · Σ_k |β_k| ]
```

- `n` 是训练样本量，`β_0` 是截距，不受惩罚，`β_k` 是第 k 个特征的系数；
- `α = 1` 是 lasso，`α = 0` 是岭回归，中间是弹性网；
- 特征要先标准化，否则惩罚对量纲大的变量不公平，glmnet 默认替你做。

**提升法**：

```text
F_0(x) = Z 的均值
F_m(x) = F_{m−1}(x) + ν · h_m(x) ,   m = 1, …, M
```

- `F_m` 是第 m 轮之后的预测函数，`F_M` 就是最终模型；
- `h_m` 是第 m 轮拟合的小树，拟合对象是上一轮的残差，一般地说是损失函数的负梯度；
- `ν` 是学习率，常取 0.01 到 0.1，越小越稳，但需要更多轮；
- `M` 是轮数，太多会过拟合，也要用交叉验证定；树的深度决定能捕捉几阶交互，深度为 1 的树桩只能做可加模型。

**k 折交叉验证**：

```text
CV(λ) = (1/K) · Σ_{k=1}^{K} (1/n_k) · Σ_{j ∈ 第 k 折} L( Z_j, f̂_λ^{(−k)}(X_j) )
```

- `K` 常取 5 或 10；`f̂_λ^{(−k)}` 是去掉第 k 折、用参数 λ 拟合的模型；`n_k` 是第 k 折的样本量；
- 选 `CV(λ)` 最小的 λ；glmnet 的 `lambda.1se` 选误差在最小值一个标准误以内、最简单的那个，用一点精度换更稳的模型；
- 选出的那个最小误差是从许多候选里挑出来的，偏乐观。要报告诚实的样本外表现，要么另留测试集，要么用嵌套交叉验证：外层分折评估，内层分折调参。

**第二步与不确定性的传递**：

```text
Y_i = α + β · Ẑ_i + W_i′θ + ε_i ,   i ∈ T

自助法：   对 b = 1, …, B：重抽 S 和 T → 重新调参训练 → 重新打分 → 估计 β̂_b
          取 β̂_b 的 2.5% 与 97.5% 分位数作置信区间
多次抽取： 对 m = 1, …, M：从每个对象的预测分布抽一个 Z̃_i → 估计 β̂_m 和方差 U_m
          β̄ = (1/M) · Σ β̂_m ,   Var(β̄) = Ū + (1 + 1/M) · V_B
```

- `W_i` 是控制变量；
- 自助法抓住的是训练样本有限带来的模型不确定性。这是经典的生成回归量问题：第二步的普通标准误把 f̂ 当成已知，其实它是估出来的；
- 多次抽取借用多重插补的 Rubin 规则：`Ū` 是各次抽取内部方差的平均，`V_B` 是 β̂_m 之间的方差，反映分数本身的不确定性；
- 多次抽取有一个容易忽略的后果：如果抽取只依据特征、不看结果变量，加进去的随机波动就是经典测量误差，每一次的 β̂_m 都被往 0 拉，得到的是偏保守的估计。Blackwell、Honaker 和 King 的多重过度插补把结果变量也放进插补模型，避开了这一点。

## 识别假设

机器学习测量不是因果识别设计，第二步回归另有它自己的识别假设。这里列的是“用预测分数代替真值”这一步需要的条件，以及每条不成立的代价。

1. **标签有效**：训练样本里的 Z 确实测到了研究想要的概念。问卷题目如果测的是对当下某场战争的态度，而不是一般的用武倾向，模型学到的就是另一个东西。代价：第二步估计的是另一个概念的效果，预测再准也救不回来。
2. **可迁移**：给定特征时目标变量的分布，在训练人群和目标人群里相同。特征的分布可以不同，历史决策者里军人更多并无妨；但特征和目标的关系不能变。如果今天的受访者里从军经历意味着更谨慎，在 1950 年代的精英里却意味着更强硬，模型就会系统地打错分。代价：校准斜率在目标人群里偏离 1，系数被衰减或放大；误差若和时代、国别相关，偏差方向无法判断。
3. **有支撑、不外推**：目标对象的特征落在训练样本覆盖的范围里。出生于 1900 年的将领，在今天的受访者里找不到同龄人。代价：模型只能外推，而不同学习器外推的方式截然不同，树模型在边界外给出常数，线性模型沿直线延伸，预测值主要反映模型的选择而不是数据。
4. **特征只通过被测特质影响结果**：第一步是线性回归时，把预测值放进第二步，形式上就是以这些特征为工具变量的两样本两阶段最小二乘；换成更灵活的学习器，道理不变。所以需要和工具变量一样的排他性。从军经历如果除了塑造鹰派倾向，还通过军事专业知识直接影响危机决策，β 就混进了这条直接渠道。代价：β 不再是特质本身的效果。
5. **预测误差和第二步的扰动项无关**：如果模型在某类领导人身上系统地高估鹰派程度，比如民主国家的领导人，而这类领导人的危机行为又因为模型之外的原因本来就不同，就会制造虚假关联。在第二步控制这个类别，只能吸收误差中随类别整体平移的那一部分。代价：偏差不再只是衰减，方向事先无法判断。
6. **训练与评估没有泄漏**：测试集在训练、调参、预处理中都没被用过。代价：报告的准确率虚高，读者会高估测量质量。

## 稳健性检验：做什么，为什么

| 检验 | 检查哪条假设 | 怎么做，怎么看 |
|---|---|---|
| 留出测试集、嵌套交叉验证 | 没有泄漏，没有过拟合 | 调参全部放在训练集内部，测试集只在最后用一次，报告均方根误差、相关系数或分类的 AUC。要做这一步，是因为交叉验证挑出的最小误差本身偏乐观；测试集表现明显差于交叉验证，往往说明调参时偷看了数据，或者测试集太小、结果本身不稳 |
| 校准图和校准斜率 | 第二步偏差的方向和大小 | 在测试集上把真值对预测值回归，或把预测值分十组、画每组真值的均值。斜率小于 1，系数会被衰减；大于 1，会被放大。要做这一步，是因为准确率高不等于校准好，而第二步的偏差取决于后者 |
| 对照目标人群的专家编码 | 可迁移 | 在目标人群里找一小批有可靠资料的对象，比如有回忆录或档案的决策者，请不知道模型分数的专家独立打分，比较排序一致性和校准斜率。这是直接检验可迁移的唯一办法，训练集上表现再好，也只说明模型在今天的人群里好用 |
| 分组迁移验证 | 可迁移 | 按出生年代或国家把训练样本分组，在一组上训练、另一组上测试。准确率大幅下降，说明特征和目标的关系随人群而变，搬到更远的年代只会更糟 |
| 支撑检查 | 不外推 | 比较两批对象每个特征的分布；或训练一个分类器区分两批对象，分得越轻松，两批人越不像。只保留落在训练范围内的目标对象重做第二步，结果应当不变 |
| 换学习器 | 结果不依赖某个模型的外推方式 | 用 lasso、随机森林、提升法、普通线性回归分别打分，看分数之间的相关和第二步的 β。要做这一步，是因为学习器的选择是研究者的自由度；只有一种学习器给出显著结果时，结论更可能来自模型在边界外的行为 |
| 去掉可疑特征重训 | 特征只通过被测特质影响结果 | 把可能直接影响结果的特征从第一步拿掉，重新打分、重做第二步。β 大幅变化，说明它在搬运这些特征的直接作用 |
| 自助法或多次抽取 | 标准误没有忽略第一步 | 把训练、调参、打分、回归整条流程放进自助法循环，或从预测分布多次抽取再合并。和把分数当真值的普通标准误比，差距越大，原先的显著性越不可靠 |
| 用专家编码子样本校正 | 预测误差和第二步的扰动项无关 | 在目标人群里随机抽一部分对象请专家编码，用真值和预测值之差校正第二步，比如回归校准，或 Fong 和 Tyler、Egami 等人提出的方法。校正前后差别大，说明预测误差和研究变量纠缠在一起 |

需要强调：测试集上的准确率只回答模型在与训练样本同分布的新对象上准不准。机器学习测量真正的风险在于搬家，从受访者搬到历史人物，从今天搬到冷战。这一步的证据只能来自目标人群本身，几十个专家编码的案例，往往比训练集上多一个百分点的准确率更有说服力。

## 怎么读结果

- **第一步的表现**：连续目标看均方根误差，要和目标变量的标准差对照：标准差为 1、误差为 0.6，相当于解释了约六成的方差；误差接近标准差，模型几乎没学到东西。分类目标看 AUC、精确率和召回率，类别不平衡时，九成样本是 0，全猜 0 的模型准确率也有 90%。
- **特征重要性**：帮助判断排他性和可迁移是否可疑，但它不是因果效应，也不稳定，两个高度相关的特征，lasso 可能只留一个。
- **第二步的 β**：读作“预测的特质每增加一个单位，结果平均变化多少”，分数标准化后就是一个标准差的效果。它更接近真实效果的下界还是上界，要看校准斜率。
- **标准误**：看作者用的是普通标准误，还是自助法或多次抽取的标准误，前者通常偏小。
- **多种学习器并列**：重点不是哪个最显著，而是方向和大小是否一致。

读的时候可以这样说：作者用受访者的履历训练提升模型，测试集上预测值和真值的相关为 0.6；预测的鹰派程度每高一个标准差，危机升级的概率高 β；换用 lasso 和随机森林结果相近，自助法置信区间不含 0；但模型对冷战一代是否同样准确，只有几十个专家编码案例支撑。

## 常见陷阱

- **在测试集上调参，或把交叉验证误差当样本外表现**：两者都会高估模型。
- **预处理泄漏**：在全样本上做标准化、插补或特征筛选，再切分训练集和测试集；同一个人出现在多轮问卷里、被分到训练和测试两边，也是泄漏。
- **把训练集上的准确率当效度**：准确率说的是受访者，效度要在历史人物身上证明。
- **解读第一步的系数**：lasso 留下哪些变量、提升法的特征重要性，都只服务于预测，换一个随机种子就可能不同。
- **用硬标签代替概率**：把分类概率切成 0 和 1 丢掉信息；二分类的误分类和真值负相关，不是经典测量误差，标准的衰减校正公式不再适用。
- **第二步控制了全部预测特征**：第一步是线性模型时，预测值和这些控制变量完全共线；用非线性学习器时，β 只靠模型的非线性来识别，非常脆弱。
- **只报告效果最好的学习器**：挑选本身就制造偏差，诚实的做法是事先定好主模型，其余作稳健性。
- **训练标签本身有误差**：问卷自报的特质也带噪声，第一步能学到的上限就是这个噪声水平。

## 代码写法

R，常用 glmnet、caret，第二步用 fixest：

```r
library(glmnet)
library(caret)
library(fixest)
set.seed(2024)

# 1. 切分有真值的样本：80% 训练，20% 测试；测试集只在最后用一次
fml   <- hawk ~ age + military + educ + cohort
rhs   <- delete.response(terms(fml))
idx   <- createDataPartition(survey$hawk, p = 0.8, list = FALSE)
train <- survey[idx, ]
test  <- survey[-idx, ]

# 2. lasso：10 折交叉验证选惩罚强度
cv_l <- cv.glmnet(model.matrix(fml, data = train)[, -1], train$hawk, alpha = 1, nfolds = 10)

# 3. 提升法：caret 在训练集内部做 10 折交叉验证，调轮数、深度和学习率
grid <- expand.grid(n.trees = c(200, 500, 1000), interaction.depth = 1:3,
                    shrinkage = c(0.01, 0.05), n.minobsinnode = 10)
gb <- train(fml, data = train, method = "gbm", tuneGrid = grid,
            trControl = trainControl(method = "cv", number = 10), verbose = FALSE)

# 4. 测试集上的表现和校准斜率
test$hawk_hat   <- predict(gb, newdata = test)
test$hawk_lasso <- as.numeric(predict(cv_l, newx = model.matrix(fml, data = test)[, -1], s = "lambda.1se"))
cor(test$hawk_hat, test$hawk)
cor(test$hawk_hat, test$hawk_lasso)           # 两种学习器的分数是否一致
coef(lm(hawk ~ hawk_hat, data = test))["hawk_hat"]

# 5. 给历史决策者打分，进入第二步
leaders$hawk_hat <- predict(gb, newdata = leaders)
m <- feols(escalate ~ hawk_hat + cap_ratio + alliance | year, data = leaders, cluster = ~country)

# 6. 自助法：整条流程重跑；为了速度这里用 lasso，正式做法应与主模型一致
#    目标样本按国家聚类时，应整国重抽
boot_beta <- replicate(200, {
  tr  <- survey[sample(nrow(survey), replace = TRUE), ]
  lg  <- leaders[sample(nrow(leaders), replace = TRUE), ]
  fit <- cv.glmnet(model.matrix(fml, data = tr)[, -1], tr$hawk, alpha = 1)
  lg$hawk_hat <- as.numeric(predict(fit, newx = model.matrix(rhs, data = lg)[, -1], s = "lambda.1se"))
  coef(feols(escalate ~ hawk_hat + cap_ratio + alliance | year, data = lg))["hawk_hat"]
})
quantile(boot_beta, c(0.025, 0.975))
```

Stata，16 版起有官方的 lasso 系列命令；没有官方的提升法，需要时作者常在 do 文件里嵌一段 `python:` 调用 scikit-learn：

```stata
* 1. 有真值的受访者随机分成训练 (1) 和测试 (2)
splitsample if !missing(hawk), generate(part) split(0.8 0.2) rseed(2024)

* 2. lasso，10 折交叉验证；lassogof 分别报告训练集和测试集上的拟合
lasso linear hawk age i.military educ cohort if part == 1, selection(cv, folds(10)) rseed(2024)
lassogof, over(part)

* 3. 给所有人打分，包括没有 hawk 的历史决策者；测试集上看校准斜率
predict hawk_hat, penalized
regress hawk hawk_hat if part == 2

* 4. 第二步只用历史决策者
regress escalate hawk_hat cap_ratio alliance i.year if is_leader == 1, vce(cluster country)

* 5. 自助法：把两步写进一个程序，按受访者和决策者分层重抽
capture program drop twostep
program define twostep
    capture drop hawk_hat
    lasso linear hawk age i.military educ cohort if is_leader == 0, rseed(1)
    predict hawk_hat, penalized
    regress escalate hawk_hat cap_ratio alliance i.year if is_leader == 1
end
bootstrap _b[hawk_hat], reps(200) seed(2024) strata(is_leader): twostep
```

Python，scikit-learn 加 statsmodels：

```python
import pandas as pd
import statsmodels.formula.api as smf
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.linear_model import LassoCV
from sklearn.model_selection import GridSearchCV, KFold, train_test_split
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

features = ["age", "military", "educ", "cohort"]
X_tr, X_te, z_tr, z_te = train_test_split(
    survey[features], survey["hawk"], test_size=0.2, random_state=2024)

# lasso：标准化放进管道，交叉验证的每一折各自标准化，避免泄漏
lasso = make_pipeline(StandardScaler(), LassoCV(cv=10)).fit(X_tr, z_tr)

# 提升法：网格搜索加 10 折交叉验证
gb = GridSearchCV(
    HistGradientBoostingRegressor(random_state=2024),
    {"learning_rate": [0.03, 0.1], "max_depth": [2, 3], "max_iter": [200, 500]},
    cv=KFold(n_splits=10, shuffle=True, random_state=2024),
    scoring="neg_root_mean_squared_error",
).fit(X_tr, z_tr)

# 测试集上的校准斜率
test = pd.DataFrame({"z": z_te, "z_hat": gb.predict(X_te)})
print(smf.ols("z ~ z_hat", data=test).fit().params["z_hat"])

# 给历史决策者打分，进入第二步
leaders["hawk_hat"] = gb.predict(leaders[features])
m = smf.ols("escalate ~ hawk_hat + cap_ratio + alliance + C(year)", data=leaders).fit(
    cov_type="cluster", cov_kwds={"groups": leaders["country"]})
```

在作者代码里认机器学习测量，看这几处：在一份数据上调用 `cv.glmnet`、`glmnet`、`train`、`gbm`、`xgb.train`、`randomForest`、`ranger`、`SuperLearner`，Stata 的 `lasso`、`elasticnet`、`splitsample`，Python 的 `GridSearchCV`、`cross_val_score`、`train_test_split`；随后对另一份数据调用 `predict(..., newdata = ...)` 或 `predict(..., newx = ...)`，存成一个新列；这一列再出现在 `lm`、`feols`、`regress` 的右边。拟合和打分用的不是同一份数据，是认出这种方法的关键；如果 `replicate`、`boot` 或 `bootstrap` 包住了整段流程，说明作者在传递第一步的不确定性。

## 延伸阅读

- Hastie, Trevor, Robert Tibshirani, and Jerome Friedman. 2009. *The Elements of Statistical Learning*. 2nd ed. Springer. 第 3、7、10 章。
- James, Gareth, Daniela Witten, Trevor Hastie, and Robert Tibshirani. 2021. *An Introduction to Statistical Learning: with Applications in R*. 2nd ed. Springer. 第 5、6、8 章。
- Mullainathan, Sendhil, and Jann Spiess. 2017. "Machine Learning: An Applied Econometric Approach." *Journal of Economic Perspectives* 31 (2).
- Grimmer, Justin, Margaret E. Roberts, and Brandon M. Stewart. 2021. "Machine Learning for Social Science: An Agnostic Approach." *Annual Review of Political Science* 24.
- Fong, Christian, and Matthew Tyler. 2021. "Machine Learning Predictions as Regression Covariates." *Political Analysis* 29.
- Pagan, Adrian. 1984. "Econometric Issues in the Analysis of Regressions with Generated Regressors." *International Economic Review* 25 (1).
- Blackwell, Matthew, James Honaker, and Gary King. 2017. "A Unified Approach to Measurement Error and Missing Data: Overview and Applications." *Sociological Methods & Research* 46 (3).
- Carroll, Raymond J., David Ruppert, Leonard A. Stefanski, and Ciprian M. Crainiceanu. 2006. *Measurement Error in Nonlinear Models: A Modern Perspective*. 2nd ed. Chapman & Hall/CRC.
