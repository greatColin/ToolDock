"""便签示例模块：内存存储，示范 STARTING 耗时与半途 init 残留的幂等清理。

在独立窗口直接向宿主演示契约的标准写法：顶层仅 app / init / shutdown 三个符号，
路由在首次 init() 中注册一次（app.state.installed 把关）。
"""
import asyncio
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

app = FastAPI(title="demo_notes")


async def init() -> None:
    if getattr(app.state, "ready", False):
        return
    if not getattr(app.state, "installed", False):

        class NoteIn(BaseModel):
            text: str

        @app.get("/")
        async def page():
            return FileResponse(Path(__file__).with_name("index.html"))

        @app.get("/card")
        async def card():
            return FileResponse(Path(__file__).with_name("card.html"))

        @app.get("/notes")
        async def list_notes() -> list[dict]:
            return list(app.state.notes.values())

        @app.post("/notes", status_code=201)
        async def add_note(body: NoteIn) -> dict:
            note = {"id": app.state.next_id, "text": body.text}
            app.state.next_id += 1
            app.state.notes[note["id"]] = note
            return note

        @app.delete("/notes/{note_id}")
        async def del_note(note_id: int) -> dict:
            note = app.state.notes.pop(note_id, None)
            if note is None:
                raise HTTPException(status_code=404, detail="便签不存在")
            return note

        app.state.installed = True

    gen = app.state.gen = getattr(app.state, "gen", 0) + 1
    app.state.notes = {}
    app.state.next_id = 1
    await asyncio.sleep(2)  # 模拟耗时初始化，宿主在此期间展示 STARTING
    if app.state.gen != gen:
        return  # 初始化期间被 shutdown 中断，交由其清理
    app.state.ready = True


async def shutdown() -> None:
    app.state.gen = getattr(app.state, "gen", 0) + 1  # 使半途 init 的睡眠失效
    app.state.ready = False
    if getattr(app.state, "notes", None) is not None:
        app.state.notes.clear()
        app.state.next_id = 1
