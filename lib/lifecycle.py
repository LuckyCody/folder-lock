"""Session lifecycle — the ONE implementation of the state machine, the terminal block and the
write verdict (PROTOCOL.md §17, v5.0).

Every guard and every skill script imports THIS module for the three questions the lifecycle asks,
so they cannot disagree:

  1. In which STATE is this session?          session_state()  -> unclaimed | claimed | signed-off
  2. May this session WRITE this path?         write_verdict()  -> (allow, reason)   [edit guard, shell guard, commit guard]
  3. Did the turn END properly?                parse_terminal_block() over the final assistant message [Stop hook]

State machine (§17):  unclaimed -> claimed(<folder>) -> signed-off
  unclaimed   no identity, or a READER binding (window, no folder)      writes: _inbox/ drops + workflow-state/ only
  claimed     a fresh lock of this window (LOCK.yaml or .firing.lock)   writes: the held folder(s) + the two exceptions
  signed-off  identity bound, no fresh lock left                        writes: as unclaimed

Terminal block — the last three non-empty lines of every final message, verbatim structure:

  status: done | blocked | handed-off
  held:   <folder> | none
  next:   <exact command> | none — waiting on the owner

`held:` must agree with the locks on disk (the Stop hook checks). A headless agent may follow the block
with ONE resumer outcome line (`DONE` | `WAITING_CODY` | `FAILED: …`) — the resumer reads that line,
the Stop hook reads the block.

Nothing here writes a lock or a file; the module decides, the callers act.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent))
import lockpath as lp  # noqa: E402

INBOX = (os.environ.get("FOLDER_LOCK_INBOX") or "_inbox").strip("/")   # the drop zone (PROTOCOL §7)

STATUSES = ("done", "blocked", "handed-off")
NEXT_NONE = "none — waiting on the owner"
_DASH = r"(?:—|–|-|--)"
_STATUS_RE = re.compile(r"^\s*status:\s*(done|blocked|handed-off)\s*$", re.I)
_HELD_RE = re.compile(r"^\s*held:\s*(\S.*?)\s*$", re.I)
_NEXT_RE = re.compile(r"^\s*next:\s*(\S.*?)\s*$", re.I)
_NEXT_NONE_RE = re.compile(r"^none\s*" + _DASH + r"\s*waiting on (?:the )?(?:owner|cody)\s*$", re.I)
_OUTCOME_RE = re.compile(r"^\s*(DONE|WAITING_OWNER|WAITING_CODY|FAILED:.*)\s*$")   # goal_resumer's last-line contract (§13)
_FENCE_RE = re.compile(r"^\s*(```|~~~)\w*\s*$")
ARCHIVE_ROOT = "_archive"
WRITE_EXCEPTIONS = (INBOX + "/", "workflow-state/")   # §17 invariant 4


# ----------------------------------------------------------------------------
# terminal block
# ----------------------------------------------------------------------------

def format_terminal_block(status: str, held: str, next_: str) -> str:
    if status not in STATUSES:
        raise ValueError(f"status {status!r} not in {STATUSES}")
    return f"status: {status}\nheld:   {held or 'none'}\nnext:   {next_ or NEXT_NONE}"


def parse_terminal_block(text: str) -> Optional[dict]:
    """The block must be the LAST three non-empty lines of `text` (code fences and one trailing resumer
    outcome line are tolerated). Returns {status, held, next, held_list, outcome} or None."""
    if not text:
        return None
    lines = [l.rstrip() for l in text.replace("\r\n", "\n").split("\n")]
    lines = [l for l in lines if l.strip() and not _FENCE_RE.match(l)]
    outcome = ""
    if lines and _OUTCOME_RE.match(lines[-1]):
        outcome = lines[-1].strip()
        lines = lines[:-1]
    if len(lines) < 3:
        return None
    s, h, n = lines[-3], lines[-2], lines[-1]
    ms, mh, mn = _STATUS_RE.match(s), _HELD_RE.match(h), _NEXT_RE.match(n)
    if not (ms and mh and mn):
        return None
    held = mh.group(1).strip().strip("`")
    nxt = mn.group(1).strip()
    if nxt.lower().startswith("none") and not _NEXT_NONE_RE.match(nxt):
        return None   # `next: none` is only valid as `none — waiting on the owner`
    held_list = [] if held.lower() == "none" else [p.strip().strip("`").replace("\\", "/").strip("/")
                                                  for p in re.split(r"[,+]| and ", held) if p.strip()]
    return {"status": ms.group(1).lower(), "held": held, "next": nxt, "held_list": held_list, "outcome": outcome}


def final_assistant_text(transcript_path: str) -> Optional[str]:
    """The text of the FINAL assistant message in a Claude Code transcript (JSONL, one content block per line):
    every trailing `assistant` entry after the last `user` entry (tool results are user entries), text blocks
    joined in order. None when the transcript is missing or holds no assistant text."""
    if not transcript_path:
        return None
    p = Path(transcript_path)
    try:
        raw = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    tail: list = []
    for line in reversed(raw):
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = o.get("type")
        if t == "assistant":
            if o.get("isSidechain"):
                continue
            tail.append(o)
            continue
        if t == "user":
            if tail:
                break
            continue   # a user entry after the last assistant text (rare) — keep walking back
        # system / progress / summary entries are transparent
    texts = []
    for o in reversed(tail):
        c = o.get("message", {}).get("content")
        if isinstance(c, str):
            texts.append(c)
        elif isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                    texts.append(b["text"])
    return "\n".join(texts) if texts else None


# ----------------------------------------------------------------------------
# session state
# ----------------------------------------------------------------------------

def is_fired(env: Optional[dict] = None) -> bool:
    """A headless (loop-fired) agent: the runner exports ICM_WINDOW (fired-*) + ICM_FOLDER into the process env."""
    env = os.environ if env is None else env
    w = (env.get("ICM_WINDOW") or "").strip()
    return bool(w) and (w.lower().startswith("fired-") or bool((env.get("ICM_FOLDER") or "").strip()))


def is_reader(sid: str) -> bool:
    sess = lp.read_session(sid) if sid else {}
    return bool(sess.get("window")) and str(sess.get("kind", "")).strip().strip('"') == "reader" and not sess.get("folders")


def candidate_folders(sid: str, env: Optional[dict] = None) -> list:
    """Folders this session may hold a lock on: the binding's folders, the bridge's ICM_FOLDER, the root lane."""
    env = os.environ if env is None else env
    out: list = []
    if sid:
        for f in lp.read_session(sid).get("folders", []):
            f = f.strip().strip('"').replace("\\", "/").strip("/")
            if f and f not in out:
                out.append(f)
    f = (env.get("ICM_FOLDER") or "").strip().strip("/").replace("\\", "/")
    if f and f not in out:
        out.append(f)
    if "" not in out:
        out.append("")   # the root lock domain (<root>/.goal)
    return out


def held_folders(me: Optional[lp.Identity], sid: str = "", env: Optional[dict] = None) -> list:
    """[(folder, LockInfo)] — every fresh lock (LOCK.yaml or .firing.lock) carrying MY window among the candidates."""
    if me is None:
        return []
    out = []
    for f in candidate_folders(sid, env):
        for li in lp.locks_at(lp.LOCK_TREE / f / ".goal"):
            if li.fresh and not li.malformed and lp.same_window(li.window, me.window):
                out.append((f, li))
                break
    return out


def session_state(sid: str = "", env: Optional[dict] = None) -> dict:
    """{state: unclaimed|claimed|signed-off, window, held: [folder...], fired: bool, reader: bool}.
    Raises lp.IdentityConflict (callers fail closed)."""
    env = os.environ if env is None else env
    sid = sid or (env.get("CLAUDE_CODE_SESSION_ID") or "").strip()
    me = lp.identity(session_id=sid, env=env)
    fired = is_fired(env)
    if me is None or is_reader(sid):
        return {"state": "unclaimed", "window": me.window if me else "", "held": [], "fired": fired,
                "reader": me is not None, "identity": me}
    held = [f for f, _ in held_folders(me, sid, env)]
    return {"state": "claimed" if held else "signed-off", "window": me.window, "held": held,
            "fired": fired, "reader": False, "identity": me}


# ----------------------------------------------------------------------------
# write verdict — edit guard, shell guard and commit guard call THIS
# ----------------------------------------------------------------------------

def _foreign_fresh(locks: list, me: Optional[lp.Identity]) -> list:
    return [li for li in locks if (li.fresh or li.malformed) and not (me and lp.same_window(li.window, me.window))]


def _held_line(me: Optional[lp.Identity], sid: str) -> str:
    held = [f for f, _ in held_folders(me, sid)] if me else []
    return ("you hold: " + ", ".join(held)) if held else "you hold: nothing"


_CODES = (
    ("ARCHIVED", ("is inside _archive/",)),
    ("DROP-HELD", ("the drop folder",)),
    ("NO-IDENTITY", ("no lock held",)),
    ("READER", ("reader identity",)),
    ("UNGUARDED", ("unguarded folder",)),
    ("MALFORMED", ("malformed lock",)),
    ("FOREIGN", ("held by another session", "held by an auto-headless agent", "carries its own fresh lock", "Its workflow-state is theirs")),
    ("STALE", ("STALE lock", "went stale")),
    ("UNCLAIMED", ("free but not yours",)),
)


def verdict_code(allow: bool, reason: str) -> str:
    """Short class of a verdict, for grouping in the commit guard's report."""
    if allow:
        return "OWN" if reason.startswith("own") else "EXCEPTION"
    for code, marks in _CODES:
        if any(m in reason for m in marks):
            return code
    return "DENIED"


