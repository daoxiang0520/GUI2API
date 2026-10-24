# QQ、微信：从运行时接入到可调用 API

2026-10-24，本机实测。新增运行时引导、软件 profile 与真实执行通道，原有 Qt/C++ 元数据快照仍保持只读。

## 本轮实际结果

| 软件 | 已验证的功能 | 验证方式 |
| --- | --- | --- |
| 微信 4.1.15.13 | 搜索输入框读取、替换、清空 | UIA ValuePattern 写入，重新定位并读回；测试结束确认清空 |
| QQ 9.9.29-47354 | 搜索输入框读取、替换、清空 | 连接正在运行的 Electron，经 DOM 原生 value setter 与 input 事件更新，重新读回 |
| 同一 QQ | 当前会话的单行纯文本草稿读取、替换、清空 | 经 Chromium 内部编辑接口更新；检查 ProseMirror 文档模型，清空后再次核对模型 |

本轮没有发送消息，没有截屏、OCR、坐标点击、键鼠输入模拟或修改账号登录状态。所有测试只使用最初为空的输入框，测试字符串均已确认清除。不会通过覆盖已有草稿来完成测试。

**尚未接通：微信打开会话与聊天草稿的可靠操作、两款软件的发送/送达回执、附件与富文本操作。** 微信搜索结果的 `Invoke` 与 `Select` 虽然被控件声明支持，实际未打开会话，这部分没有开放为正式 API。QQ 草稿接口针对当前已打开会话，不提供稳定的全局联系人 ID，也不承诺收件人身份验证。

## 架构改动

```text
命名功能注册表（用途、参数、效果、测试、结果检查）
                    ↓
软件 profile（进程/窗口、字段角色、类名或 CSS、模型类型）
                    ↓
运行时引导与适配器
  微信：检查 Qt 无障碍入口 → UIA 控件对象
  QQ：已有 Node 回调 → Node-API 桥 → Electron → 页面/编辑器对象
                    ↓
同一 appctl.objects/1 → CLI / Python / MCP
```

配置在 [messengers.profiles.json](messengers.profiles.json)。字段操作和引导机制分开：支持现有运行时的新软件可以补 profile、对象提供者配置和功能清单；新框架、私有宿主或新的引导方式仍需实现适配器。不是给任何新软件登记一个名字就能自动操控。

### 微信

原来的 UIA 查询只有 3 个外壳节点。核对当前 `Weixin.dll` 的版本、代码引用、可写数据段及运行时指令后，临时启用当前进程的 Qt 无障碍入口，取得 99 个节点及真实搜索输入框。正常关闭会话后恢复原字节，再次查询回到 3 个节点。

引导仅支持已核对的 4.1.15.13。没有修改系统读屏标志、安装目录或应用文件，也没有向微信注入代码。未知版本拒绝写入；不会轮流修改未经验证的候选地址。

字段赋值采用 ValuePattern，不依赖键盘焦点，也不要求恢复最小化窗口。业务查询的触发另有前台和焦点条件：本轮曾在焦点满足时搜索“文件传输助手”，发现 `search_list` 中的精确结果；后台窗口的焦点和激活请求可能被 Windows 拒绝。已注册 API 的范围是字段赋值，不承诺后台业务搜索完成。结果项激活未通过页面后置条件，因此保留为诊断记录。

### QQ

原来的独立 Node 进程无法加载 `wrapper.node`。本次使用已运行 QQ 的 `QQNT.dll`，在实际 Node-API 回调所在主线程执行桥接代码；没有重启 QQ 或重新初始化账号。

为使后续调用不依赖偶发业务回调，加载一个本地源码编译的小型 Node-API addon，保留一个不阻止应用退出的事件循环 tick。退出会话会停止计时器、删除桥接对象并卸载 Frida 钩子。**addon DLL 按 Node 的缓存机制保留到 QQ 退出**，并非完全无注入方案。

取得 Electron `BrowserWindow` 后，按真实 HWND 绑定其 `webContents`。QQ 当前版本没有直接暴露常见的 `EditorView` 引用，因此草稿通过 Chromium 编辑接口产生正常输入变化，再等待并读取 `ProseMirror.DocViewDesc.node`；没有把改 DOM 外观当成模型写入成功。

选择器、编辑器内部对象和应用版本都可能变化。当前仅验证单行纯文本；检测到图片、提及、文本 marks 等富内容会拒绝整段替换。没有直接调用 QQ 消息服务。

## 已注册的命名 API

| 命名空间 | 功能 |
| --- | --- |
| `wechat` | `search.read`、`search.replace` |
| `qq` | `search.read`、`search.replace`、`draft.read`、`draft.replace` |

清单：[微信](capabilities/wechat-fields.json)、[QQ](capabilities/qq-fields.json)。已注册到本工作区的 `capabilities.local.json`，并执行真实验证。写入清单的测试包含“写入标记、读回、清空、再次读回”，不是仅测试空值写空值。

