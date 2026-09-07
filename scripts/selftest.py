"""Prove the guard holds by trying to break it.

  python scripts/selftest.py [<hooks dir>]     # default: ../hooks (the repo copy)

Builds a throwaway git repo in a temp dir, points core.hooksPath at the hooks
under test, then attempts the exact violations the guard exists to stop:

  1. commit on main                                  -> must be BLOCKED
  2. commit on main with MAIN_COMMIT_OK=1            -> must PASS
  3. commit on feat/x                                -> must PASS
  4. commit a path under another window's fresh lock -> must be BLOCKED
  5. same commit with ICM_WINDOW=<that window>       -> must PASS
  6. same commit under a STALE (25 h) lock           -> must PASS (stale = not enforced)
  7. commit an unrelated path while a lock exists    -> must PASS
  8. hooksPath sanity: the hook file exists, is executable, and is the one git will run

A hook that "exists" is not a hook that runs. Exit 0 only if every case behaves.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_HOOKS = HERE.parent / "hooks"


def run(cwd: Path, *args: str, env: dict = None) -> subprocess.CompletedProcess:
    e = os.environ.copy()
    e.pop("ICM_WINDOW", None)
    e.pop("ICM_LOCK_BYPASS", None)
    e.pop("MAIN_COMMIT_OK", None)
    if env:
        e.update(env)
    return subprocess.run(list(args), cwd=cwd, env=e, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def commit(repo: Path, msg: str, env: dict = None) -> subprocess.CompletedProcess:
    return run(repo, "git", "commit", "-q", "-m", msg, env=env)


def main() -> int:
    hooks = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_HOOKS
    if not (hooks / "pre-commit").is_file():
        print(f"FAIL: {hooks}/pre-commit missing")
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="folderlock-selftest-"))
    repo = tmp / "repo"
    repo.mkdir()
    try:
        run(repo, "git", "init", "-q", "-b", "main")
        run(repo, "git", "config", "user.email", "selftest@example.invalid")
        run(repo, "git", "config", "user.name", "selftest")
        run(repo, "git", "config", "commit.gpgsign", "false")
        hp = repo / ".githooks"
        shutil.copytree(hooks, hp)
        for f in hp.iterdir():
            f.chmod(f.stat().st_mode | stat.S_IXUSR)
        run(repo, "git", "config", "core.hooksPath", ".githooks")

        results = []

        def case(name: str, ok: bool, detail: str = ""):
            results.append(ok)
            print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"\n        {detail.strip()[:300]}" if detail and not ok else ""))

        # 8 first: is the hook git will run actually ours and executable?
        eff = run(repo, "git", "config", "--get", "core.hooksPath").stdout.strip()
        exe = os.access(hp / "pre-commit", os.X_OK)
        case("hooksPath points at the hooks under test and pre-commit is executable",
             eff == ".githooks" and exe, f"hooksPath={eff!r} executable={exe}")

        # 1 + 2: protected branch
        (repo / "README.md").write_text("draft\n")
        run(repo, "git", "add", "README.md")
        r = commit(repo, "draft on main")
        case("direct commit to main is BLOCKED", r.returncode != 0 and "protected branch" in r.stdout + r.stderr, r.stdout + r.stderr)
        r = commit(repo, "solo-lane commit", env={"MAIN_COMMIT_OK": "1"})
        case("commit to main with MAIN_COMMIT_OK=1 PASSES", r.returncode == 0, r.stdout + r.stderr)

        # 3: feature branch
        run(repo, "git", "checkout", "-q", "-b", "feat/x")
        (repo / "a").mkdir()
        (repo / "a" / "f.txt").write_text("1\n")
        run(repo, "git", "add", "a/f.txt")
        r = commit(repo, "on feature branch")
        case("commit on feat/x PASSES", r.returncode == 0, r.stdout + r.stderr)

        # 4 + 5: fresh foreign lock
        goal = repo / "a" / ".goal"
        goal.mkdir()
        (goal / "LOCK.yaml").write_text(
            f'holder: interactive\nwindow: "other-window"\ntask: "t"\nstream: "s"\nbranch: "main"\n'
            f'started: "{datetime.now().strftime("%Y-%m-%dT%H:%M")}"\n')
        (repo / "a" / "f.txt").write_text("2\n")
        run(repo, "git", "add", "a/f.txt")
        r = commit(repo, "into locked folder")
        case("commit into another window's FRESH lock is BLOCKED", r.returncode != 0 and "fresh folder lock" in r.stdout + r.stderr, r.stdout + r.stderr)
        r = commit(repo, "as the holder", env={"ICM_WINDOW": "Other-Window"})
        case("same commit as the lock holder (ICM_WINDOW, case-insensitive) PASSES", r.returncode == 0, r.stdout + r.stderr)

        # 7: unrelated path while lock exists
        (repo / "b.txt").write_text("x\n")
        run(repo, "git", "add", "b.txt")
        r = commit(repo, "unrelated path")
        case("commit to an unlocked path while a lock exists elsewhere PASSES", r.returncode == 0, r.stdout + r.stderr)

        # 6: stale lock
        stale = (datetime.now() - timedelta(hours=25)).strftime("%Y-%m-%dT%H:%M")
        (goal / "LOCK.yaml").write_text(f'holder: interactive\nwindow: "other-window"\ntask: "t"\nstarted: "{stale}"\n')
        (repo / "a" / "f.txt").write_text("3\n")
        run(repo, "git", "add", "a/f.txt")
        r = commit(repo, "under stale lock")
        case("commit under a STALE (25h) lock PASSES (guard enforces fresh locks only)", r.returncode == 0, r.stdout + r.stderr)

        ok = all(results)
        print(f"\n{'ALL GOOD' if ok else 'GUARD DOES NOT HOLD'}: {sum(results)}/{len(results)} cases behaved.")
        return 0 if ok else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
