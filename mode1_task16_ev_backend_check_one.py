import postflop_service as ps
import postflop_evaluator as pe


def best_action_by_ev(hero_action_evs):
    if not hero_action_evs:
        return "", float("-inf")
    best_act = max(hero_action_evs.items(), key=lambda kv: kv[1])[0]
    return best_act, float(hero_action_evs[best_act])


def pot_and_investments_for_hand_continuous_call(open_size: float):
    # UI convention for BB vs BTN open, hero BB CALL
    pot_pre = 1.5 + 2.0 * open_size - 0.5
    oop_inv = open_size - 0.5  # BB call amount
    ip_inv = open_size         # BTN open
    return pot_pre, oop_inv, ip_inv


def pot_and_investments_for_hand_continuous_3bet(open_size: float, threebet_mult: float = 3.0):
    # UI convention for BB vs BTN open, hero BB 3BET and BTN CALL
    threebet_size = open_size * threebet_mult
    pot_pre = 1.5 + open_size + threebet_size + (threebet_size - 0.5)
    oop_inv = 0.5 + threebet_size                  # BB posted 0.5 + 3bet
    ip_inv = open_size + (threebet_size - 0.5)    # BTN open + call
    return pot_pre, oop_inv, ip_inv


def solve_case(preflop_action: str, board, hero_cards, hero_pos='BB', vill_open_pos='BTN', open_size=2.2):
    stack = 100.0
    rake_pct = 0.0
    rake_cap = 3.0

    if preflop_action == 'CALL':
        pot_pre, oop_inv, ip_inv = pot_and_investments_for_hand_continuous_call(open_size)
    elif preflop_action == '3BET':
        pot_pre, oop_inv, ip_inv = pot_and_investments_for_hand_continuous_3bet(open_size)
    else:
        raise ValueError(preflop_action)

    res = ps.solve_postflop_spot(
        board=board,
        hero_pos=hero_pos,
        vill_open_pos=vill_open_pos,
        effective_stack=stack,
        open_size=open_size,
        rake_pct=rake_pct,
        rake_cap=rake_cap,
        iterations=40,
        equity_iters=12,
        pot_pre_override=pot_pre,
        hero_cards=hero_cards,
        oop_invested_pre=oop_inv,
        ip_invested_pre=ip_inv,
        preflop_action=preflop_action,
    )

    node_root = (res.get('nodes') or {}).get('root') or {}
    hero_action_evs = node_root.get('hero_action_evs') or {}
    hero_diag = node_root.get('hero_diag') or {}

    best_act, best_ev = best_action_by_ev(hero_action_evs)

    return {
        'bestAct': best_act,
        'bestEv': best_ev,
        'hero_action_evs': hero_action_evs,
        'hero_diag': hero_diag,
        'hero_evs_keys': sorted(list(hero_action_evs.keys())),
        'root_node_player': node_root.get('player'),
    }


def main():
    open_size = 2.2
    hero_cards = ['Ah', 'Kh']
    board = ['2c', '7d', '9s']

    print('=== Task #16 backend check (single spot) ===')
    print('hero_cards:', hero_cards)
    print('board:', board)
    print('open_size:', open_size)

    call_res = solve_case('CALL', board, hero_cards, open_size=open_size)
    three_res = solve_case('3BET', board, hero_cards, open_size=open_size)

    print('\n[CALL] root.player:', call_res['root_node_player'])
    print('[CALL] hero_diag:', call_res['hero_diag'])
    print('[CALL] hero_evs_keys:', call_res['hero_evs_keys'])
    print('[CALL] bestAct/bestEv:', call_res['bestAct'], call_res['bestEv'])

    print('\n[3BET] root.player:', three_res['root_node_player'])
    print('[3BET] hero_diag:', three_res['hero_diag'])
    print('[3BET] hero_evs_keys:', three_res['hero_evs_keys'])
    print('[3BET] bestAct/bestEv:', three_res['bestAct'], three_res['bestEv'])

    ok = bool(call_res['hero_evs_keys']) and bool(three_res['hero_evs_keys'])
    if not ok:
        print('\n[RESULT] FAIL: hero_action_evs missing for CALL and/or 3BET.')
    else:
        if call_res['bestAct'] == three_res['bestAct']:
            print('\n[RESULT] PASS-ish: EV available in both cases; best action same (may still be valid).')
        else:
            print('\n[RESULT] PASS: EV available in both cases; best action differs by EV.')


if __name__ == '__main__':
    main()
