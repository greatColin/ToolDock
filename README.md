# ToolDock — 统一接入口工具平台

> **English** — ToolDock is a unified access platform for personal Python tools. It carries no business logic of its own: every tool plugs in as an ordinary Python package that exposes a FastAPI `app` plus two async lifecycle hooks (`init` / `shutdown`). ToolDock handles everything else — discovery, single-port prefix routing, consistent start/stop events (`STOPPED / STARTING / RUNNING / ERROR`), and a Mac-style desktop dashboard where each tool's components can be freely composed: repeated, moved across tabs, and mixed with auto-generated control cards. Compositions persist as a SQLite template and are restored verbatim on every launch. One contract, one port, zero coupling — your tools stay plain Python; the dock does the rest.

**统一接入口平台**：本身不承担任何业务功能，仅通过一套最小接口（契约）把独立的 Python 工具插件解耦地接入进来——hub 负责发现、生命周期事件（统一启动/关闭）、单端口暴露与组件自由组合，工具本身与 hub 之间只认契约不认实现。

核心主张：
1. **接口解耦**——插件只是"提供 app + 两个异步事件（init/shutdown）"的普通 Python；hub 对插件内部零感知，插件对 hub 零依赖（契约之外无耦合）。
2. **统一生命周期**——所有插件的启停事件由 hub 一致派发与监督；控制组件/接口/右键都只是触发入口。
3. **上游自由组合**——插件的任意组件（页面/紧凑视图/控制卡）在仪表盘上可重复、可跨标签页、可混排；组合结果持久化为模板，下次启动原样初始化。
4. 仪表盘本身也是这套解耦的一个"用户"：Mac 风格桌面（图标开窗、自由网格、编辑模式）只是消费契约的表现层，与插件实现无关。

## 快速开始

```text
Python 3.10（Windows 环境示例：D:/python/envs/py310/python.exe，见 agent.md“结构”）
依赖见 requirements.txt
```

```bash
pip install -r requirements.txt
D:/python/envs/py310/python.exe main.py        # 全平台：http://127.0.0.1:8500/
```

```bash
# 只跑/调试单个模块（与平台相同前缀挂载,暴露相同路径假设）
D:/python/envs/py310/python.exe run_module.py demo_notes [--port 9900]
```

## 功能特性

- **统一生命周期**：`init/shutdown` 事件由 hub 派发；状态机 STOPPED/STARTING/RUNNING/ERROR；STARTING 期间互斥按钮禁用，ERROR 提供重试；Ctrl+C 逆序优雅关闭。
- **插件自定义**：模块 = FastAPI app + 两个异步事件；单端口前缀路由（`/名字/…`，WebSocket 原生透传）；页面自由定制（`/card` 紧凑视图可选），模块加载失败不拖垮宿主。
- **组件自由组合**：24 列自由网格（50px 单元、卡片拖动落锚/缩放、span 4×4~24×16）；组件可重复、可跨标签页混排（功能组件与控制组件并用）；组合结果即 SQLite 模板。
- **控制组件**：模块初始化即拥有，宿主渲染零模块工作量；只遥控不保活（"模块控制"页集中遥控不会引发自启；功能引用清空即停）。
- **桌面仪表盘**：标签页=App（首字母徽章、双击开窗、红灯关窗）；右上滑块切换编辑/浏览双态（防误触）；组件库按模块分组（控制组件行 + 功能组件行）。
- **模板持久化（SQLite）**：`tooldock.db` 两表 `tabs(name,idx)` / `placements(id,tab_id,module,col,row,span_w,span_h,type)`；每次编辑完整落库，启动按模板初始化；首次启动自动迁移遗留 `config.json` 并删除。
- **静态资源 no-cache**：前端改动立即生效，排除"改了没反应"的浏览器缓存坑。

## 目录结构

```text
main.py           # 宿主：发现 plugins → 前缀挂载 → 仪表盘 API → 启动初始化/优雅退出
run_module.py     # 单模块独立运行器
core/registry.py  # 注册表：模块发现/状态机/SQLite 模板持久化/SSE 事件
plugins/<名字>/   # 一个模块一个目录
  module.py       #   模块契约（唯一入口）
  *.html          #   模块页面（/ 为完整页，/card 为紧凑卡视图，可选）
dashboard/        # 仪表盘（index.html / app.js / style.css，纯原生无框架）
agent.md          # 开发约束与契约单一条目（必读：修改流程/禁止项/视觉约定）
requirements.txt
```

## 模块契约

模块 = `plugins/<目录名>/module.py`，前缀即目录名，只暴露三个顶层符号：

```python
app = FastAPI()        # 被宿主挂载到 /<目录名>
async def init()       # 幂等；init 的半途残留须可被 shutdown 清理
async def shutdown()   # 幂等，可重复调用
```

约定要点（详见 agent.md，模板模块 `plugins/demo_notes`、`plugins/demo_chart` 是可拷贝的样板）：
- 前端请求一律相对路径，禁止硬编码端口/绝对 URL；
- 页面 `html/body` 背景透明，与仪表盘窗口毛玻璃底零色差融合；文本/图表自行保证可读；
- 阻塞调用自行处理（线程池/子进程），不得卡宿主事件循环；
- init/shutdown 必须幂等（强制关闭、重开、崩溃恢复都依赖它）。

## 仪表盘交互速览

| 动作 | 方式 |
|---|---|
| 打开标签页窗口 | 双击桌面图标 |
| 关窗/最大化/追加组件 | 窗口标题栏红灯 / 黄灯 / 绿灯 |
| 摆放组件 | 绿灯组件库选/拖；编辑态卡片可拖动落锚、边缘缩放 |
| 进出编辑模式 | 右上滑块（浏览态=可点组件，编辑态=只调布局） |
| 启停某模块 | 控制组件卡按钮（任意摆放处生效）或卡片右键菜单 |
| 重命名/卸载标签页 | 图标右键（卸载=移除引用，无功能引用的模块被停止） |
| 创建标签页 | 桌面“新建标签页”图标 |

## 开发注意事项

- 运行数据 `tooldock.db` 已 gitignore；勿手编。
- 模块加载失败不会拖垮宿主：注册表记录 ERROR 并跳过。
- Ctrl+C 逆序优雅 shutdown 所有模块。
- 改前端不生效时先硬刷新（历史教训，见 agent.md）。

## 迁移你自己的工具

1. 把原服务的路由/静态页搬进 `plugins/<名字>/`，按契约暴露 `app/init/shutdown`；
2. `run_module.py <名字>` 单独跑通（含 `/card` 紧凑视图可选）；
3. 在仪表盘组件库把功能组件（及可选控制组件）拖入标签页即接入。
