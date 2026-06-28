"""gateway 重启相关的共享常量与解析辅助函数。"""

from hermes_cli.config import DEFAULT_CONFIG

# 来自 sysexits.h 的 EX_TEMPFAIL —— 用于在优雅 drain/reload 流程结束后，
# 请求 service manager 重启 gateway。
GATEWAY_SERVICE_RESTART_EXIT_CODE = 75

# 来自 sysexits.h 的 EX_CONFIG —— 致命的配置错误（例如 token 冲突、
# 没有配置任何消息平台）。s6 finish 脚本会把它转换成退出码 125
# （永久失败），这样 supervisor 就会停止重启 gateway。参见 #51228。
GATEWAY_FATAL_CONFIG_EXIT_CODE = 78

DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT = float(
    DEFAULT_CONFIG["agent"]["restart_drain_timeout"]
)


def parse_restart_drain_timeout(raw: object) -> float:
    """解析配置的 drain 超时时间，解析失败时回退到共享默认值。"""
    try:
        value = float(raw) if str(raw or "").strip() else DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT
    except (TypeError, ValueError):
        return DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT
    return max(0.0, value)
