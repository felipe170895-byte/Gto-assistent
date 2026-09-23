import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import unquote

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
STORAGE_DIR = os.path.join(ROOT_DIR, "storage")

API_PATH = "/api/query_preflop_gto.php"
STATUS_PATH = "/api/status_mode1_v2.php"

RANKS = "AKQJT98765432"
ALLOWED_STACKS = (20, 40, 100, 200)
ALLOWED_RANGE_PROFILES = ("standard", "tight", "loose")

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


def strategy_filename(
    kind: str,
    hero_pos: str,
    vill_open_pos: str,
    stack: int,
    range_profile: str = "standard",
) -> str:
    prefix = "preflop_gto_rfi" if kind == "rfi" else "preflop_gto_defend"
    base = f"{prefix}_{hero_pos}_vsOpen_{vill_open_pos}"

    stack_int = int(stack)
    if stack_int == 100:
        if range_profile == "standard":
            return f"{base}.json"
        return f"{base}_{range_profile}.json"

    # stack != 100
    if range_profile == "standard":
        return f"{base}_stack{stack_int}bb.json"
    return f"{base}_stack{stack_int}bb_{range_profile}.json"


def resolve_strategy_path(
    kind: str,
    hero_pos: str,
    vill_open_pos: str,
    stack: int,
    range_profile: str = "standard",
):
    """Prefer requested stack + rangeProfile; fall back to unsuffixed 100bb."""

    requested = os.path.join(
        STORAGE_DIR,
        strategy_filename(kind, hero_pos, vill_open_pos, stack, range_profile=range_profile),
    )
    if os.path.exists(requested):
        return requested, int(stack)

    # fallback to 100bb (matching rangeProfile first, then standard)
    if int(stack) != 100:
        fallback_rp = os.path.join(
            STORAGE_DIR,
            strategy_filename(kind, hero_pos, vill_open_pos, 100, range_profile=range_profile),
        )
        if os.path.exists(fallback_rp):
            return fallback_rp, 100

        fallback_std = os.path.join(
            STORAGE_DIR,
            strategy_filename(kind, hero_pos, vill_open_pos, 100, range_profile="standard"),
        )
        if os.path.exists(fallback_std):
            return fallback_std, 100

    return None, 100


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
        if self.path.split("?", 1)[0] != API_PATH:
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
            vill_open_pos = 'UTG'

        # aceitamos só UTG/CO/BTN como posições de open do modelo
        if vill_open_pos not in ["UTG", "CO", "BTN"]:
            vill_open_pos = 'UTG'

        if not isinstance(hero_cards, list) or len(hero_cards) != 2:
            send_json(self, 400, {"error": "missing heroCards(2)"})
            return

        c1, c2 = hero_cards[0], hero_cards[1]
        h_class = infer_class(c1, c2)

        spot_type = str(req.get("spotType", "defend") or "defend").lower().strip()
        # rangeProfile (static OPEN range)
        range_profile = str(req.get("rangeProfile", "standard") or "standard").lower().strip()
        if range_profile not in ALLOWED_RANGE_PROFILES:
            range_profile = "standard"

        if spot_type == "rfi":
            storage_file_rfi, stack_used = resolve_strategy_path(
                "rfi", hero_pos, vill_open_pos, effective_stack, range_profile
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
                    "rangeProfile": range_profile,
                    "warning": "RFI ainda sem JSON gerado. Rode python mode1_generate_rfi_v2.py. Enquanto isso, a UI mostra só o fluxo (RAISE OPEN).",
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
                    "rangeProfile": range_profile,
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
                "rangeProfile": range_profile,
            }
            threebet_options = model_params.get("threebet_options")
            if isinstance(threebet_options, list) and threebet_options:
                payload["threebet_options"] = threebet_options
            send_json(self, 200, payload)
            return

        # Seleciona arquivo de estratégia pelo HERO_POS, VILLAIN OPEN POS e stack.
        # rangeProfile (static OPEN range)
        range_profile = str(req.get("rangeProfile", "standard") or "standard").lower().strip()
        if range_profile not in ALLOWED_RANGE_PROFILES:
            range_profile = "standard"

        # Prefer requested stack + rangeProfile; fallback para 100bb.
        storage_file_new, stack_used = resolve_strategy_path(
            "defend", hero_pos, vill_open_pos, effective_stack, range_profile
        )

        # fallback (legado): se você ainda não gerou os novos JSONs por open,
        # usamos o arquivo antigo por hero_pos (sem ajuste por vill_open_pos).
        storage_file_legacy = os.path.join(STORAGE_DIR, f"preflop_gto_defend_{hero_pos}.json")

        using_new = storage_file_new is not None
        using_legacy = os.path.exists(storage_file_legacy)

        if not using_new and not using_legacy:
            send_json(self, 404, {
                "error": "strategy file not found",
                "file": strategy_filename("defend", hero_pos, vill_open_pos, effective_stack),
                "expected_new": strategy_filename("defend", hero_pos, vill_open_pos, effective_stack),
                "expected_legacy": os.path.basename(storage_file_legacy),
                "hint": "Gere os arquivos com preflop_gto_defend.py passando --villOpenPos (modo v2).",
            })
            return

        storage_file = storage_file_new if using_new else storage_file_legacy
        source_mode = "v2" if using_new else "legacy"
        source_file = os.path.basename(storage_file)
        if not using_new:
            stack_used = 100

        try:
            strat = load_strategy_json(storage_file)
        except Exception:
            send_json(self, 500, {"error": "bad strategy json"})
            return

        # sanity-check: o arquivo carregado precisa bater com o open pos solicitado
        model_params = strat.get("params") if isinstance(strat.get("params"), dict) else {}
        # Se o JSON não tiver params (legado), não validamos.
        model_vill_open_pos = model_params.get("vill_open_pos")
        if model_vill_open_pos and source_mode == 'v2':
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
                "warning": "hand class not found in strategy"
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
            "rangeProfile": range_profile,
        }
        threebet_options = model_params.get("threebet_options")
        if isinstance(threebet_options, list) and threebet_options:
            payload["threebet_options"] = threebet_options
        send_json(self, 200, payload)


def main():
    port = 8000
    # Você pode mudar a porta aqui se precisar.
    server = HTTPServer(("localhost", port), Handler)
    print(f"Server rodando em http://localhost:{port}/")
    print(f"Endpoint: POST {API_PATH}")
    server.serve_forever()


if __name__ == "__main__":
    main()
