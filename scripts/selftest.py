"""Prove the guards hold by trying to break them.

  python scripts/selftest.py [<hooks dir>]     # default: ../hooks (with ../lib)

Runs `check_locks.py --self-test` (wiring, foreign lock, no identity, holder, unguarded,
broken hooksPath) in a throwaway repo, then the protected-branch cases:
  commit on main -> BLOCKED · MAIN_COMMIT_OK=1 -> PASS · commit on feat/x -> PASS.
Exit 0 only if everything behaves. A hook that "exists" is not a hook that runs.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent


def main() -> int:
    hooks = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else SKILL / "hooks"
    lib = hooks / "lib" if (hooks / "lib").is_dir() else SKILL / "lib"
    tmp = Path(tempfile.mkdtemp(prefix="folderlock-selftest-"))
    repo = tmp / "repo"
    (repo / ".githooks" / "lib").mkdir(parents=True)
    ok_all = True
    try:
        for f in hooks.iterdir():
            if f.is_file():
                shutil.copy2(f, repo / ".githooks" / f.name)
                (repo / ".githooks" / f.name).chmod((repo / ".githooks" / f.name).stat().st_mode | stat.S_IXUSR)
        for f in lib.glob("*.py"):
            shutil.copy2(f, repo / ".githooks" / "lib" / f.name)
        env = {k: v for k, v in os.environ.items() if k not in ("ICM_WINDOW", "ICM_LOCK_BYPASS", "CLAUDE_CODE_SESSION_ID", "MAIN_COMMIT_OK")}
        env["FOLDER_LOCK_ROOT"] = str(repo)

        def run(*args, **kw):
            e = dict(env); e.update(kw.pop("env", {}))
            return subprocess.run(list(args), cwd=repo, env=e, capture_output=True, text=True, encoding="utf-8", errors="replace")

        run("git", "init", "-q", "-b", "main"); run("git", "config", "user.email", "t@x.invalid"); run("git", "config", "user.name", "t")
        run("git", "config", "commit.gpgsign", "false"); run("git", "config", "core.hooksPath", ".githooks")
        print("== commit guard self-test")
        r = subprocess.run([sys.executable, str(repo / ".githooks" / "check_locks.py"), "--self-test"], cwd=repo, env=env)
        ok_all &= r.returncode == 0

        print("== protected branch")
        (repo / ".goal").mkdir(exist_ok=True)
        (repo / ".goal" / "LOCK.yaml").write_text(f'holder: interactive\nwindow: "me"\nstatus: open\ntask: "t"\nstarted: "{datetime.now().strftime("%Y-%m-%dT%H:%M")}"\n')
        (repo / "README.md").write_text("draft\n"); run("git", "add", "README.md")
        r = run("git", "commit", "-q", "-m", "on main", env={"ICM_WINDOW": "me"}); o = r.stdout + r.stderr
        c1 = r.returncode != 0 and "protected branch" in o
        print(f"  {'PASS' if c1 else 'FAIL'}  direct commit to main is BLOCKED"); ok_all &= c1
        r = run("git", "commit", "-q", "-m", "solo", env={"ICM_WINDOW": "me", "MAIN_COMMIT_OK": "1"})
        c2 = r.returncode == 0
        print(f"  {'PASS' if c2 else 'FAIL'}  commit to main with MAIN_COMMIT_OK=1 PASSES"); ok_all &= c2
        run("git", "checkout", "-q", "-b", "feat/x"); (repo / "README.md").write_text("more\n"); run("git", "add", "README.md")
        r = run("git", "commit", "-q", "-m", "feat", env={"ICM_WINDOW": "me"})
        c3 = r.returncode == 0
        print(f"  {'PASS' if c3 else 'FAIL'}  commit on feat/x PASSES" + ("" if c3 else f"\n        {(r.stdout + r.stderr)[:300]}")); ok_all &= c3
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\nALL GOOD" if ok_all else "\nGUARD DOES NOT HOLD")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
