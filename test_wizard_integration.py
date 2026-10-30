"""Integration smoke test for the HTTP wizard endpoint.

This test is self-contained:
- Starts server.py on a random free localhost port.
- Calls /api/query_postflop_gto.php with tree_mode=flop_to_turn and forced
  turn_card + river_card.
- Validates that:
  - CHANCE nodes exist on TURN
  - TURN decision/terminal nodes carry board_key len=4 and board_key[3]==turn_card
  - RIVER decision/terminal nodes carry board_key len=5 and board_key[3]==turn_card,
    board_key[4]==river_card
  - hero_action_evs exists on at least one RIVER node

Run:
  python test_wizard_integration.py
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

import postflop_evaluator as pe


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
            "hero_cards": ["Ah", "Ad"],
            "effective_stack": 100.0,
            "open_size": 2.2,
            "rake_pct": 0.0,
            "rake_cap": 0.0,
            # Keep solve small for a smoke test.
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

        nodes = result.get("nodes") or {}

        turn_nodes = [n for n in nodes.values() if n.get("street") == "TURN"]
        chance_nodes = [n for n in nodes.values() if n.get("player") == "CHANCE"]

        print(f"total nodes: {len(nodes)}")
        print(f"CHANCE nodes: {len(chance_nodes)}")
        print(f"TURN decision nodes: {len(turn_nodes)}")

        failures: list[str] = []

        if len(chance_nodes) <= 0:
            failures.append("expected at least one CHANCE node")

        # CHANCE nodes can keep board_key=None. Validate only decision/terminal nodes.
        bad_turn_keys = [
            n
            for n in turn_nodes
            if n.get("player") in ("OOP", "IP", "TERMINAL")
            and not (isinstance(n.get("board_key"), list) and len(n.get("board_key")) == 4)
        ]
        if bad_turn_keys:
            failures.append(f"{len(bad_turn_keys)} TURN nodes missing a 4-card board_key")

        wrong_turn = []
        for n in turn_nodes:
            if n.get("player") not in ("OOP", "IP", "TERMINAL"):
                continue
            bk = n.get("board_key")
            if isinstance(bk, list) and len(bk) == 4 and bk[3] != turn_card:
                wrong_turn.append((n.get("node_id"), bk))
        if wrong_turn:
            failures.append(f"TURN board_key[3] mismatch (sample): {wrong_turn[:3]}")

        hero_evs_turn_nodes = [
            n for n in turn_nodes if n.get("player") in ("OOP", "IP", "TERMINAL") and n.get("hero_action_evs")
        ]
        if len(hero_evs_turn_nodes) <= 0:
            failures.append("expected at least one TURN node with hero_action_evs")

        # --- RIVER validations ---
        river_nodes = [n for n in nodes.values() if n.get("street") == "RIVER"]
        if len(river_nodes) <= 0:
            failures.append("expected at least one RIVER node")

        decision_river_nodes = [
            n for n in river_nodes if n.get("player") in ("OOP", "IP", "TERMINAL")
        ]

        bad_river_keys = [
            n
            for n in decision_river_nodes
            if not (isinstance(n.get("board_key"), list) and len(n.get("board_key")) == 5)
        ]
        if bad_river_keys:
            failures.append(f"{len(bad_river_keys)} RIVER nodes missing a 5-card board_key")

        wrong_river = []
        for n in decision_river_nodes:
            bk = n.get("board_key")
            if isinstance(bk, list) and len(bk) == 5:
                if bk[3] != turn_card or bk[4] != river_card:
                    wrong_river.append((n.get("node_id"), bk))
        if wrong_river:
            failures.append(f"RIVER board_key conditioning mismatch (sample): {wrong_river[:3]}")

        hero_evs_river_nodes = [
            n for n in decision_river_nodes if n.get("hero_action_evs")
        ]
        if len(hero_evs_river_nodes) <= 0:
            failures.append("expected at least one RIVER node with hero_action_evs")
        else:
            # --- GTO Wizard-level fields for the UI ---
            # poker_gto_mode1_app.html expects these fields to render hero EV +
            # hero frequency bars + diagnostics + the action-navigation graph.
            # (This is intentionally validated only on at least one node.)
            sample_river = hero_evs_river_nodes[0]
            for field in ("combo_strategies", "action_outcomes", "hero_diag", "strategy_classes"):
                if field not in sample_river:
                    failures.append(f"missing {field} on a RIVER hero node")
            if "action_outcomes" in sample_river and "actions" in sample_river:
                ao = sample_river.get("action_outcomes") or {}
                acts = sample_river.get("actions") or []
                missing_outcomes = [a for a in acts if a not in ao]
                if missing_outcomes:
                    failures.append(
                        "action_outcomes missing for some node actions (sample): "
                        + str(missing_outcomes[:5])
                    )

        hero_evs_turn_nodes = [
            n
            for n in turn_nodes
            if n.get("player") in ("OOP", "IP") and n.get("hero_action_evs")
        ]
        if len(hero_evs_turn_nodes) <= 0:
            failures.append(
                "expected at least one TURN decision node (OOP/IP) with hero_action_evs"
            )
        else:
            sample_turn = hero_evs_turn_nodes[0]
            for field in ("combo_strategies", "action_outcomes", "hero_diag", "strategy_classes"):
                if field not in sample_turn:
                    failures.append(f"missing {field} on a TURN hero node")

        if failures:
            print("FAIL:")
            for f in failures:
                print(f"  - {f}")
            raise SystemExit(1)

        print("PASS")

    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
        raise SystemExit(f"FAIL: HTTP {e.code}: {body}")
    finally:
        httpd.shutdown()
        httpd.server_close()


if __name__ == "__main__":
    main()
