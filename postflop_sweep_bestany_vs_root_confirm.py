import os
import sys
from typing import Any, Dict, List, Tuple

import postflop_service as s
import postflop_evaluator as pe


# Ensure local imports work when executed from other CWDs.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)


def best_action_by_ev(hero_action_evs: Dict[str, float]) -> Tuple[str, float]:
    if not hero_action_evs:
        return "", float("-inf")
    best_act = max(hero_action_evs.items(), key=lambda kv: kv[1])[0]
    return best_act, float(hero_action_evs[best_act])


def double_raise_nodes_with_evs(res: Dict[str, Any], *, hero_side: str) -> int:
    nodes = res.get('nodes') or {}
    cnt = 0
    for _nid, nd in nodes.items():
        if nd.get('player') != hero_side:
            continue
        # Must match the UI badge heuristic.
        is_double = (_nid or '').count('raise_') >= 2
        if not is_double:
            continue
        hae = nd.get('hero_action_evs') or {}
        if hae:
            cnt += 1
    return cnt


def solve(board: List[str], *, max_raises_per_street: int) -> Dict[str, Any]:
    hero_cards = ['Ah', 'Ad']
    hero_cls = pe.infer_class_from_cards(hero_cards[0], hero_cards[1])  # AA

    vill_cls = 'KK'

    return s.solve_postflop_spot(
        board=board,
        hero_pos='BB',
        vill_open_pos='BTN',
        effective_stack=100.0,
        open_size=2.2,
        rake_pct=0.0,
        rake_cap=0.0,
        iterations=12,
        equity_iters=4,
        preflop_action='CALL',
        hero_cards=hero_cards,
        custom_oop_range={hero_cls: 1.0},
        custom_ip_range={vill_cls: 1.0},
        bet_config={
            # Keep grids aggressive to match the main UI.
            'check_raise_sizes': [1.5, 2.0, 2.5, 3.0, 4.0],
            'allow_donk': False,
            'ip_raise_sizes': [1.5, 2.0, 2.5, 3.0, 4.0],
            'oop_lead_sizes': None,
            'ip_cbet_sizes': None,
            'max_raises_per_street': max_raises_per_street,
        },
    )


def compute_metrics(res: Dict[str, Any]) -> Dict[str, Any]:
    hero_is_oop = bool(res.get('hero_is_oop'))
    hero_side = 'OOP' if hero_is_oop else 'IP'

    root_node_id = 'root' if hero_is_oop else 'x'
    nodes = res.get('nodes') or {}
    root_node = nodes.get(root_node_id) or {}
    root_evs = root_node.get('hero_action_evs') or {}
    root_best_act, root_best_ev = best_action_by_ev(root_evs)

    best_any_ev = float('-inf')
    best_any_node_id = ''
    best_any_act = ''

    for nid, nd in nodes.items():
        if nd.get('player') != hero_side:
            continue
        hae = nd.get('hero_action_evs') or {}
        if not hae:
            continue
        act, ev = best_action_by_ev(hae)
        if ev > best_any_ev:
            best_any_ev = ev
            best_any_node_id = nid
            best_any_act = act

    return {
        'hero_side': hero_side,
        'root_node_id': root_node_id,
        'root_best_act': root_best_act,
        'root_best_ev': root_best_ev,
        'best_any_ev': best_any_ev,
        'best_any_node_id': best_any_node_id,
        'best_any_act': best_any_act,
        'double_raise_nodes_with_evs': double_raise_nodes_with_evs(res, hero_side=hero_side),
    }


def main() -> None:
    # Deterministic boards (avoid Ah/Ad for hero AA).
    boards = [
        ['Ks', 'Qs', 'Jd'],
        ['Kc', 'Qd', 'Jc'],
        ['Kh', 'Qh', 'Jd'],
        ['Kc', 'Qc', 'Jd'],
        ['As', '7d', '2c'],
        ['Ac', '7h', '2d'],
        ['Jh', 'Tc', '2d'],
        ['Qd', 'Jc', '4s'],
        ['Ks', '9d', '3c'],
        ['Kc', '9h', '3s'],
        ['Td', '9c', '5h'],
        ['Tc', '8s', '6d'],
        ['9h', '8c', '7d'],
        ['8h', '7c', '6d'],
        ['Ah', 'Kd', '2c'],  # contains Ah: skip protection handled by solver (but hero_combo can vanish)
        ['Ad', 'Kd', '2c'],  # contains Ad: skip protection handled by solver
        ['Qh', 'Jd', '3c'],
        ['Kh', 'Jh', '4d'],
        ['Ks', 'Js', '5d'],
        ['Qc', 'Tc', '9d'],
    ]

    # Remove boards that collide with hero private cards.
    hero_private = {'Ah', 'Ad'}
    boards = [b for b in boards if not any(c in hero_private for c in b)]

    metrics_A: List[Dict[str, Any]] = []
    metrics_B: List[Dict[str, Any]] = []

    for i, board in enumerate(boards, start=1):
        res2 = solve(board, max_raises_per_street=2)
        res1 = solve(board, max_raises_per_street=1)

        m2 = compute_metrics(res2)
        m1 = compute_metrics(res1)

        metrics_A.append({**m2, 'board': board})
        metrics_B.append({**m1, 'board': board})

        print(f"[{i}/{len(boards)}] board={board}")
        print(f"  cap=2 root={m2['root_best_act']} {m2['root_best_ev']:.6f} | bestAny={m2['best_any_act']} {m2['best_any_ev']:.6f} | doubleRaiseNodes={m2['double_raise_nodes_with_evs']}")
        print(f"  cap=1 root={m1['root_best_act']} {m1['root_best_ev']:.6f} | bestAny={m1['best_any_act']} {m1['best_any_ev']:.6f} | doubleRaiseNodes={m1['double_raise_nodes_with_evs']}")

    # Compare diffs.
    eps = 1e-4
    root_changed = 0
    any_changed = 0

    for m2, m1 in zip(metrics_A, metrics_B):
        if abs(m2['root_best_ev'] - m1['root_best_ev']) > eps:
            root_changed += 1
        if abs(m2['best_any_ev'] - m1['best_any_ev']) > eps:
            any_changed += 1

    print("\n=== Summary (BestAny vs Root) ===")
    print(f"Boards evaluated: {len(boards)}")
    print(f"Root EV changed in: {root_changed}/{len(boards)}")
    print(f"BestAny EV changed in: {any_changed}/{len(boards)}")


if __name__ == '__main__':
    main()
