"""Postflop hand evaluator, equity calculator, and combo classifier using eval7.

Provides:
- 52-card deck and combo helpers with board card removal (blockers).
- Hand categorization (Overpair, Top Pair, Set, Flush Draw, OESD, etc.) on given boards.
- Fast combo vs combo and range vs range equity calculation.
"""

from __future__ import annotations

import itertools
import random
from typing import Any, Dict, List, Optional, Set, Tuple

import eval7

# Cache for equity matrices to avoid recomputation for same board
_equity_cache: Dict[Tuple[str, ...], Dict[Tuple[str, str], Dict[Tuple[str, str], float]]] = {}

RANKS = "AKQJT98765432"
SUITS = "shdc"

ALL_CARDS = [f"{r}{s}" for r in RANKS for s in SUITS]


def parse_card(card_str: str) -> eval7.Card:
    """Convert string like 'As' or 'Kd' to eval7.Card."""
    c = str(card_str).strip()
    if len(c) != 2:
        raise ValueError(f"Invalid card string: '{card_str}'")
    rank, suit = c[0].upper(), c[1].lower()
    if rank not in RANKS or suit not in SUITS:
        raise ValueError(f"Invalid card string: '{card_str}'")
    return eval7.Card(f"{rank}{suit}")


def parse_board(board_strs: List[str]) -> List[eval7.Card]:
    """Convert list of card strings to eval7.Cards."""
    return [parse_card(c) for c in board_strs]


def infer_class_from_cards(c1: str, c2: str) -> str:
    """Map concrete 2-card combo to 169-style class key (e.g. 'AKs', '77', 'QTo')."""
    r1, s1 = c1[0].upper(), c1[1].lower()
    r2, s2 = c2[0].upper(), c2[1].lower()

    if r1 == r2:
        return r1 + r2

    i1 = RANKS.index(r1)
    i2 = RANKS.index(r2)
    hi, lo = (r1, r2) if i1 < i2 else (r2, r1)
    suited = (s1 == s2)
    return hi + lo + ("s" if suited else "o")


def generate_all_combos() -> List[Tuple[str, str]]:
    """Generate all 1,326 two-card combos in canonical order."""
    combos = []
    for i in range(len(ALL_CARDS)):
        for j in range(i + 1, len(ALL_CARDS)):
            combos.append((ALL_CARDS[i], ALL_CARDS[j]))
    return combos


ALL_COMBOS_1326 = generate_all_combos()


def build_class_to_combos() -> Dict[str, List[Tuple[str, str]]]:
    """Map 169 hand classes to their specific 2-card card combos."""
    class_to: Dict[str, List[Tuple[str, str]]] = {}
    for combo in ALL_COMBOS_1326:
        cls = infer_class_from_cards(combo[0], combo[1])
        if cls not in class_to:
            class_to[cls] = []
        class_to[cls].append(combo)
    return class_to


CLASS_TO_COMBOS = build_class_to_combos()


def expand_range_to_combos(
    class_weights: Dict[str, float],
    dead_cards: Optional[Set[str]] = None,
) -> Dict[Tuple[str, str], float]:
    """Expand a 169-hand weight dict into concrete 2-card combos, filtering dead cards."""
    dead = set(dead_cards or [])
    combo_weights: Dict[Tuple[str, str], float] = {}

    for cls, weight in class_weights.items():
        if weight <= 0.0:
            continue
        combos = CLASS_TO_COMBOS.get(cls, [])
        for c1, c2 in combos:
            if c1 in dead or c2 in dead:
                continue
            combo_weights[(c1, c2)] = float(weight)

    return combo_weights


def aggregate_combos_to_classes(
    combo_values: Dict[Tuple[str, str], float],
    combo_weights: Optional[Dict[Tuple[str, str], float]] = None,
) -> Dict[str, float]:
    """Aggregate combo-level values back into 169 hand class averages."""
    class_sum: Dict[str, float] = {}
    class_weight_sum: Dict[str, float] = {}

    for (c1, c2), val in combo_values.items():
        cls = infer_class_from_cards(c1, c2)
        w = combo_weights.get((c1, c2), 1.0) if combo_weights else 1.0
        if w <= 0.0:
            continue
        class_sum[cls] = class_sum.get(cls, 0.0) + val * w
        class_weight_sum[cls] = class_weight_sum.get(cls, 0.0) + w

    out: Dict[str, float] = {}
    for cls in CLASS_TO_COMBOS.keys():
        w_sum = class_weight_sum.get(cls, 0.0)
        if w_sum > 0:
            out[cls] = class_sum[cls] / w_sum
        else:
            out[cls] = 0.0

    return out


