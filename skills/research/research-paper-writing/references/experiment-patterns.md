# 实验设计模式

从使用 Hermes agent 大规模运行研究实验中提炼出的模式与最佳实践。涵盖实验基础设施、评估协议、监控以及失败恢复。

---

## 实验基础设施

### 目录结构

用一致的结构组织实验：

```
workspace/
  experiments/
    run_main.py                # 核心实验运行器
    run_baselines.py           # 基线对比
    run_ablation.py            # 消融研究
    strategies.py              # 方法实现
    config.yaml                # 共享配置
  results/
    <experiment_name>/
      <task_or_problem>/
        <strategy>/
          result.json          # 最终指标
          final_output.md      # 最终输出产物
          history.json         # 完整轨迹/日志
          pass_01/             # 每轮迭代的产物（若为迭代式）
            intermediate.md
  analysis/
    analyze_results.py         # 统计分析
    compute_stats.py           # 显著性检验
    make_charts.py             # 可视化
  paper/
    paper.tex                  # LaTeX 源
    fig_*.pdf                  # 生成的图
```

### 脚本设计原则

**1. 增量保存（崩溃恢复）**

每个实验脚本应在每个工作单元之后保存结果，并在重启时跳过已完成的工作：

```python
import json, os
from pathlib import Path

def run_experiment(problems, strategies, output_dir):
    for problem in problems:
        for strategy in strategies:
            result_path = Path(output_dir) / problem["id"] / strategy / "result.json"
            if result_path.exists():
                print(f"Skipping {problem['id']}/{strategy} (already done)")
                continue
            
            # 运行实验
            result = execute_strategy(problem, strategy)
            
            # 立即保存
            result_path.parent.mkdir(parents=True, exist_ok=True)
            with open(result_path, 'w') as f:
                json.dump(result, f, indent=2)
```

这种模式让重新运行既安全又高效。如果进程在 47/150 题处崩溃，重启会跳过前 46 题。

**2. 产物保留**

保存所有中间输出，而不仅仅是最终结果。这样无需重跑就能做事后分析：

```python
def save_pass_artifacts(output_dir, pass_num, artifacts):
    """保存迭代方法单轮的全部产物。"""
    pass_dir = Path(output_dir) / f"pass_{pass_num:02d}"
    pass_dir.mkdir(parents=True, exist_ok=True)
    
    for name, content in artifacts.items():
        with open(pass_dir / f"{name}.md", 'w') as f:
            f.write(content)
```

**3. 配置管理**

用 YAML 配置以保证可复现：

```yaml
# config.yaml
model: anthropic/claude-sonnet-4-20250514
author_temperature: 0.8
judge_temperature: 0.3
max_tokens: 4096
num_judges: 3
max_passes: 15
convergence_k: 2
```

```python
import yaml

with open("config.yaml") as f:
    config = yaml.safe_load(f)
```

**4. 关注点分离**

把生成、评估和可视化放在各自独立的脚本里：

| 脚本 | 用途 |
|--------|---------|
| `run_experiment.py` | 核心方法执行 |
| `run_baselines.py` | 在同等算力下做基线对比 |
| `run_eval.py` | 盲评 / 评审组 |
| `analyze_results.py` | 统计分析 |
| `make_charts.py` | 图表生成 |

这让你可以不重跑昂贵的生成就重新评估，不重跑分析就重新生成图。

---

## 评估协议

### 盲评评审组（用于主观任务）

评估主观输出（写作、分析、推荐）时，使用盲评评审组：

```python
import random

def run_blind_evaluation(outputs: dict, task_prompt: str, num_judges: int = 7):
    """
    对多种方法的输出做盲评。

    Args:
        outputs: {"method_name": "output_text", ...}
        task_prompt: 原始任务描述
        num_judges: 独立评审评估的次数
    """
    rankings = []
    
    for judge_i in range(num_judges):
        # 每位评审随机化标签与呈现顺序
        methods = list(outputs.keys())
        random.shuffle(methods)
        labels = {m: chr(65 + i) for i, m in enumerate(methods)}  # A, B, C...
        
        # 用随机化标签呈现给评审
        prompt = f"Task: {task_prompt}\n\n"
        for method in methods:
            prompt += f"--- Proposal {labels[method]} ---\n{outputs[method]}\n\n"
        prompt += "Rank all proposals from best to worst. Format: RANKING: [best], [second], [worst]"
        
        ranking = call_judge(prompt)
        rankings.append({"labels": labels, "ranking": ranking})
    
    # 通过 Borda 计数汇总
    return compute_borda(rankings)

def compute_borda(rankings, n_methods=3):
    """Borda 计数：第 1/2/3 名得 3/2/1 分。"""
    scores = {}
    points = {0: n_methods, 1: n_methods - 1, 2: n_methods - 2}  # 按 n_methods 调整
    
    for r in rankings:
        for position, method in enumerate(r["ranking"]):
            scores[method] = scores.get(method, 0) + points.get(position, 0)
    
    return scores
```

