# 来源文献目录

本文档列出了用于构建此 skill 的所有权威来源，按主题组织。

---

## 起源与归属

此 skill 中的写作哲学、引用验证工作流和会议参考资料最初由 **[Orchestra Research](https://github.com/orchestra-research)** 编译为 `ml-paper-writing` skill（2026 年 1 月），借鉴了 Neel Nanda 的博客文章及下面列出的其他研究者指南。该 skill 由 teknium（2026 年 1 月）整合进 hermes-agent，随后由 SHL0MS（2026 年 4 月，PR #4654）扩展为当前的 `research-paper-writing` 流水线，新增了实验设计、执行监控、迭代精炼和投稿阶段，同时保留了原始的写作哲学和参考文件。

---

## 写作哲学与指南

### 主要来源（必读）

| 来源 | 作者 | URL | 关键贡献 |
|--------|--------|-----|------------------|
| **Highly Opinionated Advice on How to Write ML Papers** | Neel Nanda | [Alignment Forum](https://www.alignmentforum.org/posts/eJGptPbbFPZGLpjsp/highly-opinionated-advice-on-how-to-write-ml-papers) | 叙事框架、「What/Why/So What」、时间分配 |
| **How to Write ML Papers** | Sebastian Farquhar (DeepMind) | [Blog](https://sebastianfarquhar.com/on-research/2024/11/04/how_to_write_ml_papers/) | 5 句摘要公式、结构模板 |
| **A Survival Guide to a PhD** | Andrej Karpathy | [Blog](http://karpathy.github.io/2016/09/07/phd/) | 论文结构配方、贡献框架 |
| **Heuristics for Scientific Writing** | Zachary Lipton (CMU) | [Blog](https://www.approximatelycorrect.com/2018/01/29/heuristics-technical-scientific-writing-machine-learning-perspective/) | 用词、章节平衡、强化词警告 |
| **Advice for Authors** | Jacob Steinhardt (UC Berkeley) | [Blog](https://jsteinhardt.stat.berkeley.edu/blog/advice-for-authors) | 精确胜于简洁、一致的术语 |
| **Easy Paper Writing Tips** | Ethan Perez (Anthropic) | [Blog](https://ethanperez.net/easy-paper-writing-tips/) | 微观技巧、撇号展开、清晰度窍门 |

### 基础科学写作

| 来源 | 作者 | URL | 关键贡献 |
|--------|--------|-----|------------------|
| **The Science of Scientific Writing** | Gopen & Swan | [PDF](https://cseweb.ucsd.edu/~swanson/papers/science-of-writing.pdf) | 主题/强调位置、旧信息在前、7 大原则 |
| **Summary of Science of Scientific Writing** | Lawrence Crowl | [Summary](https://www.crowl.org/Lawrence/writing/GopenSwan90.html) | Gopen & Swan 的浓缩版 |

### 其他资源

| 来源 | URL | 关键贡献 |
|--------|-----|------------------|
| How To Write A Research Paper In ML | [Blog](https://grigorisg9gr.github.io/machine%20learning/research%20paper/how-to-write-a-research-paper-in-machine-learning/) | 实操演练、LaTeX 技巧 |
| A Recipe for Training Neural Networks | [Karpathy Blog](http://karpathy.github.io/2019/04/25/recipe/) | 可转化为论文结构的调试方法学 |
| ICML Paper Writing Best Practices | [ICML](https://icml.cc/Conferences/2022/BestPractices) | 官方会议指南 |
| Bill Freeman's Writing Slides | [MIT](https://billf.mit.edu/sites/default/files/documents/cvprPapers.pdf) | 论文结构的视觉指南 |

---

## 官方会议指南

### NeurIPS

| 文档 | URL | 用途 |
|----------|-----|---------|
| Paper Checklist Guidelines | [NeurIPS](https://neurips.cc/public/guides/PaperChecklist) | 16 项强制清单 |
| Reviewer Guidelines 2025 | [NeurIPS](https://neurips.cc/Conferences/2025/ReviewerGuidelines) | 评估标准、评分 |
| Style Files | [NeurIPS](https://neurips.cc/Conferences/2025/PaperInformation/StyleFiles) | LaTeX 模板 |

### ICML

| 文档 | URL | 用途 |
|----------|-----|---------|
| Paper Guidelines | [ICML](https://icml.cc/Conferences/2024/PaperGuidelines) | 投稿要求 |
| Reviewer Instructions 2025 | [ICML](https://icml.cc/Conferences/2025/ReviewerInstructions) | 评审表单、评估 |
| Style & Author Instructions | [ICML](https://icml.cc/Conferences/2022/StyleAuthorInstructions) | 格式规格 |

### ICLR

| 文档 | URL | 用途 |
|----------|-----|---------|
| Author Guide 2026 | [ICLR](https://iclr.cc/Conferences/2026/AuthorGuide) | 投稿要求、LLM 披露 |
| Reviewer Guide 2025 | [ICLR](https://iclr.cc/Conferences/2025/ReviewerGuide) | 评审流程、评估 |

### ACL/EMNLP

| 文档 | URL | 用途 |
|----------|-----|---------|
| ACL Style Files | [GitHub](https://github.com/acl-org/acl-style-files) | LaTeX 模板 |
| ACL Rolling Review | [ARR](https://aclrollingreview.org/) | 投稿流程 |

### AAAI

| 文档 | URL | 用途 |
|----------|-----|---------|
| Author Kit 2026 | [AAAI](https://aaai.org/authorkit26/) | 模板与指南 |

### COLM

| 文档 | URL | 用途 |
|----------|-----|---------|
| Template | [GitHub](https://github.com/COLM-org/Template) | LaTeX 模板 |

---

## 引用 API 与工具

### API

| API | 文档 | 最适用于 |
|-----|---------------|----------|
| **Semantic Scholar** | [Docs](https://api.semanticscholar.org/api-docs/) | ML/AI 论文、引用图谱 |
| **CrossRef** | [Docs](https://www.crossref.org/documentation/retrieve-metadata/rest-api/) | DOI 查询、BibTeX 获取 |
| **arXiv** | [Docs](https://info.arxiv.org/help/api/basics.html) | 预印本、PDF 访问 |
| **OpenAlex** | [Docs](https://docs.openalex.org/) | 开放替代、批量访问 |

### Python 库

| 库 | 安装 | 用途 |
|---------|---------|---------|
| `semanticscholar` | `pip install semanticscholar` | Semantic Scholar 封装 |
| `arxiv` | `pip install arxiv` | arXiv 搜索与下载 |
| `habanero` | `pip install habanero` | CrossRef 客户端 |

### 引用验证

| 工具 | URL | 用途 |
|------|-----|---------|
| Citely | [citely.ai](https://citely.ai/citation-checker) | 批量验证 |
| ReciteWorks | [reciteworks.com](https://reciteworks.com/) | 正文引用检查 |

---

## 可视化与排版

### 制图

| 工具 | URL | 用途 |
|------|-----|---------|
| PlotNeuralNet | [GitHub](https://github.com/HarisIqbal88/PlotNeuralNet) | TikZ 神经网络图 |
| SciencePlots | [GitHub](https://github.com/garrettj403/SciencePlots) | 出版级 matplotlib |
| Okabe-Ito Palette | [Reference](https://jfly.uni-koeln.de/color/) | 色盲友好配色 |

### LaTeX 资源

| 资源 | URL | 用途 |
|----------|-----|---------|
| Overleaf Templates | [Overleaf](https://www.overleaf.com/latex/templates) | 在线 LaTeX 编辑器 |
| BibLaTeX Guide | [CTAN](https://ctan.org/pkg/biblatex) | 现代引用管理 |

---

## 关于 AI 写作与幻觉的研究

| 来源 | URL | 关键发现 |
|--------|-----|-------------|
| AI Hallucinations in Citations | [Enago](https://www.enago.com/academy/ai-hallucinations-research-citations/) | 约 40% 错误率 |
| Hallucination in AI Writing | [PMC](https://pmc.ncbi.nlm.nih.gov/articles/PMC10726751/) | 引用错误的类型 |
| NeurIPS 2025 AI Report | [ByteIota](https://byteiota.com/neurips-2025-100-ai-hallucinations-slip-through-review/) | 100+ 条幻觉引用 |

---

## 按主题的快速参考

### 关于叙事与结构
→ 入门：Neel Nanda、Sebastian Farquhar、Andrej Karpathy

### 关于句子层面的清晰度
→ 入门：Gopen & Swan、Ethan Perez、Zachary Lipton

### 关于用词与风格
→ 入门：Zachary Lipton、Jacob Steinhardt

### 关于会议特定要求
→ 入门：官方会议指南（NeurIPS、ICML、ICLR、ACL）

### 关于引用管理
→ 入门：Semantic Scholar API、CrossRef、citation-workflow.md

### 关于审稿人期望
→ 入门：会议审稿人指南、reviewer-guidelines.md

### 关于人工评估
→ 入门：human-evaluation.md、Prolific/MTurk 文档

### 关于非实证论文（理论、综述、基准、立场）
→ 入门：paper-types.md

---

## 人工评估与标注

| 来源 | URL | 关键贡献 |
|--------|-----|------------------|
| **Datasheets for Datasets** | Gebru et al., 2021 ([arXiv](https://arxiv.org/abs/1803.09010)) | 结构化数据集文档框架 |
| **Model Cards for Model Reporting** | Mitchell et al., 2019 ([arXiv](https://arxiv.org/abs/1810.03993)) | 结构化模型文档框架 |
| **Crowdsourcing and Human Computation** | [Survey](https://arxiv.org/abs/2202.06516) | 众包标注的最佳实践 |
| **Krippendorff's Alpha** | [Wikipedia](https://en.wikipedia.org/wiki/Krippendorff%27s_alpha) | 标注者间一致性指标参考 |
| **Prolific** | [prolific.co](https://www.prolific.co/) | 推荐的研究用众包平台 |

## 伦理与更广泛影响

| 来源 | URL | 关键贡献 |
|--------|-----|------------------|
| **ML CO2 Impact** | [mlco2.github.io](https://mlco2.github.io/impact/) | 计算碳足迹计算器 |
| **NeurIPS Broader Impact Guide** | [NeurIPS](https://neurips.cc/public/guides/PaperChecklist) | 关于影响声明的官方指南 |
| **ACL Ethics Policy** | [ACL](https://www.aclweb.org/portal/content/acl-code-ethics) | NLP 研究的伦理要求 |
