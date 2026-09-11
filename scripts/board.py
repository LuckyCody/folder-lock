"""Open-work board (PROTOCOL.md §6, §13, §14) — one screen of everything in flight. Renders never write.

  python scripts/board.py                    # classic full board (human view)
  python scripts/board.py json [--no-touch]  # machine view: {"folders", "deploys", "locks", "items", "digest"}
  python scripts/board.py menu [--no-touch] [--no-kick] [--all]
                                             # the owner's menu, gated on the §13 exit condition:
                                             #   ready > 0  -> digest · waiting-on-owner · locks · deploys · ONE line
                                             #                 saying the ready items are agent work (+ kick result)
                                             #   ready == 0 -> the full board — the owner decides
  python scripts/board.py --signpost         # full board + rewrite the TRACKED signpost .folder-lock/next-session.md
                                             # (a signoff artifact — never a side effect of looking at the board)

Per workfolder: the lock (window, status — `closing` = signing off, not stale — age, fresh/stale, and the holder's
peer address via lib/peers.py, §16), the pointer's `Next concrete action:` classified by the grammar (actionable /
tripwire / parked / closed / mute), staged handoffs in `.goal/inbox/`, the item status overlay (lib/items.py,
derived in memory — the store is NOT written), the deploy queue (§11) and the autorun digest since the owner
last looked (lib/autorun_log.py).

State writes happen only on `touch` (the default for `json` and `menu` = the owner looked): the digest window
closes (`last_seen`), and a session with no identity gets a READER binding so the write has an author. Every render
writes the host-local signpost copy `<STATE_ROOT>/next-session.md`; the tracked copy moves only with --signpost.
`menu` kicks the autorun loop detached when FOLDER_LOCK_RUNNER names the agent command (never blocks).
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lib"))
import lockpath as lp  # noqa: E402
import statestore  # noqa: E402

statestore.READONLY = True   # §14: a render never writes state; touch flips it for the ONE seen-stamp write

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".next", "dist", "build", ".githooks", "_archive"}   # archived folders are never on the board (§17)
WIDTH = 110
SIGNPOST_TRACKED = lp.ROOT / ".folder-lock" / "next-session.md"


def fmt_age(td) -> str:
    h, rem = divmod(int(td.total_seconds()), 3600)
    return f"{h}h{rem // 60:02d}m" if h < 48 else f"{h // 24}d"


def _fit(s: str, n: int = WIDTH) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _first_line(p: Path) -> str:
    try:
        for raw in p.read_text(encoding="utf-8", errors="replace").splitlines():
            if raw.strip():
                return raw.strip()
    except OSError:
        pass
    return ""


def _note_task(p: Path) -> str:
    try:
        m = re.search(r'^task:\s*"?(.*?)"?\s*$', p.read_text(encoding="utf-8", errors="replace"), re.M)
        return m.group(1) if m else p.stem
    except OSError:
        return p.stem


def classify(action: str):
    a = action.strip(); low = a.lower()
    if not a:
        return "mute", ""
    if low.startswith("when "):
        return "tripwire", a
    if low.startswith("parked"):
        return "parked", a
    if low.startswith("none"):
        return "closed", a
    return "actionable", a


def read_pointer(folder: Path):
    ptr = folder / "workflow-state" / "current-pointer.md"
    if not ptr.is_file():
        return None
    m = re.search(r"^next concrete action:\s*(.*)$", ptr.read_text(encoding="utf-8", errors="replace"), re.I | re.M)
    kind, line = classify(m.group(1) if m else "")
    return {"kind": kind, "action": line, "age": fmt_age(datetime.now() - datetime.fromtimestamp(ptr.stat().st_mtime))}


def _holder_label(window: str) -> str:
    try:
        import peers
        return peers.label(peers.holder(window))
    except Exception:  # noqa: BLE001
        return ""


def scan(root: Path) -> list:
    items: dict = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        p = Path(dirpath)
        if p.name == ".goal":
            folder = p.parent
            rel = folder.relative_to(root).as_posix() or "."
            it = items.setdefault(rel, {"folder": rel})
            locks = lp.locks_at(p)
            if locks:
                it["locks"] = [{"kind": li.kind, "window": li.window, "status": li.status, "task": li.task,
                                "age": fmt_age(li.age), "fresh": li.fresh,
                                "holder_label": _holder_label(li.window) if li.kind == "interactive" else "headless agent"}
                               for li in locks]
            inbox = p / "inbox"
            if inbox.is_dir():
                # a note whose first line is "# CONSUMED ..." is a placeholder (consumed, left for the
                # folder's next visitor to delete — PROTOCOL §2), never open work (v4.1)
                notes = [f for f in sorted(inbox.glob("*.staged.md")) if not _first_line(f).upper().startswith("# CONSUMED")]
                it["handoffs"] = [f.name for f in notes]
                it["handoff_tasks"] = {f.name: _note_task(f) for f in notes}
            dirnames[:] = []
        elif p.name == "workflow-state" and "current-pointer.md" in filenames:
            rel = p.parent.relative_to(root).as_posix() or "."
            items.setdefault(rel, {"folder": rel})["pointer"] = read_pointer(p.parent)
            dirnames[:] = []
    return sorted(items.values(), key=lambda x: x["folder"])


def deploy_queue() -> list:
    """PROTOCOL.md §11 — per deploy unit: pending requests, last deploy, FAILED / BLOCKED flags written by
    scripts/deployer.py. Read-only; the board never fires a deploy."""
    try:
        import deployunits as U
        units = U.load_units()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for u in units.values():
        reqs = sorted((U.REQUESTS / u.name).glob("*.yaml")) if (U.REQUESTS / u.name).is_dir() else []
        flags = {}
        for kind in ("last", "failed", "blocked"):
            p = U.STATE / kind / f"{u.name}.yaml"
            flags[kind] = lp.parse_lock_text(p.read_text(encoding="utf-8", errors="replace")) if p.is_file() else {}
        out.append({"unit": u.name, "pending": len(reqs),
                    "oldest_pending": fmt_age(datetime.now() - datetime.fromtimestamp(reqs[0].stat().st_mtime)) if reqs else "",
                    "last_deployed_at": flags["last"].get("deployed_at", ""),
                    "last_commit": (flags["last"].get("deployed_commit", "") or "")[:7],
                    "failed": bool(flags["failed"]), "failed_at": flags["failed"].get("failed_at", ""),
                    "fail_rc": flags["failed"].get("rc", ""), "retry_after": flags["failed"].get("retry_after", ""),
                    "fail_log": flags["failed"].get("log", ""),
                    "blocked": bool(flags["blocked"]), "blocked_reason": flags["blocked"].get("reason", ""),
                    "blocked_since": flags["blocked"].get("since", "")})
    return out


def item_overlay(folders: list) -> dict:
    """The §13 status overlay derived IN MEMORY over the scanned rows (items.sync under READONLY merges, never
    persists). Returns {"live": {key: rec}, "ready": n, "in_progress": n, "waiting_owner": {owner: [rec]}}."""
    try:
        import items
    except Exception:  # noqa: BLE001
        return {"live": {}, "ready": 0, "in_progress": 0, "waiting_owner": {}}
    rows = []
    for it in folders:
        f = it["folder"]
        ptr = it.get("pointer")
        if ptr is not None:
            line = items.pointer_line(f if f != "." else "")
            rows.append({"folder": f, "kind": "pointer", "ref": "workflow-state/current-pointer.md",
                         "title": line or "(pointer has no action line)", "line": line})
        for name in it.get("handoffs", []):
            rows.append({"folder": f, "kind": "handoff", "ref": name, "title": it.get("handoff_tasks", {}).get(name, name)})
    try:
        live = items.sync(rows)
    except Exception:  # noqa: BLE001
        live = {}
    waiting: dict = {}
    for k, e in live.items():
        if e.get("status") == "waiting_owner":
            waiting.setdefault(e.get("owner", "?"), []).append(dict(key=k, **e))
    return {"live": live,
            "ready": sum(1 for e in live.values() if e.get("status") == "ready"),
            "in_progress": sum(1 for e in live.values() if e.get("status") == "in_progress"),
            "waiting_owner": dict(sorted(waiting.items()))}


def build_view() -> dict:
    folders = scan(lp.ROOT)
    deploys = deploy_queue()
    ov = item_overlay(folders)
    try:
        import autorun_log as alog
        digest = alog.digest(alog.last_seen())
    except Exception:  # noqa: BLE001
        digest = []
    locks = [dict(folder=it["folder"], **lk) for it in folders for lk in it.get("locks", [])]
    return {"generated": datetime.now().strftime("%Y-%m-%d %H:%M"), "folders": folders, "deploys": deploys,
            "locks": locks, "items": {"ready": ov["ready"], "in_progress": ov["in_progress"],
                                      "waiting_owner": ov["waiting_owner"], "live": ov["live"]},
            "digest": digest}


# ----------------------------------------------------------------------------- renders

def _sec(title: str, rows: list) -> list:
    return ["", title] + [f"  {r}" for r in rows] if rows else []


def _sec_header(v: dict) -> list:
    i = v["items"]
    n_wait = sum(len(x) for x in i["waiting_owner"].values())
    live = [d for d in v["deploys"] if d["pending"] or d["failed"] or d["blocked"]]
    return [f"board {v['generated']} · {i['ready']} ready · {i['in_progress']} running · {n_wait} on the owner · "
            f"{len(v['locks'])} lock(s) · {len(v['digest'])} autorun line(s) since you looked"
            + (f" · {len(live)} deploy unit(s) live" if live else "")]


def _sec_digest(v: dict) -> list:
    dig = v["digest"]
    rows = [_fit(f"{d['ts'][5:]} {d['folder']} · {d['item']} · {d['status']} · {d['decisions']}") for d in dig[-12:]]
    if len(dig) > 12:
        rows.insert(0, f"… {len(dig) - 12} older line(s) in the folders' autorun-log.md")
    return _sec(f"✓ autorun since you last looked ({len(dig)} item(s))", rows)


def _sec_waiting(v: dict) -> list:
    rows = []
    for owner, lst in v["items"]["waiting_owner"].items():
        for r in lst:
            rows.append(_fit(f"{owner} · {r.get('title', '')[:70]} · since {r.get('blocked_since', '?')}"))
            rows.append(_fit(f"   Q: {r.get('question', '')}{' (auto-drafted)' if r.get('question_auto') else ''}"))
    return _sec("⏸ waiting on the owner (decision-ready — a one-line answer unblocks each)", rows)


def _sec_locks(v: dict) -> list:
    rows = []
    for lk in v["locks"]:
        state = "signing off" if lk["status"] == "closing" else ("LOCKED" if lk["fresh"] else "stale")
        holder = f" → {lk['holder_label']}" if lk.get("holder_label") else ""
        rows.append(_fit(f"{lk['folder']} — {state} {lk['window']} ({lk['kind']}, {lk['age']}){holder}"))
    return _sec("🔒 in progress elsewhere (→ = holder to message, PROTOCOL §16)", rows)


def _sec_deploys(v: dict) -> list:
    rows = []
    for d in (x for x in v["deploys"] if x["pending"] or x["failed"] or x["blocked"]):
        line = f"{d['unit']} — {d['pending']} pending" + (f" (oldest {d['oldest_pending']})" if d["pending"] else "")
        if d["last_deployed_at"]:
            line += f" · last {d['last_deployed_at']} @ {d['last_commit']}"
        rows.append(_fit(line))
        if d["failed"]:
            rows.append(_fit(f"  FAILED {d['failed_at']} rc={d['fail_rc']} · retry after {d['retry_after']} · log {d['fail_log']}"))
        if d["blocked"]:
            rows.append(_fit(f"  BLOCKED since {d['blocked_since']}: {d['blocked_reason']}"))
    return _sec("🚀 deploy queue (PROTOCOL §11 — `python scripts/deployer.py status`; nobody deploys by hand)", rows)


def _sec_folders(v: dict) -> list:
    rows, mute = [], []
    live = v["items"]["live"]
    for it in v["folders"]:
        ptr = it.get("pointer"); locks = it.get("locks", [])
        if ptr and ptr["kind"] == "closed" and not locks and not it.get("handoffs"):
            continue
        if ptr and ptr["kind"] == "mute":
            mute.append(it["folder"])
        tags = []
        for lk in locks:
            state = "signing off" if lk["status"] == "closing" else ("LOCKED" if lk["fresh"] else "stale lock")
            tags.append(f"[{state} {lk['window']} {lk['kind']} {lk['age']}" + (f" → {lk['holder_label']}" if lk.get("holder_label") else "") + "]")
        if it.get("handoffs"):
            tags.append(f"[{len(it['handoffs'])} handoff(s) staged]")
        st = (live.get(f"{it['folder']}|pointer|workflow-state/current-pointer.md") or {}).get("status")
        if st and st not in ("done",):
            tags.append(f"[{st}]")
        if ptr and ptr["kind"] == "tripwire":
            tags.append("[tripwire]")
        if ptr and ptr["kind"] == "parked":
            tags.append("[PARKED]")
        rows.append((f"**{it['folder']}** " + " ".join(tags)).rstrip())
        if ptr and ptr["kind"] not in ("mute", "closed"):
            rows.append(f"    -> {ptr['action'][:160]}")
        elif ptr is None and locks:
            rows.append(f"    -> (no current-pointer.md) lock task: {locks[0]['task']}")
        for name in it.get("handoffs", []):
            rows.append(f"    📦 {it.get('handoff_tasks', {}).get(name, name)[:140]}")
    out = ["", "## Folders"] + [f"- {r}" if not r.startswith("    ") else r for r in rows] if rows else []
    if mute:
        out += ["", f"{len(mute)} pointer(s) with no 'Next concrete action:' line (mute — fix them): {', '.join(mute)}"]
    return out


def render_full(v: dict) -> str:
    live = [d for d in v["deploys"] if d["pending"] or d["failed"] or d["blocked"]]
    if not v["folders"] and not live:
        return "board: nothing in flight (no locks, pointers, handoffs, or deploy requests)."
    L = [f"# Open-work board — {v['generated']}"] + _sec_header(v) + _sec_digest(v) + _sec_waiting(v) \
        + _sec_folders(v) + _sec_locks(v) + _sec_deploys(v)
    return "\n".join(L)


def render_menu(v: dict, kick_note: str = "") -> str:
    """The owner's menu, gated on the autorun exit condition (PROTOCOL §13):
      ready > 0  → the machine still has work: header · digest · waiting-on-owner · locks · deploys ·
                   ONE line saying the ready items are agent work (+ whether the loop was kicked).
      ready == 0 → the exit condition: the full board, the owner decides."""
    if v["items"]["ready"] == 0:
        return render_full(v) + "\n\n◦ exit condition: 0 ready items — everything above waits on the owner or the world (§13)"
    folders = {k.split("|", 1)[0] for k, e in v["items"]["live"].items() if e.get("status") == "ready"}
    L = _sec_header(v) + _sec_digest(v) + _sec_waiting(v) + _sec_locks(v) + _sec_deploys(v)
    L += ["", _fit(f"▸ {v['items']['ready']} ready item(s) across {len(folders)} folder(s) are agent work — "
                   f"{kick_note or 'loop not kicked'} · `python scripts/board.py` shows them")]
    return "\n".join(L)


def signpost_state_path() -> Path:
    return lp.STATE_ROOT / "next-session.md"


def write_signpost(v: dict, tracked: bool = False) -> None:
    """Generated signpost: folders with open pointers -> next action. Every render writes the host-local state copy;
    only --signpost (the signoff) rewrites the TRACKED .folder-lock/next-session.md — a briefing is a signoff
    artifact, never a side effect of looking at the board (v4.2)."""
    lines = [f"# Signpost — open work (generated {v['generated']} by scripts/board.py; never hand-edit)", "",
             "Fresh session: `python scripts/board.py menu` renders this live; claim by path (`lock.py claim <folder>`),",
             "start at the folder's `Next concrete action:`. Protocol: PROTOCOL.md.", ""]
    live = v["items"]["live"]
    for it in v["folders"]:
        ptr = it.get("pointer")
        if not ptr or ptr["kind"] in ("closed", "mute"):
            continue
        marks = []
        if it.get("locks"):
            marks.append("LOCKED")
        if it.get("handoffs"):
            marks.append(f"{len(it['handoffs'])} handoff(s) staged")
        if ptr["kind"] == "tripwire":
            marks.append("tripwire")
        st = (live.get(f"{it['folder']}|pointer|workflow-state/current-pointer.md") or {}).get("status")
        if st and st != "done":
            marks.append(st)
        suffix = f" [{' · '.join(marks)}]" if marks else ""
        lines.append(f"- **{it['folder']}** ({ptr['age']}){suffix} → {_fit(ptr['action'], 200)}")
    for d in v["deploys"]:
        if d["failed"]:
            lines.append(f"- ⚠ **deploy FAILED** `{d['unit']}` {d['failed_at']} (rc={d['fail_rc']}) → read {d['fail_log']}, fix on main, "
                         f"commit; the deployer retries after {d['retry_after']} (or `python scripts/deployer.py --retry-now`)")
        elif d["blocked"]:
            lines.append(f"- ⛔ **deploy BLOCKED** `{d['unit']}` since {d['blocked_since']} — {_fit(d['blocked_reason'], 150)}")
    text = "\n".join(lines) + "\n"
    try:
        sp = signpost_state_path(); sp.parent.mkdir(parents=True, exist_ok=True); sp.write_text(text, encoding="utf-8")
    except OSError as e:
        print(f"(signpost state copy not written: {e})", file=sys.stderr)
    if tracked:
        try:
            SIGNPOST_TRACKED.parent.mkdir(parents=True, exist_ok=True)
            SIGNPOST_TRACKED.write_text(text, encoding="utf-8")
        except OSError as e:
            print(f"(tracked signpost not written: {e})", file=sys.stderr)


# ----------------------------------------------------------------------------- modes

def _reader_identity() -> str:
    """A touch render is the owner looking — its state write needs an author. A session with no identity gets a
    READER binding (no lock, no folder; lock.py reader); a bound session keeps its window."""
    try:
        sid = (os.environ.get("CLAUDE_CODE_SESSION_ID") or "").strip()
        me = lp.identity(session_id=sid)
        if me:
            return me.window
        if not sid:
            return ""
        sys.path.insert(0, str(HERE))
        import lock as lockcli
        return lockcli.bind_reader(sid, hint="menu") or ""
    except Exception as e:  # noqa: BLE001
        print(f"(reader identity not bound: {e})", file=sys.stderr)
        return ""


def _touch(window: str, v: dict, mode: str, kick: bool) -> None:
    """The ONE state write of a render: close the digest window (last_seen). Guard-logged with the author."""
    try:
        import autorun_log as alog
        statestore.READONLY = False
        alog.mark_seen()
    except Exception as e:  # noqa: BLE001
        print(f"(seen stamp not written: {e})", file=sys.stderr)
    finally:
        statestore.READONLY = True
    lp.guard_log({"guard": "board", "event": mode, "window": window, "touch": True, "kick": kick,
                  "ready": v["items"]["ready"], "waiting_owner": sum(len(x) for x in v["items"]["waiting_owner"].values())})


def _kick_loop() -> str:
    """Kick the autorun pass (PROTOCOL §13) detached; return the one-line note for the ▸ line."""
    runner = os.environ.get("FOLDER_LOCK_RUNNER", "").strip()
    if not runner:
        return "loop not kicked (set FOLDER_LOCK_RUNNER=\"<agent command>\" to kick scripts/autorun.py from the menu)"
    import subprocess
    try:
        r = subprocess.run([sys.executable, str(HERE / "autorun.py"), "--runner", runner, "--detach"], cwd=str(lp.ROOT),
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    except Exception as e:  # noqa: BLE001
        return f"loop not kicked ({type(e).__name__}: {e})"
    if r.returncode != 0:
        return f"loop not kicked (rc={r.returncode}: {(r.stderr or r.stdout).strip()[:80]})"
    return "loop kicked (scripts/autorun.py --detach)"


def main() -> int:
    argv = sys.argv[1:]
    cmd = argv[0] if argv and not argv[0].startswith("--") else ""
    touch = "--no-touch" not in argv
    v = build_view()
    write_signpost(v, tracked="--signpost" in argv)
    if cmd == "json":
        window = _reader_identity() if touch else ""
        out = {k: v[k] for k in ("generated", "folders", "deploys", "locks", "digest")}
        out["items"] = {"ready": v["items"]["ready"], "in_progress": v["items"]["in_progress"],
                        "waiting_owner": v["items"]["waiting_owner"]}
        print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
        if touch:
            _touch(window, v, "json", False)
        return 0
    if cmd == "menu":
        full = "--all" in argv
        kick = "--no-kick" not in argv and not full
        window = _reader_identity() if touch else ""
        note = _kick_loop() if (kick and v["items"]["ready"] > 0) else ("kick skipped (--no-kick)" if not kick and not full else "")
        print(render_full(v) if full else render_menu(v, note))
        if touch:
            _touch(window, v, "menu", note.startswith("loop kicked"))
        return 0
    if cmd:
        print(f"unknown subcommand: {cmd} (expected: menu | json, or no args / --signpost)", file=sys.stderr)
        return 2
    print(render_full(v))
    return 0


if __name__ == "__main__":
    sys.exit(main())
