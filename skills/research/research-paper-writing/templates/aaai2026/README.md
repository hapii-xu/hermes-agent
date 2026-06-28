# AAAI 2026 统一LaTeX模板使用说明 / AAAI 2026 统一 LaTeX 模板指南

> **📝 重要说明 / 重要说明**：本仓库借助 Cursor 在 AAAI 2026 官方模板基础上改进得到。如果遇到不满足或有冲突的情况，请积极提 issues。
>
> **📝 重要说明**：本仓库借助 Cursor 在 AAAI 2026 官方模板基础上改进得到。如果遇到任何问题或冲突，请积极提交 issues。

[中文](#中文版本) | [英文](#英文版本)

---

## 🌐 在线查看 / 在线访问

**📖 在线阅读和测试模板**：[https://cn.overleaf.com/read/wyhcnvcrtpyt#cd4a07](https://cn.overleaf.com/read/wyhcnvcrtpyt#cd4a07)

💡 **提示 / 小贴士**：
- 中文：您可以通过上述链接在 Overleaf 中直接查看、编辑和编译模板，无需本地安装 LaTeX 环境
- 英文：您可以通过上述链接在 Overleaf 中直接查看、编辑和编译模板，无需本地安装 LaTeX 环境

---

## 中文版本

### 概述 ✅

我已经将 AAAI 2026 的两个版本（匿名投稿版本和 camera-ready 版本）**完整合并**成一个统一的模板文件 `aaai2026-unified-template.tex`。

该模板包含了原始两个模板的**所有完整内容**（共 886 行，比原始文件更全面），包括：
- 所有格式化说明和要求
- 完整的示例代码和表格
- 图片处理指南
- 参考文献格式要求
- 所有章节和附录内容
- 版本特定的 Acknowledgments 部分

### 主要差异分析

通过比较原始的两个模板，我发现主要差异在于：

#### 1. 包的加载方式
- **匿名版本**：`\usepackage[submission]{aaai2026}`
- **Camera-ready 版本**：`\usepackage{aaai2026}`

#### 2. 标题差异
- **匿名版本**："AAAI Press Anonymous Submission Instructions for Authors Using LaTeX"
- **Camera-ready 版本**："AAAI Press Formatting Instructions for Authors Using LaTeX --- A Guide"

#### 3. Links 环境的处理
- **匿名版本**：Links 环境被注释掉，防止泄露作者身份
- **Camera-ready 版本**：Links 环境正常显示

#### 4. 内容部分差异
- **匿名版本**：包含 "Preparing an Anonymous Submission" 部分的特殊说明
- **Camera-ready 版本**：包含完整的格式说明和版权信息

### 依赖文件检查结果

✅ **已验证并复制到主目录的文件**：

- `aaai2026.sty` - AAAI 2026 样式文件（两个版本完全相同）
- `aaai2026.bst` - 参考文献样式文件（两个版本完全相同）
- `aaai2026.bib` - 示例参考文献文件
- `figure1.pdf` 和 `figure2.pdf` - 示例图片文件

所有这些文件在两个版本中都是相同的，因此统一模板可以正常工作。

### 如何使用统一模板

#### 切换到匿名投稿版本
在模板文件第 11 行，**取消注释**这一行：
```latex
\def\aaaianonymous{true}
```

#### 切换到 Camera-ready 版本
在模板文件第 11 行，**注释掉**或**删除**这一行：
```latex
% \def\aaaianonymous{true}
```

### 一键切换的核心机制

统一模板使用了 LaTeX 的条件编译功能：

```latex
% 条件包加载
\ifdefined\aaaianonymous
    \usepackage[submission]{aaai2026}  % 匿名版本
\else
    \usepackage{aaai2026}              % Camera-ready 版本
\fi

% 条件标题设置
\ifdefined\aaaianonymous
    \title{AAAI Press Anonymous Submission\\Instructions for Authors Using \LaTeX{}}
\else
    \title{AAAI Press Formatting Instructions \\for Authors Using \LaTeX{} --- A Guide}
\fi

% 条件内容显示
\ifdefined\aaaianonymous
    % 匿名版本特有内容
\else
    % Camera-ready 版本特有内容
\fi
```

### 文件清单

主目录现在包含以下文件：

- `aaai2026-unified-template.tex` - 统一主论文模板文件
- `aaai2026-unified-supp.tex` - 统一补充材料模板文件
- `aaai2026.sty` - AAAI 2026 LaTeX 样式文件
- `aaai2026.bst` - 参考文献样式文件
- `aaai2026.bib` - 示例参考文献文件
- `figure1.pdf` - 示例图片 1
- `figure2.pdf` - 示例图片 2
- `README.md` - 本说明文档

### 补充材料模板 (Supplementary Material Template)

#### 概述
`aaai2026-unified-supp.tex` 是专门为 AAAI 2026 补充材料设计的统一模板，与主论文模板使用相同的版本切换机制。

#### 主要功能
- **版本切换**：通过修改一行代码在匿名投稿和 camera-ready 版本间切换
- **补充内容支持**：支持额外的实验、推导、数据、图表、算法等
- **格式一致性**：与主论文模板保持完全一致的格式要求
- **代码示例**：包含算法、代码列表等补充材料的示例

#### 使用方法
与主论文模板相同，只需修改第 11 行：
```latex
% 匿名投稿版本
\def\aaaianonymous{true}

% Camera-ready 版本
% \def\aaaianonymous{true}
```

#### 补充材料内容建议
- 额外的实验结果和消融研究
- 详细的数学推导和证明
- 更多的图表和可视化
- 算法伪代码和实现细节
- 数据集描述和预处理步骤
- 超参数设置和实验配置
- 失败案例分析
- 计算复杂度分析

### 使用检查清单 (Usage Checklist)

#### 📋 投稿前检查清单 (Pre-Submission Checklist)

**版本设置**：
- [ ] 已设置 `\def\aaaianonymous{true}`（匿名投稿）
- [ ] 已注释掉所有可能暴露身份的信息
- [ ] 已匿名化参考文献（移除作者姓名）

**内容完整性**：
- [ ] 标题、摘要、关键词已填写
- [ ] 所有章节内容完整
- [ ] 图表编号连续且正确
- [ ] 参考文献格式正确
- [ ] 补充材料（如有）已准备

**格式检查**：
- [ ] 页面边距符合要求
- [ ] 字体和字号正确
- [ ] 行间距符合标准
- [ ] 图表位置和大小合适
- [ ] 数学公式格式正确

**技术检查**：
- [ ] LaTeX 编译无错误
- [ ] 参考文献正确生成
- [ ] PDF 输出正常
- [ ] 文件大小在限制范围内

#### 📋 录用后检查清单 (Post-Acceptance Checklist)

**版本切换**：
- [ ] 已注释掉 `\def\aaaianonymous{true}`（camera-ready）
- [ ] 已添加完整的作者信息
- [ ] 已添加所有作者单位信息
- [ ] 已恢复所有被注释的内容

**内容更新**：
- [ ] 已根据审稿意见修改内容
- [ ] 已更新所有图表和实验
- [ ] 已完善补充材料
- [ ] 已检查所有链接和引用

**最终检查**：
- [ ] 最终 PDF 质量检查
- [ ] 所有文件已备份
- [ ] 符合会议最终提交要求
- [ ] 补充材料已单独提交（如需要）

#### 📋 补充材料检查清单 (Supplementary Material Checklist)

**内容组织**：
- [ ] 补充材料与主论文内容对应
- [ ] 章节结构清晰合理
- [ ] 图表编号与主论文不冲突
- [ ] 参考文献格式一致

**技术细节**：
- [ ] 算法伪代码清晰完整
- [ ] 实验设置详细说明
- [ ] 数据预处理步骤明确
- [ ] 超参数配置完整

**格式要求**：
- [ ] 使用统一的 supp 模板
- [ ] 页面设置与主论文一致
- [ ] 字体和格式符合要求
- [ ] 文件大小在限制范围内

### 实际使用建议

1. **投稿阶段**：
   - 取消注释 `\def\aaaianonymous{true}`
   - 确保不包含任何可能暴露身份的信息
   - 检查参考文献是否已匿名化

2. **录用后准备 final 版本**：
   - 注释掉或删除 `\def\aaaianonymous{true}` 这一行
   - 添加完整的作者信息和 affiliations
   - 取消注释 links 环境（如果需要）

3. **编译测试**：
   - 分别在两种模式下编译，确保都能正常工作
   - 检查输出的 PDF 是否符合要求
   - 验证参考文献格式是否正确

4. **依赖文件确认**：
   - 确保所有依赖文件都在同一目录下
   - 如果移动模板文件，记得同时移动依赖文件

### 重要注意事项

⚠️ **关于 Bibliography Style**：
- `aaai2026.sty` 文件已经自动设置了 `\bibliographystyle{aaai2026}`
- **不要**在文档中再次添加 `\bibliographystyle{aaai2026}` 命令
- 否则会出现 "`Illegal, another \bibstyle command`" 错误
- 只需要使用 `\bibliography{aaai2026}` 命令即可

### 编译命令示例

```bash
# 编译 LaTeX 文档
pdflatex aaai2026-unified-template.tex
bibtex aaai2026-unified-template
pdflatex aaai2026-unified-template.tex
pdflatex aaai2026-unified-template.tex
```

### 常见问题解决

#### 1. "Illegal, another \bibstyle command" 错误
**原因**：重复设置了 bibliography style
**解决方案**：删除文档中的 `\bibliographystyle{aaai2026}` 命令，`aaai2026.sty` 会自动处理

#### 2. 参考文献格式不正确
**原因**：可能缺少 natbib 包或者 BibTeX 文件问题
**解决方案**：确保按照标准的 LaTeX 编译流程：pdflatex → bibtex → pdflatex → pdflatex

---

## 英文版本

### 概述 ✅

我已经将 AAAI 2026 的两个版本（匿名投稿和 camera-ready）**完整合并**成单一的统一模板文件 `aaai2026-unified-template.tex`。

该模板包含了两个原始模板的**全部完整内容**（共 886 行，比原始文件更全面），包括：
- 所有格式化说明和要求
- 完整的示例代码和表格
- 图片处理指南
- 参考文献格式要求
- 所有章节和附录内容
- 版本特定的 Acknowledgments 章节

### 主要差异分析

通过比较两个原始模板，主要差异在于：

#### 1. 包加载方式
- **匿名版本**：`\usepackage[submission]{aaai2026}`
- **Camera-ready 版本**：`\usepackage{aaai2026}`

#### 2. 标题差异
- **匿名版本**："AAAI Press Anonymous Submission Instructions for Authors Using LaTeX"
- **Camera-ready 版本**："AAAI Press Formatting Instructions for Authors Using LaTeX --- A Guide"

#### 3. Links 环境处理
- **匿名版本**：Links 环境被注释掉，防止泄露身份
- **Camera-ready 版本**：Links 环境正常显示

#### 4. 内容章节差异
- **匿名版本**：包含 "Preparing an Anonymous Submission" 章节中的特殊说明
- **Camera-ready 版本**：包含完整的格式说明和版权信息

### 依赖文件验证

✅ **已验证并复制到主目录的文件**：

- `aaai2026.sty` - AAAI 2026 样式文件（两个版本中完全相同）
- `aaai2026.bst` - 参考文献样式文件（两个版本中完全相同）
- `aaai2026.bib` - 示例参考文献文件
- `figure1.pdf` 和 `figure2.pdf` - 示例图片文件

所有这些文件在两个版本中都相同，因此统一模板可以正常工作。

### 如何使用统一模板

#### 切换到匿名投稿版本
在模板文件的第 11 行，**取消注释**这一行：
```latex
\def\aaaianonymous{true}
```

#### 切换到 Camera-ready 版本
在模板文件的第 11 行，**注释掉**或**删除**这一行：
```latex
% \def\aaaianonymous{true}
```

### 一键切换的核心机制

统一模板使用 LaTeX 的条件编译：

```latex
% 条件包加载
\ifdefined\aaaianonymous
    \usepackage[submission]{aaai2026}  % 匿名版本
\else
    \usepackage{aaai2026}              % Camera-ready 版本
\fi

% 条件标题设置
\ifdefined\aaaianonymous
    \title{AAAI Press Anonymous Submission\\Instructions for Authors Using \LaTeX{}}
\else
    \title{AAAI Press Formatting Instructions \\for Authors Using \LaTeX{} --- A Guide}
\fi

% 条件内容显示
\ifdefined\aaaianonymous
    % 匿名版本特有内容
\else
    % Camera-ready 版本特有内容
\fi
```

### 文件清单

主目录现在包含以下文件：

- `aaai2026-unified-template.tex` - 统一主论文模板文件
- `aaai2026-unified-supp.tex` - 统一补充材料模板文件
- `aaai2026.sty` - AAAI 2026 LaTeX 样式文件
- `aaai2026.bst` - 参考文献样式文件
- `aaai2026.bib` - 示例参考文献文件
- `figure1.pdf` - 示例图片 1
- `figure2.pdf` - 示例图片 2
- `README.md` - 本文档

### 补充材料模板

#### 概述
`aaai2026-unified-supp.tex` 是专门为 AAAI 2026 补充材料设计的统一模板，使用与主论文模板相同的版本切换机制。

#### 主要特性
- **版本切换**：通过修改一行代码在匿名投稿和 camera-ready 版本之间切换
- **补充内容支持**：支持额外的实验、推导、数据、图表、算法等
- **格式一致性**：与主论文模板保持完全一致的格式
- **代码示例**：包含算法、代码列表及其他补充材料的示例

#### 使用方法
与主论文模板相同，只需修改第 11 行：
```latex
% 匿名投稿版本
\def\aaaianonymous{true}

% Camera-ready 版本
% \def\aaaianonymous{true}
```

#### 补充材料内容建议
- 额外的实验结果和消融研究
- 详细的数学推导和证明
- 更多图表和可视化
- 算法伪代码和实现细节
- 数据集描述和预处理步骤
- 超参数设置和实验配置
- 失败案例分析
- 计算复杂度分析

### 使用检查清单

#### 📋 投稿前检查清单

**版本设置**：
- [ ] 已设置 `\def\aaaianonymous{true}`（匿名投稿）
- [ ] 已注释掉所有可能暴露身份的信息
- [ ] 已匿名化参考文献（移除作者姓名）

**内容完整性**：
- [ ] 标题、摘要和关键词已填写
- [ ] 所有章节完整
- [ ] 图表编号连续且正确
- [ ] 参考文献格式正确
- [ ] 补充材料已准备（如有）

**格式检查**：
- [ ] 页面边距符合要求
- [ ] 字体和字号正确
- [ ] 行距符合标准
- [ ] 图表位置和大小合适
- [ ] 数学公式格式正确

**技术检查**：
- [ ] LaTeX 编译无错误
- [ ] 参考文献正确生成
- [ ] PDF 输出正常
- [ ] 文件大小在限制范围内

#### 📋 录用后检查清单

**版本切换**：
- [ ] 已注释掉 `\def\aaaianonymous{true}`（camera-ready）
- [ ] 已添加完整的作者信息
- [ ] 已添加所有作者单位信息
- [ ] 已恢复所有被注释的内容

**内容更新**：
- [ ] 已根据审稿意见修改内容
- [ ] 已更新所有图表和实验
- [ ] 已完善补充材料
- [ ] 已检查所有链接和引用

**最终检查**：
- [ ] 最终 PDF 质量检查
- [ ] 所有文件已备份
- [ ] 符合会议最终提交要求
- [ ] 补充材料已单独提交（如需要）

#### 📋 补充材料检查清单

**内容组织**：
- [ ] 补充材料与主论文内容对应
- [ ] 章节结构清晰合理
- [ ] 图表编号与主论文不冲突
- [ ] 参考文献格式一致

**技术细节**：
- [ ] 算法伪代码清晰完整
- [ ] 实验设置详细说明
- [ ] 数据预处理步骤清晰
- [ ] 超参数配置完整

**格式要求**：
- [ ] 使用统一的 supp 模板
- [ ] 页面设置与主论文一致
- [ ] 字体和格式符合要求
- [ ] 文件大小在限制范围内

### 实用使用建议

1. **投稿阶段**：
   - 取消注释 `\def\aaaianonymous{true}`
   - 确保不包含任何可能暴露身份的信息
   - 检查参考文献是否已匿名化

2. **录用后准备最终版本**：
   - 注释掉或删除 `\def\aaaianonymous{true}` 这一行
   - 添加完整的作者信息和 affiliations
   - 取消注释 links 环境（如需要）

3. **编译测试**：
   - 在两种模式下都编译，确保都能正常工作
   - 检查输出 PDF 是否符合要求
   - 验证参考文献格式是否正确

4. **依赖文件确认**：
   - 确保所有依赖文件都在同一目录下
   - 移动模板文件时记得同时移动依赖文件

### 重要说明

⚠️ **关于 Bibliography Style**：
- `aaai2026.sty` 文件会自动设置 `\bibliographystyle{aaai2026}`
- **不要**在文档中再次添加 `\bibliographystyle{aaai2026}` 命令
- 否则会出现 "`Illegal, another \bibstyle command`" 错误
- 只需使用 `\bibliography{aaai2026}` 命令

### 编译命令示例

```bash
# 编译 LaTeX 文档
pdflatex aaai2026-unified-template.tex
bibtex aaai2026-unified-template
pdflatex aaai2026-unified-template.tex
pdflatex aaai2026-unified-template.tex
```

### 常见问题与解决方案

#### 1. "Illegal, another \bibstyle command" 错误
**原因**：重复设置 bibliography style
**解决方案**：从文档中移除 `\bibliographystyle{aaai2026}` 命令，`aaai2026.sty` 会自动处理

#### 2. 参考文献格式不正确
**原因**：缺少 natbib 包或 BibTeX 文件问题
**解决方案**：遵循标准 LaTeX 编译流程：pdflatex → bibtex → pdflatex → pdflatex

---

## 版本信息 / 版本信息

- **模板版本 / 模板版本**：AAAI 2026 Unified（主论文 + 补充材料）
- **创建日期 / 创建日期**：2024 年 12 月
- **支持格式 / 支持格式**：匿名投稿 & Camera-Ready
- **模板类型 / 模板类型**：主论文模板 & 补充材料模板
- **兼容性 / 兼容性**：LaTeX 2020+ / TeXLive 2024+

---

🎉 **现在您只需要修改一行代码就可以在两个版本之间切换，同时所有必要的依赖文件都已经准备就绪！**
🎉 **现在您只需要修改一行代码就可以在两个版本之间切换，所有必要的依赖文件都已就绪可用！**
