"""Boundary-crossing handoff writer (PROTOCOL.md §2).

Session A, locked into folder X, discovers its task needs an edit in folder Y. It does
NOT edit Y (the edit-time guard would refuse anyway). It writes a handoff into Y's inbox:

  python scripts/handoff.py --to <folder> --task "<one line>" [--from <folder>] [--body notes.md | --body -]
  python scripts/handoff.py --to <folder> --task "..." --mode fire      # also writes .goal/state.yaml for a headless runner

Names and timestamps are minted (lib/mint.py). The handoff is recorded on this session's
binding so `scripts/lock.py release` refuses while it is orphaned or unregistered.
File-based invocation only. Never drive another agent's window with keystrokes.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lib"))
import lockpath as lp  # noqa: E402
import mint  # noqa: E402

ROOT = lp.ROOT


def _yaml_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def goal_status(target: Path):
    state = target / ".goal" / "state.yaml"
    if not state.exists():
        return None
    m = re.search(r"^status:\s*(\S+)", state.read_text(encoding="utf-8", errors="replace"), re.M)
    return m.group(1) if m else None


def is_parked(target: Path) -> bool:
    ptr = target / "workflow-state" / "current-pointer.md"
    try:
        for raw in ptr.read_text(encoding="utf-8", errors="replace").splitlines():
            s = raw.strip().lower()
            if s.startswith("next concrete action:"):
                return s[len("next concrete action:"):].strip().startswith("parked")
    except OSError:
        pass
    return False


def _register_inbox(target_rel: str) -> None:
    """Inbox index = state-store document `inboxes` (PROTOCOL §14) — was <repo>/.goal/inboxes.txt."""
    import statestore
    statestore.register_inbox(target_rel)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--to", required=True); ap.add_argument("--task", required=True)
    ap.add_argument("--mode", choices=["stage", "fire"], default="stage")
    ap.add_argument("--from", dest="source", default=""); ap.add_argument("--body", default="")
    ap.add_argument("--force", action="store_true", help="owner-only: bypass the §13 dedup")
    a = ap.parse_args()
    target_rel = a.to.replace("\\", "/").strip("/")
    target = ROOT / target_rel if target_rel not in ("", ".") else ROOT
    target_rel = target_rel or "."
    if not target.is_dir():
        print(f"ERROR: target folder not found: {target}", file=sys.stderr)
        return 2
    mode = a.mode
    if mode == "fire" and is_parked(target):
        print(f"ERROR: {target_rel} is PARKED (owner-suspended) - fire refused. Stage instead.", file=sys.stderr)
        return 3
    if mode == "fire" and goal_status(target) == "in_progress":
        print(f"WARN: {target_rel}/.goal/state.yaml is in_progress - staging instead of firing (never clobber a live goal).")
        mode = "stage"
    body = sys.stdin.read() if a.body == "-" else (Path(a.body).read_text(encoding="utf-8") if a.body else "")
    now = mint.now()
    # dedup (PROTOCOL §13): a handoff IS a board item — no second open item with the same owner + title
    import items as _items
    _dup = _items.find_dup(_items.load(), _items.owner_of(target_rel), a.task)
    if _dup and not getattr(a, "force", False):
        print(f"DUPLICATE: an open item with this title already exists for that owner: {_dup} -> nothing written "
              f"(extend that item, or --force with the owner's say-so)", file=sys.stderr)
        return 4
    inbox = target / ".goal" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    note = inbox / f"{mint.handoff(a.task, now)}.{'staged' if mode == 'stage' else 'fired'}.md"
    note.write_text(
        f"---\nmode: {mode}\ntask: {_yaml_str(a.task)}\nfrom: \"{a.source}\"\nto: \"{target_rel}\"\n"
        f"written: \"{mint.timestamp(now)}\"\n---\n\n# Handoff: {a.task}\n\n"
        f"{body.strip() or '(no body - the task line is the whole spec)'}\n\n"
        f"Consuming this handoff means doing the task (or filing it into this folder's workflow-state), "
        f"recording the outcome in this folder's progress/log, and deleting this file.\n", encoding="utf-8")
    _register_inbox(target_rel)
    try:
        _items.upsert(target_rel, "handoff", note.name, a.task, created_by=a.source or "dispatcher", status="ready", force=True)
    except Exception as _e:
        print(f"(item not recorded: {_e})", file=sys.stderr)
    sid = (os.environ.get("CLAUDE_CODE_SESSION_ID") or "").strip()
    if sid:  # sidecar ledger (v4.1): survives every rewrite of the binding YAML; release checks it
        lp.record_handoff(sid, "staged", note.relative_to(ROOT).as_posix())
    if mode == "fire":
        (target / ".goal" / "state.yaml").write_text(
            f"goal: {_yaml_str(a.task + ' (handoff from ' + (a.source or 'dispatcher') + ' - read .goal/inbox/' + note.name + ' first)')}\n"
            f"status: in_progress\ncriteria_met: []\ncriteria_remaining:\n  - {_yaml_str(a.task)}\n", encoding="utf-8")
        print(f"FIRED: {note.relative_to(ROOT).as_posix()} + .goal/state.yaml (your headless runner picks it up)")
    else:
        print(f"STAGED: {note.relative_to(ROOT).as_posix()} (read by whoever next claims {target_rel})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
