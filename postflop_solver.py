"""Postflop CFR+ Subgame Solver for Heads-Up Poker.

Solves postflop game trees given:
- Board cards (Flop / Turn / River)
- OOP and IP starting combo ranges and weights
- Starting Pot, Effective Stack, and Rake configuration
- Configurable betting abstract tree (Sizes: 33%, 50%, 75%, 125%, All-in, Raise)

Outputs:
- GTO Strategy frequencies (% Check, Bet, Call, Raise, Fold) for every decision node:
  * OOP first decision (Check / Donk Bet)
  * IP after OOP Check (Check Behind / C-bet small / C-bet large)
  * OOP facing C-bet (Fold / Call / Check-Raise)
  * IP facing Check-Raise (Fold / Call)
- Expected Value (EV) per combo and overall range EV.
- Hand strength categories & texture distribution.
"""

from __future__ import annotations

import copy
import random
from typing import Any, Dict, List, Optional, Set, Tuple

import eval7

from mode1_game_model import apply_rake_to_pot, cap_total
import postflop_evaluator as pe

# Rank order for canonical combo keys (same as postflop_evaluator)
RANKS = "AKQJT98765432"


class PostflopBetConfig:
    """Configures available betting sizings (as fraction of pot)."""

    def __init__(
        self,
        oop_lead_sizes: Optional[List[float]] = None,      # e.g. [0.33, 0.75]
        ip_cbet_sizes: Optional[List[float]] = None,       # e.g. [0.33, 0.75]
        check_raise_sizes: Optional[List[float]] = None,   # multiplier of bet, e.g. [3.0]
        ip_raise_sizes: Optional[List[float]] = None,      # multiplier of bet, e.g. [3.0]
        allow_donk: bool = False,                          # allow OOP to bet first vs preflop aggressor
        max_raises_per_street: int = 2,
    ):
        self.oop_lead_sizes = oop_lead_sizes or ([0.33, 0.5, 0.75] if allow_donk else [])
        self.ip_cbet_sizes = ip_cbet_sizes or [0.33, 0.5, 0.75]
        # Abstrações limitadas: mantém a árvore controlada e melhora cobertura.
        self.check_raise_sizes = check_raise_sizes or [2.0, 3.0]
        self.ip_raise_sizes = ip_raise_sizes or [2.0, 3.0]
        self.allow_donk = allow_donk
        self.max_raises_per_street = max_raises_per_street


class PostflopGameNode:
    """Represents a game state node in the postflop game tree.

    Utility convention:
    - Decision nodes: ``player`` is "OOP" or "IP" (whose turn it is).
    - Terminals: ``player`` is always "TERMINAL". Payoff sign is NEVER taken
      from ``player``; only ``terminal_type`` + invested chips matter.
    """

    def __init__(
        self,
        node_id: str,
        player: str,  # "OOP", "IP", or "TERMINAL"
        pot: float,
        street: str,  # "FLOP", "TURN", "RIVER"
        history: str,  # e.g. "root", "x", "x_b33", "x_b33_c"
        is_terminal: bool = False,
        terminal_type: Optional[str] = None,  # "FOLD_OOP", "FOLD_IP", "SHOWDOWN"
        oop_invested: float = 0.0,
        ip_invested: float = 0.0,
    ):
        self.node_id = node_id
        self.player = player
        self.pot = pot
        self.street = street
        self.history = history
        self.is_terminal = is_terminal
        self.terminal_type = terminal_type
        self.oop_invested = float(oop_invested)
        self.ip_invested = float(ip_invested)

        # Actions and children: action_name -> PostflopGameNode
        self.children: Dict[str, PostflopGameNode] = {}
        self.actions: List[str] = []

        # CFR state per combo:
        # regrets[combo][action] -> float
        self.regrets: Dict[Tuple[str, str], Dict[str, float]] = {}
        # strategy_sum[combo][action] -> float (for average GTO strategy)
        self.strategy_sum: Dict[Tuple[str, str], Dict[str, float]] = {}

        # Per-combo EV-by-action (acting player's EV)
        self.action_utility_sum: Dict[Tuple[str, str], Dict[str, float]] = {}
        self.action_utility_weight: Dict[Tuple[str, str], float] = {}


