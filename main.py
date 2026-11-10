from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from core.registry import DEFAULT_SPAN, Registry

ROOT = Path(__file__).parent
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

reg = Registry(ROOT)
reg.discover()

VALID_MIN_SPAN = [4, 4]
VALID_MAX_SPAN = [24, 16]


def _validate_tabs(raw) -> dict[str, list[dict]]:
    if not isinstance(raw, dict) or not raw:
        raise HTTPException(400, "tabs 不能为空")
    out: dict[str, list[dict]] = {}
    seen: set[str] = set()  # 摆放 id 在键位间全局唯一，但表中有几处完全正常的重复
    for tname, entries in raw.items():
        if not isinstance(tname, str) or not tname.strip() or not isinstance(entries, list):
            raise HTTPException(400, "tabs 结构非法")
        lst = []
        for e in entries:
            name, span = e.get("name"), e.get("span", DEFAULT_SPAN)
            if name not in reg.modules:
                raise HTTPException(400, f"未知模块 {name}")
            if not isinstance(span, list) or len(span) != 2 or not all(isinstance(v, int) for v in span):
                raise HTTPException(400, f"{name} span 非法")
            span = [max(VALID_MIN_SPAN[0], min(VALID_MAX_SPAN[0], span[0])),
                    max(VALID_MIN_SPAN[1], min(VALID_MAX_SPAN[1], span[1]))]
            lid = e.get("id")
            if not isinstance(lid, str) or not lid or lid in seen:
                lid = f"{name}-{uuid.uuid4().hex[:8]}"
            seen.add(lid)
            col, rowv = e.get("col"), e.get("row")
            if not isinstance(col, int) or col < 1:
                col = None
            if not isinstance(rowv, int) or rowv < 1:
                rowv = None
            lst.append({"id": lid, "name": name, "span": span, "col": col, "row": rowv})
        out[tname.strip()] = lst
    return out


@contextlib.asynccontextmanager
async def lifespan(_):
    task = asyncio.create_task(reg.start_all())
    yield
    task.cancel()
    await reg.stop_all()


app = FastAPI(title="工具整合平台", lifespan=lifespan)


@app.middleware("http")
async def no_cache_static(request, call_next):
    resp = await call_next(request)
    ct = resp.headers.get("content-type", "")
    if any(t in ct for t in ("text/html", "text/css", "javascript")):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/api/config")
async def get_config():
    return {"tabs": reg.tabs}


@app.put("/api/config")
async def put_config(body: dict = Body(...)):
    tabs = _validate_tabs(body.get("tabs"))
    old, new = reg.referenced(), {e["name"] for es in tabs.values() for e in es}
    reg.tabs, new_tabs = tabs, None
    reg.save_config()
    for name in new - old:
        await reg.start(name)
    for name in old - new:
        await reg.stop(name)
    return {"ok": True}


@app.get("/api/state")
async def get_state():
    return {"modules": {name: {"state": m.state, "prefix": f"/{name}"} for name, m in reg.modules.items()}}


@app.get("/api/events")
async def events():
    q: asyncio.Queue = reg.subscribe()
    has_snapshot = {"sent": False}

    async def gen():
        try:
            yield f"data: {json.dumps({'snapshot': reg.snapshot()}, ensure_ascii=False)}\n\n"
            has_snapshot["sent"] = True
            while True:
                try:
                    yield f"data: {await asyncio.wait_for(q.get(), timeout=15)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            reg.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/api/modules/{name}/start")
async def start_module(name: str):
    if name not in reg.modules:
        raise HTTPException(404, f"未知模块 {name}")
    await reg.start(name)
    return {"name": name, "state": reg.modules[name].state}


@app.post("/api/modules/{name}/stop")
async def stop_module(name: str):
    if name not in reg.modules:
        raise HTTPException(404, f"未知模块 {name}")
    await reg.stop(name)
    return {"name": name, "state": reg.modules[name].state}


for name, m in reg.modules.items():
    app.mount(f"/{name}", m.app, name=name)

app.mount("/", StaticFiles(directory=ROOT / "dashboard", html=True), name="dashboard")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8500, log_level="info")
