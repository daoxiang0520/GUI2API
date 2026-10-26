# 多模态资源接口

GUI2API 在控件对象之外增加 `appctl.resources/1`：控件负责交互，资源负责携带图片、音频、视频、文档、文件和文本的实际内容。资源通过 `control_ref` 关联原控件，不用从标签文字猜文件内容。本仓库提供新增接口的源码；包版本基线保留 0.5.0，未创建新的发行包。

## 发现、读取与交付

| 操作 | CLI 会话 / Python 请求名 | DSH / MCP 工具名 |
|---|---|---|
| 元数据发现 | `resources` | `gui_resources` |
| 检查来源仍有效 | `resource_describe` | `gui_resource_describe` |
| 读取内容并生成文件 | `resource_read` | `gui_resource_read` |
| 释放引用、显式清理文件 | `resource_release` | `gui_resource_release` |

发现阶段不额外下载、截取图像、播放或录制。扫描结果只增加 `resource_count` 和 `resource_kinds`；资源列表包含类型、MIME、来源控件、可用尺寸/时长，以及提供者声明的读取能力。`availability` 是能力声明，读取前尚未验证。零结果不能证明应用没有资源。

```text
gui_resources({source: "已连接的来源", kind: "audio"})
gui_resources({ref: "当前图片控件的ref"})
gui_resource_describe({resource_ref: "列表返回的resource_ref"})
gui_resource_read({resource_ref: "同一会话的resource_ref", delivery: "reference"})
```

成功读取返回独立 artifact，例如：

```json
{
  "resource_ref": "本会话资源引用",
  "control_ref": "来源控件引用",
  "kind": "audio",
  "artifact": {
    "artifact_id": "生成的文件编号",
    "uri": "gui2api://artifacts/生成的文件编号",
    "path": "本机资源目录中的绝对路径",
    "mime_type": "audio/wav",
    "bytes": 1644,
    "sha256": "实际字节的SHA256",
    "content_complete": true,
    "representation": "source_bytes",
    "original_quality_verified": false
  },
  "model_understanding_verified": false
}
```

字节完整和校验和一致，证明本次传输成功。它们不证明文件是应用保存的最高质量原件，不证明完整解码成功，也不证明模型已理解内容。`format_verification` 的签名检测与完整解码分开记录。

## 来源与覆盖

| 来源 | 当前能发现/读取的资源 | 边界 |
|---|---|---|
| Chromium DOM / 显式 Electron DOM 桥 | `img`、音视频 URL、poster、字幕、下载链接、嵌入文档、已选择的 File、canvas、SVG、背景图 | 仍受登录状态、CORS、应用资源协议和实际加载状态限制 |
| UIA / MSAA / Win32 | `Image` / `Canvas` 控件的渲染候选 | 控件角色和名称不能提供原文件字节；不自动读取 QQ 缓存或私有业务对象 |
| 其他框架 | 经提供者声明、绑定在控件上的资源 | 尚无统一的私有资源解码器；必须暴露可读来源或接入提供者 |

QQ 的只读 MSAA 观察已确认：“消息列表”祖先下有名称为“图片”的 Image 节点，控件树可以返回这些节点和父链。这项能力来自通用控件枚举，早于资源层；当前尚未自动封装成带发送人、时间和媒体占位的逐条消息列表。合并转发聊天记录卡片、文件消息卡片及语音/视频消息的完整读取尚未验收；未观察到样本不证明应用不支持。

统一的是资源契约和后续处理。它不会让所有框架自动暴露私有媒体文件。QQ 当前通过原生控件树发现的图片，可以成为显式渲染读取的候选；能否读取语音/视频原文件，取决于应用是否提供可读资源来源。只有“语音消息”的文字节点时，不会返回伪造的音频。

DOM 使用应用当前提供的 URL / Blob / File；源 URL、凭据和浏览器内部句柄不放进公开资源目录。读取时在原页面上下文中按来源取字节，禁止跟随重定向，不放开浏览器访问规则。`srcObject` 等实时流只报告元数据，明确返回 `stream_unavailable`，不自动录制。

`representation` 必须明确：

- `source`：默认，读取提供者声明的源字节；无法读取就返回缺口，不自动改用截图。
- `rendered`：读取当前显示的画面，返回 `rendered_region`，标记可见范围、裁剪和缩放。音视频控件的渲染画面也只是图片，不是音视频原件。

原生渲染读取使用实时控件矩形，要求目标为可见前台窗口、交互桌面解锁，并检查遮挡、控件身份和读取前后的变化。不会自动激活窗口、移动鼠标或截图定位控件。CDP 在已连接页面中按实时元素边界读取视口画面；目前不推测 iframe 的跨帧几何位置。Electron 无通用渲染捕获器时，canvas 可显式导出为 `canvas_pixels`，按预算缩放并标记 `visible_only:false`，表示画布内容而非当前可见视口。

## Harness 得到什么

`delivery` 决定返回形态，默认为 `reference`，避免每次查询都把大文件塞进上下文。

