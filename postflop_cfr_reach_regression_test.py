import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from postflop_solver import PostflopBetConfig, PostflopCFRSolver


def test_blocker_aware_mix() -> None:
    side_combo = ("As", "Kd")
    blocked_acting_combo = ("As", "Qd")
    compatible_acting_combo = ("7s", "Qd")
    result = PostflopCFRSolver._mix_side_ev(
        None,
        {"check": {side_combo: 0.0}, "bet": {side_combo: 10.0}},
        [side_combo],
        [blocked_acting_combo, compatible_acting_combo],
        {blocked_acting_combo: 1.0, compatible_acting_combo: 1.0},
        {
            blocked_acting_combo: {"check": 1.0, "bet": 0.0},
            compatible_acting_combo: {"check": 0.0, "bet": 1.0},
        },
        ["check", "bet"],
        {side_combo: [compatible_acting_combo]},
    )
    assert result[side_combo] == 10.0, result


def test_regret_updates_when_own_reach_is_zero() -> None:
    solver = PostflopCFRSolver(
        board=["As", "7d", "2c"],
        oop_range={"KK": 1.0, "QQ": 1.0},
        ip_range={"JJ": 1.0, "TT": 1.0},
        bet_config=PostflopBetConfig(
            ip_cbet_sizes=[0.33, 0.75],
            check_raise_sizes=[2.0],
            ip_raise_sizes=[2.0],
            max_raises_per_street=2,
        ),
        equity_iters=1,
        hero_is_oop=False,
    )
    node = solver.root.children["check"]
    combo = solver.combos_ip[0]
    before = dict(node.regrets[combo])
    zero_ip_reach = {current: 0.0 for current in solver.combos_ip}
    positive_oop_reach = {current: 1.0 for current in solver.combos_oop}

    solver._cfr_traverse(node, positive_oop_reach, zero_ip_reach, weight=1.0)
    after = node.regrets[combo]

    assert any(after[action] != before[action] for action in node.actions), (
        before,
        after,
    )


if __name__ == "__main__":
    test_blocker_aware_mix()
    test_regret_updates_when_own_reach_is_zero()
    print("PASS")
