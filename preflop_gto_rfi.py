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
# Internal dual/quad 3bet grid (output is still aggregated into one external 'threebet' action)
THREEBET_OPTIONS = [6.5, 7.5, 9.0, 10.5]
THREEBET_SIZE = THREEBET_OPTIONS[-1]  # backward-compatible single-size value
FOURBET_SIZE = 21.0

# Pot model consistent with preflop_gto_defend.py
# blinds(SB+BB)=2.0, plus open=2.2 => 4.2 when hero is not BB.
POT_PRE = 4.2

# External contract (Mode 1 UI/API): still 3 actions.
# In RFI mode, the hero decision happens AFTER villain 3BET,
# so the hero "3BET" action corresponds to FOURBET in the real tree.
ACTION_HERO_AFTER_THREEBET = ["fold", "call", "threebet"]

MODEL_TAG = "mode1-preflop-discrete-rfi-v1"


def infer_class_from_cards(c1: str, c2: str) -> str:
    """Map concrete 2-card combos to a 169-style bucket key."""
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
    """Open-range weights by villain position (reused for RFI response modeling)."""
    vill_open_pos = (vill_open_pos or "CO").upper().strip()
    if vill_open_pos not in ["UTG", "CO", "BTN"]:
        vill_open_pos = "CO"

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
            classes.append(ranks[i] + ranks[j] + "s")
            classes.append(ranks[i] + ranks[j] + "o")

    return classes


def regret_matching(regrets: Dict[str, float], actions: List[str]) -> Dict[str, float]:
    # CFR+ (RM+): floor accumulated regrets at 0, then mix on positive mass.
    return regret_matching_plus(regrets, actions)


