"""
cron 任务输出和 agent 响应的投递路由。

根据以下规则把消息路由到合适的目的地：
- 显式目标（例如 "telegram:123456789"）
- 平台 home channel（例如 "telegram" → home channel）
- 来源（回到任务创建的位置）
- 本地（总是保存到文件）
"""

import logging
import os
import re
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass
from typing import Dict, List, Optional, Any

from hermes_cli.config import get_hermes_home

logger = logging.getLogger(__name__)

# 在 gateway 级别对 cron 输出做截断之前，为不支持分块的平台投递设置的上限。
# Telegram 的硬性 API 上限是 4096；这里的余量用于覆盖截断时追加的
# “full output saved to …” 页脚。原生支持长消息拆分的 adapter
# （BasePlatformAdapter.splits_long_messages）会完全绕过这一步——adapter
# 会在它自己的 send() 中分块，完整输出得以保留。
MAX_PLATFORM_OUTPUT = 4000

# 匹配那些 *仅仅* 是一个“沉默”叙述（可带可选的 markdown 包裹）的字符串。
# 覆盖：*(silent)*、_silent_、`silent`、~silent~、(silent)、silent、
# 🔇、单独的 "."、"…"，以及实际环境中出现的带空白/标记填充的变体。
# 锚定到首尾，所以那些只是 *包含* "silent" 这个词的实质性消息永远不会被匹配。
_SILENCE_NARRATION = re.compile(
    r'^[\s*_~`]*\(?\s*(silent|silence|no\s+response|no\s+reply)\s*\.?\)?[\s*_~`]*$'
    r'|^[\s*_~`]*[\U0001F507\.\u2026]+[\s*_~`]*$',
    re.IGNORECASE,
)


def _is_silence_narration(content: Optional[str]) -> bool:
    """当 ``content`` *仅仅* 是一个沉默叙述 token 时返回 True。

    带长度保护（真实消息会更长）并锚定到整个字符串，所以像
    "The deployment ran silently" 或 "Silence is golden — here is the plan..."
    这样的合法正文永远不会被误判。
    """
    if not content:
        return False
    stripped = content.strip()
    if not stripped or len(stripped) > 64:  # 长度保护
        return False
    return bool(_SILENCE_NARRATION.match(stripped))

from .config import Platform, GatewayConfig
from .session import SessionSource


def _looks_like_telegram_private_chat_id(chat_id: Optional[str]) -> bool:
    if chat_id is None:
        return False
    try:
        return int(chat_id) > 0
    except (TypeError, ValueError):
        return False


def _looks_like_int(value: Optional[str]) -> bool:
    if value is None:
        return False
    try:
        int(value)
        return True
    except (TypeError, ValueError):
        return False


def _send_result_failed(result: Any) -> bool:
    if isinstance(result, dict):
        return result.get("success") is False
    return getattr(result, "success", True) is False


def _send_result_error(result: Any) -> Optional[str]:
    if isinstance(result, dict):
        error = result.get("error")
    else:
        error = getattr(result, "error", None)
    return str(error) if error else None


def _is_thread_not_found_delivery_error(result: Any) -> bool:
    error = _send_result_error(result)
    return bool(error and "thread not found" in error.lower())