def write_verdict(rel: str, me: Optional[lp.Identity], sid: str = "", mode: str = "edit",
                  flows: Optional[list] = None) -> tuple:
    """(allow: bool, reason: str, code: str) for a repo-relative POSIX path. `mode` only shapes the wording
    (edit | shell | commit). Raises lp.RegistryUnreadable — callers deny on it (fail closed)."""
    ok, why = _verdict(rel, me, sid, mode, flows)
    return ok, why, verdict_code(ok, why)


def _verdict(rel: str, me: Optional[lp.Identity], sid: str = "", mode: str = "edit",
             flows: Optional[list] = None) -> tuple:
    rel = rel.replace("\\", "/").strip("/")
    parts = rel.split("/")
    handoff = "python scripts/handoff.py --to {f} --task \"...\""
    # 1. archived folders are read-only — unarchive first, never write into _archive/. The ONE exception is the
    #    archive move itself: the lock travelled with the folder, so a fresh lock of MY window at _archive/<slug>/.goal
    #    means this session is committing the archive (§17 archive flow).
    if parts[0] == ARCHIVE_ROOT:
        slug = parts[1] if len(parts) > 1 else ""
        if slug and me is not None:
            for li in lp.locks_at(lp.LOCK_TREE / ARCHIVE_ROOT / slug / ".goal"):
                if li.fresh and not li.malformed and lp.same_window(li.window, me.window):
                    return True, f"own fresh lock travelled to {ARCHIVE_ROOT}/{slug} (archive move in progress)"
        return False, (f"{rel} is inside {ARCHIVE_ROOT}/ — archived folders are never written. Unarchive it first: "
                       f"scripts/new.py {slug} (python scripts/new.py {slug} --unarchive), then work under its lock.")
    flows = flows if flows is not None else lp.load_registry()
    res = lp.resolve(rel, flows)
    # 2. _inbox/ drop zone — writable by every session (the sanctioned place for new inputs), unless the drop
    #    folder is held by another window
    if parts[0] == INBOX:
        if len(parts) >= 3:
            drop = f"{parts[0]}/{parts[1]}"
            foreign = _foreign_fresh(lp.locks_at(lp.LOCK_TREE / drop / ".goal"), me)
            if foreign:
                return False, (f"{rel}: the drop folder '{drop}' is held by another session: {foreign[0].describe()}. "
                               f"Message the holder (§16) or stage a handoff: " + handoff.format(f=drop))
        return True, f"{INBOX}/ drop zone (§17 exception; a drop held by another window is not)"
    # 3. runtime carriers
    if ".goal" in parts:
        return True, ".goal/ runtime carrier"
    # 4. workflow-state/ — the resume record; any session may write it unless the folder is held by another window
    if "workflow-state" in parts:
        locks = list(lp.locks_at(res.lock_dir)) if res.lock_dir is not None else []
        lit = lp.literal_lock(rel, res)
        if lit is not None:
            locks.append(lit)
        foreign = _foreign_fresh(locks, me)
        if foreign:
            return False, (f"{rel}: folder '{res.folder}' is held by another session: {foreign[0].describe()}. "
                           f"Its workflow-state is theirs to write while they hold it — " + handoff.format(f=res.folder))
        return True, "workflow-state/ (§17 exception: resume record, writable unless the folder is held by another window)"
    # 5. identity — a session with no lock may write nothing else
    if me is None:
        return False, (f"no lock held — with no lock a session may write only {INBOX}/ drops (and workflow-state/). "
                       f"{rel} -> folder '{res.folder or 'root'}'. Triage first: scripts/board.py menu + scripts/lock.py claim (existing work) or scripts/new.py <slug> (new folder). "
                       f"{lp.NO_IDENTITY_HELP}")
    if is_reader(sid):
        return False, (f"reader identity {me.window} (menu-only, no lock) may not write {rel}. Claim first: "
                       f"scripts/lock.py claim <folder> or scripts/new.py <slug> (python scripts/lock.py claim {res.folder or '<folder>'} --task \"...\")")
    if res.kind == "unguarded":
        return False, (f"unguarded folder for {rel}: '{res.folder}' is not in the registry and has no .goal/ — a folder without an "
                       f"owns: entry is not routable and must not exist. Scaffold + register + claim in one step: scripts/new.py <slug>. "
                       f"({_held_line(me, sid)})")
    # 6. the closest existing lock wins (registry remap case)
    lit = lp.literal_lock(rel, res)
    if lit is not None:
        lit_folder = lp.lock_rel(lit.path.parent.parent)
        if lp.same_window(lit.window, me.window):
            return True, f"own fresh literal lock on {lit_folder} (registry maps the path to {res.folder})"
        return False, (f"{rel} is inside '{lit_folder}', which carries its own fresh lock held by another session: {lit.describe()}. "
                       f"{_held_line(me, sid)}. Do not edit here — " + handoff.format(f=lit_folder))
    # 7. the resolved lock domain
    locks = lp.locks_at(res.lock_dir)
    mine_fresh = None
    for li in locks:
        if li.malformed:
            return False, f"malformed lock {lp.lock_rel(li.path)} guards {rel} — treated as foreign; fix or remove it with the owner"
        if lp.same_window(li.window, me.window):
            if li.fresh:
                mine_fresh = li
            continue
        if li.fresh:
            who = "an auto-headless agent" if li.kind == "fired" else "another session"
            return False, (f"{rel} is inside folder '{res.folder}' held by {who}: {li.describe()}. {_held_line(me, sid)}. "
                           f"Outside your held folder nothing is written — MESSAGE THE HOLDER first (§16), or stage a handoff: "
                           + handoff.format(f=res.folder) + f". If that lock is YOURS from before the guards: python scripts/lock.py adopt {res.folder}")
    if mine_fresh is not None:
        return True, f"own fresh lock ({mine_fresh.status}) on {res.folder}"
    stale_foreign = [li for li in locks if li.kind == "interactive" and not lp.same_window(li.window, me.window)]
    if stale_foreign:
        return False, (f"{rel} is inside folder '{res.folder}' with a STALE lock: {stale_foreign[0].describe()}. Ask the owner before "
                       f"taking over — never silently proceed over a stale lock (§1). With their say-so: "
                       f"python scripts/lock.py claim {res.folder} --task \"...\" --force-stale")
    stale_mine = [li for li in locks if lp.same_window(li.window, me.window)]
    if stale_mine:
        return False, (f"your lock on '{res.folder}' went stale ({stale_mine[0].describe()}); re-claim: "
                       f"python scripts/lock.py claim {res.folder} --task \"...\"")
    return False, (f"{rel} is outside your held folder ({_held_line(me, sid)}): folder '{res.folder}' is free but not yours. "
                   f"Writes outside the held folder are a boundary crossing (§2/§17) — stage a handoff: "
                   + handoff.format(f=res.folder) + f" — or, if the owner's task spans both folders, claim it under the same window: "
                   f"python scripts/lock.py claim {res.folder} --task \"...\"")


