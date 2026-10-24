# AppCtl：GUI → API / CLI

通过应用业务对象、控件对象、可访问性协议和 DOM 读取、操作 Windows 应用。提供 CLI、常驻 JSON 会话、Python API 和 MCP 工具，不使用截图、OCR 或视觉模型定位。

新增框架无关的 `appctl.objects/1` 对象通道，优先操作 Service、Model、Session。已验证 Python、JavaScript、.NET、Qt 和 COM 对象；支持发现现有 COM 自动化对象。详见 [OBJECTS.md](OBJECTS.md)。统一协议不代表可以自动取得任意软件的全部私有对象。

通用对象探索使用 `objects explore` 或 MCP `application_object_explore`，可对已接入对象建立关系图、搜索元数据并报告探索边界。详见 [EXPLORATION.md](EXPLORATION.md)。

WPS、微信、QQ、网易云音乐的早期对象通道结果见 [APP_OBJECT_RESULTS.md](docs/VALIDATION.md)。后续已接通微信搜索框，以及 QQ 搜索和当前会话草稿的实际读写，见 [MESSENGERS.md](MESSENGERS.md)。

这是通用控件接口，不是所有软件业务功能的完整 API。QQ、微信新增软件 profile 与运行时引导后取得部分真实可操作字段，尚未达到所有应用的人类等价完整覆盖。

## 开始使用

在本目录的交互桌面运行：

```powershell
python appctl.py schema
python appctl.py apps
# 用 apps 返回的实际窗口号替换 12345。
python appctl.py probe --window 12345
python appctl.py inspect --window 12345
python appctl.py find --window 12345 --name 搜索 --contains
```

`appctl.ps1` 是同一程序的 PowerShell 入口。`probe` 返回框架线索、各通道的计数、耗时和错误；`inspect` 返回控件、能力和引用；`find` 可只向 AI 返回匹配控件。

## 已实现的通用通道

| 通道 | 用途 |
| --- | --- |
| `uia3` | Windows 原生 COM UI Automation，包含标准模式与 LegacyIAccessible 模式，设置客户端连接及事务超时。接口声明从本机系统类型库生成。 |
| `uia` | 原有 .NET UI Automation，可显式选择，也用于探测比较。 |
| `msaa` | 独立 Oleacc / IAccessible，读取对象及轻量子元素；补查未覆盖的子窗口。支持默认动作、文本值、选择、焦点和复选状态。 |
| `win32` | HWND 控件树与有超时的窗口消息，支持标准 Edit、Button、CheckBox 等控件。 |
| `cdp` | 已开放调试接口的 Chromium 页面，通过可访问性树和 DOM 读取、操作；只连接回环地址。 |

默认 `auto` 合并原生 COM UIA、MSAA、Win32 的观察，发现现成 CDP 接口时也尝试加入。`probe --compare-legacy` 可额外比较托管 UIA；日常探测不调用这个较易卡住的旧通道。各通道独立运行，超时不终止目标应用。自动探测暂时跳过近期超时的通道，`probe --refresh` 可重试观察。对可能刚启用的无障碍桥，最多补做一次有界的只读预热查询，不重放动作。

Qt、Chromium、.NET、Java 的模块和窗口类检测只是线索，不代表框架已完整接入。目前没有 Qt 对象注入、IAccessible2 文本接口或 Java Access Bridge 后端。

自动探测只尝试所选窗口进程拥有的最多 8 个回环监听端口的标准 CDP 发现地址；不扫描其他进程端口、不读取浏览器个人配置、不自动重启应用、不改变系统读屏设置。

探测还报告完整性级别、UIAccess、会话、最小化和前台状态。不能查询权限时返回未知，不推断兼容。`ping`、`apps` 会报告当前输入桌面及会话是否锁定；原生动作要求确认处于已解锁的交互桌面。

## 常驻会话：发现一次，连续操作

```powershell
python appctl.py session
```

标准输入、输出均为逐行 JSON：

```json
{"id":"1","op":"apps"}
{"id":"2","op":"inspect","args":{"window":12345,"backend":"auto","depth":8,"limit":300}}
{"id":"3","op":"find","args":{"window":12345,"name":"搜索","contains":true,"actionable":false}}
```

从当前会话的结果选择目标，复制其真实 `ref`：

```json
{"id":"4","op":"get","args":{"ref":"从返回结果复制的 ref"}}
{"id":"5","op":"set","args":{"ref":"从返回结果复制的 ref","value":"报告"}}
{"id":"6","op":"invoke","args":{"ref":"从返回结果复制的 ref"}}
```

