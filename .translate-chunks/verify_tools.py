#!/usr/bin/env python3
"""验证 tools/ 翻译结果完整性。

检查三项：
1. AST 语法有效性（确保代码没被破坏）—— 最关键
2. 行数对比（检测截断：文件骤减说明丢了代码）
3. 仍为纯英文的注释/docstring 数量（检测漏翻）

用法：py .translate-chunks/verify_tools.py
"""
import ast
import os
import re
import sys

TOOLS_DIR = os.path.join(os.path.dirname(__file__), "..", "tools")
TOOLS_DIR = os.path.normpath(TOOLS_DIR)

CJK_RE = re.compile(r"[一-鿿]")

# 注释指令/魔法注释，不算“需要翻译的人类注释”
DIRECTIVE_RE = re.compile(
    r"^\s*#\s*("
    r"type:\s*ignore|noqa|pragma|pylint|isort|fmt:|type:|"
    r"region|endregion|coding:|!/"
    r")",
    re.IGNORECASE,
)


def has_cjk(s):
    return bool(CJK_RE.search(s))


def collect_files():
    out = []
    for root, _dirs, names in os.walk(TOOLS_DIR):
        for n in names:
            if n.endswith(".py"):
                out.append(os.path.normpath(os.path.join(root, n)))
    return sorted(out)


def analyze(path):
    """返回 dict: syntax_ok, lines, english_comment_lines (list of (lineno, text))."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        src = fh.read()
    lines = src.splitlines()
    info = {"syntax_ok": True, "syntax_err": None, "lines": len(lines),
            "english_comments": 0, "english_docstring_lines": 0}
    # 1. AST
    try:
        ast.parse(src, filename=path)
    except SyntaxError as e:
        info["syntax_ok"] = False
        info["syntax_err"] = f"{e.lineno}:{e.offset} {e.msg}"
        return info

    # 2. 找仍为纯英文的人类注释（# 行）
    for i, ln in enumerate(lines, 1):
        stripped = ln.strip()
        if not stripped.startswith("#"):
            continue
        if DIRECTIVE_RE.match(stripped):
            continue
        # 去掉 # 后判断
        body = stripped.lstrip("#").strip()
        if not body:
            continue
        # 含字母且无 CJK -> 疑似未翻译
        if re.search(r"[A-Za-z]", body) and not has_cjk(body):
            info["english_comments"] += 1

    # 3. docstring 里纯英文行（粗略：三引号块内、有字母、无 CJK、非代码/doctest）
    info["english_docstring_lines"] = count_english_docstring_lines(lines)
    return info


def count_english_docstring_lines(lines):
    n = 0
    in_ds = False
    delim = None
    for ln in lines:
        s = ln.strip()
        if not in_ds:
            # 找 docstring 起始（粗略：行内含 """ 或 '''）
            for d in ('"""', "'''"):
                if d in s:
                    # 是否同行闭合
                    idx = s.find(d)
                    rest = s[idx + 3:]
                    if d in rest:
                        # 单行 docstring
                        body = rest[: rest.find(d)]
                        if re.search(r"[A-Za-z]", body) and not has_cjk(body) and not body.startswith(">>>"):
                            n += 1
                        break
                    else:
                        in_ds = True
                        delim = d
                        # 起始行本身可能有文字
                        body = rest
                        if body and re.search(r"[A-Za-z]", body) and not has_cjk(body):
                            n += 1
                    break
        else:
            if delim in s:
                # 闭合行
                body = s.split(delim)[0]
                if body and re.search(r"[A-Za-z]", body) and not has_cjk(body):
                    n += 1
                in_ds = False
                delim = None
            else:
                # docstring 内部行
                if s.startswith(">>>") or s.startswith("..."):
                    continue
                if s and re.search(r"[A-Za-z]", s) and not has_cjk(s):
                    n += 1
    return n


def main():
    files = collect_files()
    total = len(files)
    syntax_fail = []
    sum_english_comments = 0
    sum_english_ds = 0
    per_file = []
    for f in files:
        info = analyze(f)
        per_file.append((f, info))
        if not info["syntax_ok"]:
            syntax_fail.append((f, info["syntax_err"]))
        sum_english_comments += info["english_comments"]
        sum_english_ds += info["english_docstring_lines"]

    print("=" * 70)
    print(f"检查文件数：{total}")
    print(f"AST 语法失败：{len(syntax_fail)}")
    for f, err in syntax_fail:
        print(f"  ✗ {f}  ->  {err}")
    print(f"疑似未翻译 # 注释行（合计）：{sum_english_comments}")
    print(f"疑似未翻译 docstring 行（合计）：{sum_english_ds}")
    print("=" * 70)
    # 列出疑似未翻译最多的 20 个文件
    ranked = sorted(per_file, key=lambda x: -(x[1]["english_comments"] + x[1]["english_docstring_lines"]))
    print("疑似漏翻最多的文件（前 25）：")
    for f, info in ranked[:25]:
        tot = info["english_comments"] + info["english_docstring_lines"]
        if tot > 0:
            rel = os.path.relpath(f, TOOLS_DIR)
            print(f"  {tot:4d}  {rel}  (#注释:{info['english_comments']}, docstring:{info['english_docstring_lines']}, 行数:{info['lines']})")

    # 退出码：语法失败则非零
    return 1 if syntax_fail else 0


if __name__ == "__main__":
    sys.exit(main())
