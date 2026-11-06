"""Convergence ladder test for Mode 1 postflop endpoint.

Goal:
- Reduce the chance that "quality" only changes due to low budgets.
- Check that as we increase iterations, the decision (bestAct) and EV
  stabilize (diminishing returns / convergence).

Additionally (Task 1 - deterministic equity sampling):
- Ensure the equity-sampling seed derived by the backend is deterministic
  for the same payload but independent of `iterations`.

Run:
  python test_mode1_ev_convergence_ladder.py
"""

from __future__ import annotations

import hashlib
import json
import socket
import threading
import time
import urllib.request

from http.server import HTTPServer

import postflop_evaluator as pe
import server as srv


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    addr, port = s.getsockname()
    s.close()
    return int(port)


def _post_json(url: str, payload: dict, *, timeout_s: float = 300.0) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw)


def _hero_is_oop(hero_pos: str, vill_open_pos: str) -> bool:
    # Mirrors poker_gto_mode1_app.html: heroIsOop = heroPos in {BB, SB} OR (CO && BTN)
    return hero_pos in ("BB", "SB") or (hero_pos == "CO" and vill_open_pos == "BTN")


def _active_node_id(hero_pos: str, vill_open_pos: str) -> str:
    return "root" if _hero_is_oop(hero_pos, vill_open_pos) else "x"


def _ui_pot_and_investments(preflop_action: str, open_size: float):
    if preflop_action == "CALL":
        pot_pre_override = 1.5 + 2.0 * open_size - 0.5
        oop_invested_pre = open_size - 0.5
        ip_invested_pre = open_size
        return float(pot_pre_override), float(oop_invested_pre), float(ip_invested_pre)

    if preflop_action == "3BET":
        threebet_size = open_size * 3.0
        pot_pre_override = 1.5 + open_size + threebet_size + (threebet_size - 0.5)
        oop_invested_pre = 0.5 + threebet_size
        ip_invested_pre = open_size + (threebet_size - 0.5)
        return float(pot_pre_override), float(oop_invested_pre), float(ip_invested_pre)

    raise ValueError(preflop_action)


def _best_action_and_ev(hero_action_evs: dict) -> tuple[str, float]:
    if not hero_action_evs:
        return "", float("-inf")
    act, ev = max(hero_action_evs.items(), key=lambda kv: kv[1])
    return str(act), float(ev)


