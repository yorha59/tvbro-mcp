#!/usr/bin/env python3
"""Non-pytest smoke test against the live projector (SPEC self-check #1).

Flow: tabs_list → navigate example.com → read_page → evaluate 1+1.
Prints each step's verbatim output. Exits non-zero on any failure.

Preconditions: projector on, TV Bro in the foreground (with the in-app
「Web 引擎调试」 switch enabled) and adb reachable at 192.168.100.126:5555.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tvbro_mcp.server import BrowserSession  # noqa: E402


def main() -> int:
    s = BrowserSession()
    failures: list[str] = []

    print("=== step 1/4: tabs_list() ===")
    try:
        tabs = s.tabs()
        print(json.dumps(tabs, ensure_ascii=False, indent=2))
        assert isinstance(tabs, list) and len(tabs) >= 1 and tabs[0].get("url") is not None, (
            f"unexpected tabs payload: {tabs!r}"
        )
    except Exception as exc:  # noqa: BLE001
        failures.append(f"tabs_list: {type(exc).__name__}: {exc}")
        print(f"FAILED: {type(exc).__name__}: {exc}")

    print()
    print("=== step 2/4: navigate('https://example.com') ===")
    try:
        nav = s.navigate("https://example.com")
        print(json.dumps(nav, ensure_ascii=False, indent=2))
        assert nav.get("url") and "example.com" in nav["url"], f"unexpected nav result: {nav!r}"
        assert nav.get("title"), f"missing title: {nav!r}"
    except Exception as exc:  # noqa: BLE001
        failures.append(f"navigate: {type(exc).__name__}: {exc}")
        print(f"FAILED: {type(exc).__name__}: {exc}")

    print()
    print("=== step 3/4: read_page(3000) ===")
    try:
        page = s.read_page(3000)
        print(json.dumps(page, ensure_ascii=False, indent=2))
        assert page.get("u") and "example.com" in page["u"], f"unexpected page url: {page!r}"
        assert page.get("t") and page.get("x"), f"incomplete page payload: {page!r}"
    except Exception as exc:  # noqa: BLE001
        failures.append(f"read_page: {type(exc).__name__}: {exc}")
        print(f"FAILED: {type(exc).__name__}: {exc}")

    print()
    print("=== step 4/4: evaluate('1+1') ===")
    try:
        val = s.evaluate_js("1+1")
        print(json.dumps(val, ensure_ascii=False))
        assert val == 2, f"expected 2, got {val!r}"
    except Exception as exc:  # noqa: BLE001
        failures.append(f"evaluate: {type(exc).__name__}: {exc}")
        print(f"FAILED: {type(exc).__name__}: {exc}")

    print()
    if failures:
        print("RESULT: FAIL")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("RESULT: PASS — all 4 steps ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
