from __future__ import annotations

import asyncio
import importlib
import json
import logging
import traceback
import uuid
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("registry")

STOPPED = "STOPPED"
STARTING = "STARTING"
RUNNING = "RUNNING"
ERROR = "ERROR"

DEFAULT_SPAN = [6, 5]


@dataclass
class Module:
    name: str
    app: object
    init: object
    shutdown: object
    state: str = STOPPED


class Registry:
    def __init__(self, root: Path):
        self.root = root
        self.plugins_dir = root / "plugins"
        self.config_path = root / "config.json"
        self.modules: dict[str, Module] = {}
        self.tabs: dict[str, list[dict]] = {}
        self._subs: set[asyncio.Queue] = set()

    # ---- 发现 ----
    def discover(self) -> None:
        if self.plugins_dir.is_dir():
            for d in sorted(self.plugins_dir.iterdir()):
                if not d.is_dir() or d.name.startswith("_") or d.name.startswith("."):
                    continue
                try:
                    m = importlib.import_module(f"plugins.{d.name}.module")
                    self.modules[d.name] = Module(
                        name=d.name,
                        app=m.app,
                        init=getattr(m, "init"),
                        shutdown=getattr(m, "shutdown"),
                    )
                except Exception:
                    log.error("模块 %s 加载失败:\n%s", d.name, traceback.format_exc())
        self._load_config()

    # ---- 配置 ----
    def _load_config(self) -> None:
        cfg = {}
        if self.config_path.exists():
            try:
                cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
            except Exception:
                log.error("config.json 解析失败，按空配置处理:\n%s", traceback.format_exc())
        tabs = cfg.get("tabs") or {}
        for tname in list(tabs):  # 清理失效引用
            entries = [e for e in tabs[tname] if isinstance(e, dict) and e.get("name") in self.modules]
            if entries:
                tabs[tname] = entries
            else:
                del tabs[tname]
        if not tabs:
            tabs = {"常用": []}
        placed = {e["name"] for e_list in tabs.values() for e in e_list}
        for e_list in tabs.values():
            for e in e_list:
                if not isinstance(e.get("id"), str) or not e["id"]:
                    e["id"] = f"{e['name']}-{uuid.uuid4().hex[:8]}"
        first = next(iter(tabs))
        for name in self.modules:  # 新发现模块归入第一个标签页
            if name not in placed:
                tabs[first].append({"id": f"{name}-{uuid.uuid4().hex[:8]}", "name": name, "span": DEFAULT_SPAN[:]})
        self.tabs = tabs
        self.save_config()

    def save_config(self) -> None:
        self.config_path.write_text(
            json.dumps({"tabs": self.tabs}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def referenced(self) -> set[str]:
        return {e["name"] for entries in self.tabs.values() for e in entries}

    def _referenced_order(self) -> list[str]:
        order, seen = [], set()
        for entries in self.tabs.values():
            for e in entries:
                if e["name"] not in seen:
                    order.append(e["name"])
                    seen.add(e["name"])
        return order

    # ---- 生命周期 ----
    async def start(self, name: str) -> None:
        m = self.modules.get(name)
        if m is None or m.state in (RUNNING, STARTING):
            return
        m.state = STARTING
        self._publish(name, m.state)
        try:
            await m.init()
            m.state = RUNNING
        except Exception:
            log.error("模块 %s init 失败:\n%s", name, traceback.format_exc())
            m.state = ERROR
        self._publish(name, m.state)

    async def stop(self, name: str) -> None:
        m = self.modules.get(name)
        if m is None or m.state not in (RUNNING, ERROR):
            return
        m.state = STOPPED
        try:
            await m.shutdown()
        except Exception:
            log.error("模块 %s shutdown 异常:\n%s", name, traceback.format_exc())
        self._publish(name, m.state)

    async def start_all(self) -> None:
        for name in self._referenced_order():
            await self.start(name)

    async def stop_all(self) -> None:
        for name in list(self.modules):
            await self.stop(name)

    # ---- 事件 ----
    def snapshot(self) -> dict[str, str]:
        return {name: m.state for name, m in self.modules.items()}

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def _publish(self, name: str, state: str) -> None:
        ev = json.dumps({"name": name, "state": state}, ensure_ascii=False)
        for q in self._subs:
            q.put_nowait(ev)
