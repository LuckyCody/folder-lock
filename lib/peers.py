"""Who holds which lock — folder → window → Claude session → peer address (PROTOCOL §16, v4.2).

    python lib/peers.py            table of every lock with its holder's ListAgents name + liveness
    python lib/peers.py --json     same as JSON (board.py / lock.py consume the library functions)
    python lib/peers.py --peers    every Claude Code process registered on this machine
    python lib/peers.py <window>   resolve one window to its holder

Three sources, joined here and nowhere else:
  1. `<folder>/.goal/LOCK.yaml` / `.firing.lock`      folder → window            (lockpath.locks_at)
  2. `<STATE_ROOT>/sessions/<sid>.yaml`               window → Claude session id (lock.py claim/adopt binding)
  3. `~/.claude/sessions/<pid>.json`                  session id → peer NAME (the ListAgents / SendMessage
     address), pid, cwd — written by Claude Code itself for every live interactive process on this machine
     (honours CLAUDE_CONFIG_DIR).
Liveness is the pid (OpenProcess on Windows, kill(0) elsewhere) — a registry file whose process is gone reads as
"gone", a window bound on this host with no registry entry reads as "exited / plain terminal", a window with no
binding at all reads as "other host / headless agent". Nothing here writes anything.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lockpath as lp  # noqa: E402

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".next", "dist", "build", ".githooks", "_archive"}


def _claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))


def pid_alive(pid: int) -> Optional[bool]:
    """True/False, or None when it cannot be determined (no rights, odd platform)."""
    if not pid:
        return None
    if os.name == "nt":
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not h:
                return False if k32.GetLastError() == 87 else None  # 87 = invalid parameter = no such pid
            try:
                code = ctypes.c_ulong()
                if k32.GetExitCodeProcess(h, ctypes.byref(code)):
                    return code.value == 259  # STILL_ACTIVE
                return None
            finally:
                k32.CloseHandle(h)
        except Exception:
            return None
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return None


def registry() -> list:
    """Every Claude Code process registry record on this machine (`~/.claude/sessions/<pid>.json`)."""
    out = []
    d = _claude_dir() / "sessions"
    try:
        files = sorted(d.glob("*.json"))
    except OSError:
        return out
    for f in files:
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        try:
            pid = int(rec.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        out.append({
            "pid": pid,
            "session_id": rec.get("sessionId", ""),
            "name": rec.get("name", ""),
            "kind": rec.get("kind", ""),
            "cwd": rec.get("cwd", ""),
            "entrypoint": rec.get("entrypoint", ""),
            "started_ms": rec.get("startedAt", 0),
            "alive": pid_alive(pid),
            "file": str(f),
        })
    return out


def by_session_id() -> dict:
    return {r["session_id"]: r for r in registry() if r["session_id"]}


def window_bindings() -> dict:
    """window (lower) → session id, from this host's lock.py bindings. Newest binding wins on a clash."""
    out, stamp = {}, {}
    try:
        files = list(lp.SESSIONS.glob("*.yaml"))
    except OSError:
        return out
    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        m = re.search(r'^window:\s*"?([^"\n]+)"?', text, re.M)
        if not m:
            continue
        w = m.group(1).strip().lower()
        b = re.search(r'^bound:\s*"?([^"\n]+)"?', text, re.M)
        when = b.group(1) if b else ""
        if w not in out or when > stamp.get(w, ""):
            out[w], stamp[w] = f.stem, when
    return out


def holder(window: str, bindings: Optional[dict] = None, reg: Optional[dict] = None) -> dict:
    """Resolve one window: {session_id, name, pid, alive, state, address, hint}.
    state: live | gone | unbound-here | exited | fired | unknown"""
    bindings = window_bindings() if bindings is None else bindings
    reg = by_session_id() if reg is None else reg
    sid = bindings.get((window or "").strip().lower(), "")
    if not sid:
        return {"session_id": "", "name": "", "pid": 0, "alive": None, "state": "unbound-here",
                "address": "", "hint": "no binding on this host — other host, headless agent, or plain terminal"}
    r = reg.get(sid)
    if not r:
        fired = (window or "").lower().startswith("fired-")
        return {"session_id": sid, "name": "", "pid": 0, "alive": False,
                "state": "fired" if fired else "exited", "address": "",
                "hint": ("runner-fired agent — not addressable; wait for its .firing.lock to clear or stage a handoff"
                         if fired else "bound here but no Claude Code process registered — session exited")}
    alive = r["alive"]
    state = "live" if alive else ("gone" if alive is False else "unknown")
    return {"session_id": sid, "name": r["name"], "pid": r["pid"], "alive": alive, "state": state,
            "address": r["name"] if alive else "", "hint": "" if alive else "process is gone — lock is orphaned"}


