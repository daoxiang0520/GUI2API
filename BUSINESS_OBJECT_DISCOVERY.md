# 四个真实应用的业务对象探索

后续执行通道更新见 [MESSENGERS.md](MESSENGERS.md)：微信搜索框和 QQ 搜索/当前草稿已实际读写；本文描述的 Qt/C++ 原生快照通道继续保持只读。

2026-10-23，本机实测已取得四个应用正在运行的部分原生对象元数据。新增两套可复用机制：Qt 5 的 QObject / QMetaObject 观察，以及 MSVC x64 的 RTTI / 虚表观察。机制不包含应用专用业务方法名或预先写死的对象地址；本次应用和模块范围明确选定。

本轮完成了对象发现和统一探索接口。新增原生观察通道只能描述对象，尚不能调用它们的业务方法。WPS 先前已验证的 COM 调用通道继续可用。

后续扩展到 11 个本机软件/宿主，验证了 Office、7-Zip 等实际读写，以及 Gowin Qt 对象和 VS Code Electron 控件发现，见 [UNIVERSALITY_RESULTS.md](docs/VALIDATION.md)。这些结果不改变本文四个应用原生观察通道的只读边界。

## 最新观察结果

| 应用 | 本次快照中的对象记录 | 观察到的实际类型示例 | 已取得的信息 |
| --- | ---: | --- | --- |
| 微信 | 127 | `mmui::WeChatApplication`、`mmui::ChatInputField`、`mmui::VoiceInputField` | Qt 类型、继承关系、登记的属性/方法/信号、父子关系；包括界面对象，未据此确认聊天服务接口 |
| WPS | 256 | `KPluginManager`、`KCacheFileManager`、`KFileTransferProxy`、`KPromeCloudSvrProxy` | 同上；例如发现 `getFileContent`、`getFileCache` 等登记方法，未调用 |
| QQ | 128 | `nt::wrapper::NTWrapperEngine`、`nt::wrapper::KernelNodeMiscService`、`nt::MSFService` | 已校验 RTTI 的对象类型及所观察虚表的函数槽位；缺少完整方法名和参数 |
| 网易云音乐 | 128 | `orpheus::player::AudioSourceManager`、`orpheus::storage::LibraryStorage`、`orpheus::IPCDispatcher` | 同上；未验证搜索、播放或切歌调用 |

数量是本次观察记录数，不能当作独立业务功能数或完整覆盖率。WPS、QQ、网易云达到本轮对象上限，统一探索结果保留 `source_object_budget`。微信先前一次读取曾达到 256 条，最后一次快照为 127 条；实例数量会随应用状态变化。

Qt 类型只表示静态元对象视图；动态元对象、普通 C++ 类和不在当前根对象关系中的实例可能遗漏。C++ 记录包括基础设施对象、管理对象及可能的子对象；仅有类名和虚表不能证明它具备某项业务能力。

## 取证方式

1. 枚举四个应用的主进程和已加载模块，解析程序文件的 PE 导出表及安装代码包。
2. 在独立、无窗口的自建 Qt 进程中校验布局，确认能识别 QCoreApplication、QTimer、Qt 事件分发器及原生 C++ RTTI；结束后关闭测试进程。
3. 对微信和 WPS，以 `ReadProcessMemory` 读取导出的应用实例指针或模块全局 QObject 指针，校验对象、自指针、虚表和 Qt 元数据，再沿校验过的父子关系观察。没有执行 metaObject 方法，而是解析相关机器码引用的静态元数据。
4. 对 QQ 和网易云，从所选模块的全局指针出发，校验对象虚表、CompleteObjectLocator、TypeDescriptor 和映像基址，再记录类型及有限的函数槽位。没有调用这些槽位。
5. 将观察结果作为明确标注的快照提供者接入 `appctl.objects/1`，由同一 `explore` 流程遍历和搜索。

本轮没有截图、键鼠输入、代码注入、应用重启或目标应用业务方法调用。没有解析或保存聊天正文、文档内容、账号凭证和业务字段值。原有四个应用的窗口和 PID 均通过复核。报告中的 `native_calls=0` 指目标应用函数调用，不包括用于观察的 Windows 系统 API。

## CLI 与 API

重新取得一轮当前观察：

```powershell
python probe_business_objects.py --stage run
```

这会先校验自建测试进程，再读取选定应用的元数据；不会自动调用发现的业务方法。原生观察使用现有的 pywin32、psutil 和 Capstone，Qt 自检使用现有 PyQt5。本轮未安装依赖。

查询已保存的网易云对象快照：

```powershell
python appctl.py objects --config objects.observations.json --pretty explore --target native-snapshot-demo --query player --max-objects 256 --max-requests 512
```

其它目标为 `native-snapshot-demo`、`native-snapshot-demo`、`native-snapshot-demo`。这些命令读取探测快照，不会重新读取目标进程。需要刷新时先运行探测器。MCP 使用相同配置及现有 `application_object_explore` 工具。

快照中的 `captured_at` 对应来源报告时间，不是对象事件发生时间；同一报告的各个采集阶段可能先后执行。

快照描述明确返回 `observation_only=true`，以及 `native_get/native_set/native_call/native_event_watch=false`。方法上的 `callable=false` 表示尚未接通调用；不会把发现某个方法伪装为已经可用。结构关系直接来自观察元数据，统一探索不必执行属性 getter。

## 验证与证据

- [原始发现记录](https://github.com/daoxiang0520/GUI2API/blob/main/docs/VALIDATION.md)：来源、对象、类型、成员、预算和窗口复核。
- 完整对象图：[微信](https://github.com/daoxiang0520/GUI2API/blob/main/docs/VALIDATION.md)、[WPS](https://github.com/daoxiang0520/GUI2API/blob/main/docs/VALIDATION.md)、[QQ](https://github.com/daoxiang0520/GUI2API/blob/main/docs/VALIDATION.md)、[网易云](https://github.com/daoxiang0520/GUI2API/blob/main/docs/VALIDATION.md)。
- [实际快照的 MCP 验证](https://github.com/daoxiang0520/GUI2API/blob/main/docs/VALIDATION.md)：四个目标均成功，未读取属性值或调用原生方法。沙箱中的异步测试未能启动服务端，同一测试在桌面环境通过；挂起的自建测试进程已关闭。
- [对象桥回归](docs/VALIDATION.md)：67 项测试通过，原有五类执行通道、CLI 和 MCP 25 个工具通过。原生观察测试覆盖关系循环、预算传播、只读边界及代码包读取边界；这些测试不能当作第三方业务调用已验证的证据。

原理依据：[Windows ReadProcessMemory](https://learn.microsoft.com/en-us/windows/win32/api/memoryapi/nf-memoryapi-readprocessmemory)、[Qt 5 QObjectData](https://github.com/qt/qtbase/blob/5.15/src/corelib/kernel/qobject.h)、[Qt 5 QArrayData](https://github.com/qt/qtbase/blob/5.15/src/corelib/tools/qarraydata.h)、[LLVM 对 Microsoft C++ RTTI 布局的实现](https://github.com/llvm/llvm-project/blob/main/clang/lib/CodeGen/MicrosoftCXXABI.cpp)。

调用层还需要确定签名、执行线程、对象生命周期和业务后置条件。当前没有把原生对象的内存地址或虚函数槽位直接开放成任意调用命令。
