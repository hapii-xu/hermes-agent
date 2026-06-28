"""
针对 skill Python 文件的 AST 级深度审计 —— 可选诊断工具，而非安全门禁。

依据 SECURITY.md §2.4，Skills Guard 是进程内启发式判断（"有用 —— 但
不是边界"）。本模块是一个独立的可选诊断工具，用于标记在评审第三方
skill 代码时运维方可能想人工审视的动态导入 / 动态属性访问模式。
此处标记的每个模式都有其合法用途；产出是供人工评审的提示，
而非定论。

CLI：``hermes skills audit --deep``
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import List, Tuple

# (文件、行号、模式 ID、描述)
Finding = Tuple[str, int, str, str]

_IGNORED_DIRS = {"__pycache__", ".venv", "venv", "node_modules"}


def _scan_source(content: str, rel_path: str) -> List[Finding]:
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError, RecursionError):
        return []

    findings: List[Finding] = []

    class V(ast.NodeVisitor):
        def visit_Call(self, node):
            f = node.func
            # importlib.import_module(...)
            if isinstance(f, ast.Attribute) and f.attr == "import_module":
                findings.append((rel_path, node.lineno, "dynamic_import",
                                 "importlib.import_module() — loads arbitrary modules at runtime"))
            # __import__(<计算值>)
            elif isinstance(f, ast.Name) and f.id == "__import__":
                if node.args and not isinstance(node.args[0], ast.Constant):
                    findings.append((rel_path, node.lineno, "dynamic_import_computed",
                                     "__import__ with non-literal module name"))
            # getattr(obj, <计算值>)
            elif isinstance(f, ast.Name) and f.id == "getattr":
                if len(node.args) >= 2 and not isinstance(node.args[1], ast.Constant):
                    findings.append((rel_path, node.lineno, "dynamic_getattr",
                                     "getattr with non-literal attribute name"))
            self.generic_visit(node)

        def visit_Subscript(self, node):
            # obj.__dict__[<计算值>]
            if (isinstance(node.value, ast.Attribute)
                    and node.value.attr == "__dict__"
                    and not isinstance(node.slice, ast.Constant)):
                findings.append((rel_path, node.lineno, "dict_access",
                                 "__dict__[<computed>] — dynamic attribute access"))
            self.generic_visit(node)

        def visit_Import(self, node):
            for a in node.names:
                if a.name == "importlib" or a.name.startswith("importlib."):
                    findings.append((rel_path, node.lineno, "importlib_import",
                                     f"import {a.name} — enables dynamic module loading"))
            self.generic_visit(node)

        def visit_ImportFrom(self, node):
            m = node.module or ""
            if m == "importlib" or m.startswith("importlib."):
                findings.append((rel_path, node.lineno, "importlib_import",
                                 f"from {m} import ... — enables dynamic module loading"))
            self.generic_visit(node)

    try:
        V().visit(tree)
    except (RecursionError, ValueError, RuntimeError):
        # 恶意/病态输入：返回目前已收集到的结果。
        pass

    return findings


def ast_scan_path(path: Path) -> List[Finding]:
    """扫描单个 .py 文件，或递归扫描目录下所有 .py 文件。

    返回 (文件、行号、模式 ID、描述) 元组的列表。对于非 Python
    路径、缺失路径或没有匹配模式的路径，返回空列表。
    """
    if path.is_file():
        if path.suffix.lower() != ".py":
            return []
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        return _scan_source(content, path.name)

    if not path.is_dir():
        return []

    out: List[Finding] = []
    for py in sorted(path.rglob("*.py")):
        if set(py.parent.parts) & _IGNORED_DIRS:
            continue
        try:
            content = py.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            rel = py.relative_to(path).as_posix()
        except ValueError:
            rel = py.name
        out.extend(_scan_source(content, rel))
    return out


def format_ast_report(findings: List[Finding], skill_name: str = "") -> str:
    """纯文本报告（不含 Rich 标记），按文件分组。"""
    header = f"AST deep scan: {skill_name}" if skill_name else "AST deep scan"
    if not findings:
        return f"{header}\n  No dynamic import/access patterns detected."

    lines = [header, f"  {len(findings)} finding(s):"]
    current = None
    for f, line, pid, desc in sorted(findings):
        if f != current:
            current = f
            lines.append(f"  {f}")
        lines.append(f"    L{line}  {pid}  — {desc}")
    lines.append("")
    lines.append("  Note: diagnostic hints for human review, not security verdicts.")
    return "\n".join(lines)