关键设计决策：
- **每位评审同时随机化标签与顺序**，以防止位置偏差
- **使用奇数位评审**（3、5、7）以打破平局
- **保守平局裁决**：在位者/基线赢得平局（防止假阳性）
- **CoT 评审**以约 40% 成本匹敌非 CoT 质量（1 位 CoT 评审 ≈ 3 位标准评审）

### 代码/客观评估

对于有真值评估的任务（代码、数学、事实）：

```python
import subprocess

def evaluate_code(solution: str, test_cases: list, timeout: int = 30):
    """用沙箱化执行把代码解跑在测试用例上。"""
    results = {"public": [], "private": []}
    
    for test in test_cases:
        try:
            proc = subprocess.run(
                ["python3", "-c", solution],
                input=test["input"],
                capture_output=True,
                timeout=timeout,
                text=True
            )
            actual = proc.stdout.strip()
            expected = test["expected"].strip()
            passed = actual == expected
        except subprocess.TimeoutExpired:
            passed = False
        
        category = "public" if test.get("public") else "private"
        results[category].append(passed)
    
    return {
        "public_pass_rate": sum(results["public"]) / max(len(results["public"]), 1),
        "private_pass_rate": sum(results["private"]) / max(len(results["private"]), 1),
    }
```

### 算力对等比较

始终在同等算力预算下比较方法。如果你的方法用 N 次 API 调用，基线也得到 N 次调用：

| 方法 | 调用预算 | 分配 |
|--------|-----------|------------|
| 单次通过 | 6 次调用 | 6 次独立生成 |
| Critique & revise | 6 次调用 | 1 次生成 + 5 轮修订 |
| Autoreason | 6 次调用 | 1 次生成 + 1 次分析 + 4 次修订 |
| Best-of-N | 6 次调用 | 6 次独立生成，按公开测试挑最好 |

### 人工评估设计

许多 ML/NLP 论文需要人工评估，尤其是主观任务（文本生成、摘要、对话、创意写作）。设计不佳的人工评估是常见的拒稿原因。

#### 何时需要人工评估

| 任务类型 | 是否需要？ | 备注 |
|-----------|-----------|-------|
| 文本生成（开放式） | 是 | 仅用 LLM-as-judge 在 ACL/EMNLP 不足以被接收 |
| 摘要 | 通常需要 | 至少对一部分输出做 |
| 对话系统 | 是 | 用户研究或标注 |
| 代码生成 | 否 | 测试套件是客观真值 |
| 分类 | 否 | 标准指标即可 |
| 任何带主观质量的 | 强烈建议 | 能显著增强论文 |

#### 标注协议设计

```
人工评估协议：
1. 定义评估维度（流畅度、相关性、事实准确性等）
2. 编写标注指南，给出每个分级的示例
3. 用 2-3 名标注员在 20-30 个示例上做试点
4. 计算试点标注员间一致性 —— 若低，修订指南
5. 跑完整评估
6. 报告：标注员人数、一致性指标、报酬、每项耗时
```

**评估维度**（选取相关子集）：

| 维度 | 定义 | 量表 |
|-----------|-----------|-------|
| 流畅度 | 语法正确性与自然度 | 1-5 Likert |
| 相关性 | 是否切合任务？ | 1-5 Likert |
| 事实准确性 | 所述事实是否正确？ | 二值或 1-5 |
| 连贯性 | 逻辑流畅与一致性 | 1-5 Likert |
| 信息量 | 是否提供有用信息？ | 1-5 Likert |
| 整体偏好 | 哪个输出更好？ | A/B/平局（成对） |

**成对比较**（优先于绝对评分 —— 更可靠）：
- 并排呈现两个输出（左右位置随机化）
- 提问：「哪个更好？A / B / 平局」
- 更有区分力，且不易受标注员校准漂移影响

#### 标注员间一致性

务必报告一致性指标。没有它们，审稿人会认为你的标注不可靠。

