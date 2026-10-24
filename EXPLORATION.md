# 业务对象的通用探索

现已增加编排上述流程的[自动探索层](DISCOVERY.md)：从 GUI 来源或配置对象入口生成统一能力目录，加入原生 COM 模型探测、缓存、失效重探和显式状态后置条件。本文的 `explore` 继续作为其底层对象图引擎。

业务对象的探索流程可以复用。当前新增 `explore` 客户端操作，将已有五类对象通道放进同一个探索过程：枚举入口、描述成员、沿返回对象建立关系图、搜索候选能力，并报告每一步的证据和停止原因。探索器不包含微信、QQ、WPS 或播放器的专用对象名。

目前实现的是对象结构探索。它还不能自动取得任意第三方进程的私有对象，也没有实现业务调用链追踪或自动验证任意方法的业务含义。

后续已增加进程外的 Qt5 和 MSVC x64 RTTI 观察，在微信、WPS、QQ、网易云取得部分真实对象元数据，并接入相同 explore 流程。新增通道仍然只支持观察。实测、命令与证据见 [BUSINESS_OBJECT_DISCOVERY.md](BUSINESS_OBJECT_DISCOVERY.md)。

## 已实现

- 同一套探索器用于 Python、JavaScript、.NET、Qt、COM 提供者，以及现有附加对象引用。
- 同一实例的多个入口合并；共享对象和循环引用不会重复探索。
- 默认只枚举元数据，不读取属性值、不调用方法；会启动目标配置指定的提供者，因此配置本身的激活行为仍然有效。
- `read_members` 显式选定可读取的属性名称，适用于每个被探索的对象。属性 getter 可能执行应用代码。
- `call_read_methods=true` 可沿声明为 `effect=read`、签名可用且无需参数的方法探索返回对象。不根据 get、list、search 等方法名猜测只读性质。只读声明也不构成实际行为证明。
- 返回对象继续探索；普通 JSON 数据只记录类型，不解释为对象引用、不保存实际内容。
- 搜索类型名、成员名和签名；结果标记为元数据文本匹配，不直接认证消息发送、音乐搜索等业务含义。
- 对象数、深度、成员数、请求数与时间有上限；时间在请求之间检查，进行中的请求仍受原提供者超时控制。
- 错误和预算停止显式报告。getter 或方法失败后停止本轮探索，不自动重试。
- 保存元数据结构指纹，供后续比较。指纹只对应本次包含的元数据，不代表应用版本兼容保证。

## 使用

仅枚举元数据和搜索：

```powershell
python appctl.py objects --config objects.example.json --pretty explore --target python-demo --query "child echo"
```

允许调用示例提供者声明的无需参数的只读方法，沿其返回的子对象继续探索：

```powershell
python appctl.py objects --config objects.example.json --pretty explore --target python-demo --call-read-methods --read-member label
```

第二个例子会形成 `Counter → child() → Child` 对象关系。不会尝试调用需要参数的 echo、add，也不会执行未声明为只读的方法。

沿 `child()` 探索、未读取属性值的一次实际输出见 [object-exploration-example.json](docs/VALIDATION.md)，来源是本项目创建的 Python 对象。保存的引用仅作证据，不能在新会话复用。

需要继续使用结果中的对象引用时，使用常驻会话：

```powershell
python appctl.py objects --config objects.example.json session
```

逐行发送：

```json
{"op":"explore","args":{"target":"python-demo","query":"child echo","call_read_methods":true}}
{"op":"describe","args":{"ref":"上一条返回的真实子对象 ref"}}
```

也可对 COM attach 已经返回的引用使用 `{"op":"explore","args":{"ref":"真实 ref"}}`，无需再次发现入口。单次 CLI 命令退出后，返回的引用失效。

MCP 提供同样的 `application_object_explore` 工具。提供者协议仍为 `appctl.objects/1`；`explore` 是客户端对 list、describe、选定 get/call 的编排，不要求每种运行时实现新协议。

## 泛化还需要补齐什么

| 阶段 | 可复用的机制 | 本项目当前状态 |
| --- | --- | --- |
| 取得入口 | 已有自动化接口、IPC、调试协议或运行时内的桥 | 已有配置提供者和 COM；新增选定进程的 Qt5/MSVC x64 只读元数据观察，通用业务执行环境接入尚未实现 |
| 找对象和成员 | 运行时元数据、属性描述、对象关系 | 已实现基础探索器；范围限于桥可达对象 |
| 找业务候选 | 名称、类型、签名和文档检索 | 已实现元数据文本检索；语义判断尚未自动验证 |
| 观察业务行为 | 关联用户操作、调用链、参数、返回、事件与状态变化 | 已有基础事件通道；通用调用链追踪和归因尚未实现 |
| 验证并生成业务命令 | 复核前提、后置条件、异步完成、适用版本 | 现有 call 能执行已知方法；自动验证与业务命令生成尚未实现 |

泛化可优先做到运行时级：同一种 COM、Qt、CLR 或 JavaScript 机制服务多个应用。框架反射的范围仍由运行时及应用实际暴露的元数据决定。例如 Qt 的元对象支持查询登记的方法与属性，CDP 的 Runtime 域支持枚举 JavaScript 对象属性和调用函数，但这些机制本身都不保证能从外部取得任意应用的全部业务实例。

依据：[Qt QMetaObject](https://doc.qt.io/qt-6/qmetaobject.html)、[CDP Runtime](https://chromedevtools.github.io/devtools-protocol/tot/Runtime/)。

微信、QQ 和网易云已发现的部分原生对象，仍需补齐业务执行通道。这项工作不能由“对象元数据已经可见”推断为已经完成。四个应用上轮实际调用验证范围见 [APP_OBJECT_RESULTS.md](docs/VALIDATION.md)，新增观察结果见 [BUSINESS_OBJECT_DISCOVERY.md](BUSINESS_OBJECT_DISCOVERY.md)。

## 验证

`python verify_objects.py` 验证五类对象的元数据探索、CLI、MCP，以及原有对象调用功能。新增测试覆盖共享对象、循环、默认不执行 getter、显式遍历、未知副作用、参数限制、失败停止、预算、关键词匹配和真实会话引用。结果保存在 [object-validation.json](docs/VALIDATION.md)。这些测试包含本项目创建的对象，不能替代微信聊天、QQ 消息或网易云播放器的接入验证。
