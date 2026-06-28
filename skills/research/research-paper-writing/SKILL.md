---
name: research-paper-writing
title: Research Paper Writing Pipeline
description: "撰写 ML 论文，面向 NeurIPS/ICML/ICLR：从设计到投稿。"
version: 1.1.0
author: Orchestra Research
license: MIT
dependencies: [semanticscholar, arxiv, habanero, requests, scipy, numpy, matplotlib, SciencePlots]
platforms: [linux, macos]
metadata:
  hermes:
    tags: [Research, Paper Writing, Experiments, ML, AI, NeurIPS, ICML, ICLR, ACL, AAAI, COLM, LaTeX, Citations, Statistical Analysis]
    category: research
    related_skills: [arxiv, ml-paper-writing, subagent-driven-development, plan]
    requires_toolsets: [terminal, files]

---

# Research Paper Writing Pipeline

用于产出可发表级别的 ML/AI 研究论文的端到端流水线，面向 **NeurIPS、ICML、ICLR、ACL、AAAI 以及 COLM**。此技能覆盖完整的研究生命周期：实验设计、执行、监控、分析、论文写作、评审、修订以及投稿。

这**不是一条线性流水线**——它是一个迭代循环。结果会触发新的实验。评审会触发新的分析。Agent 必须能够处理这些反馈回路。

<!-- ascii-guard-ignore -->
```
┌─────────────────────────────────────────────────────────────┐
│                    RESEARCH PAPER PIPELINE                  │
│                                                             │
│  Phase 0: Project Setup ──► Phase 1: Literature Review      │
│       │                          │                          │
│       ▼                          ▼                          │
│  Phase 2: Experiment     Phase 5: Paper Drafting ◄──┐      │
│       Design                     │                   │      │
│       │                          ▼                   │      │
│       ▼                    Phase 6: Self-Review      │      │
│  Phase 3: Execution &           & Revision ──────────┘      │
│       Monitoring                 │                          │
│       │                          ▼                          │
│       ▼                    Phase 7: Submission               │
│  Phase 4: Analysis ─────► (feeds back to Phase 2 or 5)     │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```
<!-- ascii-guard-ignore-end -->

---

## 何时使用此技能

