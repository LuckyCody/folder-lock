"""Install the folder-lock hooks into a repo and PROVE they hold.

  python scripts/install.py [<repo path>]      # default: current repo

What it does:
  1. copies hooks/* into <repo>/.githooks/
  2. sets `git config core.hooksPath .githooks`  (and tells you if it was pointing somewhere else -
     a stale hooksPath is exactly how a hook silently stops running)
  3. adds `**/.goal/` to .gitignore (locks are runtime state, never committed)
  4. runs selftest.py against the installed hooks - a hook you have not tried to break is
     a hook you are assuming works

Re-run this in EVERY clone and EVERY worktree: core.hooksPath is per-repo config, it does not
travel with the code.
"""
from __future__ import annotations

import shutil
import stat
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOOKS_SRC = HERE.parent / "hooks"


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def main() -> int:
    target = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
    top = git(target, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        print(f"ERROR: {target} is not inside a git repo", file=sys.stderr)
        return 2
    repo = Path(top.stdout.strip())

    dest = repo / ".githooks"
    dest.mkdir(exist_ok=True)
    for f in HOOKS_SRC.iterdir():
        if f.is_file():
            shutil.copy2(f, dest / f.name)
            (dest / f.name).chmod((dest / f.name).stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print(f"copied {len(list(HOOKS_SRC.iterdir()))} hook files -> {dest}")

    prev = git(repo, "config", "--get", "core.hooksPath").stdout.strip()
    if prev and prev not in (".githooks", str(dest)):
        print(f"NOTE: core.hooksPath was '{prev}' - a hook in .git/hooks/ or elsewhere was NOT running. Now: .githooks")
    elif not prev and (repo / ".git" / "hooks" / "pre-commit").is_file():
        print("NOTE: an existing .git/hooks/pre-commit will stop running once core.hooksPath is set. "
              "Merge it into .githooks/pre-commit if you still need it.")
    git(repo, "config", "core.hooksPath", ".githooks")
    print("set core.hooksPath = .githooks")

    gi = repo / ".gitignore"
    text = gi.read_text(encoding="utf-8") if gi.is_file() else ""
    if "**/.goal/" not in text.splitlines():
        with gi.open("a", encoding="utf-8") as fh:
            if text and not text.endswith("\n"):
                fh.write("\n")
            fh.write("**/.goal/\n")
        print("added **/.goal/ to .gitignore")

    print("\nself-test (the hook has to be broken on purpose before it counts as installed):", flush=True)
    rc = subprocess.run([sys.executable, str(HERE / "selftest.py"), str(dest)]).returncode
    if rc == 0:
        print("\nINSTALLED and PROVEN. Repeat `python scripts/install.py` in every clone/worktree.")
    else:
        print("\nINSTALL INCOMPLETE - the guard did not hold. Fix before relying on it.", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
