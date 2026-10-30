import os
import sys
from typing import Any, Dict, List

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from postflop_sweep_bestany_vs_root_confirm import compute_metrics, solve


BOARDS: List[List[str]] = [
    ["As", "7d", "2c"],
    ["Tc", "8s", "6d"],
    ["Ks", "Qs", "Jd"],
]
BUDGETS = [450, 900, 1350, 2700]
REDUCED_BET_CONFIG = {
    "ip_cbet_sizes": [0.33, 0.75],
    "check_raise_sizes": [2.0, 3.0],
    "ip_raise_sizes": [2.0, 3.0],
    "allow_donk": False,
    "max_raises_per_street": 2,
    "expand_partial_sizes": False,
}


def max_map_delta(
    left: Dict[str, float],
    right: Dict[str, float],
) -> float:
    keys = set(left) | set(right)
    return max(
        (abs(float(left.get(key, 0.0)) - float(right.get(key, 0.0))) for key in keys),
        default=0.0,
    )


def regret_summary(metrics: Dict[str, Any]) -> Dict[str, float]:
    diagnostics = metrics.get("regret_diagnostics") or {}
    by_node = diagnostics.get("by_node") or {}
    double_raise_values = [
        float(info.get("max_normalized_regret", 0.0))
        for node_id, info in by_node.items()
        if (node_id or "").count("raise_") >= 2
    ]
    return {
        "max": float(diagnostics.get("max_normalized_regret", 0.0)),
        "double_raise_max": max(double_raise_values, default=0.0),
        "weighted_mean": float(
            diagnostics.get("weighted_mean_normalized_regret", 0.0)
        ),
    }


def main() -> None:
    all_results: List[Dict[str, Any]] = []

    for board in BOARDS:
        previous: Dict[str, Any] | None = None
        print(f"board={board}")
        for budget in BUDGETS:
            metrics = compute_metrics(
                solve(
                    board,
                    max_raises_per_street=2,
                    iterations=budget,
                    bet_config_override=REDUCED_BET_CONFIG,
                )
            )
            all_results.append({"board": board, "budget": budget, **metrics})

            if previous is None:
                print(
                    f"  iterations={budget} root={metrics['root_best_act']} "
                    f"rootEV={metrics['root_best_ev']:.6f} "
                    f"reach={metrics['double_raise_parent_action_reach_probability']:.6f}"
                )
            else:
                regret = regret_summary(metrics)
                print(
                    f"  iterations={budget} root={metrics['root_best_act']} "
                    f"rootEV={metrics['root_best_ev']:.6f} "
                    f"deltaRootEV={abs(metrics['root_best_ev'] - previous['root_best_ev']):.6f} "
                    f"maxActionEVD={max_map_delta(metrics['root_action_evs'], previous['root_action_evs']):.6f} "
                    f"maxFreqD={max_map_delta(metrics['root_overall_frequencies'], previous['root_overall_frequencies']):.6f} "
                    f"reach={metrics['double_raise_parent_action_reach_probability']:.6f} "
                    f"regretMax={regret['max']:.6f} "
                    f"doubleRaiseRegretMax={regret['double_raise_max']:.6f}"
                )
            previous = metrics

    print("\n=== Convergence thresholds ===")
    unstable_pairs = 0
    for board in BOARDS:
        rows = [row for row in all_results if row["board"] == board]
        pair_stable = []
        for lower, upper in zip(rows, rows[1:]):
            root_delta = abs(upper["root_best_ev"] - lower["root_best_ev"])
            action_delta = max_map_delta(upper["root_action_evs"], lower["root_action_evs"])
            frequency_delta = max_map_delta(
                upper["root_overall_frequencies"], lower["root_overall_frequencies"]
            )
            reach_delta = max_map_delta(
                upper["double_raise_parent_reach_by_root_action"],
                lower["double_raise_parent_reach_by_root_action"],
            )
            stable = (
                root_delta <= 0.02
                and action_delta <= 0.03
                and frequency_delta <= 0.01
                and reach_delta <= 0.0005
            )
            pair_stable.append(stable)
            lower_regret = regret_summary(lower)
            upper_regret = regret_summary(upper)
            print(
                f"board={board} pair={lower['budget']}_to_{upper['budget']} "
                f"stable={stable} rootEVDelta={root_delta:.6f} "
                f"actionEVD={action_delta:.6f} freqD={frequency_delta:.6f} "
                f"reachD={reach_delta:.6f} "
                f"regretMax={lower_regret['max']:.6f}->{upper_regret['max']:.6f} "
                f"doubleRaiseRegretMax={lower_regret['double_raise_max']:.6f}->{upper_regret['double_raise_max']:.6f}"
            )
        if not all(pair_stable):
            unstable_pairs += 1

    print(f"Boards with at least one unstable pair: {unstable_pairs}/{len(BOARDS)}")
    final_rows = []
    for board in BOARDS:
        rows = [row for row in all_results if row["board"] == board]
        if len(rows) < 2:
            continue
        lower, upper = rows[-2], rows[-1]
        final_rows.append(
            (
                abs(upper["root_best_ev"] - lower["root_best_ev"]),
                max_map_delta(upper["root_action_evs"], lower["root_action_evs"]),
                max_map_delta(
                    upper["root_overall_frequencies"],
                    lower["root_overall_frequencies"],
                ),
                max_map_delta(
                    upper["double_raise_parent_reach_by_root_action"],
                    lower["double_raise_parent_reach_by_root_action"],
                ),
            )
        )
    if final_rows:
        print(
            "Final 1350-to-2700 maxima: "
            f"rootEVDelta={max(row[0] for row in final_rows):.6f} "
            f"actionEVD={max(row[1] for row in final_rows):.6f} "
            f"freqD={max(row[2] for row in final_rows):.6f} "
            f"reachD={max(row[3] for row in final_rows):.6f}"
        )


if __name__ == "__main__":
    main()
