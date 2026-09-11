"""Edit-time guard — Claude Code PreToolUse hook on Edit / Write / MultiEdit / NotebookEdit.
(PROTOCOL.md §10 + §17: catches writing-outside-the-held-folder WHERE it happens, before
files on disk tangle. The commit guard is the model-agnostic last line; this one is earlier.)

.claude/settings.json (scripts/install.py --claude-hooks writes this):
  {"matcher": "Edit|Write|MultiEdit|NotebookEdit", "hooks": [{"type": "command", "command": "python .githooks/require_lock.py"}]}

The decision is `lib/lifecycle.write_verdict` — the SAME function the shell guard (require_write_scope.py)
and the commit guard (check_locks.py) call, so the three cannot disagree. In short (§17 invariant 4):
  outside the repo                          -> allow (memory dir, scratchpad, other repos)
  _archive/**                               -> DENY  (archived folders are read-only; scripts/new.py <slug> unarchives)
  _inbox/**                                 -> allow (drop zone) unless the drop folder is held by another window
  .goal/**                                  -> allow (runtime carrier)
  **/workflow-state/**                      -> allow (resume record) unless the folder is held by another window
  no identity / reader identity             -> DENY  "no lock held — /next or /new first"
  registry unreadable                       -> DENY  (fail closed)
  unguarded folder (no registry entry, no .goal/) -> DENY "scripts/new.py <slug>"
  fresh lock of another window (literal or resolved) -> DENY, names the holder, YOUR held folder(s), handoff.py
  own fresh lock (open or closing)          -> allow
  free folder not held by you               -> DENY  (boundary crossing: handoff.py, or claim under the same window)
  ANY internal error                        -> DENY  (a guard that cannot evaluate fails closed)
Before the verdict: a divergent OneDrive conflict copy beside the file -> DENY (§15).

Output: JSON permissionDecision deny + exit 2 (belt and braces — either alone blocks).
Every decision is appended to <STATE_ROOT>/guard_log.jsonl (lockpath.STATE_ROOT, §14).
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


def _deny(reason: str, ctx: dict) -> int:
    ctx.update({"decision": "deny", "reason": reason})
    try:
        import lockpath as lp
        lp.guard_log(ctx)
    except Exception:
        pass
    sys.stdout.write(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": f"[require_lock] {reason}"}}) + "\n")
    sys.stderr.write(f"[require_lock] {reason}\n")
    return 2


def _allow(reason: str, ctx: dict) -> int:
    ctx.update({"decision": "allow", "reason": reason})
    try:
        import lockpath as lp
        lp.guard_log(ctx)
    except Exception:
        pass
    return 0


def main() -> int:
    raw = sys.stdin.read()
    ctx: dict = {"guard": "require_lock"}
    try:
        inp = json.loads(raw or "{}")
    except json.JSONDecodeError as e:
        return _deny(f"hook input unparseable ({e}); refusing rather than guessing", ctx)
    tool = inp.get("tool_name", "")
    ti = inp.get("tool_input") or {}
    sid = inp.get("session_id", "")
    ctx.update({"session_id": sid, "tool": tool,
                "path": ti.get("file_path") or ti.get("notebook_path") or ""})
    if tool not in FILE_TOOLS:
        return _allow("not a file-mutating tool", ctx)
    target = ti.get("file_path") or ti.get("notebook_path")
    if not target:
        return _deny(f"{tool} call carries no file path; cannot resolve its lock domain", ctx)

    import lockpath as lp
    import lifecycle as lc
    rel = lp.to_rel(target)
    if rel is None:
        return _allow("outside the repo", ctx)
    ctx["rel"] = rel

    try:
        me = lp.identity(session_id=sid)
    except lp.IdentityConflict as e:
        return _deny(str(e), ctx)
    try:
        flows = lp.load_registry()
        res = lp.resolve(rel, flows)
    except lp.RegistryUnreadable as e:
        return _deny(f"workflow registry unreadable ({e}); every path resolution depends on it — fix the registry first", ctx)
    ctx.update({"resolution": res.kind, "folder": res.folder, "window": me.window if me else ""})

    # §15: the file being edited must not have a divergent conflict copy beside it — two versions on disk,
    # the edit would land on one of them blind. Fold first (python lib/conflicts.py <folder> --resolve).
    try:
        import conflicts
        div = [c for c in conflicts.siblings_of(rel) if c.kind in ("divergent", "appendlog")]
    except Exception as e:
        return _deny(f"conflict-copy check failed for {rel} ({e!r}) — refusing (a guard that cannot evaluate fails closed)", ctx)
    if div:
        c = div[0]
        return _deny(f"{rel} has a conflict copy beside it: {c.sibling} ({c.kind}, {c.extra_lines} line(s) only in the copy). "
                     f"Two versions of one file on disk — fold the copy's lines into {c.canonical} (or decide they are noise), "
                     f"delete the copy, then edit. `python lib/conflicts.py {res.folder or ''} --json` lists it (§15).", ctx)

    ok, why, code = lc.write_verdict(rel, me, sid, mode="edit", flows=flows)
    ctx["code"] = code
    return _allow(why, ctx) if ok else _deny(f"[{code}] {why}", ctx)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:  # fail CLOSED
        sys.exit(_deny(f"guard error {e!r} — refusing (a guard that cannot evaluate fails closed)", {"guard": "require_lock"}))
