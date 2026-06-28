"""``hermes portal`` — Nous Portal 的人类可读入口。

不带子命令运行 ``hermes portal`` 会执行一次性 Portal
引导流程：OAuth 登录、选择 Nous 模型、将推理提供商切换为
Nous，并提供启用 Tool Gateway 的选项。它是
``hermes auth add nous --type oauth``（仍然可用）的友好别名，等同于
``hermes setup --portal``，并运行与首次快速设置相同的 Nous 流程。

子命令：
  (none)   登录 Nous Portal 并完成设置（一次性引导）。
  login    默认一次性引导的显式别名。
  info     显示 Portal 认证状态以及哪些 Tool Gateway 工具已被路由。
  open     在用户的默认浏览器中打开 Portal 订阅页面。
  tools    列出 Tool Gateway 工具及其在当前配置中的激活状态。

此命令刻意保持精简 — 不会重复 ``hermes auth`` 或 ``hermes tools``
中已有的功能。它是 Portal 订阅本身的引导与发现入口。
"""
from __future__ import annotations

import sys
import webbrowser

from hermes_cli.colors import Colors, color
from hermes_cli.config import load_config

DEFAULT_PORTAL_URL = "https://portal.nousresearch.com"
SUBSCRIPTION_URL = "https://portal.nousresearch.com/manage-subscription"
DOCS_URL = "https://hermes-agent.nousresearch.com/docs/user-guide/features/tool-gateway"


def _cmd_status(args) -> int:
    """显示 Portal 认证 + Tool Gateway 路由摘要。"""
    from hermes_cli.auth import get_nous_auth_status
    from hermes_cli.nous_subscription import get_nous_subscription_features

    config = load_config() or {}

    try:
        auth = get_nous_auth_status() or {}
    except Exception:
        auth = {}

    logged_in = bool(auth.get("logged_in"))

    print()
    print(color("  Nous Portal", Colors.MAGENTA))
    print(color("  ───────────", Colors.MAGENTA))
    if logged_in:
        portal = auth.get("portal_base_url") or DEFAULT_PORTAL_URL
        print(f"  Auth:    {color('✓ logged in', Colors.GREEN)}")
        print(f"  Portal:  {portal}")
        inference = auth.get("inference_base_url")
        if inference:
            print(f"  API:     {inference}")
    else:
        print(f"  Auth:    {color('not logged in', Colors.YELLOW)}")
        print(f"  Sign up: {SUBSCRIPTION_URL}")
        print(f"  Login:   hermes portal")

    # Provider 选择（独立于认证）
    model_cfg = config.get("model") if isinstance(config.get("model"), dict) else {}
    provider = str(model_cfg.get("provider") or "").strip().lower()
    if provider == "nous":
        print(f"  Model:   {color('✓ using Nous as inference provider', Colors.GREEN)}")
    elif provider:
        print(f"  Model:   currently {provider} (switch with `hermes model`)")

    # Tool Gateway 路由
    print()
    print(color("  Tool Gateway", Colors.MAGENTA))
    print(color("  ────────────", Colors.MAGENTA))
    try:
        features = get_nous_subscription_features(config)
    except Exception:
        features = None

    if features is None:
        print("  (could not resolve subscription state)")
        return 0

    rows = []
    for feat in features.items():
        if feat.managed_by_nous:
            state = color("via Nous Portal", Colors.GREEN)
        elif feat.active and feat.current_provider:
            state = feat.current_provider
        elif feat.active:
            state = "active"
        else:
            state = color("not configured", Colors.DIM)
        rows.append((feat.label, state))

    width = max((len(r[0]) for r in rows), default=0)
    for label, state in rows:
        print(f"  {label:<{width}}   {state}")

    if not logged_in:
        print()
        print(color(f"  Docs: {DOCS_URL}", Colors.DIM))
    return 0


def _cmd_open(args) -> int:
    """在默认浏览器中打开 Portal 订阅页面。"""
    target = SUBSCRIPTION_URL
    print(f"Opening {target}")
    try:
        opened = webbrowser.open(target)
    except Exception:
        opened = False
    if not opened:
        print()
        print("Could not launch a browser. Visit the URL above manually.")
        return 1
    return 0


