from pathlib import Path

UI_MAIN = Path('C:/Users/USER/Documents/Nova pasta/poker_gto_mode1_app.html')
UI_WORKTREE = Path('C:/Users/USER/Documents/Nova pasta/.claude/worktrees/rfi-two-stage-ui/poker_gto_mode1_app.html')


def assert_contains(path: Path, needle: str) -> None:
    text = path.read_text(encoding='utf-8', errors='ignore')
    if needle not in text:
        raise SystemExit(f'FAIL: {path} missing expected text: {needle!r}')


def main() -> None:
    for p in (UI_MAIN, UI_WORKTREE):
        if not p.exists():
            raise SystemExit(f'FAIL: missing UI file: {p}')

    # Both UIs must recommend by EV (not by frequency).
    # We check for the specific variables/logic we added.
    needles = [
        'hero_action_evs',
        'bestEv',
        'formatEv',
        'EV indisponível',
    ]

    # Badge should also exist.
    badge_needles = ['2ª raise']

    for p in (UI_MAIN, UI_WORKTREE):
        for n in needles:
            assert_contains(p, n)
        for n in badge_needles:
            assert_contains(p, n)

    print('PASS')


if __name__ == '__main__':
    main()
