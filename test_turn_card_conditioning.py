"""Validate exact turn_card conditioning in flop->turn mode.

Checks:
- With forced turn_card='Tc', every TURN decision node must have board_key[3]=='Tc'.
- With a different forced turn_card='Th', the TURN decision nodes must have board_key[3]=='Th'.
- CHANCE nodes are allowed to have 3-card board_key (they are not concrete turn variants).

Run:
  python test_turn_card_conditioning.py
"""

from __future__ import annotations

import postflop_service as s

import postflop_evaluator as pe


def _turn_board_card(result: dict) -> set[str]:
    nodes = result.get("nodes") or {}
    out: set[str] = set()
    for _nid, nd in nodes.items():
        if nd.get("street") != "TURN":
            continue
        # Skip CHANCE nodes; the conditioning is represented in their children.
        if nd.get("player") in ("CHANCE", None):
            continue
        bk = nd.get("board_key")
        if isinstance(bk, list) and len(bk) == 4:
            out.add(str(bk[3]))
    return out


def run(turn_card: str) -> set[str]:
    board = ["Ks", "Qs", "Jd"]

    hero_cards = ["Ah", "Ad"]  # AA
    hero_cls = pe.infer_class_from_cards(hero_cards[0], hero_cards[1])
    vill_cls = "KK"

    res = s.solve_postflop_spot(
        board=board,
        hero_pos="BB",
        vill_open_pos="BTN",
        effective_stack=100.0,
        open_size=2.2,
        rake_pct=0.0,
        rake_cap=0.0,
        iterations=4,
        equity_iters=1,
        preflop_action="CALL",
        hero_cards=hero_cards,
        custom_oop_range={hero_cls: 1.0},
        custom_ip_range={vill_cls: 1.0},
        bet_config={
            "expand_partial_sizes": False,
            "check_raise_sizes": [3.0],
            "allow_donk": False,
            "ip_raise_sizes": [2.0],
            "oop_lead_sizes": None,
            "ip_cbet_sizes": [0.5],
            "max_raises_per_street": 1,
        },
        tree_mode="flop_to_turn",
        turn_samples=1,
        turn_card=turn_card,
    )

    return _turn_board_card(res)


def main() -> None:
    cards_tc = run("Tc")
    if cards_tc != {"Tc"}:
        raise SystemExit(f"FAIL: expected TURN board_key[3] to be only 'Tc', got {sorted(cards_tc)}")

    cards_th = run("Th")
    if cards_th != {"Th"}:
        raise SystemExit(f"FAIL: expected TURN board_key[3] to be only 'Th', got {sorted(cards_th)}")

    print("PASS")


if __name__ == "__main__":
    main()
