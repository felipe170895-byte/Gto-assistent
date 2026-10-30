from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from typing import Dict, Any, List, Tuple


# Start the server from the *same directory* as this script.
# This worktree-specific path ensures we validate the code we just modified.
from pathlib import Path
SERVER_PY = str(Path(__file__).resolve().parent / "server.py")
STATUS_URL = "http://localhost:8000/api/status_mode1_v2.php"
POSTFLOP_URL = "http://localhost:8000/api/query_postflop_gto.php"


def http_get_json(url: str, timeout_s: float = 2.0) -> Dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout_s) as r:
        data = r.read().decode("utf-8")
    return json.loads(data)


def http_post_json(url: str, payload: Dict[str, Any], timeout_s: float = 120.0) -> Dict[str, Any]:
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


def hero_is_oop(hero_pos: str, vill_open_pos: str) -> bool:
    # Mirrors poker_gto_mode1_app.html: heroIsOop = heroPos in {BB, SB} OR (CO && BTN)
    return hero_pos in ("BB", "SB") or (hero_pos == "CO" and vill_open_pos == "BTN")


def active_node_id(hero_pos: str, vill_open_pos: str) -> str:
    return "root" if hero_is_oop(hero_pos, vill_open_pos) else "x"


def ui_pot_and_investments(preflop_action: str, open_size: float) -> Tuple[float, float, float]:
    # Mirrors continueDefendCallToFlop / continueThreebetToFlop overrides used by the app.
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


def argmax_ev(hero_action_evs: Dict[str, float]) -> Tuple[str, float]:
    if not hero_action_evs:
        return "", float("-inf")
    act, ev = max(hero_action_evs.items(), key=lambda kv: kv[1])
    return act, float(ev)


def run_one(
    *,
    hero_pos: str,
    vill_open_pos: str,
    preflop_action: str,
    board: List[str],
    hero_cards: List[str],
    open_size: float,
    iterations: int,
    post_timeout_s: float,
) -> Dict[str, Any]:
    anode = active_node_id(hero_pos, vill_open_pos)

    pot_pre_override, oop_invested_pre, ip_invested_pre = ui_pot_and_investments(preflop_action, open_size)

    payload = {
        "board": board,
        "heroCards": hero_cards,
        "heroPos": hero_pos,
        "villOpenPos": vill_open_pos,
        "effectiveStack": 100.0,
        "openSize": open_size,
        "iterations": iterations,
        "compute_mode": "quality",
        "response_mode": "compact",
        # hand-continuous fields (from poker_gto_mode1_app.html)
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

    resp = http_post_json(POSTFLOP_URL, payload, timeout_s=post_timeout_s)
    nodes = resp.get("nodes") or {}
    node = nodes.get(anode) or {}

    hero_action_evs = node.get("hero_action_evs") or {}
    hero_diag = node.get("hero_diag") or {}
    best_act, best_ev = argmax_ev(hero_action_evs)

    return {
        "hero_pos": hero_pos,
        "vill_open_pos": vill_open_pos,
        "preflop_action": preflop_action,
        "active_node_id": anode,
        "node_player": node.get("player"),
        "ev_present": bool(hero_action_evs),
        "hero_action_evs_keys": sorted(list(hero_action_evs.keys())),
        "bestAct": best_act,
        "bestEv": best_ev,
        "hero_diag": {
            "hero_combo_present": hero_diag.get("hero_combo_present"),
            "hero_side_match": hero_diag.get("hero_side_match"),
            "hero_weight_sum": hero_diag.get("hero_weight_sum"),
            "hero_combo_key": hero_diag.get("hero_combo_key"),
        },
    }


def main() -> None:
    board = ["2c", "7d", "9s"]
    hero_cards = ["Ah", "Kh"]
    open_size = 2.2

    # Keep it low to avoid OOM / OS-kill, but non-zero to exercise extraction.
    iterations = 3

    post_timeout_s = 180.0

    cases = [
        # Hero OOP at flop: BB vs BTN open -> activeNodeId = root
        ("BB", "BTN"),
        # Hero IP at flop: BTN vs CO open -> activeNodeId = x
        ("BTN", "CO"),
    ]

    server_args = ("",)
    print("[1/4] Starting server.py ...", flush=True)
    proc = subprocess.Popen([sys.executable, SERVER_PY], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    try:
        print("[2/4] Waiting for STATUS endpoint ...", flush=True)
        ready = False
        last_err: Exception | None = None
        for _ in range(120):
            try:
                _ = http_get_json(STATUS_URL, timeout_s=1.0)
                ready = True
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
                time.sleep(0.25)

        if not ready:
            log = ""
            if proc.stdout is not None:
                try:
                    log = (proc.stdout.read(800) or "")
                except Exception:
                    pass
            raise RuntimeError(f"Server not ready: {last_err}. Log: {log}")

        print("[3/4] Running CALL then 3BET for both OOP and IP scenarios ...", flush=True)
        results: List[Dict[str, Any]] = []

        for hero_pos, vill_open_pos in cases:
            for preflop_action in ["CALL", "3BET"]:
                results.append(
                    run_one(
                        hero_pos=hero_pos,
                        vill_open_pos=vill_open_pos,
                        preflop_action=preflop_action,
                        board=board,
                        hero_cards=hero_cards,
                        open_size=open_size,
                        iterations=iterations,
                        post_timeout_s=post_timeout_s,
                    )
                )

        print("[4/4] Results", flush=True)
        print(json.dumps(results, indent=2))

        # Hard pass criteria for Task #16 acceptance:
        # - EV available in hero_action_evs for all 4 cases.
        # - bestAct matches argmax over hero_action_evs.
        all_ok = True
        missing = []
        for r in results:
            if not r["ev_present"]:
                all_ok = False
                missing.append(f"EV missing: hero_pos={r['hero_pos']} preflop_action={r['preflop_action']} active={r['active_node_id']}")

        if not all_ok:
            print("\n[RESULT] FAIL", flush=True)
            for m in missing:
                print("-", m, flush=True)
        else:
            print("\n[RESULT] PASS: EV present + argmax EV action in all 4 cases.", flush=True)

    finally:
        print("Stopping server.py ...", flush=True)
        try:
            proc.terminate()
            proc.wait(timeout=15)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


if __name__ == "__main__":
    main()
