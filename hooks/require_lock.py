"""Edit-time guard — Claude Code PreToolUse hook on Edit / Write / MultiEdit / NotebookEdit
(PROTOCOL.md §10: catches editing-without-a-lock WHERE it happens, before files tangle;
the pre-commit guard stays the model-agnostic last line).

.claude/settings.json (scripts/install.py --claude-hooks writes this):
  {"matcher": "Edit|Write|MultiEdit|NotebookEdit",
   "hooks": [{"type": "command", "command": "python .githooks/require_lock.py"}]}

  outside the repo                       -> allow
  identity unknown                       -> DENY  "no window identity"
  registry present but unreadable        -> DENY  (fail closed)
  path -> unguarded folder (no .goal/)   -> DENY  "claim it (creates .goal/)"
  whitelist (.goal/**, own workflow-state/**) -> allow
  fresh lock of another window (LOCK.yaml or .firing.lock) -> DENY, names holder/task/since
  stale foreign lock                     -> DENY, "ask the owner" (never silently proceed)
  fresh lock of mine (open or closing)   -> allow
  no fresh lock of mine                  -> DENY  "claim first"
  ANY internal error                     -> DENY  (a guard that cannot evaluate fails closed)

Output: JSON permissionDecision deny + exit 2. Every decision -> <repo>/.goal/guard_log.jsonl.
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

FILE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def _log(ctx: dict) -> None:
    try:
        import lockpath as lp
        lp.guard_log(ctx)
    except Exception:
        pass


def _deny(reason: str, ctx: dict) -> int:
    ctx.update({"decision": "deny", "reason": reason}); _log(ctx)
    sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                        "permissionDecisionReason": f"[require_lock] {reason}"}}) + "\n")
    sys.stderr.write(f"[require_lock] {reason}\n")
    return 2


def _allow(reason: str, ctx: dict) -> int:
    ctx.update({"decision": "allow", "reason": reason}); _log(ctx)
    return 0


def main() -> int:
    ctx: dict = {"guard": "require_lock"}
    try:
        inp = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as e:
        return _deny(f"hook input unparseable ({e}); refusing rather than guessing", ctx)
    tool = inp.get("tool_name", "")
    ti = inp.get("tool_input") or {}
    ctx.update({"session_id": inp.get("session_id", ""), "tool": tool})
    if tool not in FILE_TOOLS:
        return _allow("not a file-mutating tool", ctx)
    target = ti.get("file_path") or ti.get("notebook_path")
    if not target:
        return _deny(f"{tool} call carries no file path; cannot resolve its lock domain", ctx)
    import lockpath as lp
    rel = lp.to_rel(target)
    if rel is None:
        return _allow("outside the repo", ctx)
    ctx["rel"] = rel
    try:
        me = lp.identity(session_id=inp.get("session_id", ""))
    except lp.IdentityConflict as e:
        return _deny(str(e), ctx)
    try:
        res = lp.resolve(rel)
    except lp.RegistryUnreadable as e:
        return _deny(f"registry unreadable ({e}) — fix .folder-lock/registry.yaml first", ctx)
    ctx.update({"resolution": res.kind, "folder": res.folder})
    if me is None:
        return _deny(lp.NO_IDENTITY_HELP + f" (path {rel} -> folder {res.folder or '<root>'})", ctx)
    ctx["window"] = me.window
    if res.kind == "unguarded":
        return _deny(f"unguarded folder for {rel}: {res.detail}", ctx)
    wl = lp.is_whitelisted(rel, res, me)
    if wl:
        return _allow(f"whitelist: {wl}", ctx)
    locks = lp.locks_at(res.lock_dir)
    mine_fresh = None
    for li in locks:
        if li.malformed:
            return _deny(f"malformed lock {li.path} guards {rel} — treated as foreign; fix or remove it with the owner", ctx)
        if lp.same_window(li.window, me.window):
            if li.fresh:
                mine_fresh = li
            continue
        if li.fresh:
            who = "a headless agent" if li.kind == "fired" else "another session"
            return _deny(f"{rel} is inside folder '{res.folder or '<root>'}' held by {who}: {li.describe()}. Stage a handoff "
                         f"instead (python scripts/handoff.py --to {res.folder or '.'} --task \"...\"). If that lock is YOURS from "
                         f"before the guards: python scripts/lock.py adopt {res.folder or '.'}", ctx)
    if mine_fresh is not None:
        return _allow(f"own fresh lock ({mine_fresh.status}) on {res.folder or '<root>'}", ctx)
    stale_foreign = [li for li in locks if li.kind == "interactive" and not lp.same_window(li.window, me.window)]
    if stale_foreign:
        li = stale_foreign[0]
        return _deny(f"{rel} is inside folder '{res.folder or '<root>'}' with a STALE lock: {li.describe()}. Ask the owner "
                     f"before taking over — never silently proceed over a stale lock (PROTOCOL §1). With their say-so: "
                     f"python scripts/lock.py claim {res.folder or '.'} --task \"...\" --force-stale", ctx)
    if any(lp.same_window(li.window, me.window) for li in locks):
        return _deny(f"your lock on '{res.folder or '<root>'}' went stale; re-claim: python scripts/lock.py claim {res.folder or '.'} --task \"...\"", ctx)
    return _deny(f"no lock covers {rel}: folder '{res.folder or '<root>'}' is free but unclaimed by you. Claim it first "
                 f"(python scripts/lock.py claim {res.folder or '.'} --task \"...\"); the lock is the first act, however small the edit.", ctx)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:  # fail CLOSED
        sys.exit(_deny(f"guard error {e!r} — refusing (a guard that cannot evaluate fails closed)", {"guard": "require_lock"}))
