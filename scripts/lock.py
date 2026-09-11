"""Lock writer — claim / adopt / close / reopen / release / check / consume / reader / who / whoami / mine / status
(PROTOCOL.md §1, §1b, §9, §15, §16). Nobody writes LOCK.yaml by hand.

  python scripts/lock.py claim <folder> --task "<one line>" [--stream S] [--hint word]
        Free -> mints a window (lib/mint.py), writes LOCK.yaml status: open, binds
        CLAUDE_CODE_SESSION_ID -> window in <STATE_ROOT>/sessions/, prints the identity line,
        the pointer's read-coverage proof and the cross-host rules-hash line (§15).
        Fresh foreign lock -> exit 1 (`closing` is reported as "signing off", never stale; LOCKED names the
        holder's peer address — message them first, §16); stale -> exit 2 (ask the owner; --force-stale after
        they agreed); fresh .firing.lock -> exit 3; a divergent sync conflict copy of a load-bearing file
        outside the folder -> exit 5 (fold it first, §15). Safe conflict-copy classes are folded by the claim.
  python scripts/lock.py adopt <folder> [--window W]   bind an existing lock to this session
  python scripts/lock.py close <folder>                 open -> closing (task judged complete)
  python scripts/lock.py reopen <folder> [--task ..]    closing -> open (task shifted)
  python scripts/lock.py release <folder> [--allow-dirty "<why>"]
        Refuses unless pointer mtime >= lock start, nothing uncommitted under the folder,
        every handoff this session staged still exists and is registered (or is recorded as
        consumed). Then deletes LOCK.yaml.
  python scripts/lock.py consume <note path>            record that this session consumed a handoff
        it staged (target folder must be yours or free); deletes the note. Without it, release
        calls a vanished note an orphan (v4.1 — records live in the sidecar <sid>.handoffs.txt).
  python scripts/lock.py reader [--hint menu]           READER identity: a window with no lock and no folder,
        so a menu-only session's state writes carry an author (v4.2). Edits stay denied; the Stop hook passes;
        `claim` replaces the reader binding with a real window.
  python scripts/lock.py who [--json]                   every lock -> window -> session -> holder's ListAgents
        name + live/GONE/exited/headless (lib/peers.py, §16) — who to message, never a broadcast.
  python scripts/lock.py check <folder> | whoami | mine | status
        check reports the resolved lock domain AND the folder's own LOCK.yaml when a registry
        edit moved the folder after it was claimed (literal lock, v4.1), then runs the conflict-copy
        scan dry (exit 5 on a blocking copy, nothing deleted); close/reopen/release/adopt operate on
        that literal lock when it carries your window.

Identity in Claude Code: the Bash tool exposes CLAUDE_CODE_SESSION_ID and hooks get the
same value as `session_id`; the binding maps it to the window, so no ICM_WINDOW export is
needed. Plain terminals and headless agents set ICM_WINDOW=<window> instead.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lib"))
import lockpath as lp  # noqa: E402
import mint  # noqa: E402

ROOT = lp.ROOT
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".next", "dist", "build"}


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def _folder(folder: str) -> str:
    rel = folder.replace("\\", "/").strip("/")
    return "" if rel in (".", "root") else rel


def _lock_dir_for(rel: str) -> Path:
    if rel == "":
        return lp.ROOT_LOCK_DIR
    try:
        res = lp.resolve(rel + "/__probe__")
    except lp.RegistryUnreadable as e:
        print(f"ERROR: registry unreadable — {e}", file=sys.stderr)
        sys.exit(4)
    if res.kind in ("registry", "root") and res.lock_dir:
        return res.lock_dir
    return lp.LOCK_TREE / rel / ".goal"


def _rel_of(lock_dir: Path) -> str:
    r = lp.lock_rel(lock_dir.parent)          # v4.3: relative to the LOCK TREE, never to a worktree root
    return "" if r == "." else r


def _literal_lock_dir(rel: str, resolved: Path, me, adopt: bool = False):
    """The folder's OWN .goal/ when it differs from the resolved home AND holds a LOCK.yaml that is
    (a) mine, or (b) for adopt: any interactive lock while the resolved home has none. A registry edit
    that folds a claimed folder into another home must not orphan its lock (v4.1)."""
    if not rel:
        return None
    literal = lp.LOCK_TREE / rel / ".goal"
    if literal.resolve() == resolved.resolve() or not (literal / "LOCK.yaml").is_file():
        return None
    mine = [li for li in lp.locks_at(literal) if li.kind == "interactive" and me and lp.same_window(li.window, me.window)]
    if mine:
        return literal
    if adopt and not any(li.kind == "interactive" for li in lp.locks_at(resolved)):
        return literal
    return None


def _lock_dir_for_folder(rel: str, me=None, adopt: bool = False) -> Path:
    """The lock domain a FOLDER belongs to: registry home if registered, else itself — unless a
    literal LOCK.yaml of the caller's own window sits at the folder itself (see _literal_lock_dir)."""
    resolved = _lock_dir_for(rel)
    literal = _literal_lock_dir(rel, resolved, me, adopt)
    if literal is not None:
        print(f"note: {rel} now resolves to {_rel_of(resolved) or '<root>'} in the registry, but its own LOCK.yaml carries "
              f"{'your' if me else 'an adoptable'} window — operating on the literal lock {rel}/.goal/LOCK.yaml")
        return literal
    return resolved


