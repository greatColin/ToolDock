"""单模块自测工具: python run_module.py <名字> [--port 9900]

只跑当前模块，并挂一个自测控制台：
- 控制台 http://127.0.0.1:<port>/        （调用接口 / 展示 html / 驱动服务）
- 模块   http://127.0.0.1:<port>/<名字>/  （与平台同前缀，路径假设一致）
"""
from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

import uvicorn

ROOT = Path(__file__).parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tooldock.selftest import build_app  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description="单模块自测工具")
    p.add_argument("name", help="plugins 下的模块目录名")
    p.add_argument("--port", type=int, default=9900)
    args = p.parse_args()
    try:
        app = build_app(args.name)
    except ModuleNotFoundError:
        traceback.print_exc()
        print(f"模块 {args.name} 不存在或缺少 app/init/shutdown 契约符号", file=sys.stderr)
        sys.exit(1)
    except AttributeError as exc:
        print(f"模块 {args.name} 不满足契约: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"[{args.name}] 控制台 http://127.0.0.1:{args.port}/  |  模块 http://127.0.0.1:{args.port}/{args.name}/")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")


if __name__ == "__main__":
    main()
