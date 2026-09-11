"""signoff.py — the signoff tail: status + next from the board, archive when the goal is complete, release, terminal block
(PROTOCOL.md §9 + §17). The SKILL's steps 1–3 (close, snapshot, progress + pointer, registry, memory) and the
commit stay the agent's; this script is the mechanical tail that used to be five hand-typed commands, plus the two
decisions no agent should improvise: what `status:`/`next:` are, and whether the folder is finished.

  python scriptsthe signoff (scripts/signoff.py).py [--folder <held folder>] [--decisions "<defaults decided, or none>"] [--commit <sha>]
                         [--handed-off <lane>] [--no-kick] [--dry-run]
  python scriptsthe signoff (scripts/signoff.py).py --held none [--folder <folder the session was about>] [--decisions "..."]

Held folder (normal case), in order:
  1. `items.py from-pointer <folder>`       the pointer just written becomes the folder's board item
  2. status/next from the BOARD:             open items of the folder not flagged waiting on the owner -> `next:` = the top
                                             item's exact command (a pointer's action line verbatim, `scripts/lock.py claim <folder>`
                                             for a handoff); only flagged items left -> `next: none — waiting on the owner`;
                                             the session staged handoffs and nothing is left here -> `status: handed-off`,
                                             `next:` names that lane. `status: blocked` when the folder's own pointer item
                                             waits on the owner.
  3. ARCHIVE decision: `.goal/goal.md` says `complete: true` AND zero open items for the folder ->
                                             `git mv <folder> _archive/<slug>` (untracked leftovers moved too, the lock
                                             travels), registry globs rewritten to the new path + `archived: true`, every
                                             board item of the folder -> done, codename retired, ONE commit. Nothing deleted.
  4. autorun-log line                        (`autorun_log.py append`)
  5. `lock.py release <folder>`               (refuses on a stale pointer / dirty tree / orphaned handoff — surfaced, exit 6)
  6. board --signpost + memory mirror + resumer kick (`--no-kick` skips the kick)
  7. prints the terminal block — paste it VERBATIM as the last lines of the reply (the Stop hook checks it)

held: none (a session that claimed nothing — browsing, a refused diverged checkout, deferring to an existing item):
  no lock release, no commit; ONE record line into the autorun log of `--folder` (or the root lane) and — when
  `--folder` is given and free — a dated session note under the H1 of that folder's pointer (§17: the pointer stays the
  place the next session looks); then the terminal block with `held: none` and `next:` from the board.
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
import lifecycle as lc  # noqa: E402
import mint  # noqa: E402

ROOT = lp.ROOT
TREE = lp.LOCK_TREE
ICM = Path(__file__).resolve().parent            # scripts/
LIB = ICM.parent / "lib"
FLAGGED = ("waiting_owner", "parked")
OPEN = ("ready", "in_progress", "waiting_world", "waiting_owner", "parked")


def _git(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def _py(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    e = dict(os.environ)
    if env:
        e.update(env)
    return subprocess.run([sys.executable, *args], cwd=ROOT, env=e, capture_output=True, text=True, encoding="utf-8", errors="replace")


def _sid() -> str:
    return (os.environ.get("CLAUDE_CODE_SESSION_ID") or "").strip()


def goal_complete(folder: str) -> bool:
    p = TREE / folder / ".goal" / "goal.md"
    try:
        return re.search(r"^complete:\s*true\s*$", p.read_text(encoding="utf-8", errors="replace"), re.M | re.I) is not None
    except OSError:
        return False


def folder_items(folder: str) -> list:
    """Open board items owned by the folder (statestore document `items`), newest last."""
    import items as _items
    data = _items.load()
    out = [dict(key=k, **e) for k, e in data["items"].items()
           if e.get("owner") == folder and e.get("status") in OPEN]
    out.sort(key=lambda e: (0 if e.get("kind") == "pointer" else 1, e.get("created", "")))
    return out


def codename_of(folder: str, kind: str, ref: str) -> str:
    """The twin keeps no codename map — the item key is the handle."""
    return ""


def item_command(e: dict) -> str:
    """The exact command that works an item."""
    folder = e.get("owner") or e.get("folder") or ""
    if e.get("kind") == "pointer":
        import items as _items
        line = _items.pointer_line(folder)
        if line:
            return line
    return f"python scripts/lock.py claim {folder or '<root>'} --task \"{(e.get('title') or e.get('key') or '')[:80]}\""


def decide(folder: str, handed_off: str = "", staged: list | None = None) -> dict:
    """{status, next, archive: bool, open: [...], flagged: [...]} for a folder."""
    its = folder_items(folder)
    flagged = [e for e in its if e.get("status") in FLAGGED]
    live = [e for e in its if e.get("status") not in FLAGGED]
    own_pointer = next((e for e in its if e.get("kind") == "pointer"), None)
    complete = goal_complete(folder)
    out = {"open": live, "flagged": flagged, "complete": complete, "archive": complete and not its}
    if handed_off or (staged and not live and not flagged):
        lane = handed_off or staged[0]
        target = lane.split("/.goal/")[0] if "/.goal/" in lane else lane
        out.update(status="handed-off", next=f"python scripts/lock.py claim {target} --task \"...\"")
        return out
    if live:
        out.update(status="done", next=item_command(live[0]))
    elif flagged:
        out.update(status="blocked" if (own_pointer and own_pointer.get("status") == "waiting_owner") else "done", next=lc.NEXT_NONE)
    else:
        out.update(status="done", next="python scripts/board.py menu")
    return out


def _slugify(folder: str) -> str:
    return folder.rstrip("/").split("/")[-1]


def archive(folder: str, ts: str, dry: bool) -> tuple:
    """git mv <folder> -> _archive/<slug>[-<yyyymmdd>], registry rewrite, items done, codename retired, one commit.
    Returns (dest_rel, notes). Nothing is deleted."""
    import new as nf
    slug = _slugify(folder)
    dest = f"{lc.ARCHIVE_ROOT}/{slug}"
    if (TREE / dest).exists():
        dest = f"{dest}-{ts[:10].replace('-', '')}"
    notes = [f"archive {folder} -> {dest}"]
    if dry:
        return dest, notes + ["(dry-run: nothing moved)"]
    flows = lp.load_registry()
    w = lp.workflow_for(folder + "/__probe__", flows)
    notes += nf._move_tree(TREE / folder, TREE / dest)
    if w is not None and w.home == folder:
        note = nf.registry_apply(lambda t: nf.rewrite_entry_paths(t, w.id, folder, dest, archived=True, ts=ts))
        notes.append(f"registry: {w.id} owns {dest}/** · archived: true · {note}")
    else:
        entry = nf.registry_entry(slug, dest, f"archived {ts} from {folder}", ts[:10]) + \
            f"  archived: true\n  archived_at: '{ts}'\n  archived_from: {folder}\n"
        note = nf.registry_apply(lambda t: t.rstrip("\n") + "\n" + entry)
        notes.append(f"registry: + {slug} (archived entry — the folder had none) · {note}")
    try:
        import yaml
        yaml.safe_load(lp.REGISTRY.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        notes.append(f"WARNING: registry YAML parse failed after rewrite: {e}")
    # board: items done, codename retired
    try:
        import items as _items
        data = _items.load()
        n = 0
        for k, e in data["items"].items():
            if e.get("owner") == folder and e.get("status") != "done":
                e["status"] = "done"; e["outcome"] = f"archived {ts} -> {dest}"; e["updated"] = ts; n += 1
        _items.save(data)
        notes.append(f"items: {n} -> done")
    except Exception as e:  # noqa: BLE001
        notes.append(f"items: not updated ({e})")
    try:
        import tasknames
        name = codename_of(folder, "pointer", "workflow-state/current-pointer.md")
        if name:
            tasknames.retire(name, outcome=f"archived {ts[:10]} -> {dest}")
            notes.append(f"codename {name} retired")
    except Exception as e:  # noqa: BLE001
        notes.append(f"codename: not retired ({e})")
    # session binding: the folder now lives at dest
    sid = _sid()
    if sid:
        cur = lp.read_session(sid)
        if cur.get("window"):
            folders = [dest if f == folder else f for f in cur.get("folders", [])]
            lp.write_session(sid, cur["window"], folders, {k: v for k, v in cur.items()
                                                          if k not in ("window", "session_id", "bound", "folders")})
    return dest, notes


def archive_commit(folder: str, dest: str) -> str:
    """ONE commit for the move + the registry rewrite + the autorun-log line (the registry is already staged index-only)."""
    slug = _slugify(folder)
    _git("add", "-A", "--", dest)   # the deletions at the old path are already staged by git mv (a vanished pathspec would abort the add)
    r = _git("commit", "-m", f"{slug}: archived via the signoff (scripts/signoff.py) — {folder}/ -> {dest}/ (goal complete, 0 open items); registry entry archived")
    return ("commit: ok " + _git("rev-parse", "--short", "HEAD").stdout.strip()) if r.returncode == 0 else "commit: FAILED " + (r.stdout + r.stderr).strip()[:300]


def held_none(a: argparse.Namespace, st: dict) -> int:
    folder = (a.folder or "").replace("\\", "/").strip("/")
    d = decide(folder, a.handed_off) if folder else {"status": "done", "next": "python scripts/board.py menu", "open": [], "flagged": []}
    if a.handed_off:
        d["status"], d["next"] = "handed-off", f"python scripts/lock.py claim {a.handed_off} --task \"...\""
    window = st["window"] or (os.environ.get("ICM_WINDOW") or "").strip() or "unbound"
    note = f"session {window} ended holding nothing (status {d['status']}) — {a.decisions or 'no decisions'}; next: {d['next'][:160]}"
    if not a.dry_run:
        try:
            import autorun_log
            autorun_log.append(folder, f"session {window} (held: none)", "done", decisions=note, commit=a.commit or "-")
            print(f"autorun-log: +1 line in {folder or '<root>'}")
        except Exception as e:  # noqa: BLE001
            print(f"autorun-log: not written ({e})")
        if folder:
            ptr = TREE / folder / "workflow-state" / "current-pointer.md"
            locks = lp.locks_at(TREE / folder / ".goal")
            foreign = [li for li in locks if li.fresh and not (st["identity"] and lp.same_window(li.window, st["identity"].window))]
            if ptr.is_file() and not foreign:
                data = ptr.read_bytes()
                eol = "\r\n" if b"\r\n" in data else "\n"
                lines = data.decode("utf-8", "replace").replace("\r\n", "\n").split("\n")
                ins = 1 if lines and lines[0].startswith("#") else 0
                lines.insert(ins, "")
                lines.insert(ins + 1, f"> {mint.timestamp()} (session {window}, held: none): {a.decisions or 'no writes in this folder'} — next: {d['next'][:200]}")
                ptr.write_bytes(eol.join(lines).encode("utf-8"))
                print(f"pointer: session note added under the H1 of {folder}/workflow-state/current-pointer.md")
            elif foreign:
                print(f"pointer: {folder} is held by {foreign[0].window} — no note written (their folder while they hold it)")
    print()
    print(lc.format_terminal_block(d["status"], "none", d["next"]))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--folder", default="", help="the held folder (default: the one this session holds; required when two are held)")
    ap.add_argument("--held", default="", choices=["", "none"], help="`none`: the session held nothing")
    ap.add_argument("--decisions", default="", help="defaults decided this shift (§12), or none")
    ap.add_argument("--commit", default="", help="the shift's commit sha (default: HEAD)")
    ap.add_argument("--handed-off", dest="handed_off", default="", help="the lane the work moved to (status: handed-off)")
    ap.add_argument("--no-kick", action="store_true")
    ap.add_argument("--allow-dirty", default="", help="passed to lock.py release (say why)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    sid = _sid()
    try:
        st = lc.session_state(sid)
    except lp.IdentityConflict as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 5
    if a.held == "none" or st["state"] != "claimed":
        if st["state"] == "claimed" and a.held == "none":
            print(f"ERROR: --held none, but this window holds {', '.join(st['held'])} — run without --held (or release first)", file=sys.stderr)
            return 2
        return held_none(a, st)

    held = st["held"]
    folder = (a.folder or "").replace("\\", "/").strip("/") or (held[0] if len(held) == 1 else "")
    if not folder:
        print(f"ERROR: this window holds {', '.join(held)} — say which with --folder", file=sys.stderr)
        return 2
    if folder not in held:
        print(f"ERROR: {folder} is not held by this window ({', '.join(held) or 'nothing'})", file=sys.stderr)
        return 2
    me = st["identity"]
    ts = mint.timestamp()
    sha = a.commit or _git("rev-parse", "--short", "HEAD").stdout.strip()
    print(f"signoff {folder} · window {me.window} · commit {sha}")

    # 1. the pointer just written becomes the board item
    if not a.dry_run:
        r = _py(str(LIB / "items.py"), "from-pointer", folder)
        print(("items from-pointer: " + (r.stdout.strip().splitlines() or ["ok"])[-1][:160]) if r.returncode == 0
              else f"items from-pointer: rc={r.returncode} {(r.stdout + r.stderr).strip()[:200]}")
    # 2. status / next from the board
    staged = [rel for kind, rel in lp.read_handoffs(sid) if kind == "staged"] if sid else []
    d = decide(folder, a.handed_off, staged)
    print(f"board: {len(d['open'])} open · {len(d['flagged'])} waiting on the owner/parked · goal complete: {d['complete']} -> "
          f"status {d['status']} · archive: {d['archive']}")
    # 3. archive
    dest = folder
    if d["archive"]:
        dest, notes = archive(folder, ts, a.dry_run)
        for n in notes:
            print(f"  {n}")
        if not a.dry_run:
            d["next"] = "/next"
    # 4. autorun-log line — before the archive commit (it rides in it); after the release otherwise (its own small
    #    commit: workflow-state/ is writable without a lock, §17 exception, and HEAD must carry the record)
    name = codename_of(folder, "pointer", "workflow-state/current-pointer.md") or f"{folder}|pointer"
    status_word = "done" if d["archive"] else ("waiting_owner" if d["status"] == "blocked" else ("ready" if d["open"] else "done"))

    def log_line(target: str) -> str:
        try:
            import autorun_log
            path = autorun_log.append(target, name, status_word, decisions=a.decisions or "none", commit=sha)
            return lp.lock_rel(path)
        except Exception as e:  # noqa: BLE001
            print(f"autorun-log: not written ({e})")
            return ""

    if d["archive"] and not a.dry_run:
        log_line(dest)
        print(f"  {archive_commit(folder, dest)}")
    # 5. release
    if a.dry_run:
        print("(dry-run: no release, no board refresh)")
    else:
        args = [str(ICM / "lock.py"), "release", dest] + (["--allow-dirty", a.allow_dirty] if a.allow_dirty else [])
        r = _py(*args)
        sys.stdout.write(r.stdout)
        if r.returncode != 0:
            sys.stderr.write(r.stderr)
            print(f"RELEASE REFUSED (rc={r.returncode}) — fix what lock.py listed, then run signoff.py again. No terminal block: the turn may not end yet.")
            return 6
        if not d["archive"]:
            logrel = log_line(dest)
            if logrel:
                _git("add", "--", logrel)
                rc = _git("commit", "-q", "-m", f"{folder}: autorun-log line for window {me.window} ({status_word})")
                print(f"autorun-log: +1 line ({status_word}) · " + ("committed" if rc.returncode == 0 else f"left uncommitted ({(rc.stdout + rc.stderr).strip()[:120]})"))
        # 6. board + mirror + kick
        r = _py(str(ICM / "board.py"), "--signpost")
        print("board --signpost: " + ("ok" if r.returncode == 0 else f"rc={r.returncode} {(r.stdout + r.stderr).strip()[-200:]}"))
        if not a.no_kick:
            runner = os.environ.get("AUTORUN_RUNNER", "")
            if runner:
                try:
                    subprocess.Popen([sys.executable, str(ICM / "autorun.py"), "--runner", runner, "--detach"], cwd=ROOT,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    print("autorun loop: kicked (detached)")
                except OSError as e:
                    print(f"autorun loop: kick failed ({e})")
            else:
                print("autorun loop: not kicked (set AUTORUN_RUNNER to the agent command, or run scripts/autorun.py yourself)")
    # 7. the block — held is none after the release
    print()
    print(lc.format_terminal_block(d["status"], "none" if not a.dry_run else folder, d["next"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
