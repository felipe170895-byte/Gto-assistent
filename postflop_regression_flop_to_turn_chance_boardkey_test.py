import postflop_service as s
import postflop_evaluator as pe


def count_nodes(res, *, player=None, street=None):
    nodes = res.get("nodes") or {}
    cnt = 0
    for _nid, nd in nodes.items():
        if player is not None and nd.get("player") != player:
            continue
        if street is not None and nd.get("street") != street:
            continue
        cnt += 1
    return cnt


def all_turn_subtree_boardkeys_have_len4(res) -> bool:
    nodes = res.get("nodes") or {}
    for _nid, nd in nodes.items():
        if nd.get("street") != "TURN":
            continue
        # Any node inside the sampled TURN subtree should be stamped with
        # a concrete 4-card board_key.
        if nd.get("player") not in ("OOP", "IP", "TERMINAL"):
            continue
        bk = nd.get("board_key")
        if bk is None:
            return False
        if not isinstance(bk, list):
            return False
        if len(bk) != 4:
            return False
    return True


def main():
    # FLOP only (board length=3) is required for flop->turn mode.
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
        iterations=10,
        equity_iters=3,
        preflop_action="CALL",
        hero_cards=hero_cards,
        custom_oop_range={hero_cls: 1.0},
        custom_ip_range={vill_cls: 1.0},
        bet_config={
            # Keep the tree small: allow at most one raise per street.
            "check_raise_sizes": [3.0],
            "allow_donk": False,
            "ip_raise_sizes": None,
            "oop_lead_sizes": None,
            "ip_cbet_sizes": None,
            "max_raises_per_street": 1,
        },
        tree_mode="flop_to_turn",
        turn_samples=2,
    )

    chance_nodes = count_nodes(res, player="CHANCE")
    turn_decision_nodes = count_nodes(res, street="TURN")

    if chance_nodes <= 0:
        raise SystemExit(f"FAIL: expected CHANCE nodes in flop_to_turn mode; got {chance_nodes}")

    if not all_turn_subtree_boardkeys_have_len4(res):
        raise SystemExit("FAIL: expected all TURN-subtree nodes to carry a 4-card board_key")

    if turn_decision_nodes <= 0:
        raise SystemExit("FAIL: expected at least some nodes on street TURN")

    print("PASS")


if __name__ == "__main__":
    main()
