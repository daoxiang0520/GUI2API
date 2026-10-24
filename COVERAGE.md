# 当前窗口的通用通道抽样

使用同一套通道代码，只读检查已打开窗口。深度 9、每通道最多 400 个节点；没有截图、业务按钮点击、文字输入或保存控件文字。普通探测不改变窗口状态。

时间：2026-10-23T14:03:17+08:00

表格为本轮各通道观测到的最多原始节点数，可能包含标题栏、系统菜单、代理或重复对象；不是独立可用控件数量，也不是兼容率。超时表示该次读取未完成。窗口类说明选中的实际区域，如 Progman 是桌面，CabinetWClass 是文件夹窗口。

| 选中窗口所属应用 | 窗口类 | 原生 COM UIA | 托管 UIA | MSAA | Win32 |
| --- | --- | ---: | ---: | ---: | ---: |
| 资源管理器 | Progman | 182 | 182 | 281 | 5 |
| Chrome | Chrome_WidgetWin_1 | 126 | 126 | 328 | 3 |
| QQ | Chrome_WidgetWin_1 | 9 | 超时 | 16 | 2 |
| 5E | Chrome_WidgetWin_1 | 1 | 2 | 24 | 2 |
| WPS | OpusApp | 51 | 51 | 214 | 14 |
| Steam | SDL_app | 1 | 1 | 126 | 8 |
| 计算器 / ApplicationFrameHost | ApplicationFrameWindow | 1 | 7 | 70 | 4 |
| 网易云音乐 | OrpheusBrowserHost | 1 | 1 | 58 | 4 |
| 微信 | Qt51514QWindowIcon | 3 | 3 | 24 | 2 |

## 尝试展开窗口后的观察

这轮临时请求展开、激活窗口，再恢复原有最小化状态。Windows 可能拒绝前台激活，必须结合验证字段理解结果，不能认为所有窗口已成功切到前台。

| 应用 | COM UIA 最多节点 | MSAA 最多节点 | 已核实非最小化 | 已核实前台 | 原最小化状态恢复 |
| --- | ---: | ---: | --- | --- | --- |
| 资源管理器 | 182 | 281 | 是 | 否 | 原本未最小化 |
| Chrome | 126 | 397 | 是 | 是 | 是 |
| QQ | 1 | 24 | 否 / 未确认 | 否 | 是 |
| 5E | 1 | 24 | 否 / 未确认 | 否 | 是 |
| WPS | 51 | 214 | 是 | 是 | 是 |
| Steam | 400 | 308 | 是 | 是 | 是 |
| 计算器 / ApplicationFrameHost | 54 | 138 | 是 | 是 | 是 |
| 网易云音乐 | 400 | 273 | 是 | 是 | 是 |
| 微信 | 2 | 24 | 是 | 是 | 是 |

网易云、Steam、WPS 曾通过不同通道获得额外结构；可见状态、桥接懒加载和提供者行为会影响结果。QQ 曾一次返回 386 个节点、2 个声称可写节点，但进程及窗口后来改变，后续未稳定复现；历史观察保存在 [reports/coverage-before-integrity.json](https://github.com/daoxiang0520/GUI2API/blob/main/docs/VALIDATION.md)。当前不能宣称 QQ 聊天已打通，微信也尚未发现聊天列表和可写输入框。

QQ 的条件受限搜索验证见 [qq-control-validation.json](docs/VALIDATION.md)。未找到唯一、明确的空搜索框时不写入；没有操作聊天编辑器或发送消息。

CDP 已通过独立 Chromium 测试配置的实际动作验证；本次现有窗口没有发现可直接接入的现成 CDP 端点，没有修改它们的启动参数。

实际动作、读回、事件和容器下钻使用自有测试窗口与独立浏览器验证，见 [channel-validation.json](docs/VALIDATION.md)。整体检查见 [release-validation.json](docs/VALIDATION.md)，命令说明见 [APPCTL.md](APPCTL.md)。