在以下场景使用此技能：
- **开始一篇新的研究论文**，基于已有代码库或想法
- **设计并运行实验**以支撑论文论断
- **撰写或修订**研究论文的任意章节
- **为投稿某个特定会议或 workshop 做准备**
- **回应评审**，补充额外实验或修订
- **在不同会议格式之间转换**一篇论文
- **撰写非实证类论文**——理论、综述、benchmark 或立场论文（见[实证 ML 之外的论文类型](#paper-types-beyond-empirical-ml)）
- **为** NLP、HCI 或对齐研究**设计人类评估**
- **准备录用后的交付物**——海报、报告、代码发布

## 核心理念

1. **要主动。** 交付完整草稿，而不是抛出问题。科学家很忙——产出他们能反应的具体成果，然后迭代。
2. **绝不臆造引用。** AI 生成的引用错误率约为 40%。务必通过程序化方式获取。无法验证的引用标记为 `[CITATION NEEDED]`。
3. **论文是一个故事，而不是一堆实验的集合。** 每篇论文都需要用一句话陈述一个清晰的贡献。如果你做不到这一点，论文就还没准备好。
4. **实验服务于论断。** 每个实验都必须明确说明它支撑哪条论断。绝不要运行与论文叙事无关的实验。
5. **尽早提交，频繁提交。** 每完成一批实验、每次更新论文草稿——都要用描述性信息提交。Git log 就是实验历史。

### 主动性与协作

**默认：主动。先起草，带着草稿提问。**

| 置信度 | 行动 |
|-----------------|--------|
| **高**（仓库清晰、贡献显而易见） | 写出完整草稿，交付，根据反馈迭代 |
| **中**（存在一些模糊之处） | 带着标记出的不确定点写草稿，继续推进 |
| **低**（存在大量未知） | 通过 `clarify` 提 1-2 个有针对性的问题，然后起草 |

| 章节 | 自主起草？ | 随草稿标记 |
|---------|-------------------|-----------------|
| Abstract | 是 | "将贡献表述为 X——如有需要请调整" |
| Introduction | 是 | "强调了问题 Y——如果不对请修正" |
| Methods | 是 | "包含了细节 A、B、C——请补充缺失部分" |
| Experiments | 是 | "突出了结果 1、2、3——如需请重排" |
| Related Work | 是 | "引用了论文 X、Y、Z——请补充我遗漏的" |

**仅在以下情况阻塞等待输入**：目标会议不明确、存在多个相互矛盾的表述框架、结果看起来不完整、用户明确要求先复核。

---

## Phase 0：项目搭建

**目标**：建立工作区，了解已有工作，识别贡献。

### Step 0.1：探索代码库

```bash
# 了解项目结构
ls -la
find . -name "*.py" | head -30
find . -name "*.md" -o -name "*.txt" | xargs grep -l -i "result\|conclusion\|finding"
```

寻找：
- `README.md` —— 项目概述和论断
- `results/`、`outputs/`、`experiments/` —— 已有发现
- `configs/` —— 实验设置
- `.bib` 文件 —— 已有引用
- 草稿文档或笔记

### Step 0.2：组织工作区

建立一个一致的工作区结构：

```
workspace/
  paper/               # LaTeX 源文件、图表、编译后的 PDF
  experiments/         # 实验运行脚本
  code/                # 核心方法实现
  results/             # 原始实验结果（自动生成）
  tasks/               # 任务/benchmark 定义
  human_eval/          # 人类评估材料（如需要）
```

### Step 0.3：建立版本控制

```bash
git init  # 如果尚未初始化
git remote add origin <repo-url>
git checkout -b paper-draft  # 或 main
```

**Git 纪律**：每完成一批实验都用描述性信息提交。例如：
```
Add Monte Carlo constrained results (5 runs, Sonnet 4.6, policy memo task)
Add Haiku baseline comparison: autoreason vs refinement baselines at cheap model tier
```

### Step 0.4：识别贡献

在写任何东西之前，明确表达：
- **The What（是什么）**：这篇论文贡献的单一事物是什么？
- **The Why（凭什么）**：有什么证据支撑它？
- **The So What（那又怎样）**：读者为什么要在意？

> 向科学家提议：「根据我的理解，主要贡献是：[一句话]。关键结果显示 [Y]。这是你想要的表述框架吗？」

### Step 0.5：创建 TODO 列表

使用 `todo` 工具创建一个结构化的项目计划：

```
Research Paper TODO:
- [ ] 定义一句话贡献
- [ ] 文献综述（相关工作 + baselines）
- [ ] 设计核心实验
- [ ] 运行实验
- [ ] 分析结果
- [ ] 写第一版草稿
- [ ] 自我评审（模拟审稿人）
- [ ] 根据评审修订
- [ ] 投稿准备
```

在整个项目过程中持续更新它。它作为跨会话的持久状态。

### Step 0.6：估算算力预算

在运行实验之前，估算总成本和时间：

```
算力预算清单：
- [ ] API 成本：（每 token 模型价格）×（每次运行估算 token 数）×（运行次数）
- [ ] GPU 小时：（每次实验时间）×（实验数）×（随机种子数）
- [ ] 人类评估成本：（标注员数）×（小时数）×（时薪）
- [ ] 总预算上限和余量（为重跑增加 30-50%）
```

随着实验运行跟踪实际花费：
```python
# 简单的成本跟踪模式
import json, os
from datetime import datetime

COST_LOG = "results/cost_log.jsonl"

def log_cost(experiment: str, model: str, input_tokens: int, output_tokens: int, cost_usd: float):
    entry = {
        "timestamp": datetime.now().isoformat(),
        "experiment": experiment,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": cost_usd,
    }
    with open(COST_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")
```

**预算紧张时**：在投入完整扫描之前先运行试点实验（1-2 个种子，任务子集）。用更便宜的模型调试流水线，然后在最终运行时切换到目标模型。

### Step 0.7：多作者协作

大多数论文有 3-10 位作者。尽早建立工作流：

| 工作流 | 工具 | 何时使用 |
|----------|------|-------------|
| **Overleaf** | 基于浏览器 | 多位作者同时编辑，无 git 经验 |
| **Git + LaTeX** | `git`，配合 `.gitignore` 忽略辅助文件 | 技术团队，需要基于分支的评审 |
| **Overleaf + Git 同步** | Overleaf 高级版 | 两者兼顾——实时协作加版本历史 |

**章节归属**：把每个章节指派给一位主要作者。其他人评论但不直接编辑。这能防止合并冲突和风格不一致。

```
作者协作清单：
- [ ] 就章节归属达成一致（谁写什么）
- [ ] 搭建共享工作区（Overleaf 或 git 仓库）
- [ ] 在任何人开始写之前确立符号约定
- [ ] 安排内部评审轮次（不要只在最后评审）
- [ ] 指定一人做最终排版通查
- [ ] 在制作图表之前就图表风格（颜色、字体、字号）达成一致
```

**应尽早达成一致的 LaTeX 约定**：
- `\method{}` 宏，用于一致的方法命名
- 引用风格：`\citet{}` 与 `\citep{}` 的用法
- 数学符号：向量用小写粗体，矩阵用大写粗体等
- 英式与美式拼写

---

## Phase 1：文献综述

**目标**：找到相关工作，确定 baselines，收集引用。

### Step 1.1：识别种子论文

从代码库中已经被引用的论文开始：

```bash
# 通过 terminal：
grep -r "arxiv\|doi\|cite" --include="*.md" --include="*.bib" --include="*.py"
find . -name "*.bib"
```

### Step 1.2：搜索相关工作

**加载 `arxiv` 技能**进行结构化的论文发现：`skill_view("arxiv")`。它提供 arXiv REST API 搜索、Semantic Scholar 引用图谱、作者档案以及 BibTeX 生成。

用 `web_search` 进行广泛发现，用 `web_extract` 获取特定论文：

```
# 通过 web_search：
web_search("[主要技术] + [应用领域] site:arxiv.org")
web_search("[baseline 方法] comparison ICML NeurIPS 2024")

# 通过 web_extract（针对特定论文）：
web_extract("https://arxiv.org/abs/2303.17651")
```

其他值得尝试的搜索查询：

```
搜索查询：
- "[主要技术] + [应用领域]"
- "[baseline 方法] comparison"
- "[问题名称] state-of-the-art"
- 来自已有引用的作者姓名
```

**推荐**：安装 **Exa MCP** 进行实时学术搜索：
```bash
claude mcp add exa -- npx -y mcp-remote "https://mcp.exa.ai/mcp"
```

### Step 1.2b：深化搜索（先广度，后深度）

一次扁平的搜索（一轮查询）通常会遗漏重要的相关工作。使用一种迭代的**先广度后深度**模式，灵感来自 deep research 流水线：

```
迭代式文献搜索：

第 1 轮（广度）：4-6 个并行查询，覆盖不同角度
  - "[方法] + [领域]"
  - "[问题名称] state-of-the-art 2024 2025"
  - "[baseline 方法] comparison"
  - "[替代方法] vs [你的方法]"
  → 收集论文，提取关键概念和术语

第 2 轮（深度）：基于第 1 轮所学生成后续查询
  - 在第 1 轮论文中发现的新术语
  - 第 1 轮最相关结果所引用的论文
  - 需要调查的矛盾发现
  → 收集论文，识别剩余空白

第 3 轮（定向）：填补特定空白
  - 第 1-2 轮中识别出的缺失 baselines
  - 并行工作（最近 6 个月，相同问题）
  - 关键的负面结果或失败方法
  → 当新查询返回的大多是你已看过的论文时停止
```

**何时停止**：如果某轮返回的论文中 >80% 已在你的收藏中，搜索已饱和。通常 2-3 轮即可。对于综述论文，预计需要 4-5 轮。

**对于基于 agent 的工作流**：通过 `delegate_task` 并行委派每一轮的查询。收集结果，去重，然后基于合并后的所学生成下一轮查询。

### Step 1.3：验证每一条引用

**绝不凭记忆生成 BibTeX。务必通过程序化方式获取。**

对每条引用，遵循强制的 5 步流程：

```
引用验证（每条引用必做）：
1. SEARCH → 用具体关键词查询 Semantic Scholar 或 Exa MCP
2. VERIFY → 在 2+ 个来源中确认论文存在（Semantic Scholar + arXiv/CrossRef）
3. RETRIEVE → 通过 DOI 内容协商获取 BibTeX（程序化获取，而非凭记忆）
4. VALIDATE → 确认你要引用的论断确实出现在该论文中
5. ADD → 将已验证的 BibTeX 加入参考文献库
如果任一步骤失败 → 标记为 [CITATION NEEDED]，并告知科学家
```

```python
# 通过 DOI 获取 BibTeX
import requests

def doi_to_bibtex(doi: str) -> str:
    response = requests.get(
        f"https://doi.org/{doi}",
        headers={"Accept": "application/x-bibtex"}
    )
    response.raise_for_status()
    return response.text
```

如果你无法验证某条引用：

```latex
\cite{PLACEHOLDER_author2024_verify_this}  % TODO: 验证此引用是否存在
```

**务必告诉科学家**：「我已将 [X] 条引用标记为需要验证的占位符。」

完整的 API 文档和完整的 `CitationManager` 类，请见 [references/citation-workflow.md](references/citation-workflow.md)。

### Step 1.4：组织相关工作

按方法论分组论文，而不是逐篇列举：

**好**：「一条研究脉络使用了 X 的假设 [refs]，而我们使用 Y 的假设，因为……」
**坏**：「Smith 等人提出了 X。Jones 等人提出了 Y。我们结合了两者。」

---

## Phase 2：实验设计

**目标**：设计直接支撑论文论断的实验。每个实验都必须回答一个具体问题。

### Step 2.1：把论断映射到实验

建立显式映射：

| 论断 | 实验 | 预期证据 |
|-------|-----------|-------------------|
| "我们的方法优于 baselines" | 主对比（表 1） | 胜率、统计显著性 |
| "效果在较弱模型上更明显" | 模型 scaling 研究 | 单调改进曲线 |
| "收敛需要范围约束" | 受约束 vs 不受约束 | 收敛速率对比 |

**规则**：如果某个实验不映射到任何论断，就不要运行它。

### Step 2.2：设计 Baselines

强基线是把被录用和被拒论文区分开的关键。审稿人会问：「他们有没有和 X 对比？」

标准 baseline 类别：
- **Naive baseline**：最简单的可行方法
- **强 baseline**：已知的最佳已有方法
- **消融 baseline（Ablation baselines）**：你的方法减去某一组件
- **算力匹配 baseline（Compute-matched baselines）**：相同算力预算，不同分配方式

### Step 2.3：定义评估协议

在运行任何东西之前，明确：
- **指标**：你测量什么，方向符号（越高越好/越低越好）
- **聚合**：结果如何跨运行/任务合并
- **统计检验**：哪些检验将确立显著性
- **样本量**：多少次运行/问题/任务

### Step 2.4：编写实验脚本

遵循这些来自成功研究流水线的模式：

**增量保存**——在每一步之后保存结果以支持崩溃恢复：
```python
# 在每个问题/任务之后保存
result_path = f"results/{task}/{strategy}/result.json"
if os.path.exists(result_path):
    continue  # 跳过已完成的工作
# ... 运行实验 ...
with open(result_path, 'w') as f:
    json.dump(result, f, indent=2)
```

**制品保存（Artifact preservation）**——保存所有中间输出：
```
results/<experiment>/
  <task>/
    <strategy>/
      final_output.md          # 最终结果
      history.json             # 完整轨迹
      pass_01/                 # 每轮迭代的制品
        version_a.md
        version_b.md
        critic.md
```

**关注点分离**——把生成、评估和可视化分开：
```
run_experiment.py              # 核心实验运行器
run_baselines.py               # baseline 对比
run_comparison_judge.py        # 盲评
analyze_results.py             # 统计分析
make_charts.py                 # 可视化
```

完整的设计模式、cron 监控和错误恢复，请见 [references/experiment-patterns.md](references/experiment-patterns.md)。

### Step 2.5：设计人类评估（如适用）

许多 NLP、HCI 和对齐论文需要人类评估作为主要或补充证据。请在运行自动化实验之前设计这一环节——人类评估通常有更长的准备周期（IRB 审批、招募标注员）。

**需要人类评估的情况：**
- 自动化指标无法捕捉你在意的内容（流畅度、有用性、安全性）
- 你的贡献是面向人的特质（可读性、偏好、信任）
- NLP 会议（ACL、EMNLP）的审稿人对生成任务会期望有它

**关键设计决策：**

| 决策 | 选项 | 指导 |
|----------|---------|----------|
| **标注员类型** | 专家、众包工人、终端用户 | 与你的论断所需相匹配 |
| **量表** | Likert（1-5）、成对比较、排序 | 对于 LLM 输出，成对比较比 Likert 更可靠 |
| **样本量** | 每位标注员及总条目数 | 功效分析或至少 100 条，3+ 位标注员 |
| **一致性指标** | Cohen's kappa、Krippendorff's alpha、ICC | 多于 2 位标注员用 Krippendorff's alpha；同时报告原始一致性 |
| **平台** | Prolific、MTurk、内部团队 | Prolific 重质量；MTurk 重规模；内部重领域专长 |

**标注指南清单：**
```
- [ ] 清晰的任务描述并附示例（好的和坏的都要）
- [ ] 针对模糊情形的判定标准
- [ ] 每个类别至少 2 个完整示例
- [ ] 注意力检查 / 金标准条目（占总数的 10-15%）
- [ ] 资格任务或筛选轮
- [ ] 每条预估时间及公平报酬（≥ 当地最低工资）
- [ ] 如所在机构要求，进行 IRB/伦理审查
```

**报告要求**（审稿人会检查所有这些）：
- 标注员数量及其资质
- 标注员间一致性，含具体指标和数值
- 报酬细节（金额、估算时薪）
- 标注界面描述或截图（附录）
- 总标注时间

人类评估数据的统计检验、众包质量控制模式以及 IRB 指南等完整内容，请见 [references/human-evaluation.md](references/human-evaluation.md)。

---

## Phase 3：实验执行与监控

**目标**：可靠地运行实验，监控进度，从失败中恢复。

### Step 3.1：启动实验

对长时间运行的实验使用 `nohup`：

```bash
nohup python run_experiment.py --config config.yaml > logs/experiment_01.log 2>&1 &
echo $!  # 记录 PID
```

**并行执行**：同时运行相互独立的实验，但要注意 API 速率限制。同一 API 上 4 个以上并发实验会互相拖慢。

### Step 3.2：建立监控（Cron 模式）

对长时间运行的实验，建立周期性状态检查。cron 提示词应遵循此模板：

```
监控提示词模板：
1. 检查进程是否仍在运行：ps aux | grep <pattern>
2. 读取日志最后 30 行：tail -30 <logfile>
3. 检查已完成的结果：ls <result_dir>
4. 如果结果存在，读取并报告：cat <result_file>
5. 如果全部完成，提交：git add -A && git commit -m "<描述性信息>" && git push
6. 以结构化格式报告（带关键指标的表格）
7. 回答本次实验的关键分析问题
```

**静默模式**：如果自上次检查以来没有任何变化，回复 `[SILENT]` 以抑制对用户的通知。仅在有新闻时报告。

### Step 3.3：处理失败

常见失败模式和恢复方式：

| 失败 | 检测 | 恢复 |
|---------|-----------|----------|
| API 速率限制 / 额度耗尽 | 日志中的 402/429 错误 | 等待，然后重跑（脚本会跳过已完成的工作） |
| 进程崩溃 | PID 消失、结果不完整 | 从最后一个检查点重跑 |
| 困难问题超时 | 进程卡住、日志无进展 | 杀掉并跳过，在结果中记录 |
| 模型 ID 错误 | 引用模型名的报错 | 修正 ID 并重跑 |

**关键**：脚本应始终检查已有结果并跳过已完成的工作。这使重跑安全高效。

### Step 3.4：提交已完成的结果

每批实验完成后：

```bash
git add -A
git commit -m "Add <实验名称>: <一行关键发现>"
git push
```

### Step 3.5：维护实验日志

Git 提交记录了发生了什么，但没有记录**探索树**——即根据所学决定接下来尝试什么的决策。维护一个结构化的实验日志来捕获这棵树：

```json
// experiment_journal.jsonl —— 每次实验尝试追加一条记录
{
  "id": "exp_003",
  "parent": "exp_001",
  "timestamp": "2025-05-10T14:30:00Z",
  "hypothesis": "添加范围约束将修复 exp_001 的收敛失败",
  "plan": "用 max_tokens=2000 和固定结构模板重跑 autoreason",
  "config": {"model": "haiku", "strategy": "autoreason", "max_tokens": 2000},
  "status": "completed",
  "result_path": "results/exp_003/",
  "key_metrics": {"win_rate": 0.85, "convergence_rounds": 3},
  "analysis": "范围约束修复了收敛。胜率从 0.42 跳升到 0.85。",
  "next_steps": ["在 Sonnet 上尝试相同约束", "不带结构模板测试"],
  "figures": ["figures/exp003_convergence.pdf"]
}
```

**为什么用日志而不仅是 git？** Git 跟踪文件变更。日志跟踪推理：你为什么尝试 X、学到了什么、这对下一个实验意味着什么。写论文时，这棵树对 Methods 章节（"我们观察到 X，这促使了 Y"）以及诚实的失败报告而言无价。

**选择最佳路径**：当日志显示一棵分支树（exp_001 → exp_002a、exp_002b、exp_003）时，识别出最能支撑论文论断的路径。把死胡同分支作为消融或负面结果记录在附录中。

**为每次实验快照代码**：每次运行后复制实验脚本：
```bash
cp experiment.py results/exp_003/experiment_snapshot.py
```
这样即使在后续代码变更之后也能精确复现。

---

## Phase 4：结果分析

**目标**：提取发现，计算统计量，识别故事。

### Step 4.1：聚合结果

编写分析脚本来：
1. 从一个批次加载所有结果文件
2. 计算每个任务以及聚合指标
3. 生成汇总表

```python
# 标准分析模式
import json, os
from pathlib import Path

results = {}
for result_file in Path("results/").rglob("result.json"):
    data = json.loads(result_file.read_text())
    strategy = result_file.parent.name
    task = result_file.parent.parent.name
    results.setdefault(strategy, {})[task] = data

# 计算聚合指标
for strategy, tasks in results.items():
    scores = [t["score"] for t in tasks.values()]
    print(f"{strategy}: mean={np.mean(scores):.1f}, std={np.std(scores):.1f}")
```

### Step 4.2：统计显著性

始终计算：
- **误差棒**：标准差或标准误，注明是哪一种
- **置信区间**：关键结果的 95% CI
- **配对检验**：用于比较两种方法的 McNemar 检验
- **效应量**：Cohen's d 或 h，用于实际显著性

McNemar 检验、自助法 CI 和 Cohen's h 的完整实现，请见 [references/experiment-patterns.md](references/experiment-patterns.md)。

### Step 4.3：识别故事

分析之后，明确回答：
1. **主要发现是什么？** 用一句话陈述。
2. **什么让你惊讶？** 出乎意料的结果往往能成就最好的论文。
3. **什么失败了？** 失败的实验可能信息量最大。诚实地报告失败会增强论文。
4. **需要哪些后续实验？** 结果常常引出新问题。

#### 处理负面或无效结果

当你的假设错误或结果不明确时，你有三种选择：

| 情境 | 行动 | 适合的会议 |
|-----------|--------|-----------|
| 假设错误，但**为什么**很有信息量 | 把论文围绕「为什么」的分析来组织 | NeurIPS、ICML（如果分析严谨） |
| 方法没超过 baselines，但**揭示了新东西** | 把贡献重新表述为理解/分析 | ICLR（看重理解）、workshop 论文 |
| 对流行论断的干净负面结果 | 写出来——该领域需要知道 | NeurIPS Datasets & Benchmarks、TMLR、workshops |
| 结果不明确，没有清晰故事 | 转向——运行不同的实验或重新表述 | 不要硬凑一篇并不存在的论文 |

**如何写负面结果论文：**
- 以社区相信什么、以及为什么检验它很重要作为开头
- 描述你严谨的方法论（必须无懈可击——审稿人会更严格地审视）
- 清晰地呈现带有统计证据的无效结果
- 分析**为什么**预期结果没有出现
- 讨论对领域的影响

**明确欢迎负面结果的会议**：NeurIPS（Datasets & Benchmarks track）、TMLR、ML Reproducibility Challenge、主要会议的 workshops。一些 workshop 专门征集负面结果。

### Step 4.4：制作图表

**图**：
- 所有图表使用矢量图（PDF）：`plt.savefig('fig.pdf')`
- 色盲友好调色板（Okabe-Ito 或 Paul Tol）
- 自包含的图注——读者应能在不看正文的情况下理解
- 图内不要标题——图注承担此功能

**表**：
- 使用 `booktabs` LaTeX 宏包
- 每个指标的最佳值加粗
- 包含方向符号（越高越好/越低越好）
- 一致的小数精度

```latex
\usepackage{booktabs}
\begin{tabular}{lcc}
\toprule
Method & Accuracy $\uparrow$ & Latency $\downarrow$ \\
\midrule
Baseline & 85.2 & 45ms \\
\textbf{Ours} & \textbf{92.1} & 38ms \\
\bottomrule
\end{tabular}
```

### Step 4.5：决策：更多实验还是开始写作？

| 情境 | 行动 |
|-----------|--------|
| 核心论断已被支撑，结果显著 | 进入 Phase 5（写作） |
| 结果不明确，需要更多数据 | 回到 Phase 2（设计） |
| 意外发现提示了新方向 | 回到 Phase 2（设计） |
| 缺一个审稿人会问的消融 | 运行它，然后 Phase 5 |
| 所有实验已完成但部分失败 | 记录失败，进入 Phase 5 |

### Step 4.6：编写实验日志（通向写作的桥梁）

在进入论文写作之前，创建一份结构化的实验日志，把结果与正文衔接起来。这是实验与写作之间最重要的连接组织——没有它，写作 agent 就不得不从原始结果文件中重新推导故事。

**创建 `experiment_log.md`**，结构如下：

```markdown
# Experiment Log

## Contribution（一句话）
[论文的主要论断]

## 已运行的实验

### 实验 1：[名称]
- **检验的论断**：[这支撑了哪条论文论断]
- **设置**：[模型、数据集、配置、运行次数]
- **关键结果**：[带数字的一句话]
- **结果文件**：results/exp1/final_info.json
- **生成的图**：figures/exp1_comparison.pdf
- **意外发现**：[任何出乎意料的内容]

### 实验 2：[名称]
...

## 图表
| 文件名 | 描述 | 应放在哪个章节 |
|----------|-------------|---------------------------|
| figures/main_comparison.pdf | 在 benchmark X 上对比所有方法的柱状图 | Results, Figure 2 |
| figures/ablation.pdf | 移除组件 A、B、C 的消融 | Results, Figure 3 |
...

## 失败的实验（为诚实而记录）
- [尝试了什么，为什么失败，它告诉我们什么]

## 开放问题
- [结果提出的、论文应予以回应的任何内容]
```

**为什么这很重要**：起草时，agent（或被委派的子 agent）可以与 LaTeX 模板一起加载 `experiment_log.md`，并基于实际结果产出第一版草稿。没有这个桥梁，写作 agent 必须解析原始 JSON/CSV 文件并推断故事——这是臆造或误报数字的常见来源。

**Git 纪律**：与它所描述的结果一起提交此日志。

---

## 迭代精修：策略选择

此流水线中的任何输出——论文草稿、实验脚本、分析——都可以迭代精修。autoreason 研究为每种精修策略何时有效、何时失败提供了实证证据。使用本节来选择正确的方法。

### 快速决策表

| 你的情境 | 策略 | 原因 |
|---------------|----------|-----|
| 中档模型 + 受约束任务 | **Autoreason** | 最佳击球点。生成-评估差距最大。Baselines 会主动破坏弱模型的输出。 |
| 中档模型 + 开放任务 | **Autoreason** 并添加范围约束 | 加入固定事实、结构或交付物以限定改进空间。 |
| 前沿模型 + 受约束任务 | **Autoreason** | 即便在前沿也赢下 2/3 的受约束任务。 |
| 前沿模型 + 不受约束任务 | **Critique-and-revise** 或 **single pass** | Autoreason 排在最后。模型自我评估已经足够好。 |
| 具体技术任务（系统设计） | **Critique-and-revise** | 直接的「找出-修复」循环更高效。 |
| 模板填充任务（只有一种正确结构） | **Single pass** 或 **conservative** | 决策空间最小。迭代无价值。 |
| 带测试用例的代码 | **Autoreason（代码变体）** | 对*为什么*失败进行结构化分析后再修复。恢复率 62% vs 43%。 |
| 非常弱的模型（Llama 8B 级别） | **Single pass** | 模型太弱，无法生成多样化候选。 invest 在生成质量上。 |

### 生成-评估差距（The Generation-Evaluation Gap）

**核心洞察**：Autoreason 的价值取决于一个模型的生成能力与其自我评估能力之间的差距。

```
模型层级          │ 生成       │ 自我评估   │ 差距   │ Autoreason 价值
──────────────────┼────────────┼───────────┼────────┼─────────────────
弱（Llama 8B）    │ 差         │ 差         │ 小     │ 无——无法生成多样化候选
中（Haiku 3.5）   │ 尚可       │ 差         │ 大     │ 最大——42/42 Borda 满分
中（Gemini Flash）│ 尚可       │ 中等       │ 大     │ 高——赢下 2/3
强（Sonnet 4）    │ 好         │ 尚可       │ 中     │ 中等——赢下 3/5
前沿（S4.6）      │ 优秀       │ 好         │ 小     │ 仅在受约束时
```

这种差距是结构性的，而非暂时性的。随着成本下降，今天的前沿会变成明天的中档。最佳击球点会移动但永不消失。

### Autoreason 循环（概要）

每一轮从全新、隔离的 agent 产出三个候选：

1. **Critic（评论员）** → 在现任者 A 中找问题（不修复）
2. **Author B（作者 B）** → 根据评论修订 A
3. **Synthesizer（综合者）** → 合并 A 和 B（标签随机化）
4. **Judge Panel（评审团）** → 3 位盲 CoT 评审通过 Borda 计数对 A、B、AB 排序
5. **收敛** → A 连续赢 k=2 轮 → 完成

**关键参数：**
- k=2 收敛（k=1 过早，k=3 太贵且无质量提升）
- 始终使用 CoT 评审（收敛快 3 倍）
- 作者温度 0.8，评审温度 0.3
- 保守的平局打破：平局时现任者胜
- 每个角色都是没有共享上下文的全新 agent

### 应用于论文草稿

当通过 autoreason 精修论文本身时：
- **向评论员提供 ground truth**：实际实验数据、结果 JSON、统计输出。否则模型会臆造虚假的消融研究和假置信区间。
- **至少使用 3 个工作中的评审**：一个坏的评审解析器不会只增加噪声——它会彻底阻止均衡。
- **对修订施加范围约束**：「解决这些具体弱点」而不是「改进论文」。

### 失败模式

| 失败 | 检测 | 修复 |
|---------|-----------|-----|
| 不收敛（A 从不获胜） | A 在 20+ 轮中胜率 <15% | 给任务添加范围约束 |
| 合成漂移 | 词数无界增长 | 约束结构和交付物 |
| 退化低于 single pass | Baselines 得分高于迭代输出 | 切换到 single pass；模型可能太弱 |
| 过拟合（代码） | 公开测试通过率高、私有测试通过率低 | 使用结构化分析，而非仅测试反馈 |
| 评审损坏 | 解析失败使评审团低于 3 | 先修复解析器再继续 |

完整的提示词、Borda 评分细节、模型选择指南、范围约束设计模式以及算力预算参考，请见 [references/autoreason-methodology.md](references/autoreason-methodology.md)。

---

## Phase 5：论文起草

**目标**：写出一篇完整的、可发表级别的论文。

### 大型项目的上下文管理

一个有 50+ 个实验文件、多个结果目录和大量文献笔记的论文项目，很容易超出 agent 的上下文窗口。主动管理这一点：

**每个起草任务应载入上下文的内容：**

| 起草任务 | 载入上下文 | 不要载入 |
|---------------|------------------|-------------|
| 写 Introduction | `experiment_log.md`、贡献陈述、5-10 篇最相关论文的摘要 | 原始结果 JSON、完整实验脚本、所有文献笔记 |
| 写 Methods | 实验配置、伪代码、架构描述 | 原始日志、其他实验的结果 |
| 写 Results | `experiment_log.md`、结果汇总表、图清单 | 完整分析脚本、中间数据 |
| 写 Related Work | 组织好的引用笔记（Step 1.4 输出）、.bib 文件 | 实验文件、原始 PDF |
| 修订轮 | 完整论文草稿、具体审稿人关切 | 其他一切 |

**原则：**
- **`experiment_log.md` 是主要的上下文桥梁**——它总结了写作所需的一切，无需载入原始数据文件（见 Step 4.6）
- **委派时一次只载入一个章节的上下文**。一个起草 Methods 的子 agent 不需要文献综述笔记。
- **总结，而非包含原始文件。** 对于一个 200 行的结果 JSON，载入 10 行的汇总表。对于一篇 50 页的相关论文，载入 5 句的摘要 + 你关于其相关性的 2 行笔记。
- **对于非常大的项目**：创建一个 `context/` 目录，存放预压缩的摘要：
  ```
  context/
    contribution.md          # 1 句话
    experiment_summary.md    # 关键结果表（来自 experiment_log.md）
    literature_map.md        # 组织好的引用笔记
    figure_inventory.md      # 带描述的图清单
  ```

### 叙事原则

**最关键的单一洞察**：你的论文不是一堆实验的集合——它是一个故事，有一条清晰的贡献，并由证据支撑。

每篇成功的 ML 论文都围绕 Neel Nanda 所称的「叙事」：一个简短、严谨、基于证据的技术故事，带一个读者会在意的收获。

**三大支柱（到引言结尾时必须非常清晰）：**

| 支柱 | 描述 | 测试 |
|--------|-------------|------|
| **The What** | 1-3 条具体的新颖论断 | 你能用一句话陈述它们吗？ |
| **The Why** | 严谨的实证证据 | 实验能否把你的假设与替代方案区分开？ |
| **The So What** | 读者为什么要在意 | 这是否连接到一个被认可社区问题？ |

**如果你无法用一句话陈述你的贡献，你就还没有一篇论文。**

### 本指南背后的来源

此技能综合了在顶级会议上有丰富发表经验的研究者的写作理念。写作理念层最初由 [Orchestra Research](https://github.com/orchestra-research) 作为 `ml-paper-writing` 技能编译。

| 来源 | 关键贡献 | 链接 |
|--------|-----------------|------|
| **Neel Nanda**（Google DeepMind） | 叙事原则、What/Why/So What 框架 | [How to Write ML Papers](https://www.alignmentforum.org/posts/eJGptPbbFPZGLpjsp/highly-opinionated-advice-on-how-to-write-ml-papers) |
| **Sebastian Farquhar**（DeepMind） | 5 句摘要公式 | [How to Write ML Papers](https://sebastianfarquhar.com/on-research/2024/11/04/how_to_write_ml_papers/) |
| **Gopen & Swan** | 读者期望的 7 条原则 | [Science of Scientific Writing](https://cseweb.ucsd.edu/~swanson/papers/science-of-writing.pdf) |
| **Zachary Lipton** | 用词、消除对冲 | [Heuristics for Scientific Writing](https://www.approximatelycorrect.com/2018/01/29/heuristics-technical-scientific-writing-machine-learning-perspective/) |
| **Jacob Steinhardt**（UC Berkeley） | 精确性、一致的术语 | [Writing Tips](https://bounded-regret.ghost.io/) |
| **Ethan Perez**（Anthropic） | 微观层面的清晰度技巧 | [Easy Paper Writing Tips](https://ethanperez.net/easy-paper-writing-tips/) |
| **Andrej Karpathy** | 单一贡献聚焦 | 各类讲座 |

**如需深入了解其中任何一项，见：**
- [references/writing-guide.md](references/writing-guide.md) —— 带示例的完整解释
- [references/sources.md](references/sources.md) —— 完整参考文献

### 时间分配

在以下每一项上花费大致**相等的时间**：
1. 摘要
2. 引言
3. 图表
4. 其余所有加在一起

**为什么？** 大多数审稿人在到达你的方法之前就已经形成判断。读者按以下顺序接触你的论文：标题 → 摘要 → 引言 → 图表 → 也许其余部分。

### 写作工作流

```
论文写作清单：
- [ ] Step 1：定义一句话贡献
- [ ] Step 2：起草图 1（核心想法或最有说服力的结果）
- [ ] Step 3：起草摘要（5 句公式）
- [ ] Step 4：起草引言（最多 1-1.5 页）
- [ ] Step 5：起草方法
- [ ] Step 6：起草实验与结果
- [ ] Step 7：起草相关工作
- [ ] Step 8：起草结论与讨论
- [ ] Step 9：起草局限性（所有会议都要求）
- [ ] Step 10：规划附录（证明、额外实验、细节）
- [ ] Step 11：完成论文清单
- [ ] Step 12：最终复核
```

### 两轮精修模式

用 AI agent 起草时，使用**两轮**方法（在 SakanaAI 的 AI-Scientist 流水线中被证明有效）：

**第 1 轮 —— 写 + 每节即时精修：**
对每一节，写出完整草稿，然后在同一上下文中立即精修。这能在该节还新鲜时捕获局部问题（清晰度、流畅性、完整性）。

**第 2 轮 —— 带完整论文上下文的全局精修：**
所有章节起草完成后，带着对完整论文的认知重新审视每一节。这能捕获跨章节问题：冗余、不一致的术语、叙事流畅性，以及某一节承诺了但另一节没交付的空白。

```
第二轮精修提示词（每节）：
"在完整论文的上下文中复核 [SECTION]。
- 它与论文其余部分契合吗？与其他章节是否有冗余？
- 术语是否与 Introduction 和 Methods 一致？
- 有什么可以在不削弱信息的情况下删掉吗？
- 叙事是否从上一节流入、并向下一节流出？
做最小、有针对性的编辑。不要从零重写。"
```

### LaTeX 错误清单

把此清单附加到每个精修提示词之后。这些是 LLM 写 LaTeX 时最常见的错误：

```
LaTeX 质量清单（每次编辑后核对）：
- [ ] 没有未闭合的数学符号（$ 符号配对）
- [ ] 只引用存在的图/表（\ref 与 \label 匹配）
- [ ] 没有臆造的引用（\cite 与 .bib 中的条目匹配）
- [ ] 每个 \begin{env} 都有匹配的 \end{env}（尤其是 figure、table、algorithm）
- [ ] 没有 HTML 污染（</end{figure}> 而不是 \end{figure}）
- [ ] 数学模式之外没有未转义的下划线（文本中用 \_）
- [ ] 没有重复的 \label 定义
- [ ] 没有重复的章节标题
- [ ] 正文中的数字与实际实验结果匹配
- [ ] 所有图都有图注和标签
- [ ] 没有会导致 overfull hbox 警告的过长行
```

### Step 5.0：标题

标题是论文中被阅读最多的单一元素。它决定是否有人会点进去看摘要。

**好标题**：
- 陈述贡献或发现："Autoreason: When Iterative LLM Refinement Works and Why It Fails"
- 突出惊人结果："Scaling Data-Constrained Language Models"（暗示你能做到）
- 命名方法 + 它做什么："DPO: Direct Preference Optimization of Language Models"

**坏标题**：
- 太泛："An Approach to Improving Language Model Outputs"
- 太长：超过约 15 个词的任何标题
- 只有行话："Asymptotic Convergence of Iterative Stochastic Policy Refinement"（这是给谁看的？）

**规则**：
- 如果有方法名就包含进去（便于引用）
- 包含 1-2 个审稿人会搜索的关键词
- 避免冒号，除非两半都承载含义
- 测试：审稿人能否仅从标题就知道领域和贡献？

### Step 5.1：摘要（5 句公式）

来自 Sebastian Farquhar（DeepMind）：

```
1. 你取得了什么："We introduce...", "We prove...", "We demonstrate..."
2. 为什么这既难又重要
3. 你怎么做（带上专业关键词以便被发现）
4. 你有什么证据
5. 你最 remarkable 的数字/结果
```

**删除**诸如 "Large language models have achieved remarkable success..." 之类的通用开头。

### Step 5.2：图 1

图 1 是大多数读者（在摘要之后）看的第二样东西。在写引言之前先起草它——它迫使你澄清核心想法。

| 图 1 类型 | 何时使用 | 示例 |
|---------------|-------------|---------|
| **方法图** | 新架构或流水线 | 展示你系统的 TikZ 流程图 |
| **结果预告** | 一个有说服力的结果讲完整个故事 | 柱状图："我们 vs baselines"，差距明显 |
| **问题图示** | 问题不直观 | 展示你修复的失败模式的前/后对比 |
| **概念图** | 抽象贡献需要视觉支撑 | 方法属性的 2x2 矩阵 |

**规则**：图 1 必须在不读任何文字的情况下可理解。仅图注就应传达核心想法。有目的地使用颜色——不要只是装饰。

### Step 5.3：引言（最多 1-1.5 页）

必须包含：
- 清晰的问题陈述
- 简短的方法概述
- 2-4 条要点贡献列表（双栏格式下每条最多 1-2 行）
- Methods 应该从第 2-3 页开始

### Step 5.4：方法

使重新实现成为可能：
- 概念大纲或伪代码
- 列出所有超参数
- 足以复现的架构细节
- 呈现最终设计决策；消融放到实验里

### Step 5.5：实验与结果

对每个实验，明确陈述：
- **它支撑哪条论断**
- 它如何与主要贡献相连
- 要观察什么："蓝线显示 X，这证明了 Y"

要求：
- 带方法论的误差棒（标准差 vs 标准误）
- 超参搜索范围
- 算力基础设施（GPU 型号、总小时数）
- 设置种子的方法

### Step 5.6：相关工作

按方法论组织，而不是逐篇。慷慨引用——审稿人很可能是相关论文的作者。

### Step 5.7：局限性（必需）

所有主要会议都要求这一节。诚实有帮助：
- 审稿人被指示不要因为诚实承认局限而扣分
- 通过率先识别弱点来预先应对批评
- 解释为什么局限性不会削弱核心论断

### Step 5.8：结论与讨论

**结论**（必需，0.5-1 页）：
- 用一句话重述贡献（与摘要不同的措辞）
- 总结关键发现（2-3 句，而不是清单）
- 启示：这对领域意味着什么？
- 未来工作：2-3 个具体的下一步（而不是含糊的「我们把 X 留给未来工作」）

**讨论**（可选，有时与结论合并）：
- 超出直接结果的更广泛启示
- 与其他子领域的联系
- 方法何时有效、何时无效的诚实评估
- 实际部署考量

**不要**在结论中引入新结果或论断。

### Step 5.9：附录策略

附录在所有主要会议中都不限页数，且对可复现性至关重要。结构：

| 附录章节 | 放什么 |
|-----------------|---------------|
| **证明与推导** | 对正文而言太长的完整证明。正文可以陈述定理并标注「证明见附录 A」。 |
| **额外实验** | 消融、scaling 曲线、按数据集分解、超参敏感性 |
| **实现细节** | 完整超参表、训练细节、硬件规格、随机种子 |
| **数据集文档** | 数据收集过程、标注指南、许可证、预处理 |
| **提示词与模板** | 使用的确切提示词（对于基于 LLM 的方法）、评估模板 |
| **人类评估** | 标注界面截图、给标注员的说明、IRB 细节 |
| **额外图表** | 按任务分解、轨迹可视化、失败案例示例 |

**规则**：
- 正文必须自包含——审稿人没有被要求阅读附录
- 永远不要把关键证据只放在附录
- 交叉引用：「完整结果见表 5（附录 B）」而不仅仅是「见附录」
- 使用 `\appendix` 命令，然后 `\section{A: Proofs}` 等

### 页数预算管理

当超过页数限制时：

| 削减策略 | 节省 | 风险 |
|-------------|-------|------|
| 把证明移到附录 | 0.5-2 页 | 低——标准做法 |
| 压缩相关工作 | 0.5-1 页 | 中——可能遗漏关键引用 |
| 把表格与子图合并 | 0.25-0.5 页 | 低——常常提升可读性 |
| 谨慎使用 `\vspace{-Xpt}` | 0.1-0.3 页 | 不显眼则低，明显则高 |
| 删除定性示例 | 0.5-1 页 | 中——审稿人喜欢示例 |
| 缩小图尺寸 | 0.25-0.5 页 | 高——图必须保持可读 |

**不要**：缩小字号、改边距、删除必需章节（局限性、更广泛影响），或对正文使用 `\small`/`\footnotesize`。

### Step 5.10：伦理与更广泛影响声明

大多数会议现在要求或强烈鼓励一份伦理/更广泛影响声明。这不是套话——审稿人会读它，并可能标记会触发桌面拒稿的伦理关切。

**应包含的内容：**

| 组件 | 内容 | 要求方 |
|-----------|---------|-------------|
| **积极的社会影响** | 你的工作如何造福社会 | NeurIPS、ICML |
| **潜在的负面影响** | 滥用风险、双重用途关切、失败模式 | NeurIPS、ICML |
| **公平性与偏见** | 你的方法/数据是否有已知偏见？ | 所有会议（隐式） |
| **环境影响** | 大规模训练的算力碳足迹 | ICML，越来越多 NeurIPS |
| **隐私** | 你的工作是否使用或促成对个人数据的处理？ | ACL、NeurIPS |
| **LLM 披露** | 写作或实验中是否使用了 AI？ | ICLR（强制）、ACL |

**撰写声明：**

```latex
\section*{Broader Impact Statement}
% NeurIPS/ICML：放在结论之后，不计入页数限制

% 1. 积极应用（1-2 句）
This work enables [具体应用] which may benefit [具体群体].

% 2. 风险与缓解（1-3 句，要具体）
[Method/model] could potentially be misused for [具体风险]. We mitigate
this by [具体缓解措施，例如：仅发布大于尺寸 X 的模型权重、
加入安全过滤器、记录失败模式].

% 3. 影响声明的局限（1 句）
Our evaluation is limited to [具体领域]；更广泛的部署将
需要 [具体的额外工作].
```

**常见错误：**
- 写「我们预见没有负面影响」（几乎从不是真的——审稿人会不信任这种说法）
- 含糊：「这可能被滥用」而不具体说明如何
- 忽略大规模工作的算力成本
- 在要求披露 LLM 使用的会议上忘记披露

**算力碳足迹**（针对训练密集型论文）：
```python
# 使用 ML CO2 Impact 工具方法学估算
gpu_hours = 1000  # 总 GPU 小时
gpu_tdp_watts = 400  # 例如 A100 = 400W
pue = 1.1  # Power Usage Effectiveness（数据中心开销）
carbon_intensity = 0.429  # kg CO2/kWh（美国平均值；因地区而异）

energy_kwh = (gpu_hours * gpu_tdp_watts * pue) / 1000
carbon_kg = energy_kwh * carbon_intensity
print(f"Energy: {energy_kwh:.0f} kWh, Carbon: {carbon_kg:.0f} kg CO2eq")
```

### Step 5.11：Datasheets 与 Model Cards（如适用）

如果你的论文引入了**新数据集**或**发布了模型**，请包含结构化文档。审稿人越来越期望看到它，NeurIPS Datasets & Benchmarks track 要求它。

**Datasheets for Datasets**（Gebru 等，2021）——包含在附录中：

```
数据集文档（附录）：
- 动机：为什么创建这个数据集？它支持什么任务？
- 构成：实例是什么？有多少？什么数据类型？
- 收集：数据如何收集？来源是什么？
- 预处理：应用了什么清洗/过滤？
- 分发：数据集如何分发？依据什么许可证？
- 维护：谁维护它？如何报告问题？
- 伦理考量：含个人数据吗？获得同意了吗？
  潜在危害？已知偏见？
```

**Model Cards**（Mitchell 等，2019）——模型发布时包含在附录中：

```
Model Card（附录）：
- 模型细节：架构、训练数据、训练流程
- 预期用途：主要用例、范围外用途
- 指标：评估指标及在 benchmark 上的结果
- 伦理考量：已知偏见、公平性评估
- 局限性：已知失败模式、模型表现不佳的领域
```

### 写作风格

**句子层面的清晰度（Gopen & Swan 的 7 条原则）：**

| 原则 | 规则 |
|-----------|------|
| 主谓邻近 | 让主语和动词靠近 |
| 重音位置 | 把强调放在句末 |
| 主题位置 | 先上下文，后新信息 |
| 旧在前新在后 | 熟悉信息 → 不熟悉信息 |
| 一个单元一个功能 | 每段讲一个要点 |
| 动作用动词 | 使用动词，而非名词化 |
| 先上下文后新内容 | 呈现之前先铺陈 |

**用词（Lipton、Steinhardt）：**
- 要具体："accuracy" 而不是 "performance"
- 消除对冲：除非真不确定，否则去掉 "may"
- 全文术语一致
- 避免增量式词汇："develop"，而不是 "combine"

**带示例的完整写作指南**：见 [references/writing-guide.md](references/writing-guide.md)

### 使用 LaTeX 模板

**务必先复制整个模板目录，然后在其中写作。**

```
模板设置清单：
- [ ] Step 1：把整个模板目录复制到新项目
- [ ] Step 2：验证模板按原样能编译（在做任何更改之前）
- [ ] Step 3：阅读模板的示例内容以了解结构
- [ ] Step 4：逐节替换示例内容
- [ ] Step 5：使用模板宏（检查导言区是否有 \newcommand 定义）
- [ ] Step 6：只在最后清理模板残留物
```

**Step 1：复制完整模板**

```bash
cp -r templates/neurips2025/ ~/papers/my-paper/
cd ~/papers/my-paper/
ls -la  # 应看到：main.tex, neurips.sty, Makefile 等
```

复制整个目录，而不仅是 .tex 文件。模板包含样式文件（.sty）、参考文献样式（.bst）、示例内容和 Makefile。

**Step 2：先验证模板能编译**

在做任何更改之前：
```bash
latexmk -pdf main.tex
# 或手动：pdflatex main.tex && bibtex main && pdflatex main.tex && pdflatex main.tex
```

如果未修改的模板无法编译，先修复它（通常是缺少 TeX 宏包——通过 `tlmgr install <package>` 安装）。

**Step 3：保留模板内容作为参考**

不要立即删除示例内容。把它注释掉，用作排版参考：
```latex
% 模板示例（保留作参考）：
% \begin{figure}[t]
%   \centering
%   \includegraphics[width=0.8\linewidth]{example-image}
%   \caption{Template shows caption style}
% \end{figure}

% 你的实际图：
\begin{figure}[t]
  \centering
  \includegraphics[width=0.8\linewidth]{your-figure.pdf}
  \caption{Your caption following the same style.}
\end{figure}
```

**Step 4：逐节替换内容**

系统地推进：标题/作者 → 摘要 → 引言 → 方法 → 实验 → 相关工作 → 结论 → 参考文献 → 附录。每节后编译。

**Step 5：使用模板宏**

```latex
\newcommand{\method}{YourMethodName}  % 一致的方法命名
\newcommand{\eg}{e.g.,\xspace}        % 正确的缩写
\newcommand{\ie}{i.e.,\xspace}
```

### 模板陷阱

| 陷阱 | 问题 | 解决方案 |
|---------|---------|----------|
| 只复制 `.tex` 文件 | 缺少 `.sty`，无法编译 | 复制整个目录 |
| 修改 `.sty` 文件 | 破坏会议排版 | 永远不要编辑样式文件 |
| 添加随机宏包 | 冲突，破坏模板 | 仅在必要时添加 |
| 过早删除模板内容 | 失去排版参考 | 保留为注释直到完成 |
| 不频繁编译 | 错误累积 | 每节后编译 |
| 图使用栅格 PNG | 论文中模糊 | 始终通过 `savefig('fig.pdf')` 使用矢量 PDF |

### 快速模板参考

| 会议 | 主文件 | 样式文件 | 页数限制 |
|------------|-----------|------------|------------|
| NeurIPS 2025 | `main.tex` | `neurips.sty` | 9 页 |
| ICML 2026 | `example_paper.tex` | `icml2026.sty` | 8 页 |
| ICLR 2026 | `iclr2026_conference.tex` | `iclr2026_conference.sty` | 9 页 |
| ACL 2025 | `acl_latex.tex` | `acl.sty` | 8 页（长文） |
| AAAI 2026 | `aaai2026-unified-template.tex` | `aaai2026.sty` | 7 页 |
| COLM 2025 | `colm2025_conference.tex` | `colm2025_conference.sty` | 9 页 |

**通用**：双盲、参考文献不计页数、附录不限、要求 LaTeX。

模板在 `templates/` 目录下。编译设置（VS Code、CLI、Overleaf、其他 IDE）见 [templates/README.md](templates/README.md)。

### 表格与图

**表格** —— 使用 `booktabs` 获得专业排版：

```latex
\usepackage{booktabs}
\begin{tabular}{lcc}
\toprule
Method & Accuracy $\uparrow$ & Latency $\downarrow$ \\
\midrule
Baseline & 85.2 & 45ms \\
\textbf{Ours} & \textbf{92.1} & 38ms \\
\bottomrule
\end{tabular}
```

规则：
- 每个指标的最佳值加粗
- 包含方向符号（$\uparrow$ 越高越好，$\downarrow$ 越低越好）
- 数字列右对齐
- 一致的小数精度

**图**：
- **矢量图**（PDF、EPS）用于所有图和示意图 —— `plt.savefig('fig.pdf')`
- **栅格图**（PNG 600 DPI）仅用于照片
- **色盲友好调色板**（Okabe-Ito 或 Paul Tol）
- 验证**灰度可读性**（8% 的男性有色彩视觉缺陷）
- **图内不要标题** —— 图注承担此功能
- **自包含的图注** —— 读者应能在不看正文的情况下理解

### 会议再投稿

不同会议之间的转换，见 Phase 7（投稿准备）——它覆盖了完整的转换工作流、页数变更表以及被拒后的指导。

### 专业 LaTeX 导言区

把以下宏包添加到任何论文以获得专业质量。它们与所有主要会议的样式文件兼容：

```latex
% --- 专业宏包（在会议样式文件之后添加） ---

% 排版
\usepackage{microtype}              % 微排版改进（突出、扩展）
                                     % 使文本明显更精致——始终包含

% 表格
\usepackage{booktabs}               % 专业表格横线（\toprule, \midrule, \bottomrule）
\usepackage{siunitx}                % 一致的数字排版、小数对齐
                                     % 用法：\num{12345} → 12,345；\SI{3.5}{GHz} → 3.5 GHz
                                     % 表格对齐：S 列类型用于小数对齐的数字

% 图
\usepackage{graphicx}               % 包含图形（\includegraphics）
\usepackage{subcaption}             % 带 (a), (b), (c) 标签的子图
                                     % 用法：\begin{subfigure}{0.48\textwidth} ... \end{subfigure}

% 图与算法
\usepackage{tikz}                   % 可编程矢量图
\usetikzlibrary{arrows.meta, positioning, shapes.geometric, calc, fit, backgrounds}
\usepackage[ruled,vlined]{algorithm2e}  % 专业伪代码
                                     % 替代：如果模板捆绑了 \usepackage{algorithmicx}

% 交叉引用
\usepackage{cleveref}               % 智能引用：\cref{fig:x} → "Figure 1"
                                     % 必须在 hyperref 之后加载
                                     % 处理：图、表、章节、公式、算法

% 数学（通常由会议 .sty 包含，但要核实）
\usepackage{amsmath,amssymb}        % AMS 数学环境和符号
\usepackage{mathtools}              % 扩展 amsmath（dcases, coloneqq 等）

% 颜色（用于图和示意图）
\usepackage{xcolor}                 % 颜色管理
% Okabe-Ito 色盲友好调色板：
\definecolor{okblue}{HTML}{0072B2}
\definecolor{okorange}{HTML}{E69F00}
\definecolor{okgreen}{HTML}{009E73}
\definecolor{okred}{HTML}{D55E00}
\definecolor{okpurple}{HTML}{CC79A7}
\definecolor{okcyan}{HTML}{56B4E9}
\definecolor{okyellow}{HTML}{F0E442}
```

**注意：**
- `microtype` 是对视觉质量影响最大的单一宏包。它在亚像素级别调整字符间距。始终包含它。
- `siunitx` 通过 `S` 列类型处理表格中的小数对齐——消除手动间距。
- `cleveref` 必须在 `hyperref` 之后加载。大多数会议 .sty 会加载 hyperref，所以把 cleveref 放在最后。
- 检查会议模板是否已经加载了其中任何宏包（尤其是 `algorithm`、`amsmath`、`graphicx`）。不要重复加载。

### siunitx 表格对齐

`siunitx` 使数字密集的表格显著更易读：

```latex
\begin{tabular}{l S[table-format=2.1] S[table-format=2.1] S[table-format=2.1]}
\toprule
Method & {Accuracy $\uparrow$} & {F1 $\uparrow$} & {Latency (ms) $\downarrow$} \\
\midrule
Baseline         & 85.2  & 83.7  & 45.3 \\
Ablation (no X)  & 87.1  & 85.4  & 42.1 \\
\textbf{Ours}    & \textbf{92.1} & \textbf{90.8} & \textbf{38.7} \\
\bottomrule
\end{tabular}
```

`S` 列类型自动按小数点对齐。`{}` 中的标题 escape 该对齐。

### 子图

并排图的标准模式：

```latex
\begin{figure}[t]
  \centering
  \begin{subfigure}[b]{0.48\textwidth}
    \centering
    \includegraphics[width=\textwidth]{fig_results_a.pdf}
    \caption{Results on Dataset A.}
    \label{fig:results-a}
  \end{subfigure}
  \hfill
  \begin{subfigure}[b]{0.48\textwidth}
    \centering
    \includegraphics[width=\textwidth]{fig_results_b.pdf}
    \caption{Results on Dataset B.}
    \label{fig:results-b}
  \end{subfigure}
  \caption{Comparison of our method across two datasets. (a) shows the scaling
  behavior and (b) shows the ablation results. Both use 5 random seeds.}
  \label{fig:results}
\end{figure}
```

使用 `\cref{fig:results}` → "Figure 1"，`\cref{fig:results-a}` → "Figure 1a"。

### 用 algorithm2e 写伪代码

```latex
\begin{algorithm}[t]
\caption{Iterative Refinement with Judge Panel}
\label{alg:method}
\KwIn{Task $T$, model $M$, judges $J_1 \ldots J_n$, convergence threshold $k$}
\KwOut{Final output $A^*$}
$A \gets M(T)$ \tcp*{初始生成}
$\text{streak} \gets 0$\;
\While{$\text{streak} < k$}{
  $C \gets \text{Critic}(A, T)$ \tcp*{识别弱点}
  $B \gets M(T, C)$ \tcp*{回应评论的修订版本}
  $AB \gets \text{Synthesize}(A, B)$ \tcp*{合并最佳元素}
  \ForEach{judge $J_i$}{
    $\text{rank}_i \gets J_i(\text{shuffle}(A, B, AB))$ \tcp*{盲排序}
  }
  $\text{winner} \gets \text{BordaCount}(\text{ranks})$\;
  \eIf{$\text{winner} = A$}{
    $\text{streak} \gets \text{streak} + 1$\;
  }{
    $A \gets \text{winner}$; $\text{streak} \gets 0$\;
  }
}
\Return{$A$}\;
\end{algorithm}
```

### TikZ 示意图模式

TikZ 是 ML 论文中方法图的标准。常见模式：

**流水线/流程图**（ML 论文中最常见）：

```latex
\begin{figure}[t]
\centering
\begin{tikzpicture}[
  node distance=1.8cm,
  box/.style={rectangle, draw, rounded corners, minimum height=1cm, 
              minimum width=2cm, align=center, font=\small},
  arrow/.style={-{Stealth[length=3mm]}, thick},
]
  \node[box, fill=okcyan!20] (input) {Input\\$x$};
  \node[box, fill=okblue!20, right of=input] (encoder) {Encoder\\$f_\theta$};
  \node[box, fill=okgreen!20, right of=encoder] (latent) {Latent\\$z$};
  \node[box, fill=okorange!20, right of=latent] (decoder) {Decoder\\$g_\phi$};
  \node[box, fill=okred!20, right of=decoder] (output) {Output\\$\hat{x}$};
  
  \draw[arrow] (input) -- (encoder);
  \draw[arrow] (encoder) -- (latent);
  \draw[arrow] (latent) -- (decoder);
  \draw[arrow] (decoder) -- (output);
\end{tikzpicture}
\caption{Architecture overview. The encoder maps input $x$ to latent 
representation $z$, which the decoder reconstructs.}
\label{fig:architecture}
\end{figure}
```

**对比/矩阵图**（用于展示方法变体）：

```latex
\begin{tikzpicture}[
  cell/.style={rectangle, draw, minimum width=2.5cm, minimum height=1cm, 
               align=center, font=\small},
  header/.style={cell, fill=gray!20, font=\small\bfseries},
]
  % 标题行
  \node[header] at (0, 0) {Method};
  \node[header] at (3, 0) {Converges?};
  \node[header] at (6, 0) {Quality?};
  % 行
  \node[cell] at (0, -1) {Single Pass};
  \node[cell, fill=okgreen!15] at (3, -1) {N/A};
  \node[cell, fill=okorange!15] at (6, -1) {Baseline};
  \node[cell] at (0, -2) {Critique+Revise};
  \node[cell, fill=okred!15] at (3, -2) {No};
  \node[cell, fill=okred!15] at (6, -2) {Degrades};
  \node[cell] at (0, -3) {Ours};
  \node[cell, fill=okgreen!15] at (3, -3) {Yes ($k$=2)};
  \node[cell, fill=okgreen!15] at (6, -3) {Improves};
\end{tikzpicture}
```

**迭代循环图**（用于带反馈的方法）：

```latex
\begin{tikzpicture}[
  node distance=2cm,
  box/.style={rectangle, draw, rounded corners, minimum height=0.8cm, 
              minimum width=1.8cm, align=center, font=\small},
  arrow/.style={-{Stealth[length=3mm]}, thick},
  label/.style={font=\scriptsize, midway, above},
]
  \node[box, fill=okblue!20] (gen) {Generator};
  \node[box, fill=okred!20, right=2.5cm of gen] (critic) {Critic};
  \node[box, fill=okgreen!20, below=1.5cm of $(gen)!0.5!(critic)$] (judge) {Judge Panel};
  
  \draw[arrow] (gen) -- node[label] {output $A$} (critic);
  \draw[arrow] (critic) -- node[label, right] {critique $C$} (judge);
  \draw[arrow] (judge) -| node[label, left, pos=0.3] {winner} (gen);
\end{tikzpicture}
```

### 用 latexdiff 跟踪修订

对 rebuttal 至关重要——生成一个标记了变更的 PDF，显示版本之间的差异：

```bash
# 安装
# macOS：brew install latexdiff（或随 TeX Live 提供）
# Linux：sudo apt install latexdiff

# 生成 diff
latexdiff paper_v1.tex paper_v2.tex > paper_diff.tex
pdflatex paper_diff.tex

# 对于多文件项目（带 \input{} 或 \include{}）
latexdiff --flatten paper_v1.tex paper_v2.tex > paper_diff.tex
```

这会产生一个 PDF，删除内容用红色删除线、新增内容用蓝色——是 rebuttal 补充材料的标准格式。

### 用于 matplotlib 的 SciencePlots

安装并使用以获得发表级别的图：

```bash
pip install SciencePlots
```

```python
import matplotlib.pyplot as plt
import scienceplots  # 注册样式

# 使用 science 样式（类 IEEE，干净）
with plt.style.context(['science', 'no-latex']):
    fig, ax = plt.subplots(figsize=(3.5, 2.5))  # 单栏宽度
    ax.plot(x, y, label='Ours', color='#0072B2')
    ax.plot(x, y2, label='Baseline', color='#D55E00', linestyle='--')
    ax.set_xlabel('Training Steps')
    ax.set_ylabel('Accuracy')
    ax.legend()
    fig.savefig('paper/fig_results.pdf', bbox_inches='tight')

# 可用样式：'science', 'ieee', 'nature', 'science+ieee'
# 如果生成图的机器上未安装 LaTeX，添加 'no-latex'
```

**标准图尺寸**（双栏格式）：
- 单栏：`figsize=(3.5, 2.5)` —— 适合一栏
- 双栏：`figsize=(7.0, 3.0)` —— 跨两栏
- 方形：`figsize=(3.5, 3.5)` —— 用于热力图、混淆矩阵

---

## Phase 6：自我评审与修订

**目标**：在投稿前模拟评审过程。尽早发现弱点。

### Step 6.1：模拟评审（集成模式）

从多个视角生成评审。来自自动化研究流水线（尤其是 SakanaAI 的 AI-Scientist）的关键洞察：**带元评审员的集成评审远比单次评审产生更校准的反馈。**

**第 1 步：生成 N 份独立评审**（N=3-5）

使用不同的模型或温度设置。每位评审员只看到论文，看不到其他评审。**默认偏向负面**——LLM 在评估中有充分记录的正面偏向。

```
你是 [VENUE] 的专家评审员。你严苛而彻底。
如果一篇论文有弱点，或你对某条论断不确定，请清楚地标记它
并在分数中反映出来。不要给予默认的善意。

按照官方评审指南评审此论文。评估：

1. Soundness（论断是否有充分支撑？baselines 是否公平且强？）
2. Clarity（论文写得好吗？专家能否复现？）
3. Significance（这对社区重要吗？）
4. Originality（新见解，而非仅仅是增量组合？）

以结构化 JSON 提供你的评审：
{
  "summary": "2-3 句摘要",
  "strengths": ["strength 1", "strength 2", ...],
  "weaknesses": ["weakness 1（最关键）", "weakness 2", ...],
  "questions": ["给作者的问题 1", ...],
  "missing_references": ["应引用的论文", ...],
  "soundness": 1-4,
  "presentation": 1-4,
  "contribution": 1-4,
  "overall": 1-10,
  "confidence": 1-5
}
```

**第 2 步：元评审（Area Chair 汇总）**

把所有 N 份评审喂给元评审员：

```
你是 [VENUE] 的 Area Chair。你收到了 [N] 份对一篇论文的独立评审。
你的任务是：

1. 识别跨评审员的共识性优点和弱点
2. 通过直接查阅论文来解决分歧
3. 产出一份代表汇总判断的元评审
4. 使用跨所有评审的平均数值分数

要保守：如果评审员对某个弱点是否严重存在分歧，
在作者回应之前将其视为严重。

评审：
[review_1]
[review_2]
...
```

**第 3 步：反思循环**（可选，2-3 轮）

每位评审员在看到元评审后可以精修其评审。使用早停哨兵：如果评审员回复「I am done」（无更改），则停止迭代。

**用于评审的模型选择**：评审最好用可用的最强模型完成，即使你写论文时用的是更便宜的模型。评审模型应独立于写作模型来选择。

**Few-shot 校准**：如果可能，包含 1-2 份来自目标会议的真实已发表评审作为示例。这会显著改善分数校准。示例评审见 [references/reviewer-guidelines.md](references/reviewer-guidelines.md)。

### Step 6.1b：视觉评审轮（VLM）

纯文本评审会漏掉一整类问题：图质量、排版问题、视觉一致性。如果你能访问具备视觉能力的模型，对编译后的 PDF 单独运行一次**视觉评审**：

```
你正在评审这份研究论文 PDF 的视觉呈现。
检查：
1. 图质量：图可读吗？标签清晰吗？颜色可区分吗？
2. 图-图注对齐：每个图注是否准确描述其图？
3. 排版问题：孤立的章节标题、尴尬的分页、图距离其引用很远
4. 表格排版：列对齐、一致的小数精度、最佳结果加粗
5. 视觉一致性：所有图使用相同配色方案、一致的字号
6. 灰度可读性：如果黑白打印，这些图还能理解吗？

对每个问题，指明页码和确切位置。
```

这能捕获基于文本的评审无法发现的问题：坐标轴标签不清的图、距首次引用 3 页之远的图、图 2 和图 5 之间不一致的调色板，或明显宽于栏宽的表格。

### Step 6.1c：论断验证轮

模拟评审之后，运行一次单独的验证轮。这能捕获评审员可能遗漏的事实错误：

```
论断验证协议：
1. 从论文中提取每一条事实论断（数字、比较、趋势）
2. 对每条论断，追溯到支撑它的具体实验/结果
3. 验证论文中的数字与实际结果文件匹配
4. 把任何没有可追溯来源的论断标记为 [VERIFY]
```

对于基于 agent 的工作流：把验证委派给一个**全新的子 agent**，它只接收论文文本和原始结果文件。全新的上下文能防止确认偏误——验证者不「记得」结果本应是什么。

### Step 6.2：优先级排序反馈

收集评审之后，分类：

| 优先级 | 行动 |
|----------|--------|
| **关键**（技术缺陷、缺失 baseline） | 必须修复。可能需要新实验 → 回到 Phase 2 |
| **高**（清晰度问题、缺失消融） | 应在本次修订中修复 |
| **中**（次要写作问题、额外实验） | 时间允许则修复 |
| **低**（风格偏好、枝节建议） | 留作未来工作 |

### Step 6.3：修订循环

对每个关键/高优先级问题：
1. 识别受影响的具体章节
2. 起草修复
3. 验证修复不会破坏其他论断
4. 更新论文
5. 对照评审员的关切重新检查

### Step 6.4：撰写 Rebuttal

回应真实评审时（投稿后），rebuttal 是一项与修订不同的技能：

**格式**：逐点回应。对每位评审员的每条关切：
```
> R1-W1: "The paper lacks comparison with Method X."

我们感谢评审员的建议。我们已在表 3（修订版）中加入与
Method X 的对比。我们的方法在 [指标] 上比 X 高出 3.2pp
（p<0.05）。我们注意到 X 需要 2 倍于我们的算力预算。
```

**规则**：
- 回应每一条关切——评审员会注意到你跳过了哪条
- 把最强的回应放在前面
- 简洁直接——评审员要读几十份 rebuttal
- 如果在 rebuttal 期间运行了实验，包含新结果
- 永远不要防御或轻视，即便对薄弱的批评
- 使用 `latexdiff` 生成标记变更的 PDF（见专业 LaTeX 工具一节）
- 感谢评审员给出的具体、可操作的反馈（而非泛泛的赞美）

**不要做的**：没有证据的「我们 respectfully disagree」。没有解释的「这超出范围」。只回应优点而忽视弱点。

### Step 6.5：论文演进跟踪

在关键里程碑保存快照：
```
paper/
  paper.tex                    # 当前工作版本
  paper_v1_first_draft.tex     # 第一版完整草稿
  paper_v2_post_review.tex     # 模拟评审之后
  paper_v3_pre_submission.tex  # 投稿前最终版
  paper_v4_camera_ready.tex    # 录用后最终版
```

---

## Phase 7：投稿准备

**目标**：最终检查、排版和投稿。

### Step 7.1：会议清单

每个会议都有强制性清单。认真完成它们——不完整的清单可能导致桌面拒稿。

参见 [references/checklists.md](references/checklists.md)：
- NeurIPS 16 项论文清单
- ICML 更广泛影响 + 可复现性
- ICLR LLM 披露政策
- ACL 强制局限性章节
- 通用投稿前清单

### Step 7.2：匿名化清单

双盲评审意味着评审员不能知道论文是谁写的。检查以下所有项：

```
匿名化清单：
- [ ] PDF 中任何地方都没有作者姓名或单位
- [ ] 没有致谢章节（录用后添加）
- [ ] 自引用用第三人称写："Smith et al. [1] showed..." 而不是 "We previously showed [1]..."
- [ ] 没有指向你个人仓库的 GitHub/GitLab URL
- [ ] 代码链接使用 Anonymous GitHub（https://anonymous.4open.science/）
- [ ] 图中没有机构标识或标识符
- [ ] 文件元数据中不含作者姓名（检查 PDF 属性）
- [ ] 没有「our previous work」或「in our earlier paper」的措辞
- [ ] 数据集名称不泄露机构（如需要请重命名）
- [ ] 补充材料不含可识别信息
```

**常见错误**：补充代码中可见的 git 提交信息、来自机构工具的带水印图、从前一草稿遗留的致谢、在匿名期之前发布的 arXiv 预印本。

### Step 7.3：排版验证

```
投稿前格式检查：
- [ ] 遵守页数限制（不含参考文献和附录）
- [ ] 所有图都是矢量（PDF）或高分辨率栅格（600 DPI PNG）
- [ ] 所有图在灰度下可读
- [ ] 所有表格使用 booktabs
- [ ] 参考文献正确编译（引用中没有「?」）
- [ ] 关键区域没有 overfull hbox
- [ ] 附录清晰标注并分隔
- [ ] 必需章节齐全（局限性、更广泛影响等）
```

### Step 7.4：预编译验证

在尝试 `pdflatex` 之前运行这些自动化检查。**在此处**捕获错误比调试编译器输出更快。

```bash
# 1. 用 chktex 做 lint（捕获常见 LaTeX 错误）
# 抑制嘈杂的警告：-n2（句末）、-n24（括号）、-n13（句间）、-n1（命令终止）
chktex main.tex -q -n2 -n24 -n13 -n1

# 2. 验证所有引用都在 .bib 中存在
# 从 .tex 提取 \cite{...}，逐个对照 .bib 检查
python3 -c "
import re
tex = open('main.tex').read()
bib = open('references.bib').read()
cites = set(re.findall(r'\\\\cite[tp]?{([^}]+)}', tex))
for cite_group in cites:
    for cite in cite_group.split(','):
        cite = cite.strip()
        if cite and cite not in bib:
            print(f'WARNING: \\\\cite{{{cite}}} not found in references.bib')
"

# 3. 验证所有被引用的图在磁盘上存在
python3 -c "
import re, os
tex = open('main.tex').read()
figs = re.findall(r'\\\\includegraphics(?:\[.*?\])?{([^}]+)}', tex)
for fig in figs:
    if not os.path.exists(fig):
        print(f'WARNING: Figure file not found: {fig}')
"

# 4. 检查重复的 \label 定义
python3 -c "
import re
from collections import Counter
tex = open('main.tex').read()
labels = re.findall(r'\\\\label{([^}]+)}', tex)
dupes = {k: v for k, v in Counter(labels).items() if v > 1}
for label, count in dupes.items():
    print(f'WARNING: Duplicate label: {label} (appears {count} times)')
"
```

在继续之前修复任何警告。对于基于 agent 的工作流：把 chktex 输出回喂给 agent，指示其做最小化修复。

### Step 7.5：最终编译

```bash
# 干净构建
rm -f *.aux *.bbl *.blg *.log *.out *.pdf
latexmk -pdf main.tex

# 或手动（三次 pdflatex + bibtex 用于交叉引用）
pdflatex -interaction=nonstopmode main.tex
bibtex main
pdflatex -interaction=nonstopmode main.tex
pdflatex -interaction=nonstopmode main.tex

# 验证输出存在且有内容
ls -la main.pdf
```

**如果编译失败**：解析 `.log` 文件中的第一个错误。常见修复：
- "Undefined control sequence" → 缺少宏包或命令名拼写错误
- "Missing $ inserted" → 数学模式之外有数学符号
- "File not found" → 图路径错误或缺少 .sty 文件
- "Citation undefined" → .bib 缺少条目或未运行 bibtex

### Step 7.6：会议特定要求

| 会议 | 特殊要求 |
|-------|---------------------|
| **NeurIPS** | 附录中的论文清单，录用后需通俗摘要 |
| **ICML** | Broader Impact Statement（结论之后，不计入页限） |
| **ICLR** | 要求 LLM 披露，互评协议 |
| **ACL** | 强制 Limitations 章节，Responsible NLP 清单 |
| **AAAI** | 严格样式文件——绝不允许任何修改 |
| **COLM** | 为语言模型社区表述贡献 |

### Step 7.7：会议再投稿与格式转换

在不同会议之间转换时，**永远不要在模板之间复制 LaTeX 导言区**：

```bash
# 1. 用目标模板全新开始
cp -r templates/icml2026/ new_submission/

# 2. 只复制内容章节（不是导言区）
#    - 摘要文本、章节内容、图、表、bib 条目

# 3. 调整以适应页数限制
# 4. 添加会议特定的必需章节
# 5. 更新引用
```

| 从 → 到 | 页数变化 | 关键调整 |
|-----------|-------------|-----------------|
| NeurIPS → ICML | 9 → 8 | 削减 1 页，添加 Broader Impact |
| ICML → ICLR | 8 → 9 | 扩展实验，添加 LLM 披露 |
| NeurIPS → ACL | 9 → 8 | 按 NLP 惯例重构，添加 Limitations |
| ICLR → AAAI | 9 → 7 | 大幅削减，严格遵守样式 |
| Any → COLM | 不定 → 9 | 重新表述以突出语言模型焦点 |

削减页数时：把证明移到附录、压缩相关工作、合并表格、使用子图。
扩展时：添加消融、扩展局限性、包含额外 baselines、添加定性示例。

**被拒之后**：在新版本中回应评审员的关切，但不要包含「变更」章节或提及之前的投稿（盲审）。

### Step 7.8：Camera-Ready 准备（录用后）

录用之后，准备 camera-ready 版本：

```
Camera-Ready 清单：
- [ ] 去匿名化：添加作者姓名、单位、邮箱
- [ ] 添加 Acknowledgments 章节（资助、算力资助、有帮助的评审员）
- [ ] 添加公开的代码/数据 URL（真实的 GitHub，而非匿名）
- [ ] 回应元评审员指定的任何强制性修订
- [ ] 把模板切换到 camera-ready 模式（如适用——例如 AAAI \anon → \camera）
- [ ] 如会议要求，添加版权声明
- [ ] 更新正文中任何「anonymous」占位符
- [ ] 验证最终 PDF 干净编译
- [ ] 检查 camera-ready 的页数限制（有时与投稿不同）
- [ ] 把补充材料（代码、数据、附录）上传到会议门户
```

### Step 7.9：arXiv 与预印本策略

在 ML 领域，发到 arXiv 是标准做法，但有重要的时机和匿名性考量。

**时机决策树：**

| 情境 | 建议 |
|-----------|---------------|
| 投稿双盲会议（NeurIPS、ICML、ACL） | 在投稿截止**之后**再发 arXiv，而非之前。之前发布严格来说可能违反匿名政策，尽管执行情况各异。 |
| 投稿 ICLR | ICLR 明确允许在投稿前发 arXiv。但不要在投稿本身中放作者姓名。 |
| 论文已在 arXiv，投新会议 | 大多数会议允许。**不要**在评审期间用明显回应评审反馈的变更更新 arXiv 版本。 |
| Workshop 论文 | arXiv 任何时候都可以——workshop 通常不是双盲。 |
| 想确立优先权 | 如果担心被抢先则立即发布——但接受匿名性权衡。 |

**arXiv 类别选择**（ML/AI 论文）：

| 类别 | 代码 | 最适合 |
|----------|------|----------|
| Machine Learning | `cs.LG` | 通用 ML 方法 |
| Computation and Language | `cs.CL` | NLP、语言模型 |
| Artificial Intelligence | `cs.AI` | 推理、规划、agent |
| Computer Vision | `cs.CV` | 视觉模型 |
| Information Retrieval | `cs.IR` | 搜索、推荐 |

**列出 1 个主要 + 1-2 个交叉类别。** 更多类别 = 更多可见度，但只在确实相关时交叉列出。

**版本策略：**
- **v1**：初始投稿（与会议投稿匹配）
- **v2**：录用后带 camera-ready 修正（在摘要中加入「accepted at [Venue]」）
- 不要在评审期间发布 v2，若变更明显是在回应评审反馈

```bash
# 在选择标题之前，检查你的论文标题是否已被占用
pip install arxiv
python -c "
import arxiv
results = list(arxiv.Search(query='ti:\"Your Exact Title\"', max_results=5).results())
print(f'Found {len(results)} matches')
for r in results: print(f'  {r.title} ({r.published.year})')
"
```

### Step 7.10：研究代码打包

发布干净、可运行的代码会显著增加引用量和评审员信任。把代码与 camera-ready 投稿一起打包。

**仓库结构：**

```
your-method/
  README.md              # 安装、使用、复现说明
  requirements.txt       # 或 conda 用 environment.yml
  setup.py               # 用于可 pip 安装的包
  LICENSE                # 研究推荐 MIT 或 Apache 2.0
  configs/               # 实验配置
  src/                   # 核心方法实现
  scripts/               # 训练、评估、分析脚本
    train.py
    evaluate.py
    reproduce_table1.sh  # 每个主要结果一个脚本
  data/                  # 小数据或下载脚本
    download_data.sh
  results/               # 用于验证的预期输出
```

**研究代码的 README 模板：**

```markdown
# [Paper Title]

Official implementation of "[Paper Title]" (Venue Year).

## Setup
[建立环境的确切命令]

## Reproduction
To reproduce Table 1: `bash scripts/reproduce_table1.sh`
To reproduce Figure 2: `python scripts/make_figure2.py`

## Citation
[BibTeX 条目]
```

**发布前清单：**
```
- [ ] 代码在干净克隆下可运行（在全新机器或 Docker 上测试）
- [ ] 所有依赖固定到具体版本
- [ ] 没有硬编码的绝对路径
- [ ] 仓库中没有 API 密钥、凭证或个人数据
- [ ] README 覆盖安装、复现和引用
- [ ] 存在 LICENSE 文件（MIT 或 Apache 2.0 以便最大化复用）
- [ ] 结果在预期方差内可复现
- [ ] .gitignore 排除数据文件、检查点、日志
```

**用于投稿的匿名代码**（录用之前）：
```bash
# 双盲评审使用 Anonymous GitHub
# https://anonymous.4open.science/
# 上传你的仓库 → 获得匿名 URL → 放入论文
```

---

## Phase 8：录用后交付物

**目标**：通过展示材料和社区参与最大化你已录用论文的影响。

### Step 8.1：会议海报

大多数会议要求海报展示。海报设计原则：

| 元素 | 指南 |
|---------|-----------|
| **尺寸** | 查阅会议要求（通常 24"x36" 或 A0 竖版/横版） |
| **内容** | 标题、作者、一句话贡献、方法图、2-3 个关键结果、结论 |
| **流向** | 从左上到右下（Z 型）或分栏 |
| **文字** | 标题在 3m 外可读，正文在 1m 外可读。不要整段——只用要点。 |
| **图** | 复用论文中的图，用更高分辨率。放大关键结果。 |

**工具**：LaTeX（`beamerposter` 宏包）、PowerPoint/Keynote、Figma、Canva。

**制作**：在会议前 2+ 周订购。织物海报旅行更轻便。许多会议现在也支持虚拟/数字海报。

### Step 8.2：会议报告 / Spotlight

如果获得口头或 spotlight 展示：

| 报告类型 | 时长 | 内容 |
|-----------|----------|---------|
| **Spotlight** | 5 分钟 | 问题、方法、一个关键结果。排练到恰好 5 分钟。 |
| **口头** | 15-20 分钟 | 完整故事：问题、方法、关键结果、消融、局限性。 |
| **Workshop 报告** | 10-15 分钟 | 根据 workshop 听众调整——可能需要更多背景。 |

**幻灯片设计规则：**
- 一页一个想法
- 文字最小化——讲细节，不要把细节投射出来
- 对关键图做动画，逐步建立理解
- 结尾放一张「要点」幻灯片（一句话贡献）
- 为预期问题准备备用幻灯片

### Step 8.3：博客文章 / 社交媒体

一份通俗易懂的总结会显著放大影响：

- **Twitter/X 串**：5-8 条推文。以结果而非方法开头。包含图 1 和关键结果图。
- **博客文章**：800-1500 词。写给 ML 从业者，而非评审员。跳过形式化，强调直觉和实际启示。
- **项目主页**：HTML 页面，含摘要、图、demo、代码链接、BibTeX。使用 GitHub Pages。

**时机**：在论文出现在论文集或 arXiv camera-ready 之后的 1-2 天内发布。

---

## Workshop 与短论文

Workshop 论文和短论文（例如 ACL 短论文、Findings 论文）遵循相同的流水线，但有不同约束和期望。

### Workshop 论文

| 属性 | Workshop | 主会议 |
|----------|----------|-----------------|
| **页数限制** | 4-6 页（通常） | 7-9 页 |
| **评审标准** | 对完整性门槛较低 | 必须完整、彻底 |
| **评审流程** | 通常单盲或轻评审 | 双盲、严格 |
| **看重的** | 有趣的想法、初步结果、立场文章 | 带强 baselines 的完整实证故事 |
| **arXiv** | 任何时候可发 | 时机重要（见 arXiv 策略） |
| **贡献门槛** | 新方向、有趣的负面结果、进行中的工作 | 有强证据的显著推进 |

**何时瞄准 workshop：**
- 想在写完整论文前获得反馈的早期想法
- 不值得 8+ 页的负面结果
- 关于时兴话题的立场或观点文章
- 复现研究或可复现性报告

### ACL 短论文与 Findings

ACL 系列会议有不同的投稿类型：

| 类型 | 页数 | 期望 |
|------|-------|-----------------|
| **长文** | 8 | 完整研究、强 baselines、消融 |
| **短文** | 4 | 聚焦贡献：一个清晰论点带证据 |
| **Findings** | 8 | 扎实但与主会议擦肩而过的工作 |

**短论文策略**：挑**一个**论断并彻底支撑它。不要试图把长文压成 4 页——写一篇不同的、更聚焦的论文。

---

## 实证 ML 之外的论文类型

上面的主流水线针对实证 ML 论文。其他论文类型需要不同结构和证据标准。每种类型的详细指导见 [references/paper-types.md](references/paper-types.md)。

### 理论论文

**结构**：引言 → 预备知识（定义、符号） → 主要结果（定理） → 证明概要 → 讨论 → 完整证明（附录）

**与实证论文的关键区别：**
- 贡献是定理、界或不可能性结果——而非实验数字
- Methods 章节被「预备知识」和「主要结果」取代
- 证明是证据，而非实验（不过对理论的实证验证是受欢迎的）
- 正文放证明概要、附录放完整证明是标准做法
- 实验章节可选，但如果它验证了理论预测则会增强论文

**证明写作原则：**
- 形式化陈述定理，所有假设都要显式
- 在形式化证明之前提供直觉（「关键洞察是……」）
- 证明概要应在 0.5-1 页内传达主要想法
- 使用 `\begin{proof}...\end{proof}` 环境
- 给假设编号并在定理中引用：「在假设 1-3 下，……」

### 综述 / 教程论文

**结构**：引言 → 分类法 / 组织方式 → 详细覆盖 → 开放问题 → 结论

**关键区别：**
- 贡献是组织、综合和识别开放问题——而非新方法
- 必须在范围内全面（评审员会检查是否有遗漏的引用）
- 需要清晰的分类法或组织框架
- 价值来自单篇论文无法建立的相互联系
- 最佳会议：TMLR（综述 track）、JMLR、Foundations and Trends in ML、ACM Computing Surveys

### Benchmark 论文

**结构**：引言 → 任务定义 → 数据集构建 → Baseline 评估 → 分析 → 预期用途与局限

**关键区别：**
- 贡献是 benchmark 本身——它必须填补真实的评估空白
- 数据集文档是强制的，而非可选（见 Datasheets，Step 5.11）
- 必须证明 benchmark 具有挑战性（baselines 无法饱和它）
- 必须证明 benchmark 测量的是你声称它测量的（建构效度）
- 最佳会议：NeurIPS Datasets & Benchmarks track、ACL（资源论文）、LREC-COLING

### 立场论文

**结构**：引言 → 背景 → 论点 / 主张 → 支撑证据 → 反驳 → 启示

**关键区别：**
- 贡献是一个论点，而非一个结果
- 必须认真对待反驳
- 证据可以是实证、理论或逻辑分析
- 最佳会议：ICML（立场 track）、workshops、TMLR

---

## Hermes Agent 集成

此技能为 Hermes agent 设计。它使用 Hermes 工具、委派、调度和记忆来覆盖完整的研究生命周期。

### 相关技能

把此技能与其他 Hermes 技能组合用于特定阶段：

| 技能 | 何时使用 | 如何加载 |
|-------|-------------|-------------|
| **arxiv** | Phase 1（文献综述）：搜索 arXiv、生成 BibTeX、通过 Semantic Scholar 查找相关论文 | `skill_view("arxiv")` |
| **subagent-driven-development** | Phase 5（起草）：并行章节写作，带两阶段评审（先规范合规再质量） | `skill_view("subagent-driven-development")` |
| **plan** | Phase 0（搭建）：在执行前创建结构化计划。写入 `.hermes/plans/` | `skill_view("plan")` |
| **qmd** | Phase 1（文献）：通过混合 BM25+向量搜索查询本地知识库（笔记、转录、文档） | 安装：`skill_manage("install", "qmd")` |
| **diagramming** | Phase 4-5：创建基于 Excalidraw 的图和架构示意图 | `skill_view("diagramming")` |
| **data-science** | Phase 4（分析）：用于交互式分析和可视化的 Jupyter 实时内核 | `skill_view("data-science")` |

**此技能取代 `ml-paper-writing`** ——它包含 ml-paper-writing 的全部内容，加上完整的实验/分析流水线和 autoreason 方法学。

### Hermes 工具参考

| 工具 | 在此流水线中的用法 |
|------|----------------------|
| **`terminal`** | LaTeX 编译（`latexmk -pdf`）、git 操作、启动实验（`nohup python run.py &`）、进程检查 |
| **`process`** | 后台实验管理：`process("start", ...)`、`process("poll", pid)`、`process("log", pid)`、`process("kill", pid)` |
| **`execute_code`** | 运行 Python 进行引用验证、统计分析、数据聚合。通过 RPC 拥有工具访问权。 |
| **`read_file`** / **`write_file`** / **`patch`** | 论文编辑、实验脚本、结果文件。对大型 .tex 文件用 `patch` 做定向编辑。 |
| **`web_search`** | 文献发现：`web_search("transformer attention mechanism 2024")` |
| **`web_extract`** | 获取论文内容、验证引用：`web_extract("https://arxiv.org/abs/2303.17651")` |
| **`delegate_task`** | **并行章节起草**——为每个章节生成隔离的子 agent。也用于并行引用验证。 |
| **`todo`** | 跨会话的主要状态跟踪器。每次阶段转换后更新。 |
| **`memory`** | 跨会话持久化关键决策：贡献框架、会议选择、评审员反馈。 |
| **`cronjob`** | 调度实验监控、截止日期倒计时、自动化 arXiv 检查。 |
| **`clarify`** | 在受阻时向用户提有针对性的问题（会议选择、贡献框架）。 |
| **`send_message`** | 在实验完成或草稿就绪时通知用户，即使用户不在聊天中。 |

### 工具使用模式

**实验监控**（最常见）：
```
terminal("ps aux | grep <pattern>")
→ terminal("tail -30 <logfile>")
→ terminal("ls results/")
→ execute_code("分析结果 JSON，计算指标")
→ terminal("git add -A && git commit -m '<描述性信息>' && git push")
→ send_message("Experiment complete: <摘要>")
```

**并行章节起草**（使用委派）：
```
delegate_task("基于这些实验脚本和配置起草 Methods 章节。
  包含：伪代码、所有超参数、足以复现的架构细节。使用 neurips2025 模板约定以 LaTeX 编写。")

delegate_task("起草 Related Work 章节。使用 web_search 和 web_extract
  查找论文。通过 Semantic Scholar 验证每条引用。按方法论分组。")

delegate_task("起草 Experiments 章节。阅读 results/ 下所有结果文件。
  陈述每个实验支撑哪条论断。包含误差棒和显著性。")
```

每次委派都作为**全新子 agent**运行，没有共享上下文——在提示词中提供所有必要信息。收集输出并整合。

**引用验证**（使用 execute_code）：
```python
# 在 execute_code 中：
from semanticscholar import SemanticScholar
import requests

sch = SemanticScholar()
results = sch.search_paper("attention mechanism transformers", limit=5)
for paper in results:
    doi = paper.externalIds.get('DOI', 'N/A')
    if doi != 'N/A':
        bibtex = requests.get(f"https://doi.org/{doi}", 
                              headers={"Accept": "application/x-bibtex"}).text
        print(bibtex)
```

### 用 `memory` 和 `todo` 进行状态管理

**`memory` 工具** —— 持久化关键决策（有界：MEMORY.md 约 2200 字符）：

```
memory("add", "Paper: autoreason. Venue: NeurIPS 2025 (9 pages). 
  Contribution: structured refinement works when generation-evaluation gap is wide.
  Key results: Haiku 42/42, Sonnet 3/5, S4.6 constrained 2/3.
  Status: Phase 5 — drafting Methods section.")
```

在重大决策或阶段转换之后更新 memory。这会跨会话持久化。

**`todo` 工具** —— 跟踪细粒度进度：

```
todo("add", "为 Sonnet 4.6 设计受约束任务实验")
todo("add", "运行 Haiku baseline 对比")
todo("add", "起草 Methods 章节")
todo("update", id=3, status="in_progress")
todo("update", id=1, status="completed")
```

**会话启动协议：**
```
1. todo("list")                           # 检查当前任务清单
2. memory("read")                         # 回忆关键决策
3. terminal("git log --oneline -10")      # 检查最近提交
4. terminal("ps aux | grep python")       # 检查运行中的实验
5. terminal("ls results/ | tail -20")     # 检查新结果
6. 向用户报告状态，询问方向
```

### 用 `cronjob` 进行 Cron 监控

使用 `cronjob` 工具调度周期性实验检查：

```
cronjob("create", {
  "schedule": "*/30 * * * *",  # 每 30 分钟
  "prompt": "检查实验状态：
    1. ps aux | grep run_experiment
    2. tail -30 logs/experiment_haiku.log
    3. ls results/haiku_baselines/
    4. 如完成：读取结果，计算 Borda 分数，
       git add -A && git commit -m 'Add Haiku results' && git push
    5. 报告：结果表、关键发现、下一步
    6. 如无变化：回复 [SILENT]"
})
```

**[SILENT] 协议**：当自上次检查以来没有任何变化时，精确回复 `[SILENT]`。这会抑制对用户的通知。仅在有真正值得了解的变化时报告。

**截止日期跟踪**：
```
cronjob("create", {
  "schedule": "0 9 * * *",  # 每天 9 点
  "prompt": "NeurIPS 2025 deadline: May 22. Today is {date}. 
    Days remaining: {计算}. 
    检查 todo 列表——我们进度如何？ 
    If <7 days: 警告用户剩余任务."
})
```

### 沟通模式

**何时通知用户**（通过 `send_message` 或直接回复）：
- 实验批次完成（带结果表）
- 需要决策的意外发现或失败
- 草稿章节已就绪待评审
- 截止日期临近但任务未完成

**何时不要通知：**
- 实验仍在运行、无新结果 → `[SILENT]`
- 无变化的例行监控 → `[SILENT]`
- 不需要关注的中间步骤

**报告格式** —— 始终包含结构化数据：
```
## Experiment: <名称>
Status: Complete / Running / Failed

| Task | Method A | Method B | Method C |
|------|---------|---------|---------|
| Task 1 | 85.2 | 82.1 | **89.4** |

Key finding: <一句话>
Next step: <接下来发生什么>
```

### 需要人工输入的决策点

当真正受阻时使用 `clarify` 提有针对性的问题：

| 决策 | 何时提问 |
|----------|-------------|
| 目标会议 | 开始写论文之前（影响页数限制、表述框架） |
| 贡献表述框架 | 存在多个有效框架时 |
| 实验优先级 | 当 TODO 列表的实验数超过时间允许时 |
| 投稿就绪度 | 最终投稿之前 |

**不要提问的事项**（要主动、做出选择、并标记）：
- 用词、章节顺序
- 突出哪些具体结果
- 引用完整性（用你找到的起草，标注空白）

---

## 评审员评估标准

理解评审员寻找什么有助于聚焦努力：

| 标准 | 他们检查什么 |
|-----------|----------------|
| **质量（Quality）** | 技术健全性、论断有充分支撑、公平的 baselines |
| **清晰度（Clarity）** | 写作清晰、专家可复现、符号一致 |
| **重要性（Significance）** | 社区影响、推进理解 |
| **原创性（Originality）** | 新见解（不要求新方法） |

**评分（NeurIPS 6 分制）：**
- 6：Strong Accept —— 开创性、无瑕疵
- 5：Accept —— 技术扎实、高影响
- 4：Borderline Accept —— 扎实、评估有限
- 3：Borderline Reject —— 弱点盖过优点
- 2：Reject —— 技术缺陷
- 1：Strong Reject —— 已知结果或伦理问题

详细指南、常见关切和 rebuttal 策略，请见 [references/reviewer-guidelines.md](references/reviewer-guidelines.md)。

---

## 常见问题与解决方案

| 问题 | 解决方案 |
|-------|----------|
| 摘要太泛 | 如果第一句可以加在任何 ML 论文前面，就删掉它。以你的具体贡献开头。 |
| 引言超过 1.5 页 | 把背景拆分到 Related Work。把贡献要点前置。 |
| 实验缺乏明确论断 | 在每个实验前添加：「本实验检验 [具体论断]……」 |
| 评审员觉得论文难跟 | 增加路标、使用一致术语、让图注自包含。 |
| 缺失统计显著性 | 添加误差棒、运行次数、统计检验、置信区间。 |
| 实验范围蔓延 | 每个实验都必须映射到具体论断。删掉不映射的实验。 |
| 论文被拒、需要再投 | 见 Phase 7 的会议再投稿。回应评审员关切但不要引用评审。 |
| 缺失更广泛影响声明 | 见 Step 5.10。大多数会议要求它。「无负面影响」几乎从不可信。 |
| 人类评估被批为薄弱 | 见 Step 2.5 和 [references/human-evaluation.md](references/human-evaluation.md)。报告一致性指标、标注员详情、报酬。 |
| 评审员质疑可复现性 | 发布代码（Step 7.9）、记录所有超参数、包含种子和算力详情。 |
| 理论论文缺乏直觉 | 在形式化证明前添加带通俗语言解释的证明概要。见 [references/paper-types.md](references/paper-types.md)。 |
| 结果为负面/无效 | 见 Phase 4.3 关于处理负面结果。考虑 workshops、TMLR 或重新表述为分析。 |

---

## 参考文档

| 文档 | 内容 |
|----------|----------|
| [references/writing-guide.md](references/writing-guide.md) | Gopen & Swan 7 原则、Perez 微观技巧、Lipton 用词、Steinhardt 精确性、图设计 |
| [references/citation-workflow.md](references/citation-workflow.md) | 引用 API、Python 代码、CitationManager 类、BibTeX 管理 |
| [references/checklists.md](references/checklists.md) | NeurIPS 16 项、ICML、ICLR、ACL 要求、通用投稿前清单 |
| [references/reviewer-guidelines.md](references/reviewer-guidelines.md) | 评估标准、评分、常见关切、rebuttal 模板 |
| [references/sources.md](references/sources.md) | 所有写作指南、会议指南、API 的完整参考文献 |
| [references/experiment-patterns.md](references/experiment-patterns.md) | 实验设计模式、评估协议、监控、错误恢复 |
| [references/autoreason-methodology.md](references/autoreason-methodology.md) | Autoreason 循环、策略选择、模型指南、提示词、范围约束、Borda 评分 |
| [references/human-evaluation.md](references/human-evaluation.md) | 人类评估设计、标注指南、一致性指标、众包 QC、IRB 指南 |
| [references/paper-types.md](references/paper-types.md) | 理论论文（证明写作、定理结构）、综述论文、benchmark 论文、立场论文 |

### LaTeX 模板

`templates/` 中的模板：**NeurIPS 2025**、**ICML 2026**、**ICLR 2026**、**ACL**、**AAAI 2026**、**COLM 2025**。

编译说明见 [templates/README.md](templates/README.md)。

### 关键外部来源

**写作理念：**
- [Neel Nanda: How to Write ML Papers](https://www.alignmentforum.org/posts/eJGptPbbFPZGLpjsp/highly-opinionated-advice-on-how-to-write-ml-papers)
- [Sebastian Farquhar: How to Write ML Papers](https://sebastianfarquhar.com/on-research/2024/11/04/how_to_write_ml_papers/)
- [Gopen & Swan: Science of Scientific Writing](https://cseweb.ucsd.edu/~swanson/papers/science-of-writing.pdf)
- [Lipton: Heuristics for Scientific Writing](https://www.approximatelycorrect.com/2018/01/29/heuristics-technical-scientific-writing-machine-learning-perspective/)
- [Perez: Easy Paper Writing Tips](https://ethanperez.net/easy-paper-writing-tips/)

**API：** [Semantic Scholar](https://api.semanticscholar.org/api-docs/) | [CrossRef](https://www.crossref.org/documentation/retrieve-metadata/rest-api/) | [arXiv](https://info.arxiv.org/help/api/basics.html)

**会议：** [NeurIPS](https://neurips.cc/Conferences/2025/PaperInformation/StyleFiles) | [ICML](https://icml.cc/Conferences/2025/AuthorInstructions) | [ICLR](https://iclr.cc/Conferences/2026/AuthorGuide) | [ACL](https://github.com/acl-org/acl-style-files)
