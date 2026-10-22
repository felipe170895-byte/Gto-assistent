from __future__ import annotations

import math
from typing import Dict, Any, List, Tuple

import postflop_service as ps
import postflop_evaluator as pe


def best_action_by_ev(hero_action_evs: Dict[str, float]) -> Tuple[str, float]:
    if not hero_action_evs:
        return "", float("-inf")
    best_act = max(hero_action_evs.items(), key=lambda kv: kv[1])[0]
    return best_act, float(hero_action_evs[best_act])


def ui_pot_and_investments(preflop_action: str, open_size: float) -> Tuple[float, float, float]:
    """Replicate poker_gto_mode1_app.html hand-continuous overrides.

    Assumes the same positions as the JS (BB vs BTN open), where:
    - CALL path uses potPreOverride = 1.5 + 2*open - 0.5
      and investments: oop_invested_pre = open - 0.5, ip_invested_pre = open
    - 3BET path uses potPreOverride = 1.5 + open + threebetSize + (threebetSize - 0.5)
      and investments: oop_invested_pre = 0.5 + threebetSize,
                        ip_invested_pre = open + (threebetSize - 0.5)

    NOTE: this is independent of whether the hero is OOP or IP at flop;
    it just provides consistent pot/invest figures for the solver.
    """

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


def validate_scenario(
    *,
    hero_pos: str,
    vill_open_pos: str,
    preflop_action: str,
    board: List[str],
    hero_cards: List[str],
    open_size: float,
    iterations: int,
    equity_iters: int,
    custom_oop_range: Dict[str, float],
) -> Dict[str, Any]:
    pot_pre_override, oop_invested_pre, ip_invested_pre = ui_pot_and_investments(preflop_action, open_size)

    bet_config = {
        "oop_lead_sizes": [0.33, 0.75],
        "ip_cbet_sizes": [0.33, 0.75],
        "check_raise_sizes": [3.0],
        "allow_donk": False,
    }

    res = ps.solve_postflop_spot(
        board=board,
        hero_pos=hero_pos,
        vill_open_pos=vill_open_pos,
        effective_stack=100.0,
        open_size=open_size,
        rake_pct=0.0,
        rake_cap=3.0,
        bet_config=bet_config,
        iterations=iterations,
        equity_iters=equity_iters,
        pot_pre_override=pot_pre_override,
        hero_cards=hero_cards,
        oop_invested_pre=oop_invested_pre,
        ip_invested_pre=ip_invested_pre,
        preflop_action=preflop_action,
        custom_oop_range=custom_oop_range,
        custom_ip_range=None,
    )

    # poker_gto_mode1_app.html selection:
    #   activeNodeId = heroIsOop ? 'root' : 'x'
    # for our test scenarios:
    hero_is_oop = hero_pos in ("BB", "SB") or (hero_pos == "CO" and vill_open_pos == "BTN")
    active_node_id = "root" if hero_is_oop else "x"

    node = (res.get("nodes") or {}).get(active_node_id) or {}

    hero_action_evs = node.get("hero_action_evs") or {}
    hero_diag = node.get("hero_diag") or {}

    best_act, best_ev = best_action_by_ev(hero_action_evs)

    ev_present = bool(hero_action_evs)
    w_sum = float(hero_diag.get("hero_weight_sum") or 0.0)

    return {
        "hero_pos": hero_pos,
        "vill_open_pos": vill_open_pos,
        "preflop_action": preflop_action,
        "active_node_id": active_node_id,
        "root_or_x_player": node.get("player"),
        "hero_action_evs_keys": sorted(list(hero_action_evs.keys())),
        "hero_diag": {
            "hero_combo_present": hero_diag.get("hero_combo_present"),
            "hero_combo_key": hero_diag.get("hero_combo_key"),
            "hero_side_match": hero_diag.get("hero_side_match"),
            "hero_weight_sum": w_sum,
        },
        "ev_present": ev_present,
        "bestAct": best_act,
        "bestEv": best_ev,
    }


def main():
    open_size = 2.2
    iterations = 25
    equity_iters = 8

    hero_cards = ["Ah", "Kh"]
    board = ["2c", "7d", "9s"]

    # Force epsilon injection path:
    # hero_def_range starts from custom_oop_range (wrong class),
    # then postflop_service adds a tiny epsilon for the hero's actual class.
    custom_oop_range = {"AA": 1.0}

    scenarios = [
        # Hero OOP: hero BB vs BTN open
        ("BB", "BTN"),
        # Hero IP at flop: hero BTN vs CO open
        ("BTN", "CO"),
    ]

    all_results: List[Dict[str, Any]] = []

    for hero_pos, vill_open_pos in scenarios:
        for preflop_action in ["CALL", "3BET"]:
            r = validate_scenario(
                hero_pos=hero_pos,
                vill_open_pos=vill_open_pos,
                preflop_action=preflop_action,
                board=board,
                hero_cards=hero_cards,
                open_size=open_size,
                iterations=iterations,
                equity_iters=equity_iters,
                custom_oop_range=custom_oop_range,
            )
            all_results.append(r)

    print("=== Task #16 UI-mimic check (EV argmax over hero_action_evs) ===")
    for r in all_results:
        print(
            f"\n[{r['hero_pos']} vs {r['vill_open_pos']} | {r['preflop_action']}] "
            f"activeNode={r['active_node_id']} player={r['root_or_x_player']}\n"
            f"EV present: {r['ev_present']} keys={r['hero_action_evs_keys']}\n"
            f"bestAct={r['bestAct']} bestEv={r['bestEv']:.6f}\n"
            f"diag={r['hero_diag']}"
        )

    # Basic pass/fail for EV availability in both CALL and 3BET paths.
    fail_msgs = []
    for hero_pos, vill_open_pos in scenarios:
        calls = [r for r in all_results if r["hero_pos"] == hero_pos and r["vill_open_pos"] == vill_open_pos and r["preflop_action"] == "CALL"]
        three = [r for r in all_results if r["hero_pos"] == hero_pos and r["vill_open_pos"] == vill_open_pos and r["preflop_action"] == "3BET"]
        if calls and three:
            if not calls[0]["ev_present"]:
                fail_msgs.append(f"EV missing on CALL for hero_pos={hero_pos}")
            if not three[0]["ev_present"]:
                fail_msgs.append(f"EV missing on 3BET for hero_pos={hero_pos}")

    if fail_msgs:
        print("\n[RESULT] FAIL")
        for m in fail_msgs:
            print("-", m)
    else:
        print("\n[RESULT] PASS: EV available on UI-selected node for CALL and 3BET in both OOP and IP hero scenarios.")


if __name__ == "__main__":
    main()
