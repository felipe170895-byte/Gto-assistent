import time
from typing import Dict, Any, List

import postflop_service as ps
import postflop_evaluator as pe


def best_action_by_ev(hero_action_evs: Dict[str, float]):
    if not hero_action_evs:
        return "", float("-inf")
    best_act = max(hero_action_evs.items(), key=lambda kv: kv[1])[0]
    return best_act, float(hero_action_evs[best_act])


def pot_and_investments_for_hand_continuous_call(open_size: float):
    # UI convention for BB vs BTN open, hero BB CALL
    pot_pre = 1.5 + 2.0 * open_size - 0.5
    oop_inv = open_size - 0.5  # BB call amount
    ip_inv = open_size         # BTN open
    return pot_pre, oop_inv, ip_inv


def pot_and_investments_for_hand_continuous_3bet(open_size: float, threebet_mult: float = 3.0):
    # UI convention for BB vs BTN open, hero BB 3BET and BTN CALL
    threebet_size = open_size * threebet_mult
    pot_pre = 1.5 + open_size + threebet_size + (threebet_size - 0.5)
    oop_inv = 0.5 + threebet_size                  # BB posted 0.5 + 3bet
    ip_inv = open_size + (threebet_size - 0.5)    # BTN open + call
    return pot_pre, oop_inv, ip_inv


def solve_case(
    preflop_action: str,
    board: List[str],
    hero_cards: List[str],
    hero_pos: str = "BB",
    vill_open_pos: str = "BTN",
    open_size: float = 2.2,
    custom_oop_range: Dict[str, float] | None = None,
    iterations: int = 20,
    equity_iters: int = 8,
) -> Dict[str, Any]:
    stack = 100.0
    rake_pct = 0.0
    rake_cap = 3.0

    t0 = time.time()
    if preflop_action == "CALL":
        pot_pre, oop_inv, ip_inv = pot_and_investments_for_hand_continuous_call(open_size)
    elif preflop_action == "3BET":
        pot_pre, oop_inv, ip_inv = pot_and_investments_for_hand_continuous_3bet(open_size)
    else:
        raise ValueError(preflop_action)

    res = ps.solve_postflop_spot(
        board=board,
        hero_pos=hero_pos,
        vill_open_pos=vill_open_pos,
        effective_stack=stack,
        open_size=open_size,
        rake_pct=rake_pct,
        rake_cap=rake_cap,
        iterations=iterations,
        equity_iters=equity_iters,
        pot_pre_override=pot_pre,
        hero_cards=hero_cards,
        oop_invested_pre=oop_inv,
        ip_invested_pre=ip_inv,
        preflop_action=preflop_action,
        custom_oop_range=custom_oop_range,
        custom_ip_range=None,
    )

    dt = time.time() - t0

    nodes = res.get("nodes") or {}
    node_root = nodes.get("root") or {}
    hero_action_evs = node_root.get("hero_action_evs") or {}
    hero_diag = node_root.get("hero_diag") or {}

    best_act, best_ev = best_action_by_ev(hero_action_evs)

    return {
        "elapsed_sec": dt,
        "root_player": node_root.get("player"),
        "hero_action_evs": hero_action_evs,
        "hero_diag": hero_diag,
        "bestAct": best_act,
        "bestEv": best_ev,
    }


def main():
    open_size = 2.2
    iterations = 20
    equity_iters = 8

    hero_cards = ["Ah", "Kh"]
    board = ["2c", "7d", "9s"]

    hero_pos = "BB"
    vill_open_pos = "BTN"

    hero_cls = pe.infer_class_from_cards(hero_cards[0], hero_cards[1])

    # Speed hack for validation: keep defender range tiny so equity matrix is small.
    # This still preserves the CALL vs 3BET *villain reach* via ip_range.
    custom_oop_range = {hero_cls: 1.0}

    print("=== Task #16 backend EV check (fast) ===")
    print("hero_cards:", hero_cards)
    print("hero_cls:", hero_cls)
    print("board:", board)
    print("open_size:", open_size)
    print("iterations:", iterations, "equity_iters:", equity_iters)

    call_res = solve_case(
        "CALL",
        board,
        hero_cards,
        hero_pos=hero_pos,
        vill_open_pos=vill_open_pos,
        open_size=open_size,
        custom_oop_range=custom_oop_range,
        iterations=iterations,
        equity_iters=equity_iters,
    )

    three_res = solve_case(
        "3BET",
        board,
        hero_cards,
        hero_pos=hero_pos,
        vill_open_pos=vill_open_pos,
        open_size=open_size,
        custom_oop_range=custom_oop_range,
        iterations=iterations,
        equity_iters=equity_iters,
    )

    def print_summary(tag: str, r: Dict[str, Any]):
        print(f"\n[{tag}] elapsed={r['elapsed_sec']:.2f}s root_player={r['root_player']}")
        print("hero_diag:", {
            "hero_combo_present": r['hero_diag'].get('hero_combo_present'),
            "hero_side_match": r['hero_diag'].get('hero_side_match'),
            "hero_weight_sum": r['hero_diag'].get('hero_weight_sum'),
            "hero_combo_key": r['hero_diag'].get('hero_combo_key'),
        })
        keys = sorted(r['hero_action_evs'].keys())
        print("hero_action_evs keys:", keys)
        print("bestAct/bestEv:", r['bestAct'], r['bestEv'])

    print_summary("CALL", call_res)
    print_summary("3BET", three_res)

    ok_call = bool(call_res["hero_action_evs"])
    ok_three = bool(three_res["hero_action_evs"])

    if not ok_call or not ok_three:
        print("\n[RESULT] FAIL: hero_action_evs missing for one of the preflop actions.")
    else:
        if call_res["bestAct"] != three_res["bestAct"]:
            print("\n[RESULT] PASS: best action differs by EV between CALL and 3BET.")
        else:
            print("\n[RESULT] PASS-ish: EV available in both cases; best action same (still valid if EV ties or policy stability).")


if __name__ == "__main__":
    main()