| 资源 | CLI / Python | MCP `native` | 当前 DSH `native` |
|---|---|---|---|
| 栅格图片 | 文件、MIME、SHA256 | 预算内的 `image` 内容块 + 文件引用 | 可用 attachment 服务导入图片，输出正式 `image` block |
| 音频 | 同上 | 预算内的 `audio` 内容块 + 文件引用 | 文件引用，需支持音频的后续处理器 |
| 视频 / 文档 / 普通文件 | 同上 | `resource_link` | 文件引用，需相应解码器 |
| 文本 | 文件；`text` 交付可返回有界正文 | 文本内容 + 文件引用 | 文本内容 + 文件引用 |

DSH 原生图片要求宿主提供 `attachments.saveImage`，且配置的模型和适配器支持图像输入。插件使用宿主附件契约，不会把 base64 字符串冒充图片。宿主不支持、文件超限或校验失败时，`feedback` 给出限制并保留文件引用。不会替用户修改模型或 profile。

`delivery:"text"` 只把文本资源按 UTF-8 解码，最多返回 64 KiB 并报告截断/解码策略。PDF、音视频不会自动转成正文、字幕或转写。后续可按 MIME 接文档解析、ASR、视频抽帧等处理器；这些处理器尚未包含在本层。

MCP `gui2api://artifacts/{id}` 提供本服务会话拥有的文件，整文件读取上限 8 MiB。大文件可用返回的 `chunk_uri_template` 按字节范围读取，每块最多 1 MiB；传输 MIME 为二进制，实际 MIME 见 artifact。服务退出后 URI 不再有效，已落盘文件按保留策略处理。

## 预算、引用与保留

- 单次读取默认 16 MiB，最多 256 MiB；流式传输默认 20 秒、最多 30 秒。渲染默认 100 万像素，最多 1600 万像素。
- 会话文件配额 256 MiB。MCP 原生内联上限 4 MiB；DSH 另遵守宿主图片预算。
- 资源引用有会话归属，最多登记 2048 条，600 秒后需要重新发现。读取前后复核原控件和来源；可传最多 4 个 `guard_refs` 绑定上下文。不会在失效后换目标重试。
- 原生窗口发生已观察到的变化，会撤销旧资源引用；重新发现当前资源。DOM 重新校验原元素和资源来源。文件内容的业务身份仍需应用提供证据。
- 文件默认落在 `.cache/resources/session-*/`，可用 `GUI2API_RESOURCE_ROOT` 指定目录。文件名由后端生成；失败的未完成传输会关闭并清理，已完成文件保留到显式清理。进程被强杀时可能留下临时文件。
- `resource_release({resource_ref})` 释放实时引用，默认保留文件；`resource_release({artifact_id,delete_artifacts:true})` 删除本会话拥有的指定文件。关闭来源/会话不递归删除已取回文件。
- 资源内容属于不可信应用数据；不会写进仅保存结构哈希的 learning 缓存，也不具有修改人格和权限的授权效力。

## Python 与 CLI

```python
from gui_api import GuiSession

def data(response):
    if not response['ok']:
        raise RuntimeError(response['error'])
    return response['data']

with GuiSession() as gui:
    source = data(gui.request('attach', {'window': 实际HWND}))['source']
    items = data(gui.request('resources', {'source': source, 'kind': 'image'}))['resources']
    # 结合 control_ref 与控件上下文选择目标；不要默认第一张就是聊天图片。
    selected = next(item for item in items if item['control_ref'] == 已确认的控件ref)
    result = data(gui.request('resource_read', {
        'resource_ref': selected['resource_ref'], 'representation': 'rendered'
    }))
    print(result['artifact'])
```

常驻 `python -X utf8 guictl.py session` 接受相同操作名和参数，每行一个 `{id,op,args}`。资源引用和控件引用一样，不能复制到另一个会话执行。

## 验证

`python -X utf8 -m pytest tests/test_gui_resources.py -q` 检查六类资源、大小限制、上下文/来源失效、失败清理、文件完整性及 MCP 交付。

`python -X utf8 tests/smoke_resources.py reports/resources-owned-本次唯一编号` 启动独立无头 Chromium 和本机 HTTP 样例，比较真实 PNG、WAV、WebM、PDF、二进制、文本和已选择 File 的实际字节，再通过真实 MCP stdio 检查内容块、文件 URI 与范围读取。WebM 样例含 10 帧，在浏览器中检查可解码；重新生成样例可选用开发脚本 `tests/fixtures/Build-ResourceVideo.py`（需要 OpenCV，框架运行无需它）。

DSH 的 `test/resources.test.mjs` 检查正式附件引用与 ToolRegistry 输出，同时在开发 SDK 和本机桌面版 SDK 验证。它使用测试附件服务，不调用模型、不修改 profile，也不证明某个已配置模型具备多模态能力。

`tests/smoke_resources_native.py` 使用独立 WinForms 图片控件，显式请求前台后核对输出像素；需要交互桌面和测试用 Pillow。当前本机测试被 Windows 拒绝前台激活，未捕获像素，所以原生 GDI 渲染读取尚未完成实窗验收；不能把浏览器/MCP 通过当成 QQ 图片已读取成功。
