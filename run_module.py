"""独立运行单个模块: python run_module.py <名字> [--port 9900]

挂载前缀与平台一致（/<名字>/），用于在无宿主下测试同一套路径假设。
"""
from __future__ import annotations

import argparse
import contextlib
import importlib
import sys
import traceback
from pathlib import Path

import uvicorn
from fastapi import FastAPI

ROOT = Path(__file__).parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def build(name: str, port: int) -> FastAPI:
    m = importlib.import_module(f"plugins.{name}.module")
    app = FastAPI(title=f"模块:{name}", lifespan=lifespan_for(m, name, port))
    app.mount(f"/{name}", m.app, name=name)
    return app


def lifespan_for(m, name: str, port: int):
    @contextlib.asynccontextmanager
    async def lifespan(_):
        await m.init()
        print(f"[{name}] http://127.0.0.1:{port}/{name}/")
        yield
        await m.shutdown()

    return lifespan


def main() -> None:
    p = argparse.ArgumentParser(description="独立运行单个模块")
    p.add_argument("name", help="plugins 下的模块目录名")
    p.add_argument("--port", type=int, default=9900)
    args = p.parse_args()
    try:
        app = build(args.name, args.port)
    except ModuleNotFoundError:
        traceback.print_exc()
        print(f"模块 {args.name} 不存在或缺少 app/init/shutdown 契约符号", file=sys.stderr)
        sys.exit(1)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")


if __name__ == "__main__":
    main()
