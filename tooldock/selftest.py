"""单模块自测工具：只跑当前模块，提供「调用接口 / 展示 html / 提供服务」三件事。

- 调用接口：读模块自身 OpenAPI 列出全部路由，控制台可填参数直接发起调用并看响应。
- 展示 html：按模块契约规则自动取页面（card 优先、其次整页），可预览与看源码。
- 提供服务：控制台直接驱动 init/shutdown，并可跑一次幂等自检。

控制台 API 全部在 /__test 下；模块本体挂载在 /<名字>，与平台同前缀、同路径假设。
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib
import inspect
import json
import re
import time
import traceback
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
UI_DIR = Path(__file__).resolve().parent / "selftest_ui"
API = "/__test"

STOPPED = "STOPPED"
STARTING = "STARTING"
RUNNING = "RUNNING"
ERROR = "ERROR"

SKIP_ROUTES = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
MAX_SOURCE = 200_000


# ---------------------------------------------------------------- ASGI 调用器
class AsgiClient:
    """直接驱动 ASGI app 的最小调用器：不引入 httpx 等额外依赖。

    走宿主应用 = 真实挂载链路（/<名字>/…），与浏览器访问同一套路由假设。
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def request(self, method: str, path: str, *, query: str = "", headers: dict | None = None,
                      body: bytes = b"", timeout: float = 15.0) -> dict:
        hdrs = [(b"host", b"127.0.0.1"), (b"content-length", str(len(body)).encode())]
        for k, v in (headers or {}).items():
            hdrs.append((str(k).lower().encode("latin-1"), str(v).encode("latin-1")))
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": method.upper(),
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("utf-8"),
            "query_string": query.encode("latin-1"),
            "root_path": "",
            "headers": hdrs,
            "client": ("127.0.0.1", 0),
            "server": ("127.0.0.1", 0),
            "state": {},
        }
        delivered = False

        async def receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        messages: list[dict] = []

        async def send(message):
            messages.append(message)

        started = time.perf_counter()
        try:
            await asyncio.wait_for(self.app(scope, receive, send), timeout=timeout)
        except asyncio.TimeoutError:
            return {"status": 0, "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                    "headers": {}, "body": "", "bytes": 0, "error": f"请求超时（>{timeout}s，多为流式接口）"}
        except Exception:
            return {"status": 0, "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                    "headers": {}, "body": traceback.format_exc(), "bytes": 0, "error": "调用异常"}

        status = 500
        resp_headers: dict[str, str] = {}
        chunks: list[bytes] = []
        for msg in messages:
            if msg["type"] == "http.response.start":
                status = msg["status"]
                for k, v in msg.get("headers", []):
                    resp_headers[k.decode("latin-1").lower()] = v.decode("latin-1")
            elif msg["type"] == "http.response.body":
                chunks.append(msg.get("body", b""))
        raw = b"".join(chunks)
        return {"status": status, "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                "headers": resp_headers, "body": raw.decode("utf-8", "replace"), "bytes": len(raw)}


# ---------------------------------------------------------------- 契约与 lint
def _example(schema: dict, components: dict, depth: int = 0) -> Any:
    """按 JSON Schema 生成一份示例值（够用即可，不追求完整）。"""
    if not isinstance(schema, dict) or depth > 6:
        return None
    if "$ref" in schema:
        ref = schema["$ref"].split("/")[-1]
        return _example((components.get(ref) or {}), components, depth + 1)
    for key in ("example", "default"):
        if key in schema:
            return schema[key]
    if schema.get("enum"):
        return schema["enum"][0]
    kind = schema.get("type")
    if kind == "object" or "properties" in schema:
        return {k: _example(v, components, depth + 1) for k, v in (schema.get("properties") or {}).items()}
    if kind == "array":
        return [_example(schema.get("items") or {}, components, depth + 1)]
    if kind in ("integer", "number"):
        return 0
    if kind == "boolean":
        return True
    if kind == "null":
        return None
    return "string"


def _schema_type(schema: dict) -> str:
    if not isinstance(schema, dict):
        return "any"
    if schema.get("type"):
        return str(schema["type"])
    if "properties" in schema:
        return "object"
    if "items" in schema:
        return "array"
    if "$ref" in schema:
        return str(schema["$ref"].split("/")[-1])
    return "any"


def lint_source(src: str) -> list[dict]:
    """对页面源码做软性体检：绝对地址 / 非透明背景（提示级，不阻断）。"""
    findings: list[dict] = []
    for m in re.finditer(r"""(?:fetch|open)\s*\(\s*['"](https?://[^'"]+)['"]""", src):
        findings.append({"level": "warn", "rule": "相对路径", "detail": f"前端请求用了绝对地址 {m.group(1)}"})
    for m in re.finditer(r"""(?:new\s+WebSocket|axios\.\w+)\s*\(\s*['"](https?://[^'"]+)['"]""", src):
        findings.append({"level": "warn", "rule": "相对路径", "detail": f"前端请求用了绝对地址 {m.group(1)}"})
    for block in re.findall(r"<style[^>]*>(.*?)</style>", src, re.S | re.I):
        for selectors, decls in re.findall(r"([^{}]+)\{([^{}]*)\}", block):
            names = [s.strip().lower() for s in selectors.split(",")]
            if not any(n in ("html", "body", ":root") for n in names):
                continue
            for m in re.finditer(r"background(?:-color)?\s*:\s*([^;}\n]+)", decls, re.I):
                value = m.group(1).strip().lower()
                if value in ("transparent", "none", "inherit", "initial", "unset"):
                    continue
                findings.append({"level": "warn", "rule": "透明背景",
                                 "detail": f"html/body 设了非透明背景 background: {m.group(1).strip()}"})
                break
    return findings


# ---------------------------------------------------------------- 自测主体
class SelfTest:
    """单个模块的自测主体：发现路由 / 自动取页面 / 驱动生命周期。"""

    def __init__(self, name: str, module: Any) -> None:
        self.name = name
        self.module = module
        self.prefix = f"/{name}"
        self.app = module.app
        self.dir = ROOT / "plugins" / name
        self.host: FastAPI | None = None
        self.state = STOPPED
        self.error: str | None = None
        self.spec_error: str | None = None

    # -- 调用 --
    def abs_path(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        if path == self.prefix or path.startswith(self.prefix + "/"):
            return path
        return self.prefix + path

    async def request(self, method: str, path: str, *, query: str = "", headers: dict | None = None,
                      body: bytes = b"", timeout: float = 15.0) -> dict:
        target = self.host or self.app
        return await AsgiClient(target).request(method, self.abs_path(path), query=query,
                                                headers=headers, body=body, timeout=timeout)

    # -- 路由发现 --
    def spec(self) -> dict:
        try:
            self.app.openapi_schema = None  # 模块可能在 init 里注册路由，schema 必须重算
            schema = self.app.openapi()
            self.spec_error = None
            return schema
        except Exception as exc:
            self.spec_error = f"{type(exc).__name__}: {exc}"
            return {}

    def _route_paths(self) -> list[str]:
        out = []
        for route in getattr(self.app, "routes", []):
            path = getattr(route, "path", None)
            methods = getattr(route, "methods", None) or set()
            if path and "GET" in methods and path not in SKIP_ROUTES:
                out.append(path)
        return out

    def endpoints(self) -> list[dict]:
        spec = self.spec()
        components = spec.get("components", {}).get("schemas", {}) or {}
        out: list[dict] = []
        for path, ops in (spec.get("paths") or {}).items():
            for method, op in ops.items():
                if method.upper() in ("HEAD", "OPTIONS", "TRACE"):
                    continue
                params = []
                for p in op.get("parameters") or []:
                    schema = p.get("schema") or {}
                    params.append({"name": p.get("name"), "in": p.get("in"), "required": bool(p.get("required")),
                                   "type": _schema_type(schema), "example": _example(schema, components)})
                body = None
                rb = op.get("requestBody") or {}
                content = rb.get("content") or {}
                for media, media_obj in content.items():
                    schema = media_obj.get("schema") or {}
                    body = {"media": media, "type": _schema_type(schema), "example": _example(schema, components)}
                    break
                out.append({"method": method.upper(), "path": path, "summary": op.get("summary") or "",
                            "tags": op.get("tags") or [], "params": params, "body": body,
                            "responses": sorted((op.get("responses") or {}).keys())})
        known = {(e["method"], e["path"]) for e in out}
        for route in getattr(self.app, "routes", []):
            path = getattr(route, "path", None)
            methods = getattr(route, "methods", None) or set()
            if not path or path in SKIP_ROUTES:
                continue
            for method in methods:
                if method in ("HEAD", "OPTIONS"):
                    continue
                if (method, path) not in known:
                    known.add((method, path))
                    out.append({"method": method, "path": path, "summary": "", "tags": [], "params": [],
                                "body": None, "responses": [], "note": "规范外路由"})
        out.sort(key=lambda e: (e["path"], e["method"]))
        return out

    # -- 页面发现（按契约规则自动取） --
    def _guessed_routes(self) -> dict[str, str]:
        """模块目录下的 *.html → 候选路由：index.html→/ ，card.html→/card ，其余→/<stem>。"""
        guesses: dict[str, str] = {}
        if not self.dir.is_dir():
            return guesses
        for f in sorted(self.dir.glob("*.html")):
            stem = f.stem.lower()
            if stem == "index":
                guesses["/"] = f.name
            elif stem == "card":
                guesses["/card"] = f.name
            else:
                guesses[f"/{stem}"] = f.name
        return guesses

    async def pages(self) -> list[dict]:
        route_paths = self._route_paths()
        guesses = self._guessed_routes()
        candidates: list[tuple[str, str]] = []
        for path in route_paths:
            if path in guesses:
                candidates.append((path, guesses[path]))
        for path in route_paths:  # 规则外页面：无参 GET 路由，探测 content-type
            if path in guesses or "{" in path or path in SKIP_ROUTES:
                continue
            candidates.append((path, ""))
        found: list[dict] = []
        for path, filename in candidates:
            res = await self.request("GET", path, timeout=5.0)
            ctype = res.get("headers", {}).get("content-type", "")
            if res["status"] != 200 or "text/html" not in ctype:
                continue
            source = res["body"][:MAX_SOURCE]
            title = ""
            m = re.search(r"<title[^>]*>(.*?)</title>", source, re.S | re.I)
            if m:
                title = re.sub(r"\s+", " ", m.group(1)).strip()
            found.append({
                "route": path,
                "url": self.abs_path(path),
                "file": filename,
                "title": title,
                "kind": "card" if path == "/card" else ("page" if path == "/" else "extra"),
                "bytes": res["bytes"],
                "content_type": ctype,
                "lint": lint_source(source),
            })
        found.sort(key=lambda p: ({"card": 0, "page": 1, "extra": 2}[p["kind"]], p["route"]))
        return found

    # -- 契约自检 --
    def contract(self) -> list[dict]:
        checks: list[dict] = []
        app = getattr(self.module, "app", None)
        checks.append({"rule": "顶层暴露 app（FastAPI 实例）", "ok": isinstance(app, FastAPI),
                       "hint": "" if isinstance(app, FastAPI) else "app 必须是 FastAPI()"})
        for fn_name in ("init", "shutdown"):
            fn = getattr(self.module, fn_name, None)
            ok = callable(fn) and inspect.iscoroutinefunction(fn)
            checks.append({"rule": f"{fn_name} 为 async 可调用", "ok": ok,
                           "hint": "" if ok else f"{fn_name} 必须是 async 函数"})
        allowed = {"app", "init", "shutdown"}
        extra = sorted(n for n, v in vars(self.module).items()
                       if not n.startswith("_") and n not in allowed
                       and getattr(v, "__module__", "") == self.module.__name__)
        checks.append({"rule": "顶层仅 app/init/shutdown 三个符号", "ok": not extra,
                       "hint": ("多余公开符号：" + "、".join(extra)) if extra else ""})
        return checks

    # -- 生命周期 --
    async def start(self) -> None:
        if self.state in (RUNNING, STARTING):
            return
        self.state = STARTING
        try:
            await self.module.init()
            self.state = RUNNING
            self.error = None
        except Exception:
            self.state = ERROR
            self.error = traceback.format_exc()
            # Match the host's failed-init cleanup contract; shutdown must be safe
            # after partial initialization, and may itself clarify the failure.
            try:
                await self.module.shutdown()
            except Exception:
                self.error += "\\nshutdown cleanup failed:\\n" + traceback.format_exc()

    async def stop(self) -> None:
        if self.state == STOPPED:
            return
        try:
            await self.module.shutdown()
        except Exception:
            self.state = ERROR
            self.error = traceback.format_exc()
            return
        self.state = STOPPED

    async def idempotency(self) -> dict:
        """按契约跑一遍 init×2 → shutdown×2，检查是否可重复调用。"""
        steps = []
        for label, fn in (("init #1", self.module.init), ("init #2", self.module.init),
                          ("shutdown #1", self.module.shutdown), ("shutdown #2", self.module.shutdown)):
            started = time.perf_counter()
            try:
                await fn()
                steps.append({"step": label, "ok": True,
                              "ms": round((time.perf_counter() - started) * 1000, 1)})
            except Exception as exc:
                steps.append({"step": label, "ok": False,
                              "ms": round((time.perf_counter() - started) * 1000, 1),
                              "error": f"{type(exc).__name__}: {exc}"})
        failed = [s for s in steps if not s["ok"]]
        if failed:
            self.state = ERROR
            self.error = "\n".join(f"{s['step']}: {s['error']}" for s in failed)
        else:
            self.state = STOPPED
            self.error = None
        return {"ok": not failed, "steps": steps}


# ---------------------------------------------------------------- 应用装配
def build_app(name: str) -> FastAPI:
    """装配单模块自测应用：控制台 / + 模块 /<名字> + 控制台 API /__test。"""
    module = importlib.import_module(f"plugins.{name}.module")
    missing = [s for s in ("app", "init", "shutdown") if not hasattr(module, s)]
    if missing:
        raise AttributeError(f"模块 {name} 缺少契约符号：{'、'.join(missing)}")
    tester = SelfTest(name, module)

    @contextlib.asynccontextmanager
    async def lifespan(_):
        await tester.start()
        try:
            yield
        finally:
            await tester.stop()

    app = FastAPI(title=f"ToolDock 自测：{name}", lifespan=lifespan)
    tester.host = app

    @app.middleware("http")
    async def no_cache(request, call_next):
        resp = await call_next(request)
        ctype = resp.headers.get("content-type", "")
        if any(t in ctype for t in ("text/html", "text/css", "javascript")):
            resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.get(f"{API}/info")
    async def info():
        return {"name": name, "prefix": tester.prefix, "state": tester.state, "error": tester.error,
                "spec_error": tester.spec_error, "contract": tester.contract(),
                "endpoints": tester.endpoints(), "pages": await tester.pages(),
                "files": sorted(f.name for f in tester.dir.glob("*.html")) if tester.dir.is_dir() else []}

    @app.post(f"{API}/call")
    async def call(body: dict = Body(...)):
        method = str(body.get("method") or "GET").upper()
        path = str(body.get("path") or "/")
        query = body.get("query") or {}
        headers = body.get("headers") or {}
        raw = body.get("body")
        data = b"" if raw is None else (raw.encode("utf-8") if isinstance(raw, str) else bytes(raw))
        qs = urlencode({k: v for k, v in query.items() if v not in (None, "")}, doseq=True) \
            if isinstance(query, dict) else str(query)
        hdrs = {str(k): str(v) for k, v in headers.items()} if isinstance(headers, dict) else {}
        if data and not any(k.lower() == "content-type" for k in hdrs):
            hdrs["content-type"] = "application/json"
        # Lifespan may fail or a module may intentionally be shut down. Let users
        # explicitly re-init from the console before endpoint tests.
        result = await tester.request(method, path, query=qs, headers=hdrs, body=data)
        try:
            result["json"] = json.loads(result["body"])
        except Exception:
            result["json"] = None
        return result

    @app.post(f"{API}/lifecycle")
    async def lifecycle(body: dict = Body(...)):
        action = str(body.get("action") or "")
        result = None
        if action == "init":
            await tester.start()
        elif action == "shutdown":
            await tester.stop()
        elif action == "idempotency":
            result = await tester.idempotency()
        else:
            raise HTTPException(400, f"未知动作 {action}")
        return {"state": tester.state, "error": tester.error, "result": result}

    @app.get(f"{API}/source")
    async def source(path: str):
        res = await tester.request("GET", path, timeout=5.0)
        return {"path": path, "status": res["status"],
                "content_type": res.get("headers", {}).get("content-type", ""), "source": res["body"]}

    # Starlette Mount matches the slash-terminated prefix and includes the next path
    # segment in its child scope. Explicit routes avoid the /__test/ui/... nested
    # mount edge case and keep the tiny static UI dependency-free.
    @app.get(f"{API}/ui/{{asset}}")
    async def ui_asset(asset: str):
        if asset not in {"app.js", "style.css"}:
            raise HTTPException(404, "Not Found")
        return FileResponse(UI_DIR / asset)

    app.mount(f"/{name}", module.app, name=name)

    @app.get("/")
    async def console():
        return FileResponse(UI_DIR / "index.html")

    @app.get("/favicon.ico")
    async def favicon():
        return FileResponse(UI_DIR / "favicon.svg", media_type="image/svg+xml")

    return app