# --- Hand Strength & Texture Categorizer ---

def categorize_hand_on_board(combo: Tuple[str, str], board: List[str]) -> Dict[str, any]:
    """Categorize a 2-card combo on a given board (3 to 5 cards).

    Returns a dict with:
    - category: Top Pair, Overpair, Set, Two Pair, Flush, Straight, Flush Draw, OESD, Air, etc.
    - score: eval7 integer score
    - is_made_hand: bool
    - is_draw: bool
    - details: human-readable details
    """
    c1, c2 = combo[0], combo[1]
    all_cards_str = [c1, c2] + list(board)
    eval_cards = [eval7.Card(c) for c in all_cards_str]
    score = eval7.evaluate(eval_cards)

    # eval7 hand type: score >> 24 gives hand rank type
    # 0: High Card, 1: Pair, 2: Two Pair, 3: Trips/Set, 4: Straight, 5: Flush, 6: Full House, 7: Quads, 8: Straight Flush
    hand_type_id = score >> 24
    hand_type_names = {
        0: "High Card",
        1: "Pair",
        2: "Two Pair",
        3: "Three of a Kind",
        4: "Straight",
        5: "Flush",
        6: "Full House",
        7: "Four of a Kind",
        8: "Straight Flush",
    }

    r1, s1 = c1[0], c1[1]
    r2, s2 = c2[0], c2[1]
    board_ranks = [b[0] for b in board]
    board_suits = [b[1] for b in board]

    # Rank indices (0 = Ace, 12 = Deuce)
    board_rank_indices = sorted([RANKS.index(r) for r in board_ranks])
    highest_board_idx = board_rank_indices[0]  # Lowest index = highest rank
    lowest_board_idx = board_rank_indices[-1]

    i1, i2 = RANKS.index(r1), RANKS.index(r2)
    pocket_pair = (r1 == r2)

    category = hand_type_names.get(hand_type_id, "High Card")
    draw_category = None

    # Refine Pairs
    if hand_type_id == 1:
        if pocket_pair:
            if i1 < highest_board_idx:
                category = "Overpair"
            elif i1 > lowest_board_idx:
                category = "Underpair"
            else:
                category = "Middle Pocket Pair"
        else:
            if r1 in board_ranks or r2 in board_ranks:
                paired_rank = r1 if r1 in board_ranks else r2
                paired_idx = RANKS.index(paired_rank)
                kicker = r2 if r1 in board_ranks else r1
                kicker_idx = RANKS.index(kicker)

                if paired_idx == highest_board_idx:
                    kicker_label = "Top Kicker" if kicker_idx <= 2 else "Weak Kicker"
                    category = f"Top Pair ({kicker_label})"
                elif paired_idx == lowest_board_idx:
                    category = "Bottom Pair"
                else:
                    category = "Middle Pair"
            else:
                # Board itself paired
                category = "Board Paired (High Card)"

    elif hand_type_id == 3:
        if pocket_pair and r1 in board_ranks:
            category = "Set"
        else:
            category = "Trips"

    # Check for Draws if board has 3 or 4 cards and hand is not already monster
    if len(board) in (3, 4) and hand_type_id < 4:
        # Flush Draw check
        suit_counts = {}
        for s in board_suits + [s1, s2]:
            suit_counts[s] = suit_counts.get(s, 0) + 1
        for s, count in suit_counts.items():
            if count == 4:
                # Check if hero holds cards of this suit
                hero_suited_cards = [c for c in (c1, c2) if c[1] == s]
                if len(hero_suited_cards) >= 1:
                    has_nut_suit = any(c[0] == 'A' for c in hero_suited_cards)
                    draw_category = "Nut Flush Draw" if has_nut_suit else "Flush Draw"
                    break

        # Straight Draw check over all 10 possible 5-card straight windows.
        # The wheel (A-2-3-4-5) uses Ace as index 13 (low), so we include it in
        # the rank set when an Ace is present. Each window is a set of 5
        # consecutive rank indices; a 4-of-5 match is a draw (OESD if the
        # missing card is at either end, gutshot otherwise).
        unique_ranks_idx = sorted(set([i1, i2] + board_rank_indices))
        has_ace = 0 in unique_ranks_idx
        if has_ace:
            unique_ranks_idx.append(13)  # Ace as 1 (low) for wheel detection

        # All 10 valid 5-card straight windows:
        #   0-4 (A high), 1-5, ..., 8-12 (K low), and 9-13 (wheel: A-2-3-4-5)
        straight_windows = [set(range(s, s + 5)) for s in range(9)] + [set(range(9, 14))]
        rank_set = set(unique_ranks_idx)

        is_oesd = False
        is_gutshot = False
        for window in straight_windows:
            intersect = window.intersection(rank_set)
            if len(intersect) == 4:
                missing = list(window - intersect)[0]
                if missing == min(window) or missing == max(window):
                    is_oesd = True
                else:
                    is_gutshot = True
                break  # first qualifying draw wins; no need to keep scanning

        if is_oesd:
            draw_category = "OESD" if not draw_category else f"{draw_category} + OESD"
        elif is_gutshot and not draw_category:
            draw_category = "Gutshot"

    return {
        "category": category,
        "draw": draw_category,
        "score": score,
        "hand_type_id": hand_type_id,
        "is_made_hand": hand_type_id >= 1,
        "is_draw": draw_category is not None,
    }


