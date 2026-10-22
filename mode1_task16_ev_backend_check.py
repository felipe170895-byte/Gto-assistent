import json
from typing import Dict, Any, List, Tuple

import postflop_service as ps
import postflop_evaluator as pe


def pot_and_investments_for_hand_continuous_call(open_size: float) -> Tuple[float, float, float]:
    """UI convention: BB vs BTN open, hero is BB defending via CALL."""
    # pot_pre_override: 1.5 + 2*open - 0.5
    pot_pre = 1.5 + 2.0 * open_size - 0.5
    # UI: ipInvestedPre = openSize (BTN invested), oopInvestedPre = openSize - 0.5 (BB invested)
    oop_inv = open_size - 0.5
    ip_inv = open_size
    return pot_pre, oop_inv, ip_inv


def pot_and_investments_for_hand_continuous_3bet(open_size: float, threebet_size_mult: float = 3.0) -> Tuple[float, float, float]:
    """UI convention: BB vs BTN, hero is BB 3betting and BTN calls."""
    threebet_size = open_size * threebet_size_mult
    # UI potPreOverride: 1.5 + open + threebet + (threebet - 0.5)
    pot_pre = 1.5 + open_size + threebet_size + (threebet_size - 0.5)
    # UI: oopInvestedPre = 0.5 + threebetSize, ipInvestedPre = open + (threebetSize - 0.5)
    oop_inv = 0.5 + threebet_size
    ip_inv = open_size + (threebet_size - 0.5)
    return pot_pre, oop_inv, ip_inv


def best_action_by_ev(hero_action_evs: Dict[str, float]) -> Tuple[str, float]:
    if not hero_action_evs:
        return "", float("-inf")
    best_act = max(hero_action_evs.items(), key=lambda kv: kv[1])[0]
    return best_act, float(hero_action_evs[best_act])


def run_case(board: List[str], hero_cards: List[str], open_size: float, iterations: int) -> None:
    hero_pos = "BB"
    vill_open_pos = "BTN"
    stack = 100.0

    pot_call, oop_inv_call, ip_inv_call = pot_and_investments_for_hand_continuous_call(open_size)
    pot_3bet, oop_inv_3bet, ip_inv_3bet = pot_and_investments_for_hand_continuous_3bet(open_size)

    hero_ev_results = {}

    for preflop_action, pot_pre, oop_inv, ip_inv in [
        ("CALL", pot_call, oop_inv_call, ip_inv_call),
        ("3BET", pot_3bet, oop_inv_3bet, ip_inv_3bet),
    ]:
        res = ps.solve_postflop_spot(
            board=board,
            hero_pos=hero_pos,
            vill_open_pos=vill_open_pos,
            effective_stack=stack,
            open_size=open_size,
            iterations=iterations,
            equity_iters=20,
            rake_pct=0.0,
            rake_cap=3.0,
            pot_pre_override=pot_pre,
            hero_cards=hero_cards,
            oop_invested_pre=oop_inv,
            ip_invested_pre=ip_inv,
            preflop_action=preflop_action,
        )

        nodes = res.get("nodes") or {}
        # For hero BB, UI starts at 'root' (OOP node).
        node = nodes.get("root") or {}
        hero_action_evs = node.get("hero_action_evs") or {}
        hero_diag = node.get("hero_diag") or {}

        best_act, best_ev = best_action_by_ev(hero_action_evs)

        hero_ev_results[preflop_action] = {
            "bestAct": best_act,
            "bestEv": best_ev,
            "hero_action_evs": hero_action_evs,
            "hero_diag": hero_diag,
        }

    print("\n=== Task #16 backend EV check ===")
    print("board:", board)
    print("hero_cards:", hero_cards)
    print("open_size:", open_size)
    print("iterations:", iterations)
    print("hero_pos:", hero_pos, "vill_open_pos:", vill_open_pos)

    for preflop_action in ["CALL", "3BET"]:
        r = hero_ev_results[preflop_action]
        print(f"\n[{preflop_action}] best: {r['bestAct']} EV={r['bestEv']:.6f}")
        print("hero_diag:", {
            "hero_combo_present": r["hero_diag"].get("hero_combo_present"),
            "hero_combo_key": r["hero_diag"].get("hero_combo_key"),
            "hero_side_match": r["hero_diag"].get("hero_side_match"),
            "hero_weight_sum": r["hero_diag"].get("hero_weight_sum"),
        })
        print("hero_action_evs keys:", sorted(r["hero_action_evs"].keys()))

    call_best = hero_ev_results["CALL"]["bestAct"]
    three_best = hero_ev_results["3BET"]["bestAct"]
    call_ev = hero_ev_results["CALL"]["bestEv"]
    three_ev = hero_ev_results["3BET"]["bestEv"]

    if not call_best or not three_best:
        print("\n[RESULT] FAIL: hero_action_evs missing for one of the preflop actions.")
        return

    if call_best == three_best:
        print("\n[RESULT] WARNING: best action same for CALL and 3BET (still OK if EV values differ).")
    else:
        print("\n[RESULT] PASS: best action differs by EV between CALL and 3BET.")


def main():
    open_size = 2.2
    iterations = 80

    hero_cards = ["Ah", "Kh"]

    boards = [
        ["2c", "7d", "9s"],
        ["As", "7d", "2c"],
        ["Jh", "Tc", "2d"],
        ["Qd", "Jc", "4s"],
        ["Ks", "9d", "3c"],
    ]

    for b in boards:
        # Skip boards that collide with hero cards.
        if any(c in hero_cards for c in b):
            continue
        run_case(b, hero_cards, open_size=open_size, iterations=iterations)


if __name__ == "__main__":
    main()
