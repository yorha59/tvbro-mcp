"""tvbro-mcp — MCP server (stdio) wrapping the Firefox RDP debugger socket of
TV Bro (com.phlox.tvwebbrowser.gecko, GeckoView) on the living-room projector.

Transport (verified against the live device, see SPEC.md):

    adb forward tcp:6000 localabstract:com.phlox.tvwebbrowser.gecko/firefox-debugger-socket

    Protocol is Firefox RDP (NOT CDP). TCP framing is ``<decimal length>:<JSON>``
    in both directions, e.g. ``304:{"from":"root",...}``.

Session flow (verified step by step, see SPEC.md):

1. On connect the server sends a root hello first — read and drop it.
2. ``{"to":"root","type":"listTabs"}`` → ``{"tabs":[{"actor":..,"title":..,"url":..}]}``
3. ``{"to":"<tabDescriptor>","type":"getTarget"}`` → ``{"frame":{"actor":<windowGlobalTarget>,
   "consoleActor":<consoleActor>, ...}}``
4. JS execution MUST use ``evaluateJSAsync`` on the consoleActor (``evaluateJS`` is
   unrecognizedPacketType). The stream mixes in event packets (frameUpdate, tabNavigated,
   tabListChanged, a bare resultID ack, …) — the read loop skips everything until a packet
   with ``result``/``error``/exception whose ``from`` matches the target actor.
   On this build the result packet is ``{"type":"evaluationResult","result":<raw value>,
   "hasException":bool,...}``; older builds wrap it as ``{"result":{"type":..,"value":..}}``.
   Both shapes are handled.
5. Navigation: ``{"to":"<windowGlobalTarget>","type":"navigateTo","url":..}`` — its response
   is just frameUpdate/tabNavigated events and does NOT mean completion. The wait MUST be
   two-phase (readyState-only polling returns the OLD page — the outgoing document is
   already 'complete'):
     a. remember the pre-navigation ``location.href``; poll until ``location.href``
        becomes the target url (tolerate trailing-slash differences, startsWith match;
        skip this phase when target == current url),
     b. only then poll ``document.readyState === 'complete'``.
   Both phases ≤15s @ 0.5s interval; evaluates that fail mid-swap (actor generations
   changing) are silently retried.
6. The consoleActor is invalidated by every navigation — re-run listTabs→getTarget each
   time. The TCP connection itself stays up.
7. Socket reads use timeouts, read-loop cap ~15s; JS results come from the response
   ``result`` value.

Synchronous and single-connection by design (an MCP stdio server serves requests
serially, no threads needed).
"""

from __future__ import annotations

import json
import re
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# transport constants
# ---------------------------------------------------------------------------
ADB = "/opt/homebrew/share/android-commandlinetools/platform-tools/adb"
ADB_SERIAL = "192.168.100.126:5555"
LOCAL_PORT = 6000
ABSTRACT_SOCKET = "com.phlox.tvwebbrowser.gecko/firefox-debugger-socket"

READ_TIMEOUT = 15.0  # SPEC: 读包循环超时上限 ~15s
NAV_TIMEOUT = 15.0  # SPEC: navigate 轮询 readyState ≤15s
NAV_POLL_INTERVAL = 0.5  # SPEC: 轮询间隔 0.5s
SHOT_DIR = Path.home() / ".hermes" / "cache" / "scratch"

mcp = FastMCP(
    "tvbro-mcp",
    instructions=(
        "Protocol-level control of the TV Bro Gecko browser on the living-room "
        "projector (Firefox RDP over adb). Use tabs_list to inspect tabs, navigate "
        "to load pages, read_page/evaluate to inspect the DOM, click to interact, "
        "screenshot to capture the screen."
    ),
)


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------
class BrowserError(RuntimeError):
    """Transport / protocol failure with a user-presentable message."""


class ActorGone(BrowserError):
    """A 'no such actor'-class error: caller refreshes actors and retries once."""


class JSException(BrowserError):
    """The evaluated JavaScript threw."""


def _is_no_actor_error(text: str) -> bool:
    return re.search(r"no\s*such\s*actor|nosuchactor", text, re.IGNORECASE) is not None


