import argparse
import json
import os
import random
from typing import Dict, List, Tuple

from mode1_game_model import apply_rake_to_pot, cap_total
from mode1_solver_common import (
    apply_range_profile,
    apply_range_profile_stack,
    get_or_compute_equity_matrix,
    normalize_range_profile,
    regret_matching_plus,
)

import eval7

RANKS = "AKQJT98765432"
SUITS = "shdc"

# Sizes (fixed grid / simplified model)
OPEN_SIZE = 2.2

# Dual 3bet sizing (internal only). UI still shows a single merged 3bet action.
# Internal dual/quad 3bet grid (output is still aggregated into one external 'threebet' action)
THREEBET_OPTIONS = [6.5, 7.5, 9.0, 10.5]
THREEBET_SIZE = THREEBET_OPTIONS[-1]  # backward-compatible single-size value

FOURBET_SIZE = 21.0

# Pot before hero decision: SB(1) + BB(1) + open(2.2)
POT_PRE = 4.2

# External contract (Mode 1): UI/API keep 3 actions.
ACTION_HERO_OPEN = ["fold", "call", "threebet"]
ACTION_VIL_RESP = ["fold", "call", "fourbet"]
ACTION_HERO_4RESP = ["fold", "call"]

MODEL_TAG = "mode1-preflop-discrete-v3"


def infer_class_from_cards(c1: str, c2: str) -> str:
    """Map concrete 2-card combos to a 169-style bucket key.

    Cards are like "As" "Kd". Returns keys like "AA", "AKs", "AKo".
    """
    r1, s1 = c1[0], c1[1]
    r2, s2 = c2[0], c2[1]

    if r1 == r2:
        return r1 + r2

    i1 = RANKS.index(r1)
    i2 = RANKS.index(r2)
    hi, lo = (r1, r2) if i1 < i2 else (r2, r1)

    suited = (s1 == s2)
    return hi + lo + ("s" if suited else "o")


def build_class_to_combos() -> Dict[str, List[Tuple[str, str]]]:
    """For each 169 bucket, list all non-ordered 2-card combos."""
    class_to: Dict[str, List[Tuple[str, str]]] = {}
    ranks = list(RANKS)

    # pairs
    for r in ranks:
        cards = [f"{r}{s}" for s in SUITS]
        combos = []
        for i in range(4):
            for j in range(i + 1, 4):
                combos.append((cards[i], cards[j]))
        class_to[r + r] = combos

    # suited and offsuit buckets
    for i in range(len(ranks)):
        for j in range(i + 1, len(ranks)):
            r1, r2 = ranks[i], ranks[j]

            suited = []
            for s in SUITS:
                suited.append((f"{r1}{s}", f"{r2}{s}"))
            class_to[r1 + r2 + "s"] = suited

            offsuit = []
            for s1 in SUITS:
                for s2 in SUITS:
                    if s1 == s2:
                        continue
                    offsuit.append((f"{r1}{s1}", f"{r2}{s2}"))
            class_to[r1 + r2 + "o"] = offsuit

    return class_to


def normalize(dist: Dict[str, float]) -> Dict[str, float]:
    s = sum(dist.values())
    if s <= 0:
        return dist
    return {k: v / s for k, v in dist.items()}


