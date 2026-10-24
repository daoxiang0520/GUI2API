"""Render the aggregate-only app probe report without exposing control text."""
import json
from pathlib import Path

root = Path(__file__).resolve().parent
report = json.loads((root / "app-channel-coverage.json").read_text(encoding="utf-8"))
labels = {"Weixin": "微信", "QQ": "QQ", "explorer": "资源管理器", "cloudmusic": "网易云音乐", "chrome": "Chrome", "ApplicationFrameHost": "计算器 / ApplicationFrameHost", "steamwebhelper": "Steam", "wps": "WPS", "5EClient": "5E"}
lines = ["# 当前窗口的通用通道抽样", "", "使用同一套通道代码，只读检查已打开窗口。深度 9、每通道最多 400 个节点；没有截图、业务按钮点击、文字输入或保存控件文字。普通探测不改变窗口状态。", "", "时间："+report.get("observed_at","未记录"), "", "表格为本轮各通道观测到的最多原始节点数，可能包含标题栏、系统菜单、代理或重复对象；不是独立可用控件数量，也不是兼容率。超时表示该次读取未完成。窗口类说明选中的实际区域，如 Progman 是桌面，CabinetWClass 是文件夹窗口。", "", "| 选中窗口所属应用 | 窗口类 | 原生 COM UIA | 托管 UIA | MSAA | Win32 |", "| --- | --- | ---: | ---: | ---: | ---: |"]
for app in report["apps"]:
    counts = []
    for name in ("uia3", "uia", "msaa", "win32"):
        candidates=[c for c in app["channels"] if c.get("backend")==name]
        successes=[c for c in candidates if c.get("ok") and "quality" in c]
        channel=max(successes,key=lambda c:c["quality"]["nodes"]) if successes else candidates[-1] if candidates else {}
        counts.append(str(channel["quality"]["nodes"]) if channel.get("ok") else "超时" if channel.get("error", {}).get("code") == "timeout" else channel.get("error", {}).get("code", "未检查"))
    lines.append("| " + " | ".join([labels.get(app["app"], app["app"]),app.get("class_name","") or "未知", *counts]) + " |")
visible=root / "app-visible-coverage.json"
if visible.exists():
    shown=json.loads(visible.read_text(encoding="utf-8"))
    lines += ["", "## 尝试展开窗口后的观察", "", "这轮临时请求展开、激活窗口，再恢复原有最小化状态。Windows 可能拒绝前台激活，必须结合验证字段理解结果，不能认为所有窗口已成功切到前台。", "", "| 应用 | COM UIA 最多节点 | MSAA 最多节点 | 已核实非最小化 | 已核实前台 | 原最小化状态恢复 |", "| --- | ---: | ---: | --- | --- | --- |"]
    for app in shown["apps"]:
        def count(backend):
            c=[x["quality"]["nodes"] for x in app["channels"] if x.get("backend")==backend and x.get("ok") and "quality" in x]
            return str(max(c)) if c else "未完成"
        actual=app.get("after_reveal") or app.get("reveal") or {}
        lines.append("| "+" | ".join([labels.get(app["app"],app["app"]),count("uia3"),count("msaa"),"是" if actual.get("minimized") is False else "否 / 未确认","是" if actual.get("foreground") else "否","是" if app.get("minimized_restored") else "原本未最小化" if not app.get("before",{}).get("minimized") else "未确认"])+" |")
lines += ["", "网易云、Steam、WPS 曾通过不同通道获得额外结构；可见状态、桥接懒加载和提供者行为会影响结果。QQ 曾一次返回 386 个节点、2 个声称可写节点，但进程及窗口后来改变，后续未稳定复现；历史观察保存在 [reports/coverage-before-integrity.json](reports/coverage-before-integrity.json)。当前不能宣称 QQ 聊天已打通，微信也尚未发现聊天列表和可写输入框。", "", "QQ 的条件受限搜索验证见 [qq-control-validation.json](qq-control-validation.json)。未找到唯一、明确的空搜索框时不写入；没有操作聊天编辑器或发送消息。", "", "CDP 已通过独立 Chromium 测试配置的实际动作验证；本次现有窗口没有发现可直接接入的现成 CDP 端点，没有修改它们的启动参数。", "", "实际动作、读回、事件和容器下钻使用自有测试窗口与独立浏览器验证，见 [channel-validation.json](channel-validation.json)。整体检查见 [release-validation.json](release-validation.json)，命令说明见 [APPCTL.md](APPCTL.md)。", ""]
(root / "COVERAGE.md").write_text("\n".join(lines), encoding="utf-8")
print("COVERAGE.md updated")
