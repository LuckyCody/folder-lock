"""Shared lock resolver + reader + identity — ONE implementation every guard imports
(PROTOCOL.md §1, §1b, §5, §10) so the edit-time guard, the commit guard, the Stop
hook and the lock writer can never disagree about "which lock guards this path"
or "who am I".

Resolution for a repo-relative path:
  1. optional registry `<repo>/.folder-lock/registry.yaml`
       workflows:
       - id: payroll
         owns: [finance/payroll/**, .claude/skills/payroll/**]
     -> the workflow's HOME folder (fixed prefix of its first glob; longest match wins
     when workflows nest). Lock lives at <home>/.goal/LOCK.yaml.
  2. top-level files, `.claude/**`, `.github/**` -> repo-root lock `<repo>/.goal/LOCK.yaml`
  3. otherwise the nearest ancestor folder that already has a `.goal/` dir
  4. otherwise UNGUARDED: the file's own folder (capped at 3 segments) has no `.goal/`.
     Every guard fails CLOSED on this — "claim it, which creates .goal/".
  A registry file that exists but cannot be parsed -> RegistryUnreadable (fail closed).

Identity:
  a) ICM_WINDOW env var (plain terminals, headless agents launched with it set)
  b) session binding `<STATE_ROOT>/sessions/<session_id>.yaml` written by
     `scripts/lock.py claim|adopt`; session_id = hook stdin `session_id`, or in
     Bash-run scripts Claude Code's CLAUDE_CODE_SESSION_ID env var.
  Both present and different -> IdentityConflict. Neither -> None (guards refuse).
  The binding YAML is identity only. Handoff records (`staged <path>` / `consumed <path>`)
  live in the append-only sidecar `<session_id>.handoffs.txt` next to it (v4.1) — the YAML
  is rewritten on every claim/release and must never carry a ledger.
  A binding with `kind: reader` and no folders (v4.2, `lock.py reader`) is an identity WITHOUT a
  lock domain: a menu-only session's state writes carry an author, but it may not edit anything.

Two roots (v4.2, PROTOCOL §14):
  ROOT        the working tree. Locks (`.goal/LOCK.yaml`, `.firing.lock`), inbox notes,
              `workflow-state/` and the deploy queue (`.goal/deploy/`) stay here on purpose —
              the edit guard must work offline and fail closed.
  STATE_ROOT  host-local runtime state that must NOT ride a file-sync tool: session bindings
              + handoff sidecars (`sessions/`), `guard_log.jsonl`, `selftest_last.json`, the
              state-store cache/outbox/file backend (`lib/statestore.py`), the signpost copy.
              `FOLDER_LOCK_STATE_ROOT`, else `%LOCALAPPDATA%\\folder-lock\\<repo-hash>` (Windows) /
              `$XDG_STATE_HOME|~/.local/state/folder-lock/<repo-hash>`. A binding still sitting at the
              pre-4.2 tree location `<repo>/.goal/sessions/` is copied over lazily on first use.

Literal locks (v4.1): a folder claimed as its own lock domain and later folded into another
  home by a registry edit keeps its `LOCK.yaml`. `literal_lock()` finds the closest fresh
  interactive lock at the path's own folder (or an ancestor below the resolved home); every
  guard honours it — "the closest existing lock wins": its holder may edit, everyone else
  (including the holder of the registry home) is refused.

Freshness: LOCK.yaml stale after 24h (by `started:`), .firing.lock stale after 15min
(by mtime). Missing `status:` = open (pre-status locks). Regex parsing — no PyYAML.
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

for _s in (sys.stdout, sys.stderr):  # Windows consoles default to cp1252; guards must never crash on a char
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _detect_root() -> Path:
    env = os.environ.get("FOLDER_LOCK_ROOT")
    if env:
        return Path(env).resolve()
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace").stdout.strip()
        if out:
            return Path(out).resolve()
    except OSError:
        pass
    return Path.cwd().resolve()


ROOT = _detect_root()
# The LOCK TREE (v4.3): where `.goal/` (locks, .firing.lock, inbox notes, deploy queue) lives. Code may run from a git
# worktree whose gitignored `.goal/` dirs do not exist (a headless agent on its own branch) — FOLDER_LOCK_LOCK_TREE points
# such a process at the canonical checkout so every guard still sees ONE set of locks. Default: the code root itself.
LOCK_TREE = Path(os.environ["FOLDER_LOCK_LOCK_TREE"]).resolve() if os.environ.get("FOLDER_LOCK_LOCK_TREE") else ROOT
ROOT_LOCK_DIR = LOCK_TREE / ".goal"
STATE = LOCK_TREE / ".goal"                  # tree-side runtime carrier (locks, inbox, deploy queue) — gitignored via **/.goal/


def lock_rel(p) -> str:
    """Lock-tree-relative POSIX path for messages — a lock path is never relative to a worktree root
    (`.relative_to(ROOT)` raised ValueError from a worktree and crashed the very release that would have ended it; v4.3)."""
    try:
        return Path(p).resolve().relative_to(LOCK_TREE).as_posix()
    except ValueError:
        return Path(p).as_posix()
LEGACY_SESSIONS = STATE / "sessions"         # pre-4.2 binding location: read-only fallback + lazy copy
LEGACY_INBOX_INDEX = STATE / "inboxes.txt"   # pre-4.2 inbox index: migrated into the state store (lib/statestore.py)
REGISTRY = ROOT / ".folder-lock" / "registry.yaml"


def repo_hash() -> str:
    """Stable 12-hex id of this working tree (case-folded, forward slashes) — keys the host-local state root."""
    return hashlib.sha1(str(ROOT).replace("\\", "/").lower().encode("utf-8")).hexdigest()[:12]


def _detect_state_root() -> Path:
    env = os.environ.get("FOLDER_LOCK_STATE_ROOT")
    if env:
        return Path(env)
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else Path.home() / ".local" / "state"
    return root / "folder-lock" / repo_hash()


STATE_ROOT = _detect_state_root()
SESSIONS = STATE_ROOT / "sessions"


def _host() -> str:
    h = os.environ.get("FOLDER_LOCK_HOST") or os.environ.get("COMPUTERNAME") or ""
    if not h:
        try:
            h = socket.gethostname()
        except Exception:
            h = ""
    return (h or "host").upper()


HOST = _host()

LOCK_FRESH = timedelta(hours=24)
FIRING_FRESH = timedelta(minutes=15)
ROOT_SPANNING_PREFIXES = (".claude", ".github", ".folder-lock", ".githooks")
TS_FMT = "%Y-%m-%dT%H:%M"


class RegistryUnreadable(Exception):
    pass


class IdentityConflict(Exception):
    pass


# ----------------------------------------------------------------------------- registry

@dataclass
class Workflow:
    id: str
    globs: list
    home: Optional[str]


def _fixed_prefix(glob: str) -> str:
    g = glob.split("#", 1)[0].strip().replace("\\", "/")
    for i, ch in enumerate(g):
        if ch in "*?[":
            return g[:i].rstrip("/")
    return g.rsplit("/", 1)[0] if "/" in g else ""


def load_registry() -> list:
    """[] when no registry file exists (nearest-.goal resolution only). Raises when the
    file exists but yields nothing — an unreadable registry must not disarm guards."""
    if not REGISTRY.is_file():
        return []
    try:
        text = REGISTRY.read_text(encoding="utf-8")
    except OSError as e:
        raise RegistryUnreadable(f"{REGISTRY}: {e}")
    flows: list = []
    cur = None
    in_owns = False
    for raw in text.splitlines():
        line = raw.rstrip()
        m = re.match(r"^\s*- id:\s*(\S+)", line)
        if m:
            cur = Workflow(id=m.group(1).strip("'\""), globs=[], home=None)
            flows.append(cur)
            in_owns = False
            continue
        if cur is None:
            continue
        m = re.match(r"^\s+owns:\s*\[(.*)\]\s*$", line)   # inline list form
        if m:
            cur.globs += [g.strip().strip("'\"") for g in m.group(1).split(",") if g.strip()]
            continue
        if re.match(r"^\s+owns:\s*$", line):
            in_owns = True
            continue
        if in_owns:
            m = re.match(r"^\s+- (.+)$", line)
            if m:
                g = m.group(1).strip().strip("'\"").split("#", 1)[0].strip().strip("'\"")
                if g and not re.match(r"^[A-Za-z]:/", g):
                    cur.globs.append(g)
                continue
            in_owns = False
    if not flows:
        raise RegistryUnreadable(f"{REGISTRY}: no workflows parsed")
    for w in flows:
        for g in w.globs:
            p = _fixed_prefix(g)
            if p:
                w.home = p
                break
    return flows


def _glob_match(rel: str, glob: str) -> bool:
    g = glob.replace("\\", "/")
    if g.endswith("/**") and "**" not in g[:-3]:
        # fast path for the plain `<dir>/**` form only — `**/x/**` must reach the regex branch (v4.3: the literal
        # base `**/x` never matched, so `owns:` globs of that shape were inert)
        base = g[:-3]
        return rel == base or rel.startswith(base + "/")
    if "**" in g:
        rx = re.escape(g).replace(r"\*\*/", "(?:.*/)?").replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
        return re.fullmatch(rx, rel) is not None
    return fnmatch.fnmatchcase(rel, g)


def _glob_score(g: str) -> tuple:
    """(fixed-prefix length, 1 for an exact-file glob) — an exact-file `owns:` entry beats `<dir>/**` over the same
    directory (v4.3: two workflows scored the same prefix and the first in the file won the tie, so a core module's
    `server.py` resolved to the surrounding dashboard workflow). Folder vs folder is unchanged: longest fixed prefix
    wins, first in the file at a tie."""
    g = g.replace("\\", "/")
    return (len(_fixed_prefix(g)), 0 if any(ch in g for ch in "*?[") else 1)


def workflow_for(rel: str, flows: list) -> Optional[Workflow]:
    best, best_score = None, (-1, -1)
    for w in flows:
        for g in w.globs:
            if _glob_match(rel, g):
                sc = _glob_score(g)
                if sc > best_score:
                    best, best_score = w, sc
    return best


# ----------------------------------------------------------------------------- resolution

@dataclass
class Resolution:
    kind: str            # root | registry | nearest | unguarded
    folder: str          # repo-relative folder owning the lock domain ("" = root)
    lock_dir: Optional[Path]
    workflow: Optional[str] = None
    detail: str = ""


def to_rel(path) -> Optional[str]:
    """Repo-relative POSIX path, or None when outside the repo."""
    p = Path(path)
    if not p.is_absolute():
        p = Path.cwd() / p
    try:
        p = p.resolve(strict=False)
    except OSError:
        pass
    try:
        return p.relative_to(ROOT).as_posix()
    except ValueError:
        try:
            rel = Path(os.path.relpath(str(p), str(ROOT)))
        except ValueError:
            return None
        return None if str(rel).startswith("..") else rel.as_posix()


def resolve(rel: str, flows: Optional[list] = None) -> Resolution:
    rel = rel.replace("\\", "/").strip("/")
    parts = rel.split("/")
    if flows is None:
        flows = load_registry()
    w = workflow_for(rel, flows)
    if w is not None and w.home:
        return Resolution("registry", w.home, LOCK_TREE / w.home / ".goal", w.id)
    if len(parts) == 1 or parts[0] in ROOT_SPANNING_PREFIXES or (w is not None and w.home is None):
        return Resolution("root", "", ROOT_LOCK_DIR, None, "top-level / repo-spanning file -> root lock")
    for i in range(len(parts) - 1, 0, -1):
        folder = "/".join(parts[:i])
        if (LOCK_TREE / folder / ".goal").is_dir():
            return Resolution("nearest", folder, LOCK_TREE / folder / ".goal", None, "nearest folder with .goal/")
    guess = "/".join(parts[:min(len(parts) - 1, 3)])
    return Resolution("unguarded", guess, None, None,
                      f"'{guess}' is not in the registry and has no .goal/ — claiming creates it: "
                      f"python scripts/lock.py claim {guess} --task \"...\"")


# ----------------------------------------------------------------------------- lock files

@dataclass
class LockInfo:
    kind: str                 # interactive | fired
    path: Path
    window: str = ""
    task: str = ""
    stream: str = ""
    status: str = "open"
    holder: str = ""
    started: Optional[datetime] = None
    age: timedelta = field(default_factory=lambda: timedelta(0))
    fresh: bool = False
    malformed: bool = False

    def describe(self) -> str:
        since = self.started.strftime(TS_FMT) if self.started else "?"
        h, rem = divmod(int(self.age.total_seconds()), 3600)
        return (f"{self.kind} lock window={self.window or '?'} status={self.status} "
                f"task={self.task[:90]!r} since {since} ({h}h{rem // 60:02d}m ago)")


_FIELD = re.compile(r'^(\w+):\s*(.*?)\s*$')


def parse_lock_text(text: str) -> dict:
    info: dict = {}
    for raw in text.splitlines():
        m = _FIELD.match(raw)
        if not m:
            continue
        key, val = m.group(1), m.group(2)
        if val.startswith('"') and '"' in val[1:]:
            val = val[1:val.index('"', 1)]
        elif val.startswith("'") and "'" in val[1:]:
            val = val[1:val.index("'", 1)]
        else:
            val = val.split(" #", 1)[0].strip()
        info[key] = val
    return info


def read_lock(path: Path, kind: str, now: Optional[datetime] = None) -> LockInfo:
    now = now or datetime.now()
    li = LockInfo(kind=kind, path=path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        li.malformed = True
        return li
    info = parse_lock_text(text)
    if not info and text.strip().isdigit():
        info = {"window": "", "holder": "fired"}
    li.window = info.get("window", "")
    li.task = info.get("task", "")
    li.stream = info.get("stream", "")
    li.status = (info.get("status") or "open").strip().lower()
    li.holder = info.get("holder", "interactive" if kind == "interactive" else "fired")
    if li.status not in ("open", "closing"):
        li.malformed = True
    st = info.get("started")
    if st:
        try:
            li.started = datetime.strptime(st[:16], TS_FMT)
        except ValueError:
            li.malformed = True
    if li.started is None:
        try:
            li.started = datetime.fromtimestamp(path.stat().st_mtime)
        except OSError:
            li.malformed = True
            return li
    if kind == "interactive" and not li.window:
        li.malformed = True
    ref = li.started if kind == "interactive" else datetime.fromtimestamp(path.stat().st_mtime)
    li.age = now - ref
    li.fresh = li.age < (LOCK_FRESH if kind == "interactive" else FIRING_FRESH)
    return li


def locks_at(lock_dir: Optional[Path], now: Optional[datetime] = None) -> list:
    out = []
    if not lock_dir:
        return out
    if (lock_dir / "LOCK.yaml").is_file():
        out.append(read_lock(lock_dir / "LOCK.yaml", "interactive", now))
    if (lock_dir / ".firing.lock").is_file():
        out.append(read_lock(lock_dir / ".firing.lock", "fired", now))
    return out


def same_window(a: str, b: str) -> bool:
    return bool(a) and bool(b) and a.strip().lower() == b.strip().lower()


# ----------------------------------------------------------------------------- identity

@dataclass
class Identity:
    window: str
    source: str            # env | session
    session_id: str = ""


def _safe_sid(session_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)[:80]


def session_file(session_id: str) -> Path:
    return SESSIONS / f"{_safe_sid(session_id)}.yaml"


def _ensure_migrated(session_id: str) -> None:
    """A binding (and its sidecar) that still sits at the pre-4.2 tree location is copied to STATE_ROOT
    once, so a host bound before the move keeps every identity with no manual step (PROTOCOL §14)."""
    if not session_id or not LEGACY_SESSIONS.is_dir():
        return
    try:
        for name in (f"{_safe_sid(session_id)}.yaml", f"{_safe_sid(session_id)}.handoffs.txt"):
            old, new = LEGACY_SESSIONS / name, SESSIONS / name
            if old.is_file() and not new.exists():
                SESSIONS.mkdir(parents=True, exist_ok=True)
                shutil.copy2(old, new)
    except OSError:
        pass


def read_session(session_id: str) -> dict:
    _ensure_migrated(session_id)
    try:
        text = session_file(session_id).read_text(encoding="utf-8")
    except OSError:
        return {}
    info = parse_lock_text(text)
    info["folders"] = [f.strip().strip('"') for f in re.findall(r"^\s+-\s+(.+?)\s*$", text, re.M)]
    return info


_LEGACY_HANDOFF = re.compile(r'^(handoff|consumed):\s*"?(.+?)"?\s*$', re.M)


def handoffs_file(session_id: str) -> Path:
    """Append-only sidecar next to the binding: one record per line, `staged <path>` / `consumed <path>`."""
    _ensure_migrated(session_id)
    return session_file(session_id).with_suffix(".handoffs.txt")


def _legacy_handoff_lines(text: str) -> list:
    """(kind, path) pairs from pre-sidecar bindings (`handoff:` / `consumed:` lines in the YAML)."""
    return [("staged" if k == "handoff" else "consumed", v.strip()) for k, v in _LEGACY_HANDOFF.findall(text)]


def record_handoff(session_id: str, kind: str, rel: str) -> None:
    """kind: 'staged' | 'consumed'; rel: repo-relative POSIX path of the note. Never raises on I/O."""
    if kind not in ("staged", "consumed"):
        raise ValueError(f"record_handoff kind {kind!r}")
    try:
        SESSIONS.mkdir(parents=True, exist_ok=True)
        with handoffs_file(session_id).open("a", encoding="utf-8") as fh:
            fh.write(f"{kind} {rel.replace(chr(92), '/').strip('/')}\n")
    except OSError:
        pass


def read_handoffs(session_id: str) -> list:
    """Ordered (kind, path) records of this session: sidecar first, then any legacy lines still
    sitting in the YAML (a binding written before the sidecar existed)."""
    out = []
    try:
        for raw in handoffs_file(session_id).read_text(encoding="utf-8").splitlines():
            parts = raw.strip().split(" ", 1)
            if len(parts) == 2 and parts[0] in ("staged", "consumed"):
                out.append((parts[0], parts[1].strip()))
    except OSError:
        pass
    try:
        out += _legacy_handoff_lines(session_file(session_id).read_text(encoding="utf-8"))
    except OSError:
        pass
    return out


def write_session(session_id: str, window: str, folders: list, extra: Optional[dict] = None) -> Path:
    _ensure_migrated(session_id)
    SESSIONS.mkdir(parents=True, exist_ok=True)
    p = session_file(session_id)
    # legacy bindings carried `handoff:` / `consumed:` lines inline; move them to the sidecar once,
    # then the YAML holds identity only (the rewrite can no longer lose a record)
    try:
        legacy = _legacy_handoff_lines(p.read_text(encoding="utf-8"))
    except OSError:
        legacy = []
    if legacy:
        already = set(read_handoffs(session_id)) - set(legacy)
        for kind, rel in legacy:
            if (kind, rel) not in already:
                record_handoff(session_id, kind, rel)
    lines = [f'window: "{window}"', f'session_id: "{session_id}"',
             f'bound: "{datetime.now().strftime(TS_FMT)}"']
    for k, v in (extra or {}).items():
        lines.append(f'{k}: "{v}"')
    lines.append("folders:")
    for f in folders:
        lines.append(f'  - "{f}"')
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def is_reader(session_id: str) -> bool:
    """A binding with `kind: reader` and no folders — identity without a lock domain (v4.2, PROTOCOL §1b):
    a menu-only session's state writes carry an author, but it may not edit anything."""
    cur = read_session(session_id) if session_id else {}
    return bool(cur.get("window")) and str(cur.get("kind", "")).strip().strip('"') == "reader" and not cur.get("folders")


