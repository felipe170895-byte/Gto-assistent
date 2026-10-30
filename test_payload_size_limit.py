"""Ensure server rejects oversized JSON bodies for POST endpoints.

This test validates the defensive payload-size guard added to server.py.

Run:
  python test_payload_size_limit.py
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request

from http.server import HTTPServer

import server as srv


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    addr, port = s.getsockname()
    s.close()
    return int(port)


def _post_json(url: str, payload: dict, *, timeout_s: int = 30) -> None:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        _ = resp.read()


def main() -> None:
    port = _free_port()
    httpd = HTTPServer(("127.0.0.1", port), srv.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.2)

    try:
        url = f"http://127.0.0.1:{port}{srv.POSTFLOP_API_PATH}"

        # Must exceed MAX_POST_BODY_BYTES in server.py.
        big = "x" * 2_100_000
        payload = {
            "board": ["2c", "7d", "9s"],
            "heroCards": ["Ah", "Kh"],
            "heroPos": "BB",
            "villOpenPos": "BTN",
            "effectiveStack": 100.0,
            "openSize": 2.2,
            "iterations": 3,
            "preflop_action": "CALL",
            "betConfig": {
                "oop_lead_sizes": [0.33, 0.75],
                "ip_cbet_sizes": [0.33, 0.75],
                "check_raise_sizes": [3.0],
                "allow_donk": False,
                "ip_raise_sizes": [2.0],
            },
            "oversize_marker": big,
        }

        try:
            _post_json(url, payload, timeout_s=10)
        except urllib.error.HTTPError as e:
            # server should reject early with 413
            if getattr(e, "code", None) != 413:
                body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
                raise SystemExit(f"FAIL: expected HTTP 413, got {getattr(e,'code',None)} body={body[:200]}")
        else:
            raise SystemExit("FAIL: expected HTTP 413 but request succeeded")

        print("PASS")
    finally:
        httpd.shutdown()
        httpd.server_close()


if __name__ == "__main__":
    main()
