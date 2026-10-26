# GUI2API

把 Windows 应用当前暴露的界面结构转为可发现、可调用的 API/CLI，供 AI harness 或脚本直接使用。默认通过控件对象定位和操作，无需每一步截图。

当前基线为 **0.5.0**。配套常驻 Bot、人格、记忆及 DSH 插件单独维护在 [dsh-winbot](https://github.com/daoxiang0520/dsh-winbot)。

本地绑定缓存已支持单控件实时核对、缓存 rebind 和基于 ref 的定点后置校验。见 [缓存契约](CACHE.md) 与 [速度测试](docs/CACHE_BENCHMARKS.md)；首次发现与模型等待仍有开销。

新增[多模态资源层](RESOURCES.md)：统一描述图片、音频、视频、文档、文件和文本。先发现元数据，再按需读取源字节或显式渲染内容；返回文件、MIME 与 SHA256。MCP 可交付图片/音频内容块，DSH 可导入宿主图片附件，其他类型保留文件引用。QQ 消息节点与资源内容的当前边界见资源说明。

## 能做什么

- 原生 UIA3 / UIA / MSAA / Win32，多通道发现；Chromium 可连接已有 CDP endpoint 读取 DOM。
- 统一 `get / set / invoke / focus / select / toggle / expand / collapse / scroll`；必要时显式使用 `click_input / type_text / press_keys / scroll_input`。
- 一个控件聚合多种后端方法，保留每次操作的原始绑定。目录包含边界、窗口、父链指纹、唯一性和身份置信度。
- 按名称、角色、方法和子树查询，支持 `exact / enabled / onscreen`；GUI 变化后重新观察，分别保留或撤销控件操作。
- 通用后置校验、窗口恢复、明确 ESC、建树请求和终端读取；不可靠默认动作在未提供校验时拦截。
- 使用过程中逐步登记结构和动作后的观察，支持候选导入/导出、当前引用 rebind 与健康度统计。
- 可选的 Python / JavaScript / .NET / Qt / COM 业务对象桥；元数据可见不代表已接通调用。

未暴露的自绘区域、未实例化页面、虚拟化列表和应用错误实现的接口仍有缺口。控件标签不证明用途，动作返回不代表业务完成，历史关系不证明因果。

## 环境和安装

原生桌面操作需要 Windows 10/11、Python 3.11+ 和已登录、解锁的交互桌面。原生 worker 使用系统 Windows PowerShell；生成的 DLL 和测试程序在本地构建，不纳入仓库。

```powershell
git clone https://github.com/daoxiang0520/GUI2API.git
cd GUI2API
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-gui.txt

# 可选：MCP 服务
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

其他对象运行时和显式 Electron/Qt 桥的依赖见 `requirements-objects.txt`、`requirements-messengers.txt`、`requirements-native-observation.txt`。这些通道按实际框架入口启用，不自动注入每个应用。

## CLI

```powershell
python -X utf8 guictl.py apps
python -X utf8 guictl.py inspect --window 123456 --role Button
python -X utf8 guictl.py discover --window 123456 --query 搜索 --exact --role Edit
python -X utf8 guictl.py schema
python -X utf8 guictl.py session
```

HWND 必须替换成 `apps` 返回的当前窗口。一次性命令退出后引用失效，连续操作请使用常驻 NDJSON 会话：

```json
{"op":"attach","args":{"window":123456}}
{"op":"find","args":{"source":"本会话来源","role":"Edit","method":"type_text"}}
{"op":"call","args":{"ref":"当前控件引用","method":"type_text","arguments":{"text":"hello"}}}
```

协议为 `appctl.gui/1`。DSH/MCP/一次性 discover 默认展示 `controls`，一个控件下挂 `operations`；原始 `GuiSession.request('discover', ...)` 保留兼容的方法布局，新调用方可指定 `layout:'controls'`。

## Python

```python
from gui_controls import GUI

with GUI() as gui:
    window = gui.window(window=123456)
    entry = window.one(role="Edit", method="type_text", backend="uia3")
    entry.type_text("hello", replace=True)
    print(entry.read())
```

若有多个候选，应增加实际观察到的名称、AutomationId 或父节点范围。`type_text` 默认追加；`replace=True` 会先全选。Enter 可能提交消息或表单，需明确请求。输入可以携带 `guard_refs`，在派发前重新核对上下文。

## 执行和可靠性

MSAA/Qt/legacy 默认动作可能无效或由应用转给其他对象，不可靠 `invoke` 默认返回 `unreliable_invoke`。使用实际暴露的 `click_input`、提供明确后置条件，或有意设置 `allow_unreliable:true`；框架不会在失败后自动换方法重放。

```json
{"op":"call","args":{
  "ref":"按钮引用","method":"invoke",
  "postcondition":{
    "selector":{"name":"实际观察到的面板","role":"Pane","exact":true},
    "field":"exists","equals":true,"timeout_ms":1500
  }
}}
```

后置校验只轮询读取。`set` 接受 `value` 或 `text`，设置值默认不提交；`commit:'enter'` 是明确的原生设置后按键操作。`outcome_unknown` 或校验失败后先观察实际状态，不重放。

## 文档和结构

| 入口 | 说明 |
| --- | --- |
| [RELIABILITY.md](RELIABILITY.md) | 0.5.0 身份、目录、恢复、后置校验、迁移和剩余边界 |
| [DISCOVERY.md](DISCOVERY.md) | 控件/业务对象发现及统一目录 |
| [LEARNING.md](LEARNING.md) | 增量关系登记和候选持久化 |
| [GUI_API.md](GUI_API.md) | GUI 通道及历史设计说明 |
| [OBJECTS.md](OBJECTS.md) | 对象桥协议与配置 |
| [docs/VALIDATION.md](docs/VALIDATION.md) | 聚合验证记录和复现范围 |
| `appctl_mcp.py` | MCP stdio 服务，含 GUI 和可选对象工具 |
| `native/` / `bridges/` | 原生 worker 和框架桥源码 |
| `tests/` | 自动测试及自建窗口/网页 fixture |

配置示例使用公开 COM 入口和自建对象。`objects.local.json` 等个人配置、聊天/桌面观察、浏览器 profile、缓存及凭据不纳入仓库。

## 测试

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest tests -q

# 显式执行的自建 GUI 验证，输出写到被忽略的 reports 目录
python tests/smoke_gui_reliability.py reports/owned-validation
python tests/smoke_learning.py reports/learning-validation
python tests/smoke_learning_dom.py reports/dom-validation
```

自建窗口和独立浏览器 profile 测试会操作各自创建的进程；普通自动测试不向真实好友发消息。实时应用探测和手动验收脚本需要操作者明确选择对象与范围。
