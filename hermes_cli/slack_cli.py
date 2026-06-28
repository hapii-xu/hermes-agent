"""``hermes slack ...`` CLI 子命令。

目前仅实现了 ``hermes slack manifest`` —— 它生成 Slack 应用 manifest JSON，
用于将每个 gateway 命令注册为原生 Slack 斜杠命令（``/btw``、``/stop``、
``/model`` 等），使用户获得与 Discord 和 Telegram 相同的一等斜杠命令体验。

典型工作流程::

    $ hermes slack manifest > slack-manifest.json
    # 或者：
    $ hermes slack manifest --write

然后将打印的 JSON 粘贴到 Slack 应用配置中（Features → App
Manifest → Edit），点击 Save。Slack 会对比 manifest 差异，并在
scope/命令变更时提示重新安装。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _build_full_manifest(
    bot_name: str,
    bot_description: str,
    include_assistant: bool = True,
) -> dict:
    """构建完整的 Slack manifest，合并显示信息 + 斜杠命令列表。

    斜杠命令列表始终从 ``COMMAND_REGISTRY`` 生成，以便与 Hermes 的其余部分
    保持同步。其他 manifest 部分（显示信息、OAuth scope、socket mode）设置为
    Hermes 部署的合理默认值 —— 用户可以在粘贴后在 Slack UI 中调整它们。

    当 ``include_assistant`` 为 True（默认值）时，manifest 将应用加入 Slack
    的 AI Assistant 容器：``assistant_view`` 功能、``assistant:write`` scope
    以及 ``assistant_thread_*`` 事件。Slack 随后将 DM 渲染为右侧的 Assistant
    分栏，每次交互都是一个线程，裸斜杠命令不会作为普通 ``command`` 事件传递。
    传入 ``include_assistant=False``（``--no-assistant``）以省略这三部分，
    获得扁平的 DM 界面，其中 ``/help``、``/new`` 等可以内联工作。
    """
    from hermes_cli.commands import slack_app_manifest

    partial = slack_app_manifest()
    slashes = partial["features"]["slash_commands"]

    features = {
        "app_home": {
            "home_tab_enabled": False,
            "messages_tab_enabled": True,
            "messages_tab_read_only_enabled": False,
        },
        "bot_user": {
            "display_name": bot_name[:80],
            "always_online": True,
        },
        "slash_commands": slashes,
    }

    bot_scopes = [
        "app_mentions:read",
        "channels:history",
        "channels:read",
        "chat:write",
        "commands",
        "files:read",
        "files:write",
        "groups:history",
        "groups:read",
        "im:history",
        "im:read",
        "im:write",
        "users:read",
    ]

    bot_events = [
        "app_mention",
        "message.channels",
        "message.groups",
        "message.im",
    ]

    if include_assistant:
        features["assistant_view"] = {
            "assistant_description": "Chat with Hermes in threads and DMs.",
        }
        bot_scopes.append("assistant:write")
        bot_events.extend(
            [
                "assistant_thread_context_changed",
                "assistant_thread_started",
            ]
        )
        bot_scopes.sort()
        bot_events.sort()

    return {
        "_metadata": {
            "major_version": 1,
            "minor_version": 1,
        },
        "display_information": {
            "name": bot_name[:35],
            "description": (bot_description or "Your Hermes agent on Slack")[:140],
            "background_color": "#1a1a2e",
        },
        "features": features,
        "oauth_config": {
            "scopes": {
                "bot": bot_scopes,
            },
        },
        "settings": {
            "event_subscriptions": {
                "bot_events": bot_events,
            },
            "interactivity": {
                "is_enabled": True,
            },
            "org_deploy_enabled": False,
            "socket_mode_enabled": True,
            "token_rotation_enabled": False,
        },
    }


def slack_manifest_command(args) -> int:
    """打印或写入 Slack 应用 manifest JSON。

    参数（均在 ``hermes_cli/main.py`` 中解析）：
      --write [PATH]  写入文件而非 stdout（默认路径：
                      ``$HERMES_HOME/slack-manifest.json``）
      --name NAME     覆盖 bot 显示名称（默认："Hermes"）
      --description DESC  覆盖 bot 描述
      --slashes-only  仅输出 ``features.slash_commands`` 数组（用于
                      手动合并到现有 manifest 中）
      --no-assistant  省略 Slack AI Assistant 模式（assistant_view 功能、
                      assistant:write scope、assistant_thread_* 事件），
                      使 DM 渲染为扁平聊天界面，裸斜杠命令可以内联工作，
                      而不是 Assistant 线程面板。
    """
    name = getattr(args, "name", None) or "Hermes"
    description = getattr(args, "description", None) or "Your Hermes agent on Slack"
    include_assistant = not getattr(args, "no_assistant", False)

    if getattr(args, "slashes_only", False):
        from hermes_cli.commands import slack_app_manifest

        manifest = slack_app_manifest()["features"]["slash_commands"]
    else:
        manifest = _build_full_manifest(name, description, include_assistant=include_assistant)

    payload = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"

    write_target = getattr(args, "write", None)
    if write_target is not None:
        if isinstance(write_target, bool) and write_target:
            # --write 无值 → 默认位置
            try:
                from hermes_constants import get_hermes_home

                target = Path(get_hermes_home()) / "slack-manifest.json"
            except Exception:
                target = Path(os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")) / "slack-manifest.json"
        else:
            target = Path(write_target).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload, encoding="utf-8")
        print(f"Slack manifest written to: {target}", file=sys.stderr)
        print(
            "\nNext steps:\n"
            "  1. Open https://api.slack.com/apps and pick your Hermes app\n"
            "     (or create a new one: Create New App → From an app manifest).\n"
            f"  2. Features → App Manifest → paste the contents of\n"
            f"     {target}\n"
            "  3. Save; Slack will prompt to reinstall the app if scopes or\n"
            "     slash commands changed.\n"
            "  4. Make sure Socket Mode is enabled and you have a bot token\n"
            "     (xoxb-...) and app token (xapp-...) configured via\n"
            "     `hermes setup`.\n",
            file=sys.stderr,
        )
    else:
        sys.stdout.write(payload)
    return 0
