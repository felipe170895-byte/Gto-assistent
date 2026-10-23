import os
import sys
from typing import Any, Dict, List, Tuple


# Ensure local imports work with ``python -I``, which omits the script directory.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import postflop_service as s
import postflop_evaluator as pe


def best_action_by_ev(hero_action_evs: Dict[str, float]) -> Tuple[str, float]:
    if not hero_action_evs:
        return "", float("-inf")
    best_act = max(hero_action_evs.items(), key=lambda kv: kv[1])[0]
    return best_act, float(hero_action_evs[best_act])


def root_action_for_node_id(node_id: str) -> str:
    """Return the first post-check action represented by a node id."""
    parts = (node_id or "").split("_")
    if len(parts) >= 3 and parts[0] == "x":
        return f"{parts[1]}_{parts[2]}"
    if len(parts) >= 2 and parts[0] == "b":
        return f"{parts[0]}_{parts[1]}"
    return parts[0] if parts else ""


def double_raise_nodes_with_evs(res: Dict[str, Any], *, hero_side: str) -> int:
    nodes = res.get("nodes") or {}
    cnt = 0
    for nid, nd in nodes.items():
        if nd.get("player") != hero_side:
            continue
        # Must match the UI badge heuristic.
        is_double = (nid or "").count("raise_") >= 2
        if not is_double:
            continue
        hae = nd.get("hero_action_evs") or {}
        if hae:
            cnt += 1
    return cnt


def solve(
    board: List[str],
    *,
    max_raises_per_street: int,
    iterations: int = 450,
) -> Dict[str, Any]:
    hero_cards = ["Ah", "Ad"]

    # Use an IP-hero scenario so the measured starting node ("x") has several
    # actual hero actions (check and c-bets). The previous BB/OOP setup had
    # allow_donk=False, making root a forced-check node and collapsing the
    # action-EV comparison to a single value.
    hero_range = {
        "AA": 1.0,
        "KK": 1.0,
        "QQ": 1.0,
        "AKs": 0.8,
        "KQs": 0.8,
        "QJs": 0.7,
        "JTs": 0.6,
        "98s": 0.5,
    }
    villain_range = {
        "AA": 1.0,
        "KK": 1.0,
        "QQ": 1.0,
        "AKs": 1.0,
        "AQs": 0.9,
        "KQs": 0.8,
        "JTs": 0.7,
        "T9s": 0.6,
    }

    return s.solve_postflop_spot(
        board=board,
        hero_pos="BTN",
        vill_open_pos="CO",
        effective_stack=100.0,
        open_size=2.2,
        rake_pct=0.0,
        rake_cap=0.0,
        # Equal budget for both A/B arms; the service's adaptive schedule only
        # adds work when the requested budget is below its target.
        iterations=iterations,
        equity_iters=4,
        preflop_action="CALL",
        hero_cards=hero_cards,
        custom_oop_range=villain_range,
        custom_ip_range=hero_range,
        bet_config={
            # Keep grids aggressive to match the main UI.
            "check_raise_sizes": [1.5, 2.0, 2.5, 3.0, 4.0],
            "allow_donk": False,
            "ip_raise_sizes": [1.5, 2.0, 2.5, 3.0, 4.0],
            "oop_lead_sizes": None,
            "ip_cbet_sizes": None,
            "max_raises_per_street": max_raises_per_street,
        },
    )


