import json
import os
import urllib.request
import urllib.error

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
STORAGE_DIR = os.path.join(ROOT_DIR, "storage")

# Default endpoint for local Mode 1 server.
ENDPOINT = "http://localhost:8000/api/query_preflop_gto.php"

# Mode 1 mapping used by the UI.
# hero CO  => villain open UTG
# hero BTN => villain open CO
# hero BB  => villain open BTN
MAPPING = {
    "CO": "UTG",
    "BTN": "CO",
    "BB": "BTN",
}


def infer_class_from_cards_py(card1: str, card2: str) -> str:
    # Keep it aligned with preflop_gto_defend.py + server.py.
    ranks = "AKQJT98765432"
    s1 = card1[1].lower()
    s2 = card2[1].lower()
    r1 = card1[0].upper()
    r2 = card2[0].upper()

    if r1 == r2:
        return r1 + r2

    i1 = ranks.index(r1)
    i2 = ranks.index(r2)
    hi, lo = (r1, r2) if i1 < i2 else (r2, r1)

    suited = (s1 == s2)
    return hi + lo + ("s" if suited else "o")


def check_strategy_file(path: str) -> None:
    if not os.path.exists(path):
        raise FileNotFoundError(path)

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    params = data.get("params")
    if not isinstance(params, dict):
        raise ValueError(f"missing params dict in {os.path.basename(path)}")

    if "vill_open_pos" not in params:
        raise ValueError(f"missing params.vill_open_pos in {os.path.basename(path)}")

    threebet_options = params.get("threebet_options")
    if threebet_options is not None:
        # Contract: internal solver can use a larger grid, but must still be exposed as a list.
        # We accept a variable-length list (2..6) and require numeric entries.
        if not isinstance(threebet_options, list) or len(threebet_options) < 2 or len(threebet_options) > 6:
            raise ValueError(
                f"invalid params.threebet_options in {os.path.basename(path)}; got {threebet_options}"
            )
        for x in threebet_options:
            if not isinstance(x, (int, float)):
                raise ValueError(
                    f"non-numeric params.threebet_options entry in {os.path.basename(path)}; got {threebet_options}"
                )

    hero_evs = data.get("hero_evs")
    hero_strat = data.get("hero_strategy")
    if not isinstance(hero_evs, dict) or not isinstance(hero_strat, dict):
        raise ValueError(f"missing hero_evs/hero_strategy in {os.path.basename(path)}")

    # Basic shape check for any 169 bucket.
    sample_key = next(iter(hero_evs.keys()), None)
    if not sample_key:
        raise ValueError(f"hero_evs empty in {os.path.basename(path)}")

    ev = hero_evs.get(sample_key)
    st = hero_strat.get(sample_key)
    if not isinstance(ev, dict) or not isinstance(st, dict):
        raise ValueError(f"bad bucket shape in {sample_key} ({os.path.basename(path)})")

    for field in ["fold", "call", "threebet"]:
        if field not in ev:
            raise ValueError(
                f"missing EV field '{field}' in bucket {sample_key} ({os.path.basename(path)})"
            )

    for field in ["fold", "call", "threebet"]:
        if field not in st:
            raise ValueError(
                f"missing strat field '{field}' in bucket {sample_key} ({os.path.basename(path)})"
            )


def post_spot(
    server_url: str,
    hero_pos: str,
    hero_cards,
    vill_open_pos: str,
    spotType: str = "defend",
    effective_stack: int = 100,
):
    payload = {
        "heroPos": hero_pos,
        "heroCards": hero_cards,
        "villOpenPos": vill_open_pos,
        "spotType": spotType,
        "effectiveStack": effective_stack,
    }
    body = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        server_url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        out = resp.read().decode("utf-8")
    return json.loads(out)


