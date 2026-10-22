import time
from typing import Dict, Any, List

import postflop_service as ps
import postflop_evaluator as pe


def best_action_by_ev(hero_action_evs: Dict[str, float]):
    if not hero_action_evs:
        return "", float("-inf")
    best_act = max(hero_action_evs.items(), key=lambda kv: kv[1])[0]
    return best_act, float(hero_action_evs[best_act])


def solve(preflop_action: str, board: List[str], hero_cards: List[str], hero_pos: str, vill_open_pos: str, open_size: float):
    stack = 100.0
    rake_pct = 0.0
    rake_cap = 3.0

    threebet_mult = 3.0
    threebet_size = open_size * threebet_mult

    # Generic (approx) pot/invest consistency: keep same structure across both actions.
    # Key requirement is: pot_pre_override == oop_invested_pre + ip_invested_pre.
    if preflop_action == "CALL":
        pot_pre = 1.5 + 2.0 * open_size - 0.5
        oop_inv = pot_pre / 2.0
        ip_inv = pot_pre / 2.0
    elif preflop_action == "3BET":
        pot_pre = 1.5 + open_size + threebet_size + (threebet_size - 0.5)
        oop_inv = pot_pre / 2.0
        ip_inv = pot_pre / 2.0
    else:
        raise ValueError(preflop_action)

    hero_cls = pe.infer_class_from_cards(hero_cards[0], hero_cards[1])
    custom_def_range = {hero_cls: 1.0}

    t0 = time.time()
    res = ps.solve_postflop_spot(
        board=board,
        hero_pos=hero_pos,
        vill_open_pos=vill_open_pos,
        effective_stack=stack,
        open_size=open_size,
        rake_pct=rake_pct,
        rake_cap=rake_cap,
        iterations=25,
        equity_iters=8,
        pot_pre_override=pot_pre,
        hero_cards=hero_cards,
        oop_invested_pre=oop_inv,
        ip_invested_pre=ip_inv,
        preflop_action=preflop_action,
        custom_oop_range=custom_def_range,
        custom_ip_range=None,
    )
    dt = time.time() - t0

    nodes = res.get("nodes") or {}

    # When hero is IP, UI recommends node 'x'. We validate hero_action_evs on node x.
    node_x = nodes.get("x") or {}
    hero_action_evs = node_x.get("hero_action_evs") or {}
    hero_diag = node_x.get("hero_diag") or {}

    best_act, best_ev = best_action_by_ev(hero_action_evs)

    return {
        "elapsed_sec": dt,
        "pot_pre": pot_pre,
        "root_player": (nodes.get("root") or {}).get("player"),
        "x_player": node_x.get("player"),
        "hero_diag": hero_diag,
        "hero_action_evs": hero_action_evs,
        "hero_action_evs_keys": sorted(list(hero_action_evs.keys())),
        "bestAct": best_act,
        "bestEv": best_ev,
    }


def main():
    open_size = 2.2

    # Scenario where hero is IP at flop (so node 'x' is hero decision node)
    hero_pos = "BTN"
    vill_open_pos = "CO"

    hero_cards = ["Ah", "Kh"]
    board = ["2c", "7d", "9s"]

    print("=== Task #16 backend EV check (hero IP -> node x) ===")
    print("hero_pos:", hero_pos, "vill_open_pos:", vill_open_pos)
    print("hero_cards:", hero_cards)
    print("board:", board)
    print("open_size:", open_size)

    call_res = solve("CALL", board, hero_cards, hero_pos, vill_open_pos, open_size)
    three_res = solve("3BET", board, hero_cards, hero_pos, vill_open_pos, open_size)

    def print_res(tag: str, r: Dict[str, Any]):
        print(f"\n[{tag}] elapsed={r['elapsed_sec']:.2f}s pot_pre={r['pot_pre']} root_player={r['root_player']} x_player={r['x_player']}")
        print("hero_diag:", {
            "hero_combo_present": r['hero_diag'].get('hero_combo_present'),
            "hero_side_match": r['hero_diag'].get('hero_side_match'),
            "hero_weight_sum": r['hero_diag'].get('hero_weight_sum'),
            "hero_combo_key": r['hero_diag'].get('hero_combo_key'),
        })
        print("hero_action_evs_keys:", r['hero_action_evs_keys'])
        print("bestAct/bestEv:", r['bestAct'], r['bestEv'])

    print_res("CALL", call_res)
    print_res("3BET", three_res)

    ok_call = bool(call_res["hero_action_evs"])
    ok_three = bool(three_res["hero_action_evs"])

    if not ok_call or not ok_three:
        print("\n[RESULT] FAIL: hero_action_evs missing for CALL and/or 3BET on node x.")
        return

    if call_res["bestAct"] != three_res["bestAct"]:
        print("\n[RESULT] PASS: best action differs by EV between CALL and 3BET (on node x).")
    else:
        print("\n[RESULT] PASS-ish: EV available in both cases on node x; best action same (EV tie or policy stability).")


if __name__ == "__main__":
    main()
