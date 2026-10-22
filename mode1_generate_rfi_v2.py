import argparse
import os
import subprocess
import sys


ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
STORAGE_DIR = os.path.join(ROOT_DIR, "storage")

HERO_POS_LIST = ["CO", "BTN", "BB"]
VILL_OPEN_LIST = ["UTG", "CO", "BTN"]
DEFAULT_OPEN_SIZE = 2.2


def run_cmd(cmd: list[str], dry_run: bool) -> None:
    if dry_run:
        print("[dry-run] " + " ".join(cmd))
        return
    print("[run] " + " ".join(cmd))
    subprocess.check_call(cmd)


def open_suffix(open_size: float) -> tuple[str, float]:
    x = round(float(open_size), 1)
    if abs(x - DEFAULT_OPEN_SIZE) < 1e-9:
        return "", x
    return f"_open{x:.1f}bb", x


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", default=sys.executable, help="Python interpreter")
    ap.add_argument("--iters", type=int, default=2500, help="CFR iterations")
    ap.add_argument("--equityIters", type=int, default=100, help="MC runouts per combo pairing")
    ap.add_argument("--comboSamples", type=int, default=3, help="combo-pairs samples per class-pair")
    ap.add_argument("--seed", type=int, default=123, help="Base seed")
    ap.add_argument("--evReplicates", type=int, default=8, help="Replicates for EV stabilization")
    ap.add_argument("--evSeedStep", type=int, default=10007, help="Seed step between EV replicates")
    ap.add_argument("--rakePct", type=float, default=0.0, help="Rake as a fraction of showdown pot (default 0 = disabled)")
    ap.add_argument("--rakeCap", type=float, default=0.0, help="Rake cap in bb (<=0 disables cap)")

    ap.add_argument("--smallStackThreshold", type=float, default=20.0, help="Depth (bb) threshold treated as 'small' for extra equity/iters precision")
    ap.add_argument("--itersSmall", type=int, default=4000, help="CFR iterations for small stacks")
    ap.add_argument("--equityItersSmall", type=int, default=400, help="MC runouts per combo pair for small stacks")
    ap.add_argument("--comboSamplesSmall", type=int, default=6, help="Number of combo-pairs samples per class-pair for small stacks")

    ap.add_argument("--useEquityCache", action="store_true", help="Cache computed equity matrices on disk (speeds up regen with higher precision)")
    ap.add_argument("--equityCacheDir", default=os.path.join(STORAGE_DIR, "equity_cache"), help="Directory for equity cache files")
    ap.add_argument(
        "--stacks",
        type=str,
        default="20,40,100,200",
        help="Comma-separated effective stack depths in bb (used to generate suffixed JSONs).",
    )
    ap.add_argument("--rangeProfile", default="standard", choices=["tight", "standard", "loose"], help="Static OPEN range profile")
    ap.add_argument("--openSizes", type=str, default="2.2", help="Comma-separated open sizes in bb (default 2.2)")

    ap.add_argument("--force", action="store_true", help="Regenerate even if output exists")
    ap.add_argument("--dry-run", action="store_true", help="Print commands only")
    args = ap.parse_args()

    script = os.path.join(ROOT_DIR, "preflop_gto_rfi.py")
    if not os.path.exists(script):
        raise SystemExit(f"Missing: {script}")

    os.makedirs(STORAGE_DIR, exist_ok=True)

    stacks: list[float] = []
    for x in str(args.stacks).split(","):
        x = x.strip()
        if not x:
            continue
        stacks.append(float(x))

    if not stacks:
        raise SystemExit("--stacks is empty")

    open_sizes: list[float] = []
    for x in str(args.openSizes).split(","):
        x = x.strip()
        if not x:
            continue
        open_sizes.append(round(float(x), 1))

    if not open_sizes:
        raise SystemExit("--openSizes is empty")

    missing = 0
    total = 0

    range_profile = args.rangeProfile

    for open_size in open_sizes:
        open_suf, _ = open_suffix(open_size)

        for hero_pos in HERO_POS_LIST:
            for vill_open_pos in VILL_OPEN_LIST:
                for stack_bbs in stacks:
                    stack_int = int(round(stack_bbs))

                    prefix = f"preflop_gto_rfi_{hero_pos}_vsOpen_{vill_open_pos}"

                    if stack_int == 100:
                        if range_profile == "standard":
                            out_name = f"{prefix}{open_suf}.json"
                        else:
                            out_name = f"{prefix}{open_suf}_{range_profile}.json"
                    else:
                        if range_profile == "standard":
                            out_name = f"{prefix}{open_suf}_stack{stack_int}bb.json"
                        else:
                            out_name = f"{prefix}{open_suf}_stack{stack_int}bb_{range_profile}.json"

                    out_path = os.path.join(STORAGE_DIR, out_name)
                    total += 1

                    if (not args.force) and os.path.exists(out_path):
                        print(f"[skip] {out_name} already exists")
                        continue

                    missing += 1

                    use_small = stack_bbs <= float(args.smallStackThreshold)
                    cur_iters = int(args.itersSmall) if use_small else int(args.iters)
                    cur_equityIters = int(args.equityItersSmall) if use_small else int(args.equityIters)
                    cur_comboSamples = int(args.comboSamplesSmall) if use_small else int(args.comboSamples)

                    cmd = [
                        args.python,
                        script,
                        "--heroPos",
                        hero_pos,
                        "--villOpenPos",
                        vill_open_pos,
                        "--out",
                        out_path,
                        "--iters",
                        str(cur_iters),
                        "--equityIters",
                        str(cur_equityIters),
                        "--comboSamples",
                        str(cur_comboSamples),
                        "--seed",
                        str(args.seed),
                        "--evReplicates",
                        str(args.evReplicates),
                        "--evSeedStep",
                        str(args.evSeedStep),
                        "--rakePct",
                        str(args.rakePct),
                        "--rakeCap",
                        str(args.rakeCap),
                        "--effectiveStack",
                        str(stack_bbs),
                        "--rangeProfile",
                        range_profile,
                        "--openSize",
                        str(open_size),
                    ]
                    if args.useEquityCache:
                        cmd.extend(["--useEquityCache", "--equityCacheDir", args.equityCacheDir])
                    run_cmd(cmd, args.dry_run)

    print(f"Done. Regenerated {missing}/{total} files (use --force to override skips).")


if __name__ == "__main__":
    main()
