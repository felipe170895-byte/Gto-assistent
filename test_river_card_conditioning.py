"""Validate exact river_card conditioning in flop->turn CFR+ mode.

This repo models the full hand-continuous subgame for tree_mode="flop_to_turn"
now as FLOP -> TURN -> RIVER (with TURN and RIVER CHANCE nodes).

Checks:
- With forced river_card, every RIVER decision node must have board_key[4]==river_card.
- RIVER CHANCE nodes are allowed to have board_key=None.

Run:
  python test_river_card_conditioning.py
"""

from __future__ import annotations

import postflop_service as s

import postflop_evaluator as pe


def _river_board_card(result: dict) -> set[str]:
    nodes = result.get("nodes") or {}
    out: set[str] = set()

    for _nid, nd in nodes.items():
        if nd.get("street") != "RIVER":
            continue
        # Skip CHANCE nodes; the conditioning is represented in their children.
        if nd.get("player") in ("CHANCE", None):
            continue

        bk = nd.get("board_key")
        if isinstance(bk, list) and len(bk) == 5:
            out.add(str(bk[4]))

    return out


def run(*, river_card: str, turn_card: str = "Tc") -> set[str]:
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
        river_samples=1,
        river_card=river_card,
    )

    return _river_board_card(res)


def main() -> None:
    cards = run(river_card="2c")
    if cards != {"2c"}:
        raise SystemExit(
            f"FAIL: expected RIVER board_key[4] to be only '2c', got {sorted(cards)}"
        )

    cards2 = run(river_card="2d")
    if cards2 != {"2d"}:
        raise SystemExit(
            f"FAIL: expected RIVER board_key[4] to be only '2d', got {sorted(cards2)}"
        )

    print("PASS")


if __name__ == "__main__":
    main()
