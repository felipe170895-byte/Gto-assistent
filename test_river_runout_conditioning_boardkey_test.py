"""Validate full board_key conditioning for flop->turn CFR+ extended to river.

Checks:
- With forced turn_card and forced river_card, every RIVER decision node
  must have board_key length 5 and board_key[3]==turn_card, board_key[4]==river_card.
- RIVER CHANCE nodes are allowed to have board_key None.

Run:
  python test_river_runout_conditioning_boardkey_test.py
"""

from __future__ import annotations

import postflop_service as s

import postflop_evaluator as pe


def _collect_bk(result: dict, *, street: str) -> set[tuple[str, str]]:
    nodes = result.get("nodes") or {}
    out: set[tuple[str, str]] = set()
    for _nid, nd in nodes.items():
        if nd.get("street") != street:
            continue
        if nd.get("player") in ("CHANCE", None):
            continue
        bk = nd.get("board_key")
        if isinstance(bk, list) and len(bk) == 5:
            out.add((str(bk[3]), str(bk[4])))
    return out


def main() -> None:
    board = ["Ks", "Qs", "Jd"]

    hero_cards = ["Ah", "Ad"]  # AA
    hero_cls = pe.infer_class_from_cards(hero_cards[0], hero_cards[1])
    vill_cls = "KK"

    turn_card = "Tc"
    river_card = "2c"

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
        river_samples=1,
        river_card=river_card,
    )

    river_pairs = _collect_bk(res, street="RIVER")
    if river_pairs != {(turn_card, river_card)}:
        raise SystemExit(
            f"FAIL: expected only (turn,river)=({turn_card!r},{river_card!r}); got {sorted(river_pairs)}"
        )

    print("PASS")


if __name__ == "__main__":
    main()
