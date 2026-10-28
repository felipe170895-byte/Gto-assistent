"""Sanity test: ensure hero_action_evs exists on RIVER nodes."""

from __future__ import annotations

import postflop_service as s

import postflop_evaluator as pe


def main() -> None:
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
        turn_card="Tc",
        river_samples=1,
        river_card="2c",
    )

    nodes = res.get("nodes") or {}
    river_decision_nodes = [
        n for n in nodes.values()
        if n.get("street") == "RIVER" and n.get("player") in ("OOP", "IP", "TERMINAL")
    ]

    hero_evs_nodes = [n for n in river_decision_nodes if n.get("hero_action_evs")]
    if len(hero_evs_nodes) <= 0:
        raise SystemExit("FAIL: expected at least one RIVER node with hero_action_evs")

    print("PASS")


if __name__ == "__main__":
    main()
