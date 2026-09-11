"""Shell write guard — Claude Code PreToolUse hook on Bash / PowerShell (PROTOCOL.md §17, deliverable 6).

The Edit/Write guard (`require_lock.py`) cannot see `echo x > file`, `tee`, `Out-File`, `Set-Content`. This hook
extracts the redirection / write targets of the command line (`lifecycle.shell_write_targets`) and applies the SAME
verdict function the Edit guard and the commit guard use (`lifecycle.write_verdict`): a target outside the held
folder (exceptions: `_inbox/` drops, `workflow-state/`) is denied with a message that names the held folder and
points to `handoff.py`; with no lock held everything but `_inbox/` is denied. Targets outside the repo pass.

.claude/settings.json (scripts/install.py --claude-hooks writes this), next to require_safe_git.py:
  {"matcher": "Bash|PowerShell", "hooks": [..., {"type": "command", "command": "python .githooks/require_write_scope.py"}]}

Best effort by design: the sanctioned writers (lock.py, handoff.py, items.py, new.py, signoff.py) write from
Python and are not redirections — that is the point. Internal error -> DENY (a guard that cannot evaluate fails closed).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _c in (HERE / "lib", HERE.parent / "lib"):
    if (_c / "lockpath.py").is_file():
        sys.path.insert(0, str(_c))
        break

SHELL_TOOLS = {"Bash", "PowerShell"}


def _emit(decision: str, reason: str, ctx: dict) -> int:
    ctx.update({"decision": decision, "reason": reason})
    try:
        import lockpath as lp
        lp.guard_log(ctx)
    except Exception:
        pass
    if decision != "deny":
        return 0
    sys.stdout.write(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": f"[require_write_scope] {reason}"}}) + "\n")
    sys.stderr.write(f"[require_write_scope] {reason}\n")
    return 2


def main() -> int:
    raw = sys.stdin.read()
    ctx: dict = {"guard": "require_write_scope"}
    try:
        inp = json.loads(raw or "{}")
    except json.JSONDecodeError as e:
        return _emit("deny", f"hook input unparseable ({e}); refusing rather than guessing", ctx)
    tool = inp.get("tool_name", "")
    if tool not in SHELL_TOOLS:
        return _emit("allow", "not a shell tool", ctx)
    cmd = (inp.get("tool_input") or {}).get("command") or ""
    sid = inp.get("session_id", "")
    ctx.update({"session_id": sid, "tool": tool})

    import lockpath as lp
    import lifecycle as lc
    targets = lc.shell_write_targets(cmd)
    if not targets:
        return _emit("allow", "no shell write targets", ctx)
    cwd = inp.get("cwd") or ""
    try:
        me = lp.identity(session_id=sid)
    except lp.IdentityConflict as e:
        return _emit("deny", str(e), ctx)
    try:
        flows = lp.load_registry()
    except lp.RegistryUnreadable as e:
        return _emit("deny", f"workflow registry unreadable ({e}) — every path resolution depends on it", ctx)
    checked = []
    for t in targets:
        rel = lc.to_rel_from(t, cwd)
        if rel is None:
            checked.append(f"{t} (outside repo)")
            continue
        ok, why, code = lc.write_verdict(rel, me, sid, mode="shell", flows=flows)
        if not ok:
            ctx.update({"target": t, "rel": rel, "code": code})
            return _emit("deny", f"shell write to {rel} [{code}]: {why}", ctx)
        checked.append(f"{rel} ({why[:40]})")
    ctx["targets"] = checked
    return _emit("allow", "all shell write targets inside scope", ctx)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:  # fail CLOSED
        sys.exit(_emit("deny", f"guard error {e!r} — refusing (a guard that cannot evaluate fails closed)", {"guard": "require_write_scope"}))
