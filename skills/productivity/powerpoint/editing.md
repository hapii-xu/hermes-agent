# 编辑演示文稿

## 基于模板的工作流

当把现有演示文稿用作模板时：

1. **分析现有幻灯片**：
   ```bash
   python scripts/thumbnail.py template.pptx
   python -m markitdown template.pptx
   ```
   查看生成的 `thumbnails.jpg` 以了解版式，查看 markitdown 输出以了解占位文本。

2. **规划幻灯片映射**：为每个内容小节挑选一张模板幻灯片。

   ⚠️ **使用多样的版式**——单调乏味是常见的失败模式。不要默认套用「标题 + 项目符号」的基础版式，主动寻找：
   - 多栏版式（2 栏、3 栏）
   - 图 + 文组合
   - 带文字叠加的满出血图
   - 引用或强调页
   - 分节页
   - 统计/数字强调
   - 图标网格或图标 + 文字行

   **避免：** 每张幻灯片都用同一种文字密集型版式。

   把内容类型与版式风格对齐（例如要点 → 项目符号页，团队信息 → 多栏，证言 → 引用页）。

3. **解包**：`python scripts/office/unpack.py template.pptx unpacked/`

4. **搭建演示文稿结构**（自己做，不要交给 subagent）：
   - 删除不需要的幻灯片（从 `<p:sldIdLst>` 中移除）
   - 复制要复用的幻灯片（`add_slide.py`）
   - 在 `<p:sldIdLst>` 中重排幻灯片
   - **在第 5 步之前完成所有结构性改动**

5. **编辑内容**：更新每张 `slide{N}.xml` 里的文字。
   **如果可用，在这里使用 subagent**——幻灯片是独立的 XML 文件，subagent 可以并行编辑。

6. **清理**：`python scripts/clean.py unpacked/`

7. **打包**：`python scripts/office/pack.py unpacked/ output.pptx --original template.pptx`

---

## 脚本

| 脚本 | 用途 |
|--------|---------|
| `unpack.py` | 解压并美化输出 PPTX |
| `add_slide.py` | 复制幻灯片或从版式创建 |
| `clean.py` | 移除孤立文件 |
| `pack.py` | 带校验的重新打包 |
| `thumbnail.py` | 生成幻灯片的视觉网格 |

### unpack.py

```bash
python scripts/office/unpack.py input.pptx unpacked/
```

解压 PPTX，美化输出 XML，转义智能引号。

### add_slide.py

```bash
python scripts/add_slide.py unpacked/ slide2.xml      # 复制幻灯片
python scripts/add_slide.py unpacked/ slideLayout2.xml # 从版式创建
```

打印需要添加到 `<p:sldIdLst>` 中指定位置的 `<p:sldId>`。

### clean.py

```bash
python scripts/clean.py unpacked/
```

移除不在 `<p:sldIdLst>` 中的幻灯片、未被引用的媒体和孤立的 rels。

### pack.py

```bash
python scripts/office/pack.py unpacked/ output.pptx --original input.pptx
```

校验、修复、压缩 XML，并对智能引号重新编码。

### thumbnail.py

```bash
python scripts/thumbnail.py input.pptx [output_prefix] [--cols N]
```

生成以幻灯片文件名作为标签的 `thumbnails.jpg`。默认 3 列，每张网格最多 12 张。

**仅用于模板分析**（挑选版式）。视觉 QA 请用 `soffice` + `pdftoppm` 生成全分辨率的单张幻灯片图片——参见 SKILL.md。

---

## 幻灯片操作

幻灯片顺序定义在 `ppt/presentation.xml` → `<p:sldIdLst>` 中。

**重排**：重新排列 `<p:sldId>` 元素。

**删除**：移除 `<p:sldId>`，然后运行 `clean.py`。

**新增**：使用 `add_slide.py`。绝不手动复制幻灯片文件——脚本会处理备注引用、Content_Types.xml 和关系 ID，手动复制会遗漏这些。

---

## 编辑内容

