"""已批准远程 meet 节点的本地 JSON 注册表。

位于 ``$HERMES_HOME/workspace/meetings/nodes.json``。网关
在打开 WebSocket 到远程 bot 主机之前查询它，以将 ``chrome_node`` 名称
解析为 ``(url, token)`` 对。

Schema
------
    {
      "nodes": {
        "<name>": {
          "url":   "ws://host:port",
          "token": "...",
          "added_at": <epoch_float>
        }
      }
    }
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_constants import get_hermes_home


def _default_path() -> Path:
    return Path(get_hermes_home()) / "workspace" / "meetings" / "nodes.json"


class NodeRegistry:
    """简单的文件支持注册表。跨进程非并发安全
    — 假定单写入者（网关 CLI）。"""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else _default_path()

    # ----- 存储 ------------------------------------------------------

    def _load(self) -> Dict[str, Any]:
        if not self.path.is_file():
            return {"nodes": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"nodes": {}}
        if not isinstance(data, dict) or not isinstance(data.get("nodes"), dict):
            return {"nodes": {}}
        return data

    def _save(self, data: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    # ----- 公共 API ---------------------------------------------------

    def get(self, name: str) -> Optional[Dict[str, Any]]:
        data = self._load()
        entry = data["nodes"].get(name)
        if entry is None:
            return None
        return {"name": name, **entry}

    def add(self, name: str, url: str, token: str) -> None:
        if not isinstance(name, str) or not name:
            raise ValueError("node name must be a non-empty string")
        if not isinstance(url, str) or not url:
            raise ValueError("url must be a non-empty string")
        if not isinstance(token, str) or not token:
            raise ValueError("token must be a non-empty string")
        data = self._load()
        data["nodes"][name] = {
            "url": url,
            "token": token,
            "added_at": time.time(),
        }
        self._save(data)

    def remove(self, name: str) -> bool:
        data = self._load()
        if name in data["nodes"]:
            del data["nodes"][name]
            self._save(data)
            return True
        return False

    def list_all(self) -> List[Dict[str, Any]]:
        data = self._load()
        out: List[Dict[str, Any]] = []
        for name, entry in sorted(data["nodes"].items()):
            out.append({"name": name, **entry})
        return out

    def resolve(self, chrome_node: Optional[str]) -> Optional[Dict[str, Any]]:
        """将节点名称解析为其条目。

        如果提供了 ``chrome_node``，则返回该命名节点（或 None）。
        如果 ``chrome_node`` 为 None，则在恰好注册了一个节点时
        返回该唯一节点；否则返回 None（歧义或
        空）。
        """
        if chrome_node:
            return self.get(chrome_node)
        nodes = self.list_all()
        if len(nodes) == 1:
            return nodes[0]
        return None
