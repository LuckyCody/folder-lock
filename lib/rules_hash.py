"""Rules-set hash across hosts — PROTOCOL §15 (v4.2).

Same file NAMES on every machine prove presence, not bytes. When several hosts share one tree over a sync tool
(each with its own git dir), a rule file can differ per host for hours: sync lag, an unpulled commit, a conflict
copy that won the write. This module hashes the rules set on THIS host, publishes it to the state-store document
`rules_hash` (lib/statestore.py, ETag write) and compares against what every other host last published.

  python lib/rules_hash.py                publish + compare (one line per other host)
  python lib/rules_hash.py --no-publish   compare only
  python lib/rules_hash.py --json

Called by `lock.py claim` (and by whatever session-start hook an installation wires). Ruling: a divergence is
REPORTED, loud, with the per-file byte/sha pairs — it does not block, because the disk this session runs from is
the truth for this session (files on disk win; the store is a cache). What blocks is a divergent CONFLICT COPY of
a rules file (`lib/conflicts.py`), because that is the one case where the disk itself holds two versions.

With the default file backend and one state root per host, the store holds only this host's publish — the line
then reads "no other host has published yet". The comparison becomes meaningful with the blob backend (shared
store) or a shared FOLDER_LOCK_STATE_ROOT.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lockpath as lp  # noqa: E402

ROOT = lp.ROOT
HOST = lp.HOST
DOC = "rules_hash"

RULE_FILES = [
    "CLAUDE.md", "AGENTS.md",
    ".folder-lock/registry.yaml", ".folder-lock/deploy-units.yaml", ".folder-lock/blockers.md",
    ".claude/settings.json",
]
RULE_GLOBS = [
    ".githooks/*.py", ".githooks/pre-commit", ".githooks/lib/*.py",
    ".claude/hooks/*.py", ".claude/skills/*/SKILL.md",
]


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def rule_files(root: Path = ROOT) -> list:
    """The static list plus every file the globs find — sorted, repo-relative POSIX."""
    out = set(RULE_FILES)
    for g in RULE_GLOBS:
        for p in root.glob(g):
            if p.is_file():
                try:
                    out.add(p.relative_to(root).as_posix())
                except ValueError:
                    pass
    return sorted(out)


def snapshot(root: Path = ROOT) -> dict:
    files = {}
    for rel in rule_files(root):
        p = root / rel
        try:
            b = p.read_bytes()
            files[rel] = {"sha": _sha(b)[:16], "bytes": len(b)}
        except OSError:
            files[rel] = {"sha": "missing", "bytes": 0}
    set_hash = _sha("\n".join(f"{k}:{v['sha']}" for k, v in sorted(files.items())).encode())[:16]
    try:
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True, text=True,
                              timeout=5).stdout.strip() or "?"
    except Exception:
        head = "?"
    return {"host": HOST, "ts": datetime.now().strftime("%Y-%m-%dT%H:%M"), "set_hash": set_hash, "head": head,
            "files": files}


def publish(snap: dict) -> dict:
    import statestore as s
    doc = s.load(DOC, {"hosts": {}})
    hosts = doc.setdefault("hosts", {})
    hosts[HOST] = dict(snap, updated=snap["ts"])
    s.save(DOC, doc)
    return doc


def load() -> dict:
    import statestore as s
    return s.load(DOC, {"hosts": {}})


def compare(doc: dict, snap: dict) -> list:
    lines = []
    others = {h: r for h, r in (doc.get("hosts") or {}).items() if h != HOST and isinstance(r, dict)}
    if not others:
        return [f"rules: {HOST} set {snap['set_hash'][:8]} @ {snap['head']} — no other host has published yet"]
    for host, rec in sorted(others.items()):
        if rec.get("set_hash") == snap["set_hash"]:
            lines.append(f"rules: {HOST} == {host} (set {snap['set_hash'][:8]}; theirs published {rec.get('ts', '?')} @ {rec.get('head', '?')})")
            continue
        mine, theirs = snap["files"], rec.get("files") or {}
        diffs = []
        for rel in sorted(set(mine) | set(theirs)):
            a, b = mine.get(rel, {"sha": "missing", "bytes": 0}), theirs.get(rel, {"sha": "missing", "bytes": 0})
            if a["sha"] != b["sha"]:
                diffs.append(f"{rel} {a['bytes']}B/{a['sha'][:6]} vs {b['bytes']}B/{b['sha'][:6]}")
        lines.append(f"RULES DIVERGE ⚠ {HOST} {snap['set_hash'][:8]} @ {snap['head']} vs {host} {str(rec.get('set_hash', '?'))[:8]} "
                     f"@ {rec.get('head', '?')} (their publish {rec.get('ts', '?')}): {len(diffs)} file(s) — " + "; ".join(diffs[:8])
                     + (" …" if len(diffs) > 8 else ""))
        lines.append("  Disk wins for THIS session. Before trusting a rule the other host may have changed: pull (unpulled commit?) "
                     "and `python lib/conflicts.py` (conflict copy?). A stale publish on their side clears itself at their next claim.")
    return lines


def brief(do_publish: bool = True) -> str:
    try:
        snap = snapshot()
        doc = publish(snap) if do_publish else load()
        return "\n".join(compare(doc, snap))
    except Exception as e:  # never break a claim over the hash check — but say so
        return f"rules: hash check unavailable ({type(e).__name__}: {e}) — bytes across hosts UNVERIFIED this session"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--no-publish", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    snap = snapshot()
    try:
        doc = load() if a.no_publish else publish(snap)
    except Exception as e:
        print(f"rules: store unavailable ({e}); local set {snap['set_hash']}")
        return 1
    if a.json:
        print(json.dumps({"mine": snap, "hosts": doc.get("hosts", {})}, indent=1, ensure_ascii=False))
        return 0
    for l in compare(doc, snap):
        print(l)
    return 0


if __name__ == "__main__":
    sys.exit(main())