def identity(session_id: str = "", env: Optional[dict] = None) -> Optional[Identity]:
    env = os.environ if env is None else env
    env_window = (env.get("ICM_WINDOW") or "").strip()
    sid = session_id or (env.get("CLAUDE_CODE_SESSION_ID") or "").strip()
    sess_window = (read_session(sid).get("window") or "").strip() if sid else ""
    if env_window and sess_window and not same_window(env_window, sess_window):
        raise IdentityConflict(
            f"ICM_WINDOW={env_window!r} but this session is bound to window {sess_window!r} "
            f"({SESSIONS}). Unset one; never commit under a borrowed identity.")
    if env_window:
        return Identity(env_window, "env", sid)
    if sess_window:
        return Identity(sess_window, "session", sid)
    return None


NO_IDENTITY_HELP = (
    "no window identity: this session has not claimed a folder. Claim one — "
    "`python scripts/lock.py claim <folder> --task \"...\"` mints a window and binds it to this Claude session; "
    "a hand-written pre-existing lock of yours is bound with `python scripts/lock.py adopt <folder>`. "
    "Plain terminals / headless agents: set ICM_WINDOW=<window>."
)


# ----------------------------------------------------------------------------- whitelist + log

def literal_lock(rel: str, res: Optional[Resolution] = None, now: Optional[datetime] = None) -> Optional[LockInfo]:
    """The CLOSEST fresh interactive LOCK.yaml sitting at the literal `.goal/` of `rel` (when rel is a
    folder) or of one of its ancestor folders — excluding the resolved lock dir itself (the normal
    path already saw it) and any folder that is a strict ancestor of the resolved home (a lock above
    a registered workflow never governs it). Exists for the registry-remap case: a folder claimed as
    its own lock domain and later folded into another home by a registry edit keeps its lock — the
    holder keeps working, everyone else (including the home's holder) is refused, because the most
    specific existing lock wins. None when nothing qualifies."""
    rel = rel.replace("\\", "/").strip("/")
    parts = rel.split("/") if rel else []
    skip = res.lock_dir.resolve() if (res is not None and res.lock_dir is not None) else None
    home = (res.folder or "").strip("/") if res is not None else ""
    for i in range(len(parts), 0, -1):
        folder = "/".join(parts[:i])
        cand = LOCK_TREE / folder / ".goal"
        if not cand.is_dir():
            continue
        if skip is not None and cand.resolve() == skip:
            continue
        if home and home.startswith(folder + "/"):
            continue  # ancestor of the resolved home: not a candidate
        for li in locks_at(cand, now):
            if li.kind == "interactive" and not li.malformed and li.fresh:
                return li
    return None


def own_literal_lock(rel: str, me: Optional[Identity], res: Optional[Resolution] = None,
                     now: Optional[datetime] = None) -> Optional[LockInfo]:
    """literal_lock() when it carries MY window, else None."""
    if me is None:
        return None
    li = literal_lock(rel, res, now)
    return li if (li is not None and same_window(li.window, me.window)) else None


def is_whitelisted(rel: str, res: Resolution, me: Optional[Identity]) -> str:
    parts = rel.split("/")
    if ".goal" in parts:
        return ".goal/ runtime carrier"
    if "workflow-state" in parts and me is not None:
        if res.lock_dir is not None:
            for li in locks_at(res.lock_dir):
                if li.kind == "interactive" and same_window(li.window, me.window):
                    return "workflow-state/ under own lock"
        if own_literal_lock(rel, me, res) is not None:
            return "workflow-state/ under own literal lock (registry remapped the folder)"
    return ""


def guard_log(event: dict) -> None:
    """Append one decision to <STATE_ROOT>/guard_log.jsonl (host-local, §14 — never in the tree)."""
    try:
        import json
        p = STATE_ROOT / "guard_log.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        event.setdefault("ts", datetime.now().strftime("%Y-%m-%dT%H:%M:%S"))
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        pass