def _session_handoffs() -> list:
    """Handoffs this session STAGED and has not itself CONSUMED (sidecar records; v4.1)."""
    sid = _sid()
    if not sid:
        return []
    records = lp.read_handoffs(sid)
    consumed = {rel for kind, rel in records if kind == "consumed"}
    out = []
    for kind, rel in records:
        if kind != "staged" or rel in consumed or rel in out:
            continue
        target = rel.split("/.goal/", 1)[0]
        if not (lp.LOCK_TREE / target).is_dir():
            continue  # target folder gone (fixture, filed drop): nothing left to be orphaned in
        out.append(rel)
    return out


# ----------------------------------------------------------------------------- §15 / §16 helpers

def _holder_info(window: str) -> tuple:
    """(one-line holder description, state) via lib/peers.py — §16: who to message before staging a handoff."""
    try:
        import peers
        h = peers.holder(window)
    except Exception as e:  # noqa: BLE001 — the report must never die on the lookup
        return f"(holder lookup unavailable: {e})", "unknown"
    st = h.get("state", "unbound-here")
    sid8 = (h.get("session_id") or "")[:8]
    if st == "live":
        return f"{h['name']} · live (session {sid8}, pid {h['pid']}) — SendMessage to '{h['name']}'", st
    if st == "gone":
        return f"{h.get('name') or sid8} · GONE (pid {h['pid']} exited) — orphaned lock", st
    if st == "exited":
        return f"session {sid8} · exited (bound here, no live Claude Code process) — orphaned lock", st
    if st == "fired":
        return f"headless agent · session {sid8} (not addressable)", st
    if st == "unknown":
        return f"{h.get('name') or '?'} · liveness unknown", st
    return "unbound here (other host / plain terminal / headless agent) — nothing to message", st


def _pointer_proof(home_rel: str) -> None:
    """§15 read-coverage proof, printed at claim time: size + sha256 of the pointer and its action line VERBATIM,
    so the load-bearing line is in the session's context even if a later Read slices the file."""
    p = ROOT / home_rel / "workflow-state" / "current-pointer.md" if home_rel else ROOT / "workflow-state" / "current-pointer.md"
    shown = f"{home_rel + '/' if home_rel else ''}workflow-state/current-pointer.md"
    if not p.is_file():
        print(f"pointer: none yet at {shown} — the signoff writes it (PROTOCOL §3)")
        return
    try:
        b = p.read_bytes()
    except OSError as e:
        print(f"pointer: unreadable ({e})")
        return
    lines = b.decode("utf-8", "replace").splitlines()
    print(f"pointer: {len(lines)} lines · {len(b)} B · sha256 {hashlib.sha256(b).hexdigest()[:12]} — read it IN FULL "
          f"(a Read with offset/limit is reported as PARTIAL by the read guard)")
    for i, l in enumerate(lines, 1):
        if l.startswith("Next concrete action:"):
            print(f"pointer L{i}: {l}")
            break
    else:
        print("pointer: NO `Next concrete action:` line — invalid grammar (PROTOCOL §3); fix before signoff")


