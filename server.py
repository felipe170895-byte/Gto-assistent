import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import unquote

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
STORAGE_DIR = os.path.join(ROOT_DIR, "storage")

API_PATH = "/api/query_preflop_gto.php"
POSTFLOP_API_PATH = "/api/query_postflop_gto.php"
STATUS_PATH = "/api/status_mode1_v2.php"

import postflop_evaluator as pe
import postflop_service as ps

RANKS = "AKQJT98765432"
ALLOWED_STACKS = (20, 40, 100, 200)
ALLOWED_RANGE_PROFILES = ("standard", "tight", "loose")

DEFAULT_OPEN_SIZE = 2.2

# Cache simples para evitar reler arquivos JSON em cada request
# chave: caminho absoluto do arquivo
# valor: {mtime: float, strat: dict}
STRAT_CACHE = {}


def parse_effective_stack(req: dict) -> int:
    raw = req.get("effectiveStack", 100)
    try:
        stack = int(round(float(raw)))
    except Exception:
        stack = 100
    if stack not in ALLOWED_STACKS:
        return 100
    return stack


def normalize_range_profile(x) -> str:
    p = str(x or "standard").lower().strip()
    if p not in ALLOWED_RANGE_PROFILES:
        return "standard"
    return p


def normalize_open_size(x) -> float:
    try:
        v = float(x)
    except Exception:
        v = DEFAULT_OPEN_SIZE

    # Keep a stable naming grid: 1 decimal.
    v = round(v, 1)

    # Basic sanity.
    if v <= 0:
        return DEFAULT_OPEN_SIZE
    return v


def open_suffix(open_size: float) -> tuple[str, float]:
    x = normalize_open_size(open_size)
    if abs(x - DEFAULT_OPEN_SIZE) < 1e-9:
        return "", x
    return f"_open{x:.1f}bb", x


def strategy_filename(
    kind: str,
    hero_pos: str,
    vill_open_pos: str,
    stack: int,
    range_profile: str = "standard",
    open_size: float = DEFAULT_OPEN_SIZE,
) -> str:
    prefix = "preflop_gto_rfi" if kind == "rfi" else "preflop_gto_defend"
    base = f"{prefix}_{hero_pos}_vsOpen_{vill_open_pos}"

    open_suf, open_norm = open_suffix(open_size)
    stack_int = int(stack)

    # stack == 100 keeps the legacy fallback naming for open=2.2.
    if stack_int == 100:
        if range_profile == "standard":
            return f"{base}{open_suf}.json"
        return f"{base}{open_suf}_{range_profile}.json"

    # stack != 100
    if range_profile == "standard":
        return f"{base}{open_suf}_stack{stack_int}bb.json"
    return f"{base}{open_suf}_stack{stack_int}bb_{range_profile}.json"


def resolve_strategy_path(
    kind: str,
    hero_pos: str,
    vill_open_pos: str,
    stack: int,
    range_profile: str,
    open_size: float,
):
    """Resolve JSON path using openSize + fallback chain.

    Priority:
      1) requested openSize + requested stack + requested rangeProfile
      2) fallback openSize=2.2 (keep stack and rangeProfile)
      3) fallback rangeProfile=standard (keep openSize=2.2, keep stack)
      4) if stack != 100: same cascade at 100bb (legacy stack fallback)

    Returns:
      (path_or_none, stack_used, openSize_used, rangeProfile_used, fallback_reason)
    """

    stack_int = int(stack)
    open_req = normalize_open_size(open_size)
    open_def = DEFAULT_OPEN_SIZE
    rp = normalize_range_profile(range_profile)

    candidates = [
        (open_req, stack_int, rp, ""),
        (open_def, stack_int, rp, f"fallback openSize to {open_def:.1f}bb"),
        (open_def, stack_int, "standard", f"fallback rangeProfile to standard and openSize to {open_def:.1f}bb"),
    ]
    if stack_int != 100:
        candidates.extend([
            (open_req, 100, rp, "fallback stack to 100bb"),
            (open_def, 100, rp, f"fallback openSize to {open_def:.1f}bb and stack to 100bb"),
            (open_def, 100, "standard", f"fallback openSize to {open_def:.1f}bb, rangeProfile to standard, stack to 100bb"),
        ])

    seen = set()
    for osz, st, rprof, reason in candidates:
        key = (osz, st, rprof)
        if key in seen:
            continue
        seen.add(key)
        path = os.path.join(
            STORAGE_DIR,
            strategy_filename(
                kind,
                hero_pos,
                vill_open_pos,
                st,
                range_profile=rprof,
                open_size=osz,
            ),
        )
        if os.path.exists(path):
            return path, int(st), float(osz), rprof, reason

    return None, stack_int, open_def, "standard", (
        f"strategy JSON not found for openSize={open_req:.1f}bb and rangeProfile={rp}"
    )


