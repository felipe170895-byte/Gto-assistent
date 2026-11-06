"""Quality pruning continuous mapping regression test (Mode 1).

Goal (Task 5): reduce EV/bestAct discontinuities when increasing the
iteration budget under compute_mode="quality".

This test samples a small ladder of budgets within the same server-side
"quality" tree-fanout regime (iterations <= 30) so the only major change
comes from pruning-parameter selection in postflop_service.

Run:
  python test_mode1_quality_pruning_continuous_mapping.py
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


def _best_action_and_ev(hero_action_evs: dict) -> tuple[str, float]:
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

        # Sample budgets within iterations <= 30 to keep server-side quality
        # expansion/fan-out stable (Task 5 targets pruning mapping).
        iterations_list = [10, 20, 30]

        # (hero_pos, vill_open_pos, preflop_action)
        cases: list[tuple[str, str, str]] = [
            ("BB", "BTN", "CALL"),
            ("BTN", "CO", "CALL"),
        ]

        # If pruning mapping is stepwise, these EV deltas can spike.
        # Continuous interpolation should keep them smaller.
        # Tune tolerance to allow small Monte-Carlo/CFR noise,
        # while still catching large pruning-induced discontinuities.
        ev_abs_tol_between_budgets = 0.22

        for hero_pos, vill_open_pos, preflop_action in cases:
            act_by_it: dict[int, str] = {}
            ev_by_it: dict[int, float] = {}

            for it in iterations_list:
                # Note: if this test fails intermittently due to minor CFR noise,
                # increase ev_abs_tol_between_budgets carefully (do not mask
                # large prune-induced discontinuities).
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
                    "iterations": it,
                    "pot_pre_override": pot_pre_override,
                    "oop_invested_pre": oop_invested_pre,
                    "ip_invested_pre": ip_invested_pre,
                    "preflop_action": preflop_action,
                    "compute_mode": "quality",
                    "response_mode": "compact",
                    "betConfig": {
                        "oop_lead_sizes": [0.33, 0.75],
                        "ip_cbet_sizes": [0.33, 0.75],
                        "check_raise_sizes": [3.0],
                        "allow_donk": False,
                    },
                }

                resp = _post_json(url, payload, timeout_s=240.0)

                nodes = resp.get("nodes") or {}
                node = nodes.get(_active_node_id(hero_pos, vill_open_pos)) or {}
                hero_action_evs = node.get("hero_action_evs") or {}

                if not hero_action_evs:
                    raise SystemExit(
                        "FAIL: missing hero_action_evs "
                        f"case={(hero_pos, vill_open_pos, preflop_action)} it={it}"
                    )

                act, ev = _best_action_and_ev(hero_action_evs)
                act_by_it[it] = act
                ev_by_it[it] = ev

            # Best action should not jump within the same quality regime.
            if not (act_by_it[iterations_list[0]] == act_by_it[iterations_list[1]] == act_by_it[iterations_list[2]]):
                raise SystemExit(
                    "FAIL: bestAct discontinuity "
                    f"case={(hero_pos, vill_open_pos, preflop_action)} act_by_it={act_by_it}"
                )

            # EV deltas should be smooth when pruning parameters are interpolated.
            ev10, ev20, ev30 = ev_by_it[10], ev_by_it[20], ev_by_it[30]
            if abs(ev20 - ev10) > ev_abs_tol_between_budgets or abs(ev30 - ev20) > ev_abs_tol_between_budgets:
                raise SystemExit(
                    "FAIL: EV discontinuity too large "
                    f"case={(hero_pos, vill_open_pos, preflop_action)} "
                    f"ev10={ev10:.6f} ev20={ev20:.6f} ev30={ev30:.6f} "
                    f"tol={ev_abs_tol_between_budgets}"
                )

        print("PASS")

    finally:
        httpd.shutdown()
        httpd.server_close()


if __name__ == "__main__":
    main()