def me() -> dict:
    """This session's own registry record (by CLAUDE_CODE_SESSION_ID), or {}."""
    sid = (os.environ.get("CLAUDE_CODE_SESSION_ID") or "").strip()
    return by_session_id().get(sid, {}) if sid else {}


def _walk_locks(root: Path) -> list:
    """(folder_rel, LockInfo) for every .goal lock under root."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel = Path(dirpath).relative_to(root)
        if len(rel.parts) > 6:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        if Path(dirpath).name == ".goal":
            folder = Path(dirpath).parent.relative_to(root).as_posix()
            for li in lp.locks_at(Path(dirpath)):
                out.append((folder if folder != "." else "<root>", li))
            dirnames[:] = []
    return out


def who(root: Optional[Path] = None) -> list:
    """One row per lock: folder, kind, window, status, fresh, age, task + the resolved holder."""
    root = root or lp.ROOT
    bindings, reg = window_bindings(), by_session_id()
    mine = (os.environ.get("CLAUDE_CODE_SESSION_ID") or "").strip()
    rows = []
    for folder, li in _walk_locks(root):
        h = holder(li.window, bindings, reg)
        h["mine"] = bool(mine) and h["session_id"] == mine
        secs = int(li.age.total_seconds())
        rows.append({
            "folder": folder, "kind": li.kind, "window": li.window, "status": li.status,
            "fresh": li.fresh, "age": f"{secs // 3600}h{(secs % 3600) // 60:02d}m" if secs < 86400 else f"{secs // 86400}d",
            "task": li.task, "holder": h,
        })
    rows.sort(key=lambda r: r["folder"])
    return rows


def label(h: dict) -> str:
    """Short holder tag for one-line renders: 'my-repo-61 · live' / 'gone (pid 1234)' / 'unbound here'."""
    if h.get("mine"):
        return f"{h['name']} · YOU"
    st = h.get("state")
    if st == "live":
        return f"{h['name']} · live"
    if st == "gone":
        return f"{h['name'] or h['session_id'][:8]} · GONE (pid {h['pid']})"
    if st == "exited":
        return f"session {h['session_id'][:8]} · exited"
    if st == "fired":
        return f"headless agent · session {h['session_id'][:8]} (not addressable)"
    if st == "unknown":
        return f"{h['name']} · alive?"
    return "unbound here (other host / headless agent)"


def render(rows: list) -> str:
    if not rows:
        return "no locks anywhere under the repo"
    w_f = max(6, max(len(r["folder"]) for r in rows))
    w_w = max(6, max(len(r["window"]) for r in rows))
    lines = [f"{'folder':<{w_f}}  {'window':<{w_w}}  {'status':<8} {'age':>6}  holder → message this address"]
    for r in rows:
        st = r["status"] + ("" if r["fresh"] else "/STALE")
        lines.append(f"{r['folder']:<{w_f}}  {r['window']:<{w_w}}  {st:<8} {r['age']:>6}  {label(r['holder'])}")
    m = me()
    lines.append("")
    lines.append(f"you are: {m.get('name') or '(not in the registry — plain terminal?)'} · session "
                 f"{m.get('session_id', '')[:8] or (os.environ.get('CLAUDE_CODE_SESSION_ID') or '')[:8]}")
    return "\n".join(lines)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] not in ("--json", "--peers"):
        h = holder(argv[0])
        print(json.dumps(h, indent=2) if "--json" in argv else f"{argv[0]} → {label(h)}"
              + (f"  (session {h['session_id']})" if h.get("session_id") else ""))
        return 0 if h.get("state") == "live" else 1
    if "--peers" in argv:
        for r in registry():
            print(f"{r['name']:<28} pid {r['pid']:<6} {'live' if r['alive'] else 'GONE'}  session {r['session_id']}")
        return 0
    rows = who()
    if "--json" in argv:
        print(json.dumps({"locks": rows, "peers": registry()}, indent=2, default=str))
    else:
        print(render(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
