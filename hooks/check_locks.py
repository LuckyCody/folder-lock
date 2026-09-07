"""Lock-aware commit guard (PROTOCOL.md section 1 + 5).

Blocks a commit when any STAGED path lies inside a workfolder whose
`.goal/LOCK.yaml` is FRESH (< 24 h) and held by a different session.
This is the teeth behind "never `git add -A` across streams": a parallel
session's commit sweep must not pick up another session's in-flight edits.

Identity: the committing session passes its window name via ICM_WINDOW
(`ICM_WINDOW=<window> git commit ...`). A lock whose `window:` matches
(case-insensitive) is yours - allowed.

Resolution: nearest `.goal/LOCK.yaml` walking UP from each staged path.
Top-level files (no directory) fall under the repo-root `.goal/LOCK.yaml`
if one exists. Paths with no lock anywhere up the chain are unguarded.

Escape hatches: ICM_LOCK_BYPASS=1 skips the guard (owner-approved only -
say so in the commit message). Internal errors fail OPEN with a warning;
a malformed lock fails CLOSED for the paths it guards (unknown holder =
foreign).

Exit 0 = allow, 1 = block.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

FRESH_HOURS = 24
LOCK_REL = Path(".goal") / "LOCK.yaml"


def staged_paths(repo: Path) -> list:
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "-z"],
        cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).stdout
    return [p for p in out.split("\0") if p]


def read_lock(lock: Path) -> dict:
    info: dict = {}
    try:
        text = lock.read_text(encoding="utf-8", errors="replace")
        m = re.search(r'^window:\s*"?([^"#\n]+?)"?\s*(#.*)?$', text, re.MULTILINE)
        if m:
            info["window"] = m.group(1).strip()
        m = re.search(r'^started:\s*"?(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})', text, re.MULTILINE)
        if m:
            info["started"] = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M")
    except OSError:
        pass
    return info


def is_fresh(lock: Path, info: dict) -> bool:
    now = datetime.now()
    started = info.get("started")
    if started is not None:
        return now - started < timedelta(hours=FRESH_HOURS)
    try:
        return now - datetime.fromtimestamp(lock.stat().st_mtime) < timedelta(hours=FRESH_HOURS)
    except OSError:
        return False


def guarding_lock(repo: Path, rel_path: str, cache: dict):
    parts = Path(rel_path).parts
    if len(parts) == 1:
        root_lock = repo / LOCK_REL
        return root_lock if root_lock.is_file() else None
    for i in range(len(parts) - 1, 0, -1):
        folder = Path(*parts[:i])
        if folder not in cache:
            lock = repo / folder / LOCK_REL
            cache[folder] = lock if lock.is_file() else None
        if cache[folder]:
            return cache[folder]
    return None


def main() -> int:
    if os.environ.get("ICM_LOCK_BYPASS") == "1":
        print("[folder-lock] ICM_LOCK_BYPASS=1 - lock guard skipped (owner-approved only).")
        return 0
    repo = Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                               text=True, encoding="utf-8", errors="replace").stdout.strip())
    me = os.environ.get("ICM_WINDOW", "").strip().lower()

    cache: dict = {}
    lock_info: dict = {}
    violations: dict = {}
    for rel in staged_paths(repo):
        lock = guarding_lock(repo, rel, cache)
        if lock is None:
            continue
        if lock not in lock_info:
            lock_info[lock] = read_lock(lock)
        info = lock_info[lock]
        if not is_fresh(lock, info):
            continue
        holder = info.get("window", "<unparseable lock>")
        if me and holder.strip().lower() == me:
            continue
        violations.setdefault(lock, []).append(rel)

    if not violations:
        return 0

    print("[folder-lock] COMMIT BLOCKED - staged paths lie under another session's fresh folder lock:\n")
    for lock, paths in violations.items():
        info = lock_info[lock]
        print(f"  {lock.relative_to(repo).as_posix()}")
        print(f"    holder window: {info.get('window', '?')}   started: {info.get('started', '?')}")
        for p in paths[:20]:
            print(f"    - {p}")
        if len(paths) > 20:
            print(f"    ... and {len(paths) - 20} more")
    print("\nRemedies:")
    print("  * Not your paths -> unstage them: git restore --staged <path>...")
    print("    (never sweep another stream's in-flight work into your commit)")
    print("  * It IS your lock -> commit as yourself: ICM_WINDOW=<your window> git commit ...")
    print("  * Owner-approved override -> ICM_LOCK_BYPASS=1 git commit ... (say so in the message)")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # infrastructure failure: fail OPEN, never brick commits
        print(f"[folder-lock] WARNING: lock guard errored ({e!r}) - allowing commit.")
        sys.exit(0)