def default_open_range_169(vill_open_pos: str, range_profile: str = "standard") -> Dict[str, float]:
    """Open range weights by villain open position (optional tight/standard/loose profile)."""
    vill_open_pos = (vill_open_pos or "CO").upper().strip()
    if vill_open_pos not in ["UTG", "CO", "BTN"]:
        vill_open_pos = "CO"

    # Start from a CO-like baseline
    w: Dict[str, float] = {}

    for r in RANKS:
        w[r + r] = 1.0

    # Ax suited
    for r in RANKS[1:]:
        w["A" + r + "s"] = 0.9

    # Broadways suited
    for cls in ["KQs", "KJs", "KTs", "QJs", "QTs", "JTs", "T9s", "98s", "87s", "76s"]:
        w[cls] = 0.8

    # Broadways offsuit
    for cls in ["KQo", "AQo", "AJo", "ATo"]:
        w[cls] = 0.6

    # Suited connectors/gappers (light)
    for cls in ["J9s", "T8s", "Q9s", "65s"]:
        w[cls] = 0.35

    # Position tuning
    if vill_open_pos == "UTG":
        for key in list(w.keys()):
            if key.endswith("o"):
                w[key] *= 0.75
            if key.endswith("s") and (key.startswith("A") is False):
                w[key] *= 0.85
            if key in ["J9s", "T8s", "Q9s", "65s"]:
                w[key] *= 0.70

    elif vill_open_pos == "CO":
        for key in list(w.keys()):
            if key.endswith("o"):
                w[key] *= 1.00
            if key in ["J9s", "T8s", "Q9s", "65s"]:
                w[key] *= 1.05

    elif vill_open_pos == "BTN":
        for key in list(w.keys()):
            if key.endswith("o"):
                w[key] *= 1.25
            if key in ["J9s", "T8s", "Q9s", "65s"]:
                w[key] *= 1.45
            if key.endswith("s") and key.startswith("A"):
                w[key] *= 1.10

    return apply_range_profile(w, range_profile)


def preflop_equity_mc(
    hero_combo: Tuple[str, str],
    vill_combo: Tuple[str, str],
    iters: int,
    rng: random.Random,
) -> float:
    """Monte-Carlo preflop equity with random 5-card runouts."""
    dead = {hero_combo[0], hero_combo[1], vill_combo[0], vill_combo[1]}

    deck = [r + s for r in RANKS for s in SUITS if (r + s) not in dead]

    hero_cards = [eval7.Card(hero_combo[0]), eval7.Card(hero_combo[1])]
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
        elif hr == vr:
            tie += 1

    return (win + 0.5 * tie) / iters


def build_all_169_classes() -> List[str]:
    ranks = list(RANKS)
    classes = []

    for r in ranks:
        classes.append(r + r)

    for i in range(len(ranks)):
        for j in range(i + 1, len(ranks)):
            r1, r2 = ranks[i], ranks[j]
            classes.append(r1 + r2 + "s")
            classes.append(r1 + r2 + "o")

    return classes


def regret_matching(regrets: Dict[str, float], actions: List[str]) -> Dict[str, float]:
    """CFR+ (RM+): floor accumulated regrets at 0, then mix on positive mass."""
    return regret_matching_plus(regrets, actions)


