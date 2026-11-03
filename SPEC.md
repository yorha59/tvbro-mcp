# tvbro-mcp — 投影仪浏览器协议操控 MCP Server

## 目标
MCP server（stdio），把客厅极米投影仪上的 TV Bro Gecko 浏览器（com.phlox.tvwebbrowser.gecko，2026 GeckoView 内核）暴露为协议级工具。已实测打通全链路，本 SPEC 中的协议行为全部经过验证，照抄即可，不要另发明。

## 传输链（已验证）
1. 设备：adb 192.168.100.126:5555（Mac 已授权）。adb 路径：`/opt/homebrew/share/android-commandlinetools/platform-tools/adb`
2. 调试口（应用内「Web 引擎调试」开关已打开，重启后仍有效）：
   - 设备端 abstract unix socket：`com.phlox.tvwebbrowser.gecko/firefox-debugger-socket`
   - 转发：`adb forward tcp:6000 localabstract:com.phlox.tvwebbrowser.gecko/firefox-debugger-socket`
   - server 启动时自动确保 forward 存在（`adb forward --list` 检查，无则建；adb device 掉线时报错提示）
3. 协议 = **Firefox RDP**（非 CDP）。TCP 上的分帧格式：`<十进制长度>:<JSON>`，如 `304:{"from":"root",...}`。双向同格式。

## RDP 会话流程（逐条验证过）
1. connect 后**服务端先发** root hello（`{"from":"root","applicationType":"browser","traits":{...}}`），读掉它
2. `{"to":"root","type":"listTabs"}` → `{"tabs":[{"actor":"server0.connN.tabDescriptor1","title":..,"url":..,"selected":false,...}]}`
3. `{"to":"<tabDescriptor>","type":"getTarget"}` → `{"frame":{"actor":"<windowGlobalTarget>","consoleActor":"<consoleActor>","inspectorActor":...,"url":...,"title":...}}`
4. **执行 JS**：`{"to":"<consoleActor>","type":"evaluateJSAsync","text":"<js>"}` → 响应 `{"result":{"type":"string"/"number"/...,"value":...}}`。注意：
   - 必须用 `evaluateJSAsync`（`evaluateJS` 在 windowGlobalTarget 上是 unrecognizedPacketType，会报错）
   - 流里会混杂事件包（如 `{"type":"frameUpdate",...}`），**读包循环必须跳过事件**，等到带 `result` 或 `error` 且 `from` 匹配目标 actor 的包为止
5. **导航**：`{"to":"<windowGlobalTarget>","type":"navigateTo","url":"https://..."}`（响应是 frameUpdate 事件，不代表完成）或 JS `location.href=...`。
   ⚠️ 等待逻辑必须两段式（踩过坑：只等 readyState 会因旧文档已 complete 而秒回旧页面）：
   a. 记住导航前 `location.href` 初始值；发 navigateTo 后先轮询等 **`location.href` 变为目标 url**（容错：忽略末尾斜杠差异、以 startsWith 匹配；若目标与当前 url 相同，则跳过本段）；≤15s，0.5s 间隔，期间 evaluate 失败（actor 换代）静默重试
   b. href 达标后再等 `document.readyState==='complete'`（同样 ≤15s）
   c. 两段都过了才返回 `JSON.stringify({t:document.title,u:location.href})`
6. **导航后 consoleActor 会失效**（实测）：每次 navigate 之后必须重新 listTabs→getTarget 拿新 actor。连接本身不断
7. 长等待：socket 设 timeout，读包循环超时上限 ~15s；JS 执行结果用响应里的 `result.value`

## MCP 工具（FastMCP，Python 3.11，uv 管理）
1. `tabs_list()` → [{title, url}]
2. `navigate(url)` → 等 readyState complete（轮询≤15s，间隔0.5s）→ 返回 {title, url}；导航后内部刷新 actor
3. `evaluate(js)` → 返回 result.value（JSON 序列化；异常时返回 error 字段）
4. `read_page(max_chars=3000)` → evaluateJSAsync 执行 `JSON.stringify({t:document.title,u:location.href,x:document.body.innerText.slice(0,N)})` 解析返回
5. `click(selector)` → evaluate `(document.querySelector(sel)||document.querySelector('[aria-label=\"'+sel+'\"]'))?.click() ?? 'not found'`
6. `screenshot()` → subprocess: `adb -s 192.168.100.126:5555 exec-out screencap -p` 存 `~/.hermes/cache/scratch/tvbro_shot_<ts>.png` 返回路径
7. `go_back()` / `reload()` → JS `history.back()` / `location.reload()` + actor 刷新

## 工程要求
- 单连接管理：模块级 BrowserSession 类，懒连接+断线重连；actor 缓存，报 `no such actor` 类错误时自动重新走 listTabs→getTarget 一次再重试
- `uv init`，依赖 `mcp`（官方 python-sdk 的 FastMCP）；`pyproject.toml`；入口 `src/tvbro_mcp/server.py`
- 一个 `scripts/test_client.py`（非 pytest）：连真实设备跑 tabs_list→navigate example.com→read_page→evaluate 1+1，打印每步 verbatim 输出
- 不要多线程花样，同步阻塞即可（MCP stdio server 单请求串行没问题）

## 自检（写完必须真跑，报告贴 verbatim 输出）
1. `scripts/test_client.py` 全链路通过（投影仪开着、TV Bro 在前台）
2. `uv run python -c "from tvbro_mcp.server import mcp; print('import ok')"`
