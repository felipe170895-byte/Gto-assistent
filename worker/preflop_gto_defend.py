import argparse
import json
import math
import os
import random
from typing import Dict, List, Tuple

import eval7

RANKS = "AKQJT98765432"
SUITS = "shdc"

OPEN_SIZE = 2.2
THREEBET_SIZE = 9.0
POT_PRE = 4.2  # SB(1)+BB(1)+Open(2.2)

ACTION_HERO = ["fold", "call", "threebet"]
ACTION_VILL = ["fold", "call"]

Crie: C:\Users\USER\Documents\Nova pasta\worker\preflop_gto_defend.py

Instale dependência (no seu PC/servidor onde o PHP vai chamar Python):
pip install eval7

Cole o arquivo abaixo:

import argparse
import json
import math
import os
import random
from typing import Dict, List, Tuple

import eval7

RANKS = "AKQJT98765432"
SUITS = "shdc"

OPEN_SIZE = 2.2
THREEBET_SIZE = 9.0
POT_PRE = 4.2  # SB(1)+BB(1)+Open(2.2)

ACTION_HERO = ["fold", "call", "threebet"]
ACTION_VILL = ["fold", "call"]


def infer_class_from_cards(c1: str, c2: str)
    # c like "As", "Kd"
    r1, s1 = c1[0], c1[1]
    r2, s2 = c2[0], c2[1]
    if r1 == r2:
        return r1 + r2
    # order high->low by RANKS index (A high
    i1 = RANKS.index(r1)
    i2 = RANKS.index(r2)
    hi, lo = (r1, r2) if i1 < i2 else (r2, r1)
    suited = (s1 == s2)
    return hi + lo + ("s" if suited else "o")