@dataclass
class DeliveryTarget:
    """
    单个投递目标。

    表示消息应当发送到哪里：
    - "origin" → 回到来源
    - "local" → 保存到本地文件
    - "telegram" → Telegram home channel
    - "telegram:123456" → 特定的 Telegram 聊天
    """
    platform: Platform
    chat_id: Optional[str] = None  # None 表示使用 home channel
    thread_id: Optional[str] = None
    is_origin: bool = False
    is_explicit: bool = False  # 当 chat_id 被显式指定时为 True
    
    @classmethod
    def parse(cls, target: str, origin: Optional[SessionSource] = None) -> "DeliveryTarget":
        """
        解析一个投递目标字符串。

        格式：
        - "origin" → 回到来源
        - "local" → 仅本地文件
        - "telegram" → Telegram home channel
        - "telegram:123456" → 特定的 Telegram 聊天
        """
        target_stripped = target.strip()
        target_lower = target_stripped.lower()
        
        if target_lower == "origin":
            if origin:
                return cls(
                    platform=origin.platform,
                    chat_id=origin.chat_id,
                    thread_id=origin.thread_id,
                    is_origin=True,
                )
            else:
                # 没有来源时回退到本地
                return cls(platform=Platform.LOCAL, is_origin=True)
        
        if target_lower == "local":
            return cls(platform=Platform.LOCAL)
        
        # 检查 platform:chat_id 或 platform:chat_id:thread_id 格式
        # 对 chat_id/thread_id 使用原始大小写，以保留大小写敏感的 ID
        if ":" in target_stripped:
            parts = target_stripped.split(":", 2)
            platform_str = parts[0].lower()  # 平台名不区分大小写
            chat_id = parts[1] if len(parts) > 1 else None
            thread_id = parts[2] if len(parts) > 2 else None
            try:
                platform = Platform(platform_str)
                return cls(platform=platform, chat_id=chat_id, thread_id=thread_id, is_explicit=True)
            except ValueError:
                # 未知平台，按本地处理
                return cls(platform=Platform.LOCAL)

        # 只是一个平台名（使用 home channel）
        try:
            platform = Platform(target_lower)
            return cls(platform=platform)
        except ValueError:
            # 未知平台，按本地处理
            return cls(platform=Platform.LOCAL)
    
    def to_string(self) -> str:
        """转换回字符串格式。"""
        if self.is_origin:
            return "origin"
        if self.platform == Platform.LOCAL:
            return "local"
        if self.chat_id and self.thread_id:
            return f"{self.platform.value}:{self.chat_id}:{self.thread_id}"
        if self.chat_id:
            return f"{self.platform.value}:{self.chat_id}"
        return self.platform.value


