"""SessionStart hook — the blank-window bootstrap (PROTOCOL.md §17, v5).

.claude/settings.json (scripts/install.py --claude-hooks writes this):
  "SessionStart": [{"matcher": "startup|clear", "hooks": [{"type": "command", "command": "python .githooks/session_start.py"}]}]

Runs from the repo root — where a blank window actually is — so a session that is not yet in any folder still
gets the rules before its first tool call:
  - binds a fresh READER window to the session when it has none (`lock.py reader --hint fresh`; the binding IS the
    session's identity inside Claude Code — the first claim upgrades it)
  - states the lifecycle invariants + the terminal block the Stop hook demands
  - forces the TRIAGE as the first action: continuation -> `board.py menu` + `lock.py claim`; new work -> `new.py <slug>`
  - a headless agent fired by the loop (ICM_WINDOW + ICM_FOLDER in its env) gets no triage — straight to its folder
Never blocks a session start; every failure degrades to one honest line. No board rendering here (that is the
menu's job); no briefing injection — installations that keep one append it after this block.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _c in (HERE / "lib", HERE.parent / "lib"):
    if (_c / "lockpath.py").is_file():
        sys.path.insert(0, str(_c))
        break

TERMINAL_BLOCK = ("  status: done | blocked | handed-off\n"
                  "  held:   <folder> | none\n"
                  "  next:   <exact command> | none — waiting on the owner")


def _scripts() -> str:
    """How to address the skill's scripts from the repo root (installed copy or the skill checkout)."""
    for cand in (Path(".claude/skills/folder-lock/scripts"), Path("scripts")):
        if (cand / "lock.py").is_file():
            return cand.as_posix()
    return "<skill>/scripts"


def _bind_fresh(sid: str) -> str:
    try:
        import lockpath as lp
        import mint
        cur = lp.read_session(sid)
        if cur.get("window"):
            return str(cur["window"]).strip()
        window = mint.window("fresh")
        lp.write_session(sid, window, [], extra={"user": os.environ.get("USERNAME", ""), "kind": "reader"})
        lp.guard_log({"guard": "session_start", "event": "reader", "window": window, "session_id": sid})
        return window
    except Exception:  # noqa: BLE001
        return ""


def lifecycle_text(inp: dict) -> str:
    sid = (inp.get("session_id") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "").strip()
    S = _scripts()
    try:
        import lifecycle as lc
        st = lc.session_state(sid)
    except Exception as e:  # noqa: BLE001
        return (f"SESSION LIFECYCLE (PROTOCOL §17): state unknown ({type(e).__name__}: {e}) — run `python {S}/../lib/lifecycle.py state`. "
                f"Every turn ends with the terminal block:\n{TERMINAL_BLOCK}")
    window = st["window"]
    if st["fired"]:
        folder = (os.environ.get("ICM_FOLDER") or "").strip() or (st["held"][0] if st["held"] else "?")
        return (f"SESSION LIFECYCLE (§17) — HEADLESS AGENT {window}: no triage. Folder `{folder}` is yours (.goal/.firing.lock). "
                f"Work the ONE item, sign off yourself (`python {S}/signoff.py`), and END the final reply with the terminal block, "
                f"then your outcome line (DONE | WAITING_OWNER | FAILED: …):\n{TERMINAL_BLOCK}\n"
                f"Writes outside `{folder}` are denied (handoff.py instead).")
    if not window and sid:
        window = _bind_fresh(sid)
        bound = f"window {window} (fresh reader identity bound to this session — no lock, no folder; `lock.py claim` upgrades it)" if window \
            else "no window bound — the first claim mints one"
    else:
        bound = f"window {window or 'unbound'}" + (f" · holds {', '.join(f or '<root>' for f in st['held'])}" if st["held"] else "")
    if st["state"] == "claimed":
        first = (f"FIRST ACTION: you already hold {', '.join(f or '<root>' for f in st['held'])} — read its workflow-state/current-pointer.md "
                 f"and continue at `Next concrete action:`. Sign off through `python {S}/signoff.py` when the task is done.")
    else:
        first = ("FIRST ACTION — TRIAGE, before any other tool use (reading files to decide is allowed; writing is denied until a lock is held):\n"
                 f"  continuation of open work  → python {S}/board.py menu   then   python {S}/lock.py claim <folder> --task \"...\"\n"
                 f"  new work                   → python {S}/new.py <slug> --goal \"...\"   (scaffold + registry entry + claim + board item)\n"
                 f"  only answering / browsing  → no claim; still end through `python {S}/signoff.py --held none`")
    return (
        f"SESSION LIFECYCLE (PROTOCOL §17 — mandatory, enforced by hooks) · state: {st['state']} · {bound}\n"
        f"Every session is in exactly one state: unclaimed → claimed(<folder>) → signed-off. Every session ends through the signoff, "
        f"including one that held nothing. Every final reply ENDS with the terminal block (the Stop hook blocks the turn otherwise; "
        f"`held:` must match the locks on disk):\n{TERMINAL_BLOCK}\n"
        f"{first}\n"
        f"Writes: only inside the held folder (+ `{os.environ.get('FOLDER_LOCK_INBOX', '_inbox')}/` drops and `workflow-state/`); with no lock, "
        f"nothing but the drop zone. Anything else is a handoff: python {S}/handoff.py --to <folder> --task \"...\" "
        f"(the Edit, shell and commit guards all deny — one verdict function, lib/lifecycle.py).\n"
        f"Every folder has an owns: entry in .folder-lock/registry.yaml (new.py writes it); folders are archived by the signoff when "
        f".goal/goal.md says complete: true and the board holds 0 open items — never deleted."
    )


def main() -> None:
    try:
        inp = json.loads(sys.stdin.read() or "{}") if not sys.stdin.isatty() else {}
    except (json.JSONDecodeError, OSError):
        inp = {}
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": lifecycle_text(inp)}},
                     ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 — never block a session start
        sys.exit(0)
