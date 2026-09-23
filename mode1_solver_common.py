"""Shared Mode 1 helpers: equity cache, CFR+, range profiles.

Offline-only. Does not change the external fold/call/threebet contract.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
from typing import Dict, Iterable, List, Optional, Tuple

RANKS = "AKQJT98765432"

RANGE_PROFILES = ("tight", "standard", "loose")


def normalize_range_profile(profile: Optional[str]) -> str:
    p = str(profile or "standard").strip().lower()
    if p in ("std", "default", "normal"):
        p = "standard"
    if p not in RANGE_PROFILES:
        return "standard"
    return p


def apply_range_profile(weights: Dict[str, float], profile: Optional[str]) -> Dict[str, float]:
    """Scale an open-range weight dict by a static tight/standard/loose profile.

    Standard is identity. Tight/loose only rescale offsuit and speculative
    suited connectors so existing position tuning stays the baseline.
    """
    p = normalize_range_profile(profile)
    if p == "standard":
        return dict(weights)

    out = dict(weights)
    speculative = {"J9s", "T8s", "Q9s", "65s", "98s", "87s", "76s"}
    if p == "tight":
        offsuit_mult = 0.70
        spec_mult = 0.55
        broadway_o_mult = 0.80
    else:  # loose
        offsuit_mult = 1.35
        spec_mult = 1.40
        broadway_o_mult = 1.20

    for key in list(out.keys()):
        if key.endswith("o"):
            out[key] *= offsuit_mult
            if key in ("KQo", "AQo", "AJo", "ATo"):
                out[key] *= broadway_o_mult / offsuit_mult
        if key in speculative:
            out[key] *= spec_mult
    return out


def apply_range_profile_stack(
    weights: Dict[str, float],
    profile: Optional[str],
    effective_stack: float,
    *,
    min_stack: float = 20.0,
    max_stack: float = 100.0,
) -> Dict[str, float]:
    """Stack-aware blend of tight/loose toward standard on short stacks.

    For effective_stack <= min_stack: returns standard (no tight/loose effect).
    For effective_stack >= max_stack: returns full tight/loose scaling.
    """
    p = normalize_range_profile(profile)
    if p == "standard":
        return dict(weights)

    # Blend factor: 0 on short stacks -> 1 on deep stacks.
    try:
        x = float(effective_stack)
    except Exception:
        x = max_stack
    t = (x - min_stack) / max(1e-9, (max_stack - min_stack))
    t = max(0.0, min(1.0, t))

    base = dict(weights)
    tuned = apply_range_profile(weights, p)

    out: Dict[str, float] = {}
    for k in tuned.keys():
        out[k] = base.get(k, 0.0) * (1.0 - t) + tuned[k] * t
    return out


def regret_matching_plus(regrets: Dict[str, float], actions: List[str]) -> Dict[str, float]:
    """CFR+ regret matching: floor regrets at 0, then mix on positive mass."""
    for a in actions:
        if regrets[a] < 0.0:
            regrets[a] = 0.0
    pos = [regrets[a] for a in actions]
    s = sum(pos)
    if s <= 1e-12:
        p = 1.0 / len(actions)
        return {a: p for a in actions}
    return {a: regrets[a] / s for a in actions}


def equity_cache_key(
    *,
    hero_classes: Iterable[str],
    vill_classes: Iterable[str],
    equity_iters: int,
    combo_samples: int,
    seed: int,
) -> str:
    payload = {
        "hero": list(hero_classes),
        "vill": list(vill_classes),
        "equityIters": int(equity_iters),
        "comboSamples": int(combo_samples),
        "seed": int(seed),
        "v": 1,
    }
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def load_equity_cache(cache_dir: str, key: str) -> Optional[Dict[str, Dict[str, float]]]:
    path = os.path.join(cache_dir, f"{key}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        eq = data.get("eq")
        if isinstance(eq, dict) and eq:
            return eq
    except Exception:
        return None
    return None


def save_equity_cache(cache_dir: str, key: str, eq: Dict[str, Dict[str, float]]) -> None:
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, f"{key}.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"eq": eq}, f, ensure_ascii=False)
    os.replace(tmp, path)


def compute_equity_matrix(
    *,
    hero_classes: List[str],
    vill_classes: List[str],
    class_to_combos: Dict[str, List[Tuple[str, str]]],
    equity_fn,
    equity_iters: int,
    combo_samples: int,
    rng: random.Random,
    progress_label: str = "",
) -> Dict[str, Dict[str, float]]:
    """Monte-Carlo bucket equity matrix with combo sampling."""
    eq: Dict[str, Dict[str, float]] = {h: {} for h in hero_classes}
    n_h = len(hero_classes)
    step = max(1, n_h // 10)

    for i, h in enumerate(hero_classes):
        if progress_label and (i % step == 0):
            print(f"{progress_label} eq row {i}/{n_h} ...")
        h_combos = class_to_combos[h]
        for v in vill_classes:
            v_combos = class_to_combos[v]
            acc = 0.0
            for _ in range(combo_samples):
                hero_combo = h_combos[rng.randrange(len(h_combos))]
                vill_combo = v_combos[rng.randrange(len(v_combos))]
                tries = 0
                while set(hero_combo).intersection(set(vill_combo)) and tries < 50:
                    vill_combo = v_combos[rng.randrange(len(v_combos))]
                    tries += 1
                acc += equity_fn(hero_combo, vill_combo, equity_iters, rng)
            eq[h][v] = acc / float(combo_samples)
    return eq


def get_or_compute_equity_matrix(
    *,
    hero_classes: List[str],
    vill_classes: List[str],
    class_to_combos: Dict[str, List[Tuple[str, str]]],
    equity_fn,
    equity_iters: int,
    combo_samples: int,
    rng: random.Random,
    seed: int,
    use_cache: bool,
    cache_dir: Optional[str],
    progress_label: str = "",
) -> Dict[str, Dict[str, float]]:
    if use_cache and cache_dir:
        key = equity_cache_key(
            hero_classes=hero_classes,
            vill_classes=vill_classes,
            equity_iters=equity_iters,
            combo_samples=combo_samples,
            seed=seed,
        )
        cached = load_equity_cache(cache_dir, key)
        if cached is not None:
            # Sanity: same hero/vill keys.
            if set(cached.keys()) == set(hero_classes):
                print(f"{progress_label} equity cache HIT {key[:12]}...")
                return cached
        print(f"{progress_label} equity cache MISS {key[:12]}...")
        eq = compute_equity_matrix(
            hero_classes=hero_classes,
            vill_classes=vill_classes,
            class_to_combos=class_to_combos,
            equity_fn=equity_fn,
            equity_iters=equity_iters,
            combo_samples=combo_samples,
            rng=rng,
            progress_label=progress_label,
        )
        try:
            save_equity_cache(cache_dir, key, eq)
        except Exception as e:
            print(f"{progress_label} equity cache write failed: {e}")
        return eq

    return compute_equity_matrix(
        hero_classes=hero_classes,
        vill_classes=vill_classes,
        class_to_combos=class_to_combos,
        equity_fn=equity_fn,
        equity_iters=equity_iters,
        combo_samples=combo_samples,
        rng=rng,
        progress_label=progress_label,
    )