def _cmd_tools(args) -> int:
    """列出 Tool Gateway 目录 + 当前路由。"""
    from hermes_cli.nous_subscription import get_nous_subscription_features

    config = load_config() or {}
    try:
        features = get_nous_subscription_features(config)
    except Exception:
        print("Could not resolve Tool Gateway state.", file=sys.stderr)
        return 1

    # 静态目录 — Tool Gateway 当前路由到的合作伙伴。
    catalog = [
        ("web",       "Web search & extract",  "Firecrawl"),
        ("image_gen", "Image generation",      "FAL"),
        ("tts",       "Text-to-speech",        "OpenAI TTS"),
        ("browser",   "Browser automation",    "Browser Use"),
        ("modal",     "Cloud terminal",        "Modal"),
    ]

    print()
    print(color("  Tool Gateway catalog", Colors.MAGENTA))
    print(color("  ────────────────────", Colors.MAGENTA))

    if not features.nous_auth_present:
        print(color("  Not logged into Nous Portal — sign in with `hermes portal`.", Colors.YELLOW))
        print()

    label_width = max(len(label) for _, label, _ in catalog)
    for key, label, partner in catalog:
        feat = features.features.get(key)
        if feat is None:
            state = color("unknown", Colors.DIM)
        elif feat.managed_by_nous:
            state = color("✓ via Nous Portal", Colors.GREEN)
        elif feat.active and feat.current_provider:
            state = feat.current_provider
        elif feat.active:
            state = "active"
        else:
            state = color("not configured", Colors.DIM)
        print(f"  {label:<{label_width}}  partner: {partner:<14} {state}")

    print()
    print(color(f"  Manage your subscription: {SUBSCRIPTION_URL}", Colors.DIM))
    print(color(f"  Docs: {DOCS_URL}", Colors.DIM))
    return 0


def _cmd_login(args) -> int:
    """运行一次性 Nous Portal 引导流程（登录 + 模型 + 提供商 + 工具）。

    这是 `hermes auth add nous --type oauth` 的人类可读入口。
    它复用了 `hermes setup --portal` 背后的完全相同的流程（后者
    又运行与首次快速设置相同的 Nous 流程），因此各命令保持同步：
    设备码登录、选择 Nous 模型、将推理提供商切换为 Nous，
    然后提供 Tool Gateway 的可选启用。
    """
    from hermes_cli.setup import _run_portal_one_shot

    config = load_config() or {}
    try:
        _run_portal_one_shot(config)
    except (KeyboardInterrupt, EOFError):
        print()
        print("Portal setup cancelled.")
        return 1
    return 0


def portal_command(args) -> int:
    """`hermes portal <subcommand>` 的顶层分发。"""
    sub = getattr(args, "portal_command", None)
    if sub in {None, "", "login"}:
        # 默认执行一次性引导 — `hermes portal` 是
        # `hermes auth add nous --type oauth` / `hermes setup --portal`
        # 的人类可读别名。
        return _cmd_login(args)
    if sub in {"info", "status"}:
        # `status` 作为先前默认行为的向后兼容别名保留。
        return _cmd_status(args)
    if sub == "open":
        return _cmd_open(args)
    if sub == "tools":
        return _cmd_tools(args)
    print(f"Unknown portal subcommand: {sub}", file=sys.stderr)
    print("Run `hermes portal -h` for usage.", file=sys.stderr)
    return 1


def add_parser(subparsers) -> None:
    """在指定的 argparse subparsers 对象上注册 `hermes portal`。"""
    portal_parser = subparsers.add_parser(
        "portal",
        help="Set up Nous Portal (login, model pick, Tool Gateway); see also `portal info`",
        description=(
            "Run `hermes portal` with no subcommand to log in to Nous Portal "
            "and set it up — pick a model, set Nous as your provider, and offer "
            "the Tool Gateway (the human-readable alias for `hermes auth add "
            "nous --type oauth`, identical to `hermes setup --portal`). "
            "Subcommands: login (default), info, open, tools."
        ),
    )
    portal_sub = portal_parser.add_subparsers(dest="portal_command")

    portal_sub.add_parser(
        "login",
        help="Log in to Nous Portal + set it up (default; one-shot onboarding)",
    )
    portal_sub.add_parser(
        "info",
        help="Show Portal auth + Tool Gateway routing summary",
    )
    # `status` 作为 `info` 的隐藏向后兼容别名保留。
    portal_sub.add_parser("status")
    portal_sub.add_parser(
        "open",
        help="Open the Portal subscription page in your default browser",
    )
    portal_sub.add_parser(
        "tools",
        help="List Tool Gateway tools and which are routed via Nous",
    )

    portal_parser.set_defaults(func=portal_command)