def _derive_expected_equity_seed(
    *,
    board: list[str],
    hero_cards: list[str],
    hero_pos: str,
    vill_open_pos: str,
    preflop_action: str,
    compute_mode: str,
    tree_mode: str,
    turn_card: str | None,
    river_card: str | None,
) -> int:
    """Must match the backend derivation logic for Task 1."""

    def norm_str(x: str | None) -> str:
        return str(x or "").strip().lower()

    def norm_pos(x: str | None) -> str:
        return str(x or "").strip().upper()

    seed_obj: dict[str, object] = {
        "board": board,
        "heroCards": hero_cards,
        "heroPos": norm_pos(hero_pos),
        "villOpenPos": norm_pos(vill_open_pos),
        "preflop_action": str(preflop_action).upper().strip(),
        "compute_mode": norm_str(compute_mode),
        "tree_mode": norm_str(tree_mode),
    }

    if turn_card is not None:
        seed_obj["turn_card"] = str(turn_card).strip()
    if river_card is not None:
        seed_obj["river_card"] = str(river_card).strip()

    raw = json.dumps(seed_obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(raw.encode("utf-8")).digest()
    u32 = int.from_bytes(digest[:4], "big", signed=False)

    # Stable 32-bit-ish range.
    return u32 % (2**31 - 1)


def main() -> None:
    port = _free_port()
    httpd = HTTPServer(("127.0.0.1", port), srv.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.2)

    try:
        url = f"http://127.0.0.1:{port}{srv.POSTFLOP_API_PATH}"

        board = ["2c", "7d", "9s"]
        hero_cards = ["Ah", "Kh"]
        open_size = 2.2

        # Budget ladder.
        iterations_ladder = [10, 30, 60]
        compute_mode = "quality"

        # Practical convergence tolerance.
        ev_abs_tol_last_step = 0.22

        cases: list[tuple[str, str, str]] = [
            ("BB", "BTN", "CALL"),
            ("BTN", "CO", "CALL"),
        ]

        # --- Task 1: capture equity_seed passed into compute_postflop_equity_matrix ---
        captured_seeds_by_key: dict[tuple[int, str, str, str], int] = {}
        current_req_tag: dict[str, object] = {"key": None}

        orig_fn = pe.compute_postflop_equity_matrix

        def wrapped(*args, **kwargs):
            # signature: (combos_oop, combos_ip, board, iters_per_matchup, seed=...)
            if "seed" in kwargs:
                seed_val = kwargs["seed"]
            else:
                seed_val = args[4] if len(args) >= 5 else 42

            key = current_req_tag.get("key")
            if key is not None:
                captured_seeds_by_key[key] = int(seed_val)

            return orig_fn(*args, **kwargs)

        pe.compute_postflop_equity_matrix = wrapped

        # Store per case per iterations.
        hist: dict[tuple[str, str, str], list[tuple[int, str, float]]] = {c: [] for c in cases}

        # Also validate the expected derived seed for one representative case.
        # (If this fails, Task 1 isn't implemented.)
        representative_case = cases[0]  # (hero_pos, vill_open_pos, preflop_action)
        rep_expected_seed: int | None = None

        for iters in iterations_ladder:
            for hero_pos, vill_open_pos, preflop_action in cases:
                pot_pre_override, oop_invested_pre, ip_invested_pre = _ui_pot_and_investments(
                    preflop_action, open_size
                )

                payload = {
                    "board": board,
                    "heroCards": hero_cards,
                    "heroPos": hero_pos,
                    "villOpenPos": vill_open_pos,
                    "effectiveStack": 100.0,
                    "openSize": open_size,
                    "iterations": iters,
                    "compute_mode": compute_mode,
                    "response_mode": "compact",
                    "pot_pre_override": pot_pre_override,
                    "oop_invested_pre": oop_invested_pre,
                    "ip_invested_pre": ip_invested_pre,
                    "preflop_action": preflop_action,
                    "betConfig": {
                        "oop_lead_sizes": [0.33, 0.75],
                        "ip_cbet_sizes": [0.33, 0.75],
                        "check_raise_sizes": [3.0],
                        "allow_donk": False,
                    },
                }

                # Represent RequestTag as (iters, hero_pos, vill_open_pos, preflop_action)
                key = (iters, hero_pos, vill_open_pos, preflop_action)
                current_req_tag["key"] = key

                try:
                    resp = _post_json(url, payload, timeout_s=300.0)
                except Exception as e:
                    print(
                        f"- FAIL case={(hero_pos, vill_open_pos, preflop_action)} iters={iters}: request failed: {e}"
                    )
                    hist[(hero_pos, vill_open_pos, preflop_action)].append((iters, "", float("nan")))
                    continue

                nodes = resp.get("nodes") or {}
                node = nodes.get(_active_node_id(hero_pos, vill_open_pos)) or {}
                hero_action_evs = node.get("hero_action_evs") or {}

                if not hero_action_evs:
                    print(
                        f"- FAIL case={(hero_pos, vill_open_pos, preflop_action)} iters={iters}: missing hero_action_evs"
                    )
                    hist[(hero_pos, vill_open_pos, preflop_action)].append((iters, "", float("nan")))
                    continue

                best_act, best_ev = _best_action_and_ev(hero_action_evs)
                hist[(hero_pos, vill_open_pos, preflop_action)].append((iters, best_act, best_ev))

                # Compute expected seed for representative case only once.
                if rep_expected_seed is None and (hero_pos, vill_open_pos, preflop_action) == representative_case:
                    rep_expected_seed = _derive_expected_equity_seed(
                        board=board,
                        hero_cards=hero_cards,
                        hero_pos=hero_pos,
                        vill_open_pos=vill_open_pos,
                        preflop_action=preflop_action,
                        compute_mode=compute_mode,
                        tree_mode="standard",
                        turn_card=None,
                        river_card=None,
                    )

        # Restore function to avoid affecting any other tests.
        pe.compute_postflop_equity_matrix = orig_fn
        current_req_tag["key"] = None

        # --- Task 1 assertions ---
        if rep_expected_seed is None:
            raise SystemExit("FAIL: could not compute representative expected seed")

        rep_seeds = [
            captured_seeds_by_key[(iters, representative_case[0], representative_case[1], representative_case[2])]
            for iters in iterations_ladder
        ]

        assert rep_seeds[0] == rep_seeds[1] == rep_seeds[2], (
            f"Expected identical equity_seed across iterations; got {rep_seeds}"
        )

        assert rep_seeds[0] == rep_expected_seed, (
            f"Expected equity_seed derived from payload. expected={rep_expected_seed} got={rep_seeds[0]}"
        )

        assert rep_seeds[0] != 42, "Seed should no longer be the fixed default 42"

        # Check convergence on last step.
        all_ok = True
        for case, series in hist.items():
            it_last2, act_last2, ev_last2 = series[-2]
            it_last1, act_last1, ev_last1 = series[-1]
            d_ev = abs(ev_last1 - ev_last2)

            stable_act = act_last1 == act_last2
            if ev_last2 != ev_last2 or ev_last1 != ev_last1:
                stable_ev = False
            else:
                stable_ev = d_ev <= ev_abs_tol_last_step

            if not (stable_act and stable_ev):
                all_ok = False
                print(
                    f"- FAIL case={case}: bestAct {act_last2}->{act_last1} "
                    f"EV {ev_last2:.6f}->{ev_last1:.6f} |dEV|={d_ev:.6f} "
                    f"(iters {it_last2}->{it_last1})"
                )

            trace = " | ".join(
                f"iters={iters}:{act}/{ev:.3f}" for (iters, act, ev) in series
            )
            print(f"  trace: {trace}")

        if all_ok:
            print("PASS: convergence (last-step) criteria satisfied for all cases")
        else:
            raise SystemExit(
                "FAIL: convergence criteria not satisfied for one or more cases"
            )

    finally:
        httpd.shutdown()
        httpd.server_close()


if __name__ == "__main__":
    main()
