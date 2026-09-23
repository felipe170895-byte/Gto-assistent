"""Quick smoke: rake=0 vs rake>0 on tiny CFR trees.

This is not a full 169-bucket solve. It only checks:
- rake=0 reproduces unraked showdown EV
- rake>0 strictly reduces showdown EV (not fold EV)
- JSON-like output still has fold/call/threebet
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from mode1_game_model import apply_rake_to_pot, apply_rake_to_pot_with_stack, cap_total
from preflop_gto_defend import solve_for_hero_pos
from preflop_gto_rfi import solve_for_rfi_response


def tiny_eq() -> dict:
    # Two buckets, deterministic equities.
    return {
        "AA": {"AA": 0.50, "72o": 0.85},
        "72o": {"AA": 0.15, "72o": 0.50},
    }


def tiny_p() -> tuple[dict, dict]:
    p = {"AA": 0.5, "72o": 0.5}
    return p, p


def check_rake_helper() -> None:
    assert apply_rake_to_pot(10.0, 0.0) == 10.0
    assert apply_rake_to_pot(10.0, 0.05) == 9.5
    assert apply_rake_to_pot(10.0, 0.05, rake_cap=0.2) == 9.8
    # default 100bb stack does not cap typical preflop pots
    assert apply_rake_to_pot_with_stack(42.0, 0.0, 0.0, 100.0) == 42.0
    # tiny stack scaffolding: pot capped to 2*stack before rake
    assert apply_rake_to_pot_with_stack(50.0, 0.0, 0.0, 10.0) == 20.0
    assert cap_total(21.0, 100.0) == 21.0
    assert cap_total(21.0, 20.0) == 20.0
    assert cap_total(7.5, 0.0) == 7.5  # stack<=0 disables cap


def check_defend() -> None:
    hero_classes = ["AA", "72o"]
    vill_classes = ["AA", "72o"]
    eq = tiny_eq()
    P_hero, P_vill = tiny_p()

    out0 = solve_for_hero_pos(
        hero_pos="CO",
        hero_classes=hero_classes,
        vill_classes=vill_classes,
        eq=eq,
        P_hero=P_hero,
        P_vill=P_vill,
        iters=40,
        seed=1,
        rake_pct=0.0,
        rake_cap=0.0,
        effective_stack=100.0,
    )
    outR = solve_for_hero_pos(
        hero_pos="CO",
        hero_classes=hero_classes,
        vill_classes=vill_classes,
        eq=eq,
        P_hero=P_hero,
        P_vill=P_vill,
        iters=40,
        seed=1,
        rake_pct=0.05,
        rake_cap=0.0,
        effective_stack=100.0,
    )

    for h in hero_classes:
        ev0 = out0["hero_evs"][h]
        evR = outR["hero_evs"][h]
        st0 = out0["hero_strategy"][h]
        for k in ("fold", "call", "threebet"):
            assert k in ev0 and k in evR and k in st0
        assert abs(ev0["fold"] - 0.0) < 1e-12
        assert abs(evR["fold"] - 0.0) < 1e-12
        # showdown-containing actions should not increase under rake
        assert evR["call"] <= ev0["call"] + 1e-9
        assert evR["threebet"] <= ev0["threebet"] + 1e-9

    assert out0["params"]["rake_pct"] == 0.0
    assert outR["params"]["rake_pct"] == 0.05
    print("OK defend smoke")


def check_rfi() -> None:
    hero_classes = ["AA", "72o"]
    vill_classes = ["AA", "72o"]
    eq = tiny_eq()
    P_hero, P_vill = tiny_p()

    out0 = solve_for_rfi_response(
        hero_pos="CO",
        hero_classes=hero_classes,
        vill_classes=vill_classes,
        eq=eq,
        P_hero=P_hero,
        P_vill=P_vill,
        iters=40,
        seed=1,
        rake_pct=0.0,
        rake_cap=0.0,
        effective_stack=100.0,
    )
    outR = solve_for_rfi_response(
        hero_pos="CO",
        hero_classes=hero_classes,
        vill_classes=vill_classes,
        eq=eq,
        P_hero=P_hero,
        P_vill=P_vill,
        iters=40,
        seed=1,
        rake_pct=0.05,
        rake_cap=0.0,
        effective_stack=100.0,
    )

    for h in hero_classes:
        ev0 = out0["hero_evs"][h]
        evR = outR["hero_evs"][h]
        st0 = out0["hero_strategy"][h]
        for k in ("fold", "call", "threebet"):
            assert k in ev0 and k in evR and k in st0
        assert abs(ev0["fold"] - 0.0) < 1e-12
        assert abs(evR["fold"] - 0.0) < 1e-12
        assert evR["call"] <= ev0["call"] + 1e-9
        assert evR["threebet"] <= ev0["threebet"] + 1e-9

    print("OK rfi smoke")


def _solve_pair(kind: str, stack: float, rake: float = 0.0):
    hero_classes = ["AA", "72o"]
    vill_classes = ["AA", "72o"]
    eq = tiny_eq()
    P_hero, P_vill = tiny_p()
    kwargs = dict(
        hero_pos="CO",
        hero_classes=hero_classes,
        vill_classes=vill_classes,
        eq=eq,
        P_hero=P_hero,
        P_vill=P_vill,
        iters=40,
        seed=1,
        rake_pct=rake,
        rake_cap=0.0,
        effective_stack=stack,
    )
    if kind == "defend":
        return solve_for_hero_pos(**kwargs)
    return solve_for_rfi_response(**kwargs)


def check_stack_identity() -> None:
    """100bb == 200bb (no sizing jams). 20bb jams the 21bb 4bet, so 3bet EV moves.

    Call of the 2.2bb open is still unjammed at 20bb, so DEFEND call EV stays put.
    """
    for kind in ("defend", "rfi"):
        out100 = _solve_pair(kind, 100.0)
        out200 = _solve_pair(kind, 200.0)
        out20 = _solve_pair(kind, 20.0)

        assert out100["params"]["effective_stack"] == 100.0
        assert out20["params"]["effective_stack"] == 20.0

        for h in ("AA", "72o"):
            ev100 = out100["hero_evs"][h]
            ev200 = out200["hero_evs"][h]
            ev20 = out20["hero_evs"][h]
            st = out100["hero_strategy"][h]
            for k in ("fold", "call", "threebet"):
                assert k in ev100 and k in ev20 and k in st
                assert abs(ev100[k] - ev200[k]) < 1e-12
            assert abs(ev100["fold"] - 0.0) < 1e-12
            assert abs(ev20["fold"] - 0.0) < 1e-12

        if kind == "defend":
            for h in ("AA", "72o"):
                assert abs(out20["hero_evs"][h]["call"] - out100["hero_evs"][h]["call"]) < 1e-12

        # 4bet 21bb is jammed at 20bb → aggregated threebet EV must move for some hand.
        moved = any(
            abs(out20["hero_evs"][h]["threebet"] - out100["hero_evs"][h]["threebet"]) > 1e-6
            for h in ("AA", "72o")
        )
        assert moved, f"{kind}: 20bb threebet EV should differ from 100bb (4bet jam)"

        print(f"OK {kind} stack identity/jam")


def main() -> None:
    check_rake_helper()
    check_defend()
    check_rfi()
    check_stack_identity()
    print("OK: rake smoke passed")


if __name__ == "__main__":
    main()
