# GUI2API 0.5.0：身份、目录与执行契约

本轮针对 2026-10-25 的实测问题清单更新框架和 DSH 插件。现有 profile、人格、记忆和正在运行的 Host 不变，旧发布包保留。测试与剩余缺口见 [验证记录](https://github.com/daoxiang0520/GUI2API/blob/main/docs/VALIDATION.md)。

## 一个控件，多种绑定

DSH/MCP 的 `gui_discover` 和一次性 CLI 默认返回 `controls`。一条记录包含：

```json
{
  "id": "本会话分组控件 ID",
  "name": "当前标签",
  "role": "Edit",
  "rect": {"x": 100, "y": 200, "width": 300, "height": 30},
  "window": 123456,
  "hwnd": 234567,
  "fingerprint": "哈希",
  "parent_fingerprint": "哈希",
  "path_fingerprint": "哈希",
  "sibling_index": 0,
  "identity_confidence": "medium",
  "same_name_role_count": 2,
  "bindings": [{"ref": "实时引用", "backend": "msaa"}, {"ref": "另一实时引用", "backend": "win32"}],
  "operations": [{"id": "精确操作 ID", "operation": "set", "provider": "msaa"}]
}
```

`automation_id` 可参与身份，但不要求存在。`name` 不参与身份哈希；空名节点按角色、类、AutomationId、父链和同结构兄弟位置区分。边界和 HWND 用于当前观察中的消歧，不单独授权执行。结构位置会受列表重排和布局升级影响，因此 `medium` 不表示跨版本语义已证实。

几何、角色、状态相同，且同名或共享同一编辑 HWND 的跨后端表示可以合并；不同提供者根中的强匹配也可聚合。重叠的匿名容器不会只因边界相同而合并。不确定的重复仍保留，尚未做到所有 QQ 双树或所有框架视图的完全消重。

每个操作保留原始提供者和引用。可调用分组 `id` 加 `method`，也可选择一个 `operations[].id`。分组初次选择优先使用可靠程度较高的绑定；派发后不会切换后端重放。需要精确指定后端时选择对应的操作 ID。

统计改为 `control_count / readable_control_count / writable_control_count / operation_count`。`get` 可能只是标签读取，可写计数包含焦点、选择和输入等非只读操作，不代表所有控件都可编辑文本。目录上限为 4096 个控件；对象成员另有 4096 条上限。原始 `GuiSession.request('discover', ...)` 为旧调用方保留 `layout='operations'` 默认；新调用方指定 `layout='controls'` 可避免输出重复方法列表，DSH/MCP/一次性 CLI 已默认使用它。

## 连续执行与失效

写操作或 GUI 事件使窗口观察变旧。下一次目录调用先刷新该窗口的元数据；原始对象引用、角色和 AutomationId 仍匹配的操作保留 ID。按钮等非编辑对象改名时撤销旧操作，编辑区标签变化允许保留。被删除或替换的控件单独返回 `stale_capability`，其他控件可继续使用。分组在仍有确定操作绑定存活时保持 ID，不依赖临时 MSAA COM 地址完全不变。

这是“窗口级刷新观察，控件级保留/撤销执行绑定”。目前没有完整的子树事件映射，因此不能声称每次仅枚举变化子树。图像、提供者、进程和版本更换仍可使整个来源失效。观察 ID 独立于时间戳，快速刷新不会被误认作同一次观察。

失败或结果不明的操作进入隔离状态，调用方必须先明确重观察实际状态。其他控件的自动刷新不解除隔离；调用方明确 `find/discover(refresh=true)` 后，失败操作获得新 ID，旧操作 ID 仍不可调用。其他控件不因这一失败全部作废。框架不会自动重复写操作。

## invoke 与后置校验

`InvokePattern`、MSAA 默认动作、Qt 默认动作和 DOM `click()` 的真实效果由应用实现。框架能说明派发对象和方法；无法在所有应用中观察内部是否向祖先转发。`internal_recipient_verified=false / ancestor_bubbling=unknown` 会保留，不以 UI 变化冒充内部调用证据。提供者明确报告目标不匹配时，返回 `invoke_target_mismatch`，不重试。

MSAA/Qt/legacy 默认动作会标为 `invoke_reliability='unreliable'`，建议使用实际暴露的 `click_input`。不可靠 `invoke` 没有后置校验时，在派发前返回 `unreliable_invoke`。明确使用 `arguments:{allow_unreliable:true}` 可无校验调用；该标记不提升可靠性。

```json
{"op":"discovery_invoke","args":{
  "capability":"分组控件 ID","method":"invoke",
  "postcondition":{
    "selector":{"name":"表情面板","role":"Pane","exact":true},
    "field":"exists","equals":true,"timeout_ms":1500
  }
}}
```

以上名称是调用示例，必须换成实际观察到的控件；不是已经注册的 QQ 私有功能。

`call` 和 `discovery_invoke` 均支持 GUI 后置条件：`ref` 或 `selector` 二选一，字段为 `value/name/enabled/offscreen/exists`，比较使用类型严格的 `equals`。`timeout_ms=0..5000`，`interval_ms=50..1000`。条件参数和已有引用先验证，再执行一次动作，随后只轮询读取。值校验不接受 `label_only` 或截断值；多候选视为不确定。部分树里的缺失不能通过 `exists=false`。这里的存在指本次提供者观察，不是应用全局功能存在性证明。

轮询超时在读取请求之间检查；单个提供者请求沿用自己的超时，不能把 timeout_ms 解释为严格的整次调用墙钟上限。

校验失败返回 `postcondition_failed`、动作回执和读取次数；不重放操作，不证明因果，也不能撤销已经发生的效果。未提供条件时写操作返回 `suggested_verification`，提示读取哪些字段。

## 查询、恢复与文本

`find/discover` 支持 `exact`、`role`、`enabled`、`onscreen`，`find` 还保留 `offscreen/within_ref/method/backend` 等过滤。非精确 query 支持空格分词的全部词匹配，并优先排列精确名称、ID和前缀。中文没有自动语义分词。紧凑查找也带 `rect`、窗口、指纹、同名计数。零匹配统一为 `not_observed / absence_proven=false`，不能区分尚未实例化、虚拟化、最小化、不暴露或功能不存在。

`window_action({source,action})` 提供：

| action | 行为 |
| --- | --- |
| get | 当前窗口状态 |
| restore | 请求恢复并观察最小化/可见状态 |
| bring_to_front | 请求激活并报告前台状态，Windows 仍可能拒绝 |
| dismiss_popup | 核对前台、线程菜单和拥有者后向标准菜单拥有者发一次 WM_CANCELMODE |
| escape | 核对前台窗口和桌面后发一次 ESC，不聚焦被遮挡编辑器，不开放任意窗口输入 |
| request_accessibility | 对当前窗口及至多 15 个子窗口调用标准 MSAA/UIA 建树入口；预算在调用间检查，单次受 worker 超时约束 |
| read_console | 优先 UIA TextPattern，缺失时用隔离子进程 AttachConsole + ReadConsoleOutputCharacterW 读当前进程的传统控制台可见缓冲区 |

不自动关闭任意其他窗口。自绘弹层可能不响应 WM_CANCELMODE，可由调用方明确请求 `escape`。终端没有可访问 TextPattern 或可附加缓冲区时返回 `console_unavailable`；尚未提供通用 ConPTY 会话回溯、终端写入或所有 VMware 画面对象。

建树请求不会全局设置屏幕阅读器、注入私有协议或重启应用。Chromium 的 `--force-renderer-accessibility` 作为需明确重启的计划返回，不代表当前进程已启用，更不保证所有控件可读。关闭的页面和未实例化列表仍需正常导航/滚动后再登记。

同一窗口存在多个 TextPattern 时，`read_console` 返回候选，要求选择范围后通过 `get(ref)` 读取；不会悄悄读第一个标签页。当前 Terminal 实例已只读验证可获取 TextPattern 文本，但不代表所有终端配置、标签页或完整滚动历史都可读。接口依据：[WM_GETOBJECT](https://learn.microsoft.com/en-us/windows/win32/winauto/wm-getobject)、[AttachConsole](https://learn.microsoft.com/en-us/windows/console/attachconsole)、[ReadConsoleOutputCharacter](https://learn.microsoft.com/en-us/windows/console/readconsoleoutputcharacter)。

`set / set_text` 接受 `value` 或 `text`，不可同时提供。设置值默认不提交；原生控件同时暴露 `press_keys` 时，可明确 `commit:'enter'`，在 setter 成功后核对焦点并只按一次回车。Enter 阶段失败保留 `value_written=true`，不重复 setter。DOM 尚不支持可信 Enter commit。Explorer 等要求键盘编辑事件的控件仍应选 `type_text`，再明确 `press_keys ENTER`。

键名包含 `ESC/ESCAPE`、`TAB`、`DELETE/DEL`、`BACKSPACE/BKSP`、F1–F12、方向键、Home/End/PageUp/PageDown；可组合 CTRL/SHIFT/ALT。除 ESC 等别名外，其中许多键原本已经存在，这次补齐契约说明。所有错误响应都有统一 `next_action`。

## 持久候选与 rebind

```json
{"op":"rebind","args":{"source":"当前来源","node_id":"learning 的节点哈希","method":"set"}}
{"op":"learning_export","args":{"source":"当前来源"}}
{"op":"learning_import","args":{"data":{"format":"appctl.learning-export/1","app":"应用哈希","candidates":{}}}}
```

DSH/MCP 对应 `gui_rebind / gui_learning_export / gui_learning_import`。唯一解析返回 `status='resolved'` 与当前引用；多候选返回 `ambiguous` 和区分字段；未观察到则返回 `not_observed`。不触发探索输入。健康度统计明确 rebind 的次数、成功率、最近解析/成功时间；没有尝试时成功率为空，不猜测可用性。

导入只接受有界、允许字段内的哈希候选，不导入执行引用。跨版本通过稳定应用家族匹配；使用历史 app 哈希及 `allow_migration=true` 只能获得 `migration_candidate`，不自动签发执行引用。结构整体改变时可能没有候选，目前不是自动跨版本语义迁移器。

磁盘保留哈希定位、角色、方法、最后边界、结构和健康度；不保存标签明文、输入值、调用参数、PID/HWND 或会话引用。实时查找和学习查询中的窗口/边界只代表当前来源。`historical_record_authorizes_execution=false`、`business_meaning_verified=false` 和 `causality_verified=false` 保持不变。
