#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""构建翻译工作清单（仅核心源码 + 文档，跳过 tests/）。
输出：
  .translate-chunks/worklist_py.txt   —— 需翻译的 .py（按目录分组，含行数）
  .translate-chunks/worklist_md.txt   —— 需翻译的 .md
"""
import ast
import os
import re
import io

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
CJK_RE = re.compile(r"[一-鿿　-〿＀-￯㐀-䶿]")
DIRECTIVE_RE = re.compile(
    r"^\s*#\s*(type:\s*ignore|noqa|pragma|pylint|isort|fmt:|type:|region|endregion|coding:|!/)",
    re.IGNORECASE,
)
SKIP_DIRS = {
    ".git", "node_modules", "skills-en-backup", ".translate-chunks",
    "__pycache__", ".venv", "venv", "env", ".tox", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "dist", "build", ".next", "target",
    ".plans", "site-packages", "tests",  # 跳过 tests/
}
# 只翻译这些核心源码目录/顶层文件
PY_INCLUDE_DIRS = {
    "agent", "hermes_cli", "plugins", "gateway", "tools", "cron",
    "scripts", "optional-skills", "skills", "website", "providers",
    "acp_adapter", "tui_gateway",
}
PY_INCLUDE_TOPLEVEL = {
    "cli.py", "run_agent.py", "hermes_state.py", "model_tools.py",
    "trajectory_compressor.py", "utils.py", "setup.py",
    "hermes_bootstrap.py", "hermes_constants.py", "hermes_logging.py",
    "hermes_time.py", "mcp_serve.py", "mini_swe_runner.py",
    "batch_runner.py", "toolset_distributions.py", "toolsets.py",
}


def has_cjk(s):
    return bool(CJK_RE.search(s))


def header_is_english(src):
    """模块 docstring 首个有内容行：若为纯英文（有字母无 CJK）则返回 True（=未翻译）。"""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return False
    m = ast.get_docstring(tree, clean=False)
    if not m:
        return False  # 无 docstring，靠注释判定会在主流程处理
    fm = next((ln.strip() for ln in m.splitlines() if ln.strip()), "")
    if fm.startswith(">>>"):
        return False
    return bool(re.search(r"[A-Za-z]", fm)) and not has_cjk(fm)


def count_english_comments(src):
    n = 0
    for ln in src.splitlines():
        s = ln.strip()
        if not s.startswith("#"):
            continue
        if DIRECTIVE_RE.match(s):
            continue
        body = s.lstrip("#").strip()
        if body and re.search(r"[A-Za-z]", body) and not has_cjk(body):
            n += 1
    return n


def main():
    py_work = []  # (path, lines, cm_en)
    md_work = []

    for root, dirs, names in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        rel_root = os.path.relpath(root, ROOT)
        top = rel_root.split(os.sep)[0] if rel_root != "." else ""
        for n in names:
            p = os.path.join(root, n)
            ext = os.path.splitext(n)[1].lower()
            # ---- Python ----
            if ext == ".py":
                include = (top in PY_INCLUDE_DIRS) or (rel_root == "." and n in PY_INCLUDE_TOPLEVEL)
                if not include:
                    continue
                try:
                    src = open(p, encoding="utf-8", errors="replace").read()
                except Exception:
                    continue
                lines = src.count("\n") + 1
                # 已翻译（头部非英文 且 注释英文<3）则跳过
                hdr_en = header_is_english(src)
                cm_en = count_english_comments(src)
                if not hdr_en and cm_en < 3:
                    continue  # 视为已完成
                py_work.append((p, lines, cm_en, hdr_en))
            # ---- Markdown ----
            elif ext in (".md", ".mdx"):
                # 跳过 skills（已翻）、locale 变体（.es.md/.zh-CN.md/.ur-pk.md）、备份
                if n.endswith((".es.md", ".zh-CN.md", ".ur-pk.md", ".fr.md", ".ja.md", ".de.md")):
                    continue
                # skills 目录的 md 已单独翻译完，跳过
                if top == "skills":
                    continue
                if top in ("me-docs",):
                    continue  # 用户自己的中文笔记
                try:
                    txt = open(p, encoding="utf-8", errors="replace").read()
                except Exception:
                    continue
                cjk = len(CJK_RE.findall(txt))
                letters = sum(1 for c in txt if c.isalpha() and ord(c) < 128)
                # 有字母文字但几乎无中文 -> 未翻译
                if letters > 120 and cjk / max(letters, 1) < 0.08:
                    md_work.append((p, txt.count("\n") + 1, letters, cjk))

    # 输出
    py_out = io.StringIO()
    by_dir = {}
    for p, lines, cm_en, hdr_en in sorted(py_work):
        rel = os.path.relpath(p, ROOT).replace(os.sep, "/")
        d = rel.split("/")[0] if "/" in rel else "(top)"
        by_dir.setdefault(d, []).append((rel, lines, cm_en, hdr_en))
    total_py = 0
    for d in sorted(by_dir):
        items = by_dir[d]
        py_out.write(f"\n=== {d}/  ({len(items)} files) ===\n")
        for rel, lines, cm_en, hdr_en in sorted(items, key=lambda x: -x[1]):
            total_py += 1
            flag = "HDR-EN" if hdr_en else "hdrok"
            py_out.write(f"  {lines:6d}行 cm_en={cm_en:5d} [{flag}]  {rel}\n")
    py_out.write(f"\nTOTAL py to translate: {total_py}\n")

    md_out = io.StringIO()
    md_by_dir = {}
    for p, lines, letters, cjk in sorted(md_work):
        rel = os.path.relpath(p, ROOT).replace(os.sep, "/")
        d = rel.split("/")[0] if "/" in rel else "(top)"
        md_by_dir.setdefault(d, []).append((rel, lines, letters, cjk))
    total_md = 0
    for d in sorted(md_by_dir):
        items = md_by_dir[d]
        md_out.write(f"\n=== {d}/  ({len(items)} files) ===\n")
        for rel, lines, letters, cjk in sorted(items, key=lambda x: -x[2]):
            total_md += 1
            md_out.write(f"  {lines:6d}行 letters={letters:6d} cjk={cjk:4d}  {rel}\n")
    md_out.write(f"\nTOTAL md to translate: {total_md}\n")

    py_path = os.path.join(os.path.dirname(__file__), "worklist_py.txt")
    md_path = os.path.join(os.path.dirname(__file__), "worklist_md.txt")
    with open(py_path, "w", encoding="utf-8") as f:
        f.write(py_out.getvalue())
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_out.getvalue())

    # 控制台简报（ASCII 避免乱码）
    print(f"PY files to translate: {total_py}  (report -> {py_path})")
    for d in sorted(by_dir):
        items = by_dir[d]
        big = sum(1 for _, l, _, _ in items if l >= 1500)
        print(f"  {d:<20} {len(items):>4}  (>=1500行: {big})")
    print(f"\nMD files to translate: {total_md}  (report -> {md_path})")
    for d in sorted(md_by_dir):
        print(f"  {d:<20} {len(md_by_dir[d]):>4}")


if __name__ == "__main__":
    main()