class DeliveryRouter:
    """
    把消息路由到合适的目的地。

    处理解析投递目标并把消息分发到正确平台 adapter 的逻辑。
    """
    
    def __init__(self, config: GatewayConfig, adapters: Dict[Platform, Any] = None):
        """
        初始化投递路由器。

        参数（Args）：
            config: Gateway 配置
            adapters: 把平台映射到其 adapter 实例的 dict
        """
        self.config = config
        self.adapters = adapters or {}
        self.output_dir = get_hermes_home() / "cron" / "output"
    
    async def deliver(
        self,
        content: str,
        targets: List[DeliveryTarget],
        job_id: Optional[str] = None,
        job_name: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        把内容投递到所有指定目标。

        参数（Args）：
            content: 要投递的消息/输出
            targets: 投递目标列表
            job_id: 可选的 job ID（用于 cron 任务）
            job_name: 可选的 job 名称
            metadata: 要附带的额外元数据

        返回（Returns）：
            包含每个目标投递结果的 dict
        """
        results = {}
        
        for target in targets:
            try:
                if target.platform == Platform.LOCAL:
                    result = self._deliver_local(content, job_id, job_name, metadata)
                else:
                    result = await self._deliver_to_platform(target, content, metadata)
                
                results[target.to_string()] = {
                    "success": True,
                    "result": result
                }
            except Exception as e:
                results[target.to_string()] = {
                    "success": False,
                    "error": str(e)
                }
        
        return results
    
    def _deliver_local(
        self,
        content: str,
        job_id: Optional[str],
        job_name: Optional[str],
        metadata: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """把内容保存到本地文件。"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        if job_id:
            output_path = self.output_dir / job_id / f"{timestamp}.md"
        else:
            output_path = self.output_dir / "misc" / f"{timestamp}.md"
        
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        # 构建输出文档
        lines = []
        if job_name:
            lines.append(f"# {job_name}")
        else:
            lines.append("# Delivery Output")
        
        lines.append("")
        lines.append(f"**Timestamp:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        
        if job_id:
            lines.append(f"**Job ID:** {job_id}")
        
        if metadata:
            for key, value in metadata.items():
                lines.append(f"**{key}:** {value}")
        
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append(content)
        
        output_path.write_text("\n".join(lines))
        
        return {
            "path": str(output_path),
            "timestamp": timestamp
        }
    
    def _save_full_output(self, content: str, job_id: str) -> Path:
        """把完整的 cron 输出保存到磁盘并返回文件路径。"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = get_hermes_home() / "cron" / "output"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{job_id}_{timestamp}.txt"
        path.write_text(content)
        return path

    def _filter_silence_narration_enabled(self) -> bool:
        """出站沉默叙述过滤器是否处于激活状态。

        ``HERMES_FILTER_SILENCE_NARRATION`` 环境变量在设置时会覆盖配置；
        否则以 ``gateway.filter_silence_narration`` 配置 flag 为准
        （默认 True）。
        """
        env = os.getenv("HERMES_FILTER_SILENCE_NARRATION")
        if env is not None:
            return env.strip().lower() in ("1", "true", "yes", "on")
        return bool(getattr(self.config, "filter_silence_narration", True))

    async def _deliver_to_platform(
        self,
        target: DeliveryTarget,
        content: str,
        metadata: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """把内容投递到一个消息平台。"""
        adapter = self.adapters.get(target.platform)
        
        if not adapter:
            raise ValueError(f"No adapter configured for {target.platform.value}")
        
        if not target.chat_id:
            raise ValueError(f"No chat ID for {target.platform.value} delivery")
        
        # 守卫：处理过大的 cron 输出。
        #
        # 两个相互独立的决策：
        #   1. 审计保存（AUDIT SAVE）—— 当内容超过 MAX_PLATFORM_OUTPUT 时，
        #      完整输出总是会被写到磁盘，作为可恢复的审计留痕。无论 adapter
        #      能力如何，这一步都会触发（best-effort）。
        #   2. 截断（TRUNCATION）—— 对于不支持分块的 adapter，超过上限的内容
        #      会被截断，并附带一个指向已保存文件的页脚。支持分块的 adapter
        #      （splits_long_messages=True）会收到完整 payload，并在它自己的
        #      send() 中原生地拆分。
        job_id = (metadata or {}).get("job_id", "unknown")
        saved_path: Optional[Path] = None

        if len(content) > MAX_PLATFORM_OUTPUT:
            # 第 1 步 —— 审计保存（best-effort）。该保存是一个作为副作用的审计
            # 留痕，对投递本身并非必需。如果它失败（磁盘满、权限问题），
            # 投递仍会继续——内容无论如何都会到达 adapter。
            try:
                saved_path = self._save_full_output(content, job_id)
            except OSError as exc:
                logger.warning(
                    "Audit save failed for cron output (%d chars, job=%s): %s — "
                    "delivery proceeds without audit copy",
                    len(content), job_id, exc,
                )

            # 第 2 步 —— 截断（仅针对不支持分块的 adapter）。
            if getattr(adapter, "splits_long_messages", False):
                # adapter 原生分块 —— 投递完整 payload。
                if saved_path:
                    logger.info(
                        "Cron output preserved for chunking adapter (%d chars) — "
                        "full output saved to %s",
                        len(content), saved_path,
                    )
            else:
                # 不支持分块的 adapter —— 截断并附上页脚。页脚需要一个有效的
                # 路径，因此如果上面 best-effort 的保存失败了，就在这里重试
                # （此时失败才是真正的投递问题）。
                if saved_path is None:
                    saved_path = self._save_full_output(content, job_id)
                footer = f"\n\n... [truncated, full output saved to {saved_path}]"
                visible = max(0, MAX_PLATFORM_OUTPUT - len(footer))
                logger.info(
                    "Cron output truncated (%d chars) — full output: %s",
                    len(content), saved_path,
                )
                content = content[:visible] + footer
        
        # 基底层的防回环守卫：在幻觉出的“沉默叙述”（*(silent)*、🔇、单独的
        # "." 等）到达 adapter 之前就把它丢弃。在 bot 与 bot 之间的通道里，
        # 这些 token 会来回镜像，直到某个 model 以 "no content after all
        # retries" 崩溃。行为性 prompt 规则在不同 provider 之间会漂移；这个
        # 单一的咽喉点覆盖了所有平台 adapter，无论哪个 persona 的 prompt
        # 失效。本地/文件投递（_deliver_local）是一条独立路径，从不过滤——
        # 被保存的沉默不存在回环风险。
        if self._filter_silence_narration_enabled() and _is_silence_narration(content):
            logger.warning(
                "Dropped silence-narration outbound to %s (chat=%s): %r",
                target.platform.value,
                target.chat_id,
                content[:40],
            )
            return {
                "success": True,
                "filtered": "silence_narration",
                "delivered": False,
            }

        send_metadata = dict(metadata or {})
        is_named_telegram_private_topic = False
        named_telegram_private_topic_name: Optional[str] = None
        if target.thread_id:
            has_explicit_direct_topic = (
                "direct_messages_topic_id" in send_metadata
                or "telegram_direct_messages_topic_id" in send_metadata
            )
            target_thread_id = target.thread_id
            is_named_telegram_private_topic = (
                target.platform == Platform.TELEGRAM
                and _looks_like_telegram_private_chat_id(target.chat_id)
                and not _looks_like_int(target_thread_id)
                and "thread_id" not in send_metadata
                and "message_thread_id" not in send_metadata
                and not has_explicit_direct_topic
            )
            if is_named_telegram_private_topic:
                named_telegram_private_topic_name = target_thread_id
                ensure_dm_topic = getattr(adapter, "ensure_dm_topic", None)
                if ensure_dm_topic is None:
                    raise RuntimeError(
                        "Telegram adapter cannot create named private DM topics"
                    )
                created_thread_id = await ensure_dm_topic(target.chat_id, target_thread_id)
                if not created_thread_id:
                    raise RuntimeError(
                        f"Failed to create Telegram private DM topic '{target_thread_id}'"
                    )
                target_thread_id = str(created_thread_id)
                send_metadata["thread_id"] = target_thread_id
                send_metadata["telegram_dm_topic_created_for_send"] = True
            elif (
                target.platform == Platform.TELEGRAM
                and _looks_like_telegram_private_chat_id(target.chat_id)
                and "thread_id" not in send_metadata
                and "message_thread_id" not in send_metadata
                and not has_explicit_direct_topic
            ):
                # 不是由本发送路径创建的遗留私有 topic/thread id，可能仍然
                # 需要一个 reply anchor 才能在所请求的通道里保持可见。命名目标
                # 通过上面的 createForumTopic 创建，可以直接使用
                # message_thread_id。
                reply_anchor = send_metadata.get("telegram_reply_to_message_id")
                if reply_anchor is None:
                    raise RuntimeError(
                        "Telegram private DM topic delivery requires telegram_reply_to_message_id; "
                        "send to the bare chat or provide a reply anchor"
                    )
                send_metadata["thread_id"] = target_thread_id
                send_metadata["telegram_dm_topic_reply_fallback"] = True
            elif "thread_id" not in send_metadata and "message_thread_id" not in send_metadata and not has_explicit_direct_topic:
                send_metadata["thread_id"] = target_thread_id
        result = await adapter.send(target.chat_id, content, metadata=send_metadata or None)
        if _send_result_failed(result):
            if (
                is_named_telegram_private_topic
                and named_telegram_private_topic_name
                and _is_thread_not_found_delivery_error(result)
            ):
                ensure_dm_topic = getattr(adapter, "ensure_dm_topic", None)
                if ensure_dm_topic is None:
                    raise RuntimeError(
                        "Telegram adapter cannot refresh named private DM topics"
                    )
                refreshed_thread_id = await ensure_dm_topic(
                    target.chat_id,
                    named_telegram_private_topic_name,
                    force_create=True,
                )
                if not refreshed_thread_id:
                    raise RuntimeError(
                        f"Failed to refresh Telegram private DM topic '{named_telegram_private_topic_name}'"
                    )
                send_metadata["thread_id"] = str(refreshed_thread_id)
                send_metadata["telegram_dm_topic_created_for_send"] = True
                result = await adapter.send(target.chat_id, content, metadata=send_metadata or None)
            if _send_result_failed(result):
                raise RuntimeError(_send_result_error(result) or f"{target.platform.value} delivery failed")
        return result




