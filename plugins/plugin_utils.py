"""插件作者的共享并发工具。

插件最常见的陷阱是进程级懒加载单例：

    _client = None

    def get_client():
        global _client
        if _client is not None:
            return _client
        _client = ExpensiveClient(...)   # <-- TOCTOU：两个线程同时执行到这里
        return _client

当两个线程在单例设置之前都调用 ``get_client()`` 时，两者都通过
``is not None`` 检查，都执行昂贵的初始化，第二次写入会覆盖第一次
——导致第一个客户端打开的资源（连接、文件句柄、后台线程）泄漏。

多线程 agent session 共享一个进程（委托 tool 调用、后台 worker、
自我改进 fork），因此这个竞态条件在实践中是可能触发的。与其让每个插件
作者都记住手动实现双重检查锁定，本模块为他们提供了两个线程安全的原语：

* :func:`lazy_singleton` — 零参数访问器场景的装饰器。
* :class:`SingletonSlot` — 手动槽位，用于根据 config/key 参数构建不同
  实例的访问器。

两者都是轻量导入（仅依赖 stdlib ``threading``），因此任何插件都可以导入
它们而无需引入重量级的主机模块。
"""

from __future__ import annotations

import functools
import threading
from typing import Callable, Generic, Optional, TypeVar

__all__ = ["lazy_singleton", "SingletonSlot"]

T = TypeVar("T")


def lazy_singleton(factory: Callable[[], T]) -> Callable[[], T]:
    """将零参数工厂包装为线程安全的懒加载单例访问器。

    包装后的可调用对象在每次调用时返回相同实例；即使并发首次调用，
    工厂也仅执行一次，使用双重检查锁定。附加 ``.reset()`` 属性用于
    测试/清理。

    示例::

        @lazy_singleton
        def get_client():
            return ExpensiveClient(load_config())

        client = get_client()   # 构建一次，跨线程安全
        get_client.reset()      # 丢弃实例（下次调用重新构建）

    注意：如果工厂抛出异常，不缓存任何实例，下次调用会重试
    （无论如何锁都会被释放）。
    """
    lock = threading.Lock()
    box: list = []  # 单元素 [instance]；空 == 尚未构建

    @functools.wraps(factory)
    def accessor() -> T:
        if box:
            return box[0]
        with lock:
            if box:  # 锁内重新检查
                return box[0]
            instance = factory()
            box.append(instance)
            return instance

    def reset() -> None:
        with lock:
            box.clear()

    accessor.reset = reset  # type: ignore[attr-defined]
    return accessor


class SingletonSlot(Generic[T]):
    """线程安全的懒加载槽位，用于接受构建参数的访问器。

    当缓存的实例取决于传递给访问器的 config/key 时使用此槽位
    （因此裸零参数 :func:`lazy_singleton` 不适用）。槽位缓存第一个
    成功构建的实例，并在后续调用中忽略该参数——匹配大多数插件已经依赖的
    已建立的"首次 config 获胜"单例语义。

    示例::

        _slot: SingletonSlot[Honcho] = SingletonSlot()

        def get_honcho_client(config=None):
            return _slot.get(lambda: Honcho(**resolve(config)))

        def reset_honcho_client():
            _slot.reset()

    即使并发首次调用，工厂最多运行一次。如果工厂抛出异常，
    不缓存任何内容，下次调用会重试。
    """

    __slots__ = ("_lock", "_value", "_set")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value: Optional[T] = None
        self._set = False

    def get(self, factory: Callable[[], T]) -> T:
        # 快速路径：已构建，无需加锁（在 CPython 的 GIL 下，
        # 一个 set 布尔值 + 引用读取是原子的）。
        if self._set:
            return self._value  # type: ignore[return-value]
        with self._lock:
            if self._set:  # 锁内重新检查
                return self._value  # type: ignore[return-value]
            value = factory()
            self._value = value
            self._set = True
            return value

    def peek(self) -> Optional[T]:
        """返回缓存的实例而不构建它（如果未设置则返回 None）。"""
        return self._value if self._set else None

    def reset(self) -> None:
        """丢弃缓存的实例，使下次 ``get()`` 重新构建它。"""
        with self._lock:
            self._value = None
            self._set = False