def solve_for_hero_pos(
    hero_pos: str,
    hero_classes: List[str],
    vill_classes: List[str],
    eq: Dict[str, Dict[str, float]],
    P_hero: Dict[str, float],
    P_vill: Dict[str, float],
    iters: int,
    seed: int,
    rake_pct: float,
    rake_cap: float,
    effective_stack: float,
) -> Dict:
    """Approximate CFR+ style solving (type-based, dual 3bet sizing, with 4bet branch).

    Output EVs/strategy are aggregated for the hero OPEN node only: [fold, call, threebet].

    Rake/stack fidelity:
    - rake is applied only on showdown EV terms (equity * pot).
    - fold branches are pure non-showdown, so no rake is applied.
    - effective_stack caps each player's *total committed chips* (all-in aware).
      With default 100bb, none of the current sizings jam, so results match before.
    """

    hero_posted = 1.0 if hero_pos == "BB" else 0.0
    vill_posted = OPEN_SIZE  # villain already opened

    # Dead money sitting in the pot besides the two players' committed chips
    # (SB leftover). For BB-hero this is 1.0 (SB); for CO/BTN-hero this is 2.0
    # (SB+BB) because hero_posted=0.
    dead_money = max(0.0, POT_PRE - hero_posted - vill_posted)

    # Hero calls open: remaining chips hero can put in are capped by stack.
    hero_call_total = cap_total(OPEN_SIZE, effective_stack)
    call_cost = max(0.0, hero_call_total - hero_posted)
    vill_call_total = cap_total(OPEN_SIZE, effective_stack)
    pot_call = dead_money + hero_call_total + vill_call_total
    pot_call_showdown = apply_rake_to_pot(pot_call, rake_pct, rake_cap)

    # EV if villain folds to 3bet: no showdown => no rake.
    EV_3bet_vill_fold = POT_PRE

    # Internal dual 3bet sizing
    threebet_actions = [f"threebet_{i}" for i in range(len(THREEBET_OPTIONS))]
    hero_open_actions_internal = ["fold", "call"] + threebet_actions

    # Precompute per-3bet-size pot/invest values (all-in capped).
    hero_threebet_invest: List[float] = []
    pot_3bet_call: List[float] = []
    pot_3bet_call_showdown: List[float] = []
    hero_call_4_add: List[float] = []
    pot_4bet_call: List[float] = []
    pot_4bet_call_showdown: List[float] = []
    EV_4bet_hero_fold: List[float] = []

    for s in THREEBET_OPTIONS:
        hero_3_total = cap_total(s, effective_stack)
        invest = max(0.0, hero_3_total - hero_posted)
        hero_threebet_invest.append(invest)

        # Villain calling the 3bet matches hero's (capped) total.
        vill_3_total = cap_total(hero_3_total, effective_stack)
        pot3 = dead_money + hero_3_total + vill_3_total
        pot_3bet_call.append(pot3)
        pot_3bet_call_showdown.append(apply_rake_to_pot(pot3, rake_pct, rake_cap))

        # 4bet: both players go toward FOURBET_SIZE, capped by stack.
        four_target = cap_total(FOURBET_SIZE, effective_stack)
        hero_4_total = four_target
        vill_4_total = four_target
        call4_add = max(0.0, hero_4_total - hero_3_total)
        hero_call_4_add.append(call4_add)

        pot4 = dead_money + hero_4_total + vill_4_total
        pot_4bet_call.append(pot4)
        pot_4bet_call_showdown.append(apply_rake_to_pot(pot4, rake_pct, rake_cap))

        EV_4bet_hero_fold.append(-invest)

    rng = random.Random(seed)  # kept for parity with previous implementation

    # Strategies and regrets
    n_sizes = len(THREEBET_OPTIONS)

    hero_open_sigma = {h: {a: 1.0 / len(hero_open_actions_internal) for a in hero_open_actions_internal} for h in hero_classes}
    hero_open_regrets = {h: {a: 0.0 for a in hero_open_actions_internal} for h in hero_classes}
    hero_open_sigma_sum = {h: {a: 0.0 for a in hero_open_actions_internal} for h in hero_classes}

    vill_resp_sigma = {
        v: {i: {"fold": 1/3, "call": 1/3, "fourbet": 1/3} for i in range(n_sizes)}
        for v in vill_classes
    }
    vill_resp_regrets = {
        v: {i: {"fold": 0.0, "call": 0.0, "fourbet": 0.0} for i in range(n_sizes)}
        for v in vill_classes
    }
    vill_resp_sigma_sum = {
        v: {i: {"fold": 0.0, "call": 0.0, "fourbet": 0.0} for i in range(n_sizes)}
        for v in vill_classes
    }

    hero_4resp_sigma = {
        h: {i: {"fold": 0.5, "call": 0.5} for i in range(n_sizes)}
        for h in hero_classes
    }
    hero_4resp_regrets = {
        h: {i: {"fold": 0.0, "call": 0.0} for i in range(n_sizes)}
        for h in hero_classes
    }
    hero_4resp_sigma_sum = {
        h: {i: {"fold": 0.0, "call": 0.0} for i in range(n_sizes)}
        for h in hero_classes
    }

    for _it in range(iters):
        # 1) Update hero OPEN node regrets
        for h in hero_classes:
            u_fold = 0.0

            u_call = 0.0
            for v in vill_classes:
                p_v = P_vill[v]
                e = eq[h][v]
                u_call += p_v * (e * pot_call_showdown - call_cost)

            u_three = [0.0 for _ in range(n_sizes)]

            for v in vill_classes:
                p_v = P_vill[v]
                e = eq[h][v]

                for i in range(n_sizes):
                    invest_i = hero_threebet_invest[i]

                    ev_three_call = e * pot_3bet_call_showdown[i] - invest_i
                    ev_four_call = e * pot_4bet_call_showdown[i] - (invest_i + hero_call_4_add[i])
                    ev_four_value = (
                        hero_4resp_sigma[h][i]["fold"] * EV_4bet_hero_fold[i]
                        + hero_4resp_sigma[h][i]["call"] * ev_four_call
                    )

                    u_three[i] += p_v * (
                        vill_resp_sigma[v][i]["fold"] * EV_3bet_vill_fold
                        + vill_resp_sigma[v][i]["call"] * ev_three_call
                        + vill_resp_sigma[v][i]["fourbet"] * ev_four_value
                    )

            # Expected value under current mixed strategy
            u_avg = hero_open_sigma[h]["fold"] * u_fold + hero_open_sigma[h]["call"] * u_call
            for i in range(n_sizes):
                u_avg += hero_open_sigma[h][threebet_actions[i]] * u_three[i]

            hero_open_regrets[h]["fold"] += (u_fold - u_avg)
            hero_open_regrets[h]["call"] += (u_call - u_avg)
            for i in range(n_sizes):
                hero_open_regrets[h][threebet_actions[i]] += (u_three[i] - u_avg)

            hero_open_sigma[h] = regret_matching(hero_open_regrets[h], hero_open_actions_internal)

        # 2) Update villain response node regrets (conditioned on hero choosing threebet_i)
        for v in vill_classes:
            for i in range(n_sizes):
                V_fold_total = 0.0
                V_call_total = 0.0
                V_four_total = 0.0

                for h in hero_classes:
                    w_h = P_hero[h] * hero_open_sigma[h][threebet_actions[i]]
                    if w_h <= 0:
                        continue

                    e = eq[h][v]

                    V_fold_total += w_h * (-EV_3bet_vill_fold)

                    invest_i = hero_threebet_invest[i]
                    ev_three_call = e * pot_3bet_call_showdown[i] - invest_i
                    V_call_total += w_h * (-ev_three_call)

                    ev_four_call = e * pot_4bet_call_showdown[i] - (invest_i + hero_call_4_add[i])
                    ev_four_value = (
                        hero_4resp_sigma[h][i]["fold"] * EV_4bet_hero_fold[i]
                        + hero_4resp_sigma[h][i]["call"] * ev_four_call
                    )
                    V_four_total += w_h * (-ev_four_value)

                u_avg_total = (
                    vill_resp_sigma[v][i]["fold"] * V_fold_total
                    + vill_resp_sigma[v][i]["call"] * V_call_total
                    + vill_resp_sigma[v][i]["fourbet"] * V_four_total
                )

                vill_resp_regrets[v][i]["fold"] += (V_fold_total - u_avg_total)
                vill_resp_regrets[v][i]["call"] += (V_call_total - u_avg_total)
                vill_resp_regrets[v][i]["fourbet"] += (V_four_total - u_avg_total)

                vill_resp_sigma[v][i] = regret_matching(vill_resp_regrets[v][i], ACTION_VIL_RESP)

        # 3) Update hero 4bet response node regrets (conditioned on villain choosing fourbet for each i)
        for h in hero_classes:
            for i in range(n_sizes):
                w_total = 0.0
                u4_fold = 0.0
                u4_call = 0.0

                for v in vill_classes:
                    w = P_vill[v] * vill_resp_sigma[v][i]["fourbet"]
                    if w <= 0:
                        continue

                    w_total += w
                    e = eq[h][v]

                    invest_i = hero_threebet_invest[i]
                    ev_four_call = e * pot_4bet_call_showdown[i] - (invest_i + hero_call_4_add[i])

                    u4_fold += w * EV_4bet_hero_fold[i]
                    u4_call += w * ev_four_call

                if w_total <= 1e-15:
                    continue

                u_avg = hero_4resp_sigma[h][i]["fold"] * u4_fold + hero_4resp_sigma[h][i]["call"] * u4_call

                hero_4resp_regrets[h][i]["fold"] += (u4_fold - u_avg)
                hero_4resp_regrets[h][i]["call"] += (u4_call - u_avg)

                hero_4resp_sigma[h][i] = regret_matching(hero_4resp_regrets[h][i], ACTION_HERO_4RESP)

        # accumulate averages
        for h in hero_classes:
            for a in ["fold", "call"]:
                hero_open_sigma_sum[h][a] += hero_open_sigma[h][a]
            for i in range(n_sizes):
                hero_open_sigma_sum[h][threebet_actions[i]] += hero_open_sigma[h][threebet_actions[i]]

            for i in range(n_sizes):
                hero_4resp_sigma_sum[h][i]["fold"] += hero_4resp_sigma[h][i]["fold"]
                hero_4resp_sigma_sum[h][i]["call"] += hero_4resp_sigma[h][i]["call"]

        for v in vill_classes:
            for i in range(n_sizes):
                vill_resp_sigma_sum[v][i]["fold"] += vill_resp_sigma[v][i]["fold"]
                vill_resp_sigma_sum[v][i]["call"] += vill_resp_sigma[v][i]["call"]
                vill_resp_sigma_sum[v][i]["fourbet"] += vill_resp_sigma[v][i]["fourbet"]

    denom = float(iters)

    hero_open_avg = {
        h: {a: hero_open_sigma_sum[h][a] / denom for a in hero_open_actions_internal}
        for h in hero_classes
    }

    hero4_avg = {
        h: {
            i: {a: hero_4resp_sigma_sum[h][i][a] / denom for a in ACTION_HERO_4RESP}
            for i in range(n_sizes)
        }
        for h in hero_classes
    }

    vill_avg = {
        v: {
            i: {a: vill_resp_sigma_sum[v][i][a] / denom for a in ACTION_VIL_RESP}
            for i in range(n_sizes)
        }
        for v in vill_classes
    }

    # Output aggregated for UI: fold/call/threebet
    hero_evs_out: Dict[str, Dict[str, float]] = {}
    hero_strategy_out: Dict[str, Dict[str, float]] = {}

    for h in hero_classes:
        EV_fold = 0.0

        EV_call = 0.0
        for v in vill_classes:
            p_v = P_vill[v]
            e = eq[h][v]
            EV_call += p_v * (e * pot_call_showdown - call_cost)

        mix_three = 0.0
        ev_three_num = 0.0

        for i in range(n_sizes):
            sigma_i = hero_open_avg[h][threebet_actions[i]]
            mix_three += sigma_i
            if sigma_i <= 0:
                continue

            invest_i = hero_threebet_invest[i]

            EV_three_i = 0.0
            for v in vill_classes:
                p_v = P_vill[v]
                e = eq[h][v]

                ev_three_call = e * pot_3bet_call_showdown[i] - invest_i
                ev_four_call = e * pot_4bet_call_showdown[i] - (invest_i + hero_call_4_add[i])

                ev_four_value = (
                    hero4_avg[h][i]["fold"] * EV_4bet_hero_fold[i]
                    + hero4_avg[h][i]["call"] * ev_four_call
                )

                EV_three_i += p_v * (
                    vill_avg[v][i]["fold"] * EV_3bet_vill_fold
                    + vill_avg[v][i]["call"] * ev_three_call
                    + vill_avg[v][i]["fourbet"] * ev_four_value
                )

            ev_three_num += sigma_i * EV_three_i

        EV_three = 0.0
        if mix_three > 1e-15:
            EV_three = ev_three_num / mix_three

        hero_evs_out[h] = {"fold": EV_fold, "call": EV_call, "threebet": EV_three}
        hero_strategy_out[h] = {
            "fold": hero_open_avg[h]["fold"],
            "call": hero_open_avg[h]["call"],
            "threebet": mix_three,
        }

    return {
        "hero_pos": hero_pos,
        "params": {
            "open_size": OPEN_SIZE,
            "threebet_options": THREEBET_OPTIONS,
            "threebet_size": THREEBET_SIZE,
            "fourbet_size": FOURBET_SIZE,
            "pot_pre": POT_PRE,
            "call_cost": call_cost,
            "pot_call": pot_call,
            "pot_call_showdown": pot_call_showdown,
            "hero_posted": hero_posted,
            "rake_pct": rake_pct,
            "rake_cap": rake_cap,
            "effective_stack": effective_stack,
            "iters": iters,
            "model_tag": MODEL_TAG,
        },
        "action_labels": ACTION_HERO_OPEN,
        "hero_strategy": hero_strategy_out,
        "hero_evs": hero_evs_out,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--heroPos", required=True, choices=["CO", "BTN", "BB"], help="Defender position")
    ap.add_argument("--villOpenPos", required=False, choices=["UTG", "CO", "BTN"], default="CO", help="Villain open position")
    ap.add_argument("--out", required=True, help="Output json path")
    ap.add_argument("--iters", type=int, default=2500, help="CFR iterations")
    ap.add_argument("--equityIters", type=int, default=100, help="MC runouts per combo pairing")
    ap.add_argument("--comboSamples", type=int, default=3, help="combo-pairs samples per class-pair")
    ap.add_argument("--evReplicates", type=int, default=8, help="Replicates for EV stabilization")
    ap.add_argument("--evSeedStep", type=int, default=10007, help="Seed step between EV replicates")
    ap.add_argument("--seed", type=int, default=123)
    ap.add_argument("--rakePct", type=float, default=0.0, help="Rake as a fraction of the showdown pot (0.05 = 5%)")
    ap.add_argument("--rakeCap", type=float, default=0.0, help="Rake cap in bb. <=0 disables the cap")
    ap.add_argument("--effectiveStack", type=float, default=100.0, help="Effective stack in bb (scaffolding; default 100 has no effect)")
    ap.add_argument("--rangeProfile", default="standard", choices=["tight", "standard", "loose"], help="Static villain open-range profile")
    ap.add_argument("--useEquityCache", action="store_true", help="Cache equity matrices on disk")
    ap.add_argument("--equityCacheDir", default="", help="Directory for equity cache files")
    args = ap.parse_args()

    class_to_combos = build_class_to_combos()
    hero_classes = build_all_169_classes()

    range_profile = normalize_range_profile(args.rangeProfile)
    # Stack-aware profile: blend tight/loose toward standard on short stacks.
    # This preserves offline precomputation while making ranges more stack-realistic.
    # Stack-aware profile blend must start from the STANDARD baseline.
    # default_open_range_169(..., range_profile="standard") already applies position tuning.
    open_range_base = default_open_range_169(args.villOpenPos, range_profile="standard")
    if range_profile == "standard":
        open_range = open_range_base
    else:
        open_range = apply_range_profile_stack(
            open_range_base,
            range_profile=range_profile,
            effective_stack=args.effectiveStack,
        )
    vill_classes = sorted(list(open_range.keys()))

    combo_count = {cls: len(class_to_combos[cls]) for cls in hero_classes}
    P_hero = normalize({h: combo_count[h] for h in hero_classes})

    vill_weights = {v: open_range[v] * len(class_to_combos[v]) for v in vill_classes}
    P_vill = normalize(vill_weights)

    ev_reps = max(1, int(args.evReplicates))
    print(f"EV stabilization: {ev_reps} replicate(s)")

    ev_sum: Dict[str, Dict[str, float]] = {h: {"fold": 0.0, "call": 0.0, "threebet": 0.0} for h in hero_classes}
    ev_sumsq: Dict[str, Dict[str, float]] = {h: {"fold": 0.0, "call": 0.0, "threebet": 0.0} for h in hero_classes}

    strat_sum: Dict[str, Dict[str, float]] = {h: {"fold": 0.0, "call": 0.0, "threebet": 0.0} for h in hero_classes}

    last_out: Dict | None = None

    for rep in range(ev_reps):
        rep_seed = args.seed + rep * args.evSeedStep
        rng = random.Random(rep_seed)

        cache_dir = args.equityCacheDir.strip() or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "storage", "equity_cache"
        )
        print(f"[rep {rep+1}/{ev_reps}] Computing equity matrix (can take time)...")
        eq = get_or_compute_equity_matrix(
            hero_classes=hero_classes,
            vill_classes=vill_classes,
            class_to_combos=class_to_combos,
            equity_fn=preflop_equity_mc,
            equity_iters=args.equityIters,
            combo_samples=args.comboSamples,
            rng=rng,
            seed=rep_seed,
            use_cache=bool(args.useEquityCache),
            cache_dir=cache_dir,
            progress_label=f"[rep {rep+1}]",
        )

        print(f"[rep {rep+1}/{ev_reps}] Solving CFR strategy (dual 3bet sizing)...")
        out = solve_for_hero_pos(
            hero_pos=args.heroPos,
            hero_classes=hero_classes,
            vill_classes=vill_classes,
            eq=eq,
            P_hero=P_hero,
            P_vill=P_vill,
            iters=args.iters,
            seed=args.seed + 999 + rep,
            rake_pct=args.rakePct,
            rake_cap=args.rakeCap,
            effective_stack=args.effectiveStack,
        )

        out.setdefault("params", {})
        out["params"]["vill_open_pos"] = args.villOpenPos
        out["params"]["ev_replicates"] = ev_reps
        out["params"]["ev_rep_index"] = rep
        out["params"]["rake_pct"] = args.rakePct
        out["params"]["rake_cap"] = args.rakeCap
        out["params"]["effective_stack"] = args.effectiveStack
        out["params"]["range_profile"] = range_profile
        out["params"]["equity_iters"] = args.equityIters
        out["params"]["combo_samples"] = args.comboSamples

        last_out = out

        hero_evs = out.get("hero_evs", {})
        hero_strat = out.get("hero_strategy", {})

        for h in hero_classes:
            e = hero_evs.get(h) or {}
            st = hero_strat.get(h) or {}

            ev_sum[h]["fold"] += float(e.get("fold", 0.0))
            ev_sum[h]["call"] += float(e.get("call", 0.0))
            ev_sum[h]["threebet"] += float(e.get("threebet", 0.0))

            ev_sumsq[h]["fold"] += float(e.get("fold", 0.0)) ** 2
            ev_sumsq[h]["call"] += float(e.get("call", 0.0)) ** 2
            ev_sumsq[h]["threebet"] += float(e.get("threebet", 0.0)) ** 2

            strat_sum[h]["fold"] += float(st.get("fold", 0.0))
            strat_sum[h]["call"] += float(st.get("call", 0.0))
            strat_sum[h]["threebet"] += float(st.get("threebet", 0.0))

    if last_out is None:
        raise RuntimeError("No EV replicate output produced")

    # converter somas em média/DP
    for h in hero_classes:
        for a in ["fold", "call", "threebet"]:
            mean = ev_sum[h][a] / float(ev_reps)
            ex2 = ev_sumsq[h][a] / float(ev_reps)
            var = max(0.0, ex2 - mean * mean)
            std = var ** 0.5

            ev_sum[h][a] = mean
            ev_sumsq[h][a] = std

            strat_sum[h][a] = strat_sum[h][a] / float(ev_reps)

    hero_evs_mean = ev_sum
    hero_evs_std = ev_sumsq
    hero_strategy_mean = strat_sum

    last_out["hero_evs"] = hero_evs_mean
    last_out["hero_strategy"] = hero_strategy_mean
    last_out["hero_evs_std"] = hero_evs_std

    last_out.setdefault("params", {})
    last_out["params"]["ev_replicates"] = ev_reps
    last_out["params"]["range_profile"] = range_profile
    last_out["params"]["equity_iters"] = args.equityIters
    last_out["params"]["combo_samples"] = args.comboSamples

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(last_out, f, ensure_ascii=False)

    print("Wrote:", args.out)


if __name__ == "__main__":
    main()
