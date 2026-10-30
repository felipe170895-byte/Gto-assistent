"""Validate compact payload contract for Mode 1 HTTP postflop endpoint.

This test focuses on response_mode="compact":
- Required per-node keys for the UI exist on at least one hero decision node.
- Heavy/diagnostic keys are trimmed out (e.g. regret_diagnostics).
- action_outcomes includes an entry for each declared node action.

Run:
  python test_compact_payload_contract.py
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.request

from http.server import HTTPServer

import server as srv


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    addr, port = s.getsockname()
    s.close()
    return int(port)


def _post_json(url: str, payload: dict, *, timeout_s: int = 120) -> dict:
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

        turn_card = "Tc"
        river_card = "2c"

        payload = {
            "board": ["Ks", "Qs", "Jd"],
            "heroPos": "BB",
            "villOpenPos": "BTN",
            "preflop_action": "CALL",
            "tree_mode": "flop_to_turn",
            "turn_card": turn_card,
            "river_card": river_card,
            "river_samples": 1,
            "turn_samples": 1,
            "response_mode": "compact",
            "hero_cards": ["Ah", "Ad"],
            "effective_stack": 100.0,
            "open_size": 2.2,
            "rake_pct": 0.0,
            "rake_cap": 0.0,
            "iterations": 3,
            "equity_iters": 1,
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

        result = _post_json(url, payload, timeout_s=120)

        # Heavy diagnostic blobs must be trimmed.
        assert "regret_diagnostics" not in result, "regret_diagnostics must be removed in compact mode"
        assert "oop_categories" not in result, "oop_categories must be removed in compact mode"
        assert "ip_categories" not in result, "ip_categories must be removed in compact mode"

        nodes = result.get("nodes") or {}
        assert isinstance(nodes, dict) and nodes, "expected nodes dict"

        turn_nodes = [
            n
            for n in nodes.values()
            if isinstance(n, dict)
            and n.get("street") == "TURN"
            and n.get("player") in ("OOP", "IP", "TERMINAL")
        ]
        assert turn_nodes, "expected at least one TURN decision node in compact mode"

        # TURN conditioning: board_key len=4 and board_key[3]==turn_card
        wrong_turn = []
        for n in turn_nodes:
            bk = n.get("board_key")
            if not (isinstance(bk, list) and len(bk) == 4 and bk[3] == turn_card):
                wrong_turn.append((n.get("node_id"), bk))
        assert not wrong_turn, f"TURN board_key conditioning mismatch: sample={wrong_turn[:3]}"

        river_nodes = [
            n
            for n in nodes.values()
            if isinstance(n, dict)
            and n.get("street") == "RIVER"
            and n.get("player") in ("OOP", "IP", "TERMINAL")
        ]
        assert river_nodes, "expected at least one RIVER decision node in compact mode"

        # RIVER conditioning: board_key len=5 and board_key[3]==turn_card and board_key[4]==river_card
        wrong_river = []
        for n in river_nodes:
            bk = n.get("board_key")
            if not (
                isinstance(bk, list)
                and len(bk) == 5
                and bk[3] == turn_card
                and bk[4] == river_card
            ):
                wrong_river.append((n.get("node_id"), bk))
        assert not wrong_river, f"RIVER board_key conditioning mismatch: sample={wrong_river[:3]}"

        # Find at least one hero decision node with EVs (prefer RIVER if present).
        hero_nodes = [
            n
            for n in nodes.values()
            if isinstance(n, dict)
            and n.get("player") in ("OOP", "IP", "TERMINAL")
            and n.get("hero_action_evs")
        ]
        assert hero_nodes, "expected at least one node with hero_action_evs in compact mode"

        sample = next((n for n in hero_nodes if n.get("street") == "RIVER"), hero_nodes[0])

        required_fields = (
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
        )
        for f in required_fields:
            assert f in sample, f"missing field {f} on a compact hero node"

        # action_outcomes must cover each action.
        actions = sample.get("actions") or []
        ao = sample.get("action_outcomes") or {}
        missing_outcomes = [a for a in actions if a not in ao]
        assert not missing_outcomes, f"action_outcomes missing for actions: {missing_outcomes}"

        print("PASS")
    finally:
        httpd.shutdown()
        httpd.server_close()


if __name__ == "__main__":
    main()