# --- Fast Equity Calculations ---

def evaluate_showdown_fast(
    c1_cards: Tuple[eval7.Card, eval7.Card],
    c2_cards: Tuple[eval7.Card, eval7.Card],
    board_cards: List[eval7.Card],
) -> float:
    """Return 1.0 if player 1 wins, 0.5 for tie, 0.0 if player 2 wins on a 5-card board."""
    s1 = eval7.evaluate(list(c1_cards) + board_cards)
    s2 = eval7.evaluate(list(c2_cards) + board_cards)
    if s1 > s2:
        return 1.0
    elif s1 < s2:
        return 0.0
    return 0.5


def compute_postflop_equity_matrix(
    combos_oop: List[Tuple[str, str]],
    combos_ip: List[Tuple[str, str]],
    board: List[str],
    iters_per_matchup: int = 50,
    seed: int = 42,
    *,
    exact_flop: bool = False,
    max_exact_combo_pairs: int = 250,
) -> Dict[Tuple[str, str], Dict[Tuple[str, str], float]]:
    """Compute equity matrix E[c_oop, c_ip] on a given board.

    Optimized for hand-continuous flows:
    - River (len=5): Exact deterministic showdown (no sampling)
    - Turn (len=4): Exact rollout (1 card) - no Monte Carlo
    - Flop (len=3): Efficient sampling with fixed seed
    - Cache reuse: same board+params reuses previously computed matrix
    """
    # Module-level cache: same board + params -> same equity matrix.
    # Useful when hand-continuous flows re-solve a board with different
    # pot/investment/bet configurations (solver instances).
    cache_key = (
        tuple(board),
        int(iters_per_matchup),
        int(seed),
        bool(exact_flop),
        int(max_exact_combo_pairs),
        tuple(sorted(combos_oop)),
        tuple(sorted(combos_ip)),
    )
    cached = _equity_cache.get(cache_key)
    if cached is not None:
        return cached

    board_eval7 = [parse_card(c) for c in board]
    dead_board = set(board)
    all_deck = [c for c in ALL_CARDS if c not in dead_board]

    eq_matrix: Dict[Tuple[str, str], Dict[Tuple[str, str], float]] = {c: {} for c in combos_oop}

    if len(board) == 5:
        # River: Instant deterministic showdown
        for c_oop in combos_oop:
            cards_oop = (parse_card(c_oop[0]), parse_card(c_oop[1]))
            for c_ip in combos_ip:
                if c_oop[0] == c_ip[0] or c_oop[0] == c_ip[1] or c_oop[1] == c_ip[0] or c_oop[1] == c_ip[1]:
                    continue
                cards_ip = (parse_card(c_ip[0]), parse_card(c_ip[1]))
                eq_matrix[c_oop][c_ip] = evaluate_showdown_fast(cards_oop, cards_ip, board_eval7)

    elif len(board) == 4:
        # Turn: Exact rollout over all remaining river cards (44 cards)
        # No Monte Carlo - exact calculation (more efficient than sampling)
        for c_oop in combos_oop:
            cards_oop = (parse_card(c_oop[0]), parse_card(c_oop[1]))
            set_oop = {c_oop[0], c_oop[1]}
            for c_ip in combos_ip:
                if c_ip[0] in set_oop or c_ip[1] in set_oop:
                    continue
                cards_ip = (parse_card(c_ip[0]), parse_card(c_ip[1]))
                set_both = set_oop.union({c_ip[0], c_ip[1]})
                valid_rivers = [parse_card(c) for c in all_deck if c not in set_both]

                wins = 0.0
                for r_card in valid_rivers:
                    wins += evaluate_showdown_fast(cards_oop, cards_ip, board_eval7 + [r_card])
                eq_matrix[c_oop][c_ip] = wins / float(len(valid_rivers))

    else:
        # Flop (len=3): optionally exact enumeration, otherwise Monte-Carlo.
        rng = random.Random(seed)

        # Exact flop equity enumerates ALL turn+river runouts.
        # It's expensive (C(45,2) = 990 runouts per combo pair), so it is opt-in.
        if exact_flop:
            for c_oop in combos_oop:
                cards_oop = (parse_card(c_oop[0]), parse_card(c_oop[1]))
                set_oop = {c_oop[0], c_oop[1]}
                for c_ip in combos_ip:
                    if c_ip[0] in set_oop or c_ip[1] in set_oop:
                        continue
                    cards_ip = (parse_card(c_ip[0]), parse_card(c_ip[1]))
                    set_both = set_oop.union({c_ip[0], c_ip[1]})
                    remaining_deck = [parse_card(c) for c in all_deck if c not in set_both]

                    wins = 0.0
                    n_runouts = 0
                    for i in range(len(remaining_deck)):
                        for j in range(i + 1, len(remaining_deck)):
                            wins += evaluate_showdown_fast(
                                cards_oop,
                                cards_ip,
                                board_eval7 + [remaining_deck[i], remaining_deck[j]],
                            )
                            n_runouts += 1

                    eq_matrix[c_oop][c_ip] = (wins / n_runouts) if n_runouts else 0.5
        else:
            for c_oop in combos_oop:
                cards_oop = (parse_card(c_oop[0]), parse_card(c_oop[1]))
                set_oop = {c_oop[0], c_oop[1]}
                for c_ip in combos_ip:
                    if c_ip[0] in set_oop or c_ip[1] in set_oop:
                        continue
                    cards_ip = (parse_card(c_ip[0]), parse_card(c_ip[1]))
                    set_both = set_oop.union({c_ip[0], c_ip[1]})
                    remaining_deck = [parse_card(c) for c in all_deck if c not in set_both]

                    # Sample runouts (Monte-Carlo). Use a common random draw per
                    # pair so that equity differences stay consistent across the
                    # matrix (reduces the noise floor of the CFR solve).
                    max_possible = len(remaining_deck) * (len(remaining_deck) - 1) // 2
                    n_samples = min(iters_per_matchup, max_possible)
                    n_samples = max(4, n_samples)  # Minimum for statistical validity

                    wins = 0.0
                    for _ in range(n_samples):
                        runout = rng.sample(remaining_deck, 2)
                        wins += evaluate_showdown_fast(cards_oop, cards_ip, board_eval7 + runout)
                    eq_matrix[c_oop][c_ip] = wins / float(n_samples)

    # Store in cache for future reuse (note: returning the full matrix, not just slice)
    _equity_cache[cache_key] = eq_matrix

    return eq_matrix
