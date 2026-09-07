"""Stop guard — Claude Code Stop hook: a turn cannot end holding a `closing` lock
(PROTOCOL.md §9 + §10). Report-only: never modifies a lock.

.claude/settings.json (scripts/install.py --claude-hooks writes this):
  "Stop": [{"hooks": [{"type": "command", "command": "python .githooks/require_signoff.py"}]}]

  identity unknown                 -> BLOCK once per prompt ("no window identity")
  no lock held / status: open      -> pass
  status: closing                  -> BLOCK: pointer missing or older than lock start
                                      ("pointer not updated"), else "LOCK.yaml still exists — release"
  stop_hook_active / already blocked this prompt -> pass (loop guard)
  internal error                   -> BLOCK once (fail closed)
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _c in (HERE / "lib", HERE.parent / "lib"):
    if (_c / "lockpath.py").is_file():
        sys.path.insert(0, str(_c))
        break


def _out(block: bool, reason: str, ctx: dict) -> int:
    ctx.update({"decision": "block" if block else "pass", "reason": reason})
    try:
        import lockpath as lp
        lp.guard_log(ctx)
    except Exception:
        pass
    if not block:
        return 0
    msg = f"[require_signoff] {reason}"
    sys.stdout.write(json.dumps({"decision": "block", "reason": msg,
                                 "hookSpecificOutput": {"hookEventName": "Stop", "decision": "block", "reason": msg}}) + "\n")
    sys.stderr.write(msg + "\n")
    return 2


def _already_blocked(sid: str, prompt_id: str) -> bool:
    import lockpath as lp
    marker = lp.SESSIONS / f"{sid}.stopblock"
    key = prompt_id or datetime.now().strftime("%Y-%m-%dT%H:%M")
    try:
        if marker.is_file() and marker.read_text(encoding="utf-8").strip() == key:
            return True
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(key, encoding="utf-8")
    except OSError:
        pass
    return False


def main() -> int:
    ctx: dict = {"guard": "require_signoff"}
    try:
        inp = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as e:
        return _out(True, f"hook input unparseable ({e})", ctx)
    sid = inp.get("session_id", "")
    ctx["session_id"] = sid
    if inp.get("stop_hook_active"):
        return _out(False, "stop_hook_active (loop guard)", ctx)
    if inp.get("agent_id"):
        return _out(False, "subagent stop — the main session owns the lock", ctx)
    import lockpath as lp
    try:
        me = lp.identity(session_id=sid)
    except lp.IdentityConflict as e:
        return _out(False, "conflict already reported", ctx) if _already_blocked(sid, inp.get("prompt_id", "")) else _out(True, str(e), ctx)
    if me is None:
        if _already_blocked(sid, inp.get("prompt_id", "")):
            return _out(False, "no identity — already reported this prompt", ctx)
        return _out(True, lp.NO_IDENTITY_HELP, ctx)
    ctx["window"] = me.window
    folders = [f for f in lp.read_session(sid).get("folders", [])] if sid else []
    if os.environ.get("ICM_FOLDER"):
        folders.append(os.environ["ICM_FOLDER"].strip().strip("/") or ".")
    if "." not in folders:
        folders.append(".")  # the root lock is checked for every session
    problems = []
    for f in folders:
        base = lp.ROOT if f == "." else lp.ROOT / f
        for li in lp.locks_at(base / ".goal"):
            if not lp.same_window(li.window, me.window) or not li.fresh or li.status != "closing":
                continue
            ptr = base / "workflow-state" / "current-pointer.md"
            label = f or "<root>"
            if not ptr.is_file():
                problems.append(f"{label}: lock is `closing` but workflow-state/current-pointer.md does not exist — write it, commit, then `python scripts/lock.py release {f}`")
            elif li.started and datetime.fromtimestamp(ptr.stat().st_mtime) < li.started:
                problems.append(f"{label}: lock is `closing` but the pointer was last written {datetime.fromtimestamp(ptr.stat().st_mtime).strftime(lp.TS_FMT)}, "
                                f"before the lock start {li.started.strftime(lp.TS_FMT)} — update current-pointer.md, commit, release")
            else:
                problems.append(f"{label}: lock is `closing`, pointer is updated, but the lock still exists — commit as yourself, then `python scripts/lock.py release {f}`")
    if not problems:
        return _out(False, "no closing lock held", ctx)
    if _already_blocked(sid, inp.get("prompt_id", "")):
        return _out(False, "closing lock — already blocked once this prompt", ctx)
    return _out(True, "cannot end the turn holding a closing lock. " + " | ".join(problems) + " — finish the signoff now (it is agent-initiated).", ctx)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        sys.exit(_out(True, f"guard error {e!r} — refusing to end the turn once; fix the guard", {"guard": "require_signoff"}))
