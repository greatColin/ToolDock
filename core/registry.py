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
        self.db_path = root / "hub.db"
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

    # ---- 配置（SQLite：样例即模板，进程内只持有内存形态）----
    def _db(self):
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS tabs(
                id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, idx INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS placements(
                id TEXT PRIMARY KEY, tab_id INTEGER NOT NULL, module TEXT NOT NULL,
                col INTEGER, row INTEGER, span_w INTEGER NOT NULL DEFAULT 6, span_h INTEGER NOT NULL DEFAULT 5,
                FOREIGN KEY(tab_id) REFERENCES tabs(id) ON DELETE CASCADE);
        """)
        return conn

    def _migration_source(self) -> dict | None:
        """旧 config.json 存在且库为空时，读它并一次性搬入（先读后删）。"""
        if self.config_path.exists():
            try:
                return (json.loads(self.config_path.read_text(encoding="utf-8")) or {}).get("tabs") or {}
            except Exception:
                log.error("config.json 解析失败，忽略迁移:\n%s", traceback.format_exc())
        return None

    def _load_config(self) -> None:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT name FROM tabs ORDER BY idx").fetchall()
            tabs = {name: [] for (name,) in rows}
            for pid, tname, module, col, row, sw, sh in conn.execute(
                "SELECT p.id, t.name, p.module, p.col, p.row, p.span_w, p.span_h "
                "FROM placements p JOIN tabs t ON t.id = p.tab_id ORDER BY t.idx, p.rowid"):
                tabs.setdefault(tname, []).append(
                    {"id": pid, "name": module, "span": [sw, sh], "col": col, "row": row})
        if not tabs:  # 空库尝试迁移旧 config.json
            legacy = self._migration_source()
            if legacy:
                tabs = legacy
                self.tabs = self._sanitize(tabs)
                self.save_config()
                self.config_path.unlink()
                log.info("已从 config.json 迁移 %d 个标签页到 %s", len(self.tabs), self.db_path.name)
                return
        self.tabs = self._sanitize(tabs)
        self.save_config()

    def _sanitize(self, tabs: dict) -> dict:
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
        return tabs

    def save_config(self) -> None:
        with self._db() as conn:
            conn.execute("DELETE FROM tabs")
            for idx, (tname, entries) in enumerate(self.tabs.items()):
                cur = conn.execute("INSERT INTO tabs(name, idx) VALUES (?, ?)", (tname, idx))
                tab_id = cur.lastrowid
                for e in entries:
                    col = e.get("col")
                    row = e.get("row")
                    conn.execute(
                        "INSERT OR REPLACE INTO placements(id, tab_id, module, col, row, span_w, span_h)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (e["id"], tab_id, e["name"],
                         col if isinstance(col, int) and col >= 1 else None,
                         row if isinstance(row, int) and row >= 1 else None,
                         e["span"][0], e["span"][1]))

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
