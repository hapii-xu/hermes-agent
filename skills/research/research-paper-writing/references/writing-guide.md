# ML 论文写作哲学与最佳实践

本参考汇编了 Neel Nanda、Andrej Karpathy、Sebastian Farquhar、Zachary Lipton 和 Jacob Steinhardt 等知名 ML 研究者的写作建议。

---

## 目录

- [叙事原则](#叙事原则)
- [时间分配](#时间分配)
- [摘要写作公式](#摘要写作公式)
- [引言结构](#引言结构)
- [句子层面的清晰度](#句子层面的清晰度)
- [用词与精确性](#用词与精确性)
- [数学写作](#数学写作)
- [图设计](#图设计)
- [应避免的常见错误](#应避免的常见错误)

---

## 叙事原则

### 来自 Neel Nanda

「论文是一个简短、严谨、以证据为基础的技术故事，带有读者关心的结论。」

叙事建立在三大支柱之上，到引言结束时必须极其清晰：

**「是什么（What）」**：一到三个具体的新颖主张，统合在一个连贯主题下。像「我们研究 X」这种含糊的贡献会立即失败——审稿人需要精确、可证伪的主张。

**「为什么（Why）」**：严谨的实证证据，令人信服地支撑那些主张，包括诚实调参的强基线，以及能区分相互竞争假设的实验，而不是仅仅展示「还不错的结果」。

**「那又怎样（So What）」**：读者为什么应当关心，把你的贡献与社区公认重要的问题联系起来。

### 来自 Andrej Karpathy

「论文不是你报告的一堆随机实验。论文推销的是之前不存在或不显眼的单一东西。整篇论文围绕这个核心贡献以外科手术般的精度组织起来。」

无论你呈现的是新架构、理论结果，还是对现有方法的更深入理解，这都适用——NeurIPS 明确指出「原创性不一定要求全新的方法」。

**实践含义**：如果你无法用一句话陈述你的贡献，你还没有真正写成一篇论文。其他一切——实验、相关工作、讨论——都只是为了支撑那个核心主张而存在。

---

## 时间分配

### 来自 Neel Nanda

在以下每一项上花**大致相同的时间**：
1. 摘要
2. 引言
3. 图
4. 其他所有内容合起来

这不是夸张——大多数审稿人在到达你的方法章节之前就已形成初步判断。读者按可预测的模式接触你的论文：**标题 → 摘要 → 引言 → 图 → 也许看其余部分。**

### 审稿人阅读模式

对审稿人行为的研究表明：
- 摘要 100% 会被读
- 引言被 90%+ 的审稿人略读
- 多数审稿人在方法之前先看图
- 只有在建立兴趣之后才读完整方法

**含义**：把论文的价值前置。不要把贡献埋在后面。

---

## 摘要写作公式

### Sebastian Farquhar 的 5 句公式

1. **你取得了什么**：「我们提出……」「我们证明……」「我们展示……」
2. **为什么这很难且重要**
3. **你怎么做到的**（带专业关键词便于检索）
4. **你有什么证据**
5. **你最了不起的数字/结果**

### 示例（好摘要）

```
We prove that gradient descent on overparameterized neural networks
converges to global minima at a linear rate. [What]
This resolves a fundamental question about why deep learning works
despite non-convex optimization landscapes. [Why hard/important]
Our proof relies on showing that the Neural Tangent Kernel remains
approximately constant during training, reducing the problem to
kernel regression. [How with keywords]
We validate our theory on CIFAR-10 and ImageNet, showing that
predicted convergence rates match experiments within 5%. [Evidence]
This is the first polynomial-time convergence guarantee for
networks with practical depth and width. [Remarkable result]
```
（译注：上述为英文摘要示例，保留原文以体现写作结构。）

### 应避免什么

来自 Zachary Lipton：「如果第一句话可以拼到任何 ML 论文前面，就删掉它。」

**删掉这些开头**：
- 「大语言模型已经取得了显著成功……」
- 「深度学习已经革命性地改变了……」
- 「近年来，神经网络已经……」

**改用你的具体贡献开头。**

---

## 引言结构

### 要求

- **最多 1-1.5 页**（双栏格式下）
- **方法应在第 2-3 页之前开始**
- 必须包含 **2-4 条要点式贡献清单**（每条最多 1-2 行）

### 结构模板

```markdown
1. 开篇钩子（2-3 句）
   - 陈述你的论文要解决的问题
   - 为什么现在就重要

2. 背景/挑战（1 段）
   - 是什么让这个问题难？
   - 别人尝试过什么？为什么不够？

3. 你的方法（1 段）
   - 你做了什么不同的？
   - 启用你贡献的关键洞察

4. 贡献要点（2-4 条）
   - 要具体、可证伪
   - 每条要点：最多 1-2 行

5. 结果预览（2-3 句）
   - 最亮眼的数字
   - 评估范围

6. 论文组织（可选，1-2 句）
   - 「第 2 节呈现……第 3 节描述……」
```

### 贡献要点：好 vs 坏

**好：**
- 我们证明在假设 Y 下 X 在 O(n log n) 时间内收敛
- 我们提出 Z，一种把内存减少 40% 的 3 层架构
- 我们展示 A 在基准 C 上比 B 强 15%

**坏：**
- 我们研究 X 问题（这不是贡献）
- 我们提供大量实验（太含糊）
- 我们对该领域做出若干贡献（什么也没说）

---

## 句子层面的清晰度

### 来自 Gopen & Swan：《科学写作的科学》

George Gopen 和 Judith Swan 1990 年的开创性论文确立了**读者对信息在散文中出现的位置有结构性预期**。违反这些预期会迫使读者把精力花在结构上而非内容上。

> 「若读者要领会作者的意思，作者必须理解读者需要什么。」

#### 读者预期的 7 条原则

**原则 1：主谓邻近**

让语法主语和动词紧挨在一起。任何插入的内容都会被读作较不重要的打断。

**弱**：「The model, which was trained on 100M tokens and fine-tuned on domain-specific data using LoRA with rank 16, achieves state-of-the-art results」

**强**：「The model achieves state-of-the-art results after training on 100M tokens and fine-tuning with LoRA (rank 16)」

**原则 2：重音位置（把最好的留到最后）**

读者会自然地强调**句子的最后几个词**。把最重要的信息放在那里。

**弱**：「Accuracy improves by 15% when using attention」
**强**：「When using attention, accuracy improves by **15%**」

**原则 3：主题位置（先说重要的）**

句子开头建立视角。把「谁的故事」要素放在前面——读者期望句子是关于最先出现的那个角色的。

**弱**：「A novel attention mechanism that computes alignment scores is introduced」
**强**：「To address the alignment problem, we introduce a novel attention mechanism」

**原则 4：先旧信息后新信息**

把熟悉信息（旧）放在主题位置以向后衔接；把新信息放在重音位置以示强调。

**弱**：「Sparse attention was introduced by Child et al. The quadratic complexity of standard attention motivates this work.」
**强**：「Standard attention has quadratic complexity. To address this, Child et al. introduced sparse attention.」

**原则 5：一个单元一个功能**

每个话语单元（句子、段落、章节）应服务单一功能。如果你有两点，就用两个单元。

**原则 6：在动词里表达动作**

把每个句子的动作放在动词里，而不是名词化的名词里。

**弱**：「We performed an analysis of the results」（名词化）
**强**：「We analyzed the results」（动作在动词里）

**原则 7：新信息之前先给上下文**

在要求读者考虑任何新东西之前，先提供上下文。这在所有层面都适用——句子、段落、章节。

**弱**：「Equation 3 shows that convergence is guaranteed when the learning rate satisfies...」
**强**：「For convergence to be guaranteed, the learning rate must satisfy the condition in Equation 3...」

#### 汇总表

| 原则 | 规则 | 助记 |
|-----------|------|----------|
| 主谓邻近 | 主语和动词靠近 | 「别打断自己」 |
| 重音位置 | 强调在句末 | 「把最好的留到最后」 |
| 主题位置 | 上下文在句首 | 「先说重要的」 |
| 先旧后新 | 熟悉 → 陌生 | 「在已知地面上建构」 |
| 一单元一功能 | 每段 = 一个要点 | 「一个容器装一个想法」 |
| 动作在动词 | 用动词，不用名词化 | 「动词做事，名词静坐」 |
| 先上下文后新 | 呈现之前先解释 | 「先搭台」 |

---

## 微观层面写作技巧

### 来自 Ethan Perez（Anthropic）

这些实用的微观技巧在句子和词汇层面提升清晰度。

#### 代词管理

**最小化代词**（「this」「it」「these」「that」）。当代词必要时，把它们当形容词与名词连用：

**弱**：「This shows that the model converges.」
**强**：「This result shows that the model converges.」

**弱**：「It improves performance.」
**强**：「This modification improves performance.」

#### 动词位置

**把动词放在句子靠前位置**以便于解析：

**弱**：「The gradient, after being computed and normalized, updates the weights.」
**强**：「The gradient updates the weights after being computed and normalized.」

#### 撇号展开

为清晰起见转换所有格结构：

**原**：「X's Y」 → **展开**：「The Y of X」

**之前**：「The model's accuracy on the test set」
**之后**：「The accuracy of the model on the test set」

这不总是更好，但当句子读起来别扭时，试试展开。

#### 应删除的词

几乎所有情况下都删掉这些填充词：
- 「actually」
- 「a bit」
- 「fortunately」 / 「unfortunately」
- 「very」 / 「really」
- 「quite」
- 「basically」
- 「essentially」
- 过度的连接词（不需要时的「however」「moreover」「furthermore」）

#### 句子构造规则

1. **一句一个想法** —— 若难以用一句话表达，就需要两句
2. **不要重复发音** —— 避免同句中出现发音相似的词
3. **每句都增加信息** —— 删掉仅仅重述的句子
4. **始终用主动语态** —— 指明行动者（「We find...」而非「It is found...」）
5. **展开缩写** —— 「don't」→「do not」以保持正式

#### 段落架构

- **首句**：清晰陈述要点
- **中间句**：用证据支撑
- **末句**：强化或过渡

不要把关键信息埋在段落中间。

---

## 用词与精确性

### 来自 Zachary Lipton

**除非存在真正的不确定，否则消除对冲**：
- 除非必要，删除「may」和「can」
- 「provides *very* tight approximation」透着不自信
- 「provides tight approximation」是自信的

**避免空洞的强化词**：
- 删除：very、extremely、highly、significantly（除非是统计意义上的）
- 这些词信号的是不自信，而非力量

### 来自 Jacob Steinhardt

**精确优于简短**：用具体的词替换含糊的词。

| 含糊 | 具体 |
|-------|----------|
| performance | accuracy、latency、throughput |
| improves | 准确率提升 X%、延迟降低 Y |
| large | 1B 参数、100M token |
| fast | 3 倍更快、50ms 延迟 |
| good results | 92% 准确率、0.85 F1 |

**一致的术语**：用不同的词指代同一概念会造成混乱。

**选定一个并坚持**：
- 「model」 vs 「network」 vs 「architecture」
- 「training」 vs 「learning」 vs 「optimization」
- 「sample」 vs 「example」 vs 「instance」

### 词汇信号

**避免暗示增量工作的词**：
- 永不：「combine」「modify」「expand」「extend」
- 改用：「develop」「propose」「introduce」

**原因**：「We combine X and Y」听起来像你把两个已有想法钉在一起。「We develop a method that leverages X for Y」听起来像真正的贡献。

---

## 数学写作

### 来自 Ethan Perez

**展开撇号** 以求清晰：
- 弱：「X's Y」
- 强：「The Y of X」

示例：「the model's accuracy」 → 「the accuracy of the model」

### 一般原则

1. **在定理之前正式陈述所有假设**
2. **在证明旁边提供直观解释**
3. **全篇使用一致的记号**
4. **首次使用时定义符号**

### 记号约定

```latex
% 标量：小写斜体
$x$, $y$, $\alpha$, $\beta$

% 向量：小写粗体
$\mathbf{x}$, $\mathbf{v}$

% 矩阵：大写粗体
$\mathbf{W}$, $\mathbf{X}$

% 集合：大写花体
$\mathcal{X}$, $\mathcal{D}$

% 函数：命名函数用 roman
$\mathrm{softmax}$, $\mathrm{ReLU}$
```

---

## 图设计

### 来自 Neel Nanda

图应能讲一个连贯的故事，即便读者跳过正文。许多读者最初确实会跳过正文。

### 设计原则

1. **图 1 至关重要**：通常是读者在摘要之后首先查看的
2. **自包含的图注**：读者应能脱离正文理解图
3. **图内不要标题**：图注承担此功能（ICML/NeurIPS 规则）
4. **矢量图**：图表用 PDF/EPS，照片才用 PNG（600 DPI）

### 无障碍要求

8% 的男性有色觉缺陷。你的图必须对他们也有效。

**解决方案**：
- 使用色盲友好调色板：Okabe-Ito 或 Paul Tol
- 避免红绿组合
- 验证图在灰度下也有效
- 除颜色外，用不同线型（实线、虚线、点线）区分

### 工具

```python
# SciencePlots：出版级就绪样式
import matplotlib.pyplot as plt
plt.style.use(['science', 'ieee'])

# 或 Nature 风格
plt.style.use(['science', 'nature'])
```

---

## 应避免的常见错误

### 结构错误

| 错误 | 解决方案 |
|---------|----------|
| 引言太长（>1.5 页） | 把背景移到相关工作 |
| 方法被埋（第 3 页之后） | 贡献前置，精简引言 |
| 缺少贡献要点 | 加 2-4 条具体、可证伪的主张 |
| 实验没有明确主张 | 说明每个实验测试什么 |

### 写作错误

| 错误 | 解决方案 |
|---------|----------|
| 通用的摘要开头 | 用你的具体贡献开头 |
| 术语不一致 | 每个概念选定一个词 |
| 被动语态过多 | 用主动语态：「We show」而非「It is shown」 |
| 处处对冲 | 除非真的不确定，否则要自信 |

### 图错误

| 错误 | 解决方案 |
|---------|----------|
| 图表用栅格图 | 用矢量（PDF/EPS） |
| 红绿配色 | 用色盲友好调色板 |
| 图内有标题 | 把标题放进图注 |
| 图注依赖正文 | 让图注自包含 |

### 引用错误

| 错误 | 解决方案 |
|---------|----------|
| 逐篇列举的相关工作 | 按方法论组织 |
| 漏掉相关引用 | 审稿人就是论文作者——慷慨引用 |
| AI 生成的引用 | 始终通过 API 验证 |
| 引用格式不一致 | 用 BibLaTeX 配一致 key |

---

## 提交前检查清单

提交前，核实：

**叙事**：
- [ ] 能用一句话陈述贡献
- [ ] 引言中三大支柱（What/Why/So What）清晰
- [ ] 每个实验都支撑一个具体主张

**结构**：
- [ ] 摘要遵循 5 句公式
- [ ] 引言 ≤1.5 页
- [ ] 方法在第 2-3 页前开始
- [ ] 包含 2-4 条贡献要点
- [ ] 有局限性章节

**写作**：
- [ ] 全篇术语一致
- [ ] 没有通用开头句
- [ ] 除非必要，已删除对冲
- [ ] 所有图都有自包含的图注

**技术**：
- [ ] 所有引用都通过 API 验证
- [ ] 误差棒附带方法论
- [ ] 算力资源有文档
- [ ] 已说明代码/数据可得性