def compute_metrics(res: Dict[str, Any]) -> Dict[str, Any]:
    hero_is_oop = bool(res.get("hero_is_oop"))
    hero_side = "OOP" if hero_is_oop else "IP"

    root_node_id = "root" if hero_is_oop else "x"
    nodes = res.get("nodes") or {}
    root_node = nodes.get(root_node_id) or {}
    root_evs = root_node.get("hero_action_evs") or {}
    root_best_act, root_best_ev = best_action_by_ev(root_evs)
    root_overall_frequencies = {
        str(action): float(freq)
        for action, freq in (root_node.get("overall_frequencies") or {}).items()
        if isinstance(freq, (int, float))
    }
    root_reach_mass = root_node.get("reach_mass") or {}
    root_hero_reach = float(root_reach_mass.get("hero_joint") or 0.0)
    root_hero_combo = str(res.get("hero_combo_key") or "")
    root_action_reach_mass = root_node.get("action_reach_mass") or {}

    # Reach instrumentation is optional so this sweep remains compatible with
    # older cached solver results. Missing metadata must be reported as absent,
    # not silently treated as a zero-opportunity tree.
    has_reach_metrics = "reach_mass" in root_node
    has_action_reach_metrics = "action_reach_mass" in root_node
    root_action_evs = {
        str(action): float(ev)
        for action, ev in root_evs.items()
        if isinstance(ev, (int, float))
    }
    root_action_evs = dict(sorted(root_action_evs.items()))
    root_action_reach_probability = {
        str(action): float((mass or {}).get("joint_probability") or 0.0)
        for action, mass in root_action_reach_mass.items()
    }
    root_action_hero_reach_probability = {
        str(action): float((mass or {}).get("hero_joint_probability") or 0.0)
        for action, mass in root_action_reach_mass.items()
    }

    best_any_ev = float("-inf")
    best_any_node_id = ""
    best_any_act = ""
    weighted_double_raise_reach = 0.0
    weighted_double_raise_reach_probability = 0.0
    weighted_double_raise_hero_reach = 0.0
    double_raise_node_count = 0
    double_raise_parent_action_reach_probability = 0.0
    double_raise_hero_parent_action_reach_probability = 0.0
    double_raise_parent_action_count = 0
    double_raise_parent_hero_action_count = 0
    double_raise_parent_reach_by_root_action: Dict[str, float] = {}
    double_raise_parent_hero_reach_by_root_action: Dict[str, float] = {}
    double_raise_parent_reach_by_root_action_and_entry: Dict[str, float] = {}
    double_raise_parent_hero_reach_by_root_action_and_entry: Dict[str, float] = {}

    for nid, nd in nodes.items():
        # Reach is a property of the whole line, not only of the player who
        # acts at the node. In the IP-hero scenario, the OOP node after the
        # hero's reraise is the double-raise node, so filtering by hero_side
        # here would incorrectly report zero reach.
        is_double = (nid or "").count("raise_") >= 2
        if is_double and has_reach_metrics:
            reach_weight = float(nd.get("reach_weight") or 0.0)
            double_raise_node_count += 1
            weighted_double_raise_reach += reach_weight
            reach_mass = nd.get("reach_mass") or {}
            weighted_double_raise_reach_probability += float(
                reach_mass.get("joint_probability") or 0.0
            )
            weighted_double_raise_hero_reach += float(
                reach_mass.get("hero_joint_probability") or 0.0
            )

            if has_action_reach_metrics:
                parent_id = nid.rsplit("_raise_", 1)[0]
                parent = nodes.get(parent_id) or {}
                parent_action_map = parent.get("action_reach_mass") or {}
                # Node ids encode the incoming action as ``raise_15x``. The
                # final split only returns ``15x``, so restore the action
                # prefix before looking it up in the parent's reach map.
                entered_by = "raise_" + nid.rsplit("_raise_", 1)[-1]
                entered_mass = parent_action_map.get(entered_by)
                if entered_mass is not None:
                    entered_joint_probability = float(
                        entered_mass.get("joint_probability") or 0.0
                    )
                    entered_hero_probability = float(
                        entered_mass.get("hero_joint_probability") or 0.0
                    )
                    double_raise_parent_action_count += 1
                    double_raise_parent_action_reach_probability += entered_joint_probability
                    double_raise_parent_hero_action_count += 1
                    double_raise_hero_parent_action_reach_probability += entered_hero_probability

                    root_action = root_action_for_node_id(nid)
                    double_raise_parent_reach_by_root_action[root_action] = (
                        double_raise_parent_reach_by_root_action.get(root_action, 0.0)
                        + entered_joint_probability
                    )
                    double_raise_parent_hero_reach_by_root_action[root_action] = (
                        double_raise_parent_hero_reach_by_root_action.get(root_action, 0.0)
                        + entered_hero_probability
                    )
                    root_action_entry = f"{root_action}:{entered_by}"
                    double_raise_parent_reach_by_root_action_and_entry[root_action_entry] = (
                        double_raise_parent_reach_by_root_action_and_entry.get(root_action_entry, 0.0)
                        + entered_joint_probability
                    )
                    double_raise_parent_hero_reach_by_root_action_and_entry[root_action_entry] = (
                        double_raise_parent_hero_reach_by_root_action_and_entry.get(root_action_entry, 0.0)
                        + entered_hero_probability
                    )

        # Hero EV / BestAny remain restricted to nodes where the hero acts.
        if nd.get("player") != hero_side:
            continue
        hae = nd.get("hero_action_evs") or {}
        if not hae:
            continue
        act, ev = best_action_by_ev(hae)
        if ev > best_any_ev:
            best_any_ev = ev
            best_any_node_id = nid
            best_any_act = act

    regret_diagnostics = res.get("regret_diagnostics") or {}

    return {
        "hero_side": hero_side,
        "root_node_id": root_node_id,
        "root_best_act": root_best_act,
        "root_best_ev": root_best_ev,
        "root_action_evs": root_action_evs,
        "root_overall_frequencies": root_overall_frequencies,
        "root_action_reach_probability": root_action_reach_probability,
        "root_action_hero_reach_probability": root_action_hero_reach_probability,
        "root_hero_reach": root_hero_reach,
        "root_hero_combo": root_hero_combo,
        "best_any_ev": best_any_ev,
        "best_any_node_id": best_any_node_id,
        "best_any_act": best_any_act,
        "double_raise_nodes_with_evs": double_raise_nodes_with_evs(
            res, hero_side=hero_side
        ),
        "double_raise_node_count_measured": double_raise_node_count,
        "double_raise_reach_weight_sum": weighted_double_raise_reach,
        "double_raise_reach_probability_sum": weighted_double_raise_reach_probability,
        "double_raise_hero_reach_probability_sum": weighted_double_raise_hero_reach,
        "double_raise_parent_action_reach_probability": double_raise_parent_action_reach_probability,
        "double_raise_hero_parent_action_reach_probability": double_raise_hero_parent_action_reach_probability,
        "double_raise_parent_action_count": double_raise_parent_action_count,
        "double_raise_hero_parent_action_count": double_raise_parent_hero_action_count,
        "double_raise_parent_reach_by_root_action": double_raise_parent_reach_by_root_action,
        "double_raise_parent_hero_reach_by_root_action": double_raise_parent_hero_reach_by_root_action,
        "double_raise_parent_reach_by_root_action_and_entry": double_raise_parent_reach_by_root_action_and_entry,
        "double_raise_parent_hero_reach_by_root_action_and_entry": double_raise_parent_hero_reach_by_root_action_and_entry,
        "double_raise_reach_weight_mean": (
            weighted_double_raise_reach / double_raise_node_count
            if double_raise_node_count
            else 0.0
        ),
        "has_reach_metrics": has_reach_metrics,
        "has_action_reach_metrics": has_action_reach_metrics,
        "regret_diagnostics": regret_diagnostics,
    }