# ----------------------------------------------------------------------------
# shell redirection targets (Bash / PowerShell PreToolUse)
# ----------------------------------------------------------------------------

_REDIR_RE = re.compile(
    r"(?:(?<![\w$&=-])\d?>{1,2}\s*|\btee\s+(?:-a\s+|--append\s+)?|\b(?:Out-File|Set-Content|Add-Content)\b(?:\s+-(?:File|Literal)?Path)?\s+)"
    r"(?P<q>[\"']?)(?P<path>[^\s\"'|;&<>]+)(?P=q)", re.I)
_SKIP_TARGETS = {"/dev/null", "nul", "$null", "&1", "&2", "/dev/stderr", "/dev/stdout", "-"}


def shell_write_targets(command: str) -> list:
    """Paths a shell command redirects or writes into (`>`, `>>`, `tee`, `Out-File`, `Set-Content`, `Add-Content`).
    Best effort — the sanctioned writers (lock.py, handoff.py, items.py) write from Python and are not caught,
    which is the point; `echo x > <file>` is."""
    out = []
    for m in _REDIR_RE.finditer(command or ""):
        p = m.group("path").strip()
        if not p or p.lower() in _SKIP_TARGETS or p.startswith(("&", "$", "%", "$(", "`")):
            continue
        if p.lower().startswith(("/dev/", "nul:")):
            continue
        if p not in out:
            out.append(p)
    return out