def try_server_contract_test() -> None:
    print("[2/3] Optional server contract self-test (skips if server not running)...")

    # pick a couple deterministic spots
    spots = [
        ("CO", ["As", "Kd"], MAPPING["CO"], "defend", 20),
        ("BTN", ["7s", "7d"], MAPPING["BTN"], "defend", 100),
        ("BB", ["Ac", "Qd"], MAPPING["BB"], "defend", 200),

        ("CO", ["As", "Kd"], MAPPING["CO"], "rfi", 40),
    ]

    ok_count = 0
    for hero_pos, hero_cards, vill_open_pos, spotType, effective_stack in spots:
        h_class = infer_class_from_cards_py(hero_cards[0], hero_cards[1])
        try:
            resp = post_spot(
                ENDPOINT,
                hero_pos,
                hero_cards,
                vill_open_pos,
                spotType=spotType,
                effective_stack=effective_stack,
            )
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            print(f"[warn] Server not reachable; skipping contract self-test: {e}")
            return

        if resp.get("error"):
            raise RuntimeError(f"server error: {resp}")

        if resp.get("hero_class") != h_class:
            raise RuntimeError(
                f"hero_class mismatch. expected {h_class}, got {resp.get('hero_class')} (resp={resp})"
            )

        evs = resp.get("evs")
        strat = resp.get("strategy")
        if not isinstance(evs, list) or len(evs) != 3:
            raise RuntimeError(f"bad evs shape: {resp}")
        if not isinstance(strat, list) or len(strat) != 3:
            raise RuntimeError(f"bad strategy shape: {resp}")

        sm = resp.get("source_mode")
        if spotType == "rfi":
            if sm not in (None, "rfi_v2", "rfi_placeholder"):
                raise RuntimeError(f"unexpected source_mode for rfi: {sm} resp={resp}")
        else:
            if sm is not None and sm != "v2":
                # legacy is allowed if new v2 JSONs are missing.
                if sm != "legacy":
                    raise RuntimeError(f"expected source_mode=v2|legacy (or missing), got {sm} resp={resp}")

        if "effective_stack_used" in resp:
            esu = resp.get("effective_stack_used")
            if esu not in (20, 40, 100, 200):
                raise RuntimeError(f"bad effective_stack_used in response: {resp}")

        if "threebet_options" in resp:
            tbo = resp.get("threebet_options")
            if not isinstance(tbo, list) or len(tbo) < 2 or len(tbo) > 6:
                raise RuntimeError(f"bad threebet_options in response: {resp}")
            for x in tbo:
                if not isinstance(x, (int, float)):
                    raise RuntimeError(f"non-numeric threebet_options entry: {resp}")

        ok_count += 1

    print(f"OK: server self-test passed ({ok_count} spots).")


def main():
    hero_positions = ["CO", "BTN", "BB"]

    print("[1/3] Checking v2 strategy JSONs existence + shape...")
    missing = []
    for hero_pos in hero_positions:
        vill_open_pos = MAPPING[hero_pos]
        # Accept either the legacy standard JSON or the new rangeProfile-suffixed ones.
        # We prefer the non-suffixed file (standard=default), but allow suffixed variants to exist.
        base_legacy = os.path.join(STORAGE_DIR, f"preflop_gto_defend_{hero_pos}_vsOpen_{vill_open_pos}.json")
        base_tight = os.path.join(STORAGE_DIR, f"preflop_gto_defend_{hero_pos}_vsOpen_{vill_open_pos}_tight.json")
        base_loose = os.path.join(STORAGE_DIR, f"preflop_gto_defend_{hero_pos}_vsOpen_{vill_open_pos}_loose.json")

        candidates = [base_legacy, base_tight, base_loose]
        path = next((p for p in candidates if os.path.exists(p)), base_legacy)
        try:
            check_strategy_file(path)
        except Exception as e:
            missing.append((path, str(e)))

    if missing:
        print("Some v2 JSONs are missing or malformed:")
        for p, err in missing:
            print(f"- {os.path.basename(p)}: {err}")
        raise SystemExit(1)

    print("OK: all required v2 JSONs exist and have basic schema.")

    print("[1b/3] Checking RFI v2 strategy JSONs existence + shape...")
    rfi_missing = []
    for hero_pos in hero_positions:
        for vill_open_pos in ["UTG", "CO", "BTN"]:
            base_legacy = os.path.join(STORAGE_DIR, f"preflop_gto_rfi_{hero_pos}_vsOpen_{vill_open_pos}.json")
            base_tight = os.path.join(STORAGE_DIR, f"preflop_gto_rfi_{hero_pos}_vsOpen_{vill_open_pos}_tight.json")
            base_loose = os.path.join(STORAGE_DIR, f"preflop_gto_rfi_{hero_pos}_vsOpen_{vill_open_pos}_loose.json")

            candidates = [base_legacy, base_tight, base_loose]
            path = next((p for p in candidates if os.path.exists(p)), base_legacy)
            try:
                check_strategy_file(path)
            except Exception as e:
                rfi_missing.append((path, str(e)))

    if rfi_missing:
        print("Some RFI v2 JSONs are missing or malformed:")
        for p, err in rfi_missing:
            print(f"- {os.path.basename(p)}: {err}")
        raise SystemExit(1)

    print("OK: all required RFI v2 JSONs exist and have basic schema.")

    try_server_contract_test()

    print("[3/3] Done.")


if __name__ == "__main__":
    main()
