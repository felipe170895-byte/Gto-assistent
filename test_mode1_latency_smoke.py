"""Latency smoke test for Mode 1 postflop HTTP endpoint.

This test is meant to prevent regressions where the backend becomes slow enough
that the real API validation script times out.

It:
- Starts server.py on a random free localhost port.
- Sends the same 4 requests as mode1_task16_real_api_validation_safe.py
  (CALL/3BET for both OOP and IP scenarios).
- Repeats for a small number of iterations (default 3).
- Asserts:
  - hero_action_evs is present in the expected active node for each request.
  - per-request duration stays below a conservative threshold.

Run:
  python test_mode1_latency_smoke.py
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


def _post_json(url: str, payload: dict, *, timeout_s: float = 120.0) -> dict:
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


def _hero_is_oop(hero_pos: str, vill_open_pos: str) -> bool:
    # Mirrors poker_gto_mode1_app.html: heroIsOop = heroPos in {BB, SB} OR (CO && BTN)
    return hero_pos in ("BB", "SB") or (hero_pos == "CO" and vill_open_pos == "BTN")


def _active_node_id(hero_pos: str, vill_open_pos: str) -> str:
    return "root" if _hero_is_oop(hero_pos, vill_open_pos) else "x"


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
        open_size = 2.2
        iterations = 3  # keep small (the contract/timeout regression case)

        # Real-script cases: (hero_pos, vill_open_pos)
        cases = [("BB", "BTN"), ("BTN", "CO")]

        # Conservative threshold: in practice should be ~2-5s/request.
        # Fail fast if it regresses badly.
        per_request_max_s = 15.0

        repeats = 3
        durations: list[float] = []

        for _rep in range(repeats):
            for hero_pos, vill_open_pos in cases:
                for preflop_action in ("CALL", "3BET"):
                    pot_pre_override, oop_invested_pre, ip_invested_pre = _ui_pot_and_investments(
                        preflop_action, open_size
                    )

                    payload = {
                        "board": board,
                        "heroCards": hero_cards,
                        "heroPos": hero_pos,
                        "villOpenPos": vill_open_pos,
                        "effectiveStack": 100.0,
                        "openSize": open_size,
                        "iterations": iterations,
                        "pot_pre_override": pot_pre_override,
                        "oop_invested_pre": oop_invested_pre,
                        "ip_invested_pre": ip_invested_pre,
                        "preflop_action": preflop_action,
                        "betConfig": {
                            "oop_lead_sizes": [0.33, 0.75],
                            "ip_cbet_sizes": [0.33, 0.75],
                            "check_raise_sizes": [3.0],
                            "allow_donk": False,
                        },
                    }

                    t0 = time.perf_counter()
                    resp = _post_json(url, payload, timeout_s=180.0)
                    dt = time.perf_counter() - t0
                    durations.append(dt)

                    nodes = resp.get("nodes") or {}
                    active_id = _active_node_id(hero_pos, vill_open_pos)
                    node = nodes.get(active_id) or {}
                    hero_action_evs = node.get("hero_action_evs") or {}

                    if not hero_action_evs:
                        raise SystemExit(
                            f"FAIL: missing hero_action_evs rep={_rep} hero_pos={hero_pos} "
                            f"vill_open_pos={vill_open_pos} preflop_action={preflop_action}"
                        )

                    if dt > per_request_max_s:
                        raise SystemExit(
                            f"FAIL: request too slow dt={dt:.2f}s (max {per_request_max_s}s) rep={_rep} "
                            f"hero_pos={hero_pos} vill_open_pos={vill_open_pos} preflop_action={preflop_action}"
                        )

        durations_sorted = sorted(durations)
        p95 = durations_sorted[int(0.95 * (len(durations_sorted) - 1))]
        print(
            "PASS: Mode1 latency stable "
            f"({len(durations)} reqs) p95={p95:.2f}s max={max(durations):.2f}s"
        )

    finally:
        httpd.shutdown()
        httpd.server_close()


if __name__ == "__main__":
    main()