# ---------------------------------------------------------------------------
# browser session
# ---------------------------------------------------------------------------
class BrowserSession:
    """Single RDP connection to TV Bro's GeckoView debugger socket.

    - lazy connect, reconnect on connection loss
    - caches tabDescriptor / windowGlobalTarget / consoleActor
    - navigation invalidates the consoleActor: resolve_actors() re-runs
      listTabs→getTarget (connection stays up)
    - on 'no such actor' errors: refresh actors once, then retry
    """

    _NO_ACTOR_RE = re.compile(r"no\s*such\s*actor|nosuchactor", re.IGNORECASE)

    def __init__(self) -> None:
        self.sock: socket.socket | None = None
        self._buf = b""
        self.tab_descriptor: str | None = None
        self.window_target: str | None = None
        self.console_actor: str | None = None

    # -- adb ----------------------------------------------------------------
    def _adb(self, *args: str, timeout: float = 20.0) -> subprocess.CompletedProcess:
        return subprocess.run([ADB, *args], capture_output=True, text=True, timeout=timeout)

    def ensure_forward(self) -> None:
        """Ensure adb forward tcp:6000 → localabstract socket exists (SPEC)."""
        # no-op when already connected; reconnects the projector otherwise
        self._adb("connect", ADB_SERIAL)
        state = self._adb("-s", ADB_SERIAL, "get-state")
        if state.returncode != 0 or state.stdout.strip() != "device":
            detail = state.stdout.strip() or state.stderr.strip() or "unknown"
            raise BrowserError(
                f"adb device {ADB_SERIAL} offline/unreachable (state: {detail}). "
                "Is the projector on and the Mac still authorized for adb?"
            )
        listing = self._adb("forward", "--list").stdout
        if not any(
            f"tcp:{LOCAL_PORT}" in line and ABSTRACT_SOCKET in line
            for line in listing.splitlines()
        ):
            fwd = self._adb("forward", f"tcp:{LOCAL_PORT}", f"localabstract:{ABSTRACT_SOCKET}")
            if fwd.returncode != 0:
                raise BrowserError(
                    f"adb forward tcp:{LOCAL_PORT} failed: {(fwd.stdout + fwd.stderr).strip()}"
                )

    # -- connection ----------------------------------------------------------
    def connect(self) -> None:
        self.close()
        self.ensure_forward()
        try:
            self.sock = socket.create_connection(("127.0.0.1", LOCAL_PORT), timeout=10)
        except OSError as exc:
            raise BrowserError(
                f"cannot connect to 127.0.0.1:{LOCAL_PORT} (adb forward broken?): {exc}"
            ) from exc
        self._buf = b""
        # SPEC step 1: 服务端先发 root hello（applicationType/traits），读掉它
        try:
            hello = self.read_packet(10.0)
        except (ConnectionError, OSError) as exc:
            raise BrowserError(
                "debugger socket closed immediately — is TV Bro running with the "
                f"「Web 引擎调试」 switch on? ({exc})"
            ) from exc
        if hello.get("from") != "root":
            # tolerate unexpected first packet; framing stays in sync regardless
            print(f"[tvbro-mcp] unexpected first packet: {str(hello)[:200]}", file=sys.stderr)

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self._buf = b""

    def _ensure_session(self) -> None:
        if self.sock is None:
            self.connect()
        if self.console_actor is None:
            self.resolve_actors()

    def _run(self, op: Callable[[], Any]) -> Any:
        """Run op() with lazy session setup; on connection loss reconnect once."""
        self._ensure_session()
        try:
            return op()
        except (ConnectionError, OSError):
            # SPEC: 断线重连
            self.close()
            self.connect()
            self.resolve_actors()
            return op()

    # -- framing: <decimal length>:<JSON> ------------------------------------
    def send(self, pkt: dict) -> None:
        if self.sock is None:
            raise BrowserError("not connected")
        data = json.dumps(pkt, ensure_ascii=False).encode("utf-8")
        self.sock.sendall(str(len(data)).encode() + b":" + data)

    def read_packet(self, timeout: float = READ_TIMEOUT) -> dict:
        """Read exactly one length-prefixed JSON frame (byte-accurate)."""
        if self.sock is None:
            raise BrowserError("not connected")
        deadline = time.monotonic() + timeout
        while True:
            colon = self._buf.find(b":")
            if colon == -1:
                pass  # need more data
            elif not self._buf[:colon].isdigit():
                self._buf = self._buf[colon + 1:]  # resync past garbage (defensive)
                continue
            else:
                length = int(self._buf[:colon])
                end = colon + 1 + length
                if len(self._buf) >= end:
                    payload, self._buf = self._buf[colon + 1:end], self._buf[end:]
                    return json.loads(payload.decode("utf-8", "replace"))
                if length > 64 * 1024 * 1024:
                    self._buf = self._buf[colon + 1:]
                    continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BrowserError(f"timed out after {timeout:.0f}s waiting for an RDP packet")
            self.sock.settimeout(remaining)
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout as exc:
                raise BrowserError(
                    f"timed out after {timeout:.0f}s waiting for an RDP packet"
                ) from exc
            if not chunk:
                raise ConnectionError("debugger socket closed by peer")
            self._buf += chunk

    # -- request/response -----------------------------------------------------
    def request(
        self,
        to: str,
        pkt: dict,
        expect: tuple[str, ...],
        timeout: float = READ_TIMEOUT,
        extra: Callable[[dict], bool] | None = None,
    ) -> dict:
        """Send pkt; skip events; return the first matching response packet.

        A packet is the response when (SPEC step 4):
          - ``from`` == target actor AND one of the ``expect`` keys is present
            (or the ``extra`` predicate accepts it — used for exception-shaped
            evaluationResults), or
          - it is an ``error`` packet (from the target, from root, or without ``from``).
        Everything else (frameUpdate / tabNavigated / tabListChanged / resultID
        acks / bare ``{"from":...}``) is an event and gets skipped.
        """
        self.send(pkt)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BrowserError(
                    f"timed out waiting for response from {to} (type={pkt.get('type')})"
                )
            p = self.read_packet(remaining)
            if "error" in p and p.get("from") in (to, "root", None):
                return p
            if p.get("from") == to and (any(k in p for k in expect) or (extra and extra(p))):
                return p
            # else: event packet → skip

    @staticmethod
    def _error_text(p: dict) -> str:
        return f"{p.get('error', 'unknownError')}: {p.get('message', '')}".strip()

    # -- actors (SPEC steps 2–3, 6) -------------------------------------------
    def list_tabs(self) -> list[dict]:
        p = self.request("root", {"to": "root", "type": "listTabs"}, expect=("tabs",))
        if "error" in p and "tabs" not in p:
            raise BrowserError(self._error_text(p))
        return p.get("tabs", [])

    def resolve_actors(self) -> tuple[str, str, str]:
        """listTabs → getTarget → cache (tabDescriptor, windowGlobalTarget, consoleActor)."""
        tabs = self.list_tabs()
        if not tabs:
            raise BrowserError("TV Bro reports no open tabs")
        tab = next((t for t in tabs if t.get("selected")), tabs[0])
        desc = tab.get("actor")
        p = self.request(desc, {"to": desc, "type": "getTarget"}, expect=("frame",))
        if "error" in p and "frame" not in p:
            text = self._error_text(p)
            if self._NO_ACTOR_RE.search(text):
                raise ActorGone(text)
            raise BrowserError(text)
        frame = p.get("frame") or {}
        self.tab_descriptor = desc
        self.window_target = frame.get("actor")
        self.console_actor = frame.get("consoleActor")
        if not self.window_target or not self.console_actor:
            raise BrowserError(f"getTarget response missing actors: {str(frame)[:300]}")
        return self.tab_descriptor, self.window_target, self.console_actor

    # -- evaluate (SPEC step 4) ------------------------------------------------
    def _eval_once(self, js: str, timeout: float) -> Any:
        if not self.console_actor:
            raise BrowserError("no consoleActor — call resolve_actors() first")
        pkt = self.request(
            self.console_actor,
            {"to": self.console_actor, "type": "evaluateJSAsync", "text": js},
            expect=("result",),
            timeout=timeout,
            extra=lambda p: bool(p.get("hasException")) or "exceptionMessage" in p,
        )
        if "error" in pkt and "result" not in pkt:
            text = self._error_text(pkt)
            if self._NO_ACTOR_RE.search(text):
                raise ActorGone(text)
            raise BrowserError(text)
        if pkt.get("hasException") or pkt.get("exceptionMessage"):
            raise JSException(
                str(pkt.get("exceptionMessage") or pkt.get("errorMessageName") or "JS exception")
            )
        res = pkt.get("result")
        # old-style wrapper {"type":…,"value":…} → unwrap; this build sends the raw value
        if isinstance(res, dict) and "type" in res:
            return res.get("value")
        return res

    def evaluate_js(self, js: str, timeout: float = READ_TIMEOUT) -> Any:
        """evaluateJSAsync with lazy connect, reconnect, and one actor-refresh retry."""

        def op() -> Any:
            try:
                return self._eval_once(js, timeout)
            except ActorGone:
                # SPEC: 报 no such actor 类错误时重新 listTabs→getTarget 一次再重试
                self.resolve_actors()
                return self._eval_once(js, timeout)

        return self._run(op)

    def page_js(self, js: str, timeout: float = READ_TIMEOUT, attempts: int = 3) -> Any:
        """Stale-actor-tolerant evaluate for page ops.

        A consoleActor killed by a navigation goes SILENT (no error packet, only
        a read timeout — verified on the device), so on timeout we force a
        listTabs→getTarget refresh and retry. JS exceptions propagate as-is.
        """
        last: BrowserError | None = None
        for _ in range(attempts):
            try:
                return self.evaluate_js(js, timeout=timeout)
            except JSException:
                raise
            except BrowserError as exc:
                last = exc
                try:
                    self.resolve_actors()
                except BrowserError:
                    pass  # mid-swap; try again
        raise last if last is not None else BrowserError("page_js failed")

    # -- high-level operations (SPEC MCP tools) --------------------------------
    def tabs(self) -> list[dict]:
        def op() -> list[dict]:
            return self.list_tabs()

        return [
            {"title": t.get("title"), "url": t.get("url")} for t in self._run(op)
        ]

    def _poll_js(self, js: str, done: Callable[[Any], bool], what: str, timeout: float) -> Any:
        """Poll a JS expression until done(value) is truthy (SPEC 导航 wait phases).

        ≤timeout at 0.5s intervals; evaluate failures during the poll (actor
        generation swap after a navigation, silently-dead consoleActor) are
        silently retried — SPEC step 5a.
        """
        deadline = time.monotonic() + timeout
        last: Any = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BrowserError(
                    f"timed out after {timeout:.0f}s waiting for {what}"
                    f" (last saw: {str(last)[:120]!r})"
                )
            try:
                # SPEC step 6: consoleActor 失效 after nav → re-resolve every poll;
                # keep the per-attempt timeout short so a silently-dead actor
                # cannot eat the whole poll budget
                self.resolve_actors()
                last = self.evaluate_js(js, timeout=min(4.0, remaining, READ_TIMEOUT))
                if done(last):
                    return last
            except BrowserError:
                pass  # transient while the document swaps actors
            time.sleep(min(NAV_POLL_INTERVAL, max(0.05, deadline - time.monotonic())))

    def navigate(self, url: str, timeout: float = NAV_TIMEOUT) -> dict:
        """navigateTo + TWO-PHASE wait (SPEC step 5), then refresh actors and
        return {title, url}.

        Polling readyState alone returns the OLD page — the outgoing document is
        already 'complete'. So:
          phase a — poll until location.href becomes the target url (tolerate
                    trailing-slash differences, startsWith match; skipped when
                    the target equals the current url),
          phase b — then poll document.readyState === 'complete'.
        Each phase ≤15s @ 0.5s interval; only after both pass does this return
        JSON.stringify({t:document.title,u:location.href}) of the new page.
        """

        def norm(u: Any) -> str:
            return str(u or "").strip().rstrip("/")

        def href_is_target(href: Any) -> bool:
            # SPEC 5a 容错：忽略末尾斜杠差异、以 startsWith 匹配
            return norm(href).startswith(norm(url))

        def op() -> dict:
            self.resolve_actors()  # fresh windowGlobalTarget for navigateTo
            # SPEC 5a: 记住导航前 location.href 初始值（读失败则按未达标处理）
            initial: Any = None
            try:
                initial = self.evaluate_js("location.href", timeout=4.0)
            except BrowserError:
                pass
            # SPEC step 5: response is frameUpdate events only — do not wait for it
            self.send({"to": self.window_target, "type": "navigateTo", "url": url})
            # SPEC 5a: 目标与当前 url 相同则跳过本段
            if not href_is_target(initial):
                self._poll_js(
                    "location.href",
                    href_is_target,
                    f"location.href to become {url}",
                    timeout,
                )
            # SPEC 5b: href 达标后再等 document.readyState==='complete'
            self._poll_js(
                "document.readyState",
                lambda v: v == "complete",
                "document.readyState to become 'complete'",
                timeout,
            )
            # SPEC step 6: navigation done → actors must be re-resolved before use
            self.resolve_actors()
            info = self.page_js('JSON.stringify({t:document.title,u:location.href})')
            try:
                d = json.loads(info) if isinstance(info, str) else {}
            except ValueError:
                d = {}
            return {"title": d.get("t"), "url": d.get("u", url)}

        return self._run(op)

    def read_page(self, max_chars: int = 3000) -> dict:
        n = int(max_chars)
        js = (
            "JSON.stringify({t:document.title,u:location.href,"
            f"x:document.body.innerText.slice(0,{n})}})"
        )
        raw = self.page_js(js)
        try:
            return json.loads(raw) if isinstance(raw, str) else {"raw": raw}
        except ValueError:
            raise BrowserError(f"read_page got an unparseable result: {str(raw)[:200]}")

    def click(self, selector: str) -> Any:
        sel = json.dumps(selector)  # JSON string literal is valid JS
        js = (
            f"(document.querySelector({sel})"
            f"||document.querySelector('[aria-label=\"'+{sel}+'\"]'))"
            f"?.click() ?? 'not found'"
        )
        return self.page_js(js)

    def _nav_js(self, js: str) -> Any:
        def op() -> Any:
            val = self.page_js(js)
            time.sleep(0.3)  # let the navigation kick in
            self.resolve_actors()  # SPEC: navigate-class actions invalidate actors
            return val

        return self._run(op)

    def go_back(self) -> Any:
        return self._nav_js("history.back()")

    def reload(self) -> Any:
        return self._nav_js("location.reload()")

    def screenshot(self) -> str:
        """adb exec-out screencap -p → ~/.hermes/cache/scratch/tvbro_shot_<ts>.png"""
        self.ensure_forward()
        r = subprocess.run(
            [ADB, "-s", ADB_SERIAL, "exec-out", "screencap", "-p"],
            capture_output=True,
            timeout=30,
        )
        if r.returncode != 0 or not r.stdout.startswith(b"\x89PNG"):
            raise BrowserError(
                f"screencap failed (rc={r.returncode}): {r.stderr[:200].decode('utf-8', 'replace')}"
            )
        SHOT_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        path = SHOT_DIR / f"tvbro_shot_{ts}.png"
        path.write_bytes(r.stdout)
        return str(path)