def to_rel_from(path: str, cwd: str = "") -> Optional[str]:
    """Repo-relative POSIX path for a shell target, resolved against the tool call's cwd (hook input `cwd`)."""
    p = Path(path)
    if not p.is_absolute():
        base = Path(cwd) if cwd else Path.cwd()
        p = base / p
    return lp.to_rel(p)


# ----------------------------------------------------------------------------
# registry — parse per entry, own-change test for the commit guard, archive flag
# ----------------------------------------------------------------------------

def registry_blocks(text: str) -> dict:
    """{id: block_text} — one block per `- id:` entry, the header (everything before the first entry) under ''."""
    blocks: dict = {}
    cur, buf = "", []
    for raw in text.replace("\r\n", "\n").split("\n"):
        m = re.match(r"^- id:\s*(\S+)", raw)
        if m:
            blocks[cur] = "\n".join(buf)
            cur, buf = m.group(1).strip("'\""), [raw]
        else:
            buf.append(raw)
    blocks[cur] = "\n".join(buf)
    return blocks


def _home_of_block(block: str) -> str:
    for raw in block.split("\n"):
        m = re.match(r"^  - (\S.*)$", raw)
        if m and not re.match(r"^[A-Za-z]:/", m.group(1)):
            g = m.group(1).strip().strip("'\"").split("#", 1)[0].strip().strip("'\"")
            return lp._fixed_prefix(g)
    return ""