def load_strategy_json(path: str):
    mtime = os.path.getmtime(path)
    cached = STRAT_CACHE.get(path)
    if cached and cached.get("mtime") == mtime:
        return cached.get("strat")

    with open(path, "r", encoding="utf-8") as f:
        strat = json.load(f)

    STRAT_CACHE[path] = {"mtime": mtime, "strat": strat}
    return strat


def send_json(handler: BaseHTTPRequestHandler, status: int, payload: dict):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    # CORS (permite fetch mesmo quando a página foi aberta via file://)
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def infer_class(card1: str, card2: str) -> str:
    # card like "As" or "Kd"
    card1 = card1.strip()
    card2 = card2.strip()
    r1, s1 = card1[0].upper(), card1[1].lower()
    r2, s2 = card2[0].upper(), card2[1].lower()

    if r1 == r2:
        return r1 + r2

    i1 = RANKS.index(r1)
    i2 = RANKS.index(r2)
    if i1 <= i2:
        hi, lo = r1, r2
    else:
        hi, lo = r2, r1

    t = "s" if s1 == s2 else "o"
    return hi + lo + t


class Handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        # CORS preflight (ex.: POST com Content-Type: application/json)
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        # (CORS não é crítico pra GET estático, mas manter comportamentos consistentes)

        path = unquote(self.path.split("?", 1)[0])
        if path.startswith("/"):
            path = path[1:]

        # Diagnóstico Mode 1 v2 (quick readiness + shape checks)
        if path == STATUS_PATH.lstrip("/"):
            hero_positions = ["CO", "BTN", "BB"]
            vill_open_positions = ["UTG", "CO", "BTN"]

            records = []
            rfi_records = []
            for hero_pos in hero_positions:
                for vill_open_pos in vill_open_positions:
                    # Status endpoint intentionally checks openSize=2.2 (legacy naming).
                    fname = f"preflop_gto_defend_{hero_pos}_vsOpen_{vill_open_pos}.json"
                    fpath = os.path.join(STORAGE_DIR, fname)
                    exists = os.path.exists(fpath)
                    rec = {
                        "hero_pos": hero_pos,
                        "vill_open_pos": vill_open_pos,
                        "file": fname,
                        "exists": exists,
                    }
                    if exists:
                        try:
                            with open(fpath, "r", encoding="utf-8") as f:
                                data = json.load(f)
                            params = data.get("params") if isinstance(data.get("params"), dict) else {}
                            rec["params_vill_open_pos"] = params.get("vill_open_pos")
                            rec["has_hero_evs"] = isinstance(data.get("hero_evs"), dict)
                            rec["has_hero_strategy"] = isinstance(data.get("hero_strategy"), dict)
                        except Exception as e:
                            rec["json_error"] = str(e)
                    records.append(rec)

                    rfi_fname = f"preflop_gto_rfi_{hero_pos}_vsOpen_{vill_open_pos}.json"
                    rfi_fpath = os.path.join(STORAGE_DIR, rfi_fname)
                    rfi_exists = os.path.exists(rfi_fpath)
                    rfi_rec = {
                        "hero_pos": hero_pos,
                        "vill_open_pos": vill_open_pos,
                        "file": rfi_fname,
                        "exists": rfi_exists,
                    }
                    if rfi_exists:
                        try:
                            with open(rfi_fpath, "r", encoding="utf-8") as f:
                                data = json.load(f)
                            params = data.get("params") if isinstance(data.get("params"), dict) else {}
                            rfi_rec["params_vill_open_pos"] = params.get("vill_open_pos")
                            rfi_rec["has_hero_evs"] = isinstance(data.get("hero_evs"), dict)
                            rfi_rec["has_hero_strategy"] = isinstance(data.get("hero_strategy"), dict)
                        except Exception as e:
                            rfi_rec["json_error"] = str(e)
                    rfi_records.append(rfi_rec)

            existing_count = sum(1 for r in records if r.get("exists"))
            rfi_existing_count = sum(1 for r in rfi_records if r.get("exists"))
            send_json(self, 200, {
                "v2_files_total": len(records),
                "v2_files_existing": existing_count,
                "rfi_files_total": len(rfi_records),
                "rfi_files_existing": rfi_existing_count,
                "files": records,
                "rfi_files": rfi_records,
            })
            return

        # Prevent directory traversal
        fs_path = os.path.abspath(os.path.join(ROOT_DIR, path))
        if not fs_path.startswith(os.path.abspath(ROOT_DIR)):
            send_json(self, 403, {"error": "forbidden"})
            return

        if os.path.isdir(fs_path):
            # basic directory handling
            index_path = os.path.join(fs_path, "index.html")
            if os.path.exists(index_path):
                fs_path = index_path
            else:
                send_json(self, 404, {"error": "not found"})
                return

        if not os.path.exists(fs_path):
            send_json(self, 404, {"error": "not found"})
            return

        # Let BaseHTTPRequestHandler guess content-type by filename extension
        try:
            with open(fs_path, "rb") as f:
                data = f.read()
            self.send_response(200)
            # Minimal content-type mapping
            ext = os.path.splitext(fs_path)[1].lower()
            ct = {
                ".html": "text/html; charset=utf-8",
                ".js": "application/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".json": "application/json; charset=utf-8",
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".svg": "image/svg+xml",
                ".woff": "font/woff",
                ".woff2": "font/woff2",
                ".ttf": "font/ttf",
            }.get(ext, "application/octet-stream")
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception:
            send_json(self, 500, {"error": "failed to serve file"})

    def do_POST(self):
        req_path = self.path.split("?", 1)[0]
        if req_path not in (API_PATH, POSTFLOP_API_PATH, "/api/solve_postflop.php"):
            send_json(self, 404, {"error": "unknown endpoint"})
            return

        raw_len = int(self.headers.get("Content-Length", "0") or "0")
        raw_body = self.rfile.read(raw_len) if raw_len > 0 else b""

        try:
            req = json.loads(raw_body.decode("utf-8")) if raw_body else None
        except Exception:
            send_json(self, 400, {"error": "invalid json"})
            return

        if not isinstance(req, dict):
            send_json(self, 400, {"error": "invalid json"})
            return

        # ----------------------------------------------------
        # POSTFLOP CFR+ SOLVER ENDPOINT
        # ----------------------------------------------------
        if req_path in (POSTFLOP_API_PATH, "/api/solve_postflop.php"):
            board = req.get("board", [])
            if not isinstance(board, list) or len(board) not in (3, 4, 5):
                send_json(self, 400, {"error": "board must be a list of 3, 4, or 5 cards (e.g. ['As', '7h', '2c'])"})
                return

            hero_pos = str(req.get("heroPos", "BB")).upper().strip()
            vill_open_pos = str(req.get("villOpenPos", "BTN")).upper().strip()
            effective_stack = float(req.get("effectiveStack", 100.0))
            open_size = float(req.get("openSize", 2.2))
            rake_pct = float(req.get("rakePct", 0.05))
            rake_cap = float(req.get("rakeCap", 3.0))
            iterations = int(req.get("iterations", 150))
            equity_iters = int(req.get("equity_iters", 12))
            bet_config = req.get("betConfig", None)
            hero_cards = req.get("heroCards", None)
            pot_pre_override = req.get("pot_pre_override", None)
            if pot_pre_override is not None:
                pot_pre_override = float(pot_pre_override)

            preflop_action = req.get("preflop_action", None)

            oop_invested_pre = req.get("oop_invested_pre", None)
            ip_invested_pre = req.get("ip_invested_pre", None)
            if oop_invested_pre is not None:
                oop_invested_pre = float(oop_invested_pre)
            if ip_invested_pre is not None:
                ip_invested_pre = float(ip_invested_pre)

            try:
                results = ps.solve_postflop_spot(
                    board=board,
                    hero_pos=hero_pos,
                    vill_open_pos=vill_open_pos,
                    effective_stack=effective_stack,
                    open_size=open_size,
                    rake_pct=rake_pct,
                    rake_cap=rake_cap,
                    bet_config=bet_config,
                    iterations=iterations,
                    equity_iters=equity_iters,
                    pot_pre_override=pot_pre_override,
                    oop_invested_pre=oop_invested_pre,
                    ip_invested_pre=ip_invested_pre,
                    preflop_action=preflop_action,
                    hero_cards=hero_cards,
                )

                if isinstance(hero_cards, list) and len(hero_cards) == 2:
                    c1, c2 = hero_cards[0], hero_cards[1]
                    hand_cls = pe.infer_class_from_cards(c1, c2)
                    combo_key = f"{c1}{c2}" if f"{c1}{c2}" in results.get("oop_categories", {}) or f"{c1}{c2}" in results.get("ip_categories", {}) else f"{c2}{c1}"

                    is_hero_oop = results.get("hero_is_oop", True)
                    cat_dict = results.get("oop_categories", {}) if is_hero_oop else results.get("ip_categories", {})
                    hero_cat = cat_dict.get(combo_key, pe.categorize_hand_on_board((c1, c2), board))

                    results["hero_hand_info"] = {
                        "combo": [c1, c2],
                        "class": hand_cls,
                        "combo_key": combo_key,
                        "category": hero_cat.get("category", "High Card"),
                        "draw": hero_cat.get("draw"),
                        "score": hero_cat.get("score"),
                    }

                send_json(self, 200, {
                    "status": "ok",
                    **results
                })
            except Exception as e:
                import traceback
                traceback.print_exc()
                send_json(self, 500, {"error": f"Postflop solving failed: {str(e)}"})
            return

        # ----------------------------------------------------
        # PREFLOP GTO ENDPOINT
        # ----------------------------------------------------

        hero_pos = str(req.get("heroPos", "")).upper().strip()
        hero_cards = req.get("heroCards", None)
        vill_open_pos = str(req.get("villOpenPos", "")).upper().strip() if req.get("villOpenPos", None) is not None else ""

        effective_stack = parse_effective_stack(req)

        if hero_pos == "UTG":
            hero_pos = "CO"  # fallback
        if hero_pos == "SB":
            hero_pos = "BB"  # fallback

        if hero_pos not in ["CO", "BTN", "BB"]:
            # Não travar a UI: caso venha valor inesperado, normalizamos.
            hero_pos = "CO"

        if not vill_open_pos:
            # fallback: se a UI não mandar, assume UTG como open
            vill_open_pos = "UTG"

        # aceitamos só UTG/CO/BTN como posições de open do modelo
        if vill_open_pos not in ["UTG", "CO", "BTN"]:
            vill_open_pos = "UTG"

        if not isinstance(hero_cards, list) or len(hero_cards) != 2:
            send_json(self, 400, {"error": "missing heroCards(2)"})
            return

        c1, c2 = hero_cards[0], hero_cards[1]
        h_class = infer_class(c1, c2)

        spot_type = str(req.get("spotType", "defend") or "defend").lower().strip()
        range_profile = normalize_range_profile(req.get("rangeProfile", "standard"))
        open_size_req = normalize_open_size(req.get("openSize", DEFAULT_OPEN_SIZE))

        if spot_type == "rfi":
            storage_file_rfi, stack_used, open_used, rp_used, fallback_reason = resolve_strategy_path(
                "rfi",
                hero_pos,
                vill_open_pos,
                effective_stack,
                range_profile,
                open_size_req,
            )

            if not storage_file_rfi:
                send_json(self, 200, {
                    "spotType": "rfi",
                    "rfi_placeholder": True,
                    "hero_class": h_class,
                    "evs": [0.0, 0.0, 0.0],
                    "evs_std": [0.0, 0.0, 0.0],
                    "strategy": [1/3, 1/3, 1/3],
                    "source_mode": "rfi_placeholder",
                    "source_file": None,
                    "vill_open_pos": vill_open_pos,
                    "effective_stack_used": stack_used,
                    "rangeProfile": rp_used,
                    "rangeProfile_used": rp_used,
                    "openSize_used": open_used,
                    "fallback_reason": fallback_reason,
                    "warning": "RFI ainda sem JSON gerado (nem no fallback). Rode python mode1_generate_rfi_v2.py.",
                })
                return

            try:
                strat = load_strategy_json(storage_file_rfi)
            except Exception:
                send_json(self, 500, {"error": "bad rfi strategy json"})
                return

            model_params = strat.get("params") if isinstance(strat.get("params"), dict) else {}
            model_vill_open_pos = model_params.get("vill_open_pos")
            if model_vill_open_pos:
                model_vill_open_pos = str(model_vill_open_pos).upper().strip()
                if model_vill_open_pos != vill_open_pos:
                    send_json(self, 500, {
                        "error": "rfi strategy vill_open_pos mismatch",
                        "requested": vill_open_pos,
                        "model": model_vill_open_pos,
                        "source_file": os.path.basename(storage_file_rfi),
                    })
                    return

            evs_obj = (strat.get("hero_evs", {}) or {}).get(h_class)
            s_obj = (strat.get("hero_strategy", {}) or {}).get(h_class)
            if not evs_obj or not s_obj:
                send_json(self, 200, {
                    "spotType": "rfi",
                    "hero_class": h_class,
                    "evs": [0.0, 0.0, 0.0],
                    "evs_std": [0.0, 0.0, 0.0],
                    "strategy": [1/3, 1/3, 1/3],
                    "source_mode": "rfi_v2",
                    "source_file": os.path.basename(storage_file_rfi),
                    "vill_open_pos": vill_open_pos,
                    "effective_stack_used": stack_used,
                    "rangeProfile": rp_used,
                    "rangeProfile_used": rp_used,
                    "openSize_used": open_used,
                    "fallback_reason": fallback_reason,
                    "warning": "hand class not found in RFI strategy",
                })
                return

            fold = float(evs_obj.get("fold", 0.0))
            call = float(evs_obj.get("call", 0.0))
            three = float(evs_obj.get("threebet", 0.0))

            evs_std_obj = (strat.get("hero_evs_std", {}) or {}).get(h_class) or {}
            fold_std = float(evs_std_obj.get("fold", 0.0))
            call_std = float(evs_std_obj.get("call", 0.0))
            three_std = float(evs_std_obj.get("threebet", 0.0))

            s_fold = float(s_obj.get("fold", 1/3))
            s_call = float(s_obj.get("call", 1/3))
            s_three = float(s_obj.get("threebet", 1/3))

            payload = {
                "spotType": "rfi",
                "rfi_placeholder": False,
                "hero_class": h_class,
                "evs": [fold, call, three],
                "evs_std": [fold_std, call_std, three_std],
                "strategy": [s_fold, s_call, s_three],
                "source_mode": "rfi_v2",
                "source_file": os.path.basename(storage_file_rfi),
                "vill_open_pos": vill_open_pos,
                "effective_stack_used": stack_used,
                "rangeProfile": rp_used,
                "rangeProfile_used": rp_used,
                "openSize_used": open_used,
                "fallback_reason": fallback_reason,
            }

            threebet_options = model_params.get("threebet_options")
            if isinstance(threebet_options, list) and threebet_options:
                payload["threebet_options"] = threebet_options

            send_json(self, 200, payload)
            return

        # defend
        storage_file_new, stack_used, open_used, rp_used, fallback_reason = resolve_strategy_path(
            "defend",
            hero_pos,
            vill_open_pos,
            effective_stack,
            range_profile,
            open_size_req,
        )

        # Legacy fallback: older DEFEND JSON without vill_open_pos/open sizing.
        storage_file_legacy = os.path.join(STORAGE_DIR, f"preflop_gto_defend_{hero_pos}.json")

        using_new = storage_file_new is not None
        using_legacy = (not using_new) and os.path.exists(storage_file_legacy)

        if not using_new and not using_legacy:
            send_json(self, 404, {
                "error": "strategy file not found",
                "file": None,
                "expected_new": strategy_filename(
                    "defend",
                    hero_pos,
                    vill_open_pos,
                    effective_stack,
                    range_profile=range_profile,
                    open_size=open_size_req,
                ),
                "expected_fallback_open": strategy_filename(
                    "defend",
                    hero_pos,
                    vill_open_pos,
                    effective_stack,
                    range_profile=range_profile,
                    open_size=DEFAULT_OPEN_SIZE,
                ),
                "expected_fallback_rp": strategy_filename(
                    "defend",
                    hero_pos,
                    vill_open_pos,
                    effective_stack,
                    range_profile="standard",
                    open_size=DEFAULT_OPEN_SIZE,
                ),
                "expected_legacy": os.path.basename(storage_file_legacy),
                "hint": "Gere os arquivos com preflop_gto_defend.py/mode1_generate_v2.py passando --openSizes.",
            })
            return

        storage_file = storage_file_new if using_new else storage_file_legacy
        if using_new:
            source_mode = "v2"
            source_file = os.path.basename(storage_file)
        else:
            source_mode = "legacy"
            source_file = os.path.basename(storage_file)
            stack_used = 100
            open_used = DEFAULT_OPEN_SIZE
            rp_used = "standard"
            fallback_reason = fallback_reason or "legacy fallback"

        try:
            strat = load_strategy_json(storage_file)
        except Exception:
            send_json(self, 500, {"error": "bad strategy json"})
            return

        # sanity-check: the v2 JSON (when present) must match vill_open_pos
        model_params = strat.get("params") if isinstance(strat.get("params"), dict) else {}
        model_vill_open_pos = model_params.get("vill_open_pos")
        if model_vill_open_pos and source_mode == "v2":
            model_vill_open_pos = str(model_vill_open_pos).upper().strip()
            if model_vill_open_pos != vill_open_pos:
                send_json(self, 500, {
                    "error": "strategy vill_open_pos mismatch",
                    "requested": vill_open_pos,
                    "model": model_vill_open_pos,
                    "source_file": source_file,
                })
                return

        evs_obj = (strat.get("hero_evs", {}) or {}).get(h_class)
        s_obj = (strat.get("hero_strategy", {}) or {}).get(h_class)

        if not evs_obj or not s_obj:
            send_json(self, 200, {
                "hero_class": h_class,
                "evs": [0.0, 0.0, 0.0],
                "strategy": [1/3, 1/3, 1/3],
                "warning": "hand class not found in strategy",
                "source_mode": source_mode,
                "source_file": source_file,
                "vill_open_pos": vill_open_pos,
                "effective_stack_used": stack_used,
                "rangeProfile": rp_used,
                "rangeProfile_used": rp_used,
                "openSize_used": open_used,
                "fallback_reason": fallback_reason,
            })
            return

        fold = float(evs_obj.get("fold", 0.0))
        call = float(evs_obj.get("call", 0.0))
        three = float(evs_obj.get("threebet", 0.0))

        evs_std_obj = (strat.get("hero_evs_std", {}) or {}).get(h_class) or {}
        fold_std = float(evs_std_obj.get("fold", 0.0))
        call_std = float(evs_std_obj.get("call", 0.0))
        three_std = float(evs_std_obj.get("threebet", 0.0))

        s_fold = float(s_obj.get("fold", 1/3))
        s_call = float(s_obj.get("call", 1/3))
        s_three = float(s_obj.get("threebet", 1/3))

        payload = {
            "hero_class": h_class,
            "evs": [fold, call, three],
            "evs_std": [fold_std, call_std, three_std],
            "strategy": [s_fold, s_call, s_three],
            "source_mode": source_mode,
            "source_file": source_file,
            "vill_open_pos": vill_open_pos,
            "effective_stack_used": stack_used,
            "rangeProfile": rp_used,
            "rangeProfile_used": rp_used,
            "openSize_used": open_used,
            "fallback_reason": fallback_reason,
        }

        threebet_options = model_params.get("threebet_options")
        if isinstance(threebet_options, list) and threebet_options:
            payload["threebet_options"] = threebet_options

        send_json(self, 200, payload)


def main():
    port = 8000
    server = HTTPServer(("localhost", port), Handler)
    print(f"Server rodando em http://localhost:{port}/")
    print(f"Endpoint: POST {API_PATH}")
    server.serve_forever()


if __name__ == "__main__":
    main()
