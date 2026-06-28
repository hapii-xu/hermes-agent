"""每个 profile 的 gateway s6 服务的容器启动协调。

/run/service/ 下的服务目录位于 **tmpfs** 上，每次容器重启时都会被清空。
``$HERMES_HOME/profiles/<name>/`` 下的 profile 目录位于持久化 VOLUME 上，
每个目录的 ``gateway_state.json`` 中记录了其 gateway 的最后状态。
本模块负责桥接两者：在每次容器启动时，遍历持久化的 profile，
重建 s6 服务槽，并仅自动启动最后记录状态为 ``running`` 的 gateway。

通过 Dockerfile（Phase 4 Task 4.0）以 /etc/cont-init.d/02-reconcile-profiles
的形式接入镜像。在 01-hermes-setup（stage2 hook）完成 volume chown
和 $HERMES_HOME 初始化之后、s6-rc 启动用户服务之前，以 root 身份运行。

若无此模块，每次 ``docker restart`` 将静默清空所有 profile 的 gateway，
即使用户的 profile 仍在磁盘上。
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Sequence

log = logging.getLogger(__name__)

# 只有此期望状态才会触发自动重启。其他状态
#（startup_failed、starting、stopped、missing）将槽位注册为 down 状态，
# 等待用户显式操作 — 这避免了崩溃循环，即损坏的 gateway 在
# `docker restart` 周期中不断被重启。旧版安装只有 gateway_state；
# 新版生命周期命令单独持久化 desired_state，以防瞬时运行时状态
#（draining/startup_failed）在 pod/容器重建后抹去操作者的持久启停意图。
_AUTOSTART_STATES = frozenset({"running"})

# 在重建服务槽之前清扫的陈旧运行时文件。这些文件保存的是
# 容器命名空间中的状态（PID、进程表），重启后即为垃圾 ——
# 新容器中数值相等的 PID 是不同的进程。参见计划中的风险登记。
_STALE_RUNTIME_FILES = ("gateway.pid", "processes.json")

ReconcileActionLabel = Literal["started", "registered", "skipped"]


@dataclass(frozen=True)
class ReconcileAction:
    """单次协调过程中某个 profile 的结果。"""
    profile: str
    prior_state: str | None
    action: ReconcileActionLabel


def reconcile_profile_gateways(
    *,
    hermes_home: Path,
    scandir: Path,
    dry_run: bool = False,
    container_argv: Sequence[str] | None = None,
) -> list[ReconcileAction]:
    """为每个持久化 profile 重建 s6 服务注册。

    始终为根 profile 注册 ``gateway-default`` 槽位
    （位于 ``$HERMES_HOME`` 顶层而非 ``profiles/`` 下的隐式 profile）。
    ``hermes_cli.gateway`` 中的分发器将空 profile 后缀映射到 ``gateway-default``，
    因此该槽位是 ``hermes gateway start``（无 ``-p``）的目标。
    若无此槽位，容器内裸的 ``hermes gateway start`` 将落到
    ``s6-svc -u /run/service/gateway-default`` → 未捕获的
    ``CalledProcessError`` → 堆栈跟踪暴露给用户（PR #30136 review）。

    默认槽位的先前状态从
    ``$HERMES_HOME/gateway_state.json`` 读取（与 profile 根目录同级，
    不在 ``profiles/`` 下）；其中的陈旧运行时文件的清扫方式与命名 profile 相同。

    Args:
        hermes_home: 容器的 HERMES_HOME（通常为 /opt/data）。
            Profile 位于 ``<hermes_home>/profiles/<name>/`` 下；
            默认 profile 位于 ``<hermes_home>`` 本身。
        scandir: s6 动态 scandir（通常为 /run/service）。服务
            目录创建在 ``<scandir>/gateway-<profile>/`` 下。
        dry_run: 为 True 时，遍历并返回操作列表，不触及文件系统。
            用于测试和 `--dry-run` 调试。
        container_argv: 可选的容器 PID 1 argv 覆盖值。生产环境
            读取 ``/proc/1/cmdline``；测试直接注入。

    Returns:
        每个 profile 对应一个 :class:`ReconcileAction`，顺序为：
        先 ``default``，然后按目录顺序的命名 profile。
    """
    actions: list[ReconcileAction] = []

    # 默认 profile — 始终注册，即使根 profile 目录从未被填充过。
    # 该槽位存在是为了让 ``hermes gateway start``（无 ``-p``）有地方落；
    # 仅当先前状态为 "running" 时才自动启动（与命名 profile 规则相同）。
    # 若容器以旧版 `gateway run` 命令启动且尚无状态，
    # 则将该意图种入为 `running`，使 s6 协调器保持 pre-s6 行为。
    legacy_default_state = _maybe_migrate_legacy_gateway_run_state(
        hermes_home,
        container_argv=container_argv,
        dry_run=dry_run,
    )
    default_prior_state = legacy_default_state or _read_desired_state(hermes_home)
    default_should_start = default_prior_state in _AUTOSTART_STATES
    if not dry_run:
        _cleanup_stale_runtime_files(hermes_home)
        _register_service(scandir, "default", start=default_should_start)
    actions.append(ReconcileAction(
        profile="default",
        prior_state=default_prior_state,
        action="started" if default_should_start else "registered",
    ))

    profiles_root = hermes_home / "profiles"
    if profiles_root.is_dir():
        for entry in sorted(profiles_root.iterdir()):
            if not entry.is_dir():
                continue
            # SOUL.md 始终由 `hermes profile create` 生成（config.yaml 不是 ——
            # 那在之后通过 `hermes setup` 生成）。将其用作"真实 profile"的标记，
            # 以防止误抓 stray 目录（备份、手动 mkdir）。
            if not (entry / "SOUL.md").exists():
                continue
            # "default" 服务名为根 profile（上方）保留 ——
            # 若用户以某种方式创建了 ``profiles/default/`` 目录，
            # 则跳过以避免槽位冲突。如果他们重命名目录，
            # 其 gateway 仍可通过 ``hermes -p default-named gateway start`` 访问；
            # 我们不在此尝试消除歧义。
            if entry.name == "default":
                log.warning(
                    "profiles/default/ exists — skipping to avoid colliding "
                    "with the reserved root-profile s6 slot",
                )
                continue

            prior_state = _read_desired_state(entry)
            should_start = prior_state in _AUTOSTART_STATES

            if not dry_run:
                _cleanup_stale_runtime_files(entry)
                _register_service(scandir, entry.name, start=should_start)

            actions.append(ReconcileAction(
                profile=entry.name,
                prior_state=prior_state,
                action="started" if should_start else "registered",
            ))

    if not dry_run:
        _write_reconcile_log(hermes_home, actions)
    return actions


def _maybe_migrate_legacy_gateway_run_state(
    hermes_home: Path,
    *,
    container_argv: Sequence[str] | None,
    dry_run: bool,
) -> str | None:
    """为 pre-s6 的 `gateway run` 容器初始化根 gateway_state。

    tini 镜像允许 Docker 用户将 gateway 作为容器命令运行
    （`docker run ... gateway run`）。在 s6 迁移后，
    profile gateway 从持久化的 gateway_state.json 恢复；
    没有状态文件的旧版容器因此会将默认服务注册为 down 且永不启动。
    仅在不存在根 gateway_state.json 时才合成状态，
    以确保显式的已停止/已失败状态在重启后保持优先。
    """
    state_file = hermes_home / "gateway_state.json"
    if state_file.exists():
        return None

    if os.environ.get("HERMES_GATEWAY_NO_SUPERVISE", "").lower() in ("1", "true", "yes"):
        return None

    argv = tuple(container_argv) if container_argv is not None else _read_container_argv()
    if not _is_legacy_gateway_run_request(argv):
        return None

    if not dry_run:
        import time
        state_file.write_text(json.dumps({
            "gateway_state": "running",
            "desired_state": "running",
            "timestamp": int(time.time()),
            "migrated_from": "legacy-container-cmd",
        }) + "\n")
    return "running"


def _read_container_argv() -> tuple[str, ...]:
    """Best-effort read of the container's main program argv.

    Under s6-overlay v2, PID 1 is ``/init`` and its argv contains the
    ``main-wrapper.sh`` path.  Under s6-overlay v3, PID 1 is
    ``s6-svscan`` and the actual command (``rc.init top main-wrapper.sh
    ...``) lives on a different PID.  We try PID 1 first (fast path,
    covers v2 and pre-s6 images), then fall back to scanning
    ``/proc/*/cmdline`` for a process whose argv contains
    ``main-wrapper.sh`` (the rc.init-launched PID in v3).
    """
    # Fast path: PID 1 is the command itself (s6-overlay v2 / tini).
    try:
        raw = Path("/proc/1/cmdline").read_bytes()
        argv = tuple(
            part.decode("utf-8", "replace") for part in raw.split(b"\0") if part
        )
        if any("main-wrapper.sh" in part for part in argv):
            return argv
    except OSError:
        pass

    # 慢速路径：s6-overlay v3 — PID 1 为 s6-svscan；
    # 查找 argv 中包含 main-wrapper.sh 的 rc.init 启动进程。
    try:
        proc_dir = Path("/proc")
        for entry in proc_dir.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                raw = (entry / "cmdline").read_bytes()
            except OSError:
                continue
            argv = tuple(
                part.decode("utf-8", "replace")
                for part in raw.split(b"\0")
                if part
            )
            if any("main-wrapper.sh" in part for part in argv):
                return argv
    except OSError:
        pass

    return ()


def _strip_container_argv_prefix(argv: Sequence[str]) -> list[str]:
    """剥离容器 argv 的 s6/wrapper 前缀，仅保留 hermes 参数。

    处理两种容器命令 argv 形态：

    * **s6-overlay v2 / tini：** PID 1 argv 为
      ``/init /opt/hermes/docker/main-wrapper.sh <subcommand> [args...]``。
    * **s6-overlay v3：** PID 1 为 ``s6-svscan``，命令位于
      rc.init 启动进程上，形如 ``/bin/sh -e
      /run/s6/basedir/scripts/rc.init top /opt/hermes/docker/main-wrapper.sh
      <subcommand> [args...]``（参见 :func:`_read_container_argv`）。

    不按位置逐个剥离前导 token（s6 一旦改变其 launcher 形态即会静默失效 ——
    v2→v3 就是这种情况），而是丢弃 ``main-wrapper.sh`` token 及其之前的所有内容：
    该 wrapper 路径是镜像拥有的稳定边界，子命令始终紧随其后。
    pre-s6 / 直接 ``hermes`` 调用不带 wrapper，则退回剥离裸 ``init`` 前缀。
    wrapper 重新执行 ``hermes <subcommand>``，因此显式的前导 ``hermes`` 也会被剥离。
    被 legacy-gateway 和 dashboard 角色检测器共用。
    """
    args = list(argv)

    # 首选边界：main-wrapper.sh 之前（含）的所有内容均为 launcher 前缀。
    # 用一条规则同时覆盖 s6-overlay v2 (`/init …main-wrapper.sh …`)
    # 和 v3 (`/bin/sh -e …rc.init top …main-wrapper.sh …`)。
    wrapper_idx = next(
        (i for i, a in enumerate(args) if a.endswith("main-wrapper.sh")),
        None,
    )
    if wrapper_idx is not None:
        args = args[wrapper_idx + 1 :]
    elif args and Path(args[0]).name == "init":
        # 防御性处理：argv 中有 `init` 前缀但无 wrapper token。
        args = args[1:]

    # wrapper 重新执行 `hermes <subcommand>`；剥离显式的 hermes。
    if args and Path(args[0]).name == "hermes":
        args = args[1:]
    return args


def _is_legacy_gateway_run_request(argv: Sequence[str]) -> bool:
    """若 Docker 命令等同于 `gateway run`，则返回 True。"""
    args = _strip_container_argv_prefix(argv)
    if "--no-supervise" in args:
        return False
    return len(args) >= 2 and args[0] == "gateway" and args[1] == "run"


def _is_dashboard_container(argv: Sequence[str]) -> bool:
    """若容器命令为 dashboard，则返回 True。

    纯 dashboard 容器（``hermes dashboard ...``）从不生成或管理
    per-profile gateway — 那是 gateway 容器的职责。
    在那里协调 profile gateway s6 槽位不仅是无用功：
    当 gateway 与 dashboard 容器共享绑定挂载的 HERMES_HOME 时，
    两者会竞争 ``flock()`` 同一 ``logs/gateways/<profile>/lock`` 文件，
    产生 "Resource busy" 错误并引发 s6-log 重启风暴。
    因此 dashboard 容器完全跳过协调。

    从 PID 1 argv（``/proc/1/cmdline``）检测，而非操作者标志：
    角色是容器命令的固有事实，不可调整；标志可能在手写的
    compose/k8s manifest 中被遗忘 — 恰恰重新引入本函数所预防的风暴。
    与 :func:`_is_legacy_gateway_run_request` 的 argv 处理方式相同。
    """
    args = _strip_container_argv_prefix(argv)
    return bool(args) and args[0] == "dashboard"


def _read_desired_state(profile_dir: Path) -> str | None:
    """读取持久化的 gateway 期望状态以供协调使用。

    较新的状态文件携带 ``desired_state``：由 s6 生命周期命令写入的操作者意图。
    旧版文件只有 ``gateway_state``；保留其作为兼容性回退，
    以确保现有运行/停止的 profile 在下次显式启停前保持原有行为。

    文件缺失或无法解析时，视为"无期望状态"，
    以防止因损坏文件而阻断整个协调过程。
    """
    state_file = profile_dir / "gateway_state.json"
    if not state_file.exists():
        return None
    try:
        data = json.loads(state_file.read_text())
        desired_state = data.get("desired_state")
        if desired_state is not None:
            return desired_state
        return data.get("gateway_state")
    except (OSError, json.JSONDecodeError):
        log.warning(
            "could not read %s; treating as no prior state", state_file,
        )
        return None


def _cleanup_stale_runtime_files(profile_dir: Path) -> None:
    """Remove gateway.pid and processes.json — they reference PIDs in
    the dead container's process namespace and would otherwise confuse
    the newly-started gateway's process-mismatch checks."""
    for name in _STALE_RUNTIME_FILES:
        (profile_dir / name).unlink(missing_ok=True)


def _register_service(scandir: Path, profile: str, *, start: bool) -> None:
    """Recreate the s6 service slot for one profile.

    Mirrors the rendering in :func:`S6ServiceManager.register_profile_gateway`,
    but here we control the start state directly via the ``down`` marker
    file (s6-svscan honors it on rescan). Cannot use the manager
    directly because the cont-init.d phase runs as root before
    s6-svscan starts scanning the dynamic scandir — the manager's
    ``s6-svscanctl -a`` call would fail with no control socket.

    Atomicity: build the new layout in a sibling temp directory and
    rename it into place via :meth:`Path.replace`. This matches
    :meth:`S6ServiceManager.register_profile_gateway` (PR #30136
    review item O4) — even though cont-init.d runs before s6-svscan
    starts scanning, an atomic publication keeps the contract uniform
    between the two registration paths and protects against a
    half-populated dir if the script is interrupted mid-write.
    """
    import shutil

    from hermes_cli.service_manager import (
        S6ServiceManager,
        _seed_supervise_skeleton,
        validate_profile_name,
    )

    validate_profile_name(profile)
    service_dir = scandir / f"gateway-{profile}"
    tmp_dir = service_dir.with_name(service_dir.name + ".tmp")

    # Wipe any leftover tmp from a previous interrupted run.
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True)

    try:
        (tmp_dir / "type").write_text("longrun\n")

        # Reuse the manager's run-script rendering — single source of
        # truth so register_profile_gateway and reconcile_profile_gateways
        # stay consistent. extra_env is empty here; users who need
        # per-profile env can set it via the profile's config.yaml
        # (which the gateway itself loads).
        run = tmp_dir / "run"
        run.write_text(S6ServiceManager._render_run_script(profile, extra_env={}))
        run.chmod(0o755)

        finish = tmp_dir / "finish"
        finish.write_text(S6ServiceManager._render_finish_script())
        finish.chmod(0o755)

        # Persistent log rotation (OQ8-C).
        log_subdir = tmp_dir / "log"
        log_subdir.mkdir()
        log_run = log_subdir / "run"
        log_run.write_text(S6ServiceManager._render_log_run(profile))
        log_run.chmod(0o755)

        # The presence of a `down` file tells s6-supervise to NOT
        # start the service when s6-svscan picks it up. User brings
        # it up explicitly with `hermes -p <profile> gateway start`
        # (which routes through the Phase 4
        # _dispatch_via_service_manager_if_s6 helper to `s6-svc -u`).
        if not start:
            (tmp_dir / "down").touch()

        # Pre-create the supervise/ skeleton with hermes ownership
        # BEFORE we publish the slot. Mirrors the same pre-creation
        # step in S6ServiceManager.register_profile_gateway — when
        # s6-svscan picks the published slot up, the s6-supervise it
        # spawns will EEXIST our dirs/FIFOs and inherit hermes
        # ownership, so runtime s6-svc / s6-svstat / s6-svwait calls
        # (all dispatched as the hermes user) won't hit EACCES. See
        # ``_seed_supervise_skeleton`` in service_manager.py for the
        # full rationale.
        _seed_supervise_skeleton(tmp_dir)

        # Publish atomically. Path.replace handles the existing-target
        # case the same way os.rename does on POSIX: the target is
        # silently replaced, so a previous reconcile pass's slot is
        # cleanly overwritten in one operation.
        if service_dir.exists():
            shutil.rmtree(service_dir)
        tmp_dir.replace(service_dir)
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise


def _write_reconcile_log(
    hermes_home: Path, actions: list[ReconcileAction],
) -> None:
    """Append one line per profile to $HERMES_HOME/logs/container-boot.log.

    Operators inspect this to debug "why didn't my profile come back
    up". Keeping a separate log file (vs. mixing into agent.log) lets
    troubleshooters grep for "profile=foo" without wading through
    unrelated activity.

    Size-bounded: when the file exceeds ``_LOG_ROTATE_BYTES``
    (defaults to 256 KiB ≈ 3000 reconcile lines), the current file
    is renamed to ``container-boot.log.1`` (replacing any previous
    rotation) before the new entries are appended. This gives long-
    lived containers a soft cap of ~512 KiB across the two files
    without pulling in logrotate or s6-log machinery just for this
    one append-only file (PR #30136 review item O3).
    """
    import time
    log_dir = hermes_home / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "container-boot.log"

    # Rotate before opening to append, so the new entries always land
    # in a fresh file when we crossed the threshold last time.
    try:
        if log_path.exists() and log_path.stat().st_size >= _LOG_ROTATE_BYTES:
            log_path.replace(log_dir / "container-boot.log.1")
    except OSError as exc:
        # Rotation failure is non-fatal — keep appending to the
        # existing file rather than losing the entry entirely.
        log.warning("could not rotate %s: %s", log_path, exc)

    ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    with log_path.open("a", encoding="utf-8") as f:
        for a in actions:
            f.write(
                f"{ts} profile={a.profile} prior_state={a.prior_state} "
                f"action={a.action}\n"
            )


# 256 KiB soft cap on container-boot.log; rotated to .1 when crossed.
# At ~80 B per reconcile-action line this is ~3000 lines, or about a
# year of daily reboots on a 5-profile container. Two files = ~512 KiB
# worst case. Tuned for visibility (small enough to grep / cat without
# scrolling forever) more than space (the persistent volume has GB).
_LOG_ROTATE_BYTES = 256 * 1024


def main() -> int:
    """Entry point invoked from /etc/cont-init.d/02-reconcile-profiles."""
    # A dashboard-only container never spawns or supervises per-profile
    # gateways, so reconciling their s6 slots here is pure waste — and
    # actively harmful: when the gateway and dashboard containers share a
    # bind-mounted HERMES_HOME, both race to flock() the same s6-log lock
    # files under logs/gateways/<profile>/lock, producing "Resource busy"
    # failures and a restart storm. Detect the role from PID 1 argv and
    # skip reconciliation in the dashboard container. No operator flag:
    # the role is a fact about the container's command, and a flag can be
    # forgotten in a hand-written manifest, reintroducing the storm.
    if _is_dashboard_container(_read_container_argv()):
        print(
            "reconcile: skipping (dashboard container — does not need "
            "per-profile gateways)"
        )
        return 0

    hermes_home = Path(os.environ.get("HERMES_HOME", "/opt/data"))
    scandir = Path(os.environ.get("S6_PROFILE_GATEWAY_SCANDIR", "/run/service"))
    actions = reconcile_profile_gateways(
        hermes_home=hermes_home, scandir=scandir,
    )
    for a in actions:
        print(
            f"reconcile: profile={a.profile} "
            f"prior_state={a.prior_state} action={a.action}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
