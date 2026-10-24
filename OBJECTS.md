# 应用内部对象 → API / CLI

QQ、微信的后续真实执行通道见 [MESSENGERS.md](MESSENGERS.md)：软件 profile、运行时引导及字段适配器复用本对象协议，已验证微信搜索、QQ 搜索与当前会话草稿读写。

对象通道使用 `appctl.objects/1`。同一个客户端表示对象身份、属性、方法、参数、返回对象和事件；对象不依赖窗口句柄、屏幕位置、绘制状态或控件树。优先操作应用的 Service、Model、Session，再用桌面通道补充 UI 能力。

当前已验证 Python、JavaScript、.NET、Qt 四种运行时，以及 COM 对象模型。统一协议可以由任何语言或框架实现，但**自动取得任意未改造应用的私有对象尚未实现**。运行时可反射，也仍需取得正确的实例和执行上下文。

## 立即试用

在本项目目录运行；示例仅创建独立的测试业务对象：

Python 桥和核心协议使用标准库；JavaScript 桥需要本机 Node.js；CLR 启动器使用 Windows 自带的 .NET Framework / Windows PowerShell；COM 和 Qt 的可选依赖列于 `requirements-objects.txt`。本机已有这些依赖，本轮没有安装额外软件。

```powershell
python appctl.py objects schema
python appctl.py objects --config objects.example.json targets
python appctl.py objects --config objects.example.json describe --target qt-demo --object counter
python appctl.py objects --config objects.example.json call --target qt-demo --object counter --member 'add(int)' --arguments '[3]'
```

相同的 `list / describe / get / set / call / watch / events / release` 适用于不同桥接端。实际可用成员、类型和事件以 `describe` 为准。JavaScript 缺少静态类型的参数会明确标为 `unknown`；不会推测成已知类型。

新增 `explore` 客户端编排：跨运行时建立对象关系图、去重共享实例、搜索成员，并记录预算与缺口。默认只读取元数据；选定属性或声明为只读的方法可继续探索返回的对象。私有进程入口和业务含义的自动验证尚未实现，详见 [EXPLORATION.md](EXPLORATION.md)。

一次 CLI 调用结束会关闭它启动的桥接进程，示例状态随之结束。连续操作请用常驻会话、Python API 或 MCP。

## 常驻会话

```powershell
python appctl.py objects --config objects.example.json session
```

逐行发送 JSON。先取根对象引用：

```json
{"id":"1","op":"list","args":{"target":"python-demo"}}
```

从返回结果中选择名称为 `counter` 的对象，将真实 `ref` 填入后续请求：

```json
{"id":"2","op":"describe","args":{"ref":"返回的对象 ref"}}
{"id":"3","op":"watch","args":{"ref":"返回的对象 ref","names":["changed"]}}
{"id":"4","op":"call","args":{"ref":"返回的对象 ref","member":"add","arguments":[3]}}
{"id":"5","op":"get","args":{"ref":"返回的对象 ref","member":"count"}}
{"id":"6","op":"events","args":{"target":"python-demo"}}
```

普通 JSON 返回值使用 `{"kind":"value","value":...}`，业务对象返回值使用 `{"kind":"object","ref":...,"type":...}`。对返回的子对象可继续 describe、读取或调用。参数中的字典即使包含 `kind`、`ref` 字段也保持字面数据，不会成为对象引用。

传递对象参数时，`arguments` 中保留一个位置，再由 `object_arguments` 明确指定引用：

```json
{"op":"call","args":{"ref":"counter 的 ref","member":"same_child","arguments":[null],"object_arguments":{"0":"child() 返回的 ref"}}}
```

对象引用绑定原始提供者和实例，不能跨运行时传递。提供者退出、超时、重新连接或对象释放后拒绝旧引用，不自动迁移到另一个对象。空闲引用的有效期为十分钟。

## 连接现有 Windows COM 对象

```powershell
python appctl.py objects discover --runtime com
```