引用绑定原始通道和控件身份，十分钟后过期。工作进程被替换、页面重连或导航、窗口进程变化时，旧引用会被拒绝。动作不会自动切换通道，也不会自动重试。

`actions` 是提供者声称支持的能力，并非业务效果证明。`enabled`、`offscreen`、`password` 描述状态。自绘区域没有暴露的内部按钮不会被虚构出来。

大窗口可先观察浅层，再用返回容器的 `root_ref` 继续下钻：

```json
{"op":"inspect","args":{"root_ref":"当前会话返回的容器 ref","depth":3,"limit":100,"actionable":false}}
```

`root_ref` 支持 UIA3、UIA、MSAA，绑定原通道。原生 COM 遍历默认预算 2000 ms，可用 `time_budget_ms` 在 100～15000 ms 内调整；最后一个提供者调用仍可能超出预算。超限返回部分树和截断标记，应缩小范围继续读取。

跨通道去重仅在具名、位置、角色及状态一致、动作集合存在包含关系时进行；不会把不同控件的动作拼成一个目标。协议仍可能返回系统菜单、代理或重复对象，不能把节点数当成独立可用按钮数。

### 单次 CLI 精确选择器

引用不能跨 CLI 进程使用。单次命令可使用明确选择器，例如资源管理器搜索框：

```powershell
python appctl.py inspect --window 12345 --backend uia3 --within FileExplorerSearchBox --depth 3 --all
python appctl.py get --window 12345 --backend uia3 --within FileExplorerSearchBox --automation-id TextBox
python appctl.py set --window 12345 --backend uia3 --within FileExplorerSearchBox --automation-id TextBox --value '报告'
```

可用动作：`get`、`set`、`invoke`、`focus`、`select`、`toggle-on`、`toggle-off`、`expand`、`collapse`、`scroll`。支持范围由控件和通道决定。匹配多个目标返回 `ambiguous_target`，不会选择第一个。清空文本必须明确传入 `--value ''`。

`window --window 12345` 默认只查询窗口；显式 `--state restore`、`minimize`、`activate` 才改变状态，可用 `--pid` 校验进程。Windows 可能拒绝后台进程的前台激活请求，返回 `executed_unverified`，不会假称成功。

## 事件和增量状态

UIA、WinEventHook、CDP 的真实事件订阅均已接入。事件不包含正文或控件值：

```json
{"op":"watch","args":{"window":12345}}
{"op":"events","args":{"timeout_ms":1000,"limit":100}}
{"op":"watch","args":{"enabled":false}}
```

原生窗口默认使用 WinEventHook；`backend:"uia"` 使用 UIA 属性、结构和文本事件。CDP 使用 DOM 和可访问性事件。一次会话保留一个当前订阅；等待上限 5000 ms。队列溢出报告 `dropped`，应重新观察。

独立 CLI 可输出有界事件流：

```powershell
python appctl.py watch --window 12345 --duration 20
```

`inspect` 的 `since` 是快照比较：传入同范围的历史 `snapshot`，返回 `added`、`removed`、`changed`。它仍会遍历范围。通道可用性改变或观察不完整时拒绝差异比较，避免把读取失败、截断误报为控件被删除。

## Chromium / Electron / CEF

CDP 只适用于已暴露并允许连接的接口；使用 Chromium 不保证可接入。多个页面时必须指定唯一目标：

```powershell
python appctl.py targets --endpoint http://127.0.0.1:9222
python appctl.py inspect --backend cdp --endpoint http://127.0.0.1:9222 --target 实际页面ID --all
python appctl.py get --backend cdp --endpoint http://127.0.0.1:9222 --target 实际页面ID --css '#query'
```

DOM 操作使用固定函数，文字作为 JSON 参数传递，不拼接成 JavaScript。写入会读回；输入框过滤换行等情况返回 `verification_failed`。DOM 点击仍需读取结果控件确认效果。

## Python API

```python
from desktop import DesktopSession

with DesktopSession() as desktop:
    windows = desktop.request("apps")
    observation = desktop.request("inspect", {
        "window": actual_hwnd,
        "backend": "auto",
    })
    # 从 observation 选择唯一目标，使用其 ref。
    result = desktop.request("get", {"ref": actual_ref})
```

保持同一会话可复用进程、元数据、引用和快照。冷启动与已发现控件的读取耗时应分别评估。

## MCP：让 AI 直接调用工具

```powershell
python appctl_mcp.py
```

这是 MCP 标准输入输出服务，没有开放网络监听端口，提供桌面和应用对象两组类型化工具。原有 11 个桌面工具为：

