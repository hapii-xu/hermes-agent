---
name: dogfood
description: "Web 应用的探索式 QA：发现 bug、收集证据、生成报告。"
version: 1.0.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [qa, testing, browser, web, dogfood]
    related_skills: []
---

# Dogfood：系统化的 Web 应用 QA 测试

## 概述

本技能指导你使用浏览器工具集对 Web 应用进行系统化的探索式 QA 测试。你将浏览应用、与元素交互、捕获问题的证据，并生成结构化的 bug 报告。

## 前置条件

- 必须具备浏览器工具集（`browser_navigate`、`browser_snapshot`、`browser_click`、`browser_type`、`browser_vision`、`browser_console`、`browser_scroll`、`browser_back`、`browser_press`）
- 用户提供的目标 URL 和测试范围

## 输入

用户提供：
1. **目标 URL** —— 测试的入口点
2. **范围** —— 重点关注哪些区域/功能（或"全站"以进行全面测试）
3. **输出目录**（可选）—— 保存截图和报告的位置（默认：`./dogfood-output`）

## 工作流程

遵循以下 5 阶段的系统化工作流程：

### 阶段 1：规划

1. 创建输出目录结构：
   ```
   {output_dir}/
   ├── screenshots/       # 证据截图
   └── report.md          # 最终报告（在阶段 5 生成）
   ```
2. 根据用户输入确定测试范围。
3. 通过规划要测试哪些页面和功能来构建粗略的站点地图：
   - 落地/主页
   - 导航链接（头部、页脚、侧边栏）
   - 关键用户流程（注册、登录、搜索、结账等）
   - 表单和交互元素
   - 边缘情况（空状态、错误页面、404）

### 阶段 2：探索

对于计划中的每个页面或功能：

1. **导航**到页面：
   ```
   browser_navigate(url="https://example.com/page")
   ```

2. **拍取快照**以理解 DOM 结构：
   ```
   browser_snapshot()
   ```

3. **检查控制台**是否有 JavaScript 错误：
   ```
   browser_console(clear=true)
   ```
   在每次导航之后和每次重要交互之后都这样做。静默的 JS 错误是高价值的发现。

4. **拍取带注释的截图**以视觉评估页面并识别交互元素：
   ```
   browser_vision(question="描述页面布局，识别任何视觉问题、损坏的元素或无障碍问题", annotate=true)
   ```
   `annotate=true` 标志在交互元素上叠加编号的 `[N]` 标签。每个 `[N]` 映射到 ref `@eN`，用于后续浏览器命令。

5. **系统化地测试交互元素**：
   - 点击按钮和链接：`browser_click(ref="@eN")`
   - 填写表单：`browser_type(ref="@eN", text="test input")`
   - 测试键盘导航：`browser_press(key="Tab")`、`browser_press(key="Enter")`
   - 滚动浏览内容：`browser_scroll(direction="down")`
   - 用无效输入测试表单验证
   - 测试空提交

6. **每次交互之后**，检查：
   - 控制台错误：`browser_console()`
   - 视觉变化：`browser_vision(question="交互后发生了什么变化？")`
   - 预期行为与实际行为

### 阶段 3：收集证据

对于发现的每个问题：

1. **拍取截图**展示问题：
   ```
   browser_vision(question="捕获并描述此页面上可见的问题", annotate=false)
   ```
   保存响应中的 `screenshot_path` —— 你将在报告中引用它。

2. **记录细节**：
   - 问题出现的 URL
   - 复现步骤
   - 预期行为
   - 实际行为
   - 控制台错误（如有）
   - 截图路径

3. **使用问题分类法对问题进行分类**（见 `references/issue-taxonomy.md`）：
   - 严重性：严重 / 高 / 中 / 低
   - 类别：功能 / 视觉 / 无障碍 / 控制台 / UX / 内容

### 阶段 4：分类

1. 审查所有收集的问题。
2. 去重 —— 合并那些是同一 bug 在不同位置表现的问题。
3. 为每个问题分配最终的严重性和类别。
4. 按严重性排序（严重优先，然后是高、中、低）。
5. 按严重性和类别统计问题，用于执行摘要。

### 阶段 5：报告

使用 `templates/dogfood-report-template.md` 中的模板生成最终报告。

报告必须包含：
1. **执行摘要**，包含问题总数、按严重性细分和测试范围
2. **每个问题的区块**，包含：
   - 问题编号和标题
   - 严重性和类别徽章
   - 观察到的 URL
   - 问题描述
   - 复现步骤
   - 预期与实际行为
   - 截图引用（使用 `MEDIA:<screenshot_path>` 用于行内图片）
   - 相关的控制台错误
3. 所有问题的**摘要表**
4. **测试说明** —— 测试了什么、未测试什么、任何阻碍

将报告保存到 `{output_dir}/report.md`。

## 工具参考

| 工具 | 用途 |
|------|---------|
| `browser_navigate` | 前往某个 URL |
| `browser_snapshot` | 获取 DOM 文本快照（无障碍树） |
| `browser_click` | 通过 ref（`@eN`）或文字点击元素 |
| `browser_type` | 在输入字段中键入 |
| `browser_scroll` | 在页面上上下滚动 |
| `browser_back` | 在浏览器历史中后退 |
| `browser_press` | 按下键盘按键 |
| `browser_vision` | 截图 + AI 分析；使用 `annotate=true` 获取元素标签 |
| `browser_console` | 获取 JS 控制台输出和错误 |

## 提示

- **总是在导航之后和重要交互之后检查 `browser_console()`。** 静默的 JS 错误是最有价值的发现之一。
- **当需要推理交互元素位置或快照 ref 不清晰时，使用 `browser_vision` 的 `annotate=true`**。
- **同时用有效和无效输入测试** —— 表单验证 bug 很常见。
- **滚动浏览长页面** —— 折叠下方的内容可能有渲染问题。
- **测试导航流程** —— 端到端地点击多步骤流程。
- 通过注意截图中可见的任何布局问题来**检查响应式行为**。
- **不要忘记边缘情况**：空状态、超长文字、特殊字符、快速连续点击。
- 向用户报告截图时，包含 `MEDIA:<screenshot_path>`，以便他们能行内查看证据。
