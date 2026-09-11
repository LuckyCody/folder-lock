"""Autorun log — one line per item worked autonomously (PROTOCOL §13).

Each workfolder keeps `workflow-state/autorun-log.md`; the signoff appends one line per shift, the resumer
one line when a fire ends without a signoff. `board.py` / the owner's exit view show the digest since the
owner last looked.

    python lib/autorun_log.py append <folder> --item "<title>" --status <status> [--decisions "..."] [--commit <sha>] [--by <window>]
    python lib/autorun_log.py digest [--since <YYYY-MM-DDTHH:MM>] [--json]
    python lib/autorun_log.py mark-seen
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mint  # noqa: E402
import lockpath as lp  # noqa: E402

ROOT = lp.ROOT
SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__", ".next", "dist", "build", ".githooks"}
LINE = re.compile(r"^- (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}) · (.+?) · (.+?) · (\w+) · (\S+) · by (\S+) · decisions: (.*)$")


def append(folder: str, item: str, status: str, decisions: str = "", commit: str = "-", by: str = "") -> Path:
    p = ROOT / folder / "workflow-state" / "autorun-log.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    by = by or os.environ.get("ICM_WINDOW") or "interactive"
    if not p.exists():
        p.write_text(f"# Autorun log — {folder}\n\nOne line per item worked in this folder (PROTOCOL §13). Never hand-edit.\n\n", encoding="utf-8")
    with p.open("a", encoding="utf-8") as fh:
        fh.write(f"- {mint.timestamp()} · {folder} · {item.replace(' · ', ' - ')[:160]} · {status} · {(commit or '-')[:12]} · by {by} · "
                 f"decisions: {' '.join((decisions or '(none)').split())[:400]}\n")
    return p


def digest(since: str = "") -> list:
    out = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP]
        if Path(dirpath).name == "workflow-state" and "autorun-log.md" in filenames:
            for raw in (Path(dirpath) / "autorun-log.md").read_text(encoding="utf-8", errors="replace").splitlines():
                m = LINE.match(raw.strip())
                if m and (not since or m.group(1) > since):
                    ts, folder, item, status, commit, by, decisions = m.groups()
                    out.append({"ts": ts, "folder": folder, "item": item, "status": status, "commit": commit, "by": by, "decisions": decisions})
    out.sort(key=lambda e: e["ts"])
    return out


def last_seen() -> str:
    """The owner's digest window: state-store document `last_seen` (PROTOCOL §14) — was <repo>/.goal/autorun_last_seen.txt."""
    import statestore
    return str(statestore.load("last_seen", {"ts": ""}).get("ts", "")).strip()


def mark_seen() -> None:
    import statestore
    statestore.save("last_seen", {"ts": mint.timestamp()})


def _cli(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="autorun_log.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a_ = sub.add_parser("append"); a_.add_argument("folder"); a_.add_argument("--item", required=True); a_.add_argument("--status", required=True)
    a_.add_argument("--decisions", default=""); a_.add_argument("--commit", default="-"); a_.add_argument("--by", default="")
    d = sub.add_parser("digest"); d.add_argument("--since", default=None); d.add_argument("--json", action="store_true")
    sub.add_parser("mark-seen")
    a = ap.parse_args(argv)
    if a.cmd == "append":
        print(f"logged -> {append(a.folder, a.item, a.status, a.decisions, a.commit, a.by)}"); return 0
    if a.cmd == "digest":
        rows = digest(a.since if a.since is not None else last_seen())
        print(json.dumps(rows, ensure_ascii=False, indent=1) if a.json else "\n".join(
            f"{r['ts']} {r['folder']} · {r['item'][:60]} · {r['status']} · {r['decisions'][:100]}" for r in rows) or "(no lines)")
        return 0
    if a.cmd == "mark-seen":
        mark_seen(); print("marked"); return 0
    return 2


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