该命令只读取当前会话 ROT 中最多 128 个已注册对象的类型信息，不读取文档属性或调用业务方法。可能含文件路径的 moniker 名称用散列 ID 代替。没有注册到 ROT 或未提供 IDispatch/ITypeInfo 的应用不会出现在可用对象列表。

在同一个常驻会话里，用真实发现 ID 接入：

```json
{"op":"attach","args":{"runtime":"com","rot_id":"discover 返回的 rot_id"}}
```

也可显式使用已运行对象的 ProgID，例如 `{"op":"attach","args":{"progid":"Excel.Application"}}`。这里使用 GetActiveObject，不会启动 Excel。成功后返回对象引用，可先 describe，再按实际成员操作。

若明确需要创建自动化对象，可使用 `{"op":"attach","args":{"progid":"kwps.Application","activate":true}}`。CLI 为 `attach --progid kwps.Application --activate`。激活可能启动应用；默认 activate 为 false。会话结束释放引用，不自动关闭用户文档或应用。实际 WPS、微信、QQ、网易云音乐结果见 [APP_OBJECT_RESULTS.md](docs/VALIDATION.md)。

COM 桥接支持属性、方法和索引属性。索引属性以 `Item@get`、`Item@put` 等方法表示。当前没有 COM connection-point 事件适配。

COM 元数据包含继承的 ITypeInfo 接口，并区分输入参数与 `[out, retval]` 返回值。显式 by-reference 输出参数的调用封送尚未实现，对应方法返回 `callable=false` 并在执行前拒绝。可用 `--cache-metadata` 为稳定接口开启按对象身份缓存；属性值不缓存，默认关闭元数据缓存。`--system-shell` 显式取得标准 Windows Shell.Application 对象。Office、Shell 和更多软件的实测及配置见 [UNIVERSALITY_RESULTS.md](docs/VALIDATION.md) 与 [objects.universality.json](objects.universality.json)。

## 各桥接端的实际范围

| 桥接端 | 对象入口和发现方式 | 当前边界 |
| --- | --- | --- |
| Python | 显式注册根对象，静态成员发现、方法签名、属性和子对象 | 不扫描其他 Python 进程的私有堆；普通 Python 事件需 Event 适配 |
| JavaScript | 在 Node 进程中加载显式对象工厂，读取属性描述符及原型方法，支持 Promise 和 EventEmitter | 不自动接入任意 Electron 进程；不恢复被擦除的 TypeScript 类型 |
| .NET | 加载指定源码或程序集工厂，通过 Reflection 发现方法、属性和 CLR 事件 | 当前启动器使用 .NET Framework；不能据此声称已附加任意 .NET/Core 进程 |
| Qt | 注册 PyQt5 QObject，实际使用 QMetaObject 属性、公开槽、可调用方法及信号 | 不自动附加微信等原生 Qt 进程，也不发现未注册到元对象系统的任意 C++ 方法 |
| COM | ITypeInfo / IDispatch；可接入现有 ROT 或 GetActiveObject 实例 | 取决于应用公开的自动化对象；事件适配尚未实现 |
| Java / JVM | 协议可由 Java 桥实现 | 本轮未实现 JVM 桥 |
| 原生对象观察 | 进程外只读观察 Qt5 QObject/QMetaObject 或 MSVC x64 RTTI，并以明确标注的快照接入统一协议 | 已在微信、WPS、QQ、网易云发现部分对象；只支持元数据和结构关系，不支持原生属性读写、函数调用或事件钩子，见 [BUSINESS_OBJECT_DISCOVERY.md](BUSINESS_OBJECT_DISCOVERY.md) |
| 原生 C / C++ | 可由插件、明确 ABI 或应用服务桥实现相同协议 | 任意闭源对象及无类型信息函数没有通用的自动调用入口 |

这些桥接端不把控件数量当作能力覆盖率。框架识别、发现对象、知道方法签名、理解业务语义是不同的完成条件。

