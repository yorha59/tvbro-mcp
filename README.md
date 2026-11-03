# tvbro-mcp

MCP server (stdio) that exposes the TV Bro Gecko browser
(`com.phlox.tvwebbrowser.gecko`, 2026 GeckoView) running on the living-room
XGIMI projector as protocol-level tools, via **Firefox RDP** (not CDP) tunneled
through adb. All protocol behavior is specified and verified in
[SPEC.md](SPEC.md).

## Transport

```
adb forward tcp:6000 localabstract:com.phlox.tvwebbrowser.gecko/firefox-debugger-socket
```

The forward is ensured automatically at startup. RDP framing on TCP is
`<decimal length>:<JSON>` in both directions.

## Tools

| Tool | Description |
|---|---|
| `tabs_list()` | `[{title, url}]` of open tabs |
| `navigate(url)` | `navigateTo` + wait `readyState == 'complete'` (≤15s), returns `{title, url}` |
| `evaluate(js)` | `evaluateJSAsync`, returns `result.value` JSON-serialized (`{"error": …}` on exception) |
| `read_page(max_chars=3000)` | `{t: title, u: url, x: body innerText}` |
| `click(selector)` | CSS selector, falls back to `[aria-label="…"]` |
| `screenshot()` | `adb exec-out screencap -p` → `~/.hermes/cache/scratch/tvbro_shot_<ts>.png` |
| `go_back()` / `reload()` | `history.back()` / `location.reload()` + actor refresh |

## Usage

```bash
uv run tvbro-mcp            # stdio MCP server
uv run python scripts/test_client.py   # live-device smoke test
```

Requires: Python 3.11 (uv-managed), `mcp` SDK, adb at
`/opt/homebrew/share/android-commandlinetools/platform-tools/adb`, projector
reachable at `192.168.100.126:5555` with TV Bro in the foreground and its
in-app「Web 引擎调试」switch enabled.
