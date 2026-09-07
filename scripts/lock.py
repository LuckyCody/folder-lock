"""Folder lock - take / check / release / status (PROTOCOL.md section 1).

One mutual-exclusion domain per workfolder, carried by `<folder>/.goal/LOCK.yaml`.
Interactive locks go stale after 24 h. Auto-fired agents use a sibling
`.goal/.firing.lock` (stale after 15 min); the two exclude each other.

  python scripts/lock.py take <folder> --window <name> --task "<one line>" [--stream S] [--branch B]
  python scripts/lock.py check <folder>
  python scripts/lock.py release <folder> --window <name>
  python scripts/lock.py status            # every lock in the repo, freshness-tagged

Exit codes for `take`/`check`: 0 free-or-yours, 1 fresh foreign lock (STOP),
2 stale lock (ask the owner, never silently proceed), 3 agent firing lock.
"""
from __future__ import annotations

import argparse
import getpass
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

FRESH_HOURS = 24
FIRING_FRESH_MIN = 15
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".next", "dist", "build"}


def repo_root() -> Path:
    out = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                         text=True, encoding="utf-8", errors="replace").stdout.strip()
    return Path(out) if out else Path.cwd()


def parse_lock(path: Path) -> dict:
    info: dict = {}
    try:
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r'^(\w+):\s*"?([^"#\n]*?)"?\s*(#.*)?$', raw)
            if m:
                info[m.group(1)] = m.group(2).strip()
    except OSError:
        pass
    return info


def age(path: Path, info: dict) -> timedelta:
    started = info.get("started")
    if started:
        try:
            return datetime.now() - datetime.strptime(started, "%Y-%m-%dT%H:%M")
        except ValueError:
            pass
    return datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)


def fmt_age(td: timedelta) -> str:
    h, rem = divmod(int(td.total_seconds()), 3600)
    return f"{h}h{rem // 60:02d}m"


def inspect(folder: Path, me: str = "") -> int:
    """Print the folder's lock state; return the exit code documented above."""
    lock = folder / ".goal" / "LOCK.yaml"
    firing = folder / ".goal" / ".firing.lock"
    code = 0
    if firing.is_file():
        fage = datetime.now() - datetime.fromtimestamp(firing.stat().st_mtime)
        if fage < timedelta(minutes=FIRING_FRESH_MIN):
            print(f"AGENT HOLDS {folder}: .firing.lock is {fmt_age(fage)} old (fresh < {FIRING_FRESH_MIN}m). Wait or stage a handoff.")
            code = 3
        else:
            print(f"note: stale .firing.lock ({fmt_age(fage)}) in {folder} - an agent run died or forgot to clean up.")
    if lock.is_file():
        info = parse_lock(lock)
        a = age(lock, info)
        holder = info.get("window", "<unparseable>")
        line = (f"window={holder!r} task={info.get('task', '?')!r} stream={info.get('stream', '?')!r} "
                f"branch={info.get('branch', '?')!r} started={info.get('started', '?')} age={fmt_age(a)}")
        if me and holder.lower() == me.lower():
            print(f"YOURS {folder}: {line}")
        elif a < timedelta(hours=FRESH_HOURS):
            print(f"LOCKED {folder}: {line}\n  -> STOP. Another session holds this folder. Stage a handoff instead of editing here.")
            code = max(code, 1)
        else:
            print(f"STALE {folder}: {line}\n  -> Ask the owner before taking over. Never silently proceed over a stale lock.")
            code = max(code, 2)
    elif code == 0:
        print(f"FREE {folder}")
    return code


def cmd_take(a: argparse.Namespace) -> int:
    folder = (repo_root() / a.folder).resolve()
    if not folder.is_dir():
        print(f"ERROR: not a folder: {folder}", file=sys.stderr)
        return 4
    state = inspect(folder, a.window)
    if state == 1 or state == 3:
        return state
    if state == 2 and not a.force_stale:
        print("  (re-run with --force-stale once the owner has agreed)")
        return 2
    goal = folder / ".goal"
    goal.mkdir(exist_ok=True)
    branch = a.branch or subprocess.run(["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
                                        capture_output=True, text=True).stdout.strip() or "detached"
    (goal / "LOCK.yaml").write_text(
        f'holder: {a.holder}\n'
        f'window: "{a.window}"\n'
        f'task: "{a.task}"\n'
        f'stream: "{a.stream}"\n'
        f'branch: "{branch}"\n'
        f'started: "{datetime.now().strftime("%Y-%m-%dT%H:%M")}"\n'
        f'user: "{getpass.getuser()}"\n',
        encoding="utf-8",
    )
    print(f"TAKEN {folder.relative_to(repo_root()).as_posix()} as window={a.window!r}. "
          f"Commit with: ICM_WINDOW={a.window} git commit ...")
    return 0


def cmd_check(a: argparse.Namespace) -> int:
    return inspect((repo_root() / a.folder).resolve(), os.environ.get("ICM_WINDOW", ""))


def cmd_release(a: argparse.Namespace) -> int:
    folder = (repo_root() / a.folder).resolve()
    lock = folder / ".goal" / "LOCK.yaml"
    if not lock.is_file():
        print(f"nothing to release in {folder}")
        return 0
    holder = parse_lock(lock).get("window", "")
    if holder.lower() != a.window.lower() and not a.force:
        print(f"REFUSED: lock held by window={holder!r}, you are {a.window!r}. --force to override (say why).")
        return 1
    lock.unlink()
    print(f"RELEASED {folder.relative_to(repo_root()).as_posix()}")
    return 0


def cmd_status(a: argparse.Namespace) -> int:
    root = repo_root()
    me = os.environ.get("ICM_WINDOW", "")
    found = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        if Path(dirpath).name == ".goal" and ("LOCK.yaml" in filenames or ".firing.lock" in filenames):
            inspect(Path(dirpath).parent, me)
            found += 1
            dirnames[:] = []
    if not found:
        print("no locks anywhere in the repo")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("take")
    t.add_argument("folder")
    t.add_argument("--window", required=True)
    t.add_argument("--task", required=True)
    t.add_argument("--stream", default="")
    t.add_argument("--branch", default="")
    t.add_argument("--holder", default="interactive", choices=["interactive", "fired"])
    t.add_argument("--force-stale", action="store_true")
    t.set_defaults(fn=cmd_take)
    c = sub.add_parser("check")
    c.add_argument("folder")
    c.set_defaults(fn=cmd_check)
    r = sub.add_parser("release")
    r.add_argument("folder")
    r.add_argument("--window", required=True)
    r.add_argument("--force", action="store_true")
    r.set_defaults(fn=cmd_release)
    s = sub.add_parser("status")
    s.set_defaults(fn=cmd_status)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
