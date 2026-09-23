from __future__ import annotations


def apply_rake_to_pot(pot: float, rake_pct: float, rake_cap: float = 0.0) -> float:
    """Return pot after rake.

    - pot, rake_pct, rake_cap are in the same currency units (bb in this project).
    - If rake_pct <= 0 => no rake.
    - If rake_cap <= 0 => no cap (cap is disabled).
    """

    pot = float(pot)
    rake_pct = float(rake_pct)
    rake_cap = float(rake_cap)

    if pot <= 0:
        return pot
    if rake_pct <= 0:
        return pot

    rake = pot * rake_pct
    if rake_cap > 0:
        rake = min(rake, rake_cap)

    return pot - rake


def cap_total(amount: float, effective_stack: float) -> float:
    """Cap a player's total committed chips by the effective stack.

    effective_stack <= 0 disables the cap (used as "infinite / unused").
    """

    amount = float(amount)
    if amount < 0.0:
        amount = 0.0
    stack = float(effective_stack)
    if stack <= 0.0:
        return amount
    return min(amount, stack)


def cap_pot_by_effective_stack(pot: float, effective_stack: float) -> float:
    """Legacy scaffolding: cap a showdown pot by 2*effective_stack.

    Prefer cap_total() on each player's contribution. Kept for smoke-test
    compatibility with the Milestone 1 helper.
    """

    pot = float(pot)
    effective_stack = float(effective_stack)

    if pot <= 0:
        return pot
    if effective_stack <= 0:
        return pot

    return min(pot, 2.0 * effective_stack)


def apply_rake_to_pot_with_stack(
    pot: float,
    rake_pct: float,
    rake_cap: float,
    effective_stack: float,
) -> float:
    """Apply rake to a pot, after applying a simple effective-stack cap.

    Legacy helper. Solvers now cap *investments* via cap_total() and then
    rake the resulting pot with apply_rake_to_pot().
    """

    pot_capped = cap_pot_by_effective_stack(pot, effective_stack)
    return apply_rake_to_pot(pot_capped, rake_pct, rake_cap)
