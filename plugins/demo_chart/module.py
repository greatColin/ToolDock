"""温度折线图示例模块：示范后台 asyncio 任务随模块生命周期启停。

顶层仅 app / init / shutdown 三个符号；路由在首次 init() 中注册一次，
数据由 init 创建的后台任务每秒产出一个随机温度点，shutdown 负责取消并清空。
"""
import asyncio
import random
from collections import deque
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

app = FastAPI(title="demo_chart")


async def init() -> None:
    max_points = 120  # 页面折线保留的原始点数；卡片取最近 20 点
    if getattr(app.state, "ready", False):
        return
    if not getattr(app.state, "installed", False):

        @app.get("/")
        async def page():
            return FileResponse(Path(__file__).with_name("index.html"))

        @app.get("/card")
        async def card():
            return FileResponse(Path(__file__).with_name("card.html"))

        @app.get("/current")
        async def current(n: int = max_points) -> dict:
            kept = max(1, min(n, max_points))
            return {"points": list(app.state.temps)[-kept:]}

        app.state.installed = True

    gen = app.state.gen = getattr(app.state, "gen", 0) + 1

    async def produce() -> None:
        while True:
            app.state.temps.append(round(20 + random.random() * 10, 1))
            await asyncio.sleep(1)
            if app.state.gen != gen:
                return  # shutdown 已换组，取消只是兜底

    app.state.temps = deque(maxlen=max_points)
    app.state.temps.append(round(20 + random.random() * 10, 1))  # 立即出首个点
    app.state.ready = True
    app.state.task = asyncio.create_task(produce())


async def shutdown() -> None:
    app.state.gen = getattr(app.state, "gen", 0) + 1  # 生产任务自检后自动退出
    task = getattr(app.state, "task", None)
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass  # 取消已生效；其他异常才需要上抛
        app.state.task = None
    app.state.ready = False
    if getattr(app.state, "temps", None) is not None:
        app.state.temps.clear()
