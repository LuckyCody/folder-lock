"""Run a deploy unit's tests and leave the marker the commit guard looks for (PROTOCOL.md §11).

    python scripts/test_unit.py <unit> [<unit> ...]
    python scripts/test_unit.py --all
    python scripts/test_unit.py --for <repo-rel path or folder>

Runs the unit's `tests:` command from the repo root, then writes
<repo>/.goal/deploy/state/tests/<unit>--<window>.json {result PASS|FAIL, commit, ran_at, rc}. The pre-commit
guard WARNS — never refuses — when a commit touches a unit and this session has no fresh PASS marker for
it. Best effort: the marker proves the tests ran in this session, not that they covered the exact staged bytes.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lib"))
import deployunits as U  # noqa: E402
import lockpath as lp  # noqa: E402


def run_unit(unit: U.Unit, window: str) -> int:
    if not unit.tests:
        print(f"{unit.name}: no tests configured — PASS marker written (nothing to run)")
        U.write_test_marker(unit.name, window, "PASS", "", 0, "no tests configured")
        return 0
    print(f"{unit.name}: {unit.tests}")
    t0 = time.time()
    r = subprocess.run(unit.tests, shell=True, cwd=U.ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (r.stdout or "") + (r.stderr or "")
    sys.stdout.write(out if len(out) < 6000 else out[-6000:])
    result = "PASS" if r.returncode == 0 else "FAIL"
    p = U.write_test_marker(unit.name, window, result, unit.tests, r.returncode, out)
    print(f"{unit.name}: {result} in {time.time() - t0:.1f}s -> marker {p.relative_to(U.ROOT).as_posix()}")
    return r.returncode


def main(argv: list) -> int:
    units = U.load_units()
    try:
        me = lp.identity()
    except lp.IdentityConflict as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 5
    window = me.window if me else ""
    if not window:
        print("note: no window identity — marker written under 'no-window'; it will not satisfy the guard for a claimed session")
    if "--all" in argv:
        names = list(units)
    elif "--for" in argv:
        rel = argv[argv.index("--for") + 1] if argv.index("--for") + 1 < len(argv) else ""
        names = U.units_for_path(rel, units) or U.units_for_folder(rel, units)
        if not names:
            print(f"no deploy unit covers {rel!r}")
            return 0
    else:
        names = [a for a in argv if not a.startswith("--")]
    if not names:
        print(__doc__)
        return 2
    worst = 0
    for n in names:
        if n not in units:
            print(f"unknown unit {n!r}; known: {', '.join(units) or '(none)'}", file=sys.stderr)
            worst = max(worst, 2)
            continue
        worst = max(worst, run_unit(units[n], window))
    return worst


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
