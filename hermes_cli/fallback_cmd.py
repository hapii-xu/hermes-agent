"""
hermes fallback — 管理 fallback provider 链。

当主模型遇到速率限制、过载或连接错误时，会按顺序尝试 fallback provider。
参见: https://hermes-agent.nousresearch.com/docs/user-guide/features/fallback-providers

子命令:
  hermes fallback [list]   显示当前 fallback 链（无子命令时的默认操作）
  hermes fallback add      通过与 `hermes model` 相同的选择器选取 provider + model，
                           然后将选择追加到链中
  hermes fallback remove   从链中选取一个条目进行删除
  hermes fallback clear    移除所有 fallback 条目

存储: ``~/.hermes/config.yaml`` 中的 ``fallback_providers``（顶层，由
``{provider, model, base_url?, api_mode?}`` 字典组成的列表）。旧版单字典
``fallback_model`` 格式在首次 add 时会迁移为新的列表格式。
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional

from hermes_cli.fallback_config import get_fallback_chain


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _read_chain(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """返回规范化的 fallback 链（字典列表形式）。

    同时支持新的列表格式（``fallback_providers``）和旧版
    ``fallback_model`` 格式。当两者同时存在时，有效链会合并，
    ``fallback_providers`` 的条目保持在前面。返回的列表始终是
    全新副本——调用方可以自由修改而不会影响原始 config 字典。
    """
    return get_fallback_chain(config)


def _write_chain(config: Dict[str, Any], chain: List[Dict[str, Any]]) -> None:
    """将链持久化到 ``fallback_providers`` 并清除旧版键。"""
    config["fallback_providers"] = chain
    # 写入时移除旧版单字典键，确保只有一个数据源。
    if "fallback_model" in config:
        config.pop("fallback_model", None)


def _format_entry(entry: Dict[str, Any]) -> str:
    """将 fallback 条目渲染为单行可读文本。"""
    provider = entry.get("provider", "?")
    model = entry.get("model", "?")
    base = entry.get("base_url")
    suffix = f"  [{base}]" if base else ""
    return f"{model}  (via {provider}){suffix}"


def _extract_fallback_from_model_cfg(model_cfg: Any) -> Optional[Dict[str, Any]]:
    """从 ``config["model"]`` 快照中提取 ``{provider, model, base_url?, api_mode?}`` 字典。"""
    if not isinstance(model_cfg, dict):
        return None
    provider = (model_cfg.get("provider") or "").strip()
    # 选择器会将选中的模型写入 ``model.default``。
    model = (model_cfg.get("default") or model_cfg.get("model") or "").strip()
    if not provider or not model:
        return None
    entry: Dict[str, Any] = {"provider": provider, "model": model}
    base_url = (model_cfg.get("base_url") or "").strip()
    if base_url:
        entry["base_url"] = base_url
    api_mode = (model_cfg.get("api_mode") or "").strip()
    if api_mode:
        entry["api_mode"] = api_mode
    return entry


def _snapshot_auth_active_provider() -> Any:
    """返回 auth.json 中当前的 ``active_provider``，不可用时返回哨兵值。"""
    try:
        from hermes_cli.auth import _load_auth_store
        store = _load_auth_store()
        return store.get("active_provider")
    except Exception:
        return None


def _restore_auth_active_provider(value: Any) -> None:
    """回写之前快照的 ``active_provider`` 值。"""
    try:
        from hermes_cli.auth import _auth_store_lock, _load_auth_store, _save_auth_store
        with _auth_store_lock():
            store = _load_auth_store()
            store["active_provider"] = value
            _save_auth_store(store)
    except Exception:
        # 尽力而为——如果 auth.json 无法恢复，用户的
        # 主 provider 可能已被选择器停用。可以重新运行
        # `hermes model` 来修复。不要因此导致 fallback add 失败。
        pass


# ---------------------------------------------------------------------------
# 子命令处理函数
# ---------------------------------------------------------------------------

def cmd_fallback_list(args) -> None:  # noqa: ARG001
    """打印当前 fallback 链。"""
    from hermes_cli.config import load_config

    config = load_config()
    chain = _read_chain(config)

    print()
    if not chain:
        print("  No fallback providers configured.")
        print()
        print("  Add one with:  hermes fallback add")
        print()
        return

    primary = _describe_primary(config)
    if primary:
        print(f"  Primary:   {primary}")
        print()
    print(f"  Fallback chain ({len(chain)} {'entry' if len(chain) == 1 else 'entries'}):")
    for i, entry in enumerate(chain, 1):
        print(f"    {i}. {_format_entry(entry)}")
    print()
    print("  Tried in order when the primary fails (rate-limit, 5xx, connection errors).")
    print("  Docs: https://hermes-agent.nousresearch.com/docs/user-guide/features/fallback-providers")
    print()


def _describe_primary(config: Dict[str, Any]) -> Optional[str]:
    """主模型的单行描述，用于显示。"""
    model_cfg = config.get("model")
    if isinstance(model_cfg, dict):
        provider = (model_cfg.get("provider") or "?").strip() or "?"
        model = (model_cfg.get("default") or model_cfg.get("model") or "?").strip() or "?"
        return f"{model}  (via {provider})"
    if isinstance(model_cfg, str) and model_cfg.strip():
        return model_cfg.strip()
    return None


def cmd_fallback_add(args) -> None:
    """启动与 `hermes model` 相同的选择器，然后将选择追加到链中。"""
    from hermes_cli.main import _require_tty, select_provider_and_model
    from hermes_cli.config import load_config, save_config

    _require_tty("fallback add")

    # 在选择器运行之前做快照，这样可以通过比较前后状态
    # 来区分"用户确实选了内容"和"用户取消了"。
    before_cfg = load_config()
    model_before = copy.deepcopy(before_cfg.get("model"))
    active_provider_before = _snapshot_auth_active_provider()

    print()
    print("  Adding a fallback provider.  The picker below is the same one used by")
    print("  `hermes model` — select the provider + model you want as a fallback.")
    print()

    try:
        select_provider_and_model(args=args)
    except SystemExit:
        # 某些 provider 流程在认证失败时会退出——恢复状态后重新抛出。
        _restore_model_cfg(model_before)
        _restore_auth_active_provider(active_provider_before)
        raise

    # 读取选择器运行后的状态以查看用户选了什么。
    after_cfg = load_config()
    model_after = after_cfg.get("model")

    new_entry = _extract_fallback_from_model_cfg(model_after)
    if not new_entry:
        # 选择器未完成（用户取消或流程中止）。无需处理。
        _restore_model_cfg(model_before)
        _restore_auth_active_provider(active_provider_before)
        print()
        print("  No fallback added.")
        return

    # 选择器选中的内容与当前主模型相同→没有变化，
    # 也没有有意义的内容可以作为自身的 fallback。
    primary_entry = _extract_fallback_from_model_cfg(model_before)
    if primary_entry and primary_entry["provider"] == new_entry["provider"] \
            and primary_entry["model"] == new_entry["model"]:
        _restore_model_cfg(model_before)
        _restore_auth_active_provider(active_provider_before)
        print()
        print(f"  Selected model matches the current primary ({_format_entry(new_entry)}).")
        print("  A provider cannot be a fallback for itself — no change.")
        return

    # 恢复主模型后重新加载 config，然后将新条目追加到
    # ``fallback_providers``。这里特意重新加载（而不是直接修改
    # ``after_cfg``），因为选择器可能修改了其他顶层键
    # （custom_providers、providers 凭证），我们希望保留这些更改。
    _restore_model_cfg(model_before)
    _restore_auth_active_provider(active_provider_before)

    final_cfg = load_config()
    chain = _read_chain(final_cfg)

    # 拒绝完全重复的 fallback 条目。
    for existing in chain:
        if existing.get("provider") == new_entry["provider"] \
                and existing.get("model") == new_entry["model"]:
            print()
            print(f"  {_format_entry(new_entry)} is already in the fallback chain — skipped.")
            return

    chain.append(new_entry)
    _write_chain(final_cfg, chain)
    save_config(final_cfg)

    print()
    print(f"  Added fallback: {_format_entry(new_entry)}")
    print(f"  Chain is now {len(chain)} {'entry' if len(chain) == 1 else 'entries'} long.")
    print()
    print("  Run `hermes fallback list` to view, or `hermes fallback remove` to delete.")


def _restore_model_cfg(model_before: Any) -> None:
    """将 ``config["model"]`` 恢复到之前捕获的快照。"""
    from hermes_cli.config import load_config, save_config

    cfg = load_config()
    if model_before is None:
        cfg.pop("model", None)
    else:
        cfg["model"] = copy.deepcopy(model_before)
    save_config(cfg)


def cmd_fallback_remove(args) -> None:  # noqa: ARG001
    """从链中选取一个条目并移除。"""
    from hermes_cli.config import load_config, save_config

    config = load_config()
    chain = _read_chain(config)

    if not chain:
        print()
        print("  No fallback providers configured — nothing to remove.")
        print()
        return

    choices = [_format_entry(e) for e in chain]
    choices.append("Cancel")

    try:
        from hermes_cli.setup import _curses_prompt_choice
        idx = _curses_prompt_choice("Select a fallback to remove:", choices, 0)
    except Exception:
        idx = _numbered_pick("Select a fallback to remove:", choices)

    if idx is None or idx < 0 or idx >= len(chain):
        print()
        print("  Cancelled — no change.")
        return

    removed = chain.pop(idx)
    _write_chain(config, chain)
    save_config(config)

    print()
    print(f"  Removed fallback: {_format_entry(removed)}")
    if chain:
        print(f"  Chain is now {len(chain)} {'entry' if len(chain) == 1 else 'entries'} long.")
    else:
        print("  Fallback chain is now empty.")
    print()


def cmd_fallback_clear(args) -> None:  # noqa: ARG001
    """移除所有 fallback 条目（需确认）。"""
    from hermes_cli.config import load_config, save_config

    config = load_config()
    chain = _read_chain(config)

    if not chain:
        print()
        print("  No fallback providers configured — nothing to clear.")
        print()
        return

    print()
    print(f"  Current fallback chain ({len(chain)} {'entry' if len(chain) == 1 else 'entries'}):")
    for i, entry in enumerate(chain, 1):
        print(f"    {i}. {_format_entry(entry)}")
    print()
    try:
        resp = input("  Clear all entries? [y/N]: ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        print()
        print("  Cancelled.")
        return
    if resp not in {"y", "yes"}:
        print("  Cancelled — no change.")
        return

    _write_chain(config, [])
    save_config(config)
    print()
    print("  Fallback chain cleared.")
    print()


def _numbered_pick(question: str, choices: List[str]) -> Optional[int]:
    """当 curses 不可用时的回退编号列表选择器。"""
    print(question)
    for i, c in enumerate(choices, 1):
        print(f"  {i}. {c}")
    print()
    while True:
        try:
            val = input(f"Choice [1-{len(choices)}]: ").strip()
            if not val:
                return None
            idx = int(val) - 1
            if 0 <= idx < len(choices):
                return idx
            print(f"Please enter 1-{len(choices)}")
        except ValueError:
            print("Please enter a number")
        except (KeyboardInterrupt, EOFError):
            print()
            return None


# ---------------------------------------------------------------------------
# 命令分发
# ---------------------------------------------------------------------------

def cmd_fallback(args) -> None:
    """``hermes fallback [子命令]`` 的顶层分发器。"""
    sub = getattr(args, "fallback_command", None)
    if sub in {None, "", "list", "ls"}:
        cmd_fallback_list(args)
    elif sub == "add":
        cmd_fallback_add(args)
    elif sub in {"remove", "rm"}:
        cmd_fallback_remove(args)
    elif sub == "clear":
        cmd_fallback_clear(args)
    else:
        print(f"Unknown fallback subcommand: {sub}")
        print("Use one of: list, add, remove, clear")
        raise SystemExit(2)