def build_postflop_tree(
    pot_pre: float,
    effective_stack: float,
    bet_config: PostflopBetConfig,
    street: str = "FLOP",
    oop_invested_pre: Optional[float] = None,
    ip_invested_pre: Optional[float] = None,
) -> PostflopGameNode:
    """Build a standard postflop betting tree for a single street subgame.

    For hand-continuous flows (preflop->flop->turn->river), oop_invested_pre and
    ip_invested_pre should be the amounts already invested by each player in previous
    streets. If None, they default to pot_pre/2.0 (equal investment assumption).
    """

    # Validate inputs for hand-continuous consistency
    if (oop_invested_pre is not None) != (ip_invested_pre is not None):
        raise ValueError("Both oop_invested_pre and ip_invested_pre must be provided or both omitted")

    if oop_invested_pre is not None and ip_invested_pre is not None:
        # Hand-continuous mode: use provided investments
        oop_base = float(oop_invested_pre)
        ip_base = float(ip_invested_pre)
        # Verify pot consistency: pot should equal sum of investments
        expected_pot = oop_base + ip_base
        if abs(pot_pre - expected_pot) > 1e-6:
            raise ValueError(
                f"Pot inconsistency: pot_pre={pot_pre} != "
                f"oop_invested_pre+ip_invested_pre={expected_pot}"
            )
    else:
        # Default mode: equal investments (preflop pots assumed symmetric)
        oop_base = pot_pre / 2.0
        ip_base = pot_pre / 2.0

    root = PostflopGameNode(
        node_id="root",
        player="OOP",
        pot=pot_pre,
        street=street,
        history="root",
        oop_invested=oop_base,
        ip_invested=ip_base,
    )

    # 1. OOP first action
    if bet_config.allow_donk and bet_config.oop_lead_sizes:
        oop_actions = ["check"] + [f"bet_{int(s*100)}" for s in bet_config.oop_lead_sizes]
    else:
        oop_actions = ["check"]

    root.actions = oop_actions

    # Build OOP Check subtree -> IP turn to act
    node_x = PostflopGameNode(
        "x",
        "IP",
        pot_pre,
        street,
        "x",
        oop_invested=oop_base,
        ip_invested=ip_base,
    )
    root.children["check"] = node_x

    # IP after OOP Check: Check behind (Showdown / Next street) or C-bet
    ip_cbet_actions = ["check"] + [f"bet_{int(s*100)}" for s in bet_config.ip_cbet_sizes]
    node_x.actions = ip_cbet_actions

    # IP checks behind -> Showdown / Next street
    node_x.children["check"] = PostflopGameNode(
        "x_x",
        "TERMINAL",
        pot_pre,
        street,
        "x_x",
        is_terminal=True,
        terminal_type="SHOWDOWN",
        oop_invested=oop_base,
        ip_invested=ip_base,
    )

    # 1b. Donk bet OOP -> IP response
    oop_remaining = max(0.0, effective_stack - oop_base)
    ip_remaining = max(0.0, effective_stack - ip_base)

    if bet_config.allow_donk and bet_config.oop_lead_sizes:
        for s in bet_config.oop_lead_sizes:
            act_name = f"bet_{int(s*100)}"
            bet_amount = cap_total(pot_pre * s, oop_remaining)
            new_pot = pot_pre + bet_amount

            node_donk = PostflopGameNode(
                f"b_{act_name}",
                "IP",
                new_pot,
                street,
                f"b_{act_name}",
                oop_invested=oop_base + bet_amount,
                ip_invested=ip_base,
            )
            root.children[act_name] = node_donk

            node_donk.actions = ["fold", "call"]
            if bet_config.ip_raise_sizes:
                for r_mult in bet_config.ip_raise_sizes:
                    node_donk.actions.append(f"raise_{int(r_mult*10)}x")

            # IP folds to donk bet -> OOP wins
            node_donk.children["fold"] = PostflopGameNode(
                f"b_{act_name}_f",
                "TERMINAL",
                new_pot,
                street,
                f"b_{act_name}_f",
                is_terminal=True,
                terminal_type="FOLD_IP",
                oop_invested=oop_base + bet_amount,
                ip_invested=ip_base,
            )

            # IP calls donk bet -> Showdown
            call_pot = pot_pre + 2.0 * bet_amount
            node_donk.children["call"] = PostflopGameNode(
                f"b_{act_name}_c",
                "TERMINAL",
                call_pot,
                street,
                f"b_{act_name}_c",
                is_terminal=True,
                terminal_type="SHOWDOWN",
                oop_invested=oop_base + bet_amount,
                ip_invested=ip_base + bet_amount,
            )

            # IP raises donk -> OOP response
            if bet_config.ip_raise_sizes:
                for r_mult in bet_config.ip_raise_sizes:
                    raise_act = f"raise_{int(r_mult*10)}x"
                    raise_additional = cap_total(bet_amount * r_mult, max(0.0, ip_remaining - bet_amount))

                    raise_pot = pot_pre + 2.0 * bet_amount + raise_additional

                    node_raise = PostflopGameNode(
                        f"b_{act_name}_{raise_act}",
                        "OOP",
                        raise_pot,
                        street,
                        f"b_{act_name}_{raise_act}",
                        oop_invested=oop_base + bet_amount,
                        ip_invested=ip_base + bet_amount + raise_additional,
                    )
                    node_donk.children[raise_act] = node_raise
                    node_raise.actions = ["fold", "call"]

                    # OOP folds to raise -> IP wins
                    node_raise.children["fold"] = PostflopGameNode(
                        f"b_{act_name}_{raise_act}_f",
                        "TERMINAL",
                        raise_pot,
                        street,
                        f"b_{act_name}_{raise_act}_f",
                        is_terminal=True,
                        terminal_type="FOLD_OOP",
                        oop_invested=oop_base + bet_amount,
                        ip_invested=ip_base + bet_amount + raise_additional,
                    )

                    # OOP calls raise -> Showdown
                    call_raise_pot = pot_pre + 2.0 * bet_amount + 2.0 * raise_additional
                    node_raise.children["call"] = PostflopGameNode(
                        f"b_{act_name}_{raise_act}_c",
                        "TERMINAL",
                        call_raise_pot,
                        street,
                        f"b_{act_name}_{raise_act}_c",
                        is_terminal=True,
                        terminal_type="SHOWDOWN",
                        oop_invested=oop_base + bet_amount + raise_additional,
                        ip_invested=ip_base + bet_amount + raise_additional,
                    )

    # IP C-bets -> OOP response
    for s in bet_config.ip_cbet_sizes:
        act_name = f"bet_{int(s*100)}"
        bet_amount = cap_total(pot_pre * s, ip_remaining)
        new_pot = pot_pre + bet_amount

        node_cbet = PostflopGameNode(
            f"x_{act_name}",
            "OOP",
            new_pot,
            street,
            f"x_{act_name}",
            oop_invested=oop_base,
            ip_invested=ip_base + bet_amount,
        )
        node_x.children[act_name] = node_cbet

        # OOP responses to IP C-bet: Fold, Call, Check-Raise
        oop_resp_actions = ["fold", "call"]
        if bet_config.check_raise_sizes:
            for r_mult in bet_config.check_raise_sizes:
                oop_resp_actions.append(f"raise_{int(r_mult*10)}x")

        node_cbet.actions = oop_resp_actions

        # OOP folds -> IP wins
        node_cbet.children["fold"] = PostflopGameNode(
            f"x_{act_name}_f",
            "TERMINAL",
            new_pot,
            street,
            f"x_{act_name}_f",
            is_terminal=True,
            terminal_type="FOLD_OOP",
            oop_invested=oop_base,
            ip_invested=ip_base + bet_amount,
        )

        # OOP calls -> Pot += bet_amount -> Showdown
        call_pot = pot_pre + 2.0 * bet_amount
        node_cbet.children["call"] = PostflopGameNode(
            f"x_{act_name}_c",
            "TERMINAL",
            call_pot,
            street,
            f"x_{act_name}_c",
            is_terminal=True,
            terminal_type="SHOWDOWN",
            oop_invested=oop_base + bet_amount,
            ip_invested=ip_base + bet_amount,
        )

        # OOP raises -> IP response (Fold, Call)
        if bet_config.check_raise_sizes:
            for r_mult in bet_config.check_raise_sizes:
                raise_act = f"raise_{int(r_mult*10)}x"
                # raise_amount representa a parte EXTRA acima do call (ou seja, além de bet_amount)
                max_extra = max(0.0, oop_remaining - bet_amount)
                raise_amount = cap_total(bet_amount * r_mult, max_extra)
                # OOP raise includes calling the bet (bet_amount) plus an extra raise_additional (raise_amount)
                raise_pot = pot_pre + 2.0 * bet_amount + raise_amount

                node_raise = PostflopGameNode(
                    f"x_{act_name}_{raise_act}",
                    "IP",
                    raise_pot,
                    street,
                    f"x_{act_name}_{raise_act}",
                    oop_invested=oop_base + bet_amount + raise_amount,
                    ip_invested=ip_base + bet_amount,
                )
                node_cbet.children[raise_act] = node_raise
                node_raise.actions = ["fold", "call"]

                # Optional 2nd raise on this street (IP reraise after OOP check-raise).
                # This uses the existing ip_raise_sizes grid as multipliers.
                if bet_config.max_raises_per_street >= 2 and bet_config.ip_raise_sizes:
                    for r_mult2 in bet_config.ip_raise_sizes:
                        raise2_act = f"raise_{int(r_mult2*10)}x"

                        # At this node, IP has already called the original cbet (bet_amount),
                        # and OOP has an extra raise_amount on top.
                        # The extra beyond calling is capped by remaining chips.
                        max_extra2 = max(0.0, ip_remaining - bet_amount - raise_amount)
                        raise2_additional = cap_total(raise_amount * r_mult2, max_extra2)

                        # Skip degenerate (all-in capped away) raise options.
                        if raise2_additional <= 0.0:
                            continue

                        # pot after IP reraise (OOP hasn't called the extra yet)
                        reraise_pot = pot_pre + 2.0 * bet_amount + 2.0 * raise_amount + raise2_additional

                        node_reraise = PostflopGameNode(
                            f"x_{act_name}_{raise_act}_{raise2_act}",
                            "OOP",
                            reraise_pot,
                            street,
                            f"x_{act_name}_{raise_act}_{raise2_act}",
                            oop_invested=oop_base + bet_amount + raise_amount,
                            ip_invested=ip_base + bet_amount + raise_amount + raise2_additional,
                        )

                        node_raise.actions.append(raise2_act)
                        node_raise.children[raise2_act] = node_reraise

                        node_reraise.actions = ["fold", "call"]

                        # OOP folds to the reraise -> IP wins (fold terminal)
                        node_reraise.children["fold"] = PostflopGameNode(
                            f"x_{act_name}_{raise_act}_{raise2_act}_f",
                            "TERMINAL",
                            reraise_pot,
                            street,
                            f"x_{act_name}_{raise_act}_{raise2_act}_f",
                            is_terminal=True,
                            terminal_type="FOLD_OOP",
                            oop_invested=oop_base + bet_amount + raise_amount,
                            ip_invested=ip_base + bet_amount + raise_amount + raise2_additional,
                        )

                        # OOP calls -> showdown with both investing the extra
                        call_reraise_pot = pot_pre + 2.0 * bet_amount + 2.0 * raise_amount + 2.0 * raise2_additional
                        node_reraise.children["call"] = PostflopGameNode(
                            f"x_{act_name}_{raise_act}_{raise2_act}_c",
                            "TERMINAL",
                            call_reraise_pot,
                            street,
                            f"x_{act_name}_{raise_act}_{raise2_act}_c",
                            is_terminal=True,
                            terminal_type="SHOWDOWN",
                            oop_invested=oop_base + bet_amount + raise_amount + raise2_additional,
                            ip_invested=ip_base + bet_amount + raise_amount + raise2_additional,
                        )

                # IP folds to raise -> OOP wins
                node_raise.children["fold"] = PostflopGameNode(
                    f"x_{act_name}_{raise_act}_f",
                    "TERMINAL",
                    raise_pot,
                    street,
                    f"x_{act_name}_{raise_act}_f",
                    is_terminal=True,
                    terminal_type="FOLD_IP",
                    oop_invested=oop_base + bet_amount + raise_amount,
                    ip_invested=ip_base + bet_amount,
                )

                # IP calls raise -> Showdown
                # IP call matches the full raise_amount on top of their existing bet
                call_raise_pot = pot_pre + 2.0 * bet_amount + 2.0 * raise_amount
                node_raise.children["call"] = PostflopGameNode(
                    f"x_{act_name}_{raise_act}_c",
                    "TERMINAL",
                    call_raise_pot,
                    street,
                    f"x_{act_name}_{raise_act}_c",
                    is_terminal=True,
                    terminal_type="SHOWDOWN",
                    oop_invested=oop_base + bet_amount + raise_amount,
                    ip_invested=ip_base + bet_amount + raise_amount,
                )

    return root