def _report(lock_dir: Path, me) -> int:
    rel = _rel_of(lock_dir) or "<root>"
    to = _rel_of(lock_dir) or "."
    locks = lp.locks_at(lock_dir)
    if not locks:
        print(f"FREE {rel}")
        return 0
    code = 0
    for li in locks:
        mine = me is not None and lp.same_window(li.window, me.window)
        if li.malformed:
            print(f"MALFORMED {rel}: {li.path.name} — {li.describe()} (treated as foreign)")
            code = max(code, 1)
        elif mine:
            print(f"YOURS {rel}: {li.describe()}")
        elif li.kind == "fired":
            if li.fresh:
                print(f"AGENT HOLDS {rel}: {li.describe()} — wait or stage a handoff (python scripts/handoff.py --to {to} ...)")
                code = max(code, 3)
            else:
                print(f"note: stale .firing.lock in {rel} ({li.describe()}) — a headless run died; its runner should clear it")
        elif li.fresh and li.status == "closing":
            line, _ = _holder_info(li.window)
            print(f"SIGNING OFF {rel}: {li.describe()} — the holder is mid-signoff, not stale. Do not take over.\n  Holder: {line}")
            code = max(code, 1)
        elif li.fresh:
            line, state = _holder_info(li.window)
            if state in ("live", "unknown"):
                action = (f"  -> STOP editing here — but do not park the work. MESSAGE THE HOLDER first (PROTOCOL §16): "
                          f"'you hold {li.window} on {rel}: release ETA, or hand it over?' — same-machine sessions answer within "
                          f"a minute. Stage a handoff (python scripts/handoff.py --to {to} ...) only when the holder says 'not soon'.")
            elif state == "fired":
                action = (f"  -> STOP editing here. A headless agent holds it (no peer address): wait for its lock to clear or "
                          f"stage a handoff (python scripts/handoff.py --to {to} ...).")
            elif state in ("gone", "exited"):
                action = ("  -> STOP editing here. The holder session is gone — this is an ORPHANED lock (§1): ask the owner "
                          "before taking over (claim --force-stale only after they agree); never silently proceed.")
            else:
                action = (f"  -> STOP editing here. No binding on this host (other host / plain terminal): stage a handoff "
                          f"(python scripts/handoff.py --to {to} ...) — nothing here can be messaged.")
            print(f"LOCKED {rel}: {li.describe()}\n  Holder: {line}\n{action}\n"
                  f"  `python scripts/lock.py who` lists every lock with its holder.")
            code = max(code, 1)
        else:
            print(f"STALE {rel}: {li.describe()}\n  -> Ask the owner before taking over. Never silently proceed over a stale lock.")
            code = max(code, 2)
    return code


def _me():
    try:
        return lp.identity()
    except lp.IdentityConflict as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(5)


def _sid() -> str:
    return (os.environ.get("CLAUDE_CODE_SESSION_ID") or "").strip()


def _bind(window: str, folder_rel: str, add: bool = True) -> None:
    sid = _sid()
    if not sid:
        print(f"note: CLAUDE_CODE_SESSION_ID not set (plain terminal) — export ICM_WINDOW={window} yourself.")
        return
    cur = lp.read_session(sid)
    folders = [f for f in cur.get("folders", []) if f is not None]
    key = folder_rel or "."
    if add and key not in folders:
        folders.append(key)
    if not add:
        folders = [f for f in folders if f != key]
    lp.write_session(sid, window, folders, {"user": os.environ.get("USERNAME") or os.environ.get("USER") or ""})


def bind_reader(sid: str, hint: str = "menu") -> str:
    """READER identity (v4.2): a window with NO lock and NO folder, so a menu-only session's state writes (seen
    stamp, guard log) carry an author. Idempotent: an existing binding of any kind is kept. Returns the window."""
    if not sid:
        return ""
    cur = lp.read_session(sid)
    if cur.get("window"):
        return str(cur["window"]).strip()
    window = mint.window(hint or "menu")
    lp.write_session(sid, window, [], extra={"user": os.environ.get("USERNAME") or os.environ.get("USER") or "", "kind": "reader"})
    lp.guard_log({"guard": "lock", "event": "reader", "window": window, "session_id": sid})
    return window


