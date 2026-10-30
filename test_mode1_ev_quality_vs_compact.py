"""Compare EV/best action between low-budget and higher-budget Mode 1 solves.

Goal:
- Keep response_mode="compact" contract, but increase iterations budget.
- Validate that higher-budget results are internally stable across repeats.
- Provide a quality signal: how much bestEv/bestAct changes vs the low budget.

Run:
  python test_mode1_ev_quality_vs_compact.py
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


def _quantile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return float("nan")
    idx = int(q * (len(sorted_vals) - 1))
    return float(sorted_vals[idx])


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

        # Low budget: used by earlier tests.
        iterations_low = 3
        repeats_low = 4
        low_ev_abs_tol = 1e-4

        # Higher budget: quality signal for better GTO decisions.
        # (Still keep response_mode=compact to ensure UI-compatibility.)
        iterations_high = 60
        repeats_high = 3
        high_ev_abs_tol = 5e-4

        # (hero_pos, vill_open_pos, preflop_action)
        cases: list[tuple[str, str, str]] = []
        for hero_pos, vill_open_pos in (("BB", "BTN"), ("BTN", "CO")):
            for preflop_action in ("CALL", "3BET"):
                cases.append((hero_pos, vill_open_pos, preflop_action))

        history_low: dict[tuple[str, str, str], list[tuple[str, float]]] = {c: [] for c in cases}
        history_high: dict[tuple[str, str, str], list[tuple[str, float]]] = {c: [] for c in cases}

        def run_budget(*, iterations: int, repeats: int, out_hist, compute_mode: str):
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
                        # keep response payload contract stable
                        "response_mode": "compact",
                        "compute_mode": compute_mode,
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
                            f"iterations={iterations} hero_pos={hero_pos} vill_open_pos={vill_open_pos} preflop_action={preflop_action}"
                        )

                    best_act, best_ev = _best_action_and_ev(hero_action_evs)
                    out_hist[(hero_pos, vill_open_pos, preflop_action)].append((best_act, best_ev))

        print("Running low-budget repeats...")
        run_budget(
            iterations=iterations_low,
            repeats=repeats_low,
            out_hist=history_low,
            compute_mode="compact",
        )

        print("Running higher-budget repeats...")
        run_budget(
            iterations=iterations_high,
            repeats=repeats_high,
            out_hist=history_high,
            compute_mode="quality",
        )

        # Assertions: internal stability at each budget.
        def assert_stable(hist: list[tuple[str, float]], *, ev_abs_tol: float, label: str, case):
            base_act, base_ev = hist[0]
            for i, (act, ev) in enumerate(hist[1:], start=1):
                if act != base_act:
                    raise SystemExit(
                        f"FAIL: bestAct drift ({label}) case={case} rep0={base_act} rep{i}={act}"
                    )
                if abs(ev - base_ev) > ev_abs_tol:
                    raise SystemExit(
                        f"FAIL: bestEv drift ({label}) case={case} rep0={base_ev:.6f} rep{i}={ev:.6f} tol={ev_abs_tol}"
                    )

        # Quality signal output.
        print("\nQuality signal (compact response_mode):")
        for case in cases:
            hist_low = history_low[case]
            hist_high = history_high[case]

            assert_stable(
                hist_low,
                ev_abs_tol=low_ev_abs_tol,
                label=f"low(iters={iterations_low})",
                case=str(case),
            )
            assert_stable(
                hist_high,
                ev_abs_tol=high_ev_abs_tol,
                label=f"high(iters={iterations_high})",
                case=str(case),
            )

            low_best_act, low_best_ev = hist_low[0]
            high_best_act, high_best_ev = hist_high[0]

            # Print p05/p95 for extra context (no strict asserts).
            low_evs = sorted(ev for _, ev in hist_low)
            high_evs = sorted(ev for _, ev in hist_high)

            low_p05 = low_evs[int(0.05 * (len(low_evs) - 1))] if len(low_evs) > 1 else low_best_ev
            low_p95 = low_evs[int(0.95 * (len(low_evs) - 1))] if len(low_evs) > 1 else low_best_ev
            high_p05 = high_evs[int(0.05 * (len(high_evs) - 1))] if len(high_evs) > 1 else high_best_ev
            high_p95 = high_evs[int(0.95 * (len(high_evs) - 1))] if len(high_evs) > 1 else high_best_ev

            print(
                f"- case={case}: low best={low_best_act} ev={low_best_ev:.6f} (p05={low_p05:.6f}, p95={low_p95:.6f}) "
                f"| high best={high_best_act} ev={high_best_ev:.6f} (p05={high_p05:.6f}, p95={high_p95:.6f}) "
                f"| delta_ev={high_best_ev - low_best_ev:+.6f}"
            )

        print("PASS")

    finally:
        httpd.shutdown()
        httpd.server_close()


if __name__ == "__main__":
    main()
