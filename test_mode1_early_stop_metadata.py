"""Convergence early-stop metadata test for Mode 1 postflop endpoint.

This test validates Task 3 (CFR+ convergence guard):

- When `convergence_guard` is enabled, the response must include:
  - `iterations_completed`
  - `stop_reason`
- For compact responses, convergence history must NOT be included.
- For full responses, convergence history must be included.

Run:
  python test_mode1_early_stop_metadata.py
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


def _ui_pot_and_investments(preflop_action: str, open_size: float):
    if preflop_action == "CALL":
        pot_pre_override = 1.5 + 2.0 * open_size - 0.5
        oop_invested_pre = open_size - 0.5
        ip_invested_pre = open_size
        return float(pot_pre_override), float(oop_invested_pre), float(ip_invested_pre)

    if preflop_action == "3BET":
        threebet_size = open_size * 3.0
        pot_pre_override = 1.5 + open_size + threebet_size + (threebet_size - 0.5)
        oop_invested_pre = 0.5 + threebet_size
        ip_invested_pre = open_size + (threebet_size - 0.5)
        return float(pot_pre_override), float(oop_invested_pre), float(ip_invested_pre)

    raise ValueError(preflop_action)


def main() -> None:
    port = _free_port()
    httpd = HTTPServer(("127.0.0.1", port), srv.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.2)

    try:
        url = f"http://127.0.0.1:{port}{srv.POSTFLOP_API_PATH}"

        board = ["2c", "7d", "9s"]
        hero_cards = ["Ah", "Kh"]
        hero_pos = "BB"
        vill_open_pos = "BTN"
        open_size = 2.2

        preflop_action = "CALL"
        pot_pre_override, oop_invested_pre, ip_invested_pre = _ui_pot_and_investments(
            preflop_action, open_size
        )

        # Request sizes kept intentionally small.
        requested_iterations = 20

        base_payload = {
            "board": board,
            "heroCards": hero_cards,
            "heroPos": hero_pos,
            "villOpenPos": vill_open_pos,
            "effectiveStack": 100.0,
            "openSize": open_size,
            "iterations": requested_iterations,
            "compute_mode": "quality",
            "tree_mode": "standard",
            "pot_pre_override": pot_pre_override,
            "oop_invested_pre": oop_invested_pre,
            "ip_invested_pre": ip_invested_pre,
            "preflop_action": preflop_action,
            # Keep tree small / deterministic.
            "betConfig": {
                "oop_lead_sizes": None,
                "ip_cbet_sizes": [0.5],
                "check_raise_sizes": [3.0],
                "allow_donk": False,
                "ip_raise_sizes": [2.0],
                "expand_partial_sizes": False,
                "max_raises_per_street": 1,
            },
            # Early-stop config.
            "convergence_guard": True,
            # Use a very loose threshold so we deterministically trigger.
            "convergence_eps": 1e6,
            "convergence_patience": 1,
            # Check every 5 iterations; with patience=1 and eps loose,
            # we expect to stop at the first probe checkpoint.
            "convergence_probe_every": 5,
        }

        # --- Compact: metadata must exist, but no convergence history.
        payload_compact = dict(base_payload)
        payload_compact["response_mode"] = "compact"
        resp_compact = _post_json(url, payload_compact, timeout_s=240.0)

        assert "iterations_completed" in resp_compact, "Missing iterations_completed (compact)"
        assert "stop_reason" in resp_compact, "Missing stop_reason (compact)"

        iters_done = resp_compact["iterations_completed"]
        stop_reason = resp_compact["stop_reason"]

        assert iters_done <= requested_iterations, (
            f"iterations_completed={iters_done} > requested_iterations={requested_iterations}"
        )
        assert stop_reason == "metric_below_threshold", (
            f"stop_reason={stop_reason!r} (expected metric_below_threshold)"
        )

        assert "convergence_metric_history" not in resp_compact, (
            "convergence_metric_history must be absent in compact responses"
        )

        # --- Full: history must be included.
        payload_full = dict(base_payload)
        payload_full["response_mode"] = "full"
        resp_full = _post_json(url, payload_full, timeout_s=240.0)

        assert "iterations_completed" in resp_full, "Missing iterations_completed (full)"
        assert "stop_reason" in resp_full, "Missing stop_reason (full)"
        assert "convergence_metric_history" in resp_full, "Missing convergence_metric_history (full)"

        hist = resp_full["convergence_metric_history"]
        assert isinstance(hist, list) and len(hist) >= 1, "convergence_metric_history must be a non-empty list"

        print("PASS")

    finally:
        httpd.shutdown()
        httpd.server_close()


if __name__ == "__main__":
    main()
