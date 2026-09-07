"""Lock writer — claim / adopt / close / reopen / release / check / whoami / mine / status
(PROTOCOL.md §1, §1b, §9). Nobody writes LOCK.yaml by hand.

  python scripts/lock.py claim <folder> --task "<one line>" [--stream S] [--hint word]
        Free -> mints a window (lib/mint.py), writes LOCK.yaml status: open, binds
        CLAUDE_CODE_SESSION_ID -> window in <repo>/.goal/sessions/, prints the identity line.
        Fresh foreign lock -> exit 1 (`closing` is reported as "signing off", never stale);
        stale -> exit 2 (ask the owner; --force-stale after they agreed); fresh .firing.lock -> exit 3.
  python scripts/lock.py adopt <folder> [--window W]   bind an existing lock to this session
  python scripts/lock.py close <folder>                 open -> closing (task judged complete)
  python scripts/lock.py reopen <folder> [--task ..]    closing -> open (task shifted)
  python scripts/lock.py release <folder> [--allow-dirty "<why>"]
        Refuses unless pointer mtime >= lock start, nothing uncommitted under the folder,
        every handoff this session wrote still exists and is registered. Then deletes LOCK.yaml.
  python scripts/lock.py check <folder> | whoami | mine | status

Identity in Claude Code: the Bash tool exposes CLAUDE_CODE_SESSION_ID and hooks get the
same value as `session_id`; the binding maps it to the window, so no ICM_WINDOW export is
needed. Plain terminals and headless agents set ICM_WINDOW=<window> instead.
"""
from __future__ import annotations

import argparse
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
    return ROOT / rel / ".goal"


def _rel_of(lock_dir: Path) -> str:
    r = lock_dir.parent.relative_to(ROOT).as_posix()
    return "" if r == "." else r


def _report(lock_dir: Path, me) -> int:
    rel = _rel_of(lock_dir) or "<root>"
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
                print(f"AGENT HOLDS {rel}: {li.describe()} — wait or stage a handoff (python scripts/handoff.py --to {rel} ...)")
                code = max(code, 3)
            else:
                print(f"note: stale .firing.lock in {rel} ({li.describe()}) — a headless run died; its runner should clear it")
        elif li.fresh and li.status == "closing":
            print(f"SIGNING OFF {rel}: {li.describe()} — the holder is mid-signoff, not stale. Do not take over.")
            code = max(code, 1)
        elif li.fresh:
            print(f"LOCKED {rel}: {li.describe()}\n  -> STOP. Another session holds this folder. Stage a handoff instead of editing here.")
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
    return 0


def cmd_adopt(a) -> int:
    lock_dir = _lock_dir_for(_folder(a.folder))
    locks = [li for li in lp.locks_at(lock_dir) if li.kind == "interactive"]
    if not locks:
        print(f"ERROR: no LOCK.yaml at {lock_dir} to adopt — claim instead.", file=sys.stderr)
        return 4
    li = locks[0]
    if a.window and not lp.same_window(a.window, li.window):
        print(f"ERROR: lock window is {li.window!r}, you said {a.window!r}.", file=sys.stderr)
        return 1
    me = _me()
    if me and not lp.same_window(me.window, li.window):
        print(f"ERROR: this session is already {me.window!r}; release it before adopting {li.window!r}.", file=sys.stderr)
        return 5
    if "status:" not in li.path.read_text(encoding="utf-8", errors="replace"):
        with li.path.open("a", encoding="utf-8") as fh:
            fh.write("status: open\n")
    _bind(li.window, _rel_of(lock_dir))
    print(f"ADOPTED {_rel_of(lock_dir) or '<root>'} · window={li.window} · bound to session {_sid() or '(none — set ICM_WINDOW)'}")
    return 0


def _set_status(folder: str, new: str, task: str = "", stream: str = "") -> int:
    lock_dir = _lock_dir_for(_folder(folder))
    me = _me()
    if me is None:
        print(f"ERROR: {lp.NO_IDENTITY_HELP}", file=sys.stderr)
        return 5
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
    lock_dir = _lock_dir_for(_folder(a.folder))
    home = _rel_of(lock_dir)
    me = _me()
    if me is None:
        print(f"ERROR: {lp.NO_IDENTITY_HELP}", file=sys.stderr)
        return 5
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
    sid = _sid()
    for h in (lp.read_session(sid).get("handoffs", []) if sid else []):
        hp = ROOT / h
        if not hp.exists():
            problems.append(f"orphaned handoff: {h} was written this session but is gone")
            continue
        try:
            idx = lp.INBOX_INDEX.read_text(encoding="utf-8").splitlines()
        except OSError:
            idx = []
        if h.split("/.goal/", 1)[0] not in idx:
            problems.append(f"handoff {h} not registered in .goal/inboxes.txt — the next session would never see it")
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
    return _report(_lock_dir_for(_folder(a.folder)), _me())


def cmd_whoami(a) -> int:
    me = _me()
    if me is None:
        print(f"NO IDENTITY (session {_sid() or 'unknown'}). {lp.NO_IDENTITY_HELP}")
        return 5
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
        for li in lp.locks_at(ROOT / (f if f != "." else "") / ".goal"):
            if lp.same_window(li.window, me.window):
                print(f"{f}: {li.describe()}")
    for h in sess.get("handoffs", []):
        print(f"handoff written: {h}")
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
    sub.add_parser("whoami").set_defaults(fn=cmd_whoami)
    sub.add_parser("mine").set_defaults(fn=cmd_mine)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
