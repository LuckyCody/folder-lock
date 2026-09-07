"""Open-work board (PROTOCOL.md section 6) - one screen of everything in flight.

  python scripts/board.py           # human view
  python scripts/board.py json      # machine view

Lists, per workfolder: the lock (holder, age, fresh/stale), the current
pointer's `Next concrete action:` line classified by the pointer grammar
(actionable / tripwire / parked / closed / mute), and staged handoffs waiting
in `.goal/inbox/`. A fresh window reads this, picks a folder, takes its lock,
and starts at the pointer. A dead window costs nothing.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lock import SKIP_DIRS, age, fmt_age, parse_lock, repo_root  # noqa: E402

FRESH_HOURS = 24


def classify(action: str):
    a = action.strip()
    low = a.lower()
    if not a:
        return "mute", ""
    if low.startswith("when "):
        return "tripwire", a
    if low.startswith("parked"):
        return "parked", a
    if low.startswith("none"):
        return "closed", a
    return "actionable", a


def read_pointer(folder: Path):
    ptr = folder / "workflow-state" / "current-pointer.md"
    if not ptr.is_file():
        return None
    text = ptr.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"^next concrete action:\s*(.*)$", text, re.I | re.M)
    kind, line = classify(m.group(1) if m else "")
    return {"kind": kind, "action": line, "age": fmt_age(datetime.now() - datetime.fromtimestamp(ptr.stat().st_mtime))}


def scan(root: Path) -> list:
    items: dict = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        p = Path(dirpath)
        if p.name == ".goal":
            folder = p.parent
            rel = folder.relative_to(root).as_posix() or "."
            it = items.setdefault(rel, {"folder": rel})
            lock = p / "LOCK.yaml"
            if lock.is_file():
                info = parse_lock(lock)
                a = age(lock, info)
                it["lock"] = {"window": info.get("window", "?"), "task": info.get("task", ""),
                              "age": fmt_age(a), "fresh": a < timedelta(hours=FRESH_HOURS)}
            inbox = p / "inbox"
            if inbox.is_dir():
                it["handoffs"] = [f.name for f in sorted(inbox.glob("*.staged.md"))]
            dirnames[:] = []
        elif p.name == "workflow-state" and "current-pointer.md" in filenames:
            folder = p.parent
            rel = folder.relative_to(root).as_posix() or "."
            items.setdefault(rel, {"folder": rel})["pointer"] = read_pointer(folder)
            dirnames[:] = []
    return sorted(items.values(), key=lambda x: x["folder"])


def main() -> int:
    root = repo_root()
    items = scan(root)
    if len(sys.argv) > 1 and sys.argv[1] == "json":
        print(json.dumps(items, indent=2, ensure_ascii=False))
        return 0
    if not items:
        print("board: nothing in flight (no locks, pointers, or handoffs).")
        return 0
    print(f"# Open-work board - {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
    mute = []
    for it in items:
        ptr = it.get("pointer")
        lock = it.get("lock")
        if ptr and ptr["kind"] == "closed" and not lock and not it.get("handoffs"):
            continue
        if ptr and ptr["kind"] == "mute":
            mute.append(it["folder"])
        tags = []
        if lock:
            tags.append(f"[LOCKED {lock['window']} {lock['age']}]" if lock["fresh"] else f"[stale lock {lock['window']} {lock['age']}]")
        if it.get("handoffs"):
            tags.append(f"[{len(it['handoffs'])} handoff(s) staged]")
        if ptr and ptr["kind"] == "tripwire":
            tags.append("[tripwire]")
        if ptr and ptr["kind"] == "parked":
            tags.append("[PARKED]")
        head = f"- **{it['folder']}** " + " ".join(tags)
        print(head.rstrip())
        if ptr and ptr["kind"] not in ("mute", "closed"):
            print(f"    -> {ptr['action'][:160]}")
        elif ptr is None and lock:
            print(f"    -> (no current-pointer.md) lock task: {lock['task']}")
    if mute:
        print(f"\n{len(mute)} pointer(s) with no 'Next concrete action:' line (mute - fix them): {', '.join(mute)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
