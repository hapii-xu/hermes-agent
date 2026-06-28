"""
Hermes Agent 可选后端的懒安装器。

许多 Hermes 功能（Mistral TTS、ElevenLabs TTS、Honcho memory、Bedrock、
Slack、Matrix 等）需要并非每个用户都需要的 Python 包。历史的做法是把它们
全部打包到 ``pyproject.toml`` 的 extras（``hermes-agent[all]``）里，并在
安装时立即安装。这有两个问题：

1. **脆弱性。** 当某个 extra 的传递依赖在 PyPI 上变得不可用（因恶意软件
   被隔离、被撤回、上传损坏）时，*整个* ``[all]`` 的解析会失败，新安装
   会静默回退到一个精简层级——一次丢失 10+ 个不相关的 extras。

2. **臃肿。** 一个只跟一个 provider 对话的用户会拉入几百个他们永远不会
   import 的包。

懒安装模式同时解决了这两个问题。后端在它们首次导入路径的顶部调用
:func:`ensure`。如果依赖缺失，``ensure`` 会检查 ``security.allow_lazy_installs``
配置标志（默认为 true）并运行一次 venv 范围的 pip install。如果用户已
显式禁用懒安装，``ensure`` 会抛出 :class:`FeatureUnavailable`，并附带
指向 ``hermes tools`` 或手动 pip 命令的明确修复提示。

安全模型：

* **默认限定在 venv 内。** 安装目标为活动 venv 中的 ``sys.executable``。
  我们绝不触碰系统 Python。
* **持久目标模式（不可变镜像）。** 当部署封闭了 agent 自己的 venv
  （Docker 镜像设置 ``HERMES_DISABLE_LAZY_INSTALLS=1`` 并把 ``/opt/hermes``
  设为只读）时，设置 ``HERMES_LAZY_INSTALL_TARGET`` 会把懒安装重定向到
  持久数据卷上的一个可写目录（例如 ``/opt/data/lazy-packages``）。该目录
  被**追加到 ``sys.path`` 的末尾**——绝不前插，绝不通过 ``PYTHONPATH``
  导出——因此 agent 自己的 site-packages 在每次名称冲突时都获胜。以这种方式
  安装的包只能新增可导入的模块；它绝不能遮蔽、降级或破坏核心已内置的模块。
  一个糟糕/不兼容的后端包能做的最坏的事就是导入失败并报告自己不可用——
  agent 核心保持健康。这就是"一个懒安装的包无法搞坏 Hermes"的结构性保证，
  也正是当初能安全封闭 venv 的原因。跨镜像重建的编译 wheel 安全性由目标
  子目录上的 ABI/Python 版本戳记来处理（见 :func:`_ensure_target_ready`）。
* **仅按包名从 PyPI 安装。** spec 可以是 ``"package>=1.0,<2"`` 等。我们
  *不支持* ``--index-url`` 覆盖、``git+https://``、file: 路径，或任何其他
  可能被恶意配置劫持的输入。
* **白名单。** 只有出现在 :data:`LAZY_DEPS` 中的 spec 才能通过此路径安装。
  feature 名拼错不会让用户得到"安装任何东西"的语义。
* **可退出。** 在 ``config.yaml`` 中设置 ``security.allow_lazy_installs: false``
  会在两种模式下都禁用运行时安装。处于受限网络或严格安全姿态的用户可以
  把自己固定在安装时已安装的内容上。
* **离线检测。** 如果安装失败（离线、镜像宕机、PyPI 404/隔离），我们会
  将失败作为 :class:`FeatureUnavailable` 暴露出来，并附带实际的 pip
  stderr——不会静默重试，不会缓存坏状态。

新增后端：

1. 在 :data:`LAZY_DEPS` 中添加一个条目，写明包 spec。
2. 在后端模块导入路径的顶部，在一个 try/except 中调用
   ``ensure("feature.name")``，把 :class:`FeatureUnavailable` 转换为一个
   有用的运行时错误。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import site
import subprocess
import sys
import sysconfig
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


# =============================================================================
# 可懒安装后端的白名单。
#
# 键是以点分隔的 feature 名（"namespace.backend"）。值是可被 pip 安装的
# spec 元组，与 pyproject.toml 中对应的 extra 一致。框架强制只有来自此
# 映射表的 spec 才能流入 pip install 命令。
# =============================================================================


LAZY_DEPS: dict[str, tuple[str, ...]] = {
    # ─── 推理 provider ───────────────────────────────────────────────
    # 原生 Anthropic SDK —— 当 provider=anthropic 时需要（不是通过
    # OpenRouter / 聚合器，后者使用 openai SDK）。
    "provider.anthropic": ("anthropic==0.87.0",),  # CVE-2026-34450, CVE-2026-34452
    # AWS Bedrock provider
    "provider.bedrock": ("boto3==1.42.89",),
    # Microsoft Foundry —— Entra ID 认证（托管标识、工作负载标识、
    # 服务主体、az login、VS Code、azd、PowerShell）。仅在选择了
    # model.auth_mode=entra_id 时加载；基于密钥的 azure-foundry 用户
    # 永远不为这个导入付费。
    "provider.azure_identity": ("azure-identity==1.25.3",),

    # ─── 网页搜索后端 ───────────────────────────────────────────────
    "search.exa": ("exa-py==2.10.2",),
    "search.firecrawl": ("firecrawl-py==4.17.0",),
    "search.parallel": ("parallel-web==0.4.2",),

    # ─── TTS provider ─────────────────────────────────────────────────
    # 固定到精确版本以匹配 pyproject.toml 的无范围策略
    # （见 [project.dependencies] 顶部的注释）。升级时，同时更新此映射表
    # AND pyproject.toml 中对应的 extra。
    #
    # mistralai 的版本钉跟随 pyproject.toml 的 `mistral` extra。PyPI
    # 于 2026-05-12 隔离了该项目（恶意的 2.4.6，Mini Shai-Hulud）；
    # 2.4.6 已被移除，干净的发布已恢复（2.4.7、2.4.8）。Voxtral
    # STT + TTS 共用同一个 SDK。
    "tts.mistral": ("mistralai==2.4.8",),
    "tts.edge": ("edge-tts==7.2.7",),
    "tts.elevenlabs": ("elevenlabs==1.59.0",),

    # ─── 语音转文本 provider ──────────────────────────────────────────
    "stt.mistral": ("mistralai==2.4.8",),
    "stt.faster_whisper": (
        "faster-whisper==1.2.1",
        "sounddevice==0.5.5",
        "numpy==2.4.3",
    ),

    # ─── 图像生成后端 ─────────────────────────────────────────────────
    "image.fal": ("fal-client==0.13.1",),

    # ─── 记忆 provider ──────────────────────────────────────────────────
    "memory.honcho": ("honcho-ai==2.0.1",),
    "memory.hindsight": ("hindsight-client==0.6.1",),

    # ─── 消息平台（按需懒安装）──────────────────────────────────────────
    "platform.telegram": ("python-telegram-bot[webhooks]==22.6",),
    # brotlicffi 为 aiohttp 提供可用的 2 参数 Decompressor.process()，用于
    # Discord CDN 的 Brotli 编码附件。没有它，aiohttp 会回退到 google 的
    # `Brotli` 包（1 参数 API），任何上传到 Discord 网关的 .txt/.md/.doc
    # 都会在 att.read() 处以 "Can not decode content-encoding: br" 解码
    # 失败 —— 见 #12511 / #15744。
    "platform.discord": ("discord.py[voice]==2.7.1", "brotlicffi==1.2.0.1"),
    "platform.slack": (
        "slack-bolt==1.27.0",
        "slack-sdk==3.40.1",
        "aiohttp==3.13.4",  # CVE-2026-34513/34518/34519/34520/34525
    ),
    "platform.matrix": (
        "mautrix[encryption]==0.21.0",
        "aiosqlite==0.22.1",
        "asyncpg==0.31.0",
        "aiohttp-socks==0.11.0",
    ),
    "platform.dingtalk": (
        "dingtalk-stream==0.24.3",
        "alibabacloud-dingtalk==2.2.42",
        "qrcode==7.4.2",
    ),
    "platform.feishu": (
        "lark-oapi==1.5.3",
        "qrcode==7.4.2",
    ),
    # 企业微信（WeCom）回调模式适配器 —— 解析不受信任的 XML POST 体。
    # 只拉入 defusedxml；aiohttp/httpx 是每个消息适配器的核心依赖，通过
    # `platform.discord` / `platform.slack` 等附带。
    "platform.wecom_callback": ("defusedxml==0.7.1",),
    # Microsoft Teams 适配器 —— microsoft-teams-apps 拉入一棵很重的依赖树
    # （microsoft-teams-api/cards/common、dependency-injector、msal）。像
    # 其他每个消息平台一样按需懒安装；也作为 `teams` extra 暴露在
    # pyproject 中，供打包者/显式安装使用。
    "platform.teams": ("microsoft-teams-apps==2.0.13.4", "aiohttp==3.13.4"),

    # ─── 终端后端 ─────────────────────────────────────────────────────
    "terminal.modal": ("modal==1.3.4",),
    "terminal.daytona": ("daytona==0.155.0",),

    # ─── 技能（Skills）───────────────────────────────────────────────────
    "skill.google_workspace": (
        "google-api-python-client==2.194.0",
        "google-auth-oauthlib==1.3.1",
        "google-auth-httplib2==0.3.1",
    ),
    "skill.youtube": ("youtube-transcript-api==1.2.4",),

    # ─── 工具 ─────────────────────────────────────────────────────
    # ACP 适配器（VS Code / Zed / JetBrains 集成）
    "tool.acp": ("agent-client-protocol==0.9.0",),
    # 仪表盘（`hermes dashboard`）
    "tool.dashboard": (
        "fastapi==0.133.1",
        "uvicorn[standard]==0.41.0",
        "starlette==1.0.1",  # CVE-2026-48710 (BadHost) —— 保持懒安装与 pyproject [web] 同步
        "python-multipart==0.0.27",  # FastAPI UploadFile/Form 用于流式上传 (NS-501)
    ),
    # 视觉图像缩放恢复（Pillow）。Pillow 现在是一个核心依赖
    # （pyproject 的 `dependencies`），所以这个条目是一个兜底加固措施，
    # 针对以某种方式丢掉了它的精简/源码构建安装。视觉调用点使用
    # prompt=False，因此它永远不会在会话中途抛出一个阻塞的 input()
    # 提示（#40490）。
    "tool.vision": ("Pillow==12.2.0",),
    # Computer Use（cua-driver）—— 用于通过 stdio 启动并与 cua-driver
    # 进程通信的 MCP 客户端 SDK。与 pyproject.toml 中的 `mcp` /
    # `computer-use` extra 一致。一键安装器通过 `[all]` 拉入它；在这里
    # 懒安装覆盖了精简/部分/extra 损坏的安装，使 computer_use 永远不会
    # 在 `No module named 'mcp'` 处死胡同。
    "tool.computer_use": (
        "mcp==1.26.0",
        "starlette==1.0.1",  # CVE-2026-48710 —— 保持与 pyproject [computer-use] 同步
    ),
}


# 用于 spec 校验的保守正则 —— 包名加可选的版本范围。拒绝任何看起来像
# URL、文件路径或 shell 元字符的内容。
_SAFE_SPEC = re.compile(
    r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*"        # 包名
    r"(?:\[[A-Za-z0-9_,\-]+\])?"            # 可选的 [extras]
    r"(?:[<>=!~]=?[A-Za-z0-9_.\-+,*<>=!~]+)?"  # 可选的版本说明符
    r"$"
)


class FeatureUnavailable(RuntimeError):
    """一个可懒安装的功能缺失且无法使其可用。

    要么依赖从未安装且用户已禁用懒安装，要么安装尝试失败。
    """

    def __init__(self, feature: str, missing: tuple[str, ...], reason: str):
        self.feature = feature
        self.missing = missing
        self.reason = reason
        super().__init__(self._format())

    def _format(self) -> str:
        spec_list = " ".join(repr(s) for s in self.missing)
        return (
            f"Feature {self.feature!r} unavailable: {self.reason}. "
            f"To enable manually: uv pip install {spec_list}  "
            f"(or: pip install {spec_list})."
        )


@dataclass(frozen=True)
class _InstallResult:
    success: bool
    stdout: str
    stderr: str


# =============================================================================
# 内部实现
# =============================================================================


# 把懒安装从（已封闭的）agent venv 重定向到持久卷上一个可写目录的环境变量。
# 由 Docker 镜像设置为 /opt/data/lazy-packages。这是一个内部桥接变量，
# 不是面向用户的配置：面向用户的旋钮仍然是 config.yaml 中的
# security.allow_lazy_installs。未设置时，懒安装照旧进入活动 venv。
_LAZY_TARGET_ENV = "HERMES_LAZY_INSTALL_TARGET"

# 写入目标目录的戳记文件名，记录它所填充的 Python X.Y + ABI。如果容器
# 重建升级了解释器，持久存储中的编译 wheel（.so）会 ABI 不兼容；我们
# 检测到不匹配就清空存储，让包针对新解释器重新解析，而不是导入一个过期
# 的 .so。
_TARGET_STAMP_NAME = ".python-abi"


def _python_abi_tag() -> str:
    """标识当前运行解释器 ABI 的稳定令牌。

    将 X.Y 版本与 EXT_SUFFIX（它编码了 ABI 标签和平台，例如
    ``cpython-313-x86_64-linux-gnu``）组合在一起。两个能共享编译 wheel
    的解释器会产生相同的令牌。
    """
    ver = f"{sys.version_info.major}.{sys.version_info.minor}"
    ext = sysconfig.get_config_var("EXT_SUFFIX") or ""
    return f"{ver}:{ext}"


def _lazy_install_target() -> Optional[Path]:
    """返回持久安装目标目录，venv 范围模式则返回 None。

    仅当 :data:`_LAZY_TARGET_ENV` 被设置为非空值时才返回路径。该目录由
    :func:`_ensure_target_ready` 按需创建。
    """
    raw = os.environ.get(_LAZY_TARGET_ENV, "").strip()
    if not raw:
        return None
    return Path(raw)


def _ensure_target_ready(target: Path) -> Optional[str]:
    """创建目标目录并校验其 ABI 戳记。

    如果戳记缺失则写入。如果戳记存在但记录的解析器 ABI 与当前运行的不同
    （例如容器镜像重建到了更新的 Python 上），则清空目录内容并重写戳记，
    这样过期的编译 wheel 就不会针对不兼容的解释器被导入。

    成功时返回 ``None``，如果目录无法创建/写入（例如只读挂载、权限错误）
    则返回一个错误字符串。
    """
    want = _python_abi_tag()
    stamp = target / _TARGET_STAMP_NAME
    try:
        if target.exists():
            have = ""
            try:
                have = stamp.read_text(encoding="utf-8").strip()
            except (OSError, FileNotFoundError):
                have = ""
            if have and have != want:
                logger.info(
                    "Lazy install target %s was built for ABI %r but running "
                    "ABI is %r; wiping stale packages.",
                    target, have, want,
                )
                for child in target.iterdir():
                    if child.is_dir() and not child.is_symlink():
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        try:
                            child.unlink()
                        except OSError:
                            pass
        target.mkdir(parents=True, exist_ok=True)
        stamp.write_text(want, encoding="utf-8")
    except OSError as e:
        return f"lazy install target {target} is not writable: {e}"
    return None


def _activate_target_on_syspath(target: Path) -> None:
    """把持久目标追加到 ``sys.path``，使其包可被导入。

    追加到末尾（绝不前插），这样 agent 自己的 venv site-packages 在每次
    名称冲突时都优先。幂等。使用 :func:`site.addsitedir` 使目标内的
    ``.pth`` 文件（命名空间包、可编辑安装）生效，然后强制执行追加顺序
    ——否则 ``addsitedir`` 会插入到靠近前部的位置。
    """
    target_str = str(target)
    # 快照已有条目，以便之后恢复优先级。
    before = list(sys.path)
    if target_str not in before:
        site.addsitedir(target_str)
    # site.addsitedir 可能已把目标（以及任何 .pth 添加的目录）插入到了
    # 前部。把每个新加入的条目移到末尾，保留核心 venv 的优先级。新条目
    # 是那些在 `before` 中不存在的条目。
    new_entries = [p for p in sys.path if p not in before]
    if new_entries:
        sys.path[:] = [p for p in sys.path if p not in new_entries] + new_entries
    # importlib.metadata 会缓存基于路径的发行版查找器；清空它，使一个
    # 刚激活的目录对本进程内的 version() 检查可见。
    try:
        import importlib
        importlib.invalidate_caches()
    except Exception:
        pass


def activate_durable_lazy_target() -> None:
    """公开接口：把持久懒安装目标挂到 ``sys.path`` 上。

    当 :data:`_LAZY_TARGET_ENV` 未设置或目录尚不存在时，是一个安全的
    no-op。在进程启动早期（后端导入之前）调用一次，使上一次运行安装到
    持久存储中的包在本次运行中可导入。永不抛异常。
    """
    target = _lazy_install_target()
    if target is None:
        return
    try:
        if target.exists():
            _activate_target_on_syspath(target)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("Failed to activate durable lazy target %s: %s", target, e)


def _allow_lazy_installs() -> bool:
    """返回当前环境是否允许懒安装。

    解析顺序：

    1. config.yaml 中的 ``security.allow_lazy_installs: false`` 是绝对的
       退出选项——它在 venv 范围模式和持久目标模式下都会禁用安装。这是
       面向用户的杀开关。
    2. ``HERMES_DISABLE_LAZY_INSTALLS=1`` 封闭了 *agent venv*（由不可变
       Docker 镜像设置）。它阻止 venv 范围的安装——除非配置了持久安装
       目标，此时安装会被重定向到那里（一条在结构上无法破坏已封闭 venv
       的路径），因此被允许。

    默认为 True。如果配置不可读，我们失败放行（允许），因为拒绝安装会
    把人锁在他们自己的后端之外；阻止的决定是一个显式的用户 opt-in。
    """
    # (1) 配置杀开关在任何模式下都优先。
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
    except Exception:
        cfg = None
    if cfg is not None:
        sec = cfg.get("security") or {}
        if not bool(sec.get("allow_lazy_installs", True)):
            return False

    # (2) 封闭 venv 的环境变量：仅在没有可重定向的安全持久目标时才阻止。
    # 设置了目标时，安装会进入数据卷（在 sys.path 上只追加），因此封闭
    # 得以保留。
    if os.environ.get("HERMES_DISABLE_LAZY_INSTALLS") == "1":
        return _lazy_install_target() is not None

    return True


def _spec_is_safe(spec: str) -> bool:
    """拒绝包含 URL、路径或 shell 元字符的 pip spec。"""
    if not spec or len(spec) > 200:
        return False
    if any(ch in spec for ch in (";", "|", "&", "`", "$", "\n", "\r", "\t", "\\")):
        return False
    if spec.startswith(("-", "/", ".")) or "://" in spec or "@" in spec:
        return False
    return bool(_SAFE_SPEC.match(spec))


def _pkg_name_from_spec(spec: str) -> str:
    """从一个 pip spec 中提取裸包名。

    ``"slack-bolt>=1.18.0,<2"`` → ``"slack-bolt"``
    ``"mautrix[encryption]>=0.20"`` → ``"mautrix"``
    """
    m = re.match(r"^([A-Za-z0-9_][A-Za-z0-9_.\-]*)", spec)
    return m.group(1) if m else spec


def _specifier_from_spec(spec: str) -> str:
    """仅提取 pip spec 的版本说明符部分。

    ``"honcho-ai==2.0.1"`` → ``"==2.0.1"``
    ``"mautrix[encryption]>=0.20,<1"`` → ``">=0.20,<1"``
    ``"package"`` → ``""``（无版本约束）
    """
    # 剥离包名 + 可选的 [extras] 块。
    m = re.match(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*(?:\[[A-Za-z0-9_,\-]+\])?", spec)
    if not m:
        return ""
    return spec[m.end():]


def _is_satisfied(spec: str) -> bool:
    """``spec`` 在当前环境中是否已被满足？

    同时检查存在性 AND 版本。如果包安装的版本落在 spec 范围之外，返回
    False，使调用方会升级/降级到锁定的版本。这正是 ``hermes update``
    能把 :data:`LAZY_DEPS` 中的版本钉变动传播到已安装后端的原因，而不是
    静默地留下过期版本。

    如果 ``packaging`` 因任何原因不可用（它是 pip 的传递依赖，所以这应该
    永不发生），我们回退到仅检查存在性，以便倾向于"不要折腾"。
    """
    pkg = _pkg_name_from_spec(spec)
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:
        return False
    try:
        installed = version(pkg)
    except PackageNotFoundError:
        return False
    except Exception:
        return False

    spec_tail = _specifier_from_spec(spec)
    if not spec_tail:
        # 裸 ``"package"`` —— 无版本约束，存在即足够。
        return True

    try:
        from packaging.specifiers import InvalidSpecifier, SpecifierSet
        from packaging.version import InvalidVersion, Version
    except ImportError:
        # packaging 不可用 —— 回退到"已安装即视为满足"。
        return True

    try:
        return Version(installed) in SpecifierSet(spec_tail)
    except (InvalidSpecifier, InvalidVersion, Exception):
        # 格式错误的 spec 或无法解析的已安装版本 —— 不要折腾。
        return True


def _is_present(spec: str) -> bool:
    """廉价的仅存在性检查（包名已安装，任意版本）。

    供 :func:`active_features` 使用，用于检测用户先前激活过的后端，无论
    版本钉是否变动。
    """
    pkg = _pkg_name_from_spec(spec)
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:
        return False
    try:
        version(pkg)
        return True
    except PackageNotFoundError:
        return False
    except Exception:
        return False


def _core_constraints_file() -> Optional[Path]:
    """写一个 pip 约束文件，把核心环境中每个已可导入的包锁定到其已安装版本。

    作为 ``--constraint`` 传给持久目标安装，使解析器把共享的传递依赖
    （httpx、pydantic、aiohttp……）锁定到核心 venv 已内置的精确版本，
    而不是把更新的副本拉入持久存储。两个收益：

    * 持久存储保持精简——只有真正全新的包才会落在那里；共享依赖对核心
      解析为"已满足"。
    * 一个*要求*与核心冲突版本的后端会在安装时大声失败（解析器冲突），
      而不是静默安装一个在 sys.path 上永远无法获胜的被遮蔽副本。

    返回临时约束文件的路径，如果枚举失败则返回 None（此时调用方在不带
    约束的情况下安装——仍然安全，只是不够整洁）。
    """
    try:
        from importlib.metadata import distributions
    except ImportError:
        return None
    try:
        import tempfile
        lines = []
        seen = set()
        for dist in distributions():
            name = dist.metadata["Name"] if dist.metadata else None
            ver = dist.version
            if not name or not ver:
                continue
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"{name}=={ver}")
        if not lines:
            return None
        fd, path = tempfile.mkstemp(prefix="hermes-core-constraints-", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(sorted(lines)) + "\n")
        return Path(path)
    except Exception as e:
        logger.debug("Could not build core constraints file: %s", e)
        return None


def _venv_pip_install(specs: tuple[str, ...], *, timeout: int = 300) -> _InstallResult:
    """使用 uv → pip → ensurepip 阶梯安装 ``specs``。

    两种模式：

    * **venv 范围（默认）。** 安装到活动 venv（``sys.executable``）。用于
      正常安装。
    * **持久目标。** 当设置了 :data:`_LAZY_TARGET_ENV` 时，通过
      ``--target`` 安装到该目录，并将共享依赖约束到核心 venv 的版本
      （见 :func:`_core_constraints_file`）。该目标在 ``sys.path`` 上只
      追加，因此永远无法遮蔽核心。由不可变 Docker 镜像使用，以把懒安装
      排除在已封闭的 venv 之外。

    镜像 ``hermes_cli.tools_config._pip_install`` 中的策略，但在此保持
    独立，使本模块不依赖 CLI。
    """
    if not specs:
        return _InstallResult(True, "", "")

    target = _lazy_install_target()
    constraints: Optional[Path] = None

    if target is not None:
        err = _ensure_target_ready(target)
        if err:
            return _InstallResult(False, "", err)
        constraints = _core_constraints_file()

    target_args: list[str] = []
    if target is not None:
        # --target 告诉 uv 和 pip 都安装到一个任意目录。
        target_args = ["--target", str(target)]
    constraint_args: list[str] = []
    if constraints is not None:
        constraint_args = ["--constraint", str(constraints)]

    try:
        venv_root = Path(sys.executable).parent.parent
        uv_env = {**os.environ, "VIRTUAL_ENV": str(venv_root)}

        # 第一级：uv（首选 —— 快，不需要 venv 里有 pip）
        uv_bin = shutil.which("uv")
        if uv_bin:
            try:
                r = subprocess.run(
                    [uv_bin, "pip", "install", *target_args, *constraint_args, *specs],
                    capture_output=True, text=True, timeout=timeout, env=uv_env,
                    stdin=subprocess.DEVNULL,
                )
                if r.returncode == 0:
                    if target is not None:
                        _activate_target_on_syspath(target)
                    return _InstallResult(True, r.stdout or "", r.stderr or "")
                logger.debug("uv pip install failed: %s", r.stderr)
            except (subprocess.TimeoutExpired, FileNotFoundError) as e:
                logger.debug("uv invocation failed: %s", e)

        # 第二级：python -m pip（需要时用 ensurepip 引导）
        pip_cmd = [sys.executable, "-m", "pip"]
        try:
            probe = subprocess.run(
                pip_cmd + ["--version"],
                capture_output=True, text=True, timeout=15,
                stdin=subprocess.DEVNULL,
            )
            if probe.returncode != 0:
                raise FileNotFoundError("pip not in venv")
        except (subprocess.TimeoutExpired, FileNotFoundError):
            try:
                subprocess.run(
                    [sys.executable, "-m", "ensurepip", "--upgrade", "--default-pip"],
                    capture_output=True, text=True, timeout=120, check=True,
                    stdin=subprocess.DEVNULL,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                return _InstallResult(False, "",
                                      f"pip not available and ensurepip failed: {e}")

        try:
            r = subprocess.run(
                pip_cmd + ["install", *target_args, *constraint_args, *specs],
                capture_output=True, text=True, timeout=timeout,
                stdin=subprocess.DEVNULL,
            )
            if r.returncode == 0 and target is not None:
                _activate_target_on_syspath(target)
            return _InstallResult(r.returncode == 0, r.stdout or "", r.stderr or "")
        except subprocess.TimeoutExpired as e:
            return _InstallResult(False, "", f"pip install timed out: {e}")
        except Exception as e:
            return _InstallResult(False, "", f"pip install failed: {e}")
    finally:
        if constraints is not None:
            try:
                constraints.unlink()
            except OSError:
                pass


# =============================================================================
# 公开 API
# =============================================================================


def feature_specs(feature: str) -> tuple[str, ...]:
    """返回某个 feature 的已注册 spec，否则抛出 KeyError。"""
    if feature not in LAZY_DEPS:
        raise KeyError(f"Unknown lazy feature: {feature!r}")
    return LAZY_DEPS[feature]


def feature_missing(feature: str) -> tuple[str, ...]:
    """返回 ``feature`` 中当前未安装的 spec 子集。"""
    return tuple(s for s in feature_specs(feature) if not _is_satisfied(s))


def ensure(feature: str, *, prompt: bool = True) -> None:
    """确保 ``feature`` 的所有包都可导入。

    如果缺失，尝试在活动 venv 中安装它们。如果用户已禁用懒安装或安装
    尝试失败，抛出 :class:`FeatureUnavailable`。

    ``prompt``：为 True（默认）且 stdin 是 TTY 时，在安装前询问用户确认。
    非交互式调用方（网关、cron、批处理）使用 prompt=False 并跳过确认——
    此时的闸门是配置标志。
    """
    if feature not in LAZY_DEPS:
        raise FeatureUnavailable(
            feature, (), f"feature {feature!r} not in LAZY_DEPS allowlist"
        )

    missing = feature_missing(feature)
    if not missing:
        return

    # 根据白名单 + 安全正则校验每个 spec。双重保险——上面的
    # keys-in-LAZY_DEPS 检查已经对此做了约束。
    for spec in missing:
        if not _spec_is_safe(spec):
            raise FeatureUnavailable(
                feature, missing,
                f"refusing to install unsafe spec {spec!r}"
            )

    if not _allow_lazy_installs():
        raise FeatureUnavailable(
            feature, missing,
            "lazy installs disabled (security.allow_lazy_installs=false)"
        )

    # 仅当我们拥有一个 TTY 且 prompt_toolkit 未在运行时才显示交互式确认。
    # 当一个 prompt_toolkit 应用拥有终端时，裸 input() 会死锁，因为按键
    # 被路由到它的事件循环而不是 stdin，于是提示会永远阻塞。在 TUI 下
    # 我们跳过提示并继续——懒安装由 security.allow_lazy_installs 闸门
    # 控制，因此到达这里已经是用户 opt-in。
    _pt_active = False
    if "prompt_toolkit.application.current" in sys.modules:
        try:
            from prompt_toolkit.application.current import get_app_or_none
            _app = get_app_or_none()
            _pt_active = _app is not None and getattr(_app, "is_running", False)
        except Exception:
            _pt_active = False

    if prompt and not _pt_active and sys.stdin.isatty() and sys.stdout.isatty():
        spec_list = ", ".join(missing)
        try:
            answer = input(
                f"\nFeature {feature!r} requires: {spec_list}\n"
                f"Install into the active venv now? [Y/n] "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "n"
        if answer and answer not in {"y", "yes"}:
            raise FeatureUnavailable(
                feature, missing, "user declined install at prompt"
            )

    logger.info("Lazy-installing %s for feature %r", " ".join(missing), feature)
    result = _venv_pip_install(missing)
    if not result.success:
        # 暴露实际的 pip 错误，以便用户调试 PyPI 侧问题
        #（404 隔离、网络中断等）。
        snippet = (result.stderr or result.stdout or "").strip()
        if snippet:
            # 裁剪到可读大小——pip 可能倾倒数页的解析跟踪。
            snippet = snippet[-2000:]
        raise FeatureUnavailable(
            feature, missing,
            f"pip install failed: {snippet or 'no error output'}"
        )

    # 安装后校验。importlib.metadata 按进程缓存，因此如果我们刚安装了
    # 某个东西，不刷新的话缓存可能看不到它。
    try:
        import importlib.metadata as _md
        if hasattr(_md, "_cache_clear"):
            _md._cache_clear()  # type: ignore[attr-defined]
    except Exception:
        pass

    still_missing = feature_missing(feature)
    if still_missing:
        raise FeatureUnavailable(
            feature, still_missing,
            "install reported success but packages still not importable "
            "(may require Python restart)"
        )

    logger.info("Lazy install complete for feature %r", feature)


def is_available(feature: str) -> bool:
    """当该 feature 的依赖已满足时返回 True。"""
    if feature not in LAZY_DEPS:
        return False
    return not feature_missing(feature)


def feature_install_command(feature: str) -> Optional[str]:
    """返回用户可手动运行的 ``pip install`` 命令，或 None。"""
    if feature not in LAZY_DEPS:
        return None
    specs = LAZY_DEPS[feature]
    return "uv pip install " + " ".join(repr(s) for s in specs)


def active_features() -> list[str]:
    """返回用户曾经懒安装过的 feature 列表。

    如果某个 feature 声明的包中至少有一个当前安装在 venv 中（仅检查存在性，
    忽略版本），该 feature 就算"活跃"。用户从未启用的 feature 保持沉默。

    供 ``hermes update`` 使用，用于判断当 :data:`LAZY_DEPS` 中的版本钉变动
    时哪些懒后端需要刷新。
    """
    active = []
    for feature, specs in LAZY_DEPS.items():
        if any(_is_present(s) for s in specs):
            active.append(feature)
    return active


def refresh_active_features(*, prompt: bool = False) -> dict[str, str]:
    """为用户先前激活过的每个 feature 重新运行 ``ensure``。

    返回一个 ``{feature: status}`` 映射，其中 status 是以下之一：
        ``"current"``  —— 版本钉已满足，未运行安装
        ``"refreshed"`` —— 版本钉过期，重装成功
        ``"failed: <reason>"`` —— 安装尝试失败；由调用方决定是否暴露
                                  （我们不抛异常）
        ``"skipped: <reason>"`` —— 被闸门挡住（配置标志、用户拒绝）

    供 ``hermes update`` 使用。永不抛异常；此处的懒安装失败不得阻塞更新
    流程的其余部分。
    """
    results: dict[str, str] = {}
    for feature in active_features():
        missing = feature_missing(feature)
        if not missing:
            results[feature] = "current"
            continue
        try:
            ensure(feature, prompt=prompt)
            results[feature] = "refreshed"
        except FeatureUnavailable as e:
            # 区分"用户选择退出"和"安装失败"，以便 update 命令渲染正确的
            # 消息。
            if "lazy installs disabled" in str(e) or "declined" in str(e):
                results[feature] = f"skipped: {e.reason}"
            else:
                results[feature] = f"failed: {e.reason}"
        except Exception as e:
            results[feature] = f"failed: {e}"
    return results


def ensure_and_bind(
    feature: str,
    importer: Callable[[], dict[str, Any]],
    target_globals: dict,
    *,
    prompt: bool = False,
) -> bool:
    """确保某个 feature 已安装，然后把名称重新绑定到调用方的 globals 中。

    把 :func:`ensure` 与一个安装后导入步骤组合在一起，后者重新绑定模块级
    的名称。这消除了手动列出懒安装后需要更新的每个全局变量的易错模式。

    ``importer`` 是一个零参数可调用对象，返回调用方需要重新绑定的所有符号
    的 ``{name: value}`` 字典。它仅在 :func:`ensure` 成功后（或包已安装时）
    被调用。

    成功返回 True，依赖无法安装或导入时返回 False。

    在平台适配器中的示例用法::

        def check_slack_requirements() -> bool:
            if SLACK_AVAILABLE:
                return True
            def _import():
                from slack_bolt.async_app import AsyncApp
                from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
                from slack_sdk.web.async_client import AsyncWebClient
                import aiohttp
                return {
                    "AsyncApp": AsyncApp,
                    "AsyncSocketModeHandler": AsyncSocketModeHandler,
                    "AsyncWebClient": AsyncWebClient,
                    "aiohttp": aiohttp,
                    "SLACK_AVAILABLE": True,
                }
            return ensure_and_bind("platform.slack", _import, globals(), prompt=False)
    """
    try:
        ensure(feature, prompt=prompt)
    except (FeatureUnavailable, Exception):
        return False

    try:
        bindings = importer()
    except ImportError:
        return False

    target_globals.update(bindings)
    return True
