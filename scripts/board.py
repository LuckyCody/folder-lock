"""Open-work board (PROTOCOL.md §6) - one screen of everything in flight.

  python scripts/board.py           # human view
  python scripts/board.py json      # machine view

Per workfolder: the lock (window, status — `closing` = signing off, not stale — age,
fresh/stale), the pointer's `Next concrete action:` classified by the grammar
(actionable / tripwire / parked / closed / mute), staged handoffs in `.goal/inbox/`.
Plus the deploy queue (PROTOCOL §11): pending requests, last deploy, FAILED / BLOCKED per unit.
The file is written to stdout only — nothing is ever auto-opened. json shape (v3):
{"folders": [...], "deploys": [...]}.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lib"))
import lockpath as lp  # noqa: E402

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".next", "dist", "build"}


def fmt_age(td) -> str:
    h, rem = divmod(int(td.total_seconds()), 3600)
    return f"{h}h{rem // 60:02d}m"


def classify(action: str):
    a = action.strip(); low = a.lower()
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
    m = re.search(r"^next concrete action:\s*(.*)$", ptr.read_text(encoding="utf-8", errors="replace"), re.I | re.M)
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
            locks = lp.locks_at(p)
            if locks:
                it["locks"] = [{"kind": li.kind, "window": li.window, "status": li.status, "task": li.task,
                                "age": fmt_age(li.age), "fresh": li.fresh} for li in locks]
            inbox = p / "inbox"
            if inbox.is_dir():
                it["handoffs"] = [f.name for f in sorted(inbox.glob("*.staged.md"))]
            dirnames[:] = []
        elif p.name == "workflow-state" and "current-pointer.md" in filenames:
            rel = p.parent.relative_to(root).as_posix() or "."
            items.setdefault(rel, {"folder": rel})["pointer"] = read_pointer(p.parent)
            dirnames[:] = []
    return sorted(items.values(), key=lambda x: x["folder"])


def deploy_queue() -> list:
    """PROTOCOL.md §11 — per deploy unit: pending requests, last deploy, FAILED / BLOCKED flags written by
    scripts/deployer.py. Read-only; the board never fires a deploy."""
    try:
        import deployunits as U
        units = U.load_units()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for u in units.values():
        reqs = sorted((U.REQUESTS / u.name).glob("*.yaml")) if (U.REQUESTS / u.name).is_dir() else []
        flags = {}
        for kind in ("last", "failed", "blocked"):
            p = U.STATE / kind / f"{u.name}.yaml"
            flags[kind] = lp.parse_lock_text(p.read_text(encoding="utf-8", errors="replace")) if p.is_file() else {}
        out.append({"unit": u.name, "pending": len(reqs),
                    "oldest_pending": fmt_age(datetime.now() - datetime.fromtimestamp(reqs[0].stat().st_mtime)) if reqs else "",
                    "last_deployed_at": flags["last"].get("deployed_at", ""),
                    "last_commit": (flags["last"].get("deployed_commit", "") or "")[:7],
                    "failed": bool(flags["failed"]), "failed_at": flags["failed"].get("failed_at", ""),
                    "fail_rc": flags["failed"].get("rc", ""), "retry_after": flags["failed"].get("retry_after", ""),
                    "fail_log": flags["failed"].get("log", ""),
                    "blocked": bool(flags["blocked"]), "blocked_reason": flags["blocked"].get("reason", ""),
                    "blocked_since": flags["blocked"].get("since", "")})
    return out


def main() -> int:
    items = scan(lp.ROOT)
    deploys = deploy_queue()
    if len(sys.argv) > 1 and sys.argv[1] == "json":
        print(json.dumps({"folders": items, "deploys": deploys}, indent=2, ensure_ascii=False))
        return 0
    if not items and not any(d["pending"] or d["failed"] or d["blocked"] for d in deploys):
        print("board: nothing in flight (no locks, pointers, handoffs, or deploy requests).")
        return 0
    print(f"# Open-work board - {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
    mute = []
    for it in items:
        ptr = it.get("pointer"); locks = it.get("locks", [])
        if ptr and ptr["kind"] == "closed" and not locks and not it.get("handoffs"):
            continue
        if ptr and ptr["kind"] == "mute":
            mute.append(it["folder"])
        tags = []
        for lk in locks:
            state = "signing off" if lk["status"] == "closing" else ("LOCKED" if lk["fresh"] else "stale lock")
            tags.append(f"[{state} {lk['window']} {lk['kind']} {lk['age']}]")
        if it.get("handoffs"):
            tags.append(f"[{len(it['handoffs'])} handoff(s) staged]")
        if ptr and ptr["kind"] == "tripwire":
            tags.append("[tripwire]")
        if ptr and ptr["kind"] == "parked":
            tags.append("[PARKED]")
        print((f"- **{it['folder']}** " + " ".join(tags)).rstrip())
        if ptr and ptr["kind"] not in ("mute", "closed"):
            print(f"    -> {ptr['action'][:160]}")
        elif ptr is None and locks:
            print(f"    -> (no current-pointer.md) lock task: {locks[0]['task']}")
    if mute:
        print(f"\n{len(mute)} pointer(s) with no 'Next concrete action:' line (mute - fix them): {', '.join(mute)}")
    live = [d for d in deploys if d["pending"] or d["failed"] or d["blocked"]]
    if live:
        print("\n## Deploy queue (PROTOCOL §11 — `python scripts/deployer.py status`; nobody deploys by hand)")
        for d in live:
            line = f"- **{d['unit']}** {d['pending']} pending" + (f" (oldest {d['oldest_pending']})" if d["pending"] else "")
            if d["last_deployed_at"]:
                line += f" · last {d['last_deployed_at']} @ {d['last_commit']}"
            print(line)
            if d["failed"]:
                print(f"    FAILED {d['failed_at']} rc={d['fail_rc']} · retry after {d['retry_after']} · log {d['fail_log']}")
            if d["blocked"]:
                print(f"    BLOCKED since {d['blocked_since']}: {d['blocked_reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
