# 实证 ML 之外的论文类型

本指南用于撰写非标准论文类型：理论论文、综述/教程论文、基准/数据集论文和立场论文。每种类型都有独特的结构、证据标准和会议期望。

---

## 目录

- [理论论文（Theory Papers）](#理论论文theory-papers)
- [综述与教程论文（Survey and Tutorial Papers）](#综述与教程论文survey-and-tutorial-papers)
- [基准与数据集论文（Benchmark and Dataset Papers）](#基准与数据集论文benchmark-and-dataset-papers)
- [立场论文（Position Papers）](#立场论文position-papers)
- [可复现性与复现论文（Reproducibility and Replication Papers）](#可复现性与复现论文reproducibility-and-replication-papers)

---

## 理论论文（Theory Papers）

### 何时撰写理论论文

如果你的论文满足以下条件，就应该是理论论文：
- 主要贡献是一个定理、界、不可能性结果或形式化刻画
- 实验只是辅助性验证，不是核心证据
- 贡献在于推进理解，而不是达到 state-of-the-art 的数值

### 结构

```
1. Introduction (1-1.5 pages)
   - Problem statement and motivation
   - Informal statement of main results
   - Comparison to prior theoretical work
   - Contribution bullets (state theorems informally)

2. Preliminaries (0.5-1 page)
   - Notation table
   - Formal definitions
   - Assumptions (numbered, referenced later)
   - Known results you build on

3. Main Results (2-3 pages)
   - Theorem statements (formal)
   - Proof sketches (intuition + key steps)
   - Corollaries and special cases
   - Discussion of tightness / optimality

4. Experimental Validation (1-2 pages, optional but recommended)
   - Do theoretical predictions match empirical behavior?
   - Synthetic experiments that isolate the phenomenon
   - Comparison to bounds from prior work

5. Related Work (1 page)
   - Theoretical predecessors
   - Empirical work your theory explains

6. Discussion & Open Problems (0.5 page)
   - Limitations of your results
   - Conjectures suggested by your analysis
   - Concrete open problems

Appendix:
   - Full proofs
   - Technical lemmas
   - Extended experimental details
```

（译注：1. 引言（1–1.5 页）：问题陈述与动机、主要结果的非正式陈述、与既有理论工作的对比、贡献要点（非正式陈述定理）；2. 预备知识（0.5–1 页）：符号表、形式化定义、假设（编号、后续引用）、所依赖的已知结果；3. 主要结果（2–3 页）：定理陈述（形式化）、证明概要（直觉 + 关键步骤）、推论与特例、对紧致性/最优性的讨论；4. 实验验证（1–2 页，可选但推荐）：理论预测是否与经验行为相符、隔离现象的人造实验、与既有工作的界对比；5. 相关工作（1 页）：理论先驱、你的理论所能解释的经验工作；6. 讨论与开放问题（0.5 页）：结果局限、分析所引出的猜想、具体开放问题；附录：完整证明、技术引理、扩展实验细节。）

### 撰写定理

**一个表述良好的定理模板：**

```latex
\begin{assumption}[Bounded Gradients]\label{assum:bounded-grad}
There exists $G > 0$ such that $\|\nabla f(x)\| \leq G$ for all $x \in \mathcal{X}$.
\end{assumption}

\begin{theorem}[Convergence Rate]\label{thm:convergence}
Under Assumptions~\ref{assum:bounded-grad} and~\ref{assum:smoothness},
Algorithm~\ref{alg:method} with step size $\eta = \frac{1}{\sqrt{T}}$ satisfies
\[
\frac{1}{T}\sum_{t=1}^{T} \mathbb{E}\left[\|\nabla f(x_t)\|^2\right]
\leq \frac{2(f(x_1) - f^*)}{\sqrt{T}} + \frac{G^2}{\sqrt{T}}.
\]
In particular, after $T = O(1/\epsilon^2)$ iterations, we obtain an
$\epsilon$-stationary point.
\end{theorem}
```

**定理陈述的规则：**
- 明确陈述所有假设（编号、命名）
- 给出形式化的界，而不只是「以 O(·) 速率收敛」
- 加一个通俗语言的推论：「特别地，这意味着……」
- 与已知界比较：「相较于 [既有工作] 的 O(·) 界，本结果改进了……倍」

### 证明概要（Proof Sketches）

证明概要是理论论文正文中最重要的部分。审稿人会据此评估你是真正有洞见，还是仅有机械推导。

**好的证明概要范式：**

```latex
\begin{proof}[Proof Sketch of Theorem~\ref{thm:convergence}]
The key insight is that [one sentence describing the main idea].

The proof proceeds in three steps:
\begin{enumerate}
\item \textbf{Decomposition.} We decompose the error into [term A]
  and [term B] using [technique]. This reduces the problem to
  bounding each term separately.

\item \textbf{Bounding [term A].} By [assumption/lemma], [term A]
  is bounded by $O(\cdot)$. The critical observation is that
  [specific insight that makes this non-trivial].

\item \textbf{Combining.} Choosing $\eta = 1/\sqrt{T}$ balances
  the two terms, yielding the stated bound.
\end{enumerate}

The full proof, including the technical lemma for Step 2,
appears in Appendix~\ref{app:proofs}.
\end{proof}
```

（译注：关键洞见是 [一句话描述核心思想]。证明分三步进行：1. 分解（Decomposition）。用 [技术] 把误差分解为 [项 A] 和 [项 B]。这将问题转化为分别给每一项做界。2. 给 [项 A] 做界。根据 [假设/引理]，[项 A] 被 O(·) 所界。关键观察在于 [使该步骤非平凡的具体洞见]。3. 合并（Combining）。取 η = 1/√T 平衡两项，得到所述界。包含第 2 步技术引理的完整证明见附录。）

**差的证明概要**：换种符号重述定理，或仅仅说「证明遵循标准技术」。

### 附录中的完整证明

```latex
\appendix
\section{Proofs}\label{app:proofs}

\subsection{Proof of Theorem~\ref{thm:convergence}}

We first establish two technical lemmas.

\begin{lemma}[Descent Lemma]\label{lem:descent}
Under Assumption~\ref{assum:smoothness}, for any step size $\eta \leq 1/L$:
\[
f(x_{t+1}) \leq f(x_t) - \frac{\eta}{2}\|\nabla f(x_t)\|^2 + \frac{\eta^2 L}{2}\|\nabla f(x_t)\|^2.
\]
\end{lemma}

\begin{proof}
[Complete proof with all steps]
\end{proof}

% Continue with remaining lemmas and main theorem proof
% 继续完成剩余引理和主定理的证明
```

### 理论论文的常见陷阱

| 陷阱 | 问题 | 修复 |
|---------|---------|-----|
| 假设过强 | 使结果变得平凡 | 讨论哪些假设是必要的；证明下界 |
| 未与既有界比较 | 审稿人无法评估贡献 | 加一个界的对比表 |
| 证明概要只是完整证明的缩水版 | 无法传达洞见 | 聚焦于 1–2 个关键想法；把机械推导放到附录 |
| 无实验验证 | 审稿人质疑实用相关性 | 加上测试预测的人造实验 |
| 符号不一致 | 让审稿人困惑 | 在预备知识中建立符号表 |
| 在简单证明可行时使用过于复杂的证明 | 审稿人怀疑有错 | 优先选择清晰而非一般性 |

### 理论论文的会议

| 会议 | 理论录取率 | 备注 |
|-------|----------------------|-------|
| **NeurIPS** | 中等 | 看重具有实际含义的理论 |
| **ICML** | 高 | 强理论 track |
| **ICLR** | 中等 | 偏好带实验验证的理论 |
| **COLT** | 高 | 聚焦理论的会议 |
| **ALT** | 高 | 算法学习理论 |
| **STOC/FOCS** | 适合 TCS 风格的结果 | 若贡献主要是组合/算法方面 |
| **JMLR** | 高 | 无页数限制；适合长证明 |

---

## 综述与教程论文（Survey and Tutorial Papers）

### 何时撰写综述

- 某个子领域已成熟到值得做综合
- 你发现了各篇论文之间未被指出的联系
- 新进入该领域的人没有好的入门点
- 自上一篇综述以来，领域格局已发生显著变化

**警告**：综述需要真正的专长。非该领域的人写的综述，无论多么全面，都会错过细节并错误刻画工作。

### 结构

```
1. Introduction (1-2 pages)
   - Scope definition (what's included and excluded, and why)
   - Motivation for the survey now
   - Overview of organization (often with a figure)

2. Background / Problem Formulation (1-2 pages)
   - Formal problem definition
   - Notation (used consistently throughout)
   - Historical context

3. Taxonomy (the core contribution)
   - Organize methods along meaningful axes
   - Present taxonomy as a figure or table
   - Each category gets a subsection

4. Detailed Coverage (bulk of paper)
   - For each category: representative methods, key ideas, strengths/weaknesses
   - Comparison tables within and across categories
   - Don't just describe — analyze and compare

5. Experimental Comparison (if applicable)
   - Standardized benchmark comparison
   - Fair hyperparameter tuning for all methods
   - Not always feasible but significantly strengthens the survey

6. Open Problems & Future Directions (1-2 pages)
   - Unsolved problems the field should tackle
   - Promising but underexplored directions
   - This section is what makes a survey a genuine contribution

7. Conclusion
```

（译注：1. 引言（1–2 页）：范围定义（纳入与排除什么，以及为什么）、当下做此综述的动机、组织概览（常配图）；2. 背景/问题形式化（1–2 页）：形式化问题定义、符号（全文一致使用）、历史背景；3. 分类法（核心贡献）：沿有意义的轴线组织方法、以图或表呈现分类法、每个类别各设小节；4. 详细覆盖（论文主体）：每个类别给出代表性方法、关键想法、优缺点、类别内与跨类别对比表、不要只描述——要分析比较；5. 实验对比（如适用）：标准化基准对比、对所有方法做公平超参数调优、不一定可行但会显著增强综述；6. 开放问题与未来方向（1–2 页）：该领域应解决的未解问题、有前景但欠探索的方向、这一节使综述成为真正贡献；7. 结论。）

### 分类法设计

分类法是综述的核心智识贡献。它应当：

- **有意义**：类别应对应真实的方法差异，而不是任意的分组
- **完备**：每一篇相关论文都应能归入某一处
- **互斥（理想情况下）**：每篇论文归入一个主类别
- **命名有信息量**：「基于注意力的方法」>「类别 3」
- **可视化**：展示分类法的图几乎总是有帮助的

**「LLM 推理」综述的分类轴线示例：**
- 按技术：思维链（chain-of-thought）、思维树（tree-of-thought）、自洽性（self-consistency）、工具使用
- 按训练要求：仅提示、微调、RLHF
- 按推理类型：数学、常识、逻辑、因果

### 写作标准

- **引用每一篇相关论文**——作者会检查自己的工作是否被纳入
- **公正**——不要否定你不偏爱的方法
- **综合而非只是罗列**——识别模式、权衡、开放问题
- **包含对比表**——即使是定性的也行（特征/属性清单）
- **投稿前更新**——检查 arXiv 上自你开始写作以来发表的论文

### 综述的会议

| 会议 | 备注 |
|-------|-------|
| **TMLR**（综述 track） | 专门的综述投稿；无页数限制 |
| **JMLR** | 长格式，广受尊敬 |
| **Foundations and Trends in ML** | 受邀，但可主动提议 |
| **ACM Computing Surveys** | 广泛的 CS 受众 |
| **arXiv**（独立） | 无同行评审，但若写得好可见度高 |
| **会议教程** | 在 NeurIPS/ICML/ACL 上做教程；再写成论文 |

---

## 基准与数据集论文（Benchmark and Dataset Papers）

### 何时撰写基准论文

- 现有基准不能衡量你认为重要的东西
- 出现了新能力但没有标准评估
- 现有基准已饱和（所有方法得分都 >95%）
- 你想在碎片化的子领域标准化评估

### 结构

```
1. Introduction
   - What evaluation gap does this benchmark fill?
   - Why existing benchmarks are insufficient

2. Task Definition
   - Formal task specification
   - Input/output format
   - Evaluation criteria (what makes a good answer?)

3. Dataset Construction
   - Data source and collection methodology
   - Annotation process (if human-annotated)
   - Quality control measures
   - Dataset statistics (size, distribution, splits)

4. Baseline Evaluation
   - Run strong baselines (don't just report random/majority)
   - Show the benchmark is challenging but not impossible
   - Human performance baseline (if feasible)

5. Analysis
   - Error analysis on baselines
   - What makes items hard/easy?
   - Construct validity: does the benchmark measure what you claim?

6. Intended Use & Limitations
   - What should this benchmark be used for?
   - What should it NOT be used for?
   - Known biases or limitations

7. Datasheet (Appendix)
   - Full datasheet for datasets (Gebru et al.)
```

（译注：1. 引言：该基准填补了什么评估空白？为何现有基准不足；2. 任务定义：形式化任务规格、输入/输出格式、评估标准（何为好答案？）；3. 数据集构建：数据来源与采集方法、标注过程（若为人工标注）、质量控制措施、数据集统计（规模、分布、划分）；4. 基线评估：跑强基线（不要只报告随机/多数类）、展示基准有挑战但并非不可能、人类表现基线（如可行）；5. 分析：对基线做错误分析、什么使条目难/易？构念效度（construct validity）：基准是否衡量了你所声称的？6. 预期用途与局限：该基准应用于什么？不应用于什么？已知偏见或局限；7. 数据表（附录）：数据集的完整 datasheet（Gebru 等）。）

### 证据标准

审稿人评估基准时所用的标准不同于方法论文：

| 标准 | 审稿人检查什么 |
|-----------|---------------------|
| **评估的新颖性** | 是否衡量了现有基准未衡量的东西？ |
| **构念效度** | 基准是否真正衡量了所陈述的能力？ |
| **难度校准** | 既不太易（饱和）也不太难（随机性能） |
| **标注质量** | 一致性指标、标注者资质、指南 |
| **文档** | Datasheet、许可证、维护计划 |
| **可复现性** | 其他人能否轻松使用此基准？ |
| **伦理考量** | 偏见分析、知情同意、敏感内容处理 |

### 数据集文档（必填）

遵循 Datasheets for Datasets 框架（Gebru et al., 2021）：

```
Datasheet Questions:
1. Motivation
   - Why was this dataset created?
   - Who created it and on behalf of whom?
   - Who funded the creation?

2. Composition
   - What do the instances represent?
   - How many instances are there?
   - Does it contain all possible instances or a sample?
   - Is there a label? If so, how was it determined?
   - Are there recommended data splits?

3. Collection Process
   - How was the data collected?
   - Who was involved in collection?
   - Over what timeframe?
   - Was ethical review conducted?

4. Preprocessing
   - What preprocessing was done?
   - Was the "raw" data saved?

5. Uses
   - What tasks has this been used for?
   - What should it NOT be used for?
   - Are there other tasks it could be used for?

6. Distribution
   - How is it distributed?
   - Under what license?
   - Are there any restrictions?

7. Maintenance
   - Who maintains it?
   - How can users contact the maintainer?
   - Will it be updated? How?
   - Is there an erratum?
```

（译注：Datasheet 问题：1. 动机：为何创建此数据集？谁创建、代表谁创建？谁资助？2. 组成：实例代表什么？有多少实例？是包含所有可能实例还是样本？是否有标签？若有，如何确定？是否有推荐的数据划分？3. 采集过程：数据如何采集？谁参与采集？在什么时间段？是否经过伦理审查？4. 预处理：做了什么预处理？是否保存了「原始」数据？5. 用途：曾被用于什么任务？不应用于什么？是否还有其他可用的任务？6. 分发：如何分发？何种许可证？是否有任何限制？7. 维护：谁维护？用户如何联系维护者？是否会更新？如何更新？是否有勘误？）

### 基准论文的会议

| 会议 | 备注 |
|-------|-------|
| **NeurIPS Datasets & Benchmarks** | 专门 track；此类的最佳会议 |
| **ACL**（资源论文） | 聚焦 NLP 的数据集 |
| **LREC-COLING** | 语言资源 |
| **TMLR** | 适合带分析的基准 |

---

## 立场论文（Position Papers）

### 何时撰写立场论文

- 你对领域应当如何发展有论点
- 你想挑战一个普遍假设
- 你想基于分析提出研究议程
- 你发现了当前方法学中的系统性问题

### 结构

```
1. Introduction
   - State your thesis clearly in the first paragraph
   - Why this matters now

2. Background
   - Current state of the field
   - Prevailing assumptions you're challenging

3. Argument
   - Present your thesis with supporting evidence
   - Evidence can be: empirical data, theoretical analysis, logical argument,
     case studies, historical precedent
   - Be rigorous — this isn't an opinion piece

4. Counterarguments
   - Engage seriously with the strongest objections
   - Explain why they don't undermine your thesis
   - Concede where appropriate — it strengthens credibility

5. Implications
   - What should the field do differently?
   - Concrete research directions your thesis suggests
   - How should evaluation/methodology change?

6. Conclusion
   - Restate thesis
   - Call to action
```

（译注：1. 引言：在第一段清晰陈述论点、为何当下重要；2. 背景：领域当前状态、你要挑战的普遍假设；3. 论证：用支持性证据呈现论点、证据可以是经验数据、理论分析、逻辑论证、案例研究、历史先例、要严谨——这不是社论；4. 反驳：认真对待最强有力的反对意见、解释为何它们不会动摇你的论点、在适当处让步——这会增强可信度；5. 含义：领域应当有何不同做法？你的论点所提示的具体研究方向、评估/方法学应如何改变？6. 结论：重申论点、行动号召。）

### 写作标准

- **以最强版本的论点开场**——不要在第一段就含糊其辞
- **诚实地回应反驳**——最好的立场论文回应的是最强而非最弱的反对意见
- **提供证据**——没有证据的立场论文只是社论
- **要具体**——「该领域应当做 X」好于「需要更多工作」
- **不要稻草人化既有工作**——公正地刻画对立立场

### 立场论文的会议

| 会议 | 备注 |
|-------|-------|
| **ICML**（立场 track） | 专门的立场论文 track |
| **NeurIPS**（Workshop 论文） | Workshop 常欢迎立场文章 |
| **ACL**（主题论文） | 当你的立场与会议主题相符时 |
| **TMLR** | 接受论证充分的立场论文 |
| **CACM** | 面向更广泛的 CS 受众 |

---

## 可复现性与复现论文（Reproducibility and Replication Papers）

### 何时撰写可复现性论文

- 你尝试复现已发表的结果并成功/失败
- 你想在不同条件下验证论断
- 你发现某个流行方法的性能依赖于未报告的细节

### 结构

```
1. Introduction
   - What paper/result are you reproducing?
   - Why is this reproduction valuable?

2. Original Claims
   - State the exact claims from the original paper
   - What evidence was provided?

3. Methodology
   - Your reproduction approach
   - Differences from original (if any) and why
   - What information was missing from the original paper?

4. Results
   - Side-by-side comparison with original results
   - Statistical comparison (confidence intervals overlap?)
   - What reproduced and what didn't?

5. Analysis
   - If results differ: why? What's sensitive?
   - Hidden hyperparameters or implementation details?
   - Robustness to seed, hardware, library versions?

6. Recommendations
   - For original authors: what should be clarified?
   - For practitioners: what to watch out for?
   - For the field: what reproducibility lessons emerge?
```

（译注：1. 引言：你在复现什么论文/结果？为何此复现有价值？2. 原始论断：陈述原论文的确切论断、提供了什么证据？3. 方法学：你的复现方法、与原作的差异（若有）及原因、原论文缺失了什么信息？4. 结果：与原结果的并排对比、统计对比（置信区间是否重叠？）、什么复现了什么没复现？5. 分析：若结果不同：为何？哪些敏感？隐藏的超参数或实现细节？对种子、硬件、库版本的鲁棒性？6. 建议：对原作者：应澄清什么？对实践者：应注意什么？对领域：浮现出什么可复现性经验？）

### 会议

| 会议 | 备注 |
|-------|-------|
| **ML Reproducibility Challenge** | NeurIPS 的年度挑战 |
| **ReScience** | 专门刊登复现工作的期刊 |
| **TMLR** | 接受带分析的复现 |
| **Workshops** | 主要会议的可复现性 workshop |
