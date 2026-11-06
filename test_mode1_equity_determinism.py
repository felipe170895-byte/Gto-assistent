"""Deterministic equity sampling test for Mode 1.

This test is intentionally strict:
- We monkeypatch `postflop_evaluator.compute_postflop_equity_matrix` to capture
  the `seed` parameter used for FLOP equity Monte-Carlo sampling.
- We call the Mode 1 postflop endpoint twice with identical payload except
  for `iterations`.

Expected behavior (from the implementation plan):
- Server derives a payload-based `equity_seed` that is:
  - deterministic for the same payload
  - independent of `iterations`
- Therefore the captured `seed` passed into equity calculation must:
  - equal an expected hash-derived value
  - be identical across the two requests.

On current main branch state (before the Task 1 implementation), the solver
uses a fixed default seed, so this test should FAIL.

Run:
  python test_mode1_equity_determinism.py
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


def _post_json(url: str, payload: dict, *, timeout_s: float = 180.0) -> dict:
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


def _ui_pot_and_investments(preflop_action: str, open_size: float):
    preflop_action = preflop_action.upper().strip()
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
    # Must match the implementation plan in server.py.
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

    # Stable 32-bit range.
    return u32 % (2**31 - 1)


def main() -> None:
    port = _free_port()
    httpd = HTTPServer(("127.0.0.1", port), srv.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.2)

    captured: list[int] = []
    orig_fn = pe.compute_postflop_equity_matrix

    def wrapped(*args, **kwargs):
        seed = kwargs.get("seed")
        if seed is None:
            # signature: (combos_oop, combos_ip, board, iters_per_matchup, seed=...)
            if len(args) >= 5:
                seed = args[4]
            else:
                seed = 42
        captured.append(int(seed))
        return orig_fn(*args, **kwargs)

    pe.compute_postflop_equity_matrix = wrapped

    try:
        url = f"http://127.0.0.1:{port}{srv.POSTFLOP_API_PATH}"

        board = ["2c", "7d", "9s"]
        hero_cards = ["Ah", "Kh"]
        hero_pos = "BB"
        vill_open_pos = "BTN"
        preflop_action = "CALL"
        open_size = 2.2

        compute_mode = "quality"
        response_mode = "compact"
        tree_mode = "standard"

        # No explicit chance conditioning in this test.
        turn_card = None
        river_card = None

        expected_seed = _derive_expected_equity_seed(
            board=board,
            hero_cards=hero_cards,
            hero_pos=hero_pos,
            vill_open_pos=vill_open_pos,
            preflop_action=preflop_action,
            compute_mode=compute_mode,
            tree_mode=tree_mode,
            turn_card=turn_card,
            river_card=river_card,
        )

        iterations_values = [10, 60]
        seeds_seen: list[int] = []

        for iters in iterations_values:
            captured.clear()

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
                "response_mode": response_mode,
                "tree_mode": tree_mode,
                "pot_pre_override": pot_pre_override,
                "oop_invested_pre": oop_invested_pre,
                "ip_invested_pre": ip_invested_pre,
                "preflop_action": preflop_action,
                "betConfig": {
                    "expand_partial_sizes": False,
                    "check_raise_sizes": [3.0],
                    "allow_donk": False,
                    "ip_raise_sizes": [2.0],
                    "oop_lead_sizes": None,
                    "ip_cbet_sizes": [0.5],
                    "max_raises_per_street": 1,
                },
            }

            _post_json(url, payload, timeout_s=180.0)

            assert (
                len(captured) >= 1
            ), "Expected equity computation to call compute_postflop_equity_matrix at least once"
            seeds_seen.append(captured[-1])

        # Strict assertions.
        assert seeds_seen[0] == seeds_seen[1], (
            f"Expected identical equity_seed across iterations; got {seeds_seen}"
        )

        assert seeds_seen[0] == expected_seed, (
            f"Expected equity_seed derived from payload. expected={expected_seed} got={seeds_seen[0]}"
        )

        # Sanity: the expected seed should not be the old hardcoded default.
        assert seeds_seen[0] != 42, "Seed should no longer be the fixed default 42"

        print("PASS")

    finally:
        pe.compute_postflop_equity_matrix = orig_fn
        httpd.shutdown()
        httpd.server_close()


if __name__ == "__main__":
    main()
