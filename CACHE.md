# 本地绑定缓存与测速

GUI2API 在进程内复用控件快照、目录和原始 provider 引用。缓存命中只选择候选；调用前仍读取目标的实时元数据，写操作不会自动重放。

## 命中路径

- `discovery_invoke` 对确定的 operation 复用原始绑定，核对实时角色、标识、类别、标签和方法。分组控件先选定一个 operation，再验证该 provider，失败后不换后端重放。
- `rebind` 在有界的结构缓存中寻找候选，再检查候选的实时对象。歧义保留为候选列表；跨版本迁移仍不直接给可执行引用。
- 使用 `postcondition: {ref, field: "value", equals: ...}` 时，实时观察该对象并读取实际值。`name`、`enabled`、`offscreen` 也支持单对象读取；不会用缓存值证明动作成功。
- GUI 数据源为原生 UIA/UIA3/MSAA/Win32 和已接通的 CDP/Electron DOM。旧版或不支持单对象读取的 provider 保留完整观察路径。

## 失效与边界

原生事件同时维护一般变化和结构变化的 revision。属性事件仍使一般查询变脏，但相同结构下的确定绑定可经实时检查复用。创建、销毁、显示/隐藏、重排以及未知事件保守地视为结构变化。

结构提示最多复用 30 秒（事件监听可用）或 5 秒（没有可靠监听）。监听丢失、事件覆盖不明确、结构变化、身份/方法变化、版本变化或实时检查失败时，重新扫描。单对象读取不会延长整个窗口快照的有效期，也不会把其他控件标成已更新。

选择器后置条件保持完整观察，以重新判断唯一性；`exists` 条件也保留完整观察。部分控件树中的零匹配仍不是“不存在”的证据。

历史注册和磁盘缓存仍只包含受限的哈希与后端提示，不保存窗口引用、控件值或聊天内容。焦点、遮挡、guard_refs 和发送前检查保持实时执行。

## 推荐调用方式

在一次会话中，首次 `attach` 和 `discover` 后复用返回的控件/operation ID。目标确定时用 `ref` 后置校验；需要重新判断多候选或查找新控件时用 selector/find。不要每一步都强制 `refresh:true`。收到不确定结果时观察实际状态，不能为了命中缓存而重放动作。

`rebind` 返回 `cache_hit` 和 `resolution_observation`；后置校验返回 `read_scope` 与 `cached_value_used:false`。这些字段只描述读取范围，不证明业务完成。

定点 `rebind` 不重新统计全窗口的同名数量，因此不返回旧的同名计数来冒充实时唯一性。候选数量基于有界结构提示与候选的实时检查，`uniqueness_scope` 标明范围。

新建 GUI 会话会加载当前后端代码。已有 Python worker 继续运行其已加载版本；可在会话空闲时使用 `gui_reset` 后重新 attach/discover。更新后端不需要修改 DSH profile；请在现有会话空闲时重新连接。

## 可复现基准

```powershell
python tests/benchmark_gui_cache.py reports/cache-benchmark/uia3 12 uia3
python tests/benchmark_gui_cache.py reports/cache-benchmark/msaa 12 msaa
python tests/benchmark_gui_cache.py reports/cache-benchmark/win32 12 win32
python tests/benchmark_gui_cache_dom.py reports/cache-benchmark/dom 12
```

基准只操作自建测试窗口/本地网页，不发送系统键鼠输入，不截图，不修改 DSH profile。报告包含每次样本、整树扫描次数、定点元数据读取次数（`bound_reads`）和 provider 总请求数，以及 fixture 中实际发生的写入次数。实时 `get` 计入 provider 请求，不计入元数据读取。首轮启动和后续操作分别计时；结果不包含模型推理、网络等待或 DSH 调度时间。

测试失败原样记录。焦点变化只是干扰线索，不能自动把失败归因于用户正在使用电脑。引用失效、provider 限制和缓存实现问题需分别分析。
