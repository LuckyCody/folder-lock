"""Sync conflict-copy scanner + resolver — PROTOCOL §15 (v4.2).

File-sync tools (OneDrive, Dropbox, Syncthing, …) write a SIBLING next to a file when two hosts changed it:
`<stem>-<HOSTNAME>[-N].<ext>`, ` - Copy`, ` - Kopie`, ` (N)`, `.sync-conflict-…`. To a human the folder shows two
files and they pick one. To an agent that scans the directory a conflict copy of CLAUDE.md looks exactly like
CLAUDE.md, and a `registry-HOSTNAME.yaml` beside the registry is a second registry. Unattended runs never notice.

Rule (fails closed, §10): a load-bearing file with a DIVERGENT sibling blocks `lock.py claim` (exit 5) and
`require_lock` denies edits to that file until the sibling is folded away. Siblings that carry no information —
byte-identical, a strict prefix / line-subset of the canonical, or a copy of a GENERATED file — are deleted on
sight by `--resolve` (nothing is lost: the canonical already holds every byte). Append-only logs (`autorun-log.md`,
`*.jsonl`, `*_log.txt`) are line-union merged into the canonical, then the sibling goes. Anything else is kept,
listed, and — when load-bearing — blocks.

  python lib/conflicts.py                       scan the rules scope (root files, .claude/, .githooks/, .folder-lock/, root .goal/)
  python lib/conflicts.py <folder> [...]        + those folders (what `lock.py claim` gates on)
  python lib/conflicts.py <folder> --resolve    fold the safe classes away, list what remains
  python lib/conflicts.py --json                machine-readable

Hosts: this machine's name (lockpath.HOST) + FOLDER_LOCK_HOSTS=<A,B,…> + the Windows default patterns
(WIN-…, DESKTOP-…, LAPTOP-…). Never touches `.git`, build caches, `_archive/`, worktrees, or `sessions/`.
Every deletion / merge is appended to the guard log (lockpath.guard_log, guard="conflicts").
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lockpath as lp  # noqa: E402

ROOT = lp.ROOT
HOST = lp.HOST
KNOWN_HOSTS = {h.strip().upper() for h in [HOST, *os.environ.get("FOLDER_LOCK_HOSTS", "").split(",")] if h.strip()}

_HOST_ALT = "|".join(sorted((re.escape(h) for h in KNOWN_HOSTS), key=len, reverse=True)
                     + [r"WIN-[A-Z0-9]{12}", r"DESKTOP-[A-Z0-9]{7}", r"LAPTOP-[A-Z0-9]{7}"])
_HOST_RX = re.compile(rf"^(?P<stem>.*?)-(?P<host>{_HOST_ALT})(?:-(?P<n>\d+))?(?P<ext>\.[^.]+)?$", re.I)
_COPY_RX = re.compile(r"^(?P<stem>.+?) - (?:Copy|Kopie)(?: \(\d+\))?(?P<ext>\.[^.]+)?$")
_PAREN_RX = re.compile(r"^(?P<stem>.+?) \((?P<n>\d+)\)(?P<ext>\.[^.]+)?$")
_SYNCTHING_RX = re.compile(r"^(?P<stem>.+?)\.sync-conflict-\d{8}-\d{6}-[A-Z0-9]+(?P<ext>\.[^.]+)?$")

PRUNE_DIRS = {"node_modules", ".venv", "venv", "__pycache__", ".git", ".next", "dist", "build", ".pytest_cache", "worktrees",
              "_archive", "sessions", "cache", "outbox", "store", ".mypy_cache", "site-packages", "deploy"}
MAX_ENTRIES = 60_000

# the rules set — a divergent sibling here blocks claim + edit (PROTOCOL §15)
RULE_FILES = {"CLAUDE.md", "AGENTS.md", ".claude/settings.json",
              ".folder-lock/registry.yaml", ".folder-lock/deploy-units.yaml", ".folder-lock/blockers.md"}
RULE_PREFIXES = (".claude/hooks/", ".claude/skills/", ".githooks/", ".folder-lock/")
FOLDER_LOAD_BEARING = {"LOCK.yaml", "current-pointer.md", "memory.md", "DECISIONS.md", "CONTEXT.md", "workflow.yaml"}
GENERATED = {".folder-lock/next-session.md"}
APPEND_ONLY_NAMES = {"autorun-log.md", "guard_log.jsonl", "deploys.jsonl"}
APPEND_ONLY_SUFFIX = (".jsonl", "_log.txt", "_log.md")


def load_bearing(rel: str) -> bool:
    if rel in RULE_FILES or rel.startswith(RULE_PREFIXES):
        return True
    name = rel.rsplit("/", 1)[-1]
    if name in FOLDER_LOAD_BEARING:
        return True
    return "/.goal/inbox/" in rel or rel.startswith(".goal/inbox/")


def canonical_name(name: str) -> Optional[str]:
    """The canonical file name a conflict-copy NAME points at, or None when the name is not a conflict copy."""
    m = _SYNCTHING_RX.match(name)
    if m:
        return f"{m.group('stem')}{m.group('ext') or ''}"
    m = _HOST_RX.match(name)
    if m:
        return f"{m.group('stem')}{m.group('ext') or ''}"
    m = _COPY_RX.match(name)
    if m:
        return f"{m.group('stem')}{m.group('ext') or ''}"
    m = _PAREN_RX.match(name)
    if m and (m.group("ext") or "").lower() not in (".pdf", ".jpg", ".jpeg", ".png", ".docx", ".xlsx"):
        # " (1).pdf" is how browsers name a second download — only treat text-ish extensions as conflict copies
        return f"{m.group('stem')}{m.group('ext') or ''}"
    return None


@dataclass
class Conflict:
    sibling: str          # repo-relative path of the conflict copy
    canonical: str        # repo-relative path it shadows
    kind: str             # identical | subset | generated | appendlog | divergent | orphan
    load_bearing: bool
    sib_bytes: int = 0
    canon_bytes: int = 0
    extra_lines: int = 0  # lines in the sibling that the canonical does not have
    action: str = ""      # filled by resolve(): deleted | merged+deleted | kept

    @property
    def blocking(self) -> bool:
        return self.load_bearing and self.kind == "divergent"

    def describe(self) -> str:
        tag = "BLOCKING " if self.blocking else ""
        extra = f", {self.extra_lines} line(s) only in the copy" if self.kind in ("divergent", "appendlog") else ""
        return f"{tag}{self.kind:9s} {self.sibling}  ->  {self.canonical} ({self.sib_bytes}B vs {self.canon_bytes}B{extra})"


def _lines(b: bytes) -> list:
    return b.decode("utf-8", "replace").splitlines()


def _is_append_only(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return name in APPEND_ONLY_NAMES or name.endswith(APPEND_ONLY_SUFFIX)


def _rel(p: Path) -> str:
    try:
        return p.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return p.as_posix()


def classify(sib: Path, canon: Path) -> Conflict:
    rel_s, rel_c = _rel(sib), _rel(canon)
    lb = load_bearing(rel_c)
    try:
        sb = sib.read_bytes()
    except OSError:
        sb = b""
    if not canon.is_file():
        return Conflict(rel_s, rel_c, "orphan", lb, len(sb), 0)
    try:
        cb = canon.read_bytes()
    except OSError:
        cb = b""
    if sb == cb:
        return Conflict(rel_s, rel_c, "identical", lb, len(sb), len(cb))
    if rel_c in GENERATED:
        return Conflict(rel_s, rel_c, "generated", lb, len(sb), len(cb))
    if cb.startswith(sb):
        return Conflict(rel_s, rel_c, "subset", lb, len(sb), len(cb))
    sl, cl = _lines(sb), _lines(cb)
    cset = set(cl)
    extra = [l for l in sl if l not in cset and l.strip()]
    if not extra:
        return Conflict(rel_s, rel_c, "subset", lb, len(sb), len(cb))
    kind = "appendlog" if _is_append_only(rel_c) else "divergent"
    return Conflict(rel_s, rel_c, kind, lb, len(sb), len(cb), len(extra))


def _iter_files(target: Path, recursive: bool) -> Iterable[Path]:
    if target.is_file():
        yield target
        return
    if not target.is_dir():
        return
    if not recursive:
        for e in os.scandir(target):
            if e.is_file(follow_symlinks=False):
                yield Path(e.path)
        return
    n = 0
    for dirpath, dirnames, filenames in os.walk(target):
        dirnames[:] = [d for d in dirnames if d not in PRUNE_DIRS and not d.startswith(".git")]
        for f in filenames:
            n += 1
            if n > MAX_ENTRIES:
                return
            yield Path(dirpath) / f


def scan(targets: Iterable[tuple]) -> list:
    """targets: (path, recursive). Returns Conflicts, deduplicated, load-bearing first."""
    seen, out = set(), []
    for target, recursive in targets:
        for f in _iter_files(target, recursive):
            canon_name = canonical_name(f.name)
            if canon_name is None or canon_name == f.name:
                continue
            key = f.resolve().as_posix()
            if key in seen:
                continue
            seen.add(key)
            out.append(classify(f, f.parent / canon_name))
    out.sort(key=lambda c: (not c.blocking, not c.load_bearing, c.canonical))
    return out


def siblings_of(rel: str) -> list:
    """Conflict copies that shadow ONE file (repo-relative). What `require_lock` checks for the file being edited."""
    canon = ROOT / rel
    d = canon.parent
    if not d.is_dir():
        return []
    out = []
    for e in os.scandir(d):
        if not e.is_file(follow_symlinks=False) or e.name == canon.name:
            continue
        if canonical_name(e.name) == canon.name:
            out.append(classify(Path(e.path), canon))
    return out


def root_scope() -> list:
    """The rules scope every claim + every session start checks: root files, .claude/, .githooks/, .folder-lock/, root .goal/."""
    return [(ROOT, False), (ROOT / ".claude", True), (ROOT / ".githooks", True), (ROOT / ".folder-lock", True),
            (ROOT / ".goal", True)]


def folder_scope(rel: str) -> list:
    rel = rel.strip("/")
    if not rel:
        return []
    return [(ROOT / rel, True)] if (ROOT / rel).is_dir() else []


def resolve(conflicts: list, apply: bool = True) -> tuple:
    """Fold the safe classes away. Returns (resolved, remaining)."""
    resolved, remaining = [], []
    for c in conflicts:
        sib = ROOT / c.sibling
        canon = ROOT / c.canonical
        try:
            if c.kind in ("identical", "subset", "generated"):
                if apply:
                    sib.unlink()
                c.action = "deleted" if apply else "would delete"
                resolved.append(c)
            elif c.kind == "appendlog":
                if apply:
                    cl = _lines(canon.read_bytes())
                    cset = set(cl)
                    extra = [l for l in _lines(sib.read_bytes()) if l not in cset and l.strip()]
                    with canon.open("a", encoding="utf-8", newline="\n") as fh:
                        if cl and not canon.read_bytes().endswith(b"\n"):
                            fh.write("\n")
                        for l in extra:
                            fh.write(l + "\n")
                    sib.unlink()
                c.action = f"merged {c.extra_lines} line(s) + deleted" if apply else "would merge+delete"
                resolved.append(c)
            else:
                c.action = "kept"
                remaining.append(c)
        except OSError as e:
            c.action = f"kept (io error: {e})"
            remaining.append(c)
        if apply and c.action.startswith(("deleted", "merged")):
            lp.guard_log({"guard": "conflicts", "decision": c.action, "sibling": c.sibling, "canonical": c.canonical,
                          "kind": c.kind, "load_bearing": c.load_bearing})
    return resolved, remaining


def _owned_by(canonical_rel: str, folder_rel: str) -> bool:
    """Does the canonical file resolve (registry owns: globs) into the folder being claimed? "" = root lock."""
    try:
        res = lp.resolve(canonical_rel)
    except Exception:
        return False
    if res.kind == "unguarded":
        return bool(folder_rel) and canonical_rel.startswith(folder_rel.rstrip("/") + "/")
    home = res.folder or ""
    if home == folder_rel:
        return True
    return bool(folder_rel) and canonical_rel.startswith(folder_rel.rstrip("/") + "/")


def gate(folder_rel: str = "", apply: bool = True, quiet: bool = False) -> list:
    """What `lock.py claim` runs: scan the rules scope + the folder, fold the safe classes, return the BLOCKING list
    (divergent copies of load-bearing files OUTSIDE the claimed folder). Divergent copies INSIDE the folder are the
    claim's FIRST ACT — printed, not blocking: the claim is how you earn the right to fold them."""
    folder_rel = (folder_rel or "").replace("\\", "/").strip("/")
    found = scan(root_scope() + folder_scope(folder_rel))
    if not found:
        return []
    resolved, remaining = resolve(found, apply=apply)
    if not quiet:
        for c in resolved:
            print(f"conflict copy {c.action}: {c.sibling} ({c.kind} of {c.canonical})")
        for c in remaining:
            print(f"conflict copy kept: {c.describe()}")
    mine = [c for c in remaining if c.blocking and _owned_by(c.canonical, folder_rel)]
    blocking = [c for c in remaining if c.blocking and c not in mine]
    if mine and not quiet:
        n = len(mine)
        print(f"FIRST ACT after this claim — {n} divergent conflict cop{'y' if n == 1 else 'ies'} of load-bearing "
              f"file(s) in YOUR folder: read the copy's extra lines, put what is true into the canonical (script/shell — the edit "
              f"guard denies Edit on the canonical until the copy is gone), delete the copy, record the decision in your progress log.")
        for c in mine:
            print(f"  fold: {c.sibling}  ->  {c.canonical} ({c.extra_lines} line(s) only in the copy)")
    if blocking and not quiet:
        print(f"CONFLICT COPY — {len(blocking)} load-bearing file(s) OUTSIDE {folder_rel or '<root>'} have a divergent sibling. "
              f"Claim the folder that owns the canonical and fold there (or message its holder, §16) before working here. "
              f"Listing: python lib/conflicts.py {folder_rel} --json")
    return blocking