class PostflopCFRSolver:
    """High-performance Counterfactual Regret Minimization (CFR+) solver for postflop nodes."""

    def __init__(
        self,
        board: List[str],
        oop_range: Dict[str, float],   # 169-class weights (e.g. from preflop defend/call)
        ip_range: Dict[str, float],    # 169-class weights (e.g. from preflop RFI/open)
        pot_pre: float = 5.4,
        effective_stack: float = 100.0,
        rake_pct: float = 0.0,
        rake_cap: float = 0.0,
        bet_config: Optional[PostflopBetConfig] = None,
        equity_iters: int = 40,
        seed: int = 42,
        hero_cards: Optional[List[str]] = None,
        hero_is_oop: bool = True,
        oop_invested_pre: Optional[float] = None,
        ip_invested_pre: Optional[float] = None,
    ):
        self.board = [b.strip() for b in board]
        self.pot_pre = float(pot_pre)
        self.effective_stack = float(effective_stack)
        self.rake_pct = float(rake_pct)
        self.rake_cap = float(rake_cap)
        self.equity_iters = equity_iters
        self.seed = seed

        self.bet_config = bet_config or PostflopBetConfig()
        self.dead_board = set(self.board)

        # Expand combo weights and filter dead board cards
        self.oop_combo_weights = pe.expand_range_to_combos(oop_range, self.dead_board)
        self.ip_combo_weights = pe.expand_range_to_combos(ip_range, self.dead_board)

        self.combos_oop = sorted(list(self.oop_combo_weights.keys()))
        self.combos_ip = sorted(list(self.ip_combo_weights.keys()))

        # Build Game Tree (support hand-continuous initial investments)
        self.root = build_postflop_tree(
            self.pot_pre,
            self.effective_stack,
            self.bet_config,
            street="FLOP" if len(self.board) == 3 else ("TURN" if len(self.board) == 4 else "RIVER"),
            oop_invested_pre=oop_invested_pre,
            ip_invested_pre=ip_invested_pre,
        )

        # Compute / Initialize Equity Matrix
        self.equity_matrix = pe.compute_postflop_equity_matrix(
            self.combos_oop,
            self.combos_ip,
            self.board,
            iters_per_matchup=self.equity_iters,
            seed=self.seed,
        )

        # Hero combo tracking for EV-by-action recommendation
        # Importante: as keys dos combos em postflop_evaluator vêm do gerador ALL_COMBOS_1326,
        # que ordena os cards por índice em ALL_CARDS (rank + suit). Então, para pocket pairs
        # (mesmo rank), precisamos considerar também a ordem de suit; caso contrário
        # hero_action_evs pode ficar vazio por mismatch de chave.
        self.hero_cards = hero_cards
        self.hero_is_oop = hero_is_oop
        self._hero_combo: Optional[Tuple[str, str]] = None

        # Only a single seat's range should contain the hero's actual hand.
        # Using the union (OOP or IP) can accidentally pick the hero combo as it
        # exists in the *wrong* seat-range, leaving hero_action_evs empty.
        hero_side_combos = self.combos_oop if self.hero_is_oop else self.combos_ip

        if hero_cards and len(hero_cards) == 2:
            c1, c2 = hero_cards[0], hero_cards[1]
            cand1 = (c1, c2)
            cand2 = (c2, c1)

            # Prefer the exact tuple key present in the hero seat's expanded combos.
            if cand1 in hero_side_combos:
                self._hero_combo = cand1
            elif cand2 in hero_side_combos:
                self._hero_combo = cand2
            else:
                # Fallback: sort by the same ALL_CARDS order used by postflop_evaluator.
                SUITS = "shdc"  # must match postflop_evaluator

                def card_index(card: str) -> int:
                    card = str(card).strip()
                    r = card[0].upper()
                    s = card[1].lower()
                    return RANKS.index(r) * len(SUITS) + SUITS.index(s)

                ordered = tuple(sorted([c1, c2], key=card_index))
                if ordered in hero_side_combos:
                    self._hero_combo = ordered
                else:
                    self._hero_combo = None

        # Initialize Regrets and Strategies in tree
        self._init_node_states(self.root)

        # Freeze the utility convention before any CFR iteration.
        self._validate_tree_convention(self.root)
        self._sanity_checked_nodes: Set[str] = set()

        # Track how many CFR+ iterations were already run (needed for adaptive batching).
        self._cfr_iters_done: int = 0

    def _init_node_states(self, node: PostflopGameNode) -> None:
        if node.is_terminal:
            return

        combos = self.combos_oop if node.player == "OOP" else self.combos_ip
        for c in combos:
            node.regrets[c] = {a: 0.0 for a in node.actions}
            node.strategy_sum[c] = {a: 0.0 for a in node.actions}
            node.action_utility_sum[c] = {a: 0.0 for a in node.actions}
            node.action_utility_weight[c] = 0.0

        for child in node.children.values():
            self._init_node_states(child)

    def _get_current_strategy(
        self,
        node: PostflopGameNode,
        combo: Tuple[str, str],
    ) -> Dict[str, float]:
        """CFR+ Regret-Matching+ strategy for a given combo."""
        regrets = node.regrets.get(combo, {})
        actions = node.actions
        if not actions:
            return {}

        pos_regrets = [max(0.0, regrets.get(a, 0.0)) for a in actions]
        total_pos = sum(pos_regrets)

        if total_pos <= 1e-12:
            default_prob = 1.0 / len(actions)
            return {a: default_prob for a in actions}

        return {a: max(0.0, regrets.get(a, 0.0)) / total_pos for a in actions}

    def solve(self, iterations: int = 300) -> Dict[str, Any]:
        """Run CFR+ iterations across the tree.

        Supports incremental calls (adaptive batching) by keeping an internal iteration offset.
        """

        start_it = self._cfr_iters_done + 1
        end_it = self._cfr_iters_done + iterations

        for it in range(start_it, end_it + 1):
            # Weight for CFR+ strategy averaging (linear weighting by absolute iter index)
            weight = float(it)

            # Reach probabilities at root
            reach_oop = {c: self.oop_combo_weights[c] for c in self.combos_oop}
            reach_ip = {c: self.ip_combo_weights[c] for c in self.combos_ip}

            # Traverse tree
            self._cfr_traverse(self.root, reach_oop, reach_ip, weight)

        self._cfr_iters_done = end_it

        # Extract final strategies & EVs across all nodes
        return self._extract_all_node_results()

    def _validate_tree_convention(self, node: PostflopGameNode) -> None:
        """Defensive: terminals never carry a seat, investments reconstruct the pot."""
        if node.is_terminal:
            if node.player != "TERMINAL":
                raise ValueError(
                    f"Terminal {node.node_id} must have player='TERMINAL', got {node.player!r}"
                )
            if node.terminal_type not in ("FOLD_OOP", "FOLD_IP", "SHOWDOWN"):
                raise ValueError(
                    f"Terminal {node.node_id} has unknown terminal_type={node.terminal_type!r}"
                )
        else:
            if node.player not in ("OOP", "IP"):
                raise ValueError(
                    f"Decision node {node.node_id} must be OOP/IP, got {node.player!r}"
                )

        invested_sum = node.oop_invested + node.ip_invested
        if abs(invested_sum - node.pot) > 1e-6:
            raise ValueError(
                f"Node {node.node_id}: pot={node.pot} != oop_invested+ip_invested={invested_sum}"
            )

        for child in node.children.values():
            self._validate_tree_convention(child)

    def _rake_split(self, pot: float) -> Tuple[float, float]:
        """Return (pot_after_rake, rake_half) used by the zero-sum payoff convention."""
        pot_after_rake = apply_rake_to_pot(pot, self.rake_pct, self.rake_cap)
        rake_amount = max(0.0, pot - pot_after_rake)
        return pot_after_rake, rake_amount / 2.0

    def _fold_payoffs(self, node: PostflopGameNode) -> Tuple[float, float]:
        """Constant (payoff_oop, payoff_ip) for a fold terminal. Sum is 0."""
        pot_after_rake, rake_half = self._rake_split(node.pot)
        if node.terminal_type == "FOLD_OOP":
            payoff_oop = -node.oop_invested + rake_half
            payoff_ip = pot_after_rake - node.ip_invested + rake_half
        elif node.terminal_type == "FOLD_IP":
            payoff_oop = pot_after_rake - node.oop_invested + rake_half
            payoff_ip = -node.ip_invested + rake_half
        else:
            raise ValueError(f"_fold_payoffs called on {node.terminal_type}")
        return payoff_oop, payoff_ip

    def _showdown_bounds(self, node: PostflopGameNode) -> Tuple[float, float, float, float]:
        """Inclusive EV bounds: 0-equity (lose) .. 1-equity (win), same rebate as folds."""
        pot_after_rake, rake_half = self._rake_split(node.pot)
        lo_oop = -node.oop_invested + rake_half
        hi_oop = pot_after_rake - node.oop_invested + rake_half
        lo_ip = -node.ip_invested + rake_half
        hi_ip = pot_after_rake - node.ip_invested + rake_half
        return lo_oop, hi_oop, lo_ip, hi_ip

    def _sanity_side_evs(
        self,
        node: PostflopGameNode,
        ev_oop: Dict[Tuple[str, str], float],
        ev_ip: Dict[Tuple[str, str], float],
    ) -> None:
        """Cheap internal checks. Raises on a broken payoff convention.

        Fold payoffs are constant, so they are always verified. Showdown bounds
        are verified once per node (the formula is affine in equity in [0, 1]).
        """
        if node.terminal_type in ("FOLD_OOP", "FOLD_IP"):
            payoff_oop, payoff_ip = self._fold_payoffs(node)
            if abs(payoff_oop + payoff_ip) > 1e-6:
                raise ValueError(
                    f"{node.node_id}: fold payoffs not zero-sum "
                    f"(oop={payoff_oop}, ip={payoff_ip})"
                )
            for c, v in ev_oop.items():
                if abs(v - payoff_oop) > 1e-9:
                    raise ValueError(f"{node.node_id}: OOP combo {c} fold EV {v} != {payoff_oop}")
            for c, v in ev_ip.items():
                if abs(v - payoff_ip) > 1e-9:
                    raise ValueError(f"{node.node_id}: IP combo {c} fold EV {v} != {payoff_ip}")
            return

        if node.node_id in self._sanity_checked_nodes:
            return
        self._sanity_checked_nodes.add(node.node_id)

        lo_oop, hi_oop, lo_ip, hi_ip = self._showdown_bounds(node)
        for c, v in ev_oop.items():
            if v < lo_oop - 1e-6 or v > hi_oop + 1e-6:
                raise ValueError(
                    f"{node.node_id}: OOP combo {c} showdown EV {v} outside [{lo_oop}, {hi_oop}]"
                )
        for c, v in ev_ip.items():
            if v < lo_ip - 1e-6 or v > hi_ip + 1e-6:
                raise ValueError(
                    f"{node.node_id}: IP combo {c} showdown EV {v} outside [{lo_ip}, {hi_ip}]"
                )

    def _mix_side_ev(
        self,
        action_evs: Dict[str, Dict[Tuple[str, str], float]],
        side_combos: List[Tuple[str, str]],
        acting_combos: List[Tuple[str, str]],
        acting_reach: Dict[Tuple[str, str], float],
        acting_strats: Dict[Tuple[str, str], Dict[str, float]],
        actions: List[str],
    ) -> Dict[Tuple[str, str], float]:
        """Opponent (or same-side parent) EV: mix child utilities by the acting player's reach-weighted strategy."""
        tot = 0.0
        mix = {a: 0.0 for a in actions}
        for c in acting_combos:
            r = acting_reach.get(c, 0.0)
            if r <= 0.0:
                continue
            tot += r
            st = acting_strats[c]
            for a in actions:
                mix[a] += st[a] * r
        if tot > 1e-15:
            for a in actions:
                mix[a] /= tot
        elif actions:
            u = 1.0 / len(actions)
            for a in actions:
                mix[a] = u

        out: Dict[Tuple[str, str], float] = {}
        for c in side_combos:
            out[c] = sum(mix[a] * action_evs[a].get(c, 0.0) for a in actions)
        return out

    def _cfr_traverse(
        self,
        node: PostflopGameNode,
        reach_oop: Dict[Tuple[str, str], float],
        reach_ip: Dict[Tuple[str, str], float],
        weight: float,
    ) -> Dict[str, Dict[Tuple[str, str], float]]:
        """Recursive CFR+. Returns both sides: {"OOP": ev_oop, "IP": ev_ip}.

        Sign is never taken from ``node.player``. Terminals are always keyed by
        seat (OOP/IP) via ``terminal_type`` + invested chips.
        """

        if node.is_terminal:
            return self._compute_terminal_utilities(node, reach_oop, reach_ip)

        current_player = node.player
        combos = self.combos_oop if current_player == "OOP" else self.combos_ip

        current_strats: Dict[Tuple[str, str], Dict[str, float]] = {}
        for c in combos:
            current_strats[c] = self._get_current_strategy(node, c)

        action_ev_oop: Dict[str, Dict[Tuple[str, str], float]] = {}
        action_ev_ip: Dict[str, Dict[Tuple[str, str], float]] = {}
        action_utilities: Dict[str, Dict[Tuple[str, str], float]] = {}

        for act in node.actions:
            child = node.children[act]
            if current_player == "OOP":
                new_reach_oop = {c: reach_oop[c] * current_strats[c][act] for c in combos}
                child_res = self._cfr_traverse(child, new_reach_oop, reach_ip, weight)
            else:
                new_reach_ip = {c: reach_ip[c] * current_strats[c][act] for c in combos}
                child_res = self._cfr_traverse(child, reach_oop, new_reach_ip, weight)

            action_ev_oop[act] = child_res["OOP"]
            action_ev_ip[act] = child_res["IP"]
            action_utilities[act] = child_res[current_player]

        node_ev: Dict[Tuple[str, str], float] = {}

        # CFR+ with counterfactual reach:
        # - For regrets: weight by TOTAL opponent reach (not same combo)
        # - For strategy_sum: weight by current player's reach
        # The counterfactual is: "how often does opponent give me THIS decision opportunity"
        # Since we're iterating over MY combos, we use opponent's TOTAL reach probability
        opponent_reach = reach_ip if current_player == "OOP" else reach_oop
        total_opponent_reach = sum(opponent_reach.values()) if opponent_reach else 0.0

        for c in combos:
            strat = current_strats[c]
            ev = sum(strat[a] * action_utilities[a].get(c, 0.0) for a in node.actions)
            node_ev[c] = ev

            # My reach probability (used for strategy_sum and EV accumulation)
            my_reach = reach_oop.get(c, 0.0) if current_player == "OOP" else reach_ip.get(c, 0.0)

            # Counterfactual reach: use TOTAL opponent reach, not same-combo reach
            # This is critical - opponent having a *different* combo still gives us
            # the decision opportunity. Using same-combo would give cf_prob ≈ 0
            cf_prob = total_opponent_reach

            if my_reach > 0.0 and cf_prob > 0.0:
                for a in node.actions:
                    # Regret: difference between action's EV and current strategy EV
                    # Weighted by counterfactual reach (how often opponent lets us decide)
                    regret_delta = action_utilities[a].get(c, 0.0) - ev
                    node.regrets[c][a] = max(0.0, node.regrets[c][a] + regret_delta * cf_prob)

                    # Strategy sum: weighted by current player's reach
                    node.strategy_sum[c][a] += strat[a] * weight * my_reach

                    # EV-by-action accumulation (for hero_action_evs)
                    act_ev = action_utilities[a].get(c, 0.0)
                    node.action_utility_sum[c][a] += act_ev * weight * my_reach
                node.action_utility_weight[c] += weight * my_reach

        if current_player == "OOP":
            ev_oop = node_ev
            ev_ip = self._mix_side_ev(
                action_ev_ip, self.combos_ip, combos, reach_oop, current_strats, node.actions
            )
        else:
            ev_ip = node_ev
            ev_oop = self._mix_side_ev(
                action_ev_oop, self.combos_oop, combos, reach_ip, current_strats, node.actions
            )

        return {"OOP": ev_oop, "IP": ev_ip}

    def _compute_terminal_utilities(
        self,
        node: PostflopGameNode,
        reach_oop: Dict[Tuple[str, str], float],
        reach_ip: Dict[Tuple[str, str], float],
    ) -> Dict[str, Dict[Tuple[str, str], float]]:
        """Payoffs for BOTH seats. Never uses ``node.player`` for sign or keys."""
        pot_after_rake, rake_half = self._rake_split(node.pot)

        ev_oop: Dict[Tuple[str, str], float] = {c: 0.0 for c in self.combos_oop}
        ev_ip: Dict[Tuple[str, str], float] = {c: 0.0 for c in self.combos_ip}

        if node.terminal_type in ("FOLD_OOP", "FOLD_IP"):
            payoff_oop, payoff_ip = self._fold_payoffs(node)
            for c_oop in self.combos_oop:
                ev_oop[c_oop] = payoff_oop
            for c_ip in self.combos_ip:
                ev_ip[c_ip] = payoff_ip
            self._sanity_side_evs(node, ev_oop, ev_ip)
            return {"OOP": ev_oop, "IP": ev_ip}

        # SHOWDOWN: EV = equity_share * pot_after_rake - invested + rake_half
        for c_oop in self.combos_oop:
            oop_share_w = 0.0
            weight_count = 0.0
            c_oop_set = {c_oop[0], c_oop[1]}
            for c_ip, r_ip in reach_ip.items():
                if r_ip <= 0.0 or c_ip[0] in c_oop_set or c_ip[1] in c_oop_set:
                    continue
                eq = self.equity_matrix.get(c_oop, {}).get(c_ip, 0.5)
                oop_share_w += r_ip * eq
                weight_count += r_ip
            if weight_count > 0:
                ev_oop[c_oop] = (oop_share_w / weight_count) * pot_after_rake - node.oop_invested + rake_half
            else:
                ev_oop[c_oop] = 0.5 * pot_after_rake - node.oop_invested + rake_half

        for c_ip in self.combos_ip:
            ip_share_w = 0.0
            weight_count = 0.0
            c_ip_set = {c_ip[0], c_ip[1]}
            for c_oop, r_oop in reach_oop.items():
                if r_oop <= 0.0 or c_oop[0] in c_ip_set or c_oop[1] in c_ip_set:
                    continue
                eq_oop = self.equity_matrix.get(c_oop, {}).get(c_ip, 0.5)
                ip_share_w += r_oop * (1.0 - eq_oop)
                weight_count += r_oop
            if weight_count > 0:
                ev_ip[c_ip] = (ip_share_w / weight_count) * pot_after_rake - node.ip_invested + rake_half
            else:
                ev_ip[c_ip] = 0.5 * pot_after_rake - node.ip_invested + rake_half

        self._sanity_side_evs(node, ev_oop, ev_ip)
        return {"OOP": ev_oop, "IP": ev_ip}

    def _extract_all_node_results(self) -> Dict[str, Any]:
        """Collect all decision nodes in the game tree and extract GTO strategies."""

        nodes_data: Dict[str, Any] = {}

        def traverse_collect(node: PostflopGameNode):
            if node.is_terminal:
                return

            combos = self.combos_oop if node.player == "OOP" else self.combos_ip
            weights = self.oop_combo_weights if node.player == "OOP" else self.ip_combo_weights

            node_combo_strats: Dict[Tuple[str, str], Dict[str, float]] = {}
            for c in combos:
                s_sum = node.strategy_sum.get(c, {})
                tot = sum(s_sum.values())
                if tot > 0:
                    node_combo_strats[c] = {a: s_sum[a] / tot for a in node.actions}
                else:
                    d_p = 1.0 / len(node.actions)
                    node_combo_strats[c] = {a: d_p for a in node.actions}

            class_strategies: Dict[str, Dict[str, float]] = {}
            for a in node.actions:
                act_vals = {c: node_combo_strats[c][a] for c in combos}
                agg = pe.aggregate_combos_to_classes(act_vals, weights)
                for cls, val in agg.items():
                    if cls not in class_strategies:
                        class_strategies[cls] = {}
                    class_strategies[cls][a] = val

            # Overall frequency of each action at this node
            overall_freqs: Dict[str, float] = {}
            total_active_weight = sum(weights.get(c, 0.0) for c in combos)
            for a in node.actions:
                if total_active_weight > 0:
                    action_weight = sum(node_combo_strats[c][a] * weights.get(c, 0.0) for c in combos)
                    overall_freqs[a] = action_weight / total_active_weight
                else:
                    overall_freqs[a] = 1.0 / len(node.actions)

            # Node metadata and strategies
            action_outcomes: Dict[str, Dict[str, Any]] = {}
            for act in node.actions:
                child = node.children.get(act)
                if not child:
                    continue
                if child.is_terminal:
                    action_outcomes[act] = {
                        "terminal_type": child.terminal_type,
                        "pot": child.pot,
                        "oop_invested": child.oop_invested,
                        "ip_invested": child.ip_invested,
                    }
                else:
                    action_outcomes[act] = {
                        "next_node_id": child.node_id,
                        "terminal_type": None,
                        "pot": child.pot,
                        "oop_invested": child.oop_invested,
                        "ip_invested": child.ip_invested,
                    }

            # EV-by-action for the hero's combo (only when this node is the hero's side)
            hero_action_evs: Dict[str, float] = {}
            hero_diag: Dict[str, Any] = {}

            hero_combo = self._hero_combo
            hero_side = "OOP" if self.hero_is_oop else "IP"
            node_player = node.player

            hero_combo_key = None
            if hero_combo is not None:
                hero_combo_key = f"{hero_combo[0]}{hero_combo[1]}"

            hero_diag["hero_combo_present"] = hero_combo is not None
            hero_diag["hero_combo_key"] = hero_combo_key
            hero_diag["hero_side_match"] = (node_player == hero_side)
            hero_diag["hero_weight_sum"] = 0.0

            if hero_combo is not None and node_player == hero_side:
                u_sum = node.action_utility_sum.get(hero_combo, {})
                w_sum = float(node.action_utility_weight.get(hero_combo, 0.0))
                hero_diag["hero_weight_sum"] = w_sum

                if w_sum > 0.0:
                    hero_action_evs = {
                        a: round(u_sum.get(a, 0.0) / w_sum, 6)
                        for a in node.actions
                    }

            nodes_data[node.node_id] = {
                "node_id": node.node_id,
                "history": node.history,
                "player": node.player,
                "pot": node.pot,
                "street": node.street,
                "oop_invested": node.oop_invested,
                "ip_invested": node.ip_invested,
                "actions": node.actions,
                "overall_frequencies": overall_freqs,
                "strategy_classes": class_strategies,
                "combo_strategies": {f"{c[0]}{c[1]}": node_combo_strats[c] for c in combos},
                "action_outcomes": action_outcomes,
                "hero_action_evs": hero_action_evs,
                "hero_diag": hero_diag,
            }

            for child in node.children.values():
                traverse_collect(child)

        traverse_collect(self.root)

        # Categorize hands for OOP and IP
        oop_categories = {}
        for c in self.combos_oop:
            oop_categories[f"{c[0]}{c[1]}"] = pe.categorize_hand_on_board(c, self.board)

        ip_categories = {}
        for c in self.combos_ip:
            ip_categories[f"{c[0]}{c[1]}"] = pe.categorize_hand_on_board(c, self.board)

        # Hero-specific metadata for correct UI display
        hero_combo_key = None
        if self._hero_combo is not None:
            hero_combo_key = f"{self._hero_combo[0]}{self._hero_combo[1]}"
        hero_side = "OOP" if self.hero_is_oop else "IP"
        hero_categories = oop_categories if self.hero_is_oop else ip_categories
        hero_category = None
        if hero_combo_key and hero_combo_key in hero_categories:
            hero_category = hero_categories[hero_combo_key].get("category")
        hero_draw = None
        if hero_combo_key and hero_combo_key in hero_categories:
            hero_draw = hero_categories[hero_combo_key].get("draw")

        return {
            "board": self.board,
            "pot_pre": self.pot_pre,
            "effective_stack": self.effective_stack,
            "nodes": nodes_data,
            "oop_categories": oop_categories,
            "ip_categories": ip_categories,
            "hero_is_oop": self.hero_is_oop,
            "hero_combo": self._hero_combo,
            "hero_combo_key": hero_combo_key,
            "hero_categories": hero_categories,
            "hero_category": hero_category,
            "hero_draw": hero_draw,
            "num_oop_combos": len(self.combos_oop),
            "num_ip_combos": len(self.combos_ip),
        }
