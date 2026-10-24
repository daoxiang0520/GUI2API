# 使用时增量登记

`0.4.0` 在 GUI2API 的正常观察和调用中加入 `InteractionLearning`，协议为 `appctl.learning/1`。不要求先遍历整个软件，也不为了学习点击控件、打开页面、输入内容或调用候选业务函数。

`0.5.0` 增加结构身份、控件聚合、`gui_rebind`、候选导入/导出和解析健康度。名称不再参与身份哈希，匿名兄弟节点按结构位置区分。实时结果附边界、窗口/父链指纹，磁盘仅保留哈希定位、最后边界与健康计数。跨版本匹配只产生待复核候选；完整契约见 [RELIABILITY.md](RELIABILITY.md)。

## 登记时机

1. 完成一次窗口观察，登记定位特征、提供者父子关系和结构状态。
2. 参数检查通过、准备执行用户要求的 GUI 动作时，记录目标特征与方法，不保存参数值。
3. 动作返回后保留待观察记录；下一次正常扫描、查找或图查询提供后续观察，再登记关系。
4. 几次动作之间没有新观察时，将它们记录成整段序列，不把变化单独归给最后一个按钮。
5. 同类观察、关系合并计数；后续会话读取历史时，必须重新发现当前对象，返回当前引用及匹配数量。

动作返回时，可访问性树可能尚未更新。首次后续观察若结构未变，会记录 `structure_changed=false`，并保留最多 5 秒的后续观察机会；后续请求的真实观察可补充新状态。框架不会自动轮询、等待或再次执行动作。`awaiting_later_observation` 表示当前是否仍在等待；超出窗口后的变化不能据此认定属于该动作。

只有通过 GUI2API 发出的操作才具有动作记录。人工操作或应用自行更新产生的新结构可以登记，但不会猜测它由哪个按钮触发。当前实现按已附加的来源分别学习，独立弹窗需正常附加后观察，不会仅凭“同进程/刚出现”推断跨窗口因果关系。

## 关系证据

| 关系 | 来源 | 含义 |
|---|---|---|
| `contains` | `provider_tree` | 本次提供者返回的父子结构，映射为定位模式之间的关系 |
| `transition`，单步 | `observed_after_action` | 请求过该动作，随后观察到这个状态 |
| `transition`，多步 | `observed_after_sequence` | 请求过这段操作序列，随后观察到这个状态 |

结构状态使用交互控件的定位模式、出现次数和可见/启用/部分可访问性状态，不将文本框值或全部聊天内容当作状态。相同哈希表示该结构抽象相同，不证明应用内部状态相同。

所有操作观察关系都保留 `causality_verified=false`、`business_completion_proven=false`。`before_state_fresh=false` 表示操作前最近一次观察已超过其新鲜度窗口；仍可保留为弱线索，不能当作已验证前置条件。历史记录不生成“发送成功”“文件已送达”等业务认证。

结果不明或调用失败时，不登记成功状态转移，不重放动作。未经后续观察的操作、缺少前序结构、超出范围或预算的记录会保留计数或标为部分覆盖，不补造关系。

## 查询接口

DSH/MCP 提供 `gui_learning_graph`，共用同一 GUI 会话：

```json
{"source":"本会话已附加来源","query":"表情","relation":"transitions"}
```

常驻 CLI 的对应请求：

```json
{"op":"learning_graph","args":{"source":"本会话来源","relation":"transitions","limit":100}}
```

一次读取也可使用 `python guictl.py learning --window 123456 --query 表情 --relation transitions`，退出后返回的实时引用失效。Python 控件包装提供 `window.learning_graph(query='表情', relation='transitions')`。

查询会沿用 `find` 的缓存、事件失效和有界刷新策略，不执行探索动作。关系可筛选 `all`、`contains`、`transitions`，分页 `limit=1..1000`、`offset=0..20000`。

每个节点是 `identity=locator_pattern`，不是永远有效的应用对象。返回 `live_matches`、`live_match_count` 和 `unique_live_match`，避免把同名/同路径候选自动当作唯一对象。仅当前名称支持子串匹配；历史名称只支持规范化后的精确哈希匹配。名称只从当前界面或调用者提供的查询中获得，不从磁盘恢复。

历史关系没有调用权限：`historical_record_authorizes_execution=false`。实际操作继续使用当前 `source/ref` 与其广告方法，沿用原有身份、状态、焦点和参数检查；没有新增按历史哈希直接执行的接口。

## 持久化与边界

默认文件位于 discovery 缓存同目录，名称为 `<discovery文件主名>.learning.json`。可通过 `GUI2API_LEARNING_CACHE`、CLI `--learning-cache` 或 `GuiSession(learning_cache=...)` 指定，无需修改 DSH profile。

磁盘保存应用指纹、定位哈希、有限角色/方法、最后边界、结构关系、解析健康计数和证据类型。不保存 GUI 标签明文、聊天内容、输入值、调用参数、源引用、PID、HWND 或函数地址。哈希不等同于加密。当前会话内仍保留正常操作所需的可访问性元数据。

原生来源按应用/框架指纹隔离；Python/JavaScript 宿主程序还将可识别的入口脚本文件指纹计入命名空间。DOM 来源加入文档 origin，继续受当前 endpoint/target 范围约束。它们不是应用身份的通用证明，版本或入口变化后可能需要重新学习。

上限：32 个应用指纹，每个 2048 个定位模式、128 个状态、512 条关系；状态至多纳入 512 个交互模式，未观察操作序列至多 8 步；文件最多 2 MiB，超限保留有界数据并报告截断。并发写入合并已知关系；同一关系的观测计数可能偏低，不作为精确审计次数。损坏文件忽略，保存失败不改变 GUI 调用结果，也不会重做动作。

这层提供使用过程中逐步增长的登记，不是应用全部功能的穷尽图。场景/命中测试提供者、通用跨窗口归因、业务参数化流程和完整功能覆盖仍需分别实现。验证见 [本轮记录](https://github.com/daoxiang0520/GUI2API/blob/main/docs/VALIDATION.md)。
