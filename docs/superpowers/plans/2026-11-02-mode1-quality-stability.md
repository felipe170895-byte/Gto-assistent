# Mode 1 Quality Stability + Convergence Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans.

**Goal:** Improve `compute_mode="quality"` decisions for Mode 1 by reducing EV drift/timeouts and making convergence behavior more trustworthy.

**Architecture:** We’ll (1) decouple “equity sampling quality” from “tree structure fan-out” so ladder comparisons reflect convergence rather than topology changes, (2) add deterministic equity sampling via seed derivation, and (3) implement a convergence/early-stop guard inside the CFR+ solve loop. Finally, we’ll add telemetry hooks (behind `response_mode="full"`) to identify bottlenecks.

**Tech Stack:** Python 3, in-repo HTTP server (`server.py`), CFR+ solver (`postflop_solver.py`), equity calculator (`postflop_evaluator.py`).

**Spec:** `poker_gto_mode1_project_bundle.md` (Mode 1 endpoint/UI expectations + contract stability) and existing tests in this repo (e.g. `test_mode1_ev_quality_vs_compact.py`, `test_mode1_ev_convergence_ladder.py`).

## Global Constraints
- Preserve the existing HTTP contract for `response_mode="compact"` (must not add large diagnostic blobs).
- Respect server latency bounds (existing `MAX_POST_BODY_BYTES`, timeouts observed in ladder tests).
- Maintain hero exact-class inclusion (`must_include=hero_cls`) so `hero_action_evs` is not empty.
- Keep changes incremental and TDD-focused using existing ladder/stability tests.

## Review Focus
1) **Deterministic equity sampling:** same payload + seed produces stable `hero_action_evs` across ladder rungs.
   - Tests: `test_mode1_ev_convergence_ladder.py`, `test_mode1_ev_quality_vs_compact.py`.
2) **Tree-structure stability across ladders:** `compute_mode="quality"` must not silently change betting expansion/fan-out for adjacent rungs.
   - Tests: `test_mode1_ev_convergence_ladder.py` (bestAct stability), `test_compact_payload_contract.py` (contract).
3) **Early-stop correctness:** early-stop should not violate output shape and should populate `iterations_completed`/`stop_reason`.
   - Tests: add a targeted regression test (new) for stop metadata when early-stop triggers.
4) **No payload bloat in compact:** `response_mode="compact"` must remain small.
   - Tests: `test_compact_payload_contract.py`, `test_payload_size_limit.py`.
5) **Timeout robustness:** guard/telemetry should not reintroduce client disconnect crashes.
   - Tests: `test_mode1_latency_smoke.py`, manual exercise of 413 path if present.

---

### Task 1: Deterministic equity sampling (seed derivation)

**Files:**
- Modify: `server.py`
- Modify: `postflop_service.py`
- Modify: `postflop_solver.py`
- Modify: `postflop_evaluator.py`
- Test: `test_mode1_ev_convergence_ladder.py`

**Interfaces:**
- Consumes: HTTP request fields: `board`, `heroCards`, `heroPos`, `villOpenPos`, `iterations`, `compute_mode`, `response_mode`.
- Produces: deterministic equity matrix via `seed` parameter used by `pe.compute_postflop_equity_matrix(..., seed=...)`.

- [ ] **Step 1: Add failing test**

Create `test_mode1_equity_determinism.py` that calls the Mode 1 endpoint twice with identical payload but different `iterations` and asserts equity-derived drift is reduced when seed is fixed (decision stability threshold).

- [ ] **Step 2: Run test to verify it fails**

Run: `python test_mode1_equity_determinism.py` (expected FAIL due to Monte-Carlo variance / nondeterminism).

- [ ] **Step 3: Implement seed derivation**

In `server.py`, derive `equity_seed` from request deterministically:
- Hash normalized payload fields excluding `iterations` (or include but then keep stable across ladder) such as: `board`, `heroCards`, `heroPos`, `villOpenPos`, `preflop_action`, `compute_mode`, `tree_mode`, and the forced board-card fields (`turn_card`, `river_card` when used).
- Use a stable 32-bit integer modulo range.

Propagate to:
- `postflop_service.solve_postflop_spot(..., equity_seed=equity_seed)`
- `postflop_solver.PostflopCFRSolver(..., equity_seed=equity_seed)`
- `postflop_evaluator.compute_postflop_equity_matrix(..., seed=equity_seed)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python test_mode1_equity_determinism.py`.

- [ ] **Step 5: Commit**

Commit with message: `feat: deterministic equity sampling seed`.

---

### Task 2: Decouple “equity quality” vs “tree fan-out” in compute_mode

**Files:**
- Modify: `server.py`
- Modify: `postflop_service.py`
- Modify: `postflop_solver.py` (only if needed for parameters)
- Test: `test_mode1_ev_convergence_ladder.py`

**Interfaces:**
- Consumes: `compute_mode` and `iterations`.
- Produces: `expand_partial_sizes`, `max_raises_per_street`, and pruning parameters that do not change game topology between adjacent ladder rungs.

- [ ] **Step 1: Add/adjust ladder expectation**

Update `test_mode1_ev_convergence_ladder.py` to assert that last-step bestAct stability is achieved at the final rung once deterministic equity seeding is in place (tune tolerance if needed, but keep strict on bestAct).

- [ ] **Step 2: Run test to verify it fails**

Run ladder test and record baseline drift.

- [ ] **Step 3: Implement separation**

