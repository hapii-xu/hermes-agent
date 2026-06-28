# Dogfood QA 报告

**目标：** {target_url}
**日期：** {date}
**范围：** {scope_description}
**测试者：** Hermes Agent（自动化探索式 QA）

---

## 执行摘要

| 严重性 | 数量 |
|----------|-------|
| 🔴 严重 | {critical_count} |
| 🟠 高 | {high_count} |
| 🟡 中 | {medium_count} |
| 🔵 低 | {low_count} |
| **总计** | **{total_count}** |

**总体评估：** {one_sentence_assessment}

---

## 问题

<!-- 为发现的每个问题重复此区块，按严重性排序（严重优先） -->

### 问题 #{issue_number}：{issue_title}

| 字段 | 值 |
|-------|-------|
| **严重性** | {severity} |
| **类别** | {category} |
| **URL** | {url_where_found} |

**描述：**
{detailed_description_of_the_issue}

**复现步骤：**
1. {step_1}
2. {step_2}
3. {step_3}

**预期行为：**
{what_should_happen}

**实际行为：**
{what_actually_happens}

**截图：**
MEDIA:{screenshot_path}

**控制台错误**（如适用）：
```
{console_error_output}
```

---

<!-- 每个问题区块结束 -->

## 问题摘要表

| # | 标题 | 严重性 | 类别 | URL |
|---|-------|----------|----------|-----|
| {n} | {title} | {severity} | {category} | {url} |

## 测试覆盖范围

### 已测试页面
- {list_of_pages_visited}

### 已测试功能
- {list_of_features_exercised}

### 未测试 / 超出范围
- {areas_not_covered_and_why}

### 阻碍
- {any_issues_that_prevented_testing_certain_areas}

---

## 备注

{any_additional_observations_or_recommendations}