def registry_change_is_own(repo: Path, me: Optional[lp.Identity], sid: str = "", rel: str = "") -> tuple:
    """(own: bool, detail: str, homes: set). A staged change to the workflow registry is the committer's OWN when every changed
    entry's home (before and after) is a folder this window holds fresh — or a folder that no longer exists on disk
    while its counterpart (the archived / unarchived path) IS held. That is exactly what /new and the archive flow
    write; anything else (another entry edited, the header touched) is not own and falls back to the lock rule."""
    import subprocess
    if not rel:
        try:
            rel = lp.REGISTRY.resolve().relative_to(Path(repo).resolve()).as_posix()
        except ValueError:
            rel = ".folder-lock/registry.yaml"

    def show(spec: str) -> str:
        r = subprocess.run(["git", "show", spec], cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace")
        return r.stdout if r.returncode == 0 else ""

    head, index = registry_blocks(show(f"HEAD:{rel}")), registry_blocks(show(f":{rel}"))
    if me is None:
        return False, "no identity", set()
    def norm(t: str) -> str:   # the LAST block carries the file's trailing newline — compare without it
        return (t or "").rstrip()
    if norm(head.get("", "")) != norm(index.get("", "")):
        return False, "registry header changed", set()
    changed = [k for k in set(head) | set(index) if k and norm(head.get(k)) != norm(index.get(k))]
    if not changed:
        return False, "no entry changed", set()
    held = {f for f, _ in held_folders(me, sid)}
    all_homes: set = set()
    for k in changed:
        homes = {h for h in (_home_of_block(head.get(k, "")), _home_of_block(index.get(k, ""))) if h}
        if not homes:
            return False, f"entry {k}: no in-repo owns glob", set()
        ok = False
        for h in homes:
            if h in held:
                ok = True
                continue
            # counterpart moved: the folder is gone from disk and the other home is held
            if not (lp.LOCK_TREE / h).exists() and any(o in held for o in homes if o != h):
                continue
            return False, f"entry {k}: home '{h}' is not held by {me.window}", set()
        if not ok:
            return False, f"entry {k}: none of its homes {sorted(homes)} is held by {me.window}", set()
        all_homes |= homes
    return True, f"own registry change: {', '.join(sorted(changed))} (homes held by {me.window})", all_homes


def archived_workflow(rel: str, flows: Optional[list] = None) -> Optional[lp.Workflow]:
    """The registry entry covering `rel` when that entry is `archived: true` (or the path sits under _archive/)."""
    rel = rel.replace("\\", "/").strip("/")
    flows = flows if flows is not None else lp.load_registry()
    w = lp.workflow_for(rel + "/__probe__", flows) or lp.workflow_for(rel, flows)
    if w is not None and getattr(w, "archived", False):
        return w
    return None


def is_archived_folder(rel: str, flows: Optional[list] = None) -> bool:
    rel = rel.replace("\\", "/").strip("/")
    if rel == ARCHIVE_ROOT or rel.startswith(ARCHIVE_ROOT + "/"):
        return True
    try:
        return archived_workflow(rel, flows) is not None
    except lp.RegistryUnreadable:
        return False


# ----------------------------------------------------------------------------
# registry coverage audit (§17 invariant 5)
# ----------------------------------------------------------------------------

def audit_registry(root: Optional[Path] = None, max_depth: int = 3) -> list:
    """Workfolders (a dir with .goal/ or workflow-state/) whose paths resolve to NO registry entry. Report only."""
    root = root or lp.LOCK_TREE
    flows = lp.load_registry()
    prune = {".git", "node_modules", ".venv", "venv", "__pycache__", ARCHIVE_ROOT, ".claude", ".githooks", ".folder-lock", "dist", "build", ".next",
             INBOX, "worktrees"}
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        relp = Path(dirpath).relative_to(root)
        if len(relp.parts) > max_depth:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if d not in prune and not d.lower().startswith("temp")]
        if relp.parts and ({".goal", "workflow-state"} & set(dirnames)):
            rel = relp.as_posix()
            if lp.workflow_for(rel + "/__probe__", flows) is None:
                out.append(rel)
    return sorted(out)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="lifecycle queries (read-only)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("state", help="this session's lifecycle state")
    v = sub.add_parser("verdict", help="may this session write <path>?"); v.add_argument("path")
    b = sub.add_parser("block", help="parse a terminal block from a file (or - for stdin)"); b.add_argument("file")
    sub.add_parser("audit-registry", help="workfolders without an owns: entry")
    t = sub.add_parser("targets", help="shell write targets of a command"); t.add_argument("command")
    a = ap.parse_args()
    if a.cmd == "state":
        st = session_state()
        print(f"state={st['state']} window={st['window'] or '-'} held={','.join(st['held']) or 'none'} fired={st['fired']}")
    elif a.cmd == "verdict":
        sid = (os.environ.get("CLAUDE_CODE_SESSION_ID") or "").strip()
        me = lp.identity(session_id=sid)
        rel = lp.to_rel(a.path)
        if rel is None:
            print("ALLOW outside the repo")
        else:
            ok, why, code = write_verdict(rel, me, sid)
            print(("ALLOW " if ok else "DENY ") + f"[{code}] " + why)
            raise SystemExit(0 if ok else 2)
    elif a.cmd == "block":
        txt = _sys.stdin.read() if a.file == "-" else Path(a.file).read_text(encoding="utf-8", errors="replace")
        blk = parse_terminal_block(txt)
        print(json.dumps(blk, ensure_ascii=False) if blk else "NO TERMINAL BLOCK")
        raise SystemExit(0 if blk else 2)
    elif a.cmd == "audit-registry":
        missing = audit_registry()
        for m in missing:
            print(m)
        print(f"{len(missing)} workfolder(s) without an owns: entry")
    elif a.cmd == "targets":
        for x in shell_write_targets(a.command):
            print(x)
