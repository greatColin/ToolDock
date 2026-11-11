# ToolDock — Unified Access Platform for Tools

**English** | [简体中文](readme_zh.md)

ToolDock is a unified access platform for personal Python tools. It carries no business logic of its own: every tool plugs in as an ordinary Python package that exposes a FastAPI `app` plus two async lifecycle hooks (`init` / `shutdown`). ToolDock handles everything else — discovery, single-port prefix routing, consistent lifecycle events (`STOPPED / STARTING / RUNNING / ERROR`), and a Mac-style desktop dashboard where each tool's components can be freely composed: duplicated, moved across tabs, and mixed with auto-generated control cards. Compositions persist as a SQLite template and are restored verbatim on every launch. One contract, one port, zero coupling — your tools stay plain Python; the dock does the rest.

Core principles:

1. **Contract-based decoupling** — a plugin is just plain Python that "provides an app + two async events (init/shutdown)". The hub knows nothing about a plugin's internals; a plugin has zero dependency on the hub (no coupling beyond the contract).
2. **Unified lifecycle** — start/stop events for every plugin are dispatched and supervised consistently by the hub; control components, HTTP endpoints and context menus are just triggers.
3. **Free composition upstream** — any plugin component (page / compact view / control card) can be duplicated, spread across tabs, and mixed on the dashboard; the composition is persisted as a template and restored on the next launch.
4. The dashboard is itself a consumer of this decoupling: the Mac-style desktop (icon windows, free grid, edit mode) is merely a presentation layer that consumes the contract, independent of plugin implementation.

## Quick Start

```text
Python 3.10 (Windows example: D:/python/envs/py310/python.exe — see "Structure" in agent.md)
Dependencies: requirements.txt
```

```bash
pip install -r requirements.txt
D:/python/envs/py310/python.exe main.py        # Whole platform: http://127.0.0.1:8500/
```

```bash
# Run/debug a single module (mounted with the same prefix as the platform, so the same path assumptions hold)
D:/python/envs/py310/python.exe run_module.py demo_notes [--port 9900]
```

## Features

- **Unified lifecycle**: `init`/`shutdown` events dispatched by the hub; state machine STOPPED/STARTING/RUNNING/ERROR; buttons mutually disabled during STARTING; ERROR offers retry; Ctrl+C shuts down gracefully in reverse order.
- **Custom plugins**: a module = FastAPI app + two async events; single-port prefix routing (`/<name>/…`, WebSocket passed through natively); pages fully customizable (`/card` compact view optional); a module that fails to load never takes down the host.
- **Free composition**: 24-column free grid (50px cells, cards drag to anchor / resize; span 4×4–24×16); components can be duplicated and mixed across tabs (function and control components together); the composition is persisted as a SQLite template.
- **Control components**: available as soon as a module is initialized, rendered by the host with zero module effort; remote control only, no keep-alive (remoting from the "Module Control" tab does not auto-start modules; clearing the function references stops them).
- **Desktop dashboard**: tab = App (initial badge, double-click to open a window, red light to close); top-right slider toggles edit/browse modes (prevents mis-taps); component library grouped by module (control row + function row).
- **Template persistence (SQLite)**: `tooldock.db` with two tables `tabs(name, idx)` / `placements(id, tab_id, module, col, row, span_w, span_h, type)`; every edit is persisted in full, startup initializes from the template; the legacy `config.json` is migrated on first launch and then deleted.
- **No-cache static assets**: frontend changes take effect immediately, avoiding the "I changed it but nothing happened" browser-cache trap.

## Directory Structure

```text
main.py           # host: discover plugins → prefix mounts → dashboard API → init on startup / graceful shutdown
run_module.py     # standalone runner for a single module
tooldock/registry.py  # registry: module discovery / state machine / SQLite template persistence / SSE events
plugins/<name>/   # one directory per module
  module.py       #   module contract (single entry point)
  *.html          #   module pages (/ = full page, /card = compact card view, optional)
dashboard/        # dashboard (index.html / app.js / style.css, vanilla, no framework)
agent.md          # development constraints and the single source for the contract (required reading: change flow / prohibitions / visual rules)
requirements.txt
```

## Module Contract

A module is `plugins/<dir name>/module.py`; the prefix is the directory name; it exposes exactly three top-level symbols:

```python
app = FastAPI()        # mounted by the host at /<dir name>
async def init()       # idempotent; leftovers from a partial init must be cleanable by shutdown
async def shutdown()   # idempotent, safe to call repeatedly
```

Key points (see agent.md for details; the template modules `plugins/demo_notes` and `plugins/demo_chart` are copy-ready samples):

- Frontend requests always use relative paths; hard-coded ports or absolute URLs are forbidden;
- Page `html/body` backgrounds are transparent so they blend seamlessly into the dashboard window's frosted glass; text/charts must stay readable on their own;
- Handle blocking calls yourself (thread pool / subprocess); never block the host event loop;
- init/shutdown must be idempotent (forced close, reopen, and crash recovery all rely on it).

## Dashboard Cheat Sheet

| Action | How |
|---|---|
| Open a tab window | Double-click the desktop icon |
| Close / maximize / add components | Window title-bar red light / yellow light / green light |
| Place components | Pick or drag from the green-light component library; in edit mode cards can be dragged to anchor and resized at their edges |
| Enter / leave edit mode | Top-right slider (browse mode = components clickable, edit mode = layout only) |
| Start / stop a module | Control card button (works wherever it is placed) or the card context menu |
| Rename / uninstall a tab | Icon context menu (uninstall = remove references; modules with no function references are stopped) |
| Create a tab | "New Tab" desktop icon |

## Development Notes

- Runtime data `tooldock.db` is gitignored; never edit it by hand.
- A module that fails to load never takes down the host: the registry records ERROR and skips it.
- Ctrl+C gracefully shuts down all modules in reverse order.
- If frontend changes do not take effect, hard-refresh first (past lesson, see agent.md).

## Migrating Your Own Tools

1. Move the original service's routes/static pages into `plugins/<name>/` and expose `app/init/shutdown` per the contract.
2. Get it running standalone with `run_module.py <name>` (including the optional `/card` compact view).
3. Drag the function component (and optional control component) from the dashboard component library into a tab — integrated.
