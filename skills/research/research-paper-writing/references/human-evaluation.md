# ML/AI 研究的人工评估指南

面向 ML/AI 论文中设计、执行和报告人工评估的综合指南。人工评估是许多 NLP、HCI 和对齐论文的主要证据，且越来越多 ML 会议也期望把它作为补充证据。

---

## 目录

- [何时需要人工评估](#何时需要人工评估)
- [研究设计](#研究设计)
- [标注指南](#标注指南)
- [平台与招募](#平台与招募)
- [质量控制](#质量控制)
- [一致性指标](#一致性指标)
- [人工评估的统计分析](#人工评估的统计分析)
- [报告要求](#报告要求)
- [IRB 与伦理](#irb-与伦理)
- [常见陷阱](#常见陷阱)

---

## 何时需要人工评估

| 场景 | 是否需要人工评估？ | 备注 |
|----------|---------------------|-------|
| 文本生成质量（流畅度、连贯性） | **是** | 自动指标（BLEU、ROUGE）与人工判断相关性差 |
| 生成文本的事实准确性 | **强烈建议** | 自动事实核查不可靠 |
| 安全性/毒性评估 | **细微情形需要** | 分类器会漏掉依赖上下文的伤害 |
| 两个系统之间的偏好 | **是** | 比较 LLM 输出最可靠的方法 |
| 摘要质量 | **是** | ROUGE 不能很好反映忠实度或相关性 |
| 任务完成度（UI、agent） | **是** | 用户研究是金标准 |
| 分类准确率 | **通常不需要** | 真值标签足够；人工评估增加成本却无洞察 |
| 困惑度或 loss 比较 | **不需要** | 自动指标就是正确的评估 |

---

## 研究设计

### 评估类型

| 类型 | 何时使用 | 优点 | 缺点 |
|------|-------------|------|------|
| **成对比较** | 比较两个系统 | 最可靠，最小化量表偏差 | 只比较成对，对系统数为平方级增长 |
| **Likert 量表**（1-5 或 1-7） | 给单个输出评分 | 易于汇总 | 主观锚定、量表压缩 |
| **排序** | 给 3+ 个系统排序 | 捕获完整偏好顺序 | 条目越多认知负担越大 |
| **Best-worst scaling** | 高效比较多个系统 | 比 Likert 更可靠，对条目数为线性 | 需谨慎挑选条目 |
| **二值判断** | 是/否决策（合语法？符合事实？） | 简单、一致性高 | 丢失细微差别 |
| **错误标注** | 识别具体错误类型 | 诊断信息丰富 | 昂贵，需要受训标注员 |

**对大多数 ML 论文的建议**：成对比较最站得住脚。审稿人很少质疑其有效性。Likert 量表则始终同时报告均值和分布。

### 样本量规划

**最小可行样本量：**

| 研究类型 | 最少条目数 | 最少标注员 | 备注 |
|------------|--------------|-------------------|-------|
| 成对比较 | 100 对 | 每对 3 人 | 在 p<0.05 下能检出约 10% 胜率差 |
| Likert 评分 | 100 条 | 每条 3 人 | 足以算出有意义的均值 |
| 排序 | 50 组 | 每组 3 人 | 每组包含所有被比较的系统 |
| 错误标注 | 200 条 | 每条 2 人 | 结构化方案预期一致性更高 |

**功效分析**（用于更精确地规划）：

```python
from scipy import stats
import numpy as np

def sample_size_pairwise(effect_size=0.10, alpha=0.05, power=0.80):
    """
    估算成对比较的样本量（符号检验）。
    effect_size: 相对 0.50 的预期胜率差
    """
    p_expected = 0.50 + effect_size
    # 二项分布的正态近似
    z_alpha = stats.norm.ppf(1 - alpha / 2)
    z_beta = stats.norm.ppf(power)
    n = ((z_alpha * np.sqrt(0.25) + z_beta * np.sqrt(p_expected * (1 - p_expected))) ** 2) / (effect_size ** 2)
    return int(np.ceil(n))

print(f"Sample size for 10% effect: {sample_size_pairwise(0.10)}")  # ~200
print(f"Sample size for 15% effect: {sample_size_pairwise(0.15)}")  # ~90
print(f"Sample size for 20% effect: {sample_size_pairwise(0.20)}")  # ~50
```

### 控制偏差

| 偏差 | 缓解 |
|------|-----------|
| **顺序偏差**（偏好第一条） | 每位标注员随机化呈现顺序 |
| **长度偏差**（越长越好） | 控制长度或单独分析 |
| **锚定**（第一条标注设定量表） | 加入热身条目（不计分） |
| **疲劳**（质量随时间下降） | 限制单次时长（最多 30-45 分钟），随机化条目顺序 |
| **标注员专业度** | 报告标注员背景；使用资格任务 |

---

## 标注指南

写得好的标注指南是评估质量最大的单一影响因素。在此投入大量时间。

### 好指南的结构

```markdown
# [任务名] 标注指南

## 概述
[1-2 句描述任务]

## 定义
[定义标注员在判断中会用到的每个术语]
- 质量：[本研究的具体定义]
- 流畅度：[具体定义]
- 事实性：[具体定义]

## 评分量表
[对每个量表点，提供：]
- 数值
- 标签（如「优秀」「良好」「尚可」「差」「不可接受」）
- 符合该评分的定义
- 1-2 个该等级的具体示例

## 示例

### 示例 1：[评分 = 5]
输入：[确切输入]
输出：[确切输出]
评分：5
解释：[为何这是 5]

### 示例 2：[评分 = 2]
输入：[确切输入]
输出：[确切输出]
评分：2
解释：[为何这是 2]

[每个评分等级至少 2 个示例，覆盖边界情况]

## 边界情况
- 若输出是 [模糊情况]：[指引]
- 若输入是 [异常情况]：[指引]

## 常见错误
- 不要 [常见标注错误]
- 不要让 [偏差] 影响你的评分
```

### 试点测试

在完整研究**之前始终跑试点**：
1. 3-5 名标注员、20-30 个条目
2. 计算一致性指标
3. 在小组会上讨论分歧
4. 根据混淆点修订指南
5. 若一致性差（kappa <0.40），跑第二次试点

---

## 平台与招募

| 平台 | 最适合 | 成本 | 质量 |
|----------|----------|------|---------|
| **Prolific** | 通用标注、问卷 | $8-15/小时 | 高（学术导向的池子） |
| **Amazon MTurk** | 大规模简单任务 | $5-12/小时 | 不一（需强质控） |
| **Surge AI** | NLP 专用标注 | $15-25/小时 | 很高（受训标注员） |
| **Scale AI** | 生产级标注 | 不定 | 高（托管劳动力） |
| **内部团队** | 需要领域专长 | 不定 | 专用任务最高 |
| **Upwork/承包商** | 长期标注项目 | $10-30/小时 | 取决于招聘 |

**公平报酬**：始终至少按标注员所在地的当地最低工资等价支付。许多会议（尤其是 ACL）现在会询问标注员报酬。低于最低工资支付是伦理风险。

**Prolific 设置（大多数 ML 论文推荐）：**
1. 在 prolific.co 创建研究
2. 设置预筛过滤器（语言、国家、通过率 >95%）
3. 由试点估出每任务耗时 → 设定公平报酬
4. 用 Prolific 自带的注意力检查或加自己的
5. 收集 Prolific ID 用于质量跟踪（但不要在论文中分享）

---

## 质量控制

### 注意力检查

包含正确答案毫无歧义的条目：

```python
# 注意力检查的类型
attention_checks = {
    "instructed_response": "For this item, please select 'Strongly Agree' regardless of content.",
    "obvious_quality": "Rate this clearly ungrammatical text: 'The cat dog house green yesterday.'",  # 应得最低分
    "gold_standard": "Items where expert consensus exists (pre-annotated by authors)",
    "trap_question": "What color is the sky on a clear day? (embedded in annotation interface)"
}

# 推荐：总条目的 10-15% 应为检查项
# 排除标准：未通过 2+ 项注意力检查 → 排除该标注员
```

### 标注员资格

对于需要专长的任务：

```
资格任务设计：
1. 创建一组 20-30 个带已知正确标签的条目
2. 要求标注员在主任务前完成它
3. 设阈值：与金标签一致率 ≥80% 才合格
4. 记录资格分数用于报告
```

### 收集期间的监控

```python
# 实时质量监控
def monitor_quality(annotations):
    """在收集过程中检查标注质量问题。"""
    issues = []
    
    # 1. 检查是否一路选同一项（所有答案都一样）
    for annotator_id, items in annotations.groupby('annotator'):
        if items['rating'].nunique() <= 1:
            issues.append(f"Annotator {annotator_id}: straight-lining detected")
    
    # 2. 检查每条耗时（太快 = 没读）
    median_time = annotations['time_seconds'].median()
    fast_annotators = annotations.groupby('annotator')['time_seconds'].median()
    for ann_id, time in fast_annotators.items():
        if time < median_time * 0.3:
            issues.append(f"Annotator {ann_id}: suspiciously fast ({time:.0f}s vs median {median_time:.0f}s)")
    
    # 3. 检查注意力检查表现
    checks = annotations[annotations['is_attention_check']]
    for ann_id, items in checks.groupby('annotator'):
        accuracy = (items['rating'] == items['gold_rating']).mean()
        if accuracy < 0.80:
            issues.append(f"Annotator {ann_id}: failing attention checks ({accuracy:.0%})")
    
    return issues
```

---

## 一致性指标

### 用哪个指标

| 指标 | 何时使用 | 解读 |
|--------|-------------|---------------|
| **Cohen's kappa (κ)** | 恰好 2 名标注员、分类 | 校正偶然一致后的一致性 |
| **Fleiss' kappa** | 3+ 名标注员、都评相同条目、分类 | Cohen's 的多标注员扩展 |
| **Krippendorff's alpha (α)** | 任意数量标注员、处理缺失数据 | 最通用；推荐默认 |
| **ICC（组内相关系数）** | 连续评分（Likert） | 评分者间的一致性 |
| **百分比一致性** | 与 kappa/alpha 并列报告 | 原始一致性（未校正偶然） |
| **Kendall's W** | 排序 | 排序者间的一致性 |

**始终至少报告两个**：一个校正偶然的指标（kappa 或 alpha）以及原始百分比一致性。

### 解读指南

| 数值 | Krippendorff's α / Cohen's κ | 质量 |
|-------|-------------------------------|---------|
| > 0.80 | 一致性极好 | 对大多数用途可靠 |
| 0.67 - 0.80 | 一致性良好 | 多数 ML 论文可接受 |
| 0.40 - 0.67 | 一致性中等 | 勉强；需在论文中讨论 |
| < 0.40 | 一致性差 | 修订指南并重做标注 |

**注**：Krippendorff 推荐 α > 0.667 作为得出初步结论的最低值。带主观判断的 NLP 任务（流畅度、有用性）通常在 0.40-0.70。

### 实现

```python
import numpy as np
from sklearn.metrics import cohen_kappa_score
import krippendorff  # pip install krippendorff

def compute_agreement(annotations_matrix):
    """
    annotations_matrix: shape (n_items, n_annotators)
    值：评分（int 或 float）。缺失用 np.nan。
    """
    results = {}
    
    # Krippendorff's alpha（处理缺失数据、任意数量标注员）
    results['krippendorff_alpha'] = krippendorff.alpha(
        annotations_matrix.T,  # krippendorff 期望 (annotators, items)
        level_of_measurement='ordinal'  # 或 'nominal'、'interval'、'ratio'
    )
    
    # 两两 Cohen's kappa（每次取 2 名标注员）
    n_annotators = annotations_matrix.shape[1]
    kappas = []
    for i in range(n_annotators):
        for j in range(i + 1, n_annotators):
            mask = ~np.isnan(annotations_matrix[:, i]) & ~np.isnan(annotations_matrix[:, j])
            if mask.sum() > 0:
                k = cohen_kappa_score(
                    annotations_matrix[mask, i].astype(int),
                    annotations_matrix[mask, j].astype(int)
                )
                kappas.append(k)
    results['mean_pairwise_kappa'] = np.mean(kappas) if kappas else None
    
    # 原始百分比一致性
    agree_count = 0
    total_count = 0
    for item in range(annotations_matrix.shape[0]):
        ratings = annotations_matrix[item, ~np.isnan(annotations_matrix[item, :])]
        if len(ratings) >= 2:
            # 所有标注员一致
            if len(set(ratings.astype(int))) == 1:
                agree_count += 1
            total_count += 1
    results['percent_agreement'] = agree_count / total_count if total_count > 0 else None
    
    return results
```

---

## 人工评估的统计分析

### 成对比较

```python
from scipy import stats

def analyze_pairwise(wins_a, wins_b, ties=0):
    """
    分析成对比较结果。
    wins_a: 系统 A 获胜的次数
    wins_b: 系统 B 获胜的次数
    ties: 平局数（从符号检验中排除）
    """
    n = wins_a + wins_b  # 排除平局
    
    # 符号检验（精确二项分布）
    p_value = stats.binom_test(wins_a, n, 0.5, alternative='two-sided')
    
    # 带置信区间 95% 的胜率（Wilson score interval）
    win_rate = wins_a / n if n > 0 else 0.5
    z = 1.96
    denominator = 1 + z**2 / n
    center = (win_rate + z**2 / (2 * n)) / denominator
    margin = z * np.sqrt((win_rate * (1 - win_rate) + z**2 / (4 * n)) / n) / denominator
    ci_lower = center - margin
    ci_upper = center + margin
    
    return {
        'win_rate_a': win_rate,
        'win_rate_b': 1 - win_rate,
        'p_value': p_value,
        'ci_95': (ci_lower, ci_upper),
        'significant': p_value < 0.05,
        'n_comparisons': n,
        'ties': ties,
    }
```

### Likert 量表分析

```python
def analyze_likert(ratings_a, ratings_b):
    """比较两个系统的 Likert 评分（配对）。"""
    # Wilcoxon 符号秩检验（非参数、配对）
    stat, p_value = stats.wilcoxon(ratings_a, ratings_b, alternative='two-sided')
    
    # 效应量（秩二列相关）
    n = len(ratings_a)
    r = 1 - (2 * stat) / (n * (n + 1))
    
    return {
        'mean_a': np.mean(ratings_a),
        'mean_b': np.mean(ratings_b),
        'std_a': np.std(ratings_a),
        'std_b': np.std(ratings_b),
        'wilcoxon_stat': stat,
        'p_value': p_value,
        'effect_size_r': r,
        'significant': p_value < 0.05,
    }
```

### 多重比较校正

比较两个以上系统时：

```python
from statsmodels.stats.multitest import multipletests

# 在为所有两两计算出 p 值之后
p_values = [0.03, 0.001, 0.08, 0.04, 0.15, 0.002]
rejected, corrected_p, _, _ = multipletests(p_values, method='holm')
# 在论文中使用校正后的 p 值
```

---

## 报告要求

NLP 会议（ACL、EMNLP、NAACL）的审稿人会检查以下全部。ML 会议（NeurIPS、ICML）也越来越期望这些。

### 必须报告的内容

```latex
% 在论文的人工评估章节中：
\paragraph{Annotators.} We recruited [N] annotators via [platform].
[Describe qualifications or screening.] Annotators were paid
\$[X]/hour, above the [country] minimum wage.

\paragraph{Agreement.} Inter-annotator agreement was [metric] = [value]
(Krippendorff's $\alpha$ = [value]; raw agreement = [value]\%).
[If low: explain why the task is subjective and how you handle disagreements.]

\paragraph{Evaluation Protocol.} Each [item type] was rated by [N]
annotators on a [scale description]. We collected [total] annotations
across [N items]. [Describe randomization and blinding.]
```

### 放进附录的内容

```
附录：人工评估详情
- 完整标注指南（原文照录）
- 标注界面截图
- 资格任务详情与阈值
- 注意力检查条目与失败率
- 按标注员逐个的一致性分解
- 完整结果表（不只是均值）
- 报酬计算
- IRB 批准号（如适用）
```

---

## IRB 与伦理

### 何时需要 IRB 批准

| 情形 | 是否需要 IRB？ |
|-----------|---------------|
| 众包工评文本质量 | **通常不需要**（在多数机构不属于「人类受试者研究」） |
| 对真实用户的用户研究 | **需要**（多数美/欧机构） |
| 收集个人信息 | **需要** |
| 研究标注员行为/认知 | **需要**（他们成为研究对象） |
| 使用已有标注数据 | **通常不需要**（二次数据分析） |

**查你所在机构的政策。**「人类受试者研究」的定义各不相同。有疑问时，提交 IRB 协议 —— 对低风险研究审查通常很快。

### 人工评估伦理检查清单

```
- [ ] 告知标注员任务目的（不欺骗）
- [ ] 标注员可随时退出而不受惩罚
- [ ] 除平台 ID 外不收集个人可识别信息
- [ ] 被评估内容不让标注员暴露于伤害
  （若是：内容警告 + 可退出 + 更高报酬）
- [ ] 公平报酬（>= 等价当地最低工资）
- [ ] 数据安全存储，仅限研究团队访问
- [ ] 如机构要求，已获 IRB 批准
```

---

## 常见陷阱

| 陷阱 | 问题 | 修复 |
|---------|---------|-----|
| 标注员太少（1-2 人） | 无法计算一致性指标 | 每条至少 3 名标注员 |
| 没有注意力检查 | 无法发现低质量标注 | 加入 10-15% 注意力检查 |
| 不报告报酬 | 审稿人标记为伦理问题 | 始终报告时薪 |
| 生成任务只用自动指标 | 审稿人会要求人工评估 | 至少加入成对比较 |
| 不做指南试点 | 一致性低、预算浪费 | 始终先与 3-5 人试点 |
| 只报告均值 | 掩盖标注员分歧 | 报告分布与一致性 |
| 不控制顺序/位置 | 位置偏差抬高结果 | 随机化呈现顺序 |
| 把标注员一致性等同于真值 | 高一致性不代表正确 | 用专家判断验证 |