提供者可在 describe 中返回 `relationships`，表示无需 getter 执行的结构关系。探索器按相同对象/深度预算遍历这些关系，并保留原生观察的来源截断信息。`objects.observations.json` 是只读快照配置，与可执行的 WPS COM 提供者分开。

## 将自己的业务对象接入

Python 可直接嵌入 SDK：

```python
from object_bridge import ObjectHost

ObjectHost({"application": application_service}).serve()
```

只有注册根对象及其返回的对象可达。公开属性会出现在描述中，属性 getter 只在明确读取时执行；默认不暴露私有成员。方法效果默认标为 `unknown`。

独立工厂可以按 `objects.example.json` 配置到自己的 `objects.local.json`。桥接命令始终是参数数组，使用 shell=False，不解释命令拼接。默认不加载示例配置，不修改 AI 客户端全局设置。

Node 工厂导出 `createRoots()`，可额外导出 `events = {对象名: [事件名]}`。CLR 工厂返回 `IDictionary<string, object>`，启动器支持指定 Source 或 Assembly。新语言只需实现同一 NDJSON 请求/应答和 `ping` 握手即可接入客户端。

桥接进程的 stdout 保留给协议。现有 Python print、Node console 和 CLR Console.Out 日志会转向 stderr；提供者直接写原始 stdout 时也必须遵守这一约定。

在已有 GUI 进程嵌入时，应将调用调度到对象所属线程，并保留应用自己的事件循环。当前测试 Qt 桥在创建对象的线程执行请求，并在等待期间泵送 Qt 事件；它不是对外部 GUI 进程的通用注入器。.NET 测试桥在自己的 STA 线程创建和调用对象；现有 WPF/WinForms 对象仍需相应的线程调度集成。

业务对象变成 API 后，方法签名并不自动说明业务前提和副作用。需要应用元数据或适配器提供这些含义。NapCat 一类项目可以作为既有业务能力桥接端接入；本轮没有安装 NapCat、接入 QQ 账号或验证真实 QQ 业务调用。

现可通过 [命名功能注册层](CAPABILITIES.md) 为已接通的对象添加用途、参数、固定绑定、测试和结果检查，再开放命名 API。用途推断、用例通过和业务完成仍分别记录；注册层不会补出尚未接通的原生调用通道。

## MCP 与验证

`appctl_mcp.py` 同时提供桌面和应用对象工具。对象工具使用同一协议，包含发现、COM 接入、对象描述、读写、调用、事件和引用释放。设置服务进程的 `APPCTL_OBJECT_CONFIG` 为配置文件绝对路径即可启用注册提供者。没有配置时仍可查看能力或执行 COM 只读发现。

`call` 的 `returned` 仅表示函数返回。下游消息送达、任务完成等结果需要对应业务状态或事件验证。调用超时、断连和异常不会自动重试；可能已产生副作用时明确报告未知结果。事件只包含对象引用、事件名及序号，不复制正文和参数。事件队列上限 1024，溢出报告 dropped。

运行 `python verify_objects.py` 可重复验证跨运行时对象调用、真实 COM、CLI、MCP 和边界测试。结果保存在 `object-validation.json` 和 `object-mcp-validation.json`。其中 owned fixture 结果不能当作第三方应用兼容性证明。

通用桥验证结果：四种运行时及一个系统 COM 对象模型通过。新增探索器在五类通道的元数据探索已验证，加入命名功能注册后 MCP 提供 28 个工具；最新测试数量见 [object-validation.json](docs/VALIDATION.md)。最初 ROT 探测没有发现可用自动化对象；后续显式激活 WPS 后已取得真实业务对象，并接入微信、网易云的系统音频对象。各通道范围见 [APP_OBJECT_RESULTS.md](docs/VALIDATION.md)。没有修改个人文档或发送聊天消息。

另以自建 COM 对象进行真实跨进程验证：先注册到 ROT，再由独立桥接进程发现、接入、调用并读回，最后撤销临时注册。这个检查验证了外部对象入口，测试对象仍由本项目创建。
