# poker-gto (Mode 1) — bundle único para copiar/colar

Este arquivo serve para **reduzir erros**: ao invés de criarmos várias pastas/arquivos agora (o que pode falhar no seu ambiente), eu mantenho **aqui** todo o código necessário para:

1) Pré-computar estratégia/EV pré-flop (offline) em Python
2) Servir isso via endpoint PHP
3) Fazer o `poker_app.html` chamar o endpoint com `fetch()` e usar EV/strategy reais (sem “EV fake”)

> **Como usar**
> - Copie os trechos para os arquivos indicados.
> - Depois rode o servidor PHP local e abra o `poker_app.html` via `http://...`.

---

## A) Pasta/arquivos esperados (crie manualmente)
Dentro de `C:\Users\USER\Documents\Nova pasta\` crie:

- `api\query_preflop_gto.php`
- `worker\preflop_gto_defend.py`
- `storage\` (vai receber os JSONs do precompute)

---

## B) Worker Python (offline precompute)
**Arquivo:** `worker\preflop_gto_defend.py`

```python
import argparse
import json
import os
import random
from typing import Dict, List, Tuple

import eval7

RANKS = "AKQJT98765432"
SUITS = "shdc"

OPEN_SIZE = 2.2
THREEBET_SIZE = 9.0
POT_PRE = 4.2  # SB(1)+BB(1)+Open(2.2)

ACTION_HERO = ["fold", "call", "threebet"]
ACTION_VILL = ["fold", "call"]


def infer_class_from_cards(c1: str, c2: str) -> str:
    # c like "As", "Kd"
    r1, s1 = c1[0], c1[1]
    r2, s2 = c2[0], c2[1]
    if r1 == r2:
        return r1 + r2

    # order high->low by RANKS index (A high)
    i1 = RANKS.index(r1)
    i2 = RANKS.index(r2)
    hi, lo = (r1, r2) if i1 < i2 else (r2, r1)
    suited = (s1 == s2)
    return hi + lo + ("s" if suited else "o")


def build_class_to_combos() -> Dict[str, List[Tuple[str, str]]]:
    class_to = {}
    ranks = list(RANKS)

    # pairs
    for r in ranks:
        cards = [f"{r}{s}" for s in SUITS]
        combos = []
        for i in range(4):
            for j in range(i + 1, 4):
                combos.append((cards[i], cards[j]))
        class_to[r + r] = combos

    # suited and offsuit (169-style buckets)
    for i in range(len(ranks)):
        for j in range(i + 1, len(ranks)):
            r1, r2 = ranks[i], ranks[j]

            # suited
            suited = []
            for s in SUITS:
                suited.append((f"{r1}{s}", f"{r2}{s}"))
            class_to[r1 + r2 + "s"] = suited

            # offsuit
            offsuit = []
            for s1 in SUITS:
                for s2 in SUITS:
                    if s1 == s2:
                        continue
                    offsuit.append((f"{r1}{s1}", f"{r2}{s2}"))
            class_to[r1 + r2 + "o"] = offsuit

    return class_to


def normalize(dist: Dict[str, float]) -> Dict[str, float]:
    s = sum(dist.values())
    if s <= 0:
        return dist
    return {k: v / s for k, v in dist.items()}


def default_open_range_169() -> Dict[str, float]:
    # Pesos simples (ajuste depois se quiser ficar “mais GTO” pro seu gosto).
    w = {}

    # pares
    for r in RANKS:
        w[r + r] = 1.0

    # Ax suited A2s+ (inclui As2s)
    for r in RANKS[1:]:
        w["A" + r + "s"] = 0.9

    # broadways suited
    for cls in ["KQs", "KJs", "KTs", "QJs", "QTs", "JTs", "T9s", "98s", "87s", "76s"]:
        w[cls] = 0.8

    # offsuit broadways (leve)
    for cls in ["KQo", "AQo", "AJo", "ATo"]:
        w[cls] = 0.6

    # suited connectors/gappers leves
    for cls in ["J9s", "T8s", "Q9s", "65s"]:
        w[cls] = 0.35

    return w


