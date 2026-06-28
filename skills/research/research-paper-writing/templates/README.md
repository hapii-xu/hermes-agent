# ML/AI 会议的 LaTeX 模板

本目录包含主要机器学习和 AI 会议的官方 LaTeX 模板。

---

## 将 LaTeX 编译为 PDF

### 方式 1：VS Code 配合 LaTeX Workshop（推荐）

**安装：**
1. 安装 [TeX Live](https://www.tug.org/texlive/)（推荐完整发行版）
   - macOS：`brew install --cask mactex`
   - Ubuntu：`sudo apt install texlive-full`
   - Windows：从 [tug.org/texlive](https://www.tug.org/texlive/) 下载

2. 安装 VS Code 扩展：James Yu 的 **LaTeX Workshop**
   - 打开 VS Code → 扩展（Cmd/Ctrl+Shift+X）→ 搜索「LaTeX Workshop」→ 安装

**使用：**
- 在 VS Code 中打开任意 `.tex` 文件
- 保存文件（Cmd/Ctrl+S）→ 自动编译为 PDF
- 点击绿色播放按钮或使用 `Cmd/Ctrl+Alt+B` 进行构建
- 查看 PDF：点击「View LaTeX PDF」图标或 `Cmd/Ctrl+Alt+V`
- 并排视图：`Cmd/Ctrl+Alt+V` 然后拖动标签页

**设置**（添加到 VS Code `settings.json`）：
```json
{
  "latex-workshop.latex.autoBuild.run": "onSave",
  "latex-workshop.view.pdf.viewer": "tab",
  "latex-workshop.latex.recipes": [
    {
      "name": "pdflatex → bibtex → pdflatex × 2",
      "tools": ["pdflatex", "bibtex", "pdflatex", "pdflatex"]
    }
  ]
}
```

### 方式 2：命令行

```bash
# Basic compilation
# 基本编译
pdflatex main.tex

# With bibliography (full workflow)
# 含参考文献的完整工作流
pdflatex main.tex
bibtex main
pdflatex main.tex
pdflatex main.tex

# Using latexmk (handles dependencies automatically)
# 使用 latexmk（自动处理依赖）
latexmk -pdf main.tex

# Continuous compilation (watches for changes)
# 持续编译（监视变更）
latexmk -pdf -pvc main.tex
```

### 方式 3：Overleaf（在线）

1. 访问 [overleaf.com](https://www.overleaf.com)
2. New Project → Upload Project → 将模板文件夹以 ZIP 上传
3. 在线编辑，实时 PDF 预览
4. 无需本地安装

### 方式 4：其他 IDE

| IDE | 扩展/插件 | 备注 |
|-----|------------------|-------|
| **Cursor** | LaTeX Workshop | 同 VS Code |
| **Sublime Text** | LaTeXTools | 流行、维护良好 |
| **Vim/Neovim** | VimTeX | 强大、键盘驱动 |
| **Emacs** | AUCTeX | 全面的 LaTeX 环境 |
| **TeXstudio** | 内置 | 专用 LaTeX IDE |
| **Texmaker** | 内置 | 跨平台 LaTeX 编辑器 |

### 编译故障排查

**「File not found」错误：**
```bash
# Ensure you're in the template directory
# 确保你处于模板目录中
cd templates/icml2026
pdflatex example_paper.tex
```

**参考文献未出现：**
```bash
# Run bibtex after first pdflatex
# 在第一次 pdflatex 之后运行 bibtex
pdflatex main.tex
bibtex main        # Uses main.aux to find citations —— 用 main.aux 查找引用
pdflatex main.tex  # Incorporates bibliography —— 纳入参考文献
pdflatex main.tex  # Resolves references —— 解析引用
```

**缺少宏包：**
```bash
# TeX Live package manager
# TeX Live 宏包管理器
tlmgr install <package-name>

# Or install full distribution to avoid this
# 或安装完整发行版以避免此问题
```

---

## 可用模板

| 会议 | 目录 | 年份 | 来源 |
|------------|-----------|------|--------|
| ICML | `icml2026/` | 2026 | [Official ICML](https://icml.cc/Conferences/2026/AuthorInstructions) |
| ICLR | `iclr2026/` | 2026 | [Official GitHub](https://github.com/ICLR/Master-Template) |
| NeurIPS | `neurips2025/` | 2025 | 社区模板 |
| ACL | `acl/` | 2025+ | [Official ACL](https://github.com/acl-org/acl-style-files) |
| AAAI | `aaai2026/` | 2026 | [AAAI Author Kit](https://aaai.org/authorkit26/) |
| COLM | `colm2025/` | 2025 | [Official COLM](https://github.com/COLM-org/Template) |

## 用法

### ICML 2026

```latex
\documentclass{article}
\usepackage{icml2026}  % For submission —— 用于投稿
% \usepackage[accepted]{icml2026}  % For camera-ready —— 用于终稿（camera-ready）

\begin{document}
% Your paper content —— 你的论文内容
\end{document}
```

关键文件：
- `icml2026.sty` - 样式文件
- `icml2026.bst` - 参考文献样式
- `example_paper.tex` - 示例文档

### ICLR 2026

```latex
\documentclass{article}
\usepackage[submission]{iclr2026_conference}  % For submission —— 用于投稿
% \usepackage[final]{iclr2026_conference}  % For camera-ready —— 用于终稿

\begin{document}
% Your paper content —— 你的论文内容
\end{document}
```

关键文件：
- `iclr2026_conference.sty` - 样式文件
- `iclr2026_conference.bst` - 参考文献样式
- `iclr2026_conference.tex` - 示例文档

### ACL 系列会议（ACL、EMNLP、NAACL）

```latex
\documentclass[11pt]{article}
\usepackage[review]{acl}  % For review —— 用于评审
% \usepackage{acl}  % For camera-ready —— 用于终稿

\begin{document}
% Your paper content —— 你的论文内容
\end{document}
```

关键文件：
- `acl.sty` - 样式文件
- `acl_natbib.bst` - 参考文献样式
- `acl_latex.tex` - 示例文档

### AAAI 2026

```latex
\documentclass[letterpaper]{article}
\usepackage[submission]{aaai2026}  % For submission —— 用于投稿
% \usepackage{aaai2026}  % For camera-ready —— 用于终稿

\begin{document}
% Your paper content —— 你的论文内容
\end{document}
```

关键文件：
- `aaai2026.sty` - 样式文件
- `aaai2026.bst` - 参考文献样式

### COLM 2025

```latex
\documentclass{article}
\usepackage[submission]{colm2025_conference}  % For submission —— 用于投稿
% \usepackage[final]{colm2025_conference}  % For camera-ready —— 用于终稿

\begin{document}
% Your paper content —— 你的论文内容
\end{document}
```

关键文件：
- `colm2025_conference.sty` - 样式文件
- `colm2025_conference.bst` - 参考文献样式

## 页数限制汇总

| 会议 | 投稿 | 终稿（Camera-Ready） | 备注 |
|------------|-----------|--------------|-------|
| ICML 2026 | 8 页 | 9 页 | +不限参考文献/附录 |
| ICLR 2026 | 9 页 | 10 页 | +不限参考文献/附录 |
| NeurIPS 2025 | 9 页 | 9 页 | +清单不计入限制 |
| ACL 2025 | 8 页（长文） | 视情况 | +不限参考文献/附录 |
| AAAI 2026 | 7 页 | 8 页 | +不限参考文献/附录 |
| COLM 2025 | 9 页 | 10 页 | +不限参考文献/附录 |

## 常见问题

### 编译错误

1. **缺少宏包**：安装完整的 TeX 发行版（TeX Live Full 或 MikTeX）
2. **参考文献错误**：使用提供的 `.bst` 文件配合 `\bibliographystyle{}`
3. **字体警告**：安装 `cm-super` 或使用 `\usepackage{lmodern}`

### 匿名化

投稿时，请确保：
- `\author{}` 中无作者姓名
- 无致谢章节
- 无基金编号
- 使用匿名仓库
- 以第三人称引用自己的工作

### 常用 LaTeX 宏包

```latex
% Recommended packages (check compatibility with venue style)
% 推荐宏包（请检查与会议样式的兼容性）
\usepackage{amsmath,amsthm,amssymb}  % Math —— 数学
\usepackage{graphicx}                 % Figures —— 图
\usepackage{booktabs}                 % Tables —— 表
\usepackage{hyperref}                 % Links —— 链接
\usepackage{algorithm,algorithmic}    % Algorithms —— 算法
\usepackage{natbib}                   % Citations —— 引用
```

## 更新模板

模板每年更新。每次投稿前请查看官方来源：

- ICML: https://icml.cc/
- ICLR: https://iclr.cc/
- NeurIPS: https://neurips.cc/
- ACL: https://github.com/acl-org/acl-style-files
- AAAI: https://aaai.org/
- COLM: https://colmweb.org/
