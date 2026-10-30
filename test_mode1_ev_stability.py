"""EV stability smoke test for Mode 1 postflop HTTP endpoint.

This test is aimed at preventing regressions where the backend becomes
non-deterministic or drifts in EV outputs across repeated calls.

It:
- Starts server.py on a random free localhost port.
- Runs the same 4 requests as the Mode 1 real-API validation script:
  - hero_pos/vill_open_pos in {(BB,BTN), (BTN,CO)}
  - preflop_action in {CALL, 3BET}
- Uses iterations=3 so server selects the small equity budget (equity_iters
  chosen server-side).
- Repeats for N runs and asserts:
  - hero_action_evs is present in the expected active node
  - bestAct matches across runs
  - bestEv stays within a tight tolerance across runs

Run:
  python test_mode1_ev_stability.py
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


def _post_json(url: str, payload: dict, *, timeout_s: float = 180.0) -> dict:
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


def _argmax_ev(hero_action_evs: dict) -> tuple[str, float]:
    if not hero_action_evs:
        return "", float("-inf")
    act, ev = max(hero_action_evs.items(), key=lambda kv: kv[1])
    return str(act), float(ev)


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

        # Match the Mode 1 real-API script budget (and server-side equity_iters selection).
        iterations = 3

        # Stability tolerances.
        best_ev_abs_tol = 1e-4

        # Repeat to catch non-determinism.
        repeats = 4

        cases: list[tuple[str, str, str]] = []
        # (hero_pos, vill_open_pos, preflop_action)
        for hero_pos, vill_open_pos in (("BB", "BTN"), ("BTN", "CO")):
            for preflop_action in ("CALL", "3BET"):
                cases.append((hero_pos, vill_open_pos, preflop_action))

        per_case_history: dict[tuple[str, str, str], list[tuple[str, float]]] = {
            c: [] for c in cases
        }

        for _rep in range(repeats):
            for hero_pos, vill_open_pos, preflop_action in cases:
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

                resp = _post_json(url, payload, timeout_s=180.0)

                nodes = resp.get("nodes") or {}
                node = nodes.get(_active_node_id(hero_pos, vill_open_pos)) or {}
                hero_action_evs = node.get("hero_action_evs") or {}
                if not hero_action_evs:
                    raise SystemExit(
                        "FAIL: missing hero_action_evs "
                        f"hero_pos={hero_pos} vill_open_pos={vill_open_pos} preflop_action={preflop_action}"
                    )

                best_act, best_ev = _argmax_ev(hero_action_evs)
                per_case_history[(hero_pos, vill_open_pos, preflop_action)].append(
                    (best_act, best_ev)
                )

        # Compare across repeats.
        for (hero_pos, vill_open_pos, preflop_action), hist in per_case_history.items():
            base_act, base_ev = hist[0]
            for i, (act, ev) in enumerate(hist[1:], start=1):
                if act != base_act:
                    raise SystemExit(
                        "FAIL: bestAct drift "
                        f"case hero_pos={hero_pos} vill_open_pos={vill_open_pos} preflop_action={preflop_action} "
                        f"rep0={base_act} rep{i}={act}"
                    )
                if abs(ev - base_ev) > best_ev_abs_tol:
                    raise SystemExit(
                        "FAIL: bestEv drift "
                        f"case hero_pos={hero_pos} vill_open_pos={vill_open_pos} preflop_action={preflop_action} "
                        f"rep0={base_ev:.6f} rep{i}={ev:.6f} tol={best_ev_abs_tol}"
                    )

        print("PASS")

    finally:
        httpd.shutdown()
        httpd.server_close()


if __name__ == "__main__":
    main()
