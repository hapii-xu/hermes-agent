"""
`hermes computer-use doctor` —— cua-driver 的 `health_report` MCP 工具的轻量客户端。

cua-driver 拥有健康模型（`main` 分支上的 #1908 / be761fac）。本模块
只是驱动 stdio JSON-RPC 握手、调用 `health_report`，并渲染结构化
响应。当驱动新增检查项时，会经由这里而无需 Hermes 侧改动代码——
唯一的契约就是稳定的 `schema_version="1"` 负载结构。

退出码约定：
- 0: overall == "ok"
- 1: overall in ("degraded", "failed")
- 2: 驱动二进制缺失 / 不可达 / 协议错误
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional, Sequence


# 与 cua-driver 集成测试所固定的 ALLOWED_STATUS_VALUES + ALLOWED_OVERALL_VALUES
# 保持一致。若 health_report 扩展了词汇表，在此处补充。
_STATUS_GLYPH = {
    "pass": "✅",
    "fail": "❌",
    "skip": "⏭️",
}
_OVERALL_GLYPH = {
    "ok":       "✅",
    "degraded": "⚠️",
    "failed":   "❌",
}


def _cua_child_env() -> Dict[str, str]:
    """应用了 Hermes 遥测策略的 cua-driver 子进程环境。

    委托给 ``cua_backend.cua_driver_child_env``（除非用户主动开启，
    否则默认禁用遥测）。若该导入失败则回退到当前环境，使 doctor
    永远不会因遥测辅助函数的报错而中断。
    """
    try:
        from tools.computer_use.cua_backend import cua_driver_child_env

        return cua_driver_child_env()
    except Exception:
        return dict(os.environ)


def _drive_health_report(
    binary: str,
    *,
    include: Sequence[str] = (),
    skip: Sequence[str] = (),
    timeout: float = 12.0,
) -> Dict[str, Any]:
    """启动 `<binary> mcp`，执行 JSON-RPC 握手，调用
    `health_report`，并返回解析后的 `structuredContent` 字典。

    在协议级失败（二进制崩溃、响应格式错误、JSON-RPC 错误）时抛出
    `RuntimeError`。但当 `health_report` 存在失败的检查项时永不抛出
    异常——该工具的契约是始终返回一份结构良好的报告（带 `overall`
    字段），而绝不设置 `isError`。
    """
    args: Dict[str, Any] = {}
    if include:
        args["include"] = list(include)
    if skip:
        args["skip"] = list(skip)

    # cua-driver 输出 UTF-8（macOS 上检查消息中含 emoji，Windows 上
    # 含任意文件路径）。Python 默认的文本模式编码跟随系统区域设置——
    # 在默认的 Windows 安装上是 `cp1252`——遇到第一个非 ASCII 字节
    # 就会抛出 UnicodeDecodeError。这里固定使用该编码。
    proc = subprocess.Popen(
        [binary, "mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=_cua_child_env(),
    )
    try:
        # 1. initialize（初始化）
        proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": 1,
            "method": "initialize", "params": {},
        }) + "\n")
        proc.stdin.flush()
        init_line = proc.stdout.readline()
        if not init_line:
            stderr_tail = (proc.stderr.read() or "").strip().splitlines()[-3:]
            raise RuntimeError(
                f"cua-driver mcp produced no initialize response. "
                f"stderr tail: {stderr_tail or '(empty)'}"
            )

        # 2. tools/call health_report（调用 health_report）
        proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": 2,
            "method": "tools/call",
            "params": {"name": "health_report", "arguments": args},
        }) + "\n")
        proc.stdin.flush()
        call_line = proc.stdout.readline()
        if not call_line:
            raise RuntimeError("cua-driver mcp closed stdout without responding to health_report.")
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    try:
        resp = json.loads(call_line)
    except (ValueError, TypeError) as e:
        raise RuntimeError(f"health_report response was not valid JSON: {e}\nraw: {call_line[:200]}")

    if "error" in resp:
        raise RuntimeError(f"health_report JSON-RPC error: {resp['error']}")

    result = resp.get("result") or {}

    # 首选：structuredContent（cua-driver-rs 在 health_report 响应中始终
    # 会发送它）。对较早、未携带 structuredContent 的 cua-driver 构建版本，
    # 回退为把第一个 text 项当作 JSON 解析。
    sc = result.get("structuredContent")
    if isinstance(sc, dict):
        return sc

    for item in result.get("content", []):
        if item.get("type") == "text":
            text = item.get("text", "")
            try:
                # 许多 health_report 负载也把 JSON 放在 text 项里。
                parsed = json.loads(text)
                if isinstance(parsed, dict) and "schema_version" in parsed:
                    return parsed
            except (ValueError, TypeError):
                pass

    raise RuntimeError(
        "health_report response carried neither structuredContent nor a parseable "
        f"JSON text block. Result keys: {list(result.keys())}"
    )


def _print_text_report(report: Dict[str, Any], color: bool) -> None:
    """以与 `cua-driver call health_report` 相同的风格渲染报告
    （每项检查一行 + 一个摘要页脚）。"""
    schema = report.get("schema_version", "?")
    platform = report.get("platform", "?")
    driver_v = report.get("driver_version", "?")
    overall = report.get("overall", "?")

    header_glyph = _OVERALL_GLYPH.get(overall, "•")

    if color and overall in _OVERALL_GLYPH:
        # 不引入外部颜色库——将 ANSI 转义内联，使 doctor 命令
        # 保持为单一自包含模块。
        col_red = "\033[31m"
        col_yellow = "\033[33m"
        col_green = "\033[32m"
        col_reset = "\033[0m"
        col_dim = "\033[2m"
        col_for = {"failed": col_red, "degraded": col_yellow, "ok": col_green}.get(overall, "")
    else:
        col_red = col_yellow = col_green = col_reset = col_dim = ""
        col_for = ""

    print(
        f"{header_glyph} cua-driver {driver_v} on {platform} — "
        f"{col_for}{overall}{col_reset}"
    )

    for check in report.get("checks", []):
        name = check.get("name", "?")
        status = check.get("status", "?")
        glyph = _STATUS_GLYPH.get(status, "•")
        message = check.get("message") or ""
        if color:
            status_col = {
                "pass": col_green, "fail": col_red, "skip": col_dim,
            }.get(status, "")
            print(f"  {glyph} {status_col}{name}{col_reset}: {message}")
        else:
            print(f"  {glyph} {name}: {message}")
        hint = check.get("hint")
        if hint:
            print(f"      → {col_dim}{hint}{col_reset}")
        # `data` 是某些检查项附带的结构化负载（bundle id、AX 权限状态、
        # 版本三元组等）。存在时予以展示，因为用户 / 支持人员经常需要它。
        data = check.get("data")
        if isinstance(data, dict) and data:
            for key, value in data.items():
                rendered = value if not isinstance(value, (dict, list)) else json.dumps(value)
                print(f"      {col_dim}{key}={rendered}{col_reset}")
    _ = schema  # 标记该字段已被使用，供前向兼容的读取者参考


def run_doctor(
    driver_cmd: Optional[str] = None,
    *,
    include: Sequence[str] = (),
    skip: Sequence[str] = (),
    json_output: bool = False,
    color: Optional[bool] = None,
) -> int:
    """解析 cua-driver 二进制，调用 `health_report`，渲染结果。

    通过 `install_cua_driver` 与运行时后端共用的同一个 `_cua_driver_cmd()`
    解析器来识别 `HERMES_CUA_DRIVER_CMD`，这样 doctor 诊断的就是你的
    `computer_use` 工具集实际会调用的目标。
    """
    # Windows 的 stdout/stderr 被系统 ANSI 编解码器包裹（美式区域下
    # 为 `cp1252`，zh-CN 下为 `cp936` 等）。下方的检查矩阵输出中含
    # ✅ ❌ ⚠️ ⏭️ 这些字形——它们都无法用这些代码页编码。这里一次性、
    # 幂等地把 stdout 切换为 UTF-8：每个受支持的 TextIOWrapper（Py3.7+）
    # 都有 `.reconfigure`，且若原本已是 UTF-8，一次空操作的重编码代价
    # 也很低。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, OSError):
            pass
    if driver_cmd is None:
        try:
            from hermes_cli.tools_config import _cua_driver_cmd
            driver_cmd = _cua_driver_cmd()
        except Exception:
            driver_cmd = os.environ.get("HERMES_CUA_DRIVER_CMD") or "cua-driver"

    binary = shutil.which(driver_cmd)
    if not binary:
        print(f"cua-driver: not installed (looked for {driver_cmd!r}).")
        print("  Run: hermes computer-use install")
        return 2

    try:
        report = _drive_health_report(binary, include=include, skip=skip)
    except RuntimeError as e:
        print(f"cua-driver health_report failed: {e}", file=sys.stderr)
        return 2

    if json_output:
        json.dump(report, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        if color is None:
            color = sys.stdout.isatty()
        _print_text_report(report, color=bool(color))

    overall = report.get("overall")
    if overall in ("degraded", "failed"):
        return 1
    return 0