def solve_for_rfi_response(
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
    open_size: float = OPEN_SIZE,
) -> Dict:
    """Approximate CFR-like solving for RFI.

    Tree (abstracted):
      Hero OPEN (fixed) -> Villain responds:
        fold / call / threebet_i (dual sizes)
      If villain threebets i: Hero responds:
        fold / call / fourbet (mapped to UI 'threebet')
      If hero fourbets: villain responds:
        fold / call

    Output EV/strategy are aggregated for the hero decision AFTER villain 3BET.
    External contract: [fold, call, threebet] where 'threebet' == hero FOURBET.
    """

    hero_posted = 1.0 if hero_pos == "BB" else 0.0

    # Hero already opened: remaining chips hero can put in are capped by stack.
    hero_open_total = cap_total(float(open_size), effective_stack)
    # If hero is BB, OPEN replaces the 1.0 posted; dead money is the leftover SB.
    pot_pre = 2.0 + float(open_size)
    dead_money = max(0.0, pot_pre - hero_posted - float(open_size))
    pot_open = dead_money + hero_open_total

    open_total = hero_open_total

    # Villain flats the open: both committed to (capped) open size.
    vill_flat_total = cap_total(hero_open_total, effective_stack)
    pot_call_after_vill_call = dead_money + hero_open_total + vill_flat_total
    pot_call_after_vill_call_showdown = apply_rake_to_pot(
        pot_call_after_vill_call, rake_pct, rake_cap
    )

    n_sizes = len(THREEBET_OPTIONS)
    threebet_actions = [f"threebet_{i}" for i in range(n_sizes)]

    hero_dec_actions = ["fold", "call", "fourbet"]
    vill_dec_actions = ["fold", "call"] + threebet_actions

    vill_four_actions = ["fold", "call"]

    # Precompute pot/invest terms per 3bet size (all-in capped).
    pot_before_hero_dec = []  # pot after villain 3bet, before hero decision
    pot_after_call = []  # pot after hero call (showdown)
    pot_after_call_showdown = []
    call_add = []

    four_target = cap_total(FOURBET_SIZE, effective_stack)
    four_add = max(0.0, four_target - hero_open_total)
    pot_final_after_four_call = dead_money + four_target + four_target
    pot_four_call_showdown = apply_rake_to_pot(
        pot_final_after_four_call, rake_pct, rake_cap
    )

    for s in THREEBET_OPTIONS:
        vill_3_total = cap_total(s, effective_stack)
        pot_b = dead_money + hero_open_total + vill_3_total
        pot_before_hero_dec.append(pot_b)

        # Hero calling the 3bet matches villain's (capped) total.
        hero_call_total = cap_total(vill_3_total, effective_stack)
        c_add = max(0.0, hero_call_total - hero_open_total)
        call_add.append(c_add)

        pot_c = dead_money + hero_call_total + vill_3_total
        pot_after_call.append(pot_c)
        pot_after_call_showdown.append(apply_rake_to_pot(pot_c, rake_pct, rake_cap))

    # Strategies/regrets (initialized uniformly)
    hero_resp_sigma = {
        h: {i: {"fold": 1/3, "call": 1/3, "fourbet": 1/3} for i in range(n_sizes)}
        for h in hero_classes
    }
    hero_resp_regrets = {
        h: {i: {a: 0.0 for a in hero_dec_actions} for i in range(n_sizes)}
        for h in hero_classes
    }
    hero_resp_sigma_sum = {
        h: {i: {a: 0.0 for a in hero_dec_actions} for i in range(n_sizes)}
        for h in hero_classes
    }

    vill_resp_sigma = {
        v: {a: 1.0 / len(vill_dec_actions) for a in vill_dec_actions}
        for v in vill_classes
    }
    vill_resp_regrets = {
        v: {a: 0.0 for a in vill_dec_actions}
        for v in vill_classes
    }
    vill_resp_sigma_sum = {
        v: {a: 0.0 for a in vill_dec_actions}
        for v in vill_classes
    }

    vill_four_sigma = {
        v: {i: {"fold": 0.5, "call": 0.5} for i in range(n_sizes)}
        for v in vill_classes
    }
    vill_four_regrets = {
        v: {i: {a: 0.0 for a in vill_four_actions} for i in range(n_sizes)}
        for v in vill_classes
    }
    vill_four_sigma_sum = {
        v: {i: {a: 0.0 for a in vill_four_actions} for i in range(n_sizes)}
        for v in vill_classes
    }

    for _it in range(iters):
        # 1) Update hero decision (after villain 3BET_i)
        for h in hero_classes:
            for i in range(n_sizes):
                # conditional distribution of villain hands given villain chose this 3bet size
                w_sum = 0.0
                for v in vill_classes:
                    w_sum += P_vill[v] * vill_resp_sigma[v][threebet_actions[i]]

                if w_sum <= 1e-15:
                    continue

                # fold EV (baseline EV=0 at this node)
                u_fold = 0.0

                # call EV
                u_call = 0.0
                # fourbet EV depends on villain's response to the 4bet
                u_four = 0.0

                s = THREEBET_OPTIONS[i]

                for v in vill_classes:
                    p = P_vill[v] * vill_resp_sigma[v][threebet_actions[i]]
                    if p <= 0:
                        continue
                    prob = p / w_sum
                    e = eq[h][v]

                    # call
                    u_call += prob * (e * pot_after_call_showdown[i] - call_add[i])

                    # fourbet: villain fold gives pot_before_hero_dec[i], call gives equity showdown
                    p_fold4 = vill_four_sigma[v][i]["fold"]
                    p_call4 = vill_four_sigma[v][i]["call"]

                    ev_four_if_fold = pot_before_hero_dec[i]
                    ev_four_if_call = e * pot_four_call_showdown - four_add

                    u_four += prob * (p_fold4 * ev_four_if_fold + p_call4 * ev_four_if_call)

                sigma = hero_resp_sigma[h][i]
                u_avg = sigma["fold"] * u_fold + sigma["call"] * u_call + sigma["fourbet"] * u_four

                hero_resp_regrets[h][i]["fold"] += (u_fold - u_avg)
                hero_resp_regrets[h][i]["call"] += (u_call - u_avg)
                hero_resp_regrets[h][i]["fourbet"] += (u_four - u_avg)

                hero_resp_sigma[h][i] = regret_matching(hero_resp_regrets[h][i], hero_dec_actions)

        # 2) Update villain response (after hero OPEN)
        for v in vill_classes:
            # hero utility if villain folds to open (vill fold -> hero wins pot, but baseline excludes hero open)
            hero_u_fold = pot_open - open_total

            # hero utility if villain calls open -> showdown
            hero_u_call = 0.0
            for h in hero_classes:
                e = eq[h][v]
                hero_u_call += P_hero[h] * (e * pot_call_after_vill_call_showdown - open_total)

            # hero utility if villain threebets_i -> hero decision node
            hero_u_three = [0.0 for _ in range(n_sizes)]
            for i in range(n_sizes):
                s = THREEBET_OPTIONS[i]

                for h in hero_classes:
                    e = eq[h][v]
                    sig_h = hero_resp_sigma[h][i]

                    # hero fold: 0 EV at this node
                    ev_call = e * pot_after_call_showdown[i] - call_add[i]

                    p_fold4 = vill_four_sigma[v][i]["fold"]
                    p_call4 = vill_four_sigma[v][i]["call"]
                    ev_four_if_fold = pot_before_hero_dec[i]
                    ev_four_if_call = e * pot_four_call_showdown - four_add
                    ev_four = p_fold4 * ev_four_if_fold + p_call4 * ev_four_if_call

                    hero_u_three[i] += P_hero[h] * (
                        sig_h["fold"] * 0.0 + sig_h["call"] * ev_call + sig_h["fourbet"] * ev_four
                    )

            # villain utility is negative of hero utility
            V_fold = -hero_u_fold
            V_call = -hero_u_call

            V_three = [-x for x in hero_u_three]

            # current expected villain utility under sigma
            u_avg_total = (
                vill_resp_sigma[v]["fold"] * V_fold
                + vill_resp_sigma[v]["call"] * V_call
            )

            for i in range(n_sizes):
                u_avg_total += vill_resp_sigma[v][threebet_actions[i]] * V_three[i]

            vill_resp_regrets[v]["fold"] += (V_fold - u_avg_total)
            vill_resp_regrets[v]["call"] += (V_call - u_avg_total)
            for i in range(n_sizes):
                vill_resp_regrets[v][threebet_actions[i]] += (V_three[i] - u_avg_total)

            vill_resp_sigma[v] = regret_matching(vill_resp_regrets[v], vill_dec_actions)

        # 3) Update villain fourbet response (after hero FOURBET)
        for v in vill_classes:
            for i in range(n_sizes):
                # only reached if hero chooses fourbet at that i
                w_sum = 0.0
                for h in hero_classes:
                    w_sum += P_hero[h] * hero_resp_sigma[h][i]["fourbet"]

                if w_sum <= 1e-15:
                    continue

                u_fold_total = 0.0
                u_call_total = 0.0

                for h in hero_classes:
                    w_h = P_hero[h] * hero_resp_sigma[h][i]["fourbet"]
                    if w_h <= 0:
                        continue
                    e = eq[h][v]

                    # if villain folds the 4bet, hero wins pot_before_hero_dec[i]
                    u_fold_total += w_h * (-(pot_before_hero_dec[i]))

                    # if villain calls, hero equity showdown profit is (e*pot_final - four_add)
                    u_call_total += w_h * (-(e * pot_four_call_showdown - four_add))

                u_avg = vill_four_sigma[v][i]["fold"] * u_fold_total + vill_four_sigma[v][i]["call"] * u_call_total

                vill_four_regrets[v][i]["fold"] += (u_fold_total - u_avg)
                vill_four_regrets[v][i]["call"] += (u_call_total - u_avg)

                vill_four_sigma[v][i] = regret_matching(vill_four_regrets[v][i], vill_four_actions)

        # accumulate averages
        for h in hero_classes:
            for i in range(n_sizes):
                for a in hero_dec_actions:
                    hero_resp_sigma_sum[h][i][a] += hero_resp_sigma[h][i][a]

        for v in vill_classes:
            for a in vill_dec_actions:
                vill_resp_sigma_sum[v][a] += vill_resp_sigma[v][a]

        for v in vill_classes:
            for i in range(n_sizes):
                for a in vill_four_actions:
                    vill_four_sigma_sum[v][i][a] += vill_four_sigma[v][i][a]

    denom = float(iters)

    hero_resp_avg = {
        h: {
            i: {
                a: hero_resp_sigma_sum[h][i][a] / denom for a in hero_dec_actions
            }
            for i in range(n_sizes)
        }
        for h in hero_classes
    }

    vill_resp_avg = {
        v: {
            a: vill_resp_sigma_sum[v][a] / denom for a in vill_dec_actions
        }
        for v in vill_classes
    }

    vill_four_avg = {
        v: {
            i: {a: vill_four_sigma_sum[v][i][a] / denom for a in vill_four_actions}
            for i in range(n_sizes)
        }
        for v in vill_classes
    }

    # Aggregation over 3bet sizes, conditioned on villain actually 3betting.
    # mass_i = P(vill chooses threebet_i)
    mass_i = [0.0 for _ in range(n_sizes)]
    for i in range(n_sizes):
        a = threebet_actions[i]
        mass_i[i] = sum(P_vill[v] * vill_resp_avg[v][a] for v in vill_classes)

    denom_three = sum(mass_i)
    if denom_three <= 1e-15:
        # degenerate case: no 3bet mass
        hero_evs_out = {
            h: {"fold": 0.0, "call": 0.0, "threebet": 0.0} for h in hero_classes
        }
        hero_strategy_out = {
            h: {"fold": 1/3, "call": 1/3, "threebet": 1/3} for h in hero_classes
        }
        return {
            "hero_pos": hero_pos,
            "params": {
                "open_size": float(open_size),
                "threebet_options": THREEBET_OPTIONS,
                "threebet_size": THREEBET_SIZE,
                "fourbet_size": FOURBET_SIZE,
                "pot_pre": pot_pre,
            },
            "action_labels": ACTION_HERO_AFTER_THREEBET,
            "hero_strategy": hero_strategy_out,
            "hero_evs": hero_evs_out,
        }

    hero_evs_out: Dict[str, Dict[str, float]] = {}
    hero_strategy_out: Dict[str, Dict[str, float]] = {}

    # pot values constant across hero classes for each i
    for h in hero_classes:
        # EVs for each pure action at the hero decision point (fold/call/fourbet)
        ev_call_num = 0.0
        ev_four_num = 0.0

        for i in range(n_sizes):
            # weight over villain 3bet_i mass
            weight_i = mass_i[i]  # already aggregated over villain hand

            if weight_i <= 0:
                continue

            for v in vill_classes:
                p_v = P_vill[v] * vill_resp_avg[v][threebet_actions[i]]
                if p_v <= 0:
                    continue
                e = eq[h][v]

                ev_call_num += p_v * (e * pot_after_call_showdown[i] - call_add[i])

                p_fold4 = vill_four_avg[v][i]["fold"]
                p_call4 = vill_four_avg[v][i]["call"]

                ev_four_if_fold = pot_before_hero_dec[i]
                ev_four_if_call = e * pot_four_call_showdown - four_add
                ev_four = p_fold4 * ev_four_if_fold + p_call4 * ev_four_if_call

                ev_four_num += p_v * ev_four

        EV_call = ev_call_num / denom_three
        EV_four = ev_four_num / denom_three

        # aggregated mixed strategy (conditional on villain 3bet)
        mix_fold = 0.0
        mix_call = 0.0
        mix_four = 0.0
        for i in range(n_sizes):
            if mass_i[i] <= 0:
                continue
            w = mass_i[i] / denom_three
            mix_fold += w * hero_resp_avg[h][i]["fold"]
            mix_call += w * hero_resp_avg[h][i]["call"]
            mix_four += w * hero_resp_avg[h][i]["fourbet"]

        # map internal fourbet -> UI 'threebet'
        hero_evs_out[h] = {"fold": 0.0, "call": EV_call, "threebet": EV_four}
        hero_strategy_out[h] = {"fold": mix_fold, "call": mix_call, "threebet": mix_four}

    return {
        "hero_pos": hero_pos,
        "params": {
            "open_size": float(open_size),
            "threebet_options": THREEBET_OPTIONS,
            "threebet_size": THREEBET_SIZE,
            "fourbet_size": FOURBET_SIZE,
            "pot_pre": pot_pre,
            "pot_open": pot_open,
            "rake_pct": rake_pct,
            "rake_cap": rake_cap,
            "effective_stack": effective_stack,
            "pot_call_after_vill_call_showdown": pot_call_after_vill_call_showdown,
            "pot_four_call_showdown": pot_four_call_showdown,
            "iters": iters,
            "model_tag": MODEL_TAG,
        },
        "action_labels": ACTION_HERO_AFTER_THREEBET,
        "hero_strategy": hero_strategy_out,
        "hero_evs": hero_evs_out,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--heroPos", required=True, choices=["CO", "BTN", "BB"], help="Opener (hero) position")
    ap.add_argument(
        "--villOpenPos",
        required=False,
        choices=["UTG", "CO", "BTN"],
        default="CO",
        help="Villain position (used to parameterize range)",
    )
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
    ap.add_argument("--openSize", type=float, default=OPEN_SIZE, help="Open size in bb (default 2.2)")
    ap.add_argument("--useEquityCache", action="store_true", help="Cache equity matrices on disk")
    ap.add_argument("--equityCacheDir", default="", help="Directory for equity cache files")
    args = ap.parse_args()

    class_to_combos = build_class_to_combos()
    hero_classes = build_all_169_classes()

    range_profile = normalize_range_profile(getattr(args, "rangeProfile", "standard"))
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

    last_out = None

    for rep in range(ev_reps):
        rep_seed = args.seed + rep * args.evSeedStep
        rng = random.Random(rep_seed)

        print(f"[rep {rep+1}/{ev_reps}] Computing equity matrix...")
        cache_dir = args.equityCacheDir.strip() or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "storage", "equity_cache"
        )

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

        print(f"[rep {rep+1}/{ev_reps}] Solving CFR (RFI tree)...")
        out = solve_for_rfi_response(
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
            open_size=float(args.openSize),
        )

        out.setdefault("params", {})
        out["params"]["vill_open_pos"] = args.villOpenPos
        out["params"]["range_profile"] = range_profile
        out["params"]["equity_iters"] = args.equityIters
        out["params"]["combo_samples"] = args.comboSamples
        out["params"]["ev_replicates"] = ev_reps
        out["params"]["ev_rep_index"] = rep
        out["params"]["rake_pct"] = args.rakePct
        out["params"]["rake_cap"] = args.rakeCap
        out["params"]["effective_stack"] = args.effectiveStack
        out["params"]["open_size"] = float(args.openSize)

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

    # convert sums to mean/std
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

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(last_out, f, ensure_ascii=False)

    print("Wrote:", args.out)


if __name__ == "__main__":
    main()
