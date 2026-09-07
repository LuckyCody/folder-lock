"""Protected-branch commit guard (PROTOCOL.md section 4).

Refuses a commit while HEAD is on a protected branch (default: main, master).
Model-agnostic on purpose: a rule that lives only as text in an agent's
context window gets skipped the moment a task feels small. Git refusing is
not a suggestion.

Override for a deliberate solo-lane commit: MAIN_COMMIT_OK=1 git commit ...
Configure the branch list:  git config folderlock.protected "main,master,release"
Detached HEAD (rebases, worktree setup) is allowed. Internal errors fail OPEN
with a warning - loudly broken beats silently permissive.

Exit 0 = allow, 1 = block.
"""
from __future__ import annotations

import os
import subprocess
import sys

DEFAULT_PROTECTED = ("main", "master")


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace").stdout.strip()


def main() -> int:
    if os.environ.get("MAIN_COMMIT_OK") == "1":
        print("[folder-lock] MAIN_COMMIT_OK=1 - direct commit to protected branch allowed (solo lane).")
        return 0
    branch = git("symbolic-ref", "--quiet", "--short", "HEAD")
    if not branch:  # detached HEAD
        return 0
    configured = git("config", "--get", "folderlock.protected")
    protected = tuple(b.strip() for b in configured.split(",") if b.strip()) if configured else DEFAULT_PROTECTED
    if branch not in protected:
        return 0
    print(f"[folder-lock] COMMIT BLOCKED - you are on protected branch '{branch}'.\n")
    print("  Work happens in a worktree on a feature branch, never directly on main:")
    print("      git worktree add ../wt-<stream> -b feat/<stream>")
    print("  Already have changes here? Move them without losing anything:")
    print("      git stash && git worktree add ../wt-<stream> -b feat/<stream> && cd ../wt-<stream> && git stash pop")
    print("  Deliberate small solo commit (docs, run-state)? Say so explicitly:")
    print("      MAIN_COMMIT_OK=1 git commit ...")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"[folder-lock] WARNING: protect_main errored ({e!r}) - allowing commit.")
        sys.exit(0)
