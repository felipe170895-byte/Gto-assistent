"""Validate timing telemetry contract for Mode 1 postflop HTTP endpoint.

Contract (Task 4):
- response_mode="compact" must NOT include timings_ms.
- response_mode="full" must include timings_ms with stable keys.

Run:
  python test_mode1_timings_compact_absent.py
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.request

from http.server import HTTPServer

import server as srv


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    addr, port = s.getsockname()
    s.close()
    return int(port)


def _post_json(url: str, payload: dict, *, timeout_s: float = 240.0) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw)


def _base_payload(*, response_mode: str) -> dict:
    # Intentionally small budget to keep the test lightweight.
    # (server derives equity_iters conservatively from iterations.)
    return {
        "board": ["2c", "7d", "9s"],
        "heroPos": "BB",
        "villOpenPos": "BTN",
        "preflop_action": "CALL",
        "tree_mode": "standard",
        "response_mode": response_mode,
        "heroCards": ["Ah", "Kh"],
        "effective_stack": 100.0,
        "open_size": 2.2,
        "rake_pct": 0.0,
        "rake_cap": 0.0,
        "iterations": 10,
        # Keep payload contract stable / deterministic for timing test.
        "betConfig": {
            "expand_partial_sizes": False,
            "check_raise_sizes": [3.0],
            "allow_donk": False,
            "ip_raise_sizes": [2.0],
            "oop_lead_sizes": None,
            "ip_cbet_sizes": [0.5],
            "max_raises_per_street": 1,
        },
    }


def main() -> None:
    port = _free_port()
    httpd = HTTPServer(("127.0.0.1", port), srv.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.2)

    try:
        url = f"http://127.0.0.1:{port}{srv.POSTFLOP_API_PATH}"

        # --- compact must omit timings_ms
        payload_compact = _base_payload(response_mode="compact")
        result_compact = _post_json(url, payload_compact, timeout_s=240.0)
        assert "nodes" in result_compact, "expected nodes in compact response"
        assert "timings_ms" not in result_compact, "timings_ms must be absent in compact mode"

        # --- full must include timings_ms
        payload_full = _base_payload(response_mode="full")
        result_full = _post_json(url, payload_full, timeout_s=240.0)
        assert "nodes" in result_full, "expected nodes in full response"
        assert "timings_ms" in result_full, "timings_ms must be present in full mode"

        timings = result_full["timings_ms"]
        assert isinstance(timings, dict), "timings_ms must be a dict"

        required_keys = {"equity_matrix_ms", "tree_build_ms", "cfr_ms", "total_ms"}
        missing = [k for k in sorted(required_keys) if k not in timings]
        assert not missing, f"timings_ms missing keys: {missing}"

        # Basic numeric sanity.
        for k in required_keys:
            v = timings.get(k)
            assert isinstance(v, (int, float)), f"timings_ms[{k}] must be numeric"
            assert float(v) >= 0.0, f"timings_ms[{k}] must be >= 0"

        print("PASS")

    finally:
        httpd.shutdown()
        httpd.server_close()


if __name__ == "__main__":
    main()