```python
# Krippendorff's alpha（首选 —— 处理缺失数据，任意量表）
# pip install krippendorffs-alpha
import krippendorff

# 评分：行 = 标注员，列 = 条目，值 = 分数
ratings = [
    [3, 4, 1, 2, 5, None, 3],  # 标注员 1
    [3, 5, 1, 3, 5, 2, 3],     # 标注员 2
    [4, 4, 2, 2, 4, 2, None],  # 标注员 3
]
alpha = krippendorff.alpha(reliability_data=ratings, level_of_measurement="ordinal")
print(f"Krippendorff's alpha: {alpha:.3f}")
# 解读：>0.80 良好，0.67-0.80 可接受，<0.67 存疑
```

```python
# Cohen's kappa（恰好 2 名标注员、分类数据）
from sklearn.metrics import cohen_kappa_score

annotator_1 = [1, 2, 3, 1, 2, 3, 2]
annotator_2 = [1, 2, 2, 1, 3, 3, 2]
kappa = cohen_kappa_score(annotator_1, annotator_2)
print(f"Cohen's kappa: {kappa:.3f}")
# 解读：>0.80 优秀，0.60-0.80 充分，0.40-0.60 中等
```

| 指标 | 何时使用 | 标注员数 | 量表 |
|--------|------------|-----------|-------|
| Krippendorff's alpha | 默认选择 | 任意数量 | 任意（序数、名义、比率） |
| Cohen's kappa | 2 名标注员、分类 | 恰好 2 | 名义/序数 |
| Fleiss' kappa | 3+ 名标注员、分类 | 3+ | 名义 |
| Pearson/Spearman | 连续评分 | 2 | 区间/比率 |

#### 众包平台

| 平台 | 最适合 | 成本 | 质量 |
|----------|----------|------|---------|
| **Prolific** | 学术研究、质量较高 | $8-15/小时 | 高 —— 学术参与者池 |
| **MTurk** | 大规模、快速周转 | $2-10/小时 | 不一 —— 用资格筛选 |
| **Surge AI** | NLP 专用标注 | 高端 | 高 —— 受训标注员 |
| **专家标注员** | 领域专用（医疗、法律） | 最高 | 最高 —— 但慢 |

**伦理要求**：
- 报告报酬率（必须至少为当地最低工资）
- 如相关，描述标注员人口统计
- 如所在机构要求，取得 IRB/伦理审批
- ACL 系列会议明确要求报酬文档

#### 论文中应报告什么

```
人工评估章节检查清单：
- [ ] 标注员人数
- [ ] 标注员资格 / 招募方式
- [ ] 被评估的条目数
- [ ] 评估维度及其定义
- [ ] 所用量表（Likert、成对、二值）
- [ ] 标注员间一致性（Krippendorff's alpha 或 Cohen's kappa）
- [ ] 报酬率
- [ ] 每个标注条目耗时
- [ ] 标注员是否看到模型身份（应为盲评）
- [ ] 呈现顺序的随机化
```

---

## 统计分析

### 必需的检验

| 检验 | 何时使用 | Python |
|------|------------|--------|
| McNemar 检验 | 在相同问题上比较两种方法 | 小样本用 `scipy.stats.binomtest` |
| 双比例 z 检验 | 比较成功率 | 自写或 `statsmodels` |
| Fisher 精确检验 | 小样本成对比较 | `scipy.stats.fisher_exact` |
| 自助法 CI | 任意指标的置信区间 | 自写自助 |
| Cohen's h | 比例的效应量 | 手动计算 |

### 标准分析脚本