def build_class_to_combos() -> Dict[str, Lis
    class_to = {}
    ranks = list(RANKS)

    # pairs
    for r in ranks:
        cards = [f"{r}{s}" for s in SUITS]
        combos = []
        for i in range(4):
            for j in range(i + 1, 4):
                combos.append((cards[i], car
        class_to[r + r] = combos

    # suited and offsuit (169 total)
    for i in range(len(ranks)):
        for j in range(i + 1, len(ranks)):
            r1, r2 = ranks[i], ranks[j]
            # suited
            suited = []
            for s in SUITS:
                suited.append((f"{r1}{s}", f
            class_to[r1 + r2 + "s"] = suited

            # offsuit
            offsuit = []
            for s1 in SUITS:
                for s2 in SUITS:
                    if s1 == s2:
                        continue
                    offsuit.append((f"{r1}{s1}", f"{r2}{s2}"))
            class_to[r1 + r2 + "o"] = offsui

    return class_to


def normalize(dist: Dict[str, float]) -> Dict[str, float]:
    s = sum(dist.values())
    if s <= 0:
        return dist
    return {k: v / s for k, v in dist.items()}


def default_open_range_169() -> Dict[str, fl
    # Pesos simples (ajuste depois se quiser ficar “mais GTO” pro seu gosto).
    # Valores não precisam somar 1.
    w = {}
    # pares
    for r in RANKS:
        w[r + r] = 1.0

    # Ax suited A2s+ (inclui As2s)
    for r in RANKS[1:]:
        w["A" + r + "s"] = 0.9

    # broadways suited
    for cls in ["KQs", "KJs", "KTs", "QJs", "QTs", "JTs", "T9s", "98s", "87s", "76s"]:
        w[cls] = 0.8

    # offsuit broadways (bem leve)
    for cls in ["KQo", "AQo", "AJo", "ATo"]:
        w[cls] = 0.6

    # suited connectors/gappers leves
    for cls in ["J9s", "T8s", "Q9s", "65s"]:
        w[cls] = 0.35

    return w


def preflop_equity_mc(hero_combo: Tuple[str, str], vill_combo: Tuple[str, str], iters: int, rng: random.Random) ->
float:
    # equity hero share vs vill on random full board (5 runouts)
    # Remove used cards from deck
    dead = {hero_combo[0], hero_combo[1], vill_combo[0], vill_combo[1]}
    deck = [r + s for r in RANKS for s in SU

    hero_cards = [eval7.Card(hero_combo[0]),
    vill_cards = [eval7.Card(vill_combo[0]), eval7.Card(vill_combo[1])]

    win = 0
    tie = 0
    for _ in range(iters):
        board = rng.sample(deck, 5)
        b = [eval7.Card(x) for x in board]
        hr = eval7.evaluate(hero_cards + b)
        vr = eval7.evaluate(vill_cards + b)
        if hr > vr:
            win += 1
        elif vr == hr == vr:
            tie += 1
    return (win + 0.5 * tie) / iters


def build_all_169_classes() -> List[str]:
    ranks = list(RANKS)
    classes = []
    # pairs
    for r in ranks:
        classes.append(r + r)
    # suited/off
    for i in range(len(ranks)):
        for j in range(i + 1, len(ranks)):
            r1, r2 = ranks[i], ranks[j]
            classes.append(r1 + r2 + "s")
            classes.append(r1 + r2 + "o")
    return classes


def regret_matching(regrets: Dict[str, floatct[str, float]:
    pos = [max(0.0, regrets[a]) for a in actions]
    s = sum(pos)
    if s <= 1e-12:
        p = 1.0 / len(actions)
        return {a: p for a in actions}
    return {a: max(0.0, regrets[a]) / s for


def solve_for_hero_pos(hero_pos: str, hero_classes: List[str], vill_classes: List[str],
                        eq: Dict[str, Dict[s
                        P_hero: Dict[str, float], P_vill: Dict[str, float],
                        iters: int, seed: in
    # posted at decision
    hero_posted = 1.0 if hero_pos == "BB" el

    call_cost = OPEN_SIZE - hero_posted
    pot_call = POT_PRE + call_cost               # pot at showdown if call

    hero_threebet_invest = THREEBET_SIZE        # total hero contribution when 3bet
    hero_threebet_add = THREEBET_SIZE - hero
    villain_call_add = THREEBET_SIZE - OPEN_SIZE  # extra for villain when calling 3bet
    pot_3bet_call = POT_PRE + hero_threebet_

    EV_THREE_FOLD_FOR_HERO = POT_PRE  # villre

    rng = random.Random(seed)

    # Strategies
    hero_sigma = {h: {"fold": 1/3, "call": 1/3, "threebet": 1/3} for h in hero_classes}
    hero_sigma_sum = {h: {"fold": 0.0, "call h in hero_classes}

    vill_sigma = {v: {"fold": 0.5, "call": 0
    vill_sigma_sum = {v: {"fold": 0.0, "call": 0.0} for v in vill_classes}

    hero_regrets = {h: {"fold": 0.0, "call": 0.0, "threebet": 0.0} for h in hero_classes}
    vill_regrets = {v: {"fold": 0.0, "call":

    # CFR (deterministic, full expectation o
    for it in range(iters):
        # update hero regrets
        for h in hero_classes:
            # action EVs (hero absolute EV;
            u_fold = 0.0

            u_call = 0.0
            u_three = 0.0
            for v in vill_classes:
                p_v = P_vill[v]
                e = eq[h][v]

                # call EV
                ev_call = e * pot_call - cal
                u_call += p_v * ev_call

                # threebet EV depends on villain response
                ev_three_call = e * pot_3bet
                ev_three_fold = EV_THREE_FOLD_FOR_HERO
                u_three += p_v * (vill_sigma+ vill_sigma[v]["call"] * ev_three_call)

            u_avg = (
                hero_sigma[h]["fold"] * u_fold +
                hero_sigma[h]["call"] * u_ca
                hero_sigma[h]["threebet"] * u_three
            )

            hero_regrets[h]["fold"] += (u_fo
            hero_regrets[h]["call"] += (u_call - u_avg)
            hero_regrets[h]["threebet"] += (

            # strategy update
            hero_sigma[h] = regret_matching(hero_regrets[h], ACTION_HERO)

        # update villain regrets at response node (conditional on hero choosing threebet)
        # w_h = P(hero type h) * sigma_h(thr
        for v in vill_classes:
            w_total = 0.0
            # unnormalized totals:
            V_fold_total = 0.0
            V_call_total = 0.0

            for h in hero_classes:
                w_h = P_hero[h] * hero_sigma
                if w_h <= 0:
                    continue
                w_total += w_h
                V_fold_total += w_h * (-EV_T
                # villain calling => hero three-call EV => villain = -hero_ev
                ev_three_call = eq[h][v] * p_invest
                V_call_total += w_h * (-ev_three_call)

            # if nobody 3bets, no need to update (skip)
            if w_total <= 1e-15:
                continue

            u_avg_total = vill_sigma[v]["fold"] * V_fold_total + vill_sigma[v]["call"] * V_call_total

            vill_regrets[v]["fold"] += (V_fold_total - u_avg_total)
            vill_regrets[v]["call"] += (V_ca

            vill_sigma[v] = regret_matching()

        # accumulate averages
        for h in hero_classes:
            for a in ACTION_HERO:
                hero_sigma_sum[h][a] += hero_sigma[h][a]

        for v in vill_classes:
            for a in ACTION_VILL:
                vill_sigma_sum[v][a] += vill_sigma[v][a]

        if (iters >= 20) and (it % max(1, iters // 10) == 0):
            print(f"[{hero_pos}] it={it}/{it

    # export hero average strategies + EVs use
    denom = float(iters)
    hero_strategy_out = {}
    hero_evs_out = {}

    # average villain strategy
    vill_sigma_avg = {}
    for v in vill_classes:
        vill_sigma_avg[v] = {a: vill_sigma_sTION_VILL}

    for h in hero_classes:
        # average hero strategy
        hero_strategy_out[h] = {a: hero_sigm ACTION_HERO}

        # EVs under average strategies
        EV_fold = 0.0

        EV_call = 0.0
        EV_three = 0.0
        for v in vill_classes:
            p_v = P_vill[v]
            e = eq[h][v]

            EV_call += p_v * (e * pot_call - call_cost)

            ev_three_call = e * pot_3bet_call - hero_threebet_invest
            EV_three += p_v * (vill_sigma_av_FOR_HERO + vill_sigma_avg[v]["call"] *ev_three_call)

        hero_evs_out[h] = {
            "fold": EV_fold,
            "call": EV_call,
            "threebet": EV_three,
        }

    return {
        "hero_pos": hero_pos,
        "params": {
            "open_size": OPEN_SIZE,
            "threebet_size": THREEBET_SIZE,
            "pot_pre": POT_PRE,
            "call_cost": call_cost,
            "pot_call": pot_call,
            "pot_3bet_call": pot_3bet_call,
            "hero_posted": hero_posted,
            "iters": iters,
        },
        "action_labels": ACTION_HERO,
        "hero_strategy": hero_strategy_out,
        "hero_evs": hero_evs_out,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--heroPos", required=True, choices=["CO", "BTN", "BB"])
    ap.add_argument("--out", required=True, orage/")
    ap.add_argument("--iters", type=int, default=2500, help="CFR iterations for strategies (offline)")
    ap.add_argument("--equityIters", type=inouts per equity evaluation")
    ap.add_argument("--comboSamples", type=int, default=3, help="how many combo-pairs to average per class-pair")
    ap.add_argument("--seed", type=int, defa
    args = ap.parse_args()

    class_to_combos = build_class_to_combos()
    hero_classes = build_all_169_classes()

    open_range = default_open_range_169()  #
    # villain classes: only those present in open_range
    vill_classes = sorted(list(open_range.ke

    # probability distributions by class (co weights)
    combo_count = {cls: len(class_to_combos[cls]) for cls in hero_classes}
    P_hero = normalize({h: combo_count[h] fo

    vill_weights = {v: open_range[v] * len(cvill_classes}
    P_vill = normalize(vill_weights)

    rng = random.Random(args.seed)

    # compute equity matrix eq[hero_class][vill_class]
    # cache to speed (optional)
    eq: Dict[str, Dict[str, float]] = {h: {} for h in hero_classes}
    print("Computing equity matrix (this can
    for i, h in enumerate(hero_classes):
        if i % max(1, len(hero_classes)//10)
            print(f"eq row {i}/{len(hero_classes)} ...")
        for v in vill_classes:
            acc = 0.0
            for _ in range(args.comboSamples
                # sample concrete non-overlapping combo pair
                hc = rng.randrange(len(class
                vc = rng.randrange(len(class_to_combos[v]))
                hero_combo = class_to_combos
                vill_combo = class_to_combos[v][vc]
                tries = 0
                while set(hero_combo).intersection(set(vill_combo)) and tries < 50:
                    vc = rng.randrange(len(c
                    vill_combo = class_to_combos[v][vc]
                    tries += 1
                acc += preflop_equity_mc(hero_combo, vill_combo, args.equityIters, rng)
            eq[h][v] = acc / float(args.comb

    print("Solving CFR strategy...")
    out = solve_for_hero_pos(
        hero_pos=args.heroPos,
        hero_classes=hero_classes,
        vill_classes=vill_classes,
        eq=eq,
        P_hero=P_hero,
        P_vill=P_vill,
        iters=args.iters,
        seed=args.seed + 999
    )

    os.makedirs(os.path.dirname(args.out), e
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False
    print("Wrote:", args.out)


if __name__ == "__main__":
    main()