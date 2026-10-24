# 验证范围

上传基线为 GUI2API / DSH 插件 **0.5.0**。原工作目录的全量回归结果为：

- Python **283 项**通过。
- 开发版与安装版 DSH SDK 契约测试各 **19 项**通过，注册 24 个 DSH 工具；MCP stdio 验证注册 44 个工具。
- WinForms、Qt 和本地 Chromium DOM 自建对象：结构发现、增量关系、当前引用解析及跨会话候选复用。
- 自建 Windows 窗口：旧目录连续调用、可变标签、通用后置条件、伪造 guard 拦截、单次 Enter、恢复/前台、菜单关闭及隔离控制台读取。
- 已运行应用只读抽查覆盖 Chrome、资源管理器、QQ、微信、Windows Terminal、VMware、Docker Desktop；仅验证当前提供者观察，不证明完整业务覆盖。
- QQ 不可靠默认动作的派发前拦截，以及 Windows Terminal TextPattern 的有界只读调用。

原始本机报告含窗口/进程/路径和应用观察，不在公开仓库分发。用户 profile、人格/记忆、聊天、缓存和凭据均排除。公开测试主要使用合成对象、自建窗口和独立浏览器 profile。

仓库拆分后的验证包括：独立 GUI2API 根目录运行 Python 全量测试；dsh-winbot 从独立或相邻 GUI2API 定位后端、实际 SDK 工具注册与 NDJSON 调用；PowerShell 脚本语法及 Markdown 链接检查。具体本次结果见发布提交附带的 `PUBLICATION.json`。

当前仍存在的限制：控件身份依赖结构、提供者与实例；匿名重复只能保守合并；未实例化页面和虚拟化项目可能不在当前树中；无法通用追踪应用内部事件转发或保证任意业务完成。没有运行 DeepSeek 模型来重新验收所有实际应用任务。