```python
import numpy as np
from scipy import stats
from pathlib import Path
import json

def load_all_results(results_dir):
    """把所有结果加载为结构化格式。"""
    results = {}
    for result_file in Path(results_dir).rglob("result.json"):
        parts = result_file.relative_to(results_dir).parts
        if len(parts) >= 3:
            experiment, task, strategy = parts[0], parts[1], parts[2]
            data = json.loads(result_file.read_text())
            results.setdefault(experiment, {}).setdefault(strategy, {})[task] = data
    return results

def pairwise_mcnemar(method_a_results, method_b_results):
    """配对二值结果的 McNemar 检验。"""
    a_win_b_lose = sum(1 for a, b in zip(method_a_results, method_b_results) if a and not b)
    b_win_a_lose = sum(1 for a, b in zip(method_a_results, method_b_results) if b and not a)
    
    n = a_win_b_lose + b_win_a_lose
    if n < 25:
        # 小样本用精确二项分布
        result = stats.binomtest(a_win_b_lose, n, 0.5)
        p_value = result.pvalue
    else:
        # 卡方近似
        chi2 = (abs(a_win_b_lose - b_win_a_lose) - 1)**2 / (a_win_b_lose + b_win_a_lose)
        p_value = 1 - stats.chi2.cdf(chi2, df=1)
    
    return {
        "a_wins": a_win_b_lose,
        "b_wins": b_win_a_lose,
        "n_discordant": n,
        "p_value": p_value,
        "significant": p_value < 0.05
    }

def bootstrap_ci(data, n_bootstrap=10000, ci=0.95):
    """均值的自助法置信区间。"""
    means = []
    for _ in range(n_bootstrap):
        sample = np.random.choice(data, size=len(data), replace=True)
        means.append(np.mean(sample))
    lower = np.percentile(means, (1 - ci) / 2 * 100)
    upper = np.percentile(means, (1 + ci) / 2 * 100)
    return {"mean": np.mean(data), "ci_lower": lower, "ci_upper": upper}

def cohens_h(p1, p2):
    """两个比例的 Cohen's h 效应量。"""
    return 2 * np.arcsin(np.sqrt(p1)) - 2 * np.arcsin(np.sqrt(p2))
```

### 报告标准

论文中始终包含：
- **样本量**：n=X 个问题/任务
- **运行次数**：如适用，K 次独立运行
- **误差棒**：注明是标准差还是标准误
- **置信区间**：关键结果的 95% CI
- **显著性检验**：关键比较的 p 值
- **效应量**：Cohen's d 或 h，体现实际显著性

---

## 监控（定时任务模式）

### 定时任务提示模板

为每个实验批次创建监控提示：

```
检查 [EXPERIMENT_NAME] 实验的状态：

1. 进程检查：ps aux | grep [PROCESS_PATTERN]
2. 日志检查：tail -30 [LOG_FILE]
3. 结果检查：ls [RESULT_DIR]/eval/（或合适的结果位置）
4. 若有结果可用：
   - 读取结果 JSON 文件
   - 用表格报告指标（Borda 分数、准确率等）
   - 计算方法间的关键比较
5. 若本批次所有实验已完成：
   - git add -A && git commit -m "[COMMIT_MESSAGE]" && git push
   - 报告最终总结
6. 关键问题：[具体的分析问题]

如果自上次检查以来没有任何变化，回复 [SILENT]。
```

### 监控最佳实践

1. **先检查进程** —— 实验仍在运行且结果不完整时，不要读结果
2. **读日志尾部** —— 找错误、进度指示、完成信息
3. **数已完成 vs 预期** ——「45/150 题完成」比「存在一些结果」更有用
4. **用结构化表格报告** —— 关键指标始终放进表格
5. **回答关键问题** —— 每个实验在完成时应回答一个具体的分析问题
6. **无新闻时用 [SILENT]** —— 没有变化时抑制通知
7. **完成即提交** —— 每个完成的批次都用描述性消息提交

### 监控报告示例

```
## 代码实验（Haiku 3.5）—— 已完成

| 策略 | 通过率（150 题） | vs 单次 |
|----------|------------------------|-----------|
| single_pass | 38.0% | — |
| critique_revise | 35.2% | -2.8pp |
| **autoreason** | **40.0%** | **+2.0pp** |
| best_of_6 | 31.0% | -7.0pp |

关键发现：autoreason 相对单次通过提升 +2pp，而
best-of_6 因单一公开测试选择问题而崩溃。

已提交：`git commit -m "Add Haiku code results (150 problems, 4 strategies)"`
下一步：对这些结果跑显著性检验。
```

---

## 失败恢复

### 常见失败与恢复

| 失败 | 检测 | 恢复 |
|---------|-----------|----------|
| **API 额度耗尽** | 日志出现 402 错误、结果不完整 | 充值额度，重跑（自动跳过已完成工作） |
| **限流** | 429 错误、进度缓慢 | 加带指数退避的重试逻辑 |
| **进程崩溃** | PID 消失、日志停在某个问题中途 | 重跑脚本（从最后检查点恢复） |
| **模型 ID 错误** | 模型未找到错误 | 修正 ID（如 `claude-opus-4-6` 而非 `claude-opus-4.6`） |
| **并行变慢** | 每个实验耗时变成 2 倍 | 把并行实验降到最多 2-3 个 |
| **安全扫描拦截** | 命令被安全机制拦截 | 用 `execute_code` 而非管道 `terminal` 命令 |
| **委派失败** | `delegate_task` 返回错误 | 回退为直接做工作 |
| **难题超时** | 进程卡住、日志无进展 | 杀掉、跳过该题、在结果中注明 |
| **数据集路径不匹配** | 文件未找到错误 | 启动前核实路径 |

