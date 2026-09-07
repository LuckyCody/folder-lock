"""Boundary-crossing handoff writer (PROTOCOL.md section 2).

Session A, locked into folder X, discovers its task needs an edit in folder Y.
It does NOT edit Y. It writes a handoff into Y's inbox and carries on:

  python scripts/handoff.py --to <folder> --task "<one line>" [--from <folder>] [--body notes.md | --body -]
  python scripts/handoff.py --to <folder> --task "..." --mode fire     # also writes .goal/state.yaml for a headless runner

stage (default) lands `<target>/.goal/inbox/<ts>-<slug>.staged.md`; whoever
next claims the target folder (human or agent) reads its inbox first.
fire additionally writes `<target>/.goal/state.yaml` (status: in_progress) so
a headless goal-runner you operate can pick it up. Fire refuses to clobber a
goal that is already in_progress and refuses PARKED folders (owner-suspended,
see the pointer grammar in PROTOCOL.md section 3).

File-based invocation only. Never drive another agent's window with keystrokes.
"""
from __future__ import annotations

import argparse
import datetime
import re
import subprocess
import sys
from pathlib import Path


def repo_root() -> Path:
    out = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                         text=True, encoding="utf-8", errors="replace").stdout.strip()
    return Path(out) if out else Path.cwd()


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:48] or "handoff"


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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--to", required=True, help="target workfolder, repo-relative")
    ap.add_argument("--task", required=True, help="one-line task")
    ap.add_argument("--mode", choices=["stage", "fire"], default="stage")
    ap.add_argument("--from", dest="source", default="", help="source workfolder")
    ap.add_argument("--body", default="", help="path to a body file, or - for stdin")
    a = ap.parse_args()

    root = repo_root()
    target_rel = a.to.replace("\\", "/").strip("/")
    target = root / target_rel
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
    now = datetime.datetime.now()
    inbox = target / ".goal" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    note = inbox / f"{now.strftime('%Y%m%d-%H%M')}-{slug(a.task)}.{'staged' if mode == 'stage' else 'fired'}.md"
    note.write_text(
        f"---\nmode: {mode}\ntask: \"{a.task}\"\nfrom: \"{a.source}\"\nto: \"{target_rel}\"\n"
        f"written: \"{now.strftime('%Y-%m-%dT%H:%M')}\"\n---\n\n# Handoff: {a.task}\n\n"
        f"{body.strip() or '(no body - the task line is the whole spec)'}\n\n"
        f"Consuming this handoff means doing the task (or filing it into this folder's "
        f"workflow-state), recording the outcome in this folder's progress/log, and deleting this file.\n",
        encoding="utf-8",
    )

    if mode == "fire":
        state = target / ".goal" / "state.yaml"
        goal_line = f"{a.task} (handoff from {a.source or 'dispatcher'} - read .goal/inbox/{note.name} first)"
        state.write_text(
            f"goal: {_yaml_str(goal_line)}\nstatus: in_progress\ncriteria_met: []\n"
            f"criteria_remaining:\n  - {_yaml_str(a.task)}\n",
            encoding="utf-8",
        )
        print(f"FIRED: {note.relative_to(root).as_posix()} + .goal/state.yaml (your headless runner picks it up)")
    else:
        print(f"STAGED: {note.relative_to(root).as_posix()} (read by whoever next claims {target_rel})")
    return 0


def _yaml_str(s: str) -> str:
    """Double-quoted YAML scalar with escapes - hand-rolled f-strings once produced
    unparseable YAML when a task line contained quotes."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


if __name__ == "__main__":
    sys.exit(main())
