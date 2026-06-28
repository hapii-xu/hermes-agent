---
name: ocr-and-documents
description: "从 PDF/扫描件中提取文本（pymupdf、marker-pdf）。"
version: 2.3.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [PDF, Documents, Research, Arxiv, Text-Extraction, OCR]
    related_skills: [powerpoint]
---

# PDF 与文档提取

对于 DOCX：使用 `python-docx`（解析实际文档结构，远胜于 OCR）。
对于 PPTX：参见 `powerpoint` 技能（使用 `python-pptx`，完整支持幻灯片/备注）。
本技能覆盖 **PDF 和扫描文档**。

## 步骤 1：有远程 URL 吗？

如果文档有 URL，**总是先尝试 `web_extract`**：

```
web_extract(urls=["https://arxiv.org/pdf/2402.03300"])
web_extract(urls=["https://example.com/report.pdf"])
```

这通过 Firecrawl 处理 PDF 到 markdown 的转换，无需本地依赖。

仅在以下情况使用本地提取：文件在本地、web_extract 失败，或你需要批量处理。

## 步骤 2：选择本地提取器

| 功能 | pymupdf（约 25MB） | marker-pdf（约 3-5GB） |
|---------|-----------------|---------------------|
| **文本型 PDF** | ✅ | ✅ |
| **扫描 PDF（OCR）** | ❌ | ✅（90+ 种语言） |
| **表格** | ✅（基础） | ✅（高准确度） |
| **公式 / LaTeX** | ❌ | ✅ |
| **代码块** | ❌ | ✅ |
| **表单** | ❌ | ✅ |
| **页眉/页脚移除** | ❌ | ✅ |
| **阅读顺序检测** | ❌ | ✅ |
| **图片提取** | ✅（嵌入的） | ✅（带上下文） |
| **图片 → 文本（OCR）** | ❌ | ✅ |
| **EPUB** | ✅ | ✅ |
| **Markdown 输出** | ✅（通过 pymupdf4llm） | ✅（原生，质量更高） |
| **安装大小** | 约 25MB | 约 3-5GB（PyTorch + 模型） |
| **速度** | 即时 | 约 1-14s/页（CPU），约 0.2s/页（GPU） |

**决策**：除非你需要 OCR、公式、表单或复杂版面分析，否则使用 pymupdf。

如果用户需要 marker 的能力但系统缺少约 5GB 可用磁盘空间：
> "此文档需要 OCR/高级提取（marker-pdf），这需要约 5GB 用于 PyTorch 和模型。你的系统有 [X]GB 可用空间。选项：释放空间、提供 URL 以便我使用 web_extract，或者我可以尝试 pymupdf，它适用于文本型 PDF，但不适用于扫描文档或公式。"

---

## pymupdf（轻量级）

```bash
pip install pymupdf pymupdf4llm
```

**通过辅助脚本**：
```bash
python scripts/extract_pymupdf.py document.pdf              # 纯文本
python scripts/extract_pymupdf.py document.pdf --markdown    # Markdown
python scripts/extract_pymupdf.py document.pdf --tables      # 表格
python scripts/extract_pymupdf.py document.pdf --images out/ # 提取图片
python scripts/extract_pymupdf.py document.pdf --metadata    # 标题、作者、页数
python scripts/extract_pymupdf.py document.pdf --pages 0-4   # 特定页
```

**行内**：
```bash
python3 -c "
import pymupdf
doc = pymupdf.open('document.pdf')
for page in doc:
    print(page.get_text())
"
```

---

## marker-pdf（高质量 OCR）

```bash
# 先检查磁盘空间
python scripts/extract_marker.py --check

pip install marker-pdf
```

**通过辅助脚本**：
```bash
python scripts/extract_marker.py document.pdf                # Markdown
python scripts/extract_marker.py document.pdf --json         # 带元数据的 JSON
python scripts/extract_marker.py document.pdf --output_dir out/  # 保存图片
python scripts/extract_marker.py scanned.pdf                 # 扫描 PDF（OCR）
python scripts/extract_marker.py document.pdf --use_llm      # LLM 增强准确度
```

**CLI**（随 marker-pdf 安装）：
```bash
marker_single document.pdf --output_dir ./output
marker /path/to/folder --workers 4    # 批量
```

---

## Arxiv 论文

```
# 仅摘要（快速）
web_extract(urls=["https://arxiv.org/abs/2402.03300"])

# 完整论文
web_extract(urls=["https://arxiv.org/pdf/2402.03300"])

# 搜索
web_search(query="arxiv GRPO reinforcement learning 2026")
```

## 拆分、合并与搜索

pymupdf 原生处理这些 —— 使用 `execute_code` 或行内 Python：

```python
# 拆分：将第 1-5 页提取到新 PDF
import pymupdf
doc = pymupdf.open("report.pdf")
new = pymupdf.open()
for i in range(5):
    new.insert_pdf(doc, from_page=i, to_page=i)
new.save("pages_1-5.pdf")
```

```python
# 合并多个 PDF
import pymupdf
result = pymupdf.open()
for path in ["a.pdf", "b.pdf", "c.pdf"]:
    result.insert_pdf(pymupdf.open(path))
result.save("merged.pdf")
```

```python
# 在所有页面中搜索文本
import pymupdf
doc = pymupdf.open("report.pdf")
for i, page in enumerate(doc):
    results = page.search_for("revenue")
    if results:
        print(f"Page {i+1}: {len(results)} match(es)")
        print(page.get_text("text"))
```

无需额外依赖 —— pymupdf 在一个包中涵盖拆分、合并、搜索和文本提取。

---

## 注意事项

- `web_extract` 对于 URL 总是首选
- pymupdf 是安全的默认选择 —— 即时、无模型、到处可用
- marker-pdf 用于 OCR、扫描文档、公式、复杂版面 —— 仅在需要时安装
- 两个辅助脚本都接受 `--help` 查看完整用法
- marker-pdf 首次使用时会下载约 2.5GB 模型到 `~/.cache/huggingface/`
- 对于 Word 文档：`pip install python-docx`（优于 OCR —— 解析实际结构）
- 对于 PowerPoint：参见 `powerpoint` 技能（使用 python-pptx）