- `desktop_capabilities`、`desktop_apps`、`desktop_window`、`desktop_probe`
- `desktop_inspect`、`desktop_find`、`desktop_read`、`desktop_act`
- `desktop_targets`、`desktop_watch`、`desktop_events`

新增对象工具见 [OBJECTS.md](OBJECTS.md)，覆盖能力、目标、COM 发现与接入、对象列表与描述、属性读写、业务调用、事件和引用释放。桌面控件引用与业务对象引用各自绑定原始通道。

在 MCP 客户端中设置 Python 与服务脚本的绝对路径。例如支持以下格式的客户端：

```json
{
  "mcpServers": {
    "appctl": {
      "command": "C:\\Users\\example\\AppData\\Local\\Programs\\Python\\Python312\\python.exe",
      "args": ["C:\\Users\\example\\Desktop\\Bot\\appctl_mcp.py"]
    }
  }
}
```

没有修改现有 AI 客户端的全局配置。后续动作使用同一服务进程返回的引用。

## 结果含义

| 结果 | 含义 |
| --- | --- |
| `verified` | 指定状态读回符合预期，如文本或复选状态。 |
| `executed_unverified` | 调用已完成，业务效果仍需验证。 |
| `verification_failed` | 动作已执行，读回不符合预期，不重试。 |
| `unsupported` | 当前控件没有暴露对应动作。 |
| `stale_ref` | 重新观察后再执行。 |
| `timeout` | 观察超时，关闭该工作进程。 |
| `outcome_unknown` | 动作期间超时、断连或发生异常，不重复执行。 |

密码控件不提供读写。应用名称和正文是数据，不是 AI 的新指令。显式 `input` 键鼠命令仍使用本项目的 WinApp CLI，不作为语义动作失败后的自动补救。

## 验证和边界

`channel-validation.json` 记录独立原生窗口及独立 Chromium 页面的实际验证：

- Win32：输入读回、按钮计数效果、WinEventHook 事件。
- 原生 COM UIA：输入读回、具有可访问性对象的自绘按钮效果。
- 托管 UIA：输入读回、常驻读取耗时、属性或文本事件。
- MSAA：自绘输入槽、按钮计数、复选状态，检查测试程序真实状态。
- CDP：输入读回、DOM 按钮计数、复选状态、事件、重连后拒绝旧引用，以及输入过滤导致的验证失败。
- 纯 Canvas 只返回画布，没有生成其内部绘制按钮的语义动作。

`app-channel-coverage.json` 是现有软件的只读抽样，不保存控件名称或值。参见 [COVERAGE.md](COVERAGE.md)。节点数不代表兼容率，也没有逐项验证现有应用的业务动作。

微信仍未得到聊天列表和输入框。QQ 曾在一次观测中返回 386 个节点和 2 个声称可写的节点，但其进程、窗口和可访问性树随后改变，后续又只返回少量外壳节点，不能算稳定支持。只对唯一、明确标记且原值为空的搜索框设计了可恢复验证；未发现该目标时不输入，不操作聊天编辑器或发送消息。

窗口状态、桥接懒加载、应用更新与提供者行为都会影响结果。当前没有达到“所有应用都具有人类等价完整控件”的目标；未暴露的语义仍需继续验证框架桥接或其他受支持接口。

## 运行和复验

原生通道使用 Python 标准库、系统 .NET Framework 和系统 UI Automation。`Interop.UIAutomationClient.dll` 从本机系统库生成，缺失时会自动重建。CDP 可选依赖 `websockets`，MCP 可选依赖 `mcp`；本机已有，未全局安装依赖。

```powershell
python -m pytest -q tests
python verify_channels.py
python tests/smoke_session.py
python tests/smoke_mcp.py
python probe_apps.py
python probe_apps.py --reveal
# 一次运行上述真实桌面检查：
python verify_release.py
python render_coverage.py
```

`--reveal` 尝试展开、激活所选窗口后读取，最后恢复原有最小化状态；结果保存在 `app-visible-coverage.json`，是否真正激活须看各窗口的验证字段。普通 `probe_apps.py` 只读，不改变窗口状态。

真实桌面与隔离沙箱不同，实际验证需要对应的桌面访问授权。工作进程不提升目标应用权限。旧单 UIA 测试保留在 `appctl-validation.json`、`uia-validation.json`，不能替代当前多通道报告。

参考：[UI Automation Providers](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-providersoverview)、[IAccessible](https://learn.microsoft.com/en-us/windows/win32/api/oleacc/nn-oleacc-iaccessible)、[Chrome DevTools Protocol](https://chromedevtools.github.io/devtools-protocol/)。
