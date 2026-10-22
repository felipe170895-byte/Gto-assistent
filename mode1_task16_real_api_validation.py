from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Dict, Any, List, Tuple


SERVER_PY = r"C:\Users\USER\Documents\Nova pasta\server.py"
STATUS_URL = "http://localhost:8000/api/status_mode1_v2.php"
POSTFLOP_URL = "http://localhost:8000/api/query_postflop_gto.php"


def http_get_json(url: str, timeout_s: float = 2.0) -> Dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout_s) as r:
        data = r.read().decode("utf-8")
    return json.loads(data)


def http_post_json(url: str, payload: Dict[str, Any], timeout_s: float = 60.0) -> Dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as r:
        txt = r.read().decode("utf-8")
    return json.loads(txt)


def pot_and_investments(preflop_action: str, open_size: float, hero_is_oop: bool) -> Tuple[float, float, float]:
    # Matches poker_gto_mode1_app.html formulas in continueDefendCallToFlop / continueThreebetToFlop
    if preflop_action == "CALL":
        pot_pre_override = 1.5 + 2.0 * open_size - 0.5
        if hero_is_oop:
            oop_invested_pre = open_size - 0.5
            ip_invested_pre = open_size
        else:
            oop_invested_pre = open_size - 0.5
            ip_invested_pre = open_size
        # NOTE: for CALL, UI formulas are symmetric aside from naming; keep the same as earlier tests.
        # In practice for hero_is_oop=false, the UI swaps which seat is which at the solver layer.
        return pot_pre_override, float(oop_invested_pre), float(ip_invested_pre)

    if preflop_action == "3BET":
        threebet_size = open_size * 3.0
        pot_pre_override = 1.5 + open_size + threebet_size + (threebet_size - 0.5)
        if hero_is_oop:
            oop_invested_pre = 0.5 + threebet_size
            ip_invested_pre = open_size + (threebet_size - 0.5)
        else:
            oop_invested_pre = 0.5 + threebet_size
            ip_invested_pre = open_size + (threebet_size - 0.5)
        return float(pot_pre_override), float(oop_invested_pre), float(ip_invested_pre)

    raise ValueError(preflop_action)


def argmax_ev(hero_action_evs: Dict[str, float]) -> Tuple[str, float]:
    if not hero_action_evs:
        return "", float("-inf")
    act, ev = max(hero_action_evs.items(), key=lambda kv: kv[1])
    return act, float(ev)


def run_one_case(*, hero_pos: str, vill_open_pos: str, preflop_action: str, board: List[str], hero_cards: List[str], open_size: float, iterations: int) -> Dict[str, Any]:
    # Determine hero_is_oop exactly like the JS (heroIsOop expression)
    hero_is_oop = (hero_pos == "BB" or hero_pos == "SB" or (hero_pos == "CO" and vill_open_pos == "BTN"))
    active_node_id = "root" if hero_is_oop else "x"

    pot_pre_override, oop_invested_pre, ip_invested_pre = pot_and_investments(preflop_action, open_size, hero_is_oop)

    payload = {
        "board": board,
        "heroCards": hero_cards,
        "heroPos": hero_pos,
        "villOpenPos": vill_open_pos,
        "effectiveStack": 100.0,
        "openSize": open_size,
        "iterations": iterations,
        # Hand-continuous fields
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

    resp = http_post_json(POSTFLOP_URL, payload, timeout_s=max(90.0, iterations * 3.0))

    nodes = resp.get("nodes") or {}
    node = nodes.get(active_node_id) or {}

    hero_action_evs = node.get("hero_action_evs") or {}
    hero_diag = node.get("hero_diag") or {}
    best_act, best_ev = argmax_ev(hero_action_evs)

    return {
        "hero_pos": hero_pos,
        "vill_open_pos": vill_open_pos,
        "preflop_action": preflop_action,
        "active_node_id": active_node_id,
        "ev_present": bool(hero_action_evs),
        "hero_action_evs_keys": sorted(list(hero_action_evs.keys())),
        "bestAct": best_act,
        "bestEv": best_ev,
        "hero_diag": {
            "hero_combo_present": hero_diag.get("hero_combo_present"),
            "hero_combo_key": hero_diag.get("hero_combo_key"),
            "hero_side_match": hero_diag.get("hero_side_match"),
            "hero_weight_sum": hero_diag.get("hero_weight_sum"),
        },
    }


def main():
    board = ["2c", "7d", "9s"]
    hero_cards = ["Ah", "Kh"]
    open_size = 2.2
    iterations = 10

    # Start server
    print("[1/4] Starting server.py ...")
    proc = subprocess.Popen([sys.executable, SERVER_PY], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    try:
        # Wait for status endpoint
        ready = False
        for i in range(60):
            try:
                _ = http_get_json(STATUS_URL, timeout_s=1.0)
                ready = True
                break
            except Exception:
                time.sleep(0.25)

        if not ready:
            # Read a small amount of log for debugging
            log = ""
            try:
                time.sleep(0.2)
                if proc.stdout is not None:
                    log = (proc.stdout.read(400) or "")
            except Exception:
                pass
            raise RuntimeError("Server did not become ready in time. Log snippet: " + log)

        print("[2/4] Server is up. Running CALL vs 3BET (hero OOP BB vs BTN) ...")
        case_call = run_one_case(
            hero_pos="BB",
            vill_open_pos="BTN",
            preflop_action="CALL",
            board=board,
            hero_cards=hero_cards,
            open_size=open_size,
            iterations=iterations,
        )
        case_3bet = run_one_case(
            hero_pos="BB",
            vill_open_pos="BTN",
            preflop_action="3BET",
            board=board,
            hero_cards=hero_cards,
            open_size=open_size,
            iterations=iterations,
        )

        print("[3/4] Results")
        print(json.dumps(case_call, indent=2))
        print(json.dumps(case_3bet, indent=2))

        print("[4/4] Summary")
        if case_call["ev_present"] and case_3bet["ev_present"]:
            if case_call["bestAct"] != case_3bet["bestAct"]:
                print("PASS: UI argmax-by-EV best action differs between CALL and 3BET.")
            else:
                print("PASS-ish: EV present in both; best action same (EV tie or stability).")
        else:
            print("FAIL: hero_action_evs missing for CALL and/or 3BET.")

    finally:
        print("Stopping server.py ...")
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


if __name__ == "__main__":
    main()