Refactor logic so:
- `compute_mode="quality"` changes only equity-related knobs (equity_iters / seed strategy), not expansion grids for mid/upper ladder rungs.
- Tree expansion choices depend on explicit “tree_mode” knobs or are fixed across all rungs for a ladder.

In practice:
- Move gating for `expand_partial_sizes` and `max_raises_per_street` to depend on a new field: `tree_quality_level` or on a fixed threshold independent of `iterations` (e.g. only allow heavier expansion when `iterations` is below some safe bound used only by low-rung checks).
- Ensure `prune_class_weights` thresholds do not jump sharply between 30 and 60/50 rungs.

- [ ] **Step 4: Run ladder test**

Run: `python test_mode1_ev_convergence_ladder.py`.

- [ ] **Step 5: Commit**

Commit message: `feat: decouple quality equity from tree fan-out`.

---

### Task 3: Convergence guard (early-stop) inside CFR+ solve loop

**Files:**
- Modify: `postflop_solver.py`
- Modify: `postflop_service.py` (to pass thresholds / enable flag)
- Modify: `server.py` (optionally allow request override)
- New test: `test_mode1_early_stop_metadata.py`

**Interfaces:**
- Consumes: `iterations`, and new optional request fields like `convergence_guard=true/false`, `convergence_eps`, `convergence_patience`.
- Produces: output metadata:
  - `iterations_completed`
  - `stop_reason`
  - optional `convergence_metric_history` only for `response_mode="full"`.

- [ ] **Step 1: Write failing test**

Add test that sends a low `iterations` plus enable guard such that early-stop triggers (or use a higher `iterations` but loose thresholds so it triggers quickly). Assert presence of `stop_reason` and that `iterations_completed <= requested`.

- [ ] **Step 2: Run test to verify it fails**

Run the new test; it should fail because metadata doesn’t exist yet.

- [ ] **Step 3: Implement early-stop**

Inside `PostflopCFRSolver.solve()`:
- After each CFR iteration (or every N iterations), compute a cheap convergence metric:
  - Use regret diagnostics already computed at end; but for early-stop you need a per-iteration proxy.
  - Possible proxy: track maximum positive regret / or max absolute change in `hero_action_evs` between checkpoints.
- Define stopping criteria:
  - stop when metric < `convergence_eps` for `patience` consecutive checkpoints.

Expose:
- `stop_reason = "metric_below_threshold" | "reached_iterations"`.

- [ ] **Step 4: Run tests**

Run:
- `python test_mode1_early_stop_metadata.py`
- `python test_mode1_ev_convergence_ladder.py`

- [ ] **Step 5: Commit**

Commit message: `feat: add CFR+ convergence early-stop guard`.

---

### Task 4: Telemetry instrumentation for timeouts

**Files:**
- Modify: `postflop_solver.py`
- Modify: `server.py`
- Test: `test_mode1_latency_smoke.py`

**Interfaces:**
- Consumes: `response_mode`.
- Produces:
  - For `response_mode="full"` only: `timings_ms` dict with keys like: `equity_matrix_ms`, `tree_build_ms`, `cfr_ms`, `total_ms`.
  - For `compact`: do not include heavy diagnostics.

- [ ] **Step 1: Write failing test (lightweight)**

Add `test_mode1_timings_compact_absent.py` to assert `timings_ms` is absent in compact responses.

- [ ] **Step 2: Run to verify fails**

Run the test; expected FAIL.

- [ ] **Step 3: Implement telemetry**

Measure durations using `time.perf_counter()` around:
- `pe.compute_postflop_equity_matrix`
- tree build
- `solver.solve(iterations)`
- extract payload compaction.

Return timings only when `response_mode != "compact"`.

- [ ] **Step 4: Run tests**

Run:
- `python test_mode1_latency_smoke.py`
- `python test_mode1_timings_compact_absent.py`

- [ ] **Step 5: Commit**

Commit message: `feat: add latency telemetry (full only)`.

---

### Task 5: Budget-friendly quality pruning refactor (continuous mapping)

**Files:**
- Modify: `postflop_service.py`
- Modify: `test_mode1_ev_convergence_ladder.py`

**Interfaces:**
- Consumes: `iterations` and `compute_mode`.
- Produces: smooth mapping from budget to pruning parameters to reduce ladder discontinuities.

- [ ] **Step 1: Add failing test**

Add a test that asserts monotonic-ish behavior of `bestAct` for a small range of iteration values, or at least reduces EV drift spikes compared to pre-change logic.

- [ ] **Step 2: Run to verify fails**

Run ladder and record failures/drift.

- [ ] **Step 3: Implement continuous pruning mapping**

Replace stepwise thresholds with interpolation:
- Define `keep_top_n = lerp(a,b)` based on `iterations`.
- Define `prune_mass_fraction` similarly.

For quality, shift the curve slightly but keep ladder stability.

- [ ] **Step 4: Run tests**

Run:
- `python test_mode1_ev_convergence_ladder.py`
- `python test_mode1_ev_quality_vs_compact.py`

- [ ] **Step 5: Commit**

Commit message: `feat: continuous quality-aware pruning mapping`.

---

## Implementation approach choice

**Native recommended** because the tasks depend on shared interfaces (`compute_mode`, solver/equity seeding, and payload contract) and there are few code hotspots (`server.py`, `postflop_service.py`, `postflop_solver.py`), so one executor can keep interface consistency.
