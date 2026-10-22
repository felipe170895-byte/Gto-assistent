import sys

sys.path.append('C:/Users/USER/Documents/Nova pasta')

import postflop_service as s
import postflop_evaluator as pe


def double_raise_nodes_with_evs(res, *, hero_side: str) -> int:
    nodes = res.get('nodes') or {}
    cnt = 0
    for nid, nd in nodes.items():
        if nd.get('player') != hero_side:
            continue
        # Same heuristic as the UI badge.
        is_double = nid.count('raise_') >= 2
        if not is_double:
            continue
        hae = nd.get('hero_action_evs') or {}
        if hae:
            cnt += 1
    return cnt


def solve(board, *, max_raises_per_street: int):
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
        # Tiny ranges to keep the solver fast.
        custom_oop_range={hero_cls: 1.0},
        custom_ip_range={vill_cls: 1.0},
        bet_config={
            'check_raise_sizes': [3.0],
            'allow_donk': False,
            'ip_raise_sizes': None,
            'oop_lead_sizes': None,
            'ip_cbet_sizes': None,
            'max_raises_per_street': max_raises_per_street,
        },
    )


def main():
    # Board chosen just to be representative (not a golden case).
    board = ['Ks', 'Qs', 'Jd']
    hero_side = 'OOP'  # BB hero is OOP

    resA = solve(board, max_raises_per_street=2)
    ddA = double_raise_nodes_with_evs(resA, hero_side=hero_side)

    resB = solve(board, max_raises_per_street=1)
    ddB = double_raise_nodes_with_evs(resB, hero_side=hero_side)

    print('double-raise nodes with non-empty hero_action_evs:')
    print('  cap=2:', ddA)
    print('  cap=1:', ddB)

    # Structural assertions matching the badge behavior.
    if ddA <= 0:
        raise SystemExit('FAIL: expected at least one double-raise node with hero_action_evs when cap=2')
    if ddB != 0:
        raise SystemExit('FAIL: expected zero double-raise nodes with hero_action_evs when cap=1')

    print('PASS')


if __name__ == '__main__':
    main()
