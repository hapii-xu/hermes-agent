#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全项目扫描：找出「文件级未翻译」的源文件（注释/docstring 仍是英文）。

判断依据（沿用 verify_docstrings.py 的 AST + CJK 启发式，推广到整个仓库）：
  - Python：取模块/类/函数的【真正 docstring】（AST），统计仍为纯英文（有字母、无 CJK）的数量。
  - 其它注释类（# 行）也统计。
  - 文件级判定：若该文件的「首个真正 docstring」无 CJK，或英文 docstring 比例高，
    视为未翻译（与"翻译过的文件头部注释必已完成"的直觉一致）。

输出：未翻译文件清单（按未翻译量降序），写到 stdout 和 .translate-chunks/scan_all_report.txt。
"""
import ast
import os
import re
import sys
import io

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
CJK_RE = re.compile(r"[一-鿿　-〿＀-￯㐀-䶿]")
DIRECTIVE_RE = re.compile(
    r"^\s*#\s*(type:\s*ignore|noqa|pragma|pylint|isort|fmt:|type:|region|endregion|coding:|!/)",
    re.IGNORECASE,
)

# 跳过这些目录：备份、依赖、构建产物、翻译流水线自身、生成产物
SKIP_DIRS = {
    ".git", "node_modules", "skills-en-backup", ".translate-chunks",
    "__pycache__", ".venv", "venv", "env", ".tox", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "dist", "build", ".next", "target",
    ".plans", "site-packages",
}
# 跳过明显是数据/产物/锁文件
SKIP_EXT = {
    ".pyc", ".pyo", ".lock", ".svg", ".png", ".jpg", ".jpeg", ".gif",
    ".pdf", ".woff", ".woff2", ".ttf", ".eot", ".ico", ".webp",
    ".zip", ".gz", ".tar", ".bin", ".dat", ".xsd", ".ipynb",
}


def has_cjk(s):
    return bool(CJK_RE.search(s))


def should_skip_dir(d):
    return d in SKIP_DIRS


def collect_py_files():
    out = []
    for root, dirs, names in os.walk(ROOT):
        dirs[:] = [d for d in dirs if not should_skip_dir(d)]
        for n in names:
            ext = os.path.splitext(n)[1].lower()
            if ext == ".py":
                out.append(os.path.normpath(os.path.join(root, n)))
    return sorted(out)


def analyze_py(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        src = fh.read()
    lines = src.splitlines()
    info = {
        "syntax_ok": True, "lines": len(lines),
        "ds_total": 0, "ds_en": 0, "cm_total": 0, "cm_en": 0,
        "header_translated": None,  # None=无 docstring, True/False
    }
    try:
        tree = ast.parse(src, filename=path)
    except SyntaxError:
        info["syntax_ok"] = False
        return info

    # docstrings
    first_ds_seen = False
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            try:
                ds = ast.get_docstring(node, clean=False)
            except Exception:
                ds = None
            if ds is None:
                continue
            info["ds_total"] += 1
            first_meaningful = next((ln.strip() for ln in ds.splitlines() if ln.strip()), "")
            if first_meaningful.startswith(">>>"):
                continue
            is_en = bool(re.search(r"[A-Za-z]", first_meaningful)) and not has_cjk(first_meaningful)
            info["ds_en"] += 1 if is_en else 0
            # 用「模块 docstring」判断文件头是否已翻译
            if isinstance(node, ast.Module) and not first_ds_seen:
                first_ds_seen = True
                if first_meaningful and not first_meaningful.startswith(">>>"):
                    info["header_translated"] = not is_en

    # inline comments
    for ln in lines:
        s = ln.strip()
        if not s.startswith("#"):
            continue
        if DIRECTIVE_RE.match(s):
            continue
        body = s.lstrip("#").strip()
        if not body:
            continue
        info["cm_total"] += 1
        if re.search(r"[A-Za-z]", body) and not has_cjk(body):
            info["cm_en"] += 1
    return info


def main():
    files = collect_py_files()
    rows = []
    for f in files:
        info = analyze_py(f)
        rows.append((f, info))

    # 判定「需要翻译」的文件：英文 docstring>0 或 英文注释较多(>5)
    need = []
    for f, info in rows:
        score = info["ds_en"] + (1 if info["cm_en"] > 5 else 0)
        if score <= 0:
            continue
        need.append((f, info, score))

    need.sort(key=lambda x: -x[2])

    out = io.StringIO()

    def w(s=""):
        out.write(s + "\n")

    w("=" * 78)
    w(f"扫描 Python 文件总数：{len(files)}")
    w(f"「需要翻译」的文件数：{len(need)}")
    tot_ds_en = sum(info["ds_en"] for _, info, _ in need)
    tot_cm_en = sum(info["cm_en"] for _, info, _ in need)
    w(f"其中仍为英文的 docstring 数：{tot_ds_en}")
    w(f"其中仍为英文的 # 注释数（合计）：{tot_cm_en}")
    w("=" * 78)
    w("【需要翻译的文件清单】（按未翻译量降序，仅列前 400）：")
    for f, info, score in need[:400]:
        rel = os.path.relpath(f, ROOT)
        w(f"  [{score:4d}] {rel}  (doc_en:{info['ds_en']}, cm_en:{info['cm_en']}, 行:{info['lines']})")
    if len(need) > 400:
        w(f"  ... 另有 {len(need) - 400} 个文件未列出")

    report = out.getvalue()
    print(report)
    rep_path = os.path.join(os.path.dirname(__file__), "scan_all_report.txt")
    with open(rep_path, "w", encoding="utf-8") as fh:
        fh.write(report)
    print(f"\n[报告已写入 {rep_path}]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
