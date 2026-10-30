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

        # Optional: which public board variant this node belongs to.
        # - For single-street solves: generally left as None (defaults to solver.board)
        # - For flop->turn solves: turn subtree nodes get a concrete board_key
        #   so showdown EV can select the right equity matrix.
        self.board_key: Optional[Tuple[str, ...]] = None

        # Optional: chance probabilities for CHANCE nodes.
        # Keys match node.actions; values should sum to ~1.
        self.chance_probs: Optional[Dict[str, float]] = None

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


def build_flop_to_turn_tree(
    *,
    flop_board: List[str],
    pot_pre: float,
    effective_stack: float,
    bet_config: PostflopBetConfig,
    oop_invested_pre: Optional[float] = None,
    ip_invested_pre: Optional[float] = None,
    turn_samples: int = 8,
    seed: int = 42,
    known_dead_cards: Optional[List[str]] = None,
    forced_turn_card: Optional[str] = None,
    river_samples: Optional[int] = 1,
    forced_river_card: Optional[str] = None,
) -> PostflopGameNode:
    """Build a 3-street (FLOP -> TURN -> RIVER) subgame.

    This is an MVP for the "flop->turn CFR+" mode:
    - FLOP betting is solved normally.
    - Every FLOP SHOWDOWN terminal is replaced by a CHANCE node that deals
      sampled TURN cards.
    - For each dealt TURN card, we solve a TURN betting subgame.
    - Every TURN SHOWDOWN terminal is replaced by a CHANCE node that deals
      sampled/conditioned RIVER (runout) cards, then we solve a RIVER betting
      subgame ending in RIVER SHOWDOWN.

    The CHANCE node outcomes are public turn cards; blocker interactions are
    handled downstream by EV/strategy computations that are conditioned on
    each combo and board variant.
    """

    if len(flop_board) != 3:
        raise ValueError(f"flop_board must have exactly 3 cards, got {len(flop_board)}")

    flop_board = [c.strip() for c in flop_board]
    dead_board = set(flop_board)
    known_dead = set(known_dead_cards or [])

    # Candidate turn cards: uniform over all remaining deck cards.
    # In flop->turn mode we exclude known_dead_cards (e.g. hero hole cards)
    # so we don't include impossible branches.
    turn_candidates = [
        c for c in pe.ALL_CARDS if c not in dead_board and c not in known_dead
    ]
    if not turn_candidates:
        raise ValueError("No turn candidates available")

    rng = random.Random(seed)

    if forced_turn_card is not None:
        forced_turn_card = str(forced_turn_card).strip()
        if forced_turn_card not in turn_candidates:
            raise ValueError(
                f"forced_turn_card={forced_turn_card!r} is not a valid turn outcome for the given flop/known_dead_cards"
            )
        sampled_turns = [forced_turn_card]
    elif turn_samples is None or turn_samples <= 0 or turn_samples >= len(turn_candidates):
        sampled_turns = list(turn_candidates)
    else:
        sampled_turns = rng.sample(turn_candidates, int(turn_samples))

    # Candidate river cards for each sampled turn variant.
    # This turns the previous 2-street MVP into a full 3-street
    # (FLOP -> TURN -> RIVER) hand-continuous subgame.
    sampled_rivers_by_turn: Dict[str, List[str]] = {}

    if forced_river_card is not None:
        forced_river_card = str(forced_river_card).strip()

    for turn_card in sampled_turns:
        dead_for_river = dead_board.union({turn_card}).union(known_dead)
        river_candidates = [c for c in pe.ALL_CARDS if c not in dead_for_river]
        if not river_candidates:
            raise ValueError(
                f"No river candidates available for turn_card={turn_card!r}"
            )

        if forced_river_card is not None:
            if forced_river_card not in river_candidates:
                raise ValueError(
                    f"forced_river_card={forced_river_card!r} is not a valid river outcome "
                    f"for flop={flop_board} turn={turn_card!r}"
                )
            sampled_rivers = [forced_river_card]
        elif (
            river_samples is None
            or river_samples <= 0
            or river_samples >= len(river_candidates)
        ):
            sampled_rivers = list(river_candidates)
        else:
            sampled_rivers = rng.sample(river_candidates, int(river_samples))

        sampled_rivers_by_turn[turn_card] = sampled_rivers

    flop_root = build_postflop_tree(
        pot_pre=pot_pre,
        effective_stack=effective_stack,
        bet_config=bet_config,
        street="FLOP",
        oop_invested_pre=oop_invested_pre,
        ip_invested_pre=ip_invested_pre,
    )

    def _prefix_node_ids(node: PostflopGameNode, prefix: str) -> None:
        node.node_id = f"{prefix}_{node.node_id}"
        node.history = f"{prefix}_{node.history}"
        for child in node.children.values():
            _prefix_node_ids(child, prefix)

    def _stamp_board_key(node: PostflopGameNode, board_key: Tuple[str, ...]) -> None:
        node.board_key = board_key
        for child in node.children.values():
            _stamp_board_key(child, board_key)

    def _replace_turn_showdowns_with_river(
        turn_root: PostflopGameNode,
        turn_card: str,
    ) -> None:
        sampled_rivers = sampled_rivers_by_turn.get(turn_card, [])
        if not sampled_rivers:
            raise ValueError(f"No sampled_rivers for turn_card={turn_card!r}")

        for act, child in list(turn_root.children.items()):
            if child.is_terminal and child.terminal_type == "SHOWDOWN":
                showdown = child

                chance_id = f"chance_river_{showdown.node_id}"
                chance_node = PostflopGameNode(
                    node_id=chance_id,
                    player="CHANCE",
                    pot=showdown.pot,
                    street="RIVER",
                    history=showdown.history,
                    oop_invested=showdown.oop_invested,
                    ip_invested=showdown.ip_invested,
                )

                chance_node.actions = list(sampled_rivers)
                if sampled_rivers:
                    p_each = 1.0 / float(len(sampled_rivers))
                    chance_node.chance_probs = {r: p_each for r in sampled_rivers}

                for river_card in sampled_rivers:
                    river_board_key = tuple(flop_board + [turn_card, river_card])

                    river_root = build_postflop_tree(
                        pot_pre=showdown.pot,
                        effective_stack=effective_stack,
                        bet_config=bet_config,
                        street="RIVER",
                        oop_invested_pre=showdown.oop_invested,
                        ip_invested_pre=showdown.ip_invested,
                    )

                    _prefix_node_ids(river_root, f"{chance_id}_{turn_card}_{river_card}")
                    _stamp_board_key(river_root, river_board_key)
                    chance_node.children[river_card] = river_root

                turn_root.children[act] = chance_node
                continue

            _replace_turn_showdowns_with_river(child, turn_card)

    def _replace_showdowns(node: PostflopGameNode) -> None:
        for act, child in list(node.children.items()):
            if child.is_terminal and child.terminal_type == "SHOWDOWN":
                showdown = child

                chance_id = f"chance_turn_{showdown.node_id}"
                chance_node = PostflopGameNode(
                    node_id=chance_id,
                    player="CHANCE",
                    pot=showdown.pot,
                    street="TURN",
                    history=showdown.history,
                    oop_invested=showdown.oop_invested,
                    ip_invested=showdown.ip_invested,
                )

                chance_node.actions = list(sampled_turns)
                if sampled_turns:
                    p_each = 1.0 / float(len(sampled_turns))
                    chance_node.chance_probs = {t: p_each for t in sampled_turns}

                for turn_card in sampled_turns:
                    turn_board_key = tuple(flop_board + [turn_card])

                    turn_root = build_postflop_tree(
                        pot_pre=showdown.pot,
                        effective_stack=effective_stack,
                        bet_config=bet_config,
                        street="TURN",
                        oop_invested_pre=showdown.oop_invested,
                        ip_invested_pre=showdown.ip_invested,
                    )

                    _prefix_node_ids(turn_root, f"{chance_id}_{turn_card}")
                    _stamp_board_key(turn_root, turn_board_key)

                    # Turn betting subtree ends in TURN SHOWDOWN terminals.
                    # Replace them with a CHANCE RIVER node for each river outcome
                    # sampled/conditioned under this specific turn_card.
                    _replace_turn_showdowns_with_river(turn_root, turn_card)

                    chance_node.children[turn_card] = turn_root

                node.children[act] = chance_node
                continue

            _replace_showdowns(child)

    _replace_showdowns(flop_root)
    return flop_root


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
        tree_mode: str = "standard",
        turn_samples: int = 8,
        turn_card: Optional[str] = None,
        river_samples: int = 1,
        river_card: Optional[str] = None,
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

        # Blocker compatibility is static for a solve. Precompute it once so
        # every CFR traversal does not rebuild card sets for every combo pair.
        self._compatible_ip_by_oop = {
            oop_combo: [
                ip_combo
                for ip_combo in self.combos_ip
                if not set(oop_combo).intersection(ip_combo)
            ]
            for oop_combo in self.combos_oop
        }
        self._compatible_oop_by_ip = {
            ip_combo: [
                oop_combo
                for oop_combo in self.combos_oop
                if not set(ip_combo).intersection(oop_combo)
            ]
            for ip_combo in self.combos_ip
        }

        # Build Game Tree (support hand-continuous initial investments)
        tree_mode_norm = str(tree_mode or "standard").lower().strip()
        turn_variant_board_keys: List[Tuple[str, ...]] = []

        if tree_mode_norm == "flop_to_turn":
            if len(self.board) != 3:
                raise ValueError(
                    f"flop_to_turn expects a FLOP board (len=3); got len={len(self.board)}"
                )

            known_dead_cards: List[str] = []
            if hero_cards and isinstance(hero_cards, list):
                known_dead_cards = [str(c).strip() for c in hero_cards if c]

            # Mirror build_flop_to_turn_tree sampling so we can precompute
            # equity matrices for every sampled TURN board variant.
            dead_board = set(self.board)
            known_dead = set(known_dead_cards)
            turn_candidates = [
                c for c in pe.ALL_CARDS if c not in dead_board and c not in known_dead
            ]
            if not turn_candidates:
                raise ValueError("No turn candidates available")

            sampled_turns: List[str]
            forced_turn_card: Optional[str] = None

            if turn_card is not None:
                forced_turn_card = str(turn_card).strip()
                if forced_turn_card not in turn_candidates:
                    raise ValueError(
                        f"turn_card={forced_turn_card!r} is not a valid turn outcome for the given flop/known cards"
                    )
                sampled_turns = [forced_turn_card]
            else:
                rng = random.Random(self.seed)
                ts = int(turn_samples)
                if ts is None or ts <= 0 or ts >= len(turn_candidates):
                    sampled_turns = list(turn_candidates)
                else:
                    sampled_turns = rng.sample(turn_candidates, ts)

            turn_variant_board_keys = [tuple(self.board + [t]) for t in sampled_turns]

            forced_river_card: Optional[str] = (
                str(river_card).strip() if river_card is not None else None
            )

            self.root = build_flop_to_turn_tree(
                flop_board=self.board,
                pot_pre=self.pot_pre,
                effective_stack=self.effective_stack,
                bet_config=self.bet_config,
                oop_invested_pre=oop_invested_pre,
                ip_invested_pre=ip_invested_pre,
                turn_samples=int(turn_samples),
                seed=self.seed,
                known_dead_cards=known_dead_cards,
                forced_turn_card=forced_turn_card,
                river_samples=int(river_samples),
                forced_river_card=forced_river_card,
            )
        else:
            self.root = build_postflop_tree(
                self.pot_pre,
                self.effective_stack,
                self.bet_config,
                street="FLOP" if len(self.board) == 3 else ("TURN" if len(self.board) == 4 else "RIVER"),
                oop_invested_pre=oop_invested_pre,
                ip_invested_pre=ip_invested_pre,
            )

            # Standard mode has no explicit TURN board variant selection.
            turn_variant_board_keys = []

        # Stamp default board_key for all nodes.
        default_board_key = tuple(self.board)

        def _stamp(node: PostflopGameNode) -> None:
            # Preserve board_key if a caller already stamped it (e.g. flop->turn).
            # CHANCE nodes are not a concrete board variant, so we keep their
            # board_key unset (None) instead of stamping the default board.
            if node.board_key is None and node.player != "CHANCE":
                node.board_key = default_board_key
            for child in node.children.values():
                _stamp(child)

        _stamp(self.root)

        # Compute / Initialize Equity Matrix
        # Standard mode computes a single equity matrix for self.board.
        # flop->turn mode additionally computes equity matrices for each
        # sampled TURN board_key so SHOWDOWN EV can select the right board.
        self.equity_matrix = pe.compute_postflop_equity_matrix(
            self.combos_oop,
            self.combos_ip,
            self.board,
            iters_per_matchup=self.equity_iters,
            seed=self.seed,
        )

        self.equity_matrices: Dict[
            Tuple[str, ...],
            Dict[Tuple[str, str], Dict[Tuple[str, str], float]],
        ] = {tuple(self.board): self.equity_matrix}

        if tree_mode_norm == "flop_to_turn" and turn_variant_board_keys:
            for bkey in turn_variant_board_keys:
                eq = pe.compute_postflop_equity_matrix(
                    self.combos_oop,
                    self.combos_ip,
                    list(bkey),
                    iters_per_matchup=self.equity_iters,
                    seed=self.seed,
                )
                self.equity_matrices[bkey] = eq

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

        # CHANCE nodes don't have player strategies/regrets.
        if node.player == "CHANCE":
            for child in node.children.values():
                self._init_node_states(child)
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
            if node.player not in ("OOP", "IP", "CHANCE"):
                raise ValueError(
                    f"Decision node {node.node_id} must be OOP/IP/CHANCE, got {node.player!r}"
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
        compatible_combos_by_side: Dict[Tuple[str, str], List[Tuple[str, str]]],
    ) -> Dict[Tuple[str, str], float]:
        """Mix child utilities using only blocker-compatible acting combos."""
        out: Dict[Tuple[str, str], float] = {}
        for side_combo in side_combos:
            tot = 0.0
            mix = {a: 0.0 for a in actions}
            compatible_acting_combos = compatible_combos_by_side.get(side_combo, [])
            for acting_combo in compatible_acting_combos:
                reach = acting_reach.get(acting_combo, 0.0)
                if reach <= 0.0:
                    continue
                tot += reach
                strategy = acting_strats[acting_combo]
                for action in actions:
                    mix[action] += strategy.get(action, 0.0) * reach

            if tot > 1e-15:
                for action in actions:
                    mix[action] /= tot
            elif actions:
                uniform = 1.0 / len(actions)
                for action in actions:
                    mix[action] = uniform

            out[side_combo] = sum(
                mix[action] * action_evs[action].get(side_combo, 0.0)
                for action in actions
            )
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

        # CHANCE nodes: probability-weight utilities; no CFR regret updates.
        if node.player == "CHANCE":
            actions = node.actions or list(node.children.keys())
            if not actions:
                # Degenerate: no outgoing outcomes.
                return {
                    "OOP": {c: 0.0 for c in self.combos_oop},
                    "IP": {c: 0.0 for c in self.combos_ip},
                }

            probs = node.chance_probs or {}
            p_sum = sum(float(probs.get(a, 0.0)) for a in actions)
            if p_sum <= 1e-15:
                p_map = {a: 1.0 / len(actions) for a in actions}
            else:
                p_map = {a: float(probs.get(a, 0.0)) / p_sum for a in actions}

            ev_oop = {c: 0.0 for c in self.combos_oop}
            ev_ip = {c: 0.0 for c in self.combos_ip}

            for a in actions:
                child = node.children.get(a)
                if not child:
                    continue
                p = float(p_map.get(a, 0.0))
                if p <= 0.0:
                    continue

                # Scale reaches so deeper regrets/strategy sums include chance mass.
                # In multi-board (e.g. flop->turn) trees, the chosen chance
                # outcome commits to a concrete board_key. Combo cards that
                # overlap with that board variant become impossible, so we
                # must zero their reach mass before recursing.
                child_board_key = child.board_key if child.board_key is not None else None
                dead_variant: Optional[Set[str]] = set(child_board_key) if child_board_key else None

                if dead_variant:
                    reach_oop_child = {
                        c: (reach_oop.get(c, 0.0) * p)
                        if (c[0] not in dead_variant and c[1] not in dead_variant)
                        else 0.0
                        for c in self.combos_oop
                    }
                    reach_ip_child = {
                        c: (reach_ip.get(c, 0.0) * p)
                        if (c[0] not in dead_variant and c[1] not in dead_variant)
                        else 0.0
                        for c in self.combos_ip
                    }
                else:
                    reach_oop_child = {c: reach_oop.get(c, 0.0) * p for c in self.combos_oop}
                    reach_ip_child = {c: reach_ip.get(c, 0.0) * p for c in self.combos_ip}

                child_res = self._cfr_traverse(child, reach_oop_child, reach_ip_child, weight)

                for c in self.combos_oop:
                    ev_oop[c] += p * float(child_res["OOP"].get(c, 0.0))
                for c in self.combos_ip:
                    ev_ip[c] += p * float(child_res["IP"].get(c, 0.0))

            return {"OOP": ev_oop, "IP": ev_ip}

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
        # - Regrets use compatible opponent reach only.
        # - Strategy averaging uses the acting player's realization reach.
        # - Action EV diagnostics are averaged per infoset, not by own reach.
        opponent_reach = reach_ip if current_player == "OOP" else reach_oop
        opponent_combos = self.combos_ip if current_player == "OOP" else self.combos_oop

        for c in combos:
            strat = current_strats[c]
            ev = sum(strat[a] * action_utilities[a].get(c, 0.0) for a in node.actions)
            node_ev[c] = ev

            my_reach = (
                reach_oop.get(c, 0.0)
                if current_player == "OOP"
                else reach_ip.get(c, 0.0)
            )
            compatible_opponent_combos = (
                self._compatible_ip_by_oop.get(c, [])
                if current_player == "OOP"
                else self._compatible_oop_by_ip.get(c, [])
            )
            compatible_opponent_reach = sum(
                opponent_reach.get(opponent_combo, 0.0)
                for opponent_combo in compatible_opponent_combos
            )

            if compatible_opponent_reach > 0.0:
                for a in node.actions:
                    # Regret matching uses counterfactual opportunity reach, not
                    # the acting combo's own realization reach. A zero own reach
                    # must not freeze this combo's future regrets.
                    regret_delta = action_utilities[a].get(c, 0.0) - ev
                    node.regrets[c][a] = max(
                        0.0,
                        node.regrets[c][a]
                        + regret_delta * compatible_opponent_reach,
                    )

                    # The average strategy is realization-weighted by the acting
                    # player's reach at this information set.
                    if my_reach > 0.0:
                        node.strategy_sum[c][a] += strat[a] * weight * my_reach

                    # Action EV is conditional on this information set. Weight it
                    # by iteration only, avoiding bias from the combo's own path.
                    node.action_utility_sum[c][a] += action_utilities[a].get(c, 0.0) * weight
                node.action_utility_weight[c] += weight

        if current_player == "OOP":
            ev_oop = node_ev
            ev_ip = self._mix_side_ev(
                action_ev_ip,
                self.combos_ip,
                combos,
                reach_oop,
                current_strats,
                node.actions,
                self._compatible_oop_by_ip,
            )
        else:
            ev_ip = node_ev
            ev_oop = self._mix_side_ev(
                action_ev_oop,
                self.combos_oop,
                combos,
                reach_ip,
                current_strats,
                node.actions,
                self._compatible_ip_by_oop,
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
        board_key = node.board_key if node.board_key is not None else tuple(self.board)

        # Multi-board (flop->turn->river): compute the exact equity matrix for
        # the concrete board variant when needed.
        eq_matrix = self.equity_matrices.get(board_key)
        if eq_matrix is None:
            eq_matrix = pe.compute_postflop_equity_matrix(
                self.combos_oop,
                self.combos_ip,
                list(board_key),
                iters_per_matchup=self.equity_iters,
                seed=self.seed,
            )
            self.equity_matrices[board_key] = eq_matrix

        for c_oop in self.combos_oop:
            oop_share_w = 0.0
            weight_count = 0.0
            c_oop_set = {c_oop[0], c_oop[1]}
            for c_ip, r_ip in reach_ip.items():
                if r_ip <= 0.0 or c_ip[0] in c_oop_set or c_ip[1] in c_oop_set:
                    continue
                eq = eq_matrix.get(c_oop, {}).get(c_ip, 0.5)
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
                eq_oop = eq_matrix.get(c_oop, {}).get(c_ip, 0.5)
                ip_share_w += r_oop * (1.0 - eq_oop)
                weight_count += r_oop
            if weight_count > 0:
                ev_ip[c_ip] = (ip_share_w / weight_count) * pot_after_rake - node.ip_invested + rake_half
            else:
                ev_ip[c_ip] = 0.5 * pot_after_rake - node.ip_invested + rake_half

        self._sanity_side_evs(node, ev_oop, ev_ip)
        return {"OOP": ev_oop, "IP": ev_ip}

    def _iter_decision_nodes(self, node: PostflopGameNode):
        """Yield decision nodes in the betting tree (skip CHANCE nodes)."""
        if node.is_terminal:
            return
        if node.player in ("OOP", "IP"):
            yield node
        for child in node.children.values():
            yield from self._iter_decision_nodes(child)

    def _extract_all_node_results(self) -> Dict[str, Any]:
        """Collect all decision nodes in the game tree and extract GTO strategies."""

        nodes_data: Dict[str, Any] = {}
        # Reach summaries are computed from the final average strategy so the
        # sweep can measure the practical frequency of deep betting lines.
        node_reach_mass: Dict[str, Dict[str, float]] = {}
        node_action_reach_mass: Dict[str, Dict[str, Dict[str, float]]] = {}

        def average_strategy(
            node: PostflopGameNode,
            combo: Tuple[str, str],
        ) -> Dict[str, float]:
            s_sum = node.strategy_sum.get(combo, {})
            total = sum(s_sum.values())
            if total > 0.0:
                return {
                    action: s_sum.get(action, 0.0) / total
                    for action in node.actions
                }
            if not node.actions:
                return {}
            uniform = 1.0 / len(node.actions)
            return {action: uniform for action in node.actions}

        def collect_reach(
            node: PostflopGameNode,
            reach_oop: Dict[Tuple[str, str], float],
            reach_ip: Dict[Tuple[str, str], float],
        ) -> None:
            if node.is_terminal:
                return

            oop_mass = sum(max(0.0, value) for value in reach_oop.values())
            ip_mass = sum(max(0.0, value) for value in reach_ip.values())
            hero_reach = 0.0
            if self._hero_combo is not None:
                if self.hero_is_oop:
                    hero_reach = max(0.0, reach_oop.get(self._hero_combo, 0.0))
                else:
                    hero_reach = max(0.0, reach_ip.get(self._hero_combo, 0.0))

            opponent_mass = ip_mass if self.hero_is_oop else oop_mass
            node_reach_mass[node.node_id] = {
                "oop": float(oop_mass),
                "ip": float(ip_mass),
                "joint": float(oop_mass * ip_mass),
                "hero": float(hero_reach),
                "hero_joint": float(hero_reach * opponent_mass),
            }

            # CHANCE nodes: branch probabilities come from chance_probs, not player strategies.
            if node.player == "CHANCE":
                actions = node.actions or list(node.children.keys())
                probs = node.chance_probs or {}
                p_sum = sum(float(probs.get(a, 0.0)) for a in actions)
                if p_sum <= 1e-15:
                    p_map = {a: 1.0 / len(actions) for a in actions}
                else:
                    p_map = {a: float(probs.get(a, 0.0)) / p_sum for a in actions}

                action_reach: Dict[str, Dict[str, float]] = {}
                for action in actions:
                    p = float(p_map.get(action, 0.0))
                    action_oop_mass = oop_mass * p
                    action_ip_mass = ip_mass * p
                    action_hero_reach = hero_reach * p
                    action_reach[action] = {
                        "oop": float(action_oop_mass),
                        "ip": float(action_ip_mass),
                        "joint": float(action_oop_mass * action_ip_mass),
                        "hero": float(action_hero_reach),
                        "hero_joint": float(action_hero_reach * opponent_mass),
                    }
                node_action_reach_mass[node.node_id] = action_reach

                for action, child in node.children.items():
                    p = float(p_map.get(action, 0.0))
                    if p <= 0.0:
                        continue
                    child_oop = {c: reach_oop.get(c, 0.0) * p for c in reach_oop}
                    child_ip = {c: reach_ip.get(c, 0.0) * p for c in reach_ip}
                    collect_reach(child, child_oop, child_ip)
                return

            combos = self.combos_oop if node.player == "OOP" else self.combos_ip
            current_strategies = {combo: average_strategy(node, combo) for combo in combos}
            action_reach: Dict[str, Dict[str, float]] = {}
            hero_player = "OOP" if self.hero_is_oop else "IP"
            for action in node.actions:
                action_oop_mass = oop_mass
                action_ip_mass = ip_mass
                if node.player == "OOP":
                    action_oop_mass = sum(
                        max(0.0, reach_oop.get(combo, 0.0))
                        * current_strategies[combo].get(action, 0.0)
                        for combo in combos
                    )
                else:
                    action_ip_mass = sum(
                        max(0.0, reach_ip.get(combo, 0.0))
                        * current_strategies[combo].get(action, 0.0)
                        for combo in combos
                    )

                action_hero_reach = hero_reach
                if node.player == hero_player and self._hero_combo is not None:
                    action_hero_reach = hero_reach * current_strategies.get(
                        self._hero_combo,
                        {},
                    ).get(action, 0.0)

                action_reach[action] = {
                    "oop": float(action_oop_mass),
                    "ip": float(action_ip_mass),
                    "joint": float(action_oop_mass * action_ip_mass),
                    "hero": float(action_hero_reach),
                    "hero_joint": float(action_hero_reach * opponent_mass),
                }
            node_action_reach_mass[node.node_id] = action_reach

            combos = self.combos_oop if node.player == "OOP" else self.combos_ip
            for action, child in node.children.items():
                child_oop = dict(reach_oop)
                child_ip = dict(reach_ip)
                for combo in combos:
                    probability = average_strategy(node, combo).get(action, 0.0)
                    if node.player == "OOP":
                        child_oop[combo] = reach_oop.get(combo, 0.0) * probability
                    else:
                        child_ip[combo] = reach_ip.get(combo, 0.0) * probability
                collect_reach(child, child_oop, child_ip)

        def traverse_collect(node: PostflopGameNode):
            if node.is_terminal:
                return

            # CHANCE nodes: strategies are not player-controlled.
            if node.player == "CHANCE":
                combos: List[Tuple[str, str]] = []
                weights: Dict[Tuple[str, str], float] = {}
                node_combo_strats: Dict[Tuple[str, str], Dict[str, float]] = {}
                class_strategies: Dict[str, Dict[str, float]] = {}

                actions = node.actions or list(node.children.keys())
                probs = node.chance_probs or {}
                p_sum = sum(float(probs.get(a, 0.0)) for a in actions)
                if p_sum <= 1e-15:
                    overall_freqs = {a: 1.0 / len(actions) for a in actions}
                else:
                    overall_freqs = {a: float(probs.get(a, 0.0)) / p_sum for a in actions}

            else:
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

            reach_mass = node_reach_mass.get(node.node_id, {})
            action_reach_mass = node_action_reach_mass.get(node.node_id, {})

            nodes_data[node.node_id] = {
                "node_id": node.node_id,
                "history": node.history,
                "player": node.player,
                "pot": node.pot,
                "street": node.street,
                "board_key": list(node.board_key) if node.board_key is not None else None,
                "oop_invested": node.oop_invested,
                "ip_invested": node.ip_invested,
                "reach_mass": reach_mass,
                "reach_weight": reach_mass.get("joint", 0.0),
                "action_reach_mass": action_reach_mass,
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

        initial_reach_oop = {
            combo: self.oop_combo_weights[combo]
            for combo in self.combos_oop
        }
        initial_reach_ip = {
            combo: self.ip_combo_weights[combo]
            for combo in self.combos_ip
        }
        collect_reach(self.root, initial_reach_oop, initial_reach_ip)

        # Normalize node and action opportunity by the root mass. This keeps
        # reach metrics comparable across boards and range sizes.
        root_mass = node_reach_mass.get("root", {})
        root_joint = max(1e-12, float(root_mass.get("joint", 0.0)))
        root_hero_joint = max(1e-12, float(root_mass.get("hero_joint", 0.0)))
        for mass in node_reach_mass.values():
            mass["joint_probability"] = (
                float(mass.get("joint", 0.0)) / root_joint
            )
            mass["hero_joint_probability"] = (
                float(mass.get("hero_joint", 0.0)) / root_hero_joint
            )
        for action_map in node_action_reach_mass.values():
            for action_mass in action_map.values():
                action_mass["joint_probability"] = (
                    float(action_mass.get("joint", 0.0)) / root_joint
                )
                action_mass["hero_joint_probability"] = (
                    float(action_mass.get("hero_joint", 0.0)) / root_hero_joint
                )

        traverse_collect(self.root)

        # Residual CFR+ regret diagnostics. Cumulative positive regret is
        # normalized by completed iterations so the convergence probes can
        # distinguish a stable-looking mix from an unfinished subgame.
        node_regret_diagnostics: Dict[str, Dict[str, float]] = {}
        global_max_normalized = 0.0
        global_weighted_sum = 0.0
        global_weighted_mass = 0.0
        for decision_node in self._iter_decision_nodes(self.root):
            combo_regret_values: List[float] = []
            weighted_sum = 0.0
            weighted_mass = 0.0
            for combo in (
                self.combos_oop
                if decision_node.player == "OOP"
                else self.combos_ip
            ):
                regrets = decision_node.regrets.get(combo, {})
                max_regret = max(
                    (max(0.0, float(value)) for value in regrets.values()),
                    default=0.0,
                )
                normalized = max_regret / max(1, self._cfr_iters_done)
                combo_regret_values.append(normalized)
                combo_weight = float(
                    (self.oop_combo_weights if decision_node.player == "OOP" else self.ip_combo_weights)
                    .get(combo, 0.0)
                )
                weighted_sum += normalized * combo_weight
                weighted_mass += combo_weight

            node_max = max(combo_regret_values, default=0.0)
            node_mean = (
                sum(combo_regret_values) / len(combo_regret_values)
                if combo_regret_values
                else 0.0
            )
            node_weighted_mean = weighted_sum / weighted_mass if weighted_mass > 0.0 else 0.0
            node_regret_diagnostics[decision_node.node_id] = {
                "max_normalized_regret": float(node_max),
                "mean_normalized_regret": float(node_mean),
                "weighted_mean_normalized_regret": float(node_weighted_mean),
            }
            global_max_normalized = max(global_max_normalized, node_max)
            global_weighted_sum += weighted_sum
            global_weighted_mass += weighted_mass

        regret_diagnostics = {
            "iterations_completed": float(self._cfr_iters_done),
            "max_normalized_regret": float(global_max_normalized),
            "weighted_mean_normalized_regret": float(
                global_weighted_sum / global_weighted_mass
                if global_weighted_mass > 0.0
                else 0.0
            ),
            "by_node": node_regret_diagnostics,
        }

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
            "regret_diagnostics": regret_diagnostics,
        }
