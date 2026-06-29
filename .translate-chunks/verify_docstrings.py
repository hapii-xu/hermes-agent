#!/usr/bin/env python3
"""精确验证：用 AST 找出「真正的 docstring」（模块/类/函数），检查是否已翻译。

相比 verify_tools.py 的行扫描启发式，这里用 ast.get_docstring() 只统计
真正的 docstring（紧跟 def/class/module 的首条三引号串），不会把赋值用的
多行字符串字面量（schema description / system prompt / 模板）误判为漏翻。

同时仍统计 # 行内注释里的疑似未翻译行。
"""
import ast
import os
import re
import sys

TOOLS_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "tools"))
CJK_RE = re.compile(r"[一-鿿　-〿＀-￯]")
DIRECTIVE_RE = re.compile(
    r"^\s*#\s*(type:\s*ignore|noqa|pragma|pylint|isort|fmt:|type:|region|endregion|coding:|!/)",
    re.IGNORECASE,
)


def has_cjk(s):
    return bool(CJK_RE.search(s))


def collect_files():
    out = []
    for root, _d, names in os.walk(TOOLS_DIR):
        for n in names:
            if n.endswith(".py"):
                out.append(os.path.normpath(os.path.join(root, n)))
    return sorted(out)


def analyze(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        src = fh.read()
    lines = src.splitlines()
    info = {
        "syntax_ok": True, "syntax_err": None, "lines": len(lines),
        "ds_total": 0, "ds_untranslated": 0, "ds_samples": [],
        "cm_total": 0, "cm_untranslated": 0, "cm_samples": [],
    }
    try:
        tree = ast.parse(src, filename=path)
    except SyntaxError as e:
        info["syntax_ok"] = False
        info["syntax_err"] = f"{e.lineno}:{e.offset} {e.msg}"
        return info

    # —— 真正的 docstring（AST）——
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            ds = ast.get_docstring(node, clean=False)
            if ds is None:
                continue
            info["ds_total"] += 1
            # 取 docstring 首个有内容的非空行做判断
            first_meaningful = next((ln.strip() for ln in ds.splitlines() if ln.strip()), "")
            # 跳过纯代码/占位
            if first_meaningful.startswith(">>>"):
                continue
            if re.search(r"[A-Za-z]", first_meaningful) and not has_cjk(first_meaningful):
                info["ds_untranslated"] += 1
                if len(info["ds_samples"]) < 3:
                    info["ds_samples"].append(f"L{getattr(node, 'lineno', 0)}: {first_meaningful[:60]}")

    # —— # 行内注释 ——
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
            info["cm_untranslated"] += 1
            if len(info["cm_samples"]) < 3:
                info["cm_samples"].append(body[:60])
    return info


def main():
    files = collect_files()
    syntax_fail = []
    rows = []
    tot_ds_un, tot_cm_un = 0, 0
    for f in files:
        info = analyze(f)
        rows.append((os.path.relpath(f, TOOLS_DIR), info))
        if not info["syntax_ok"]:
            syntax_fail.append((f, info["syntax_err"]))
        tot_ds_un += info["ds_untranslated"]
        tot_cm_un += info["cm_untranslated"]

    print("=" * 72)
    print(f"files: {len(files)}")
    print(f"AST syntax failures: {len(syntax_fail)}")
    for f, err in syntax_fail:
        print(f"  FAIL {f} -> {err}")
    print(f"REAL docstrings untranslated: {tot_ds_un}")
    print(f"# inline comments untranslated: {tot_cm_un}")
    print("=" * 72)
    print("Files with untranslated docstrings (top 30):")
    ranked = sorted(rows, key=lambda x: -(x[1]["ds_untranslated"] + x[1]["cm_untranslated"]))
    for rel, info in ranked[:30]:
        tot = info["ds_untranslated"] + info["cm_untranslated"]
        if tot == 0:
            break
        print(f"  {tot:4d}  {rel}  (doc:{info['ds_untranslated']}, #cm:{info['cm_untranslated']})")
        for smp in info["ds_samples"][:2]:
            print(f"         ds | {smp}")
        for smp in info["cm_samples"][:2]:
            print(f"         cm | {smp}")
    return 1 if syntax_fail else 0


if __name__ == "__main__":
    sys.exit(main())
