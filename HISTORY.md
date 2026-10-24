# 历史验证记录

本页保留重写 README 之前的探索过程、版本号与历史采样。它不是当前接口的入门指南；请以 [README.md](README.md) 和 [GUI_API.md](GUI_API.md) 为准。

# Windows 应用的文本命令接口

**AppCtl** 已扩展为通用多通道接口：原生 COM UIA、托管 UIA、MSAA、Win32、Chromium CDP。支持自动探测、控件发现、来源绑定的操作、读回验证和事件订阅，提供 CLI、常驻 JSON 会话、Python API 及 MCP 工具。详见 [APPCTL.md](APPCTL.md)，实际软件抽样见 [COVERAGE.md](COVERAGE.md)。

新增**动态 GUI → API**：`python guictl.py session` 连接窗口后，直接分页枚举控件，并为每个控件提供方法和参数 schema，无需逐项注册应用功能。QQ 和 Chrome/Edge 已复用同一 DOM 适配器，微信、网易云、资源管理器、IDA 等使用原始控件树；引导运行时和完整覆盖仍有差异。六个新 MCP 工具和实测范围见 [GUI_API.md](GUI_API.md)。

直接控件包装位于 `gui_controls.py`：`window.one(...).click()`、`textbox.text = ...`、`item.select()`。已在 QQ 实际点击联系人并返回原页面，也验证了普通 Qt 窗口和浏览器页面；CLI 同时支持 `click/set_text/select` 等短操作，不需要注册业务功能。

新增框架无关的**应用内部对象通道**，优先操作 Service、Model、Session。已验证 Python、JavaScript、.NET、Qt 及 COM 对象，支持方法、属性、子对象和事件。使用及真实边界见 [OBJECTS.md](OBJECTS.md)，本轮结果见 [object-validation.json](docs/VALIDATION.md)。

新增通用业务对象探索器：`objects explore` 可建立对象关系图、搜索成员并记录证据和缺口。使用与尚未实现的运行时接入、业务语义探索见 [EXPLORATION.md](EXPLORATION.md)。

新增按软件接入的**命名功能注册层**：`capabilities onboard/register/list/validate/invoke`。用途说明、参数、对象绑定、测试和结果检查可以注册为可查询 API；候选不会自动变成可调用功能。支持 CLI、Python、NDJSON 和三个新增 MCP 工具，见 [CAPABILITIES.md](CAPABILITIES.md)。完整可见界面覆盖和任意软件业务的自动理解尚未实现。

QQ、微信的后续执行通道已取得实测进展：微信搜索框可通过 UIA 读写；QQ 已接通运行中的 Node-API / Electron，可读写搜索框和当前会话的纯文本草稿，并读回 ProseMirror 模型确认。六个命名功能已接入注册表和 CLI/MCP。两者均未开放发送，微信打开会话仍未验证成功。见 [MESSENGERS.md](MESSENGERS.md)。

较早的应用接口实测包括 WPS COM 业务调用、微信和网易云的系统音频对象，见 [APP_OBJECT_RESULTS.md](docs/VALIDATION.md)。

后续已在四个真实应用取得部分原生对象元数据：Qt5 对象/成员关系、MSVC C++ RTTI 类型和虚表观察，接入统一 CLI/MCP。新增通道只支持观察，业务调用仍未接通，详见 [BUSINESS_OBJECT_DISCOVERY.md](BUSINESS_OBJECT_DISCOVERY.md)。

新增 11 个软件/宿主的通用性抽样：Word、Excel、PowerPoint 与 7-Zip 完成实际读写，Windows Shell 完成只读对象链，Chrome/Edge 完成本地页面事件闭环，Gowin 与 VS Code 验证到对象/控件发现。失败、性能范围和未接通能力见 [UNIVERSALITY_RESULTS.md](docs/VALIDATION.md)；不能据此声称所有应用都可自动调用。

```powershell
.\appctl.ps1 schema
.\appctl.ps1 apps
.\appctl.ps1 session
# 让支持 MCP 的 AI 客户端启动这个标准输入输出服务：
python appctl_mcp.py
```

