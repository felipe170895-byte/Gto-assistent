"""Postflop Service: Connects preflop ranges from JSON storage with the Postflop CFR+ Solver.

Provides:
- Range extraction for OOP and IP based on preflop spot (e.g. BB vs BTN open -> BB Defend Calling range & BTN Open range).
- High-level postflop solving API with caching of solved subgames.
- Hand texture analysis and category distribution.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple

import hashlib
import postflop_evaluator as pe
from postflop_solver import PostflopBetConfig, PostflopCFRSolver

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
STORAGE_DIR = os.path.join(ROOT_DIR, "storage")


class LRUCache:
    """LRU Cache with SHA-256 keys for deterministic subgame identification."""

    def __init__(self, max_size: int = 50):
        self.max_size = max_size
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._access_order: List[str] = []  # Most recently used at end
        self._stats = {"hits": 0, "misses": 0, "evictions": 0}

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        """Get item from cache. Returns None if not found."""
        if key in self._cache:
            # Move to end (most recently used)
            self._access_order.remove(key)
            self._access_order.append(key)
            self._stats["hits"] += 1
            return self._cache[key]
        self._stats["misses"] += 1
        return None

    def put(self, key: str, value: Dict[str, Any]) -> None:
        """Add item to cache. Evicts LRU if at capacity."""
        if key in self._cache:
            # Update existing and move to end
            self._access_order.remove(key)
        elif len(self._cache) >= self.max_size:
            # Evict LRU (first item)
            lru_key = self._access_order.pop(0)
            del self._cache[lru_key]
            self._stats["evictions"] += 1

        self._cache[key] = value
        self._access_order.append(key)

    def clear(self) -> None:
        """Clear all cache entries."""
        self._cache.clear()
        self._access_order.clear()

    @property
    def stats(self) -> Dict[str, int]:
        """Return cache statistics."""
        return self._stats.copy()

    @property
    def size(self) -> int:
        """Current number of items in cache."""
        return len(self._cache)


# Global LRU cache for postflop solved subgames
# Key = SHA-256 of board + ranges + pot/stack + bet config + hero cards + investments
_POSTFLOP_CACHE = LRUCache(max_size=50)


def get_preflop_calling_range(
    hero_pos: str = "BB",
    vill_open_pos: str = "BTN",
    stack: int = 100,
    range_profile: str = "standard",
    open_size: float = 2.2,
    include_threebet: bool = True,
) -> Dict[str, float]:
    """Extract calling range weights (169 classes) for defender from preflop defend JSON.

    Args:
        include_threebet: If True, include 3bet range in the result (defending range = call + 3bet).
                          If False, return only the pure calling range (for hand-continuous CALL spots).
    """
    fname = f"preflop_gto_defend_{hero_pos}_vsOpen_{vill_open_pos}.json"
    fpath = os.path.join(STORAGE_DIR, fname)

    # Fallback to legacy filename if specific not found
    if not os.path.exists(fpath):
        fpath = os.path.join(STORAGE_DIR, f"preflop_gto_defend_{hero_pos}.json")

    calling_range: Dict[str, float] = {}

    if os.path.exists(fpath):
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                data = json.load(f)
            strat = data.get("hero_strategy", {})
            for hand_cls, freqs in strat.items():
                call_freq = float(freqs.get("call", 0.0))
                threebet_freq = float(freqs.get("threebet", 0.0))

                if include_threebet:
                    # Defending range = call + 3bet
                    defend_freq = call_freq + threebet_freq
                else:
                    # Pure calling range only (no 3bet)
                    defend_freq = call_freq

                if defend_freq > 0.0:
                    calling_range[hand_cls] = defend_freq
        except Exception as e:
            print(f"Error reading defend range from {fpath}: {e}")

    # Fallback default range if empty
    if not calling_range:
        for cls in pe.CLASS_TO_COMBOS.keys():
            if cls in ("AA", "KK", "QQ", "JJ", "TT", "99", "88", "77", "66", "55", "44", "33", "22"):
                calling_range[cls] = 0.8
            elif "s" in cls:
                calling_range[cls] = 0.6
            else:
                calling_range[cls] = 0.3

    return calling_range


def get_preflop_threebet_range(
    hero_pos: str = "BB",
    vill_open_pos: str = "BTN",
    stack: int = 100,
    range_profile: str = "standard",
    open_size: float = 2.2,
) -> Dict[str, float]:
    """Extract threebet range weights (169 classes) for defender from preflop defend JSON."""
    fname = f"preflop_gto_defend_{hero_pos}_vsOpen_{vill_open_pos}.json"
    fpath = os.path.join(STORAGE_DIR, fname)

    if not os.path.exists(fpath):
        fpath = os.path.join(STORAGE_DIR, f"preflop_gto_defend_{hero_pos}.json")

    threebet_range: Dict[str, float] = {}

    if os.path.exists(fpath):
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                data = json.load(f)
            strat = data.get("hero_strategy", {})
            for hand_cls, freqs in strat.items():
                threebet_freq = float(freqs.get("threebet", 0.0))
                if threebet_freq > 0.0:
                    threebet_range[hand_cls] = threebet_freq
        except Exception as e:
            print(f"Error reading threebet range from {fpath}: {e}")

    if not threebet_range:
        for cls in pe.CLASS_TO_COMBOS.keys():
            if "s" in cls:
                threebet_range[cls] = 0.3
            else:
                threebet_range[cls] = 0.15

    return threebet_range


def get_preflop_open_range(
    opener_pos: str = "BTN",
    stack: int = 100,
    range_profile: str = "standard",
    open_size: float = 2.2,
    defender_pos: str = "BB",
) -> Dict[str, float]:
    """Return opener open-range weights (169 classes).

    IMPORTANT (Mode 1 RFI contract): the RFI JSONs store the opener's response AFTER
    facing a villain 3BET (external contract: fold/call/threebet where threebet is
    the opener FOURBET). They do NOT provide per-hand opener open frequencies.

    For the hand-continuous flow we therefore use a position-based heuristic prior
    (scaled by range_profile) as the opener open-range.
    """

    open_range: Dict[str, float] = {}

    if opener_pos == "BTN":
        # BTN opens ~48%
        for cls in pe.CLASS_TO_COMBOS.keys():
            if cls in ("AA", "KK", "QQ", "JJ", "TT", "99", "88", "77", "66", "55", "44", "33", "22"):
                open_range[cls] = 1.0
            elif "s" in cls:
                open_range[cls] = 0.85
            elif any(cls.startswith(r) for r in ("A", "K", "Q", "J", "T")):
                open_range[cls] = 0.65
            else:
                open_range[cls] = 0.20
    elif opener_pos == "CO":
        # CO opens ~27%
        for cls in pe.CLASS_TO_COMBOS.keys():
            if cls in ("AA", "KK", "QQ", "JJ", "TT", "99", "88", "77", "66", "55"):
                open_range[cls] = 1.0
            elif "s" in cls and any(cls.startswith(r) for r in ("A", "K", "Q", "J")):
                open_range[cls] = 0.8
            elif cls in ("AKo", "AQo", "AJo", "ATo", "KQo", "KJo", "QJo"):
                open_range[cls] = 0.9
            else:
                open_range[cls] = 0.1
    else:  # UTG opens ~15%
        for cls in pe.CLASS_TO_COMBOS.keys():
            if cls in ("AA", "KK", "QQ", "JJ", "TT", "99", "88", "77"):
                open_range[cls] = 1.0
            elif cls in ("AKs", "AQs", "AJs", "ATs", "KQs", "KJs", "QJs", "JTs"):
                open_range[cls] = 1.0
            elif cls in ("AKo", "AQo", "AJo"):
                open_range[cls] = 1.0
            else:
                open_range[cls] = 0.0

    # Scale by range_profile (tight/loose) using the shared helper.
    try:
        from mode1_solver_common import apply_range_profile

        open_range = apply_range_profile(open_range, range_profile)
    except Exception:
        # If scaling helper is missing for any reason, keep baseline weights.
        pass

    return open_range


def get_preflop_threebet_calling_range(
    opener_pos: str = "BTN",
    defender_pos: str = "BB",
    stack: int = 100,
    range_profile: str = "standard",
    open_size: float = 2.2,
) -> Dict[str, float]:
    """Return conditional opener CALL range vs a hero 3BET.

    Mode 1 RFI contract details (critical): the RFI JSONs provide the opener's
    response AFTER facing a villain 3BET, with external action labels:

      - fold / call / threebet
      - and the RFI 'threebet' corresponds to opener FOURBET.

    Therefore the RFI file does NOT contain a clean opener open-frequency that
    can be used as an "open weight".

    For the hand-continuous flow we approximate the *reach to flop* for the
    opener as:
      prior_open_range(opener) * response_prob(opener, CALL)

    where prior_open_range is position-based (from get_preflop_open_range) and
    response_prob comes from RFI hero_strategy[hand_cls]['call'].
    """

    # Use the (position-based) prior opener open range.
    open_range = get_preflop_open_range(
        opener_pos=opener_pos,
        stack=stack,
        range_profile=range_profile,
        open_size=open_size,
        defender_pos=defender_pos,
    )

    # Load RFI response probabilities.
    fname = f"preflop_gto_rfi_{opener_pos}_vsOpen_{defender_pos}.json"
    fpath = os.path.join(STORAGE_DIR, fname)
    if not os.path.exists(fpath):
        # Look for any RFI file for this position
        for f in os.listdir(STORAGE_DIR) if os.path.exists(STORAGE_DIR) else []:
            if f.startswith(f"preflop_gto_rfi_{opener_pos}"):
                fpath = os.path.join(STORAGE_DIR, f)
                break

    if not os.path.exists(fpath):
        return open_range

    try:
        with open(fpath, "r", encoding="utf-8") as f:
            data = json.load(f)
        strat = data.get("hero_strategy", {})

        cond_call_range: Dict[str, float] = {}
        for hand_cls, open_w in open_range.items():
            call_w = float(strat.get(hand_cls, {}).get("call", 0.0))
            w = float(open_w) * call_w
            # Keep even tiny weights (only exclude exact zeros); this avoids
            # accidental range collapse that would make opponent reach ~0.
            if w > 0.0:
                cond_call_range[hand_cls] = w

        # If everything collapses (bad/missing RFI), fall back.
        if not cond_call_range:
            return open_range

        return cond_call_range
    except Exception as e:
        print(f"Error reading conditional 3bet-call range from {fpath}: {e}")
        return open_range


def solve_postflop_spot(
    board: List[str],
    hero_pos: str = "BB",
    vill_open_pos: str = "BTN",
    effective_stack: float = 100.0,
    open_size: float = 2.2,
    rake_pct: float = 0.0,
    rake_cap: float = 0.0,
    bet_config: Optional[Dict[str, Any]] = None,
    iterations: int = 150,
    equity_iters: int = 12,
    custom_oop_range: Optional[Dict[str, float]] = None,
    custom_ip_range: Optional[Dict[str, float]] = None,
    pot_pre_override: Optional[float] = None,
    hero_cards: Optional[List[str]] = None,
    oop_invested_pre: Optional[float] = None,
    ip_invested_pre: Optional[float] = None,
    preflop_action: Optional[str] = None,
    tree_mode: str = "standard",
    turn_samples: int = 8,
    turn_card: Optional[str] = None,
    river_samples: int = 1,
    river_card: Optional[str] = None,
    response_mode: str = "full",
) -> Dict[str, Any]:
    """Executes CFR+ postflop solver for the given board and preflop positions/ranges."""

    # 1. Determine OOP and IP positions & ranges
    # Preflop: defender is OOP (BB, SB, or caller out of position), opener is IP (BTN, CO)
    is_hero_oop = hero_pos in ("BB", "SB") or (hero_pos == "CO" and vill_open_pos == "BTN")
    oop_pos = hero_pos if is_hero_oop else vill_open_pos
    ip_pos = vill_open_pos if is_hero_oop else hero_pos

    # Hero is always the preflop responder (CALL/3BET) that reaches the flop.
    # Therefore:
    #   - hero_def_range models the defender's range (hero seat at the flop)
    #   - villain_flop_range models the opener's reach to the flop
    #     (villain seat at the flop), conditional on hero action.

    defender_pos = hero_pos
    opener_pos = vill_open_pos

    # Hero defending range depends on preflop action (hand-continuous)
    preflop_action_norm = (str(preflop_action).upper().strip() if preflop_action else None)

    # If heroCards are provided and the hero's exact class is missing (weight=0)
    # from the selected range, add a tiny epsilon so the solver can compute
    # hero_action_evs instead of returning an empty map.
    hero_cls: Optional[str] = None
    if isinstance(hero_cards, list) and len(hero_cards) == 2:
        try:
            hero_cls = pe.infer_class_from_cards(hero_cards[0], hero_cards[1])
        except Exception:
            hero_cls = None
    if preflop_action_norm == 'CALL':
        hero_def_range = custom_oop_range or get_preflop_calling_range(
            hero_pos=defender_pos,
            vill_open_pos=opener_pos,
            open_size=open_size,
            include_threebet=False,
        )
    elif preflop_action_norm == '3BET':
        hero_def_range = custom_oop_range or get_preflop_threebet_range(
            hero_pos=defender_pos,
            vill_open_pos=opener_pos,
            open_size=open_size,
        )
    else:
        # Default (legacy) defending range = call + 3bet
        hero_def_range = custom_oop_range or get_preflop_calling_range(
            hero_pos=defender_pos,
            vill_open_pos=opener_pos,
            open_size=open_size,
            include_threebet=True,
        )

    # Ensure the hero's exact 169 class has non-zero mass in the *hero seat* range.
    # Otherwise, the expanded combo set may not include the hero combo and
    # hero_action_evs comes back empty.
    if hero_cls is not None:
        hero_def_range = dict(hero_def_range or {})
        w = float(hero_def_range.get(hero_cls, 0.0) or 0.0)
        if w <= 0.0:
            max_w = max((float(v) for v in hero_def_range.values()), default=1.0)
            hero_def_range[hero_cls] = max(1e-9, 1e-6 * max_w)

    # Villain/opener range entering flop (hand-continuous)
    # - If hero arrived via CALL: opener reaches flop with its legacy/open range.
    # - If hero arrived via 3BET: opener reaches flop with the *conditional* 3bet-call
    #   subset (i.e., opener hands that call the 3bet).
    if preflop_action_norm == '3BET':
        villain_flop_range = custom_ip_range or get_preflop_threebet_calling_range(
            opener_pos=opener_pos,
            defender_pos=defender_pos,
            stack=int(effective_stack),
            range_profile='standard',
            open_size=open_size,
        )
    else:
        villain_flop_range = custom_ip_range or get_preflop_open_range(
            opener_pos=opener_pos,
            defender_pos=defender_pos,
            open_size=open_size,
        )

    # Prune class-weight ranges to keep the subgame size small enough
    # for the equity-matrix + CFR(+)+bookkeeping. This prevents OS kills
    # during end-to-end validation while preserving the hero exact class.
    prune_min_w = 1e-6
    # Runtime tuning: for very small CFR budgets (used by HTTP smoke/contract
    # tests) the equity-matrix + CFR bookkeeping dominates. Prune more
    # aggressively so the endpoint stays latency-bounded, while still
    # preserving the hero exact class via must_include=hero_cls.
    if iterations <= 10:
        # For tiny HTTP budgets, aggressively shrink the combo universe
        # to keep the FLOP equity-matrix computation within the timeout.
        prune_keep_n = 7
        prune_mass_fraction = 0.65
    elif iterations <= 30:
        prune_keep_n = 16
        prune_mass_fraction = 0.86
    elif iterations <= 80:
        prune_keep_n = 22
        prune_mass_fraction = 0.90
    elif iterations <= 150:
        prune_keep_n = 30
        prune_mass_fraction = 0.93
    else:
        prune_keep_n = 40
        prune_mass_fraction = 0.95

    def prune_class_weights(
        class_weights: Optional[Dict[str, float]],
        *,
        keep_top_n: int,
        min_weight: float,
        must_include: Optional[str] = None,
    ) -> Dict[str, float]:
        if not class_weights:
            return {}

        cleaned: Dict[str, float] = {
            k: float(v) for k, v in class_weights.items() if v is not None and float(v) > 0.0
        }
        if not cleaned:
            return {}

        def combo_count_for_class(cls: str) -> int:
            # Number of concrete 2-card combos per 169 bucket (varies: pairs=6, suited=4, offsuit=12).
            return len(pe.CLASS_TO_COMBOS.get(cls, []))

        # Pruning/renormalization must preserve REACH MASS at the combo level.
        # expand_range_to_combos assigns the same class-weight to each combo, so:
        #   mass(cls) = weight(cls) * num_combos(cls)
        original_mass = sum(v * combo_count_for_class(k) for k, v in cleaned.items())

        items: List[Tuple[str, float, float]] = []  # (cls, weight, mass)
        for k, v in cleaned.items():
            ccount = combo_count_for_class(k)
            mass = v * ccount

            # min_weight is a tiny cut-off used to drop near-zero classes.
            # After mass-aware ranking, interpret it in COMBO-MASS units for consistency.
            if mass >= min_weight or (must_include is not None and k == must_include):
                items.append((k, v, mass))

        if not items:
            # Fallback: if everything is below threshold, keep all for safety.
            items = [(k, v, v * combo_count_for_class(k)) for k, v in cleaned.items()]

        # Rank by COMBO MASS (not just class weight).
        items.sort(key=lambda t: t[2], reverse=True)

        must_entry = None
        if must_include is not None:
            for t in items:
                if t[0] == must_include:
                    must_entry = t
                    break

        # First cut: keep enough CLASSES to reach a fraction of COMBO MASS.
        # (We rescale later to preserve the original total combo mass.)
        target_mass = max(0.0, float(prune_mass_fraction) * original_mass)
        kept: List[Tuple[str, float, float]] = []
        cum_mass = 0.0

        if target_mass <= 0.0:
            kept = list(items)
        else:
            for t in items:
                kept.append(t)
                cum_mass += t[2]
                if cum_mass >= target_mass:
                    break

        # Force must-include to be present in kept (replace lowest-mass if needed).
        if must_entry is not None and not any(t[0] == must_include for t in kept):
            if kept:
                min_idx = min(range(len(kept)), key=lambda i: kept[i][2])
                kept[min_idx] = must_entry
            else:
                kept = [must_entry]

        # Second cut: cap by keep_top_n while still guaranteeing must_include.
        if keep_top_n and keep_top_n > 0 and len(kept) > keep_top_n:
            if must_entry is not None and any(t[0] == must_include for t in kept):
                kept_wo_must = [t for t in kept if t[0] != must_include]
                top_n_excl = max(0, keep_top_n - 1)
                kept = kept_wo_must[:top_n_excl] + [must_entry]
            else:
                kept.sort(key=lambda t: t[2], reverse=True)
                kept = kept[:keep_top_n]

        out: Dict[str, float] = {k: v for k, v, _mass in kept}

        # Sanity: must_include must be present in output when requested.
        if must_include is not None and must_include not in out:
            print(
                f"[prune_class_weights] WARNING: must_include={must_include!r} missing from pruned output"
            )

        kept_mass = sum(out[k] * combo_count_for_class(k) for k in out.keys())

        if kept_mass > 0.0 and original_mass > 0.0:
            scale = original_mass / kept_mass
            for k in list(out.keys()):
                out[k] = out[k] * scale

            # Sanity: after re-scale, kept_mass should match original_mass closely.
            kept_mass2 = sum(out[k] * combo_count_for_class(k) for k in out.keys())
            denom = max(1e-12, original_mass)
            if abs(kept_mass2 - original_mass) / denom > 1e-6:
                print(
                    f"[prune_class_weights] WARNING: mass mismatch after rescale: "
                    f"original={original_mass:.6g} kept={kept_mass2:.6g}"
                )

        return out

    hero_def_range = prune_class_weights(
        hero_def_range,
        keep_top_n=prune_keep_n,
        min_weight=prune_min_w,
        must_include=hero_cls,
    )
    villain_flop_range = prune_class_weights(
        villain_flop_range,
        keep_top_n=prune_keep_n,
        min_weight=prune_min_w,
        must_include=None,
    )

    # Assign ranges to solver seats (OOP/IP) based on who is OOP at the flop.
    if is_hero_oop:
        oop_range = hero_def_range
        ip_range = villain_flop_range
    else:
        oop_range = villain_flop_range
        ip_range = hero_def_range

    # 2. Starting pot calculation
    # - Default behavior approximates open/call pots from open_size
    # - For hand-continuous flows across streets, the caller may override the exact pot
    if pot_pre_override is not None:
        pot_pre = float(pot_pre_override)
    else:
        # Calculate pot based on standard preflop action
        if oop_pos == "BB":
            # BB vs BTN: SB=0.5 + BB=1.0 + BTN raise open_size
            # BB calls additional (open_size - 0.5) after posting 0.5
            # Total pot = SB + BB + BTN open + BB additional call
            # = 0.5 + 1.0 + open_size + (open_size - 0.5) = 1.5 + 2*open_size - 0.5
            pot_pre = 2 * open_size - 0.5
        else:
            # Generic case: SB + BB + 2*open_size (assume both call/raise equal)
            pot_pre = 1.5 + 2 * open_size

    # 2b. Calculate hand-continuous investments if not provided
    # For hand-continuous (preflop CALL/3BET), we need to know how much each player invested
    pot_override_warning: Optional[str] = None
    if oop_invested_pre is not None and ip_invested_pre is not None:
        # User provided both investments - validate consistency
        expected_pot = oop_invested_pre + ip_invested_pre
        if abs(pot_pre - expected_pot) > 1e-6:
            # Pot inconsistency: the solver validates pot == investments and
            # raises ValueError otherwise. Adjust pot_pre to match investments
            # for consistency, and surface the adjustment to the caller.
            pot_override_warning = (
                f"pot_pre adjusted from {pot_pre} to {expected_pot} "
                f"to match oop_invested_pre={oop_invested_pre} + "
                f"ip_invested_pre={ip_invested_pre}"
            )
            pot_pre = expected_pot
    else:
        # Auto-calculate based on standard preflop action
        if oop_pos == "BB":
            # BB vs BTN: BTN opens open_size, BB calls (open_size - 0.5)
            ip_invested_pre = float(open_size)
            oop_invested_pre = float(open_size - 0.5)  # BB already posted 0.5
        else:
            # Generic case: assume equal investments (simplified)
            oop_invested_pre = pot_pre / 2.0
            ip_invested_pre = pot_pre / 2.0

    # 3. Configure betting tree
    # "Aggressive" default: expand sizing grids to improve strategic coverage.
    # Also handle bet_config being partial (e.g. missing keys, or keys provided
    # as None / empty lists).
    bet_cfg = bet_config if isinstance(bet_config, dict) else {}

    tree_mode_norm = str(tree_mode or "standard").lower().strip()
    turn_samples_int = int(turn_samples) if turn_samples is not None else 8
    if turn_samples_int <= 0:
        turn_samples_int = 8

    aggressive_oop_lead_sizes = [0.33, 0.5, 0.75]
    aggressive_ip_cbet_sizes = [0.25, 0.33, 0.5, 0.75]
    aggressive_check_raise_sizes = [1.5, 2.0, 2.5, 3.0, 4.0]
    aggressive_ip_raise_sizes = [1.5, 2.0, 2.5, 3.0, 4.0]

    def _normalize_and_expand_list(
        v: Any,
        default: List[float],
        *,
        expand_partial: bool = True,
    ) -> List[float]:
        """Normalize a sizing grid, optionally expanding underspecified input.

        The UI keeps the historical expansion behavior. Convergence probes can
        opt out so a deliberately reduced tree remains reduced.
        """

        if v is None:
            return list(default)
        if not isinstance(v, (list, tuple)):
            return list(default)

        vals = [float(x) for x in v if x is not None]
        vals = [x for x in vals if x > 0.0]

        if not vals:
            return list(default)

        # If caller supplied fewer sizes than the aggressive baseline, consider it
        # incomplete and expand coverage.
        if expand_partial and len(vals) < len(default):
            return sorted(set(vals).union(default))

        return sorted(set(vals))

    def _norm_int(v: Any, default: int) -> int:
        if v is None:
            return default
        try:
            iv = int(v)
        except Exception:
            return default
        return iv if iv > 0 else default

    expand_partial_sizes = bool(bet_cfg.get("expand_partial_sizes", True))
    cfg = PostflopBetConfig(
        oop_lead_sizes=_normalize_and_expand_list(
            bet_cfg.get("oop_lead_sizes"),
            aggressive_oop_lead_sizes,
            expand_partial=expand_partial_sizes,
        ),
        ip_cbet_sizes=_normalize_and_expand_list(
            bet_cfg.get("ip_cbet_sizes"),
            aggressive_ip_cbet_sizes,
            expand_partial=expand_partial_sizes,
        ),
        check_raise_sizes=_normalize_and_expand_list(
            bet_cfg.get("check_raise_sizes"),
            aggressive_check_raise_sizes,
            expand_partial=expand_partial_sizes,
        ),
        ip_raise_sizes=_normalize_and_expand_list(
            bet_cfg.get("ip_raise_sizes"),
            aggressive_ip_raise_sizes,
            expand_partial=expand_partial_sizes,
        ),
        allow_donk=bool(bet_cfg.get("allow_donk", False)),
        max_raises_per_street=_norm_int(bet_cfg.get("max_raises_per_street"), 2),
    )

    # 4. Initialize and solve (with cache + adaptive batching)

    # Cache key (deterministic): include board, ranges, pot/stack, bet config, and hero cards.
    def stable_hash(obj: Any) -> str:
        payload = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    cache_key_data = {
        "board": list(board),
        "hero_pos": hero_pos,
        "vill_open_pos": vill_open_pos,
        "effective_stack": effective_stack,
        "open_size": open_size,
        "pot_pre": pot_pre,
        "rake_pct": rake_pct,
        "rake_cap": rake_cap,
        "iterations_base": iterations,
        "equity_iters": equity_iters,
        "tree_mode": tree_mode_norm,
        "turn_samples": turn_samples_int,
        "turn_card": turn_card,
        "river_samples": int(river_samples),
        "river_card": river_card,
        "bet_config": {
            "oop_lead_sizes": cfg.oop_lead_sizes,
            "ip_cbet_sizes": cfg.ip_cbet_sizes,
            "check_raise_sizes": cfg.check_raise_sizes,
            "ip_raise_sizes": cfg.ip_raise_sizes,
            "allow_donk": cfg.allow_donk,
            "max_raises_per_street": cfg.max_raises_per_street,
        },
        "oop_range": oop_range,
        "ip_range": ip_range,
        "hero_cards": hero_cards,
        "hero_is_oop": is_hero_oop,
        "oop_invested_pre": oop_invested_pre,
        "ip_invested_pre": ip_invested_pre,
        "preflop_action": preflop_action_norm,
    }

    # Defensive: ensure tree_mode/turn_samples are always present in the cache
    # key to avoid reusing incorrect subgame results across modes.
    # (If tree_mode/turn_samples are not set, defaults are already applied.)
    cache_key_data["tree_mode"] = tree_mode_norm
    cache_key_data["turn_samples"] = turn_samples_int

    cache_key = stable_hash(cache_key_data)
    cached = _POSTFLOP_CACHE.get(cache_key)

    response_mode_norm = str(response_mode or "full").lower().strip()
    if response_mode_norm in ("compact", "light", "reduced", "minimal"):
        response_mode_norm = "compact"
    else:
        response_mode_norm = "full"

    results: Optional[Dict[str, Any]] = dict(cached) if cached is not None else None

    # If we already have cached results, optionally compact the payload for HTTP
    # clients and return immediately (avoid re-solving / solver-dependent recompute).
    if results is not None:
        # Avoid mutating cached canonical result in-place.
        if response_mode_norm == "compact":
            results = dict(results)
            keep_node_keys = {
                # Required by poker_gto_mode1_app.html (EV recommendation + navigation)
                "node_id",
                "history",
                "player",
                "pot",
                "street",
                "board_key",
                "oop_invested",
                "ip_invested",
                "actions",
                "overall_frequencies",
                "hero_action_evs",
                "combo_strategies",
                "action_outcomes",
                "hero_diag",
                "strategy_classes",
            }

            nodes = results.get("nodes")
            if isinstance(nodes, dict):
                compact_nodes: Dict[str, Any] = {}
                for nid, nd in nodes.items():
                    if not isinstance(nd, dict):
                        continue
                    compact_nodes[nid] = {k: nd.get(k) for k in keep_node_keys}
                results["nodes"] = compact_nodes

            for k in (
                "regret_diagnostics",
                "oop_categories",
                "ip_categories",
                "hero_categories",
                "oop_category_distribution",
                "ip_category_distribution",
            ):
                results.pop(k, None)

        return results

    if results is None:
        solver = PostflopCFRSolver(
        board=board,
        oop_range=oop_range,
        ip_range=ip_range,
        pot_pre=pot_pre,
        effective_stack=effective_stack,
        rake_pct=rake_pct,
        rake_cap=rake_cap,
        bet_config=cfg,
        equity_iters=equity_iters,
        hero_cards=hero_cards,
        hero_is_oop=is_hero_oop,
        oop_invested_pre=oop_invested_pre,
        ip_invested_pre=ip_invested_pre,
        tree_mode=tree_mode_norm,
        turn_samples=turn_samples_int,
        turn_card=turn_card,
        river_samples=int(river_samples),
        river_card=river_card,
    )

    # Adaptive schedule: grow to a higher target when 2nd raises are enabled.
    # For compact HTTP responses we intentionally respect the caller's
    # requested iterations to keep end-to-end latency low (prevents client
    # timeouts during smoke tests).
    if response_mode_norm == "compact":
        schedule_increments: List[int] = [int(iterations)]
    else:
        aggressive_profile = bool(getattr(cfg, "max_raises_per_street", 1) >= 2)
        target_max_iters = 450 if aggressive_profile else 300

        schedule_increments = [int(iterations)]
        if iterations < 150 and target_max_iters > 150:
            schedule_increments.append(150 - int(iterations))
            schedule_increments.append(target_max_iters - 150)
        elif iterations < target_max_iters:
            schedule_increments.append(target_max_iters - int(iterations))

    # Run every scheduled batch. A hero-only snapshot can look stable while
    # villain nodes or deep second-raise subtrees still carry residual regret.
    # The requested budget is therefore the convergence budget; no early stop
    # is taken from partial strategy/EV snapshots.
    final_results: Optional[Dict[str, Any]] = None
    for inc in schedule_increments:
        if inc > 0:
            final_results = solver.solve(iterations=inc)

    results = final_results if final_results is not None else solver.solve(iterations=iterations)

    # Store in cache (LRU eviction handles capacity automatically)
    _POSTFLOP_CACHE.put(cache_key, results)

    # Detach return payload from cached canonical dict (so compact-mode edits
    # don't mutate what subsequent requests retrieve from cache).
    results = dict(results)

    # 5. Calculate range summary categories
    def summarize_categories(cat_dict: Dict[str, Dict[str, Any]], weights: Dict[Tuple[str, str], float]) -> Dict[str, float]:
        cat_counts: Dict[str, float] = {}
        tot_w = sum(weights.values())
        if tot_w <= 0:
            return {}
        for c, w in weights.items():
            key = f"{c[0]}{c[1]}"
            cat_info = cat_dict.get(key, {})
            cat_name = cat_info.get("category", "High Card")
            draw_name = cat_info.get("draw")

            cat_counts[cat_name] = cat_counts.get(cat_name, 0.0) + w
            if draw_name:
                cat_counts[draw_name] = cat_counts.get(draw_name, 0.0) + w

        return {k: round(v / tot_w, 4) for k, v in sorted(cat_counts.items(), key=lambda item: item[1], reverse=True)}

    results["oop_pos"] = oop_pos
    results["ip_pos"] = ip_pos
    results["hero_is_oop"] = is_hero_oop

    if response_mode_norm != "compact":
        results["oop_category_distribution"] = summarize_categories(
            results.get("oop_categories", {}),
            getattr(solver, "oop_combo_weights", {}),
        )
        results["ip_category_distribution"] = summarize_categories(
            results.get("ip_categories", {}),
            getattr(solver, "ip_combo_weights", {}),
        )

    # Payload compaction for HTTP clients: trim large per-node strategy/diagnostic fields.
    if response_mode_norm == "compact":
        keep_node_keys = {
            # Required by poker_gto_mode1_app.html (EV recommendation + navigation)
            "node_id",
            "history",
            "player",
            "pot",
            "street",
            "board_key",
            "oop_invested",
            "ip_invested",
            "actions",
            "overall_frequencies",
            "hero_action_evs",
            "combo_strategies",
            "action_outcomes",
            "hero_diag",
            "strategy_classes",
        }

        nodes = results.get("nodes")
        if isinstance(nodes, dict):
            compact_nodes: Dict[str, Any] = {}
            for nid, nd in nodes.items():
                if not isinstance(nd, dict):
                    continue
                compact_nodes[nid] = {k: nd.get(k) for k in keep_node_keys}
            results["nodes"] = compact_nodes

        # Drop heavy summary blobs not needed by the smoke/integration tests.
        for k in (
            "regret_diagnostics",
            "oop_categories",
            "ip_categories",
            "hero_categories",
            "oop_category_distribution",
            "ip_category_distribution",
        ):
            results.pop(k, None)

    return results
