"""Lightweight contract tests for Mode 1 (pre-flop) HTTP endpoints.

This test validates:
- /api/status_mode1_v2.php returns requested + *_used fields when query params are provided.
- /api/query_preflop_gto.php returns the JSON shape expected by poker_gto_mode1_app.html
  when the backing strategy file exists (or falls back).

Run:
  python test_preflop_status_and_contract.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple


SERVER_PY = "server.py"
STATUS_URL = "http://localhost:8000/api/status_mode1_v2.php"
QUERY_URL = "http://localhost:8000/api/query_preflop_gto.php"


def http_get_json(url: str, timeout_s: float = 2.0) -> Dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout_s) as r:
        data = r.read().decode("utf-8")
    return json.loads(data)


def http_post_json(url: str, payload: Dict[str, Any], timeout_s: float = 30.0) -> Tuple[int, Dict[str, Any]]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as r:
        status = getattr(r, "status", 200)
        txt = r.read().decode("utf-8")
    return int(status), json.loads(txt)


def wait_for_status(url: str, *, timeout_s: float = 10.0) -> Dict[str, Any]:
    deadline = time.time() + timeout_s
    last_err: Optional[BaseException] = None
    while time.time() < deadline:
        try:
            return http_get_json(url, timeout_s=1.0)
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(0.2)
    raise RuntimeError(f"Status endpoint not ready: {last_err}")


def main() -> None:
    proc = subprocess.Popen([sys.executable, SERVER_PY], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        status_url = STATUS_URL + "?openSize=2.5&stack=40&rangeProfile=tight"
        status = wait_for_status(status_url, timeout_s=15.0)

        requested = status.get("requested") or {}
        assert "openSize" in requested and "stack" in requested and "rangeProfile" in requested

        files = status.get("files") or []
        assert isinstance(files, list)

        # Pick any DEFEND record that exists according to status.
        chosen = None
        for f in files:
            if f.get("exists"):
                chosen = f
                break
        if not chosen:
            # If nothing exists for these params in this workspace, the test still proves the contract:
            # just check that the shape for *returned records* includes the *_used fields.
            # (UI should still guard, but this endpoint is also useful for diagnostics.)
            if files:
                sample = files[0]
                for k in ("openSize_used", "effective_stack_used", "rangeProfile_used", "fallback_reason"):
                    assert k in sample
            print("PASS (no backing strategy exists for requested params in this workspace)")
            return

        hero_pos = chosen.get("hero_pos")
        vill_open_pos = chosen.get("vill_open_pos")
        assert hero_pos in ("CO", "BTN", "BB")
        assert vill_open_pos in ("UTG", "CO", "BTN")

        # Query with a known-ish hand class: pocket aces.
        payload = {
            "heroPos": hero_pos,
            "villOpenPos": vill_open_pos,
            "spotType": "defend",
            "effectiveStack": int(requested.get("stack", 40)),
            "rangeProfile": requested.get("rangeProfile", "tight"),
            "openSize": requested.get("openSize", 2.5),
            "heroCards": ["Ah", "Ad"],
        }

        try:
            status_code, data = http_post_json(QUERY_URL, payload, timeout_s=60.0)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
            raise AssertionError(f"Expected 200 from query endpoint, got HTTP {e.code}: {body}") from e

        assert status_code == 200

        for k in ("hero_class", "evs", "strategy", "effective_stack_used", "rangeProfile_used", "openSize_used", "fallback_reason"):
            assert k in data, f"missing {k} in response"

        evs = data.get("evs")
        strat = data.get("strategy")
        assert isinstance(evs, list) and len(evs) == 3
        assert isinstance(strat, list) and len(strat) == 3

        print("PASS")

    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


if __name__ == "__main__":
    main()