**subagent：** 如果可用，请在这里使用它们（在第 4 步完成后）。每张幻灯片是独立的 XML 文件，subagent 可以并行编辑。在给 subagent 的提示中包含：
- 要编辑的幻灯片文件路径
- **「所有修改一律使用 Edit 工具」**
- 下方格式规则和常见陷阱

对每张幻灯片：
1. 读取该幻灯片的 XML
2. 找出所有占位内容——文本、图片、图表、图标、说明文字
3. 用最终内容替换每个占位

**使用 Edit 工具，不要用 sed 或 Python 脚本。** Edit 工具强制你明确要替换什么、替换在哪里，可靠性更好。

### 格式规则

- **所有标题、副标题、行内标签都要加粗**：在 `<a:rPr>` 上用 `b="1"`。包括：
  - 幻灯片标题
  - 幻灯片内的分节标题
  - 行首的行内标签（例如「Status:」「Description:」）
- **绝不使用 unicode 项目符号（•）**：用 `<a:buChar>` 或 `<a:buAutoNum>` 做规范的列表格式
- **项目符号一致性**：让项目符号从版式继承，只在必要时指定 `<a:buChar>` 或 `<a:buNone>`

---

## 常见陷阱

### 模板适配

当源内容项数少于模板时：
- **整块移除多余元素**（图片、形状、文本框），不要只清空文字
- 清空文字后检查是否有遗落的视觉元素
- 跑一遍视觉 QA 以发现数量不匹配

用不同长度内容替换文字时：
- **替换成更短**：通常安全
- **替换成更长**：可能溢出或意外换行
- 改动文字后用视觉 QA 验证
- 考虑截断或拆分内容以适配模板的设计约束

**模板槽位 ≠ 源条目**：若模板有 4 名团队成员但源里只有 3 名，应删除第 4 名成员的整组（图片 + 文本框），而非只删文字。

### 多项内容

若源有多项内容（编号列表、多个小节），应为每项创建独立的 `<a:p>` 元素——**绝不拼成一个字符串**。

**❌ 错误**——所有项塞进一段：
```xml
<a:p>
  <a:r><a:rPr .../><a:t>Step 1: Do the first thing. Step 2: Do the second thing.</a:t></a:r>
</a:p>
```

**✅ 正确**——分独立段落，标题加粗：
```xml
<a:p>
  <a:pPr algn="l"><a:lnSpc><a:spcPts val="3919"/></a:lnSpc></a:pPr>
  <a:r><a:rPr lang="en-US" sz="2799" b="1" .../><a:t>Step 1</a:t></a:r>
</a:p>
<a:p>
  <a:pPr algn="l"><a:lnSpc><a:spcPts val="3919"/></a:lnSpc></a:pPr>
  <a:r><a:rPr lang="en-US" sz="2799" .../><a:t>Do the first thing.</a:t></a:r>
</a:p>
<a:p>
  <a:pPr algn="l"><a:lnSpc><a:spcPts val="3919"/></a:lnSpc></a:pPr>
  <a:r><a:rPr lang="en-US" sz="2799" b="1" .../><a:t>Step 2</a:t></a:r>
</a:p>
<!-- 按此模式继续 -->
```

从原段落复制 `<a:pPr>` 以保留行距。标题用 `b="1"`。

### 智能引号

由 unpack/pack 自动处理。但 Edit 工具会把智能引号转成 ASCII。

**新增带引号的文字时，使用 XML 实体：**

```xml
<a:t>the &#x201C;Agreement&#x201D;</a:t>
```

| 字符 | 名称 | Unicode | XML 实体 |
|-----------|------|---------|------------|
| `"` | 左双引号 | U+201C | `&#x201C;` |
| `"` | 右双引号 | U+201D | `&#x201D;` |
| `'` | 左单引号 | U+2018 | `&#x2018;` |
| `'` | 右单引号 | U+2019 | `&#x2019;` |

### 其他

- **空白**：当 `<a:t>` 内含前导/尾随空格时使用 `xml:space="preserve"`
- **XML 解析**：使用 `defusedxml.minidom`，不要用 `xml.etree.ElementTree`（后者会破坏命名空间）
