"""Commit guard — pre-commit, model-agnostic (PROTOCOL.md §5 + §10). Fails CLOSED.

Refuses the commit when:
  a staged path lies under another window's fresh LOCK.yaml / .firing.lock   (the original rule)
  a staged path is in a guarded folder with no fresh lock of yours              "claim first"
  a staged path is in an unguarded folder (no .goal/)                          "claim it (creates .goal/)"
  identity unknown / identity conflict / registry unreadable
  the hook is not actually wired (git's hook dir is not this directory)        the silently-disabled case
  any internal error
WARNS (never refuses) when a staged path lies inside a deploy unit (.folder-lock/deploy-units.yaml) and this
session has no fresh PASS marker from `scripts/test_unit.py <unit>` (PROTOCOL §11, best effort).

Identity: ICM_WINDOW env, or the session binding via CLAUDE_CODE_SESSION_ID (Claude Code sets it
in the Bash tool env) -> <STATE_ROOT>/sessions/<sid>.yaml (lockpath.STATE_ROOT, §14). Override: ICM_LOCK_BYPASS=1 (say so).

  python .githooks/check_locks.py --verify-wiring
  python .githooks/check_locks.py --self-test [--if-changed]     records <STATE_ROOT>/selftest_last.json (7 cases incl. the §14 store round-trip)
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIB = next((c for c in (HERE / "lib", HERE.parent / "lib") if (c / "lockpath.py").is_file()), HERE / "lib")
sys.path.insert(0, str(LIB))
import lockpath as lp  # noqa: E402


def _git(*args: str, cwd=None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace").stdout.strip()


def staged_paths(repo: Path) -> list:
    out = subprocess.run(["git", "diff", "--cached", "--name-only", "-z"], cwd=repo, capture_output=True, text=True,
                         encoding="utf-8", errors="replace").stdout
    return [p for p in out.split("\0") if p]


def verify_wiring(repo: Path) -> str:
    hooks_dir = _git("rev-parse", "--git-path", "hooks", cwd=repo)
    if not hooks_dir:
        return "not a git repo?"
    hd = Path(hooks_dir)
    if not hd.is_absolute():
        hd = repo / hd
    try:
        hd = hd.resolve()
    except OSError:
        pass
    if hd != HERE.resolve():
        return f"git's hook dir is {hd} but this guard lives in {HERE} — the pre-commit hook is NOT running. Fix: git config core.hooksPath \"{HERE}\""
    if not (hd / "pre-commit").is_file():
        return f"{hd}/pre-commit is missing"
    return ""


def fingerprint(repo: Path) -> str:
    h = hashlib.sha256()
    for p in (HERE / "check_locks.py", HERE / "pre-commit", LIB / "lockpath.py", LIB / "statestore.py", LIB / "conflicts.py", lp.REGISTRY):
        try:
            h.update(p.read_bytes())
        except OSError:
            h.update(b"MISSING:" + str(p).encode())
    h.update(_git("config", "--get", "core.hooksPath", cwd=repo).encode())
    return h.hexdigest()[:16]


def deploy_unit_warnings(staged: list, me) -> list:
    """PROTOCOL.md §11 — best effort, WARN only, never refuses.

    A commit that touches a deploy unit (.folder-lock/deploy-units.yaml `paths`) should have that unit's
    tests run in THIS session: `python scripts/test_unit.py <unit>` leaves a per-window marker under
    .goal/deploy/state/tests/. Missing / failed / stale (>24h) marker -> one warning per unit. No units
    file, or any internal error -> no warnings (advisory; the lock rules above stay the gate)."""
    if not staged or me is None:
        return []
    try:
        import deployunits as U  # same lib dir as lockpath
        units = U.load_units()
        if not units:
            return []
        touched = U.units_for_paths(staged, units)
    except Exception as e:  # noqa: BLE001 — advisory check must never break the guard
        return [f"deploy-unit test check skipped ({e!r})"]
    warns = []
    for name, paths in sorted(touched.items()):
        if not units[name].tests:
            continue
        m = U.read_test_marker(name, me.window)
        cmd = f"python scripts/test_unit.py {name}"
        if not m:
            warns.append(f"deploy unit '{name}' touched ({len(paths)} file(s)) but its tests have not run in this "
                         f"session (window {me.window}) — run: {cmd}")
        elif m.get("result") != "PASS":
            warns.append(f"deploy unit '{name}': the last test run in this session FAILED at {m.get('ran_at')} — fix it, then: {cmd}")
        elif not m.get("fresh"):
            warns.append(f"deploy unit '{name}': test marker is older than 24h ({m.get('ran_at')}) — re-run: {cmd}")
    return warns


def guard(repo: Path) -> int:
    if os.environ.get("ICM_LOCK_BYPASS") == "1":
        print("[lock-guard] ICM_LOCK_BYPASS=1 - guard skipped (owner-approved only; say so in the message).")
        lp.guard_log({"guard": "check_locks", "decision": "bypass"})
        return 0
    wiring = verify_wiring(repo)
    if wiring:
        print(f"[lock-guard] REFUSED — hook wiring broken: {wiring}")
        return 1
    try:
        me = lp.identity()
    except lp.IdentityConflict as e:
        print(f"[lock-guard] REFUSED — {e}")
        return 1
    if me is None:
        print(f"[lock-guard] REFUSED — {lp.NO_IDENTITY_HELP}")
        lp.guard_log({"guard": "check_locks", "decision": "refuse", "reason": "no identity"})
        return 1
    try:
        flows = lp.load_registry()
    except lp.RegistryUnreadable as e:
        print(f"[lock-guard] REFUSED — registry unreadable: {e}")
        return 1
    problems: dict = {}
    cache: dict = {}
    for rel in staged_paths(repo):
        res = lp.resolve(rel, flows)
        if res.kind == "unguarded":
            problems.setdefault(f"UNGUARDED {res.folder}", []).append(rel)
            continue
        if lp.is_whitelisted(rel, res, me):
            continue
        key = str(res.lock_dir)
        if key not in cache:
            cache[key] = lp.locks_at(res.lock_dir)
        locks = cache[key]
        mine = [li for li in locks if lp.same_window(li.window, me.window) and li.fresh]
        foreign = [li for li in locks if not lp.same_window(li.window, me.window) and (li.fresh or li.malformed)]
        lit = lp.literal_lock(rel, res)
        if lit is not None:  # v4.1: a fresh LOCK.yaml at the folder itself (registry moved the folder after the claim)
            if lp.same_window(lit.window, me.window):
                continue
            lit_folder = lit.path.parent.parent.relative_to(lp.ROOT).as_posix()
            problems.setdefault(f"FOREIGN {lit_folder} (literal lock): {lit.describe()}", []).append(rel)
            continue
        if foreign:
            problems.setdefault(f"FOREIGN {res.folder or '<root>'}: {foreign[0].describe()}", []).append(rel)
        elif not mine:
            problems.setdefault(f"UNCLAIMED {res.folder or '<root>'}: no fresh lock of yours ({me.window}) — claim first", []).append(rel)
    if not problems:
        warns = deploy_unit_warnings(staged_paths(repo), me)
        for w in warns:
            print(f"[lock-guard] WARN (PROTOCOL §11, not blocking): {w}")
        if warns:
            print("[lock-guard] HEAD must stay deployable — anyone's finish deploys everyone's committed work.")
        lp.guard_log({"guard": "check_locks", "decision": "allow", "window": me.window, **({"warn": warns} if warns else {})})
        return 0
    print(f"[lock-guard] COMMIT BLOCKED (identity {me.window} via {me.source}):\n")
    for head, paths in problems.items():
        print(f"  {head}")
        for p in paths[:20]:
            print(f"    - {p}")
    print("\nRemedies:")
    print("  * FOREIGN   -> not your paths: git restore --staged <path>  (never sweep another stream's work)")
    print("  * UNCLAIMED -> python scripts/lock.py claim <folder> --task \"...\"  then commit")
    print("  * UNGUARDED -> the same claim creates the .goal/")
    print("  * owner-approved override only: ICM_LOCK_BYPASS=1 git commit ... (say so in the message)")
    lp.guard_log({"guard": "check_locks", "decision": "refuse", "window": me.window, "problems": {k: v[:5] for k, v in problems.items()}})
    return 1


def self_test(repo: Path, if_changed: bool) -> int:
    import shutil
    import tempfile
    rec = lp.STATE_ROOT / "selftest_last.json"   # §14: host-local, never in the tree
    fp = fingerprint(repo)
    if if_changed and rec.is_file():
        try:
            last = json.loads(rec.read_text(encoding="utf-8"))
            if last.get("fingerprint") == fp and last.get("result") == "PASS":
                print(f"[lock-guard] self-test: unchanged since {last.get('ts')} (fingerprint {fp}) — skipping")
                return 0
        except (OSError, json.JSONDecodeError):
            pass
    results = []

    def case(name, ok, detail=""):
        results.append((name, ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"\n        {detail[:300]}" if detail and not ok else ""))

    wiring = verify_wiring(repo)
    case("hook wiring: git's hook dir is this directory", not wiring, wiring)
    tmp = Path(tempfile.mkdtemp(prefix="folderlock-selftest-"))
    try:
        fx = tmp / "repo"
        (fx / ".githooks" / "lib").mkdir(parents=True)
        (fx / ".folder-lock").mkdir()
        for f in HERE.iterdir():
            if f.is_file():
                shutil.copy2(f, fx / ".githooks" / f.name)
        for f in LIB.glob("*.py"):
            shutil.copy2(f, fx / ".githooks" / "lib" / f.name)
        (fx / ".folder-lock" / "registry.yaml").write_text("workflows:\n- id: fixture-flow\n  owns:\n  - fixture/**\n", encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k not in ("ICM_WINDOW", "ICM_LOCK_BYPASS", "CLAUDE_CODE_SESSION_ID", "MAIN_COMMIT_OK")}
        env["FOLDER_LOCK_ROOT"] = str(fx)
        env["FOLDER_LOCK_STATE_ROOT"] = str(tmp / "state")   # never the live state root

        def run(*args, **kw):
            e = dict(env); e.update(kw.pop("env", {}))
            return subprocess.run(list(args), cwd=fx, env=e, capture_output=True, text=True, encoding="utf-8", errors="replace")

        run("git", "init", "-q", "-b", "main"); run("git", "config", "user.email", "t@x.invalid"); run("git", "config", "user.name", "t")
        run("git", "config", "commit.gpgsign", "false"); run("git", "config", "core.hooksPath", str(fx / ".githooks"))
        run("git", "checkout", "-q", "-b", "feat/x")
        (fx / "fixture" / ".goal").mkdir(parents=True)
        (fx / "fixture" / ".goal" / "LOCK.yaml").write_text(
            f'holder: interactive\nwindow: "window-a"\nstatus: open\ntask: "fixture"\nstarted: "{datetime.now().strftime(lp.TS_FMT)}"\n', encoding="utf-8")
        (fx / "fixture" / "f.txt").write_text("1\n"); run("git", "add", "fixture/f.txt")
        r = run("git", "commit", "-q", "-m", "as window-b", env={"ICM_WINDOW": "window-b"}); o = r.stdout + r.stderr
        case("staged path under a synthetic FOREIGN lock is refused", r.returncode != 0 and "FOREIGN" in o, o)
        r = run("git", "commit", "-q", "-m", "no identity"); o = r.stdout + r.stderr
        case("no identity is refused", r.returncode != 0 and "no window identity" in o, o)
        r = run("git", "commit", "-q", "-m", "as holder", env={"ICM_WINDOW": "Window-A"}); o = r.stdout + r.stderr
        case("holder (case-insensitive) passes", r.returncode == 0, o)
        (fx / "loose").mkdir(); (fx / "loose" / "x.txt").write_text("x\n"); run("git", "add", "loose/x.txt")
        r = run("git", "commit", "-q", "-m", "unguarded", env={"ICM_WINDOW": "window-a"}); o = r.stdout + r.stderr
        case("unguarded folder (no .goal/) is refused", r.returncode != 0 and "UNGUARDED" in o, o)
        run("git", "config", "core.hooksPath", str(tmp / "elsewhere"))
        r = run(sys.executable, str(fx / ".githooks" / "check_locks.py"), "--verify-wiring"); o = r.stdout + r.stderr
        case("broken hooksPath is detected loudly by --verify-wiring", r.returncode != 0 and "NOT running" in o, o)
        # PROTOCOL §14 store round-trip: writer 1 saves; writer 2 saves with a stale (create-only) etag -> merge, not overwrite
        probe = ("import sys, json; sys.path.insert(0, sys.argv[1]); import statestore as s\n"
                 "d = s.load('items', {'items': {}}); d['items']['x'] = {'title': 'x', 'updated': '2026-01-01T00:00'}; ok1 = s.save('items', d)\n"
                 "s._base.clear(); stale = {'items': {'y': {'title': 'y', 'updated': '2026-01-01T00:01'}}}; ok2 = s.save('items', stale)\n"
                 "back = s.load('items', {'items': {}})['items']; print(json.dumps({'ok1': ok1, 'ok2': ok2, 'keys': sorted(back)}))\n")
        r = run(sys.executable, "-c", probe, str(fx / ".githooks" / "lib")); o = r.stdout + r.stderr
        try:
            j = json.loads((r.stdout or "").strip().splitlines()[-1])
        except Exception:
            j = {}
        case("state store round-trip: stale-etag writer merges, both records present (§14)",
             j.get("ok1") is True and j.get("ok2") is True and j.get("keys") == ["x", "y"], o)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    ok = all(r[1] for r in results)
    rec.parent.mkdir(parents=True, exist_ok=True)
    rec.write_text(json.dumps({"ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), "fingerprint": fp,
                               "result": "PASS" if ok else "FAIL", "cases": [{"name": n, "ok": o} for n, o in results]}, indent=1), encoding="utf-8")
    print(f"[lock-guard] self-test {'PASS' if ok else 'FAIL'}: {sum(1 for r in results if r[1])}/{len(results)} (fingerprint {fp})")
    return 0 if ok else 1


def main(argv: list) -> int:
    repo = Path(_git("rev-parse", "--show-toplevel") or lp.ROOT)
    if "--verify-wiring" in argv:
        w = verify_wiring(repo)
        print("[lock-guard] wiring OK: git will run this pre-commit" if not w else f"[lock-guard] WIRING BROKEN — hook NOT running: {w}")
        return 0 if not w else 1
    if "--self-test" in argv:
        return self_test(repo, "--if-changed" in argv)
    return guard(repo)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception as e:
        print(f"[lock-guard] REFUSED — guard error {e!r}. A guard that cannot evaluate fails closed; fix it, or ICM_LOCK_BYPASS=1 with the owner's approval.")
        sys.exit(1)