### 重试命名约定

重跑失败实验时，用后缀跟踪轮次：

```
logs/experiment_haiku_0_50.log       # 第 1 轮
logs/experiment_haiku_0_50_r2.log    # 第 2 轮（额度耗尽后）
logs/experiment_haiku_0_50_r3.log    # 第 3 轮（修 bug 后）
```

### 起飞前检查清单

启动任何实验批次之前：

```
起飞前：
- [ ] API 额度足够覆盖估计的调用数
- [ ] 模型 ID 正确（先用 1 个问题测试）
- [ ] 输出目录存在且可写
- [ ] 恢复逻辑工作正常（重跑不会覆盖已有结果）
- [ ] 日志文件路径唯一（不会覆盖之前的日志）
- [ ] 数据集/任务文件可访问
- [ ] 配置与目标实验一致
```

---

## 任务/基准设计

### 开放式任务（主观评估）

设计目标清晰但质量主观的任务：

```markdown
# 任务：[标题]

## 背景
[带具体细节的场景：公司规模、约束、时间线]

## 交付物
[要求的精确格式和结构]

## 要求
- [具体、可衡量的要求]
- [不要含糊 ——「要全面」不好，「恰好包含 6 节」好]
```

### 受约束任务（用于测试范围效应）

受约束任务测试方法是否尊重范围边界。设计时用：

- **固定事实**：「只用这 N 个数据点，不要加别的」
- **固定交付物**：具体格式（路演、复盘、备忘 —— 而非「改进这个」）
- **固定结构**：「按此顺序排这些节，不要增删」
- **固定改动项**：「恰好回应这 N 点，不加别的」

**不要把字数当作范围约束。** 字数限制会造成假收敛 —— 输出因长度被拒，而非质量。约束范围（包含什么）而非长度。

### 示例：好约束 vs 坏约束

| 坏约束 | 原因 | 好约束 |
|---------------|-----|-----------------|
| 「最多 500 字」 | 评审因长度拒绝 | 「恰好 4 节，每节 3 个编号项」 |
| 「要简洁」 | 太模糊 | 「每条禁令必须引用一个具体的基本事实」 |
| 「改进这个」 | 范围无界 | 「用这个确切结构写一份 600 字事故复盘」 |
| 「让它更好」 | 无明确标准 | 「恰好回应这 3 条评审意见」 |

---

## 可视化最佳实践

### 设置：SciencePlots + matplotlib

安装 SciencePlots 以获得出版级默认值：

```bash
pip install SciencePlots matplotlib numpy
```

**选项 A：SciencePlots 样式**（推荐 —— 自动处理大多数默认值）：

```python
import matplotlib.pyplot as plt
import scienceplots  # 注册样式

# 选一个样式：
# 'science'        —— 干净、衬线字体，适合大多数会议
# 'science+ieee'   —— IEEE 风格（适合双栏论文）
# 'science+nature' —— Nature 风格
# 若生成图的机器上没装 LaTeX，加上 'no-latex'

with plt.style.context(['science', 'no-latex']):
    fig, ax = plt.subplots(figsize=(3.5, 2.5))  # 单栏宽度
    # ... 绘图 ...
    fig.savefig('paper/fig_results.pdf', bbox_inches='tight')
```

**选项 B：手动 rcParams**（需要完全控制时）：

```python
import matplotlib.pyplot as plt

plt.rcParams.update({
    'font.size': 10,
    'font.family': 'serif',
    'axes.labelsize': 11,
    'axes.titlesize': 11,
    'xtick.labelsize': 9,
    'ytick.labelsize': 9,
    'legend.fontsize': 9,
    'figure.figsize': (3.5, 2.5),    # 单栏默认
    'figure.dpi': 300,
    'savefig.dpi': 300,
    'savefig.bbox': 'tight',
    'savefig.pad_inches': 0.05,
    'axes.linewidth': 0.8,
    'lines.linewidth': 1.5,
    'lines.markersize': 5,
    'axes.grid': True,
    'grid.alpha': 0.3,
    'grid.linewidth': 0.5,
})
```

### 标准图尺寸（双栏格式）

