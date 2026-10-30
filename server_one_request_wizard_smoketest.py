"""Smoke test: validate wizard flop->turn wiring via the HTTP endpoint.

This test is intentionally lightweight:
- small iterations/equity_iters
- reduced bet sizing grids (expand_partial_sizes=False + explicit small lists)
- validates that TURN decision nodes have a 4-card board_key and that
  board_key[3] matches the forced turn_card.

Run:
  python server_one_request_wizard_smoketest.py
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request

from http.server import HTTPServer

import server as srv


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    addr, port = s.getsockname()
    s.close()
    return int(port)


def _post_json(url: str, payload: dict, *, timeout_s: int = 180) -> dict:
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


def main() -> None:
    port = _free_port()
    httpd = HTTPServer(("127.0.0.1", port), srv.Handler)

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.2)

    try:
        url = f"http://127.0.0.1:{port}{srv.POSTFLOP_API_PATH}"

        turn_cards = ["Tc", "Th"]
        river_card = "2c"
        for turn_card in turn_cards:
            payload = {
                "board": ["Ks", "Qs", "Jd"],
                "heroPos": "BB",
                "villOpenPos": "BTN",
                "preflop_action": "CALL",
                "tree_mode": "flop_to_turn",
                "turn_card": turn_card,
                "river_card": river_card,
                "river_samples": 1,
                "heroCards": ["Ah", "Ad"],
                "effective_stack": 100.0,
                "open_size": 2.2,
                "rake_pct": 0.0,
                "rake_cap": 0.0,
                # Keep solve small for this smoke test.
                "iterations": 3,
                "equity_iters": 1,
                "betConfig": {
                    "expand_partial_sizes": False,
                    "check_raise_sizes": [3.0],
                    "allow_donk": False,
                    "ip_raise_sizes": [2.0],
                    "oop_lead_sizes": [0.5],
                    "ip_cbet_sizes": [0.5],
                    "max_raises_per_street": 1,
                },
            }

            print(f"Sending request (turn_card={turn_card})...")
            result = _post_json(url, payload, timeout_s=180)

            if result.get("status") != "ok":
                raise SystemExit(
                    f"FAIL: status={result.get('status')}, error={result.get('error')}"
                )

            nodes = result.get("nodes") or {}
            turn_nodes = [n for n in nodes.values() if n.get("street") == "TURN"]
            chance_nodes = [n for n in turn_nodes if n.get("player") == "CHANCE"]
            decision_turn_nodes = [
                n
                for n in turn_nodes
                if n.get("player") in ("OOP", "IP", "TERMINAL")
            ]

            # RIVER validation (tree extension).
            river_nodes = [n for n in nodes.values() if n.get("street") == "RIVER"]
            decision_river_nodes = [
                n for n in river_nodes if n.get("player") in ("OOP", "IP", "TERMINAL")
            ]

            print(
                f"Nodes total: {len(nodes)} | TURN total: {len(turn_nodes)} (CHANCE on TURN: {len(chance_nodes)}) | RIVER total: {len(river_nodes)}"
            )

            if len(chance_nodes) <= 0:
                raise SystemExit(
                    "FAIL: expected CHANCE nodes on TURN street"
                )
            if len(decision_turn_nodes) <= 0:
                raise SystemExit(
                    "FAIL: expected at least some TURN decision nodes"
                )

            bad_keys = [
                n
                for n in decision_turn_nodes
                if not (isinstance(n.get("board_key"), list) and len(n.get("board_key")) == 4)
            ]
            if bad_keys:
                samples = [n.get("board_key") for n in bad_keys[:5]]
                raise SystemExit(
                    "FAIL: some TURN nodes have board_key != 4 cards "
                    f"(sample): {samples}"
                )

            wrong_turn = []
            for n in decision_turn_nodes:
                bk = n.get("board_key")
                if isinstance(bk, list) and len(bk) == 4:
                    if bk[3] != turn_card:
                        wrong_turn.append((n.get("node_id"), bk))

            if wrong_turn:
                raise SystemExit(
                    f"FAIL: TURN board_key[3] mismatch (sample): {wrong_turn[:3]}"
                )

            hero_evs_nodes = [n for n in decision_turn_nodes if n.get("hero_action_evs")]
            if len(hero_evs_nodes) <= 0:
                raise SystemExit(
                    "FAIL: expected at least one TURN node with hero_action_evs"
                )

            # Validate river board_key conditioning.
            wrong_river = []
            for n in decision_river_nodes:
                bk = n.get("board_key")
                if isinstance(bk, list) and len(bk) == 5:
                    if bk[3] != turn_card or bk[4] != river_card:
                        wrong_river.append((n.get("node_id"), bk))

            if wrong_river:
                raise SystemExit(
                    f"FAIL: RIVER board_key conditioning mismatch (sample): {wrong_river[:3]}"
                )

            hero_evs_river_nodes = [
                n for n in decision_river_nodes if n.get("hero_action_evs")
            ]
            if len(hero_evs_river_nodes) <= 0:
                raise SystemExit(
                    "FAIL: expected at least one RIVER node with hero_action_evs"
                )

        print("PASS")

    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
        raise SystemExit(f"FAIL: HTTP {e.code}: {body}")
    finally:
        httpd.shutdown()
        httpd.server_close()


if __name__ == "__main__":
    main()