def preflop_equity_mc(
    hero_combo: Tuple[str, str],
    vill_combo: Tuple[str, str],
    iters: int,
    rng: random.Random,
) -> float:
    # equity share vs villain on random full board (5 runouts)
    dead = {hero_combo[0], hero_combo[1], vill_combo[0], vill_combo[1]}

    deck = [r + s for r in RANKS for s in SUITS if (r + s) not in dead]

    hero_cards = [eval7.Card(hero_combo[0]), eval7.Card(hero_combo[1])]
    vill_cards = [eval7.Card(vill_combo[0]), eval7.Card(vill_combo[1])]

    win = 0
    tie = 0
    for _ in range(iters):
        board = rng.sample(deck, 5)
        b = [eval7.Card(x) for x in board]
        hr = eval7.evaluate(hero_cards + b)
        vr = eval7.evaluate(vill_cards + b)
        if hr > vr:
            win += 1
        elif vr == hr == vr:
            tie += 1

    return (win + 0.5 * tie) / iters


def build_all_169_classes() -> List[str]:
    ranks = list(RANKS)
    classes = []

    # pairs
    for r in ranks:
        classes.append(r + r)

    # suited / offsuit buckets
    for i in range(len(ranks)):
        for j in range(i + 1, len(ranks)):
            r1, r2 = ranks[i], ranks[j]
            classes.append(r1 + r2 + "s")
            classes.append(r1 + r2 + "o")

    return classes


def regret_matching(regrets: Dict[str, float], actions: List[str]) -> Dict[str, float]:
    pos = [max(0.0, regrets[a]) for a in actions]
    s = sum(pos)
    if s <= 1e-12:
        p = 1.0 / len(actions)
        return {a: p for a in actions}
    return {a: max(0.0, regrets[a]) / s for a in actions}


