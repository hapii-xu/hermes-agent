"""为系统提示词提供的本地环境工具链探测。

当终端后端为本地（agent 的工具运行在与 Hermes 本身相同的机器上）时，
我们给出关于 Python 工具链状态的一行确定性信息，使模型不必靠碰壁来
发现它。本探测解决的常见失败模式：

* Hermes 运行在某个 Python 下（例如打包 venv 里的 3.11），而用户登录
  shell 里是另一个（例如系统的 3.12）。从 PATH 解析出的 ``pip`` 可能
  与 ``python3 -m pip`` 不匹配。
* 打包 venv 的 Python 没有安装 pip 模块 → ``python3 -m pip`` 返回
  ``No module named pip``。
* 系统 Python 受 PEP-668 外部管理 → 朴素的 ``pip install`` 会以
  ``error: externally-managed-environment`` 失败。

该探测开销很小（少量 subprocess 调用，总计约 50ms），在进程生命周期内
缓存，且当检测到非默认情况时最多只输出 **一行简短信息**。当环境看起来
正常（python3 和 pip 都存在且匹配、无 PEP 668）时，它什么都不输出——
不产生 token 成本。

远端终端后端（docker、modal、ssh……）会被跳过：工具运行在沙箱内时，
宿主机的 Python 状态无关紧要。沙箱有自己现成的探测
（``agent/prompt_builder.py`` 中的 ``_probe_remote_backend``）。

通过 config.yaml 中的 ``agent.environment_probe`` 切换（默认 True）。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
from typing import Optional

logger = logging.getLogger(__name__)

# 模块级缓存。探测结果在进程生命周期内是确定的——Python 安装状态
# 不会在会话中途以任何对系统提示词有意义的方式发生变化。
_CACHE_LOCK = threading.Lock()
_CACHED_LINE: Optional[str] = None  # None = 尚未探测；"" = 已探测，无可说。

# 远端后端——与 agent/prompt_builder.py:_REMOTE_TERMINAL_BACKENDS 保持同步。
# 这里是复制而非导入，以避免循环导入（prompt_builder 不从 tools 导入任何东西）。
_REMOTE_BACKENDS = frozenset({
    "docker", "singularity", "modal", "daytona", "ssh", "managed_modal",
})


def _run(cmd: list[str], timeout: float = 3.0) -> tuple[int, str, str]:
    """运行一个短小的子进程。返回 (returncode, stdout, stderr)。

    失败（二进制缺失、超时、OSError）返回 (-1, "", "<原因>")。
    """
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
        )
        return result.returncode, (result.stdout or "").strip(), (result.stderr or "").strip()
    except FileNotFoundError:
        return -1, "", "not found"
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except OSError as exc:
        return -1, "", f"oserror: {exc}"


def _python_version_of(binary: str) -> Optional[str]:
    """返回 ``binary`` 的简短版本字符串（如 ``3.12.4``），无法获取则返回 None。"""
    if not shutil.which(binary):
        return None
    rc, out, err = _run([binary, "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')"])
    if rc == 0 and out:
        return out
    return None


def _has_pip_module(binary: str) -> bool:
    """当 ``<binary> -m pip --version`` 成功时返回 True。"""
    if not shutil.which(binary):
        return False
    rc, _out, _err = _run([binary, "-m", "pip", "--version"])
    return rc == 0


def _detect_pep668(binary: str) -> bool:
    """当 ``<binary>`` 的安装位置受 PEP-668 外部管理时返回 True。

    查找标准库旁边的 ``EXTERNALLY-MANAGED`` 文件（Debian/Ubuntu 为
    拦截朴素 ``pip install`` 而放入的标记文件）。
    """
    if not shutil.which(binary):
        return False
    code = (
        "import sys, os;"
        "stdlib = os.path.dirname(os.__file__);"
        "marker = os.path.join(stdlib, 'EXTERNALLY-MANAGED');"
        "print('yes' if os.path.exists(marker) else 'no')"
    )
    rc, out, _err = _run([binary, "-c", code])
    return rc == 0 and out.strip() == "yes"


def _pip_python_version() -> Optional[str]:
    """若 ``pip`` 在 PATH 上，返回它所绑定的 Python 版本。

    ``pip --version`` 的输出形如::

        pip 24.0 from /usr/lib/python3/dist-packages/pip (python 3.12)

    返回括号中的版本（如 ``"3.12"``），否则返回 None。
    """
    if not shutil.which("pip"):
        return None
    rc, out, _err = _run(["pip", "--version"])
    if rc != 0 or not out:
        return None
    # 解析末尾的 "(python X.Y)"。
    if "(python " in out and out.endswith(")"):
        try:
            tail = out.rsplit("(python ", 1)[1]
            return tail[:-1].strip()
        except (IndexError, AttributeError):
            return None
    return None


def _build_probe_line() -> str:
    """构建那一行信息。未检测到值得注意的情况时返回 ""。

    仅当出现异常时才输出——目的是让模型免于撞上一个本可避免的墙，
    而不是去叙述一个健康的环境。
    """
    # 若配置了远端终端后端则退出；agent 工具并不运行在宿主机的
    # Python 环境上。
    backend = (os.getenv("TERMINAL_ENV") or "local").strip().lower()
    if backend in _REMOTE_BACKENDS:
        return ""

    py3_ver = _python_version_of("python3")
    py_ver = _python_version_of("python")  # 用于带 `python` 别名的系统
    py3_has_pip = _has_pip_module("python3") if py3_ver else False
    pip_bound_to = _pip_python_version()
    py3_pep668 = _detect_pep668("python3") if py3_ver else False
    has_uv = shutil.which("uv") is not None

    # 若 python3 存在、有 pip、有 uv（或无 PEP 668），且 `pip` 与
    # `python3` 之间无版本不匹配 → 环境足够干净，可以保持静默。
    # 模型若在意细节，可自行运行命令来发现。
    mismatch = bool(pip_bound_to and py3_ver and not py3_ver.startswith(pip_bound_to))
    silent_conditions = (
        py3_ver is not None
        and py3_has_pip
        and not mismatch
        and (not py3_pep668 or has_uv)
    )
    if silent_conditions:
        return ""

    # 构建一段紧凑的事实摘要。保持在一行内，以免它喧宾夺主占据
    # 提示词；模型擅长解析高密度信息。
    bits: list[str] = []
    if py3_ver:
        py3_bit = f"python3={py3_ver}"
        if not py3_has_pip:
            py3_bit += " (no pip module)"
        bits.append(py3_bit)
    else:
        bits.append("python3=missing")

    if py_ver and py_ver != py3_ver:
        bits.append(f"python={py_ver}")
    elif not py_ver and py3_ver:
        # 在 Debian/Ubuntu 上很常见——点出来，免得模型敲 `python`
        # 撞上 "command not found"。
        bits.append("python=missing (use python3)")

    if pip_bound_to:
        if mismatch:
            bits.append(f"pip→python{pip_bound_to} (mismatch)")
        elif not py3_has_pip:
            # pip 存在但 `python3 -m pip` 不存在——脚本路径可用，
            # 但模块路径不可用。
            bits.append(f"pip→python{pip_bound_to}")
    elif py3_has_pip:
        # `pip` 不在 PATH 上，但 `python3 -m pip` 可用。
        pass
    else:
        bits.append("pip=missing")

    if py3_pep668:
        bits.append("PEP 668=yes (use venv or uv)")

    if has_uv:
        bits.append("uv=installed")

    if not bits:
        return ""

    return "Python toolchain: " + ", ".join(bits) + "."


def get_environment_probe_line(*, force_refresh: bool = False) -> str:
    """返回缓存的探测行（首次调用时构建）。

    当环境干净时返回 ""——此时系统提示词组装器应丢弃该小节，
    而不是输出一个空标题。

    ``force_refresh`` 供测试使用；真实调用方永远不需要它。
    """
    global _CACHED_LINE
    if force_refresh:
        with _CACHE_LOCK:
            _CACHED_LINE = None

    if _CACHED_LINE is not None:
        return _CACHED_LINE

    with _CACHE_LOCK:
        if _CACHED_LINE is not None:  # 发生了竞态
            return _CACHED_LINE
        try:
            line = _build_probe_line()
        except Exception as exc:  # 永不让探测失败阻塞提示词构建
            logger.debug("env_probe failed: %s", exc)
            line = ""
        _CACHED_LINE = line
        return line


def _reset_cache_for_tests() -> None:
    """测试辅助函数——在探测场景之间清空缓存。"""
    global _CACHED_LINE
    with _CACHE_LOCK:
        _CACHED_LINE = None