def run_convergence_probe(
    board: List[str],
    *,
    budgets: List[int],
) -> None:
    print(f"\n=== Convergence probe board={board} ===")
    previous: Optional[Dict[str, Any]] = None
    for budget in budgets:
        result = compute_metrics(solve(board, max_raises_per_street=2, iterations=budget))
        if previous is None:
            print(
                f"iterations={budget} root={result['root_best_act']} "
                f"rootEV={result['root_best_ev']:.6f} "
                f"rootEVs={result['root_action_evs']} "
                f"reachByAction={result['double_raise_parent_reach_by_root_action']}"
            )
        else:
            actions = set(previous["root_action_evs"]) | set(result["root_action_evs"])
            max_ev_delta = max(
                (
                    abs(
                        result["root_action_evs"].get(action, 0.0)
                        - previous["root_action_evs"].get(action, 0.0)
                    )
                    for action in actions
                ),
                default=0.0,
            )
            freq_actions = set(previous["root_overall_frequencies"]) | set(
                result["root_overall_frequencies"]
            )
            max_frequency_delta = max(
                (
                    abs(
                        result["root_overall_frequencies"].get(action, 0.0)
                        - previous["root_overall_frequencies"].get(action, 0.0)
                    )
                    for action in freq_actions
                ),
                default=0.0,
            )
            print(
                f"iterations={budget} root={result['root_best_act']} "
                f"rootEV={result['root_best_ev']:.6f} "
                f"deltaEV={abs(result['root_best_ev'] - previous['root_best_ev']):.6f} "
                f"maxActionEVDelta={max_ev_delta:.6f} "
                f"maxFrequencyDelta={max_frequency_delta:.6f} "
                f"reachByAction={result['double_raise_parent_reach_by_root_action']}"
            )
        previous = result