```powershell
python appctl.py capabilities --pretty list --app qq
python appctl.py capabilities invoke --app qq --capability draft.read
python appctl.py capabilities invoke --app wechat --capability search.read
```

长期操作使用常驻会话，避免每个命令重复连接、引导和准备窗口：

```powershell
python appctl.py capabilities session
```

输入一行 JSON 调用一次。下面的替换要求字段原值为空，结果只表示草稿值已核对，不会发出消息：

```json
{"op":"invoke","args":{"app_id":"qq","capability_id":"draft.replace","arguments":{"text":"API 草稿测试","expected":""}}}
{"op":"invoke","args":{"app_id":"qq","capability_id":"draft.read","arguments":{}}}
{"op":"invoke","args":{"app_id":"qq","capability_id":"draft.replace","arguments":{"text":"","expected":"API 草稿测试"}}}
```

Python 使用相同字段接口：

```python
from messenger_api import MessengerSession

with MessengerSession("qq") as app:
    print(app.status())
    original = app.draft.read()["value"]
    # 会拒绝富草稿；expected 必须与调用时的实际值相等。
    result = app.draft.replace("尚未发送的草稿", expected=original)
```

也可通过原有对象协议访问根对象 `application`，再取得 `search` / `draft` 子对象：

```powershell
python appctl.py objects --config objects.messengers.json session
```

已有 MCP 服务的 `application_capabilities`、`application_capability_describe`、`application_capability_invoke` 直接使用命名注册表，无需再给每个软件新增一套 MCP 工具。原始对象工具需要设置 `APPCTL_OBJECT_CONFIG` 为 `objects.messengers.json`。本轮验证的是项目内 MCP 服务，没有修改 AI 客户端全局配置。

## 执行边界

- 按进程创建时间、程序路径及当前窗口绑定；同一进程的 AppCtl 会话持有进程租约，其他桥会被拒绝，避免跨会话互相还原运行时状态。
- 字段按 profile 重新定位，必须唯一匹配。不永久保存控件树位置或原生内存地址。
- 替换必须提供 `expected`，比较后才写入；结果不符不自动换通道或重试。
- profile 及相关执行代码的摘要进入对象成员元数据。改变绑定配置或执行实现后，旧命名能力需要重新验证。
- 正常结束会还原本会话恢复的最小化状态、微信入口字节及 QQ 计时器/钩子。强制杀掉客户端可能来不及执行完整清理，当前没有崩溃恢复保证。
- 引导和部分控件需要交互桌面，不能据此宣称支持锁屏、任意页面、任意版本或所有框架。首次连接成本与常驻会话内操作成本不同。

## 验证与复现

依赖：[requirements-messengers.txt](requirements-messengers.txt)、[requirements-capabilities.txt](requirements-capabilities.txt)。QQ 小桥还需本机 x64 C 编译器。本机已有 Frida、相关 Python 包和 GCC，本轮未安装依赖。

```powershell
python probe_messenger_runtime.py --stage napi-fixture
python verify_messengers.py --app all
python verify_messenger_integration.py
```

第一条在自建 Node 进程内验证线程绑定、状态变化、事件循环小桥及清理。第二条执行真实输入框往返，仅测试空字段。第三条生成清单并验证工作区注册表、真实 CLI 和 MCP 路径；会操作测试字段，不是只读命令。

证据：[微信字段测试](https://github.com/daoxiang0520/GUI2API/blob/main/docs/VALIDATION.md)、[QQ 字段测试](https://github.com/daoxiang0520/GUI2API/blob/main/docs/VALIDATION.md)、[CLI/MCP 与六个注册功能](https://github.com/daoxiang0520/GUI2API/blob/main/docs/VALIDATION.md)、[Node-API 自建进程测试](https://github.com/daoxiang0520/GUI2API/blob/main/docs/VALIDATION.md)、[微信入口与控件树对照](https://github.com/daoxiang0520/GUI2API/blob/main/docs/VALIDATION.md)、[微信会话打开未通过](https://github.com/daoxiang0520/GUI2API/blob/main/docs/VALIDATION.md)。

[清理核查](https://github.com/daoxiang0520/GUI2API/blob/main/docs/VALIDATION.md)确认：微信入口恢复原值；QQ 计时器停止、Frida agent 分离，未发现遗留桥接全局对象；两款软件的进程身份、窗口及最小化状态保持一致。小型 addon 仍按上述 Node 缓存机制留在进程中。

实现参考：[微信无障碍入口的上游研究](https://github.com/fanyuantaier/wechatauto-replica/blob/main/wechatauto/accessibility.py)、[Node-API](https://nodejs.org/api/n-api.html)、[Frida API](https://frida.re/docs/javascript-api/)、[Qt 可访问性实现](https://github.com/qt/qtbase/blob/5.15/src/gui/accessible/qaccessible.cpp)。上游启发不代替本机验证，没有引入其数据库、凭证提取、OCR 或发送模块。
