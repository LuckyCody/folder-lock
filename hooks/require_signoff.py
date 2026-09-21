"""Stop guard — Claude Code Stop hook: no session ends without a signoff (PROTOCOL.md §9, §17).
Report-only: never modifies a lock or a file.

.claude/settings.json (scripts/install.py --claude-hooks writes this):
  "Stop": [{"hooks": [{"type": "command", "command": "python .githooks/require_signoff.py"}]}]

Two rules, both mechanical, both from lib/lifecycle.py (the same module the write guards use):

  A. SIGNOFF RECORD (§17, v5.3). The turn must have run the signoff — `scripts/signoff.py` (full) or
     `scripts/signoff.py --turn` (mid-task) — which writes `<sessions>/<sid>.signoff.json` carrying the
     terminal block as data. The record must be from THIS turn (written at or after the last human prompt)
     and its `held` must agree with the locks on disk. The chat itself is not inspected: it carries only the
     closing message for the owner. Legacy rule for reference — the block used to be required in the message:
        status: done | blocked | handed-off
        held:   <folder> | none
        next:   <exact command> | none — waiting on the owner
     and `held:` must agree with the locks on disk: `none` while a fresh lock of this window exists -> BLOCK;
     a folder named that this window does not hold -> BLOCK. Missing / malformed block -> BLOCK with
     "run the signoff (scripts/signoff.py) — every session ends with a terminal block". Applies to unclaimed sessions too (a session
     that held nothing still ends through the signoff (scripts/signoff.py), `held: none`). A headless agent may follow the block with its
     resumer outcome line (`DONE` | `WAITING_CODY` | `FAILED: …`).
  B. CLOSING LOCK (§9, unchanged). A lock with `status: closing` -> BLOCK while the folder's current-pointer.md is
     missing / older than the lock start ("pointer not updated"), else BLOCK because LOCK.yaml still exists
     ("release it").

  Idempotent: a message that already carries a well-formed, truthful block passes — running the signoff (scripts/signoff.py) twice never
  blocks twice. Loop guard: at most MAX_BLOCKS blocks per prompt (marker in <STATE_ROOT>/sessions/<sid>.stopblock);
  `stop_hook_active` alone does NOT pass a turn whose message still lacks the block (the re-stop after a block is
  exactly the turn that must carry it). Subagent stops (`agent_id`) pass — the main session owns the lifecycle.
  Internal error -> BLOCK once (fail closed). Every decision -> <STATE_ROOT>/guard_log.jsonl.
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

MAX_BLOCKS = 2
SIGNOFF_HINT = ("run the signoff — every turn ends through `python scripts/signoff.py` (full) or "
                "`python scripts/signoff.py --turn` (mid-task, ending on a question to the owner); "
                "the hook reads the signoff RECORD it writes, the chat shows only the closing message")


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


def _block_count(sid: str, prompt_id: str, bump: bool) -> int:
    """Blocks already issued for this prompt (marker `<key>\\n<count>`); bump=True records one more."""
    import lockpath as lp
    marker = lp.SESSIONS / f"{sid or 'nosid'}.stopblock"
    key = prompt_id or datetime.now().strftime("%Y-%m-%dT%H:%M")
    count = 0
    try:
        if marker.is_file():
            lines = marker.read_text(encoding="utf-8").splitlines()
            if lines and lines[0].strip() == key:
                count = int(lines[1]) if len(lines) > 1 and lines[1].strip().isdigit() else 1
        if bump:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(f"{key}\n{count + 1}\n", encoding="utf-8")
    except (OSError, ValueError):
        pass
    return count


def _closing_problems(me, folders: list) -> list:
    import lockpath as lp
    problems = []
    for f in folders:
        for li in lp.locks_at(lp.LOCK_TREE / f / ".goal"):
            if not lp.same_window(li.window, me.window) or not li.fresh or li.status != "closing":
                continue
            ptr = lp.ROOT / f / "workflow-state" / "current-pointer.md"
            if not ptr.is_file():
                problems.append(f"{f}: lock is `closing` but {f}/workflow-state/current-pointer.md does not exist — "
                                f"write it (signoff step 2b), commit, then `python scripts/signoff.py` (it releases)")
            elif li.started and datetime.fromtimestamp(ptr.stat().st_mtime) < li.started:
                problems.append(f"{f}: lock is `closing` but the pointer was last written "
                                f"{datetime.fromtimestamp(ptr.stat().st_mtime).strftime(lp.TS_FMT)}, before the lock start "
                                f"{li.started.strftime(lp.TS_FMT)} — update current-pointer.md (signoff step 2b), commit, release")
            else:
                problems.append(f"{f}: lock is `closing`, pointer is updated, but LOCK.yaml still exists — finish signoff: "
                                f"commit as yourself, then `python scripts/signoff.py` (release + record)")
    return problems


def main() -> int:
    ctx: dict = {"guard": "require_signoff"}
    try:
        inp = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as e:
        return _out(True, f"hook input unparseable ({e})", ctx)
    sid = inp.get("session_id", "")
    prompt_id = inp.get("prompt_id", "")
    ctx.update({"session_id": sid, "stop_hook_active": bool(inp.get("stop_hook_active"))})
    if inp.get("agent_id"):
        return _out(False, "subagent stop — main session owns the lifecycle", ctx)

    import lockpath as lp
    import lifecycle as lc

    def block_once(reason: str) -> int:
        n = _block_count(sid, prompt_id, bump=False)
        if n >= MAX_BLOCKS:
            return _out(False, f"already blocked {n}x this prompt — passing to avoid a loop; DEFECT: {reason}", ctx)
        _block_count(sid, prompt_id, bump=True)
        return _out(True, reason, ctx)

    try:
        st = lc.session_state(sid)
    except lp.IdentityConflict as e:
        return block_once(str(e))
    me = st["identity"]
    ctx.update({"window": st["window"], "state": st["state"], "held": st["held"], "fired": st["fired"]})

    # ---- A. signoff record (v5.3, two-output contract) ----------------------------------------------
    # The proof that a turn ended properly is the RECORD signoff.py wrote during this turn — not the
    # prose of the final message. The chat carries only the closing message (what waits on the owner,
    # answers to the owner's questions); the terminal block lives in the record and in the pointer.
    tr_path = inp.get("transcript_path", "")
    rec = lc.read_signoff_record(sid)
    if rec is None:
        return block_once(f"{SIGNOFF_HINT} — no signoff record for this session "
                          f"(state={st['state']}, held={', '.join(st['held']) or 'none'})")
    if not lc.record_covers_turn(rec, tr_path):
        return block_once(f"{SIGNOFF_HINT} — the last signoff record predates this turn's prompt "
                          f"(kind {rec.get('kind')}, status {rec.get('status')}); run it again for this turn")
    blk = {"status": rec["status"], "held": rec.get("held", "none"), "held_list": rec.get("held_list", []),
           "next": rec.get("next", "")}
    ctx["block"] = {"status": blk["status"], "held": blk["held"], "next": blk["next"][:120], "kind": rec.get("kind")}
    held_disk = set(st["held"])
    held_said = set(blk["held_list"])
    if held_said != held_disk:
        if held_said and not held_disk:
            return block_once(f"signoff record says held: {blk['held']} but this window holds no fresh lock — "
                              f"run the signoff again (or re-claim if the lock expired)")
        if held_disk and not held_said:
            return block_once(f"signoff record says held: none but this window still holds {', '.join(sorted(held_disk))} — "
                              f"release through `python scripts/signoff.py`, or end the turn with `python scripts/signoff.py --turn`")
        return block_once(f"signoff record held: {blk['held']} does not match the locks on disk ({', '.join(sorted(held_disk))}) — run the signoff again")

    # ---- B. closing lock (§9) ----------------------------------------------------------------------
    if me is not None and not st["reader"]:
        problems = _closing_problems(me, lc.candidate_folders(sid))
        if problems:
            return block_once("cannot end the turn holding a closing lock. " + " | ".join(problems) +
                              " — run the signoff (scripts/signoff.py) now (it is agent-initiated).")

    return _out(False, f"signoff record ok ({rec.get('kind')}: {blk['status']}, held {blk['held']}); no closing lock", ctx)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:  # fail closed, once per prompt (the marker is written on the way)
        try:
            sys.exit(_out(True, f"guard error {e!r} — refusing to end the turn once; fix the guard", {"guard": "require_signoff"}))
        except Exception:
            sys.exit(2)
