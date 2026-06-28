# 引用管理与幻觉防范

本参考资料提供了以编程方式管理引用、防范 AI 生成的引用幻觉以及维护干净文献库的完整工作流。

---

## 目录

- [引用验证为何重要](#引用验证为何重要)
- [引用 API 概览](#引用-api-概览)
- [已验证的引用工作流](#已验证的引用工作流)
- [Python 实现](#python-实现)
- [BibTeX 管理](#bibtex-管理)
- [常见引用格式](#常见引用格式)
- [故障排查](#故障排查)

---

## 引用验证为何重要

### 幻觉问题

研究已记录了 AI 生成的引用存在的重大问题：
- AI 生成的引用**错误率约为 40%**（Enago Academy 研究）
- NeurIPS 2025 发现**100+ 条幻觉引用**通过了评审
- 常见错误包括：
  - 捏造的论文标题配上真实作者名
  - 错误的发表会议或年份
  - 具有看似合理元数据的不存在论文
  - 错误的 DOI 或 arXiv ID

### 后果

- 在某些会议被桌面拒稿
- 失去审稿人的信任
- 若已发表，可能被撤稿
- 浪费时间追逐不存在的文献来源

### 解决方案

**永远不要凭记忆生成引用——务必通过编程方式验证。**

---

## 引用 API 概览

### 主要 API

| API | 覆盖范围 | 速率限制 | 最适用于 |
|-----|----------|-------------|----------|
| **Semantic Scholar** | 2.14 亿篇论文 | 1 RPS（免费 key） | ML/AI 论文、引用图谱 |
| **CrossRef** | 1.4 亿+ DOI | 配合 mailto 的礼貌池 | DOI 查询、BibTeX 获取 |
| **arXiv** | 预印本 | 3 秒延迟 | ML 预印本、PDF 访问 |
| **OpenAlex** | 2.4 亿+ 作品 | 10 万/天，10 RPS | MAG 的开放替代 |

### API 选择指南

```
Need ML paper search? → Semantic Scholar
Have DOI, need BibTeX? → CrossRef content negotiation
Looking for preprint? → arXiv API
Need open data, bulk access? → OpenAlex
```

（译注：需要 ML 论文搜索？→ Semantic Scholar；有 DOI 需要 BibTeX？→ CrossRef 内容协商；找预印本？→ arXiv API；需要开放数据/批量访问？→ OpenAlex）

### 没有官方的 Google Scholar API

Google Scholar 没有官方 API。抓取违反其服务条款。仅在 Semantic Scholar 覆盖不足时，才使用 SerpApi（每月 75–275 美元）。

---

## 已验证的引用工作流

### 5 步流程

```
1. SEARCH → Query Semantic Scholar with specific keywords
     ↓
2. VERIFY → Confirm paper exists in 2+ sources
     ↓
3. RETRIEVE → Get BibTeX via DOI content negotiation
     ↓
4. VALIDATE → Confirm the claim appears in source
     ↓
5. ADD → Add verified entry to .bib file
```

（译注：1. 搜索 → 用具体关键词查询 Semantic Scholar；2. 验证 → 确认论文在 2 个以上来源中存在；3. 获取 → 通过 DOI 内容协商获取 BibTeX；4. 校验 → 确认该论点出现在来源中；5. 添加 → 将已验证条目加入 .bib 文件）

### 第 1 步：搜索

针对 ML/AI 论文使用 Semantic Scholar：

```python
from semanticscholar import SemanticScholar

sch = SemanticScholar()
results = sch.search_paper("transformer attention mechanism", limit=10)

for paper in results:
    print(f"Title: {paper.title}")
    print(f"Year: {paper.year}")
    print(f"DOI: {paper.externalIds.get('DOI', 'N/A')}")
    print(f"arXiv: {paper.externalIds.get('ArXiv', 'N/A')}")
    print(f"Citation count: {paper.citationCount}")
    print("---")
```

### 第 2 步：验证存在性

确认论文至少在两个来源中存在：

```python
import requests

def verify_paper(doi=None, arxiv_id=None, title=None):
    """Verify paper exists in multiple sources."""
    # 验证论文在多个来源中存在。
    sources_found = []

    # Check Semantic Scholar
    # 检查 Semantic Scholar
    sch = SemanticScholar()
    if doi:
        paper = sch.get_paper(f"DOI:{doi}")
        if paper:
            sources_found.append("Semantic Scholar")

    # Check CrossRef (via DOI)
    # 通过 DOI 检查 CrossRef
    if doi:
        resp = requests.get(f"https://api.crossref.org/works/{doi}")
        if resp.status_code == 200:
            sources_found.append("CrossRef")

    # Check arXiv
    # 检查 arXiv
    if arxiv_id:
        resp = requests.get(
            f"http://export.arxiv.org/api/query?id_list={arxiv_id}"
        )
        if "<entry>" in resp.text:
            sources_found.append("arXiv")

    return len(sources_found) >= 2, sources_found
```

### 第 3 步：获取 BibTeX

使用 DOI 内容协商以保证准确性：

```python
import requests

def doi_to_bibtex(doi: str) -> str:
    """Get verified BibTeX from DOI via CrossRef content negotiation."""
    # 通过 CrossRef 内容协商从 DOI 获取已验证的 BibTeX。
    response = requests.get(
        f"https://doi.org/{doi}",
        headers={"Accept": "application/x-bibtex"},
        allow_redirects=True
    )
    response.raise_for_status()
    return response.text

# Example: "Attention Is All You Need"
# 示例：「Attention Is All You Need」
bibtex = doi_to_bibtex("10.48550/arXiv.1706.03762")
print(bibtex)
```

### 第 4 步：校验论点

在为某个具体论点引用论文之前，先验证该论点确实存在：

```python
def get_paper_abstract(doi):
    """Get abstract to verify claims."""
    # 获取摘要以验证论点。
    sch = SemanticScholar()
    paper = sch.get_paper(f"DOI:{doi}")
    return paper.abstract if paper else None

# Verify claim appears in abstract
# 验证论点是否出现在摘要中
abstract = get_paper_abstract("10.48550/arXiv.1706.03762")
claim = "attention mechanism"
if claim.lower() in abstract.lower():
    print("Claim appears in paper")
```

### 第 5 步：加入文献库

将已验证条目以一致的 key 格式加入你的 .bib 文件：

```python
def generate_citation_key(bibtex: str) -> str:
    """Generate consistent citation key: author_year_firstword."""
    # 生成一致的引用 key：author_year_firstword（作者_年份_首个单词）。
    import re

    # Extract author
    # 提取作者
    author_match = re.search(r'author\s*=\s*\{([^}]+)\}', bibtex, re.I)
    if author_match:
        first_author = author_match.group(1).split(',')[0].split()[-1]
    else:
        first_author = "unknown"

    # Extract year
    # 提取年份
    year_match = re.search(r'year\s*=\s*\{?(\d{4})\}?', bibtex, re.I)
    year = year_match.group(1) if year_match else "0000"

    # Extract title first word
    # 提取标题首个单词
    title_match = re.search(r'title\s*=\s*\{([^}]+)\}', bibtex, re.I)
    if title_match:
        first_word = title_match.group(1).split()[0].lower()
        first_word = re.sub(r'[^a-z]', '', first_word)
    else:
        first_word = "paper"

    return f"{first_author.lower()}_{year}_{first_word}"
```

---

## Python 实现

### 完整的引用管理器类

{% raw %}
```python
"""
Citation Manager - Verified citation workflow for ML papers.
引用管理器 —— 面向 ML 论文的已验证引用工作流。
"""

import requests
import time
from typing import Optional, List, Dict, Tuple
from dataclasses import dataclass

try:
    from semanticscholar import SemanticScholar
except ImportError:
    print("Install: pip install semanticscholar")
    SemanticScholar = None

@dataclass
class Paper:
    title: str
    authors: List[str]
    year: int
    doi: Optional[str]
    arxiv_id: Optional[str]
    venue: Optional[str]
    citation_count: int
    abstract: Optional[str]

class CitationManager:
    """Manage citations with verification."""
    # 在验证基础上管理引用。

    def __init__(self, api_key: Optional[str] = None):
        self.sch = SemanticScholar(api_key=api_key) if SemanticScholar else None
        self.verified_papers: Dict[str, Paper] = {}

    def search(self, query: str, limit: int = 10) -> List[Paper]:
        """Search for papers using Semantic Scholar."""
        # 使用 Semantic Scholar 搜索论文。
        if not self.sch:
            raise RuntimeError("Semantic Scholar not available")

        results = self.sch.search_paper(query, limit=limit)
        papers = []

        for r in results:
            paper = Paper(
                title=r.title,
                authors=[a.name for a in (r.authors or [])],
                year=r.year or 0,
                doi=r.externalIds.get('DOI') if r.externalIds else None,
                arxiv_id=r.externalIds.get('ArXiv') if r.externalIds else None,
                venue=r.venue,
                citation_count=r.citationCount or 0,
                abstract=r.abstract
            )
            papers.append(paper)

        return papers

    def verify(self, paper: Paper) -> Tuple[bool, List[str]]:
        """Verify paper exists in multiple sources."""
        # 验证论文在多个来源中存在。
        sources = []

        # Already found in Semantic Scholar via search
        # 搜索时已在 Semantic Scholar 中找到
        sources.append("Semantic Scholar")

        # Check CrossRef if DOI available
        # 如有 DOI，检查 CrossRef
        if paper.doi:
            try:
                resp = requests.get(
                    f"https://api.crossref.org/works/{paper.doi}",
                    timeout=10
                )
                if resp.status_code == 200:
                    sources.append("CrossRef")
            except Exception:
                pass

        # Check arXiv if ID available
        # 如有 ID，检查 arXiv
        if paper.arxiv_id:
            try:
                resp = requests.get(
                    f"http://export.arxiv.org/api/query?id_list={paper.arxiv_id}",
                    timeout=10
                )
                if "<entry>" in resp.text and "<title>" in resp.text:
                    sources.append("arXiv")
            except Exception:
                pass

        return len(sources) >= 2, sources

    def get_bibtex(self, paper: Paper) -> Optional[str]:
        """Get BibTeX for verified paper."""
        # 为已验证论文获取 BibTeX。
        if paper.doi:
            try:
                resp = requests.get(
                    f"https://doi.org/{paper.doi}",
                    headers={"Accept": "application/x-bibtex"},
                    timeout=10,
                    allow_redirects=True
                )
                if resp.status_code == 200:
                    return resp.text
            except Exception:
                pass

        # Fallback: generate from paper data
        # 兜底：根据论文数据生成
        return self._generate_bibtex(paper)

    def _generate_bibtex(self, paper: Paper) -> str:
        """Generate BibTeX from paper metadata."""
        # 根据论文元数据生成 BibTeX。
        # Generate citation key
        # 生成引用 key
        first_author = paper.authors[0].split()[-1] if paper.authors else "unknown"
        first_word = paper.title.split()[0].lower().replace(',', '').replace(':', '')
        key = f"{first_author.lower()}_{paper.year}_{first_word}"

        # Format authors
        # 格式化作者
        authors = " and ".join(paper.authors) if paper.authors else "Unknown"

        bibtex = f"""@article{{{key},
  title = {{{paper.title}}},
  author = {{{authors}}},
  year = {{{paper.year}}},
  {'doi = {' + paper.doi + '},' if paper.doi else ''}
  {'eprint = {' + paper.arxiv_id + '},' if paper.arxiv_id else ''}
  {'journal = {' + paper.venue + '},' if paper.venue else ''}
}}"""
        return bibtex

    def cite(self, query: str) -> Optional[str]:
        """Full workflow: search, verify, return BibTeX."""
        # 完整工作流：搜索、验证、返回 BibTeX。
        # Search
        # 搜索
        papers = self.search(query, limit=5)
        if not papers:
            return None

        # Take top result
        # 取最相关的结果
        paper = papers[0]

        # Verify
        # 验证
        verified, sources = self.verify(paper)
        if not verified:
            print(f"Warning: Could only verify in {sources}")

        # Get BibTeX
        # 获取 BibTeX
        bibtex = self.get_bibtex(paper)

        # Cache
        # 缓存
        if bibtex:
            self.verified_papers[paper.title] = paper

        return bibtex


# Usage example
# 使用示例
if __name__ == "__main__":
    cm = CitationManager()

    # Search and cite
    # 搜索并引用
    bibtex = cm.cite("attention is all you need transformer")
    if bibtex:
        print(bibtex)
```
{% endraw %}

### 快捷函数

```python
def quick_cite(query: str) -> str:
    """One-liner citation."""
    # 一行式引用。
    cm = CitationManager()
    return cm.cite(query)

def batch_cite(queries: List[str], output_file: str = "references.bib"):
    """Cite multiple papers and save to file."""
    # 引用多篇论文并保存到文件。
    cm = CitationManager()
    bibtex_entries = []

    for query in queries:
        print(f"Processing: {query}")
        bibtex = cm.cite(query)
        if bibtex:
            bibtex_entries.append(bibtex)
        time.sleep(1)  # Rate limiting —— 速率限制

    with open(output_file, 'w') as f:
        f.write("\n\n".join(bibtex_entries))

    print(f"Saved {len(bibtex_entries)} citations to {output_file}")
```

---

## BibTeX 管理

### BibTeX 与 BibLaTeX

| 特性 | BibTeX | BibLaTeX |
|---------|--------|----------|
| Unicode 支持 | 有限 | 完整 |
| 条目类型 | 标准 | 扩展（@online、@dataset） |
| 定制性 | 有限 | 高度灵活 |
| 后端 | bibtex | Biber（推荐） |

**建议**：会议投稿使用 natbib 配合 BibTeX——所有主要会议模板（NeurIPS、ICML、ICLR、ACL、AAAI、COLM）都自带 natbib 和 `.bst` 文件。BibLaTeX 配合 Biber 是期刊或你能掌控模板的个人项目的可选项。

### LaTeX 配置

```latex
% In preamble
% 在导言区（preamble）
\usepackage[
    backend=biber,
    style=numeric,
    sorting=none
]{biblatex}
\addbibresource{references.bib}

% In document
% 在正文中
\cite{vaswani_2017_attention}

% At end
% 在末尾
\printbibliography
```

### 引用命令

```latex
\cite{key}      % Numeric: [1] —— 数字式：[1]
\citep{key}     % Parenthetical: (Author, 2020) —— 括号式：(Author, 2020)
\citet{key}     % Textual: Author (2020) —— 文本式：Author (2020)
\citeauthor{key} % Just author name —— 仅作者名
\citeyear{key}  % Just year —— 仅年份
```

### 一致的引用 key

使用格式：`author_year_firstword`

```
vaswani_2017_attention
devlin_2019_bert
brown_2020_language
```

---

## 常见引用格式

### 会议论文

```bibtex
@inproceedings{vaswani_2017_attention,
  title = {Attention Is All You Need},
  author = {Vaswani, Ashish and Shazeer, Noam and Parmar, Niki and
            Uszkoreit, Jakob and Jones, Llion and Gomez, Aidan N and
            Kaiser, Lukasz and Polosukhin, Illia},
  booktitle = {Advances in Neural Information Processing Systems},
  volume = {30},
  year = {2017},
  publisher = {Curran Associates, Inc.}
}
```

### 期刊论文

```bibtex
@article{hochreiter_1997_long,
  title = {Long Short-Term Memory},
  author = {Hochreiter, Sepp and Schmidhuber, J{\"u}rgen},
  journal = {Neural Computation},
  volume = {9},
  number = {8},
  pages = {1735--1780},
  year = {1997},
  publisher = {MIT Press}
}
```

### arXiv 预印本

```bibtex
@misc{brown_2020_language,
  title = {Language Models are Few-Shot Learners},
  author = {Brown, Tom and Mann, Benjamin and Ryder, Nick and others},
  year = {2020},
  eprint = {2005.14165},
  archiveprefix = {arXiv},
  primaryclass = {cs.CL}
}
```

---

## 故障排查

### 常见问题

**问题：Semantic Scholar 返回无结果**
- 尝试更具体的关键词
- 检查作者名拼写
- 对精确短语使用引号

**问题：DOI 解析不到 BibTeX**
- DOI 可能已注册但未链接到 CrossRef
- 如有 arXiv ID，改用之
- 根据元数据手动生成 BibTeX

**问题：速率限制错误**
- 在请求之间加入延迟（1–3 秒）
- 如可用，使用 API key
- 缓存结果以避免重复查询

**问题：BibTeX 编码问题**
- 使用正确的 LaTeX 转义：`{\"u}` 表示 ü
- 确保文件以 UTF-8 编码
- 使用 BibLaTeX 配合 Biber 以获得更好的 Unicode 支持

### 验证清单

添加引用之前：

- [ ] 论文在至少 2 个来源中找到
- [ ] DOI 或 arXiv ID 已验证
- [ ] BibTeX 是获取来的（而非凭记忆生成的）
- [ ] 条目类型正确（@inproceedings vs @article）
- [ ] 作者名完整且格式正确
- [ ] 年份和会议已验证
- [ ] 引用 key 遵循一致的格式

---

## 其他资源

**API：**
- Semantic Scholar: https://api.semanticscholar.org/api-docs/
- CrossRef: https://www.crossref.org/documentation/retrieve-metadata/rest-api/
- arXiv: https://info.arxiv.org/help/api/basics.html
- OpenAlex: https://docs.openalex.org/

**Python 库：**
- `semanticscholar`: https://pypi.org/project/semanticscholar/
- `arxiv`: https://pypi.org/project/arxiv/
- `habanero`（CrossRef）: https://github.com/sckott/habanero

**验证工具：**
- Citely: https://citely.ai/citation-checker
- ReciteWorks: https://reciteworks.com/