# module-level singleton: one connection shared by all tools
_SESSION: BrowserSession | None = None


def get_session() -> BrowserSession:
    global _SESSION
    if _SESSION is None:
        _SESSION = BrowserSession()
    return _SESSION


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------
@mcp.tool()
def tabs_list() -> list[dict[str, str]]:
    """List the open tabs in TV Bro on the projector. Returns [{title, url}]."""
    return get_session().tabs()


@mcp.tool()
def navigate(url: str) -> dict[str, str]:
    """Navigate the active tab to url. Two-phase wait (≤15s each): first for
    location.href to become the target, then for readyState 'complete'.

    Returns {"title", "url"} of the loaded page.
    """
    return get_session().navigate(url)


@mcp.tool()
def evaluate(js: str) -> str | dict:
    """Evaluate JavaScript in the active tab (via evaluateJSAsync).

    Returns result.value JSON-serialized; on a JS exception returns {"error": ...}.
    """
    try:
        value = get_session().page_js(js)
        return json.dumps(value, ensure_ascii=False, default=str)
    except BrowserError as exc:
        return {"error": str(exc)}


@mcp.tool()
def read_page(max_chars: int = 3000) -> dict:
    """Read the active page: {"t": title, "u": url, "x": body innerText (truncated)}."""
    return get_session().read_page(max_chars)


@mcp.tool()
def click(selector: str) -> str:
    """Click the element matching the CSS selector; falls back to matching
    [aria-label="<selector>"]. Clicks return 'not found' when nothing matches."""
    return json.dumps(get_session().click(selector), ensure_ascii=False, default=str)


@mcp.tool()
def screenshot() -> str:
    """Capture the projector screen via adb screencap; returns the saved PNG path."""
    return get_session().screenshot()


@mcp.tool()
def go_back() -> dict:
    """history.back() in the active tab, then refresh the RDP actor cache."""
    return {"ok": True, "result": get_session().go_back()}


@mcp.tool()
def reload() -> dict:
    """location.reload() in the active tab, then refresh the RDP actor cache."""
    return {"ok": True, "result": get_session().reload()}


def main() -> None:
    # SPEC: 启动时自动确保 forward 存在；设备掉线则提示（不 crash，工具调用时再报）
    try:
        get_session().ensure_forward()
    except BrowserError as exc:
        print(f"[tvbro-mcp] warning: {exc}", file=sys.stderr)
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