def _write_lock(lock_file: Path, window: str, task: str, stream: str, status: str, started: str) -> None:
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    branch = _git("symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip() or "detached"
    lock_file.write_text(
        f"holder: interactive\nwindow: \"{window}\"\nstatus: {status}\ntask: \"{task}\"\n"
        f"stream: \"{stream}\"\nbranch: \"{branch}\"\nstarted: \"{started}\"\n", encoding="utf-8")


# ----------------------------------------------------------------------------- commands

def cmd_claim(a) -> int:
    rel = _folder(a.folder)
    if rel and not (ROOT / rel).is_dir():
        print(f"ERROR: not a folder: {ROOT / rel}", file=sys.stderr)
        return 4
    import lifecycle as lc
    if rel and lc.is_archived_folder(rel):
        print(f"REFUSED: {rel} is ARCHIVED (PROTOCOL §17) — archived folders are never claimed. Restore it first: "
              f"python scripts/new.py {rel.split('/')[-1]} --unarchive", file=sys.stderr)
        return 7
    lock_dir = _lock_dir_for(rel)
    home = _rel_of(lock_dir)
    if home != rel:
        print(f"note: {rel or '<root>'} belongs to the lock domain {home or '<root>'} — locking that (one lock per workfolder).")
    me = _me()
    state = _report(lock_dir, me)
    if state in (1, 3):
        return state
    if state == 2 and not a.force_stale:
        print("  (re-run with --force-stale once the owner has agreed)")
        return 2
    try:
        import conflicts
        blocking = conflicts.gate(home, apply=True)
    except Exception as e:  # noqa: BLE001
        print(f"conflict-copy scan skipped ({type(e).__name__}: {e}) — copies UNVERIFIED")
        blocking = []
    if blocking:
        print(f"REFUSED: claim of {home or '<root>'} blocked by {len(blocking)} divergent conflict cop{'y' if len(blocking) == 1 else 'ies'} "
              f"of load-bearing files (PROTOCOL §15). Fold them, then claim again.")
        return 5
    if me and _sid() and lp.is_reader(_sid()):
        # a menu-only READER binding (no lock, no folder) is upgraded, never reused as a lock window
        print(f"note: replacing reader identity {me.window!r} with a real window (browsing -> claiming).")
        me = None
    if me:
        window = me.window
        print(f"note: this session already holds window {me.window!r} — claiming {home or '<root>'} under the same identity "
              f"(only when the owner's task spans both folders; drift is still a handoff).")
    else:
        window = mint.window(a.hint or a.task)
    started = mint.timestamp()
    _write_lock(lock_dir / "LOCK.yaml", window, a.task, a.stream or home or "root", "open", started)
    _bind(window, home)
    print(f"CLAIMED {home or '<root>'} · window={window} · status=open · started={started}")
    print(f"identity: bound to Claude session {_sid() or '(none)'} — hooks + pre-commit resolve it automatically.")
    print(f"plain terminal / headless equivalent: ICM_WINDOW={window}")
    _pointer_proof(home)
    try:
        import rules_hash
        for l in rules_hash.brief(do_publish=True).splitlines():
            print(l)
    except Exception as e:  # noqa: BLE001
        print(f"rules: hash check skipped ({type(e).__name__}: {e}) — bytes across hosts UNVERIFIED")
    return 0


def cmd_adopt(a) -> int:
    lock_dir = _lock_dir_for_folder(_folder(a.folder), _me(), adopt=True)
    locks = [li for li in lp.locks_at(lock_dir) if li.kind == "interactive"]
    if not locks:
        print(f"ERROR: no LOCK.yaml at {lock_dir} to adopt — claim instead.", file=sys.stderr)
        return 4
    li = locks[0]
    if a.window and not lp.same_window(a.window, li.window):
        print(f"ERROR: lock window is {li.window!r}, you said {a.window!r}.", file=sys.stderr)
        return 1
    me = _me()
    if me and not lp.same_window(me.window, li.window) and not (_sid() and lp.is_reader(_sid())):
        print(f"ERROR: this session is already {me.window!r}; release it before adopting {li.window!r}.", file=sys.stderr)
        return 5
    if "status:" not in li.path.read_text(encoding="utf-8", errors="replace"):
        with li.path.open("a", encoding="utf-8") as fh:
            fh.write("status: open\n")
    _bind(li.window, _rel_of(lock_dir))
    print(f"ADOPTED {_rel_of(lock_dir) or '<root>'} · window={li.window} · bound to session {_sid() or '(none — set ICM_WINDOW)'}")
    return 0


def _set_status(folder: str, new: str, task: str = "", stream: str = "") -> int:
    me = _me()
    if me is None:
        print(f"ERROR: {lp.NO_IDENTITY_HELP}", file=sys.stderr)
        return 5
    lock_dir = _lock_dir_for_folder(_folder(folder), me)
    locks = [li for li in lp.locks_at(lock_dir) if lp.same_window(li.window, me.window)]
    if not locks:
        print(f"ERROR: no lock of yours at {lock_dir}", file=sys.stderr)
        return 1
    li = locks[0]
    text = li.path.read_text(encoding="utf-8", errors="replace")
    text = re.sub(r"^status:.*$", f"status: {new}", text, flags=re.M) if "status:" in text else text + f"status: {new}\n"
    for key, val in (("task", task), ("stream", stream)):
        if val:
            line = f'{key}: "{val}"'
            text = re.sub(rf"^{key}:.*$", line, text, flags=re.M) if re.search(rf"^{key}:", text, re.M) else text + line + "\n"
    li.path.write_text(text, encoding="utf-8")
    print(f"{_rel_of(lock_dir) or '<root>'}: status {li.status} -> {new}")
    return 0


def cmd_close(a) -> int:
    return _set_status(a.folder, "closing")


def cmd_reopen(a) -> int:
    return _set_status(a.folder, "open", a.task or "", a.stream or "")


def cmd_release(a) -> int:
    me = _me()
    if me is None:
        print(f"ERROR: {lp.NO_IDENTITY_HELP}", file=sys.stderr)
        return 5
    lock_dir = _lock_dir_for_folder(_folder(a.folder), me)
    home = _rel_of(lock_dir)
    locks = lp.locks_at(lock_dir)
    if not locks:
        print(f"nothing to release at {lock_dir}")
        return 0
    mine = [li for li in locks if lp.same_window(li.window, me.window)]
    li = mine[0] if mine else locks[0]
    if not mine and not a.force:
        print(f"REFUSED: lock held by window={li.window!r}, you are {me.window!r}. --force only with the owner's say-so.")
        return 1
    problems = []
    ptr = ROOT / home / "workflow-state" / "current-pointer.md" if home else ROOT / "workflow-state" / "current-pointer.md"
    if not ptr.is_file():
        problems.append(f"no pointer: {ptr.relative_to(ROOT).as_posix()} does not exist — write it (PROTOCOL §3)")
    elif li.started and datetime.fromtimestamp(ptr.stat().st_mtime) < li.started:
        problems.append(f"pointer not updated since lock start {li.started.strftime(lp.TS_FMT)}: {ptr.relative_to(ROOT).as_posix()}")
    staged = _session_handoffs()
    idx = None
    if staged:
        try:
            import statestore
            idx = statestore.inboxes()          # §14: the inbox index is a state-store document (cache when offline)
        except Exception as e:  # noqa: BLE001
            print(f"note: inbox index unavailable ({e}) — handoff registration not verified")
    for h in staged:
        hp = lp.LOCK_TREE / h
        if not hp.exists():
            # a vanished note whose target folder is freshly held by ANOTHER window was consumed by that
            # holder — the target's own signoff records it; not this session's orphan (v4.1)
            tgt = h.split("/.goal/", 1)[0]
            foreign = [x for x in lp.locks_at(lp.LOCK_TREE / tgt / ".goal") if not lp.same_window(x.window, me.window)] \
                if (lp.LOCK_TREE / tgt).is_dir() else []
            if foreign:
                print(f"note: handoff {h} consumed by the current holder of {tgt} ({foreign[0].window}) — not an orphan")
                continue
            problems.append(f"orphaned handoff: {h} was written this session but is gone "
                            f"(consumed without a record? python scripts/lock.py consume {h})")
            continue
        if idx is not None and h.split("/.goal/", 1)[0] not in idx:
            problems.append(f"handoff {h} not registered in the inbox index (state store `inboxes`) — the next session would never see it")
        ignored = _git("check-ignore", "-q", h).returncode == 0
        if not ignored and _git("status", "--porcelain", "--", h).stdout.strip():
            problems.append(f"handoff {h} is tracked but uncommitted — commit it before releasing")
    dirty = _git("status", "--porcelain", "--", home or ".").stdout.strip()
    if dirty and not a.allow_dirty:
        problems.append(f"uncommitted changes under {home or '<root>'} (commit as yourself first):\n" +
                        "\n".join("      " + l for l in dirty.splitlines()[:15]))
    if problems:
        print(f"RELEASE REFUSED for {home or '<root>'} — finish signoff first:")
        for p in problems:
            print(f"  - {p}")
        if dirty and not a.allow_dirty:
            print("  (--allow-dirty \"<why>\" leaves work uncommitted on purpose — say why; it is recorded)")
        return 6
    li.path.unlink()
    _bind(me.window, home, add=False)
    lp.guard_log({"guard": "lock", "event": "release", "folder": home, "window": me.window,
                  "note": a.allow_dirty or ""})
    print(f"RELEASED {home or '<root>'}" + (f" (allow-dirty: {a.allow_dirty})" if a.allow_dirty else ""))
    return 0


def cmd_check(a) -> int:
    rel = _folder(a.folder)
    me = _me()
    resolved = _lock_dir_for(rel)
    code = _report(resolved, me)
    literal = lp.LOCK_TREE / rel / ".goal" if rel else None
    if literal is not None and literal.resolve() != resolved.resolve() and (literal / "LOCK.yaml").is_file():
        print(f"note: {rel} also carries its OWN LOCK.yaml (registry maps the folder to {_rel_of(resolved) or '<root>'}):")
        code = max(code, _report(literal, me))
    try:
        import conflicts
        if conflicts.gate(_rel_of(resolved), apply=False):   # dry: reports, never deletes
            code = max(code, 5)
    except Exception as e:  # noqa: BLE001
        print(f"conflict-copy scan skipped ({e})")
    return code


def cmd_consume(a) -> int:
    """Record that THIS session consumed a handoff it staged (the target folder must be yours or free)."""
    me = _me()
    if me is None:
        print(f"ERROR: {lp.NO_IDENTITY_HELP}", file=sys.stderr)
        return 5
    rel = a.path.replace("\\", "/").strip("/")
    target = rel.split("/.goal/", 1)[0]
    locks = lp.locks_at(lp.LOCK_TREE / target / ".goal") if (lp.LOCK_TREE / target).is_dir() else []
    fresh_foreign = [li for li in locks if not lp.same_window(li.window, me.window) and li.fresh]
    mine = [li for li in locks if lp.same_window(li.window, me.window)]
    if fresh_foreign and not mine:
        print(f"REFUSED: {target} is held by window {fresh_foreign[0].window!r} — only the holder (or you, after claiming) may record consumption.")
        return 1
    if not mine:
        print(f"note: {target} is not locked by anyone — recording consumption without a holder")
    if rel not in _session_handoffs():
        print(f"nothing to do: {rel} is not an unconsumed handoff of this session")
        return 0
    lp.record_handoff(_sid(), "consumed", rel)
    if (lp.LOCK_TREE / rel).exists():
        try:
            (lp.LOCK_TREE / rel).unlink()
            print(f"consumed + deleted {rel}")
        except OSError as e:
            print(f"consumed (file left in place: {e}) {rel}")
    else:
        print(f"consumed {rel} (file already gone)")
    return 0


def cmd_reader(a) -> int:
    sid = _sid()
    if not sid:
        print("ERROR: no CLAUDE_CODE_SESSION_ID — a reader binding needs a Claude Code session (plain terminals: set ICM_WINDOW).",
              file=sys.stderr)
        return 5
    me = _me()
    if me and not lp.is_reader(sid):
        print(f"already bound: window={me.window} (a claimed session needs no reader identity)")
        return 0
    w = bind_reader(sid, a.hint)
    print(f"READER {w} · session {sid} · no lock, no folder — edits stay denied; Stop hook passes; `lock.py claim` upgrades this binding")
    return 0


def cmd_who(a) -> int:
    """Every lock with its holder resolved to a peer address (PROTOCOL §16) — read-only."""
    import peers
    rows = peers.who()
    if getattr(a, "json", False):
        import json
        print(json.dumps({"locks": rows, "peers": peers.registry()}, indent=2, default=str))
    else:
        print(peers.render(rows))
    return 0


def cmd_whoami(a) -> int:
    me = _me()
    if me is None:
        print(f"NO IDENTITY (session {_sid() or 'unknown'}). {lp.NO_IDENTITY_HELP}")
        return 5
    if _sid() and lp.is_reader(_sid()):
        print(f"READER {me.window} · session {_sid()} · no lock, no folder (browsing; claim before editing)")
        return 0
    print(f"window={me.window} source={me.source} session={_sid() or '-'}")
    return 0


def cmd_mine(a) -> int:
    me = _me()
    if me is None:
        print(f"NO IDENTITY. {lp.NO_IDENTITY_HELP}")
        return 5
    sid = _sid()
    sess = lp.read_session(sid) if sid else {}
    folders = sess.get("folders", [])
    if not folders:
        print(f"window {me.window}: no folders bound")
    for f in folders:
        for li in lp.locks_at(lp.LOCK_TREE / (f if f != "." else "") / ".goal"):
            if lp.same_window(li.window, me.window):
                print(f"{f}: {li.describe()}")
    for h in _session_handoffs():
        print(f"handoff staged (unconsumed): {h}")
    return 0


def cmd_status(a) -> int:
    me = _me()
    found = 0
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        if Path(dirpath).name == ".goal" and ("LOCK.yaml" in filenames or ".firing.lock" in filenames):
            _report(Path(dirpath), me)
            found += 1
            dirnames[:] = []
    if not found:
        print("no locks anywhere in the repo")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("claim"); c.add_argument("folder"); c.add_argument("--task", required=True)
    c.add_argument("--stream", default=""); c.add_argument("--hint", default=""); c.add_argument("--force-stale", action="store_true")
    c.set_defaults(fn=cmd_claim)
    d = sub.add_parser("adopt"); d.add_argument("folder"); d.add_argument("--window", default=""); d.set_defaults(fn=cmd_adopt)
    e = sub.add_parser("close"); e.add_argument("folder"); e.set_defaults(fn=cmd_close)
    r = sub.add_parser("reopen"); r.add_argument("folder"); r.add_argument("--task", default=""); r.add_argument("--stream", default="")
    r.set_defaults(fn=cmd_reopen)
    g = sub.add_parser("release"); g.add_argument("folder"); g.add_argument("--allow-dirty", default="")
    g.add_argument("--force", action="store_true"); g.set_defaults(fn=cmd_release)
    h = sub.add_parser("check"); h.add_argument("folder"); h.set_defaults(fn=cmd_check)
    k = sub.add_parser("consume", help="record that this session consumed a handoff it staged (target folder yours or free)")
    k.add_argument("path", help="repo-relative path of the .staged.md / .fired.md note"); k.set_defaults(fn=cmd_consume)
    rd = sub.add_parser("reader", help="bind a READER identity (no lock, no folder) so a menu-only session's state writes have an author")
    rd.add_argument("--hint", default="menu"); rd.set_defaults(fn=cmd_reader)
    w = sub.add_parser("who", help="every lock -> window -> session -> holder's peer name + live/gone (§16)")
    w.add_argument("--json", action="store_true"); w.set_defaults(fn=cmd_who)
    sub.add_parser("whoami").set_defaults(fn=cmd_whoami)
    sub.add_parser("mine").set_defaults(fn=cmd_mine)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