def brief(scope: Optional[list] = None) -> str:
    """One paragraph for a session-start briefing — report only, never deletes at startup."""
    found = scan(scope or root_scope())
    if not found:
        return ""
    blocking = [c for c in found if c.blocking]
    safe = [c for c in found if c.kind in ("identical", "subset", "generated", "appendlog")]
    other = [c for c in found if c not in blocking and c not in safe]
    lines = [f"CONFLICT COPIES in the rules scope: {len(found)} sibling(s) — {len(blocking)} BLOCKING (divergent, load-bearing), "
             f"{len(safe)} safe to fold (`python lib/conflicts.py --resolve` — claim does this itself), {len(other)} other."]
    for c in blocking[:10]:
        lines.append(f"  BLOCK {c.describe()}")
    for c in other[:5]:
        lines.append(f"  · {c.describe()}")
    if blocking:
        lines.append("  A divergent copy of a rules file means this host may be running different rules than the other. "
                     "`lock.py claim` refuses (exit 5) until the copy is folded — never read the copy as the rule.")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("folders", nargs="*", help="workfolders to include (the rules scope is always included)")
    ap.add_argument("--resolve", action="store_true", help="fold identical/subset/generated/appendlog siblings away")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    targets = root_scope()
    for f in a.folders:
        targets += folder_scope(f.strip("/").replace("\\", "/"))
    found = scan(targets)
    resolved, remaining = resolve(found, apply=a.resolve) if found else ([], [])
    blocking = [c for c in remaining if c.blocking]
    if a.json:
        print(json.dumps({"host": HOST, "found": len(found), "blocking": len(blocking),
                          "conflicts": [asdict(c) for c in found]}, indent=1, ensure_ascii=False))
    else:
        if not found:
            print("no conflict copies in scope")
        for c in resolved:
            print(f"{c.action:>28}: {c.sibling}  ({c.kind} of {c.canonical})")
        for c in remaining:
            print(f"{'kept':>28}: {c.describe()}")
        print(f"-- {len(found)} found · {len(resolved)} {'resolved' if a.resolve else 'resolvable (add --resolve)'} · "
              f"{len(remaining)} kept · {len(blocking)} BLOCKING")
    return 5 if blocking else 0


if __name__ == "__main__":
    sys.exit(main())