def main() -> None:
    # Deterministic boards (avoid Ah/Ad for hero AA).
    boards = [
        ["Ks", "Qs", "Jd"],
        ["Kc", "Qd", "Jc"],
        ["Kh", "Qh", "Jd"],
        ["Kc", "Qc", "Jd"],
        ["As", "7d", "2c"],
        ["Ac", "7h", "2d"],
        ["Jh", "Tc", "2d"],
        ["Qd", "Jc", "4s"],
        ["Ks", "9d", "3c"],
        ["Kc", "9h", "3s"],
        ["Td", "9c", "5h"],
        ["Tc", "8s", "6d"],
        ["9h", "8c", "7d"],
        ["8h", "7c", "6d"],
        ["Ah", "Kd", "2c"],
        ["Ad", "Kd", "2c"],
        ["Qh", "Jd", "3c"],
        ["Kh", "Jh", "4d"],
        ["Ks", "Js", "5d"],
        ["Qc", "Tc", "9d"],
    ]

    # Remove boards that collide with hero private cards.
    hero_private = {"Ah", "Ad"}
    boards = [b for b in boards if not any(c in hero_private for c in b)]

    if os.environ.get("RUN_CONVERGENCE_PROBE") == "1":
        run_convergence_probe(boards[4], budgets=[450, 900, 1350])
        return

    metrics_A: List[Dict[str, Any]] = []
    metrics_B: List[Dict[str, Any]] = []

    for i, board in enumerate(boards, start=1):
        res2 = solve(board, max_raises_per_street=2, iterations=450)
        res1 = solve(board, max_raises_per_street=1, iterations=450)

        m2 = compute_metrics(res2)
        m1 = compute_metrics(res1)

        metrics_A.append({**m2, "board": board})
        metrics_B.append({**m1, "board": board})

        print(f"[{i}/{len(boards)}] board={board}")
        print(
            f"  cap=2 heroCombo={m2['root_hero_combo']} root={m2['root_best_act']} {m2['root_best_ev']:.6f} "
            f"| rootHeroReach={m2['root_hero_reach']:.6f} "
            f"| bestAny={m2['best_any_act']} {m2['best_any_ev']:.6f} "
            f"| doubleRaiseNodes={m2['double_raise_node_count_measured']} "
            f"| heroDecisionDoubleRaiseNodes={m2['double_raise_nodes_with_evs']} "
            f"| reachSum={m2['double_raise_reach_weight_sum']:.6f} "
            f"| reachProbSum={m2['double_raise_reach_probability_sum']:.6f} "
            f"| heroReachProbSum={m2['double_raise_hero_reach_probability_sum']:.6f} "
            f"| parentReachProb={m2['double_raise_parent_action_reach_probability']:.6f} "
            f"| parentHeroReachProb={m2['double_raise_hero_parent_action_reach_probability']:.6f} "
            f"| reachMean={m2['double_raise_reach_weight_mean']:.6f}"
        )
        print(
            f"  cap=2 doubleRaiseByRootAction={m2['double_raise_parent_reach_by_root_action']} "
            f"| heroDoubleRaiseByRootAction={m2['double_raise_parent_hero_reach_by_root_action']}"
        )
        print(
            f"  cap=1 root={m1['root_best_act']} {m1['root_best_ev']:.6f} "
            f"| rootHeroReach={m1['root_hero_reach']:.6f} "
            f"| doubleRaiseByRootAction={m1['double_raise_parent_reach_by_root_action']} "
            f"| heroDoubleRaiseByRootAction={m1['double_raise_parent_hero_reach_by_root_action']} "
            f"| bestAny={m1['best_any_act']} {m1['best_any_ev']:.6f} "
            f"| doubleRaiseNodes={m1['double_raise_node_count_measured']} "
            f"| heroDecisionDoubleRaiseNodes={m1['double_raise_nodes_with_evs']} "
            f"| reachSum={m1['double_raise_reach_weight_sum']:.6f} "
            f"| reachProbSum={m1['double_raise_reach_probability_sum']:.6f} "
            f"| heroReachProbSum={m1['double_raise_hero_reach_probability_sum']:.6f} "
            f"| parentReachProb={m1['double_raise_parent_action_reach_probability']:.6f} "
            f"| parentHeroReachProb={m1['double_raise_hero_parent_action_reach_probability']:.6f} "
            f"| reachMean={m1['double_raise_reach_weight_mean']:.6f}"
        )

    # Compare diffs. Root values are comparable because both arms evaluate the
    # same starting node. BestAny values are deliberately diagnostic: they can
    # come from different nodes and are not a global EV metric across trees.
    eps = 1e-4
    root_changed = 0
    root_action_changed = 0
    any_changed = 0
    root_abs_deltas: List[float] = []
    total_root_action_delta = 0.0
    root_action_delta_count = 0
    max_root_action_delta = 0.0
    total_root_frequency_delta = 0.0
    root_frequency_delta_count = 0
    max_root_frequency_delta = 0.0
    root_frequency_changed = 0

    print("\n=== Per-board comparable root deltas ===")
    for m2, m1 in zip(metrics_A, metrics_B):
        root_delta = abs(m2["root_best_ev"] - m1["root_best_ev"])
        root_abs_deltas.append(root_delta)
        if root_delta > eps:
            root_changed += 1
        if m2["root_best_act"] != m1["root_best_act"]:
            root_action_changed += 1

        frequency_keys = set(m2["root_overall_frequencies"]) | set(
            m1["root_overall_frequencies"]
        )
        board_frequency_changed = False
        for action in frequency_keys:
            frequency_delta = abs(
                m2["root_overall_frequencies"].get(action, 0.0)
                - m1["root_overall_frequencies"].get(action, 0.0)
            )
            total_root_frequency_delta += frequency_delta
            root_frequency_delta_count += 1
            max_root_frequency_delta = max(max_root_frequency_delta, frequency_delta)
            if frequency_delta > eps:
                board_frequency_changed = True
        if board_frequency_changed:
            root_frequency_changed += 1

        action_keys = set(m2["root_action_evs"]) | set(m1["root_action_evs"])
        for action in action_keys:
            action_delta = abs(
                m2["root_action_evs"].get(action, 0.0)
                - m1["root_action_evs"].get(action, 0.0)
            )
            total_root_action_delta += action_delta
            root_action_delta_count += 1
            max_root_action_delta = max(max_root_action_delta, action_delta)

        if abs(m2["best_any_ev"] - m1["best_any_ev"]) > eps:
            any_changed += 1

        print(
            f"board={m2['board']} root_delta={root_delta:.6f} "
            f"root_actions={m2['root_best_act']} vs {m1['root_best_act']}"
        )

    mean_root_delta = sum(root_abs_deltas) / max(1, len(root_abs_deltas))
    max_root_delta = max(root_abs_deltas, default=0.0)
    mean_root_action_delta = total_root_action_delta / max(1, root_action_delta_count)
    mean_root_frequency_delta = total_root_frequency_delta / max(
        1, root_frequency_delta_count
    )

    reach_prob_sums = [
        float(m.get("double_raise_reach_probability_sum") or 0.0)
        for m in metrics_A
    ]
    hero_reach_prob_sums = [
        float(m.get("double_raise_hero_reach_probability_sum") or 0.0)
        for m in metrics_A
    ]
    parent_reach_prob_sums = [
        float(m.get("double_raise_parent_action_reach_probability") or 0.0)
        for m in metrics_A
    ]
    parent_hero_reach_prob_sums = [
        float(m.get("double_raise_hero_parent_action_reach_probability") or 0.0)
        for m in metrics_A
    ]
    mean_reach_prob_sum = sum(reach_prob_sums) / max(1, len(reach_prob_sums))
    max_reach_prob_sum = max(reach_prob_sums, default=0.0)
    mean_hero_reach_prob_sum = sum(hero_reach_prob_sums) / max(
        1, len(hero_reach_prob_sums)
    )
    max_hero_reach_prob_sum = max(hero_reach_prob_sums, default=0.0)
    mean_parent_reach_prob_sum = sum(parent_reach_prob_sums) / max(
        1, len(parent_reach_prob_sums)
    )
    max_parent_reach_prob_sum = max(parent_reach_prob_sums, default=0.0)
    mean_parent_hero_reach_prob_sum = sum(parent_hero_reach_prob_sums) / max(
        1, len(parent_hero_reach_prob_sums)
    )
    max_parent_hero_reach_prob_sum = max(parent_hero_reach_prob_sums, default=0.0)

    reach_eps = 1e-3
    boards_with_relevant_double_raise_reach = sum(
        1 for value in reach_prob_sums if value >= reach_eps
    )
    hero_reach_eps = 1e-3
    boards_with_relevant_hero_double_raise_reach = sum(
        1 for value in hero_reach_prob_sums if value >= hero_reach_eps
    )

    print("\n=== Summary (Root is the valid A/B metric) ===")
    print(f"Boards evaluated: {len(boards)}")
    print(f"Root EV changed above {eps:g}: {root_changed}/{len(boards)}")
    print(f"Root best action changed: {root_action_changed}/{len(boards)}")
    print(f"Root EV mean absolute delta: {mean_root_delta:.6f} BB")
    print(f"Root EV max absolute delta: {max_root_delta:.6f} BB")
    print(f"Root action EV mean absolute delta: {mean_root_action_delta:.6f} BB")
    print(f"Root action EV max absolute delta: {max_root_action_delta:.6f} BB")
    print(f"Root frequency changed above {eps:g}: {root_frequency_changed}/{len(boards)}")
    print(f"Root frequency mean absolute delta: {mean_root_frequency_delta:.6f}")
    print(f"Root frequency max absolute delta: {max_root_frequency_delta:.6f}")
    print(f"BestAny EV changed (diagnostic only): {any_changed}/{len(boards)}")
    print(f"Double-raise reachProbSum mean: {mean_reach_prob_sum:.6f}")
    print(f"Double-raise reachProbSum max: {max_reach_prob_sum:.6f}")
    print(f"Double-raise heroReachProbSum mean: {mean_hero_reach_prob_sum:.6f}")
    print(f"Double-raise heroReachProbSum max: {max_hero_reach_prob_sum:.6f}")
    print(f"Double-raise parentReachProb mean: {mean_parent_reach_prob_sum:.6f}")
    print(f"Double-raise parentReachProb max: {max_parent_reach_prob_sum:.6f}")
    print(f"Double-raise parentHeroReachProb mean: {mean_parent_hero_reach_prob_sum:.6f}")
    print(f"Double-raise parentHeroReachProb max: {max_parent_hero_reach_prob_sum:.6f}")
    print(
        f"Boards with reachProbSum >= {reach_eps:g}: "
        f"{boards_with_relevant_double_raise_reach}/{len(boards)}"
    )
    print(
        f"Boards with heroReachProbSum >= {hero_reach_eps:g}: "
        f"{boards_with_relevant_hero_double_raise_reach}/{len(boards)}"
    )
    print("reachProbSum is normalized to the root joint opportunity mass;")
    print("heroReachProbSum is normalized to the root hero opportunity mass.")
    print("parent* metrics measure the action reach entering double-raise nodes.")
    if not all(m.get("has_reach_metrics") for m in metrics_A):
        print("WARNING: reach metrics are unavailable in at least one solver result.")


if __name__ == "__main__":
    main()