| 用途 | figsize | 备注 |
|----------|---------|-------|
| 单栏 | `(3.5, 2.5)` | 放进双栏布局的一栏 |
| 双栏 | `(7.0, 3.0)` | 跨整页宽度 |
| 正方形（热力图、混淆矩阵） | `(3.5, 3.5)` | 单栏 |
| 高瘦单栏（行很多） | `(3.5, 5.0)` | 谨慎使用 |

### 色盲友好调色板（Okabe-Ito）

所有论文图都用这个调色板。它对所有常见色觉缺陷人群都可区分：

```python
COLORS = {
    'blue':    '#0072B2',
    'orange':  '#E69F00',
    'green':   '#009E73',
    'red':     '#D55E00',
    'purple':  '#CC79A7',
    'cyan':    '#56B4E9',
    'yellow':  '#F0E442',
    'black':   '#000000',
}

# 作为循环用列表：
COLOR_CYCLE = ['#0072B2', '#D55E00', '#009E73', '#E69F00', '#CC79A7', '#56B4E9']
```

还要用**标记和线型**区分线条，而不只是颜色：
```python
STYLES = [
    {'color': '#0072B2', 'marker': 'o', 'linestyle': '-'},
    {'color': '#D55E00', 'marker': 's', 'linestyle': '--'},
    {'color': '#009E73', 'marker': '^', 'linestyle': '-.'},
    {'color': '#E69F00', 'marker': 'D', 'linestyle': ':'},
]
```

### 完整示例：方法对比柱状图

```python
import matplotlib.pyplot as plt
import numpy as np

try:
    import scienceplots
    style = ['science', 'no-latex']
except ImportError:
    style = 'default'

with plt.style.context(style):
    methods = ['Single Pass', 'Critique+Revise', 'Best-of-N', 'Ours']
    scores = [73.2, 74.1, 68.5, 77.0]
    errors = [2.1, 1.8, 3.2, 1.5]
    colors = ['#56B4E9', '#E69F00', '#CC79A7', '#0072B2']
    
    fig, ax = plt.subplots(figsize=(3.5, 2.5))
    bars = ax.bar(methods, scores, yerr=errors, capsize=3,
                  color=colors, edgecolor='black', linewidth=0.5)
    
    # 突出 "Ours"
    bars[-1].set_edgecolor('#0072B2')
    bars[-1].set_linewidth(1.5)
    
    ax.set_ylabel('Pass Rate (%)')
    ax.set_ylim(60, 85)
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    
    fig.savefig('paper/fig_comparison.pdf', bbox_inches='tight')
```

### 完整示例：收敛/轨迹折线图

```python
with plt.style.context(style):
    fig, ax = plt.subplots(figsize=(3.5, 2.5))
    
    passes = np.arange(1, 16)
    ours = [65, 72, 78, 82, 85, 87, 88, 89, 89.5, 90, 90, 90, 90, 90, 90]
    baseline = [65, 68, 70, 71, 69, 67, 66, 65, 64, 63, 62, 61, 60, 59, 58]
    
    ax.plot(passes, ours, **STYLES[0], label='Ours', markersize=4)
    ax.plot(passes, baseline, **STYLES[1], label='Critique+Revise', markersize=4)
    
    # 标出收敛点
    ax.axvline(x=10, color='gray', linestyle=':', alpha=0.5, linewidth=0.8)
    ax.annotate('Converged', xy=(10, 90), fontsize=8, ha='center',
                xytext=(10, 93), arrowprops=dict(arrowstyle='->', color='gray'))
    
    ax.set_xlabel('Iteration')
    ax.set_ylabel('Quality Score')
    ax.legend(loc='lower right')
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    
    fig.savefig('paper/fig_trajectory.pdf', bbox_inches='tight')
```

### 输出规则

- **始终存为 PDF**：`fig.savefig('fig.pdf')` —— 矢量图，任意缩放都清晰
- **绝不存为 PNG** 做论文图 —— 栅格 PNG 在打印/缩放时显得模糊
- **例外**：截图、照片或像素艺术可视化 → 用 600 DPI 的 PNG
- **验证灰度**：打印成灰度 PDF，检查所有信息仍可见

### 常见比较的图表类型

| 比较类型 | 图表 | 备注 |
|----------------|-------|-------|
| 方法 vs 方法 | 分组柱状图 | 带误差棒 |
| 跨模型规模 | 带 CI 带的折线图 | 模型规模轴用对数刻度 |
| 消融研究 | 堆叠/分组柱状图 | 突出被移除的组件 |
| 轨迹/收敛 | 跨迭代的折线图 | 显示每轮的胜者 |
| 分任务细分 | 热力图或分组柱状图 | 显示跨任务的方差 |