def solve_for_hero_pos(
    hero_pos: str,
    hero_classes: List[str],
    vill_classes: List[str],
    eq: Dict[str, Dict[str, float]],
    P_hero: Dict[str, float],
    P_vill: Dict[str, float],
    iters: int,
    seed: int,
) -> Dict:
    # posted at decision
    hero_posted = 1.0 if hero_pos == "BB" else 0.0

    call_cost = OPEN_SIZE - hero_posted  # hero invests if calls
    pot_call = POT_PRE + call_cost       # pot at showdown if call

    hero_threebet_invest = THREEBET_SIZE
    hero_threebet_add = THREEBET_SIZE - hero_posted
    villain_call_add = THREEBET_SIZE - OPEN_SIZE
    pot_3bet_call = POT_PRE + hero_threebet_add + villain_call_add

    EV_THREE_FOLD_FOR_HERO = POT_PRE  # villain folds => hero wins pot_pre

    rng = random.Random(seed)

    hero_sigma = {h: {"fold": 1/3, "call": 1/3, "threebet": 1/3} for h in hero_classes}
    hero_regrets = {h: {"fold": 0.0, "call": 0.0, "threebet": 0.0} for h in hero_classes}

    vill_sigma = {v: {"fold": 0.5, "call": 0.5} for v in vill_classes}
    vill_regrets = {v: {"fold": 0.0, "call": 0.0} for v in vill_classes}

    hero_sigma_sum = {h: {"fold": 0.0, "call": 0.0, "threebet": 0.0} for h in hero_classes}
    vill_sigma_sum = {v: {"fold": 0.0, "call": 0.0} for v in vill_classes}

    for it in range(iters):
        # hero updates
        for h in hero_classes:
            u_fold = 0.0
            u_call = 0.0
            u_three = 0.0

            for v in vill_classes:
                p_v = P_vill[v]
                e = eq[h][v]

                ev_call = e * pot_call - call_cost
                u_call += p_v * ev_call

                ev_three_call = e * pot_3bet_call - hero_threebet_invest
                ev_three_fold = EV_THREE_FOLD_FOR_HERO
                u_three += p_v * (
                    vill_sigma[v]["fold"] * ev_three_fold +
                    vill_sigma[v]["call"] * ev_three_call
                )

            u_avg = (
                hero_sigma[h]["fold"] * u_fold +
                hero_sigma[h]["call"] * u_call +
                hero_sigma[h]["threebet"] * u_three
            )

            hero_regrets[h]["fold"] += (u_fold - u_avg)
            hero_regrets[h]["call"] += (u_call - u_avg)
            hero_regrets[h]["threebet"] += (u_three - u_avg)

            hero_sigma[h] = regret_matching(hero_regrets[h], ACTION_HERO)

        # villain updates at response node (hero chose threebet)
        for v in vill_classes:
            w_total = 0.0

            V_fold_total = 0.0
            V_call_total = 0.0

            for h in hero_classes:
                w_h = P_hero[h] * hero_sigma[h]["threebet"]
                if w_h <= 0:
                    continue

                w_total += w_h
                V_fold_total += w_h * (-EV_THREE_FOLD_FOR_HERO)

                ev_three_call = eq[h][v] * pot_3bet_call - hero_threebet_invest
                V_call_total += w_h * (-ev_three_call)

            if w_total <= 1e-15:
                continue

            u_avg_total = vill_sigma[v]["fold"] * V_fold_total + vill_sigma[v]["call"] * V_call_total

            vill_regrets[v]["fold"] += (V_fold_total - u_avg_total)
            vill_regrets[v]["call"] += (V_call_total - u_avg_total)

            vill_sigma[v] = regret_matching(vill_regrets[v], ACTION_VILL)

        # accumulate averages
        for h in hero_classes:
            for a in ACTION_HERO:
                hero_sigma_sum[h][a] += hero_sigma[h][a]

        for v in vill_classes:
            for a in ACTION_VILL:
                vill_sigma_sum[v][a] += vill_sigma[v][a]

        if (iters >= 20) and (it % max(1, iters // 10) == 0):
            print(f"[{hero_pos}] it={it}/{iters}")

    denom = float(iters)

    # average villain strategy
    vill_sigma_avg = {}
    for v in vill_classes:
        vill_sigma_avg[v] = {a: vill_sigma_sum[v][a] / denom for a in ACTION_VILL}

    hero_strategy_out = {}
    hero_evs_out = {}

    for h in hero_classes:
        hero_strategy_out[h] = {a: hero_sigma_sum[h][a] / denom for a in ACTION_HERO}

        EV_fold = 0.0
        EV_call = 0.0
        EV_three = 0.0

        for v in vill_classes:
            p_v = P_vill[v]
            e = eq[h][v]

            EV_call += p_v * (e * pot_call - call_cost)

            ev_three_call = e * pot_3bet_call - hero_threebet_invest
            EV_three += p_v * (
                vill_sigma_avg[v]["fold"] * EV_THREE_FOLD_FOR_HERO +
                vill_sigma_avg[v]["call"] * ev_three_call
            )

        hero_evs_out[h] = {"fold": EV_fold, "call": EV_call, "threebet": EV_three}

    return {
        "hero_pos": hero_pos,
        "params": {
            "open_size": OPEN_SIZE,
            "threebet_size": THREEBET_SIZE,
            "pot_pre": POT_PRE,
            "call_cost": call_cost,
            "pot_call": pot_call,
            "pot_3bet_call": pot_3bet_call,
            "hero_posted": hero_posted,
            "iters": iters,
        },
        "action_labels": ACTION_HERO,
        "hero_strategy": hero_strategy_out,
        "hero_evs": hero_evs_out,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--heroPos", required=True, choices=["CO", "BTN", "BB"])
    ap.add_argument("--out", required=True, help="output json path in storage/")
    ap.add_argument("--iters", type=int, default=2500, help="CFR iterations for strategies")
    ap.add_argument("--equityIters", type=int, default=250, help="MC runouts per equity")
    ap.add_argument("--comboSamples", type=int, default=3, help="combo-pairs samples per class-pair")
    ap.add_argument("--seed", type=int, default=123)
    args = ap.parse_args()

    class_to_combos = build_class_to_combos()
    hero_classes = build_all_169_classes()

    open_range = default_open_range_169()
    vill_classes = sorted(list(open_range.keys()))

    combo_count = {cls: len(class_to_combos[cls]) for cls in hero_classes}
    P_hero = normalize({h: combo_count[h] for h in hero_classes})

    vill_weights = {v: open_range[v] * len(class_to_combos[v]) for v in vill_classes}
    P_vill = normalize(vill_weights)

    rng = random.Random(args.seed)

    # equity matrix
    eq: Dict[str, Dict[str, float]] = {h: {} for h in hero_classes}
    print("Computing equity matrix (can take time)...")

    for i, h in enumerate(hero_classes):
        if i % max(1, len(hero_classes) // 10) == 0:
            print(f"eq row {i}/{len(hero_classes)} ...")

        for v in vill_classes:
            acc = 0.0
            for _ in range(args.comboSamples):
                hc = rng.randrange(len(class_to_combos[h]))
                vc = rng.randrange(len(class_to_combos[v]))
                hero_combo = class_to_combos[h][hc]
                vill_combo = class_to_combos[v][vc]

                tries = 0
                while set(hero_combo).intersection(set(vill_combo)) and tries < 50:
                    vc = rng.randrange(len(class_to_combos[v]))
                    vill_combo = class_to_combos[v][vc]
                    tries += 1

                acc += preflop_equity_mc(hero_combo, vill_combo, args.equityIters, rng)

            eq[h][v] = acc / float(args.comboSamples)

    print("Solving CFR strategy...")
    out = solve_for_hero_pos(
        hero_pos=args.heroPos,
        hero_classes=hero_classes,
        vill_classes=vill_classes,
        eq=eq,
        P_hero=P_hero,
        P_vill=P_vill,
        iters=args.iters,
        seed=args.seed + 999,
    )

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)

    print("Wrote:", args.out)


if __name__ == "__main__":
    main()
```

### Rodar precompute (once)
No PowerShell, dentro da pasta `Nova pasta`:

```powershell
pip install eval7

cd "C:\Users\USER\Documents\Nova pasta"

python worker\preflop_gto_defend.py --heroPos CO  --out storage\preflop_gto_defend_CO.json  --iters 2500 --equityIters 250 --comboSamples 3
python worker\preflop_gto_defend.py --heroPos BTN --out storage\preflop_gto_defend_BTN.json --iters 2500 --equityIters 250 --comboSamples 3
python worker\preflop_gto_defend.py --heroPos BB  --out storage\preflop_gto_defend_BB.json  --iters 2500 --equityIters 250 --comboSamples 3
```

---

## C) Endpoint PHP (serve estratégia + EV por classe da mão)
**Arquivo:** `api\query_preflop_gto.php`

```php
<?php
header("Content-Type: application/json; charset=utf-8");

$raw = file_get_contents("php://input");
$req = json_decode($raw, true);
if (!is_array($req)) {
  http_response_code(400);
  echo json_encode(["error" => "invalid json"]);
  exit;
}

$heroPos = strtoupper(trim($req["heroPos"] ?? ""));
$heroCards = $req["heroCards"] ?? null; // ["As","Kd"]

if (!$heroPos || !is_array($heroCards) || count($heroCards) !== 2) {
  http_response_code(400);
  echo json_encode(["error" => "missing heroPos or heroCards(2)"]);
  exit;
}

if ($heroPos === "UTG") $heroPos = "CO"; // fallback

if (!in_array($heroPos, ["CO","BTN","BB"])) {
  http_response_code(400);
  echo json_encode(["error" => "heroPos must be CO/BTN/BB"]);
  exit;
}

function infer_class($c1, $c2) {
  $RANKS = "AKQJT98765432";
  $r1 = $c1[0]; $s1 = $c1[1];
  $r2 = $c2[0]; $s2 = $c2[1];

  if ($r1 === $r2) return $r1 . $r2;

  $i1 = strpos($RANKS, $r1);
  $i2 = strpos($RANKS, $r2);

  if ($i1 <= $i2) { $hi=$r1; $lo=$r2; } else { $hi=$r2; $lo=$r1; }

  $t = ($s1 === $s2) ? "s" : "o";
  return $hi . $lo . $t;
}

$hClass = infer_class($heroCards[0], $heroCards[1]);

$storageFile = __DIR__ . "/../storage/preflop_gto_defend_" . $heroPos . ".json";
if (!file_exists($storageFile)) {
  http_response_code(404);
  echo json_encode(["error" => "strategy file not found: " . basename($storageFile)]);
  exit;
}

$strat = json_decode(file_get_contents($storageFile), true);
if (!is_array($strat)) {
  http_response_code(500);
  echo json_encode(["error" => "bad strategy json"]);
  exit;
}

$evsObj = $strat["hero_evs"][$hClass] ?? null;
$sObj = $strat["hero_strategy"][$hClass] ?? null;

if (!$evsObj || !$sObj) {
  echo json_encode([
    "hero_class" => $hClass,
    "evs" => [0.0, 0.0, 0.0],
    "strategy" => [1/3, 1/3, 1/3],
    "warning" => "hand class not found in strategy"
  ]);
  exit;
}

$fold = floatval($evsObj["fold"]);
$call = floatval($evsObj["call"]);
$three = floatval($evsObj["threebet"]);

$sFold = floatval($sObj["fold"]);
$sCall = floatval($sObj["call"]);
$sThree = floatval($sObj["threebet"]);

echo json_encode([
  "hero_class" => $hClass,
  "evs" => [$fold, $call, $three],
  "strategy" => [$sFold, $sCall, $sThree]
], JSON_UNESCAPED_UNICODE);
```

---

## D) Alteração no `poker_app.html` (substituir EV fake por fetch)
No `poker_app.html`, procure e substitua **a função inteira** `async function gerarProximoSpot()` (desde a linha `async function gerarProximoSpot()` até o fechamento da função) pela versão abaixo.

> Se você preferir, você pode colar essa versão aqui e eu ajusto exatamente no seu arquivo com o próximo passo (mas pra isso eu preciso que você me deixe eu editar o arquivo do seu HTML depois que você colar/alinhar o código).

```js
async function gerarProximoSpot() {
    isLocked = true;

    document.getElementById('arena-surface').classList.remove('success-flash');
    document.getElementById('btn-continue').classList.add('hidden');
    document.getElementById('solver-bars-box').classList.add('hidden');

    document.getElementById('solver-feedback').className =
      "bg-slate-950 border border-slate-900 p-3 text-center text-xs text-slate-500 font-poker";
    document.getElementById('solver-feedback').innerHTML =
      "Rodando GTO pré-flop offline (8-max)...";

    ['btn-fold', 'btn-call', 'btn-raise'].forEach(id => {
        let btn = document.getElementById(id);
        btn.disabled = true;
        btn.style.opacity = "0.2";
        btn.style.display = "flex";
    });

    let filterPos = document.getElementById('filter-hero-pos').value;
    let heroPosDisplay = filterPos === 'ANY'
        ? positionsList[Math.floor(Math.random() * positionsList.length)]
        : filterPos;

    let heroPosSolver = heroPosDisplay === "UTG" ? "CO" : heroPosDisplay;

    let oppPos = positionsList.filter(p => p !== heroPosDisplay)[0];
    let street = "PREFLOP";

    // Sorteia uma mão (duas cartas)
    let r1 = RANKS[Math.floor(Math.random()*13)], r2 = RANKS[Math.floor(Math.random()*13)];
    let s1 = SUITS[Math.floor(Math.random()*4)], s2 = SUITS[Math.floor(Math.random()*4)];
    if(r1 === r2 && s1 === s2) s2 = SUITS.filter(s => s !== s1)[0];

    let hand = [{r:r1, s:s1}, {r:r2, s:s2}];

    // Classes 169 não precisa ser calculada aqui (o PHP resolve).
    // Só mantemos comboStr para a UI.
    let rIdx1 = RANKS.indexOf(r1), rIdx2 = RANKS.indexOf(r2);
    let high = Math.min(rIdx1, rIdx2), low = Math.max(rIdx1, rIdx2);
    let comboStr = high === low
        ? RANKS[high]+RANKS[low]
        : (s1 === s2 ? RANKS[high]+RANKS[low]+"s" : RANKS[high]+RANKS[low]+"o");

    // Pré-flop modelo (defensor vs open 2.2)
    const openSize = 2.2;
    const threeBetSize = 9.0;
    const potPre = 4.2;

    let heroPosted = (heroPosSolver === "BB") ? 1.0 : 0.0;
    let facingBet = openSize - heroPosted; // to_call (BB=1.2, CO/BTN=2.2)
    let raise_to = threeBetSize;

    const API_URL = "api/query_preflop_gto.php";

    const heroCardsStr = [
        `${hand[0].r}${hand[0].s}`,
        `${hand[1].r}${hand[1].s}`
    ];

    // UI textos
    document.getElementById('board-street').textContent = street;
    document.getElementById('pot-display').textContent = `POT: ${potPre.toFixed(1)} BB`;
    document.getElementById('matrix-pos-badge').textContent = `HERO: ${heroPosDisplay}`;
    document.getElementById('board-desc').textContent =
        `PREFLOP • ${heroPosDisplay} defend vs open 2.2 • Mão: ${comboStr}.`;

    // estado antes do fetch
    activeSpot = {
        heroPos: heroPosDisplay,
        heroPosSolver,
        oppPos,
        street,
        hand,
        board: [],
        comboStr,
        pot: potPre,
        facingBet,
        raise_to,
        evs: [0,0,0]
    };

    atualizarLayoutMesa();

    // fetch do solver
    const resp = await fetch(API_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json; charset=utf-8" },
        body: JSON.stringify({
            heroPos: heroPosSolver,
            heroCards: heroCardsStr
        })
    });

    const data = await resp.json();
    if (data.error) {
        throw new Error(data.error);
    }

    activeSpot.evs = data.evs; // [fold, call, threebet]

    // Atualiza labels
    document.getElementById('lbl-call').textContent = `CALL ${facingBet.toFixed(1)} BB`;
    document.getElementById('lbl-raise').textContent = `3BET ${raise_to.toFixed(1)} BB`;

    // Barras/feedback
    document.getElementById('solver-bars-box').classList.remove('hidden');
    document.getElementById('solver-feedback').innerHTML =
      "GTO pronto. Escolha a melhor ação!";
    document.getElementById('solver-feedback').className =
      "bg-emerald-950/40 border border-emerald-500/40 p-3 text-center text-xs text-slate-500 italic font-poker";

    ['btn-fold', 'btn-call', 'btn-raise'].forEach(id => {
        let btn = document.getElementById(id);
        btn.disabled = false;
        btn.style.opacity = "1";
    });

    isLocked = false;
}
```

---

## E) Rodar localmente (servidor PHP)
Na pasta `Nova pasta`:

```powershell
cd "C:\Users\USER\Documents\Nova pasta"
php -S localhost:8000
```

Abra no navegador:

- `http://localhost:8000/poker_app.html`

---

## F) Próximo ajuste (quando você rodar)
Quando você testar e der erro (ou sucesso), me mande:
- o erro exato do Console (F12) e/ou
- o conteúdo de `data.error` se aparecer

Aí eu ajusto esse bundle e (se você quiser) também te passo a substituição exata no seu `poker_app.html`.