QQ 当前会话的单行纯文本草稿已通过真实写入、模型回读和恢复验证；微信当前覆盖搜索输入。消息发送与微信聊天输入尚未接通。以下保留最初的 WinApp CLI 历史验证记录；最新专项结果见 [MESSENGERS.md](MESSENGERS.md)。

## Windows UIA 无截图操作验证

本目录包含微软 WinApp CLI v0.7.1 便携版，通过 Windows UI Automation (UIA) 读取控件、调用按钮和读写输入框。运行入口是 `winapp.ps1`，无需修改系统 PATH。

## 已验证

`uia-validation.json` 记录了在 Bot 文件夹的资源管理器窗口进行的实际测试：

- 通过 UIA 读取控件树。
- 通过 `InvokePattern` 调用 `refreshButton`。
- 搜索框写入 `winapp-uia-probe` 后读回相同文字。
- 搜索框原值已恢复，并完成读回确认。
- 未调用截图、坐标点击或键盘输入模拟命令。

## 使用

```powershell
# 查看资源管理器窗口，取得当前 hwnd。
.\winapp.ps1 ui list-windows -a explorer --json

# 查看一个目标应用的可操作控件；多个窗口时，改用 -w 指定唯一 hwnd。
.\winapp.ps1 ui inspect -a notepad --interactive --json

# 查看按钮调用和输入框读写命令的实际参数。
.\winapp.ps1 ui invoke --help
.\winapp.ps1 ui set-value --help
.\winapp.ps1 ui get-value --help

# 重跑实际桌面验证：要求 Bot 文件夹的资源管理器窗口已打开、搜索框为空。
.\Test-Uia.ps1
```

从 `inspect` 返回结果取得当前 `selector`，传给 `invoke`、`set-value`、`get-value`。按钮可优先使用唯一 AutomationId；多个控件共用 AutomationId 时，使用各自的语义 selector。窗口号和运行时 selector 可能失效，失败后重新查询，不要盲目重试写入动作。

需要无截图、无坐标操作时，使用 `invoke`、`set-value`、`get-value` 等 UIA 命令；`click`、`send-keys` 属于输入模拟。控件必须暴露相应的 UIA 能力。此版本的 `set-value` 不接受文档中的 `--root`、`--type` 筛选参数，应使用 `inspect` 返回的唯一 selector。

该工具访问的是运行进程所在的桌面。隔离沙箱可能返回空窗口列表；在 Codex 中访问实际桌面时，需要相应的沙箱外执行授权。

`winapp.ps1` 将 CLI 协调文件放在本目录的 `.winapp-ui`，并只对当前调用设置遥测退出环境变量，结束后恢复原来的环境变量值。

## 来源

- 官方发布：https://github.com/microsoft/winappCli/releases/tag/v0.7.1
- UI Automation 文档：https://learn.microsoft.com/en-us/windows/apps/dev-tools/winapp-cli/ui-automation
- 官方 ZIP SHA256：`d925d1e32cdc320b6d271f653fd2cd3c05b5d7558deb20d1b548bead77900fcf`
- 已校验下载文件 SHA256，并确认 `winapp.exe` 的 Microsoft Corporation Authenticode 签名有效。

WinApp CLI 当前为公开预览版。本文的验证结果仅涵盖上述资源管理器控件。

## 当前窗口的只读覆盖抽样

`Measure-UiaCoverage.ps1` 对已打开的六个应用窗口进行深度上限 8 的 UIA 检查。`uia-coverage.json` 保存计数，未保存控件文字或值。

| 当前窗口 | 返回节点数 | 标记为可触发的节点数 |
| --- | ---: | ---: |
| 资源管理器（Bot） | 146 | 75 |
| 计算器 | 54 | 38 |
| QQ | 9 | 0 |
| 微信 | 2 | 0 |
| WPS | 43 | 43 |
| DeepSeek Harness | 15 | 3 |

这些数字只描述当时的窗口状态和检查深度，不代表整个应用的兼容性或覆盖比例。可触发标记也不代表已执行或验证相应动作；本次抽样没有调用操作或写入命令。自绘区域、不同页面、可访问性配置及窗口状态都可能影响返回内容。

UIA 方法返回成功仅能证明相应调用成功；保存、导出等业务结果仍需读取后续状态来确认。
