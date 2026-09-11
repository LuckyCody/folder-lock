"""Read-coverage guard — Claude Code PostToolUse hook on Read (PROTOCOL §15, v4.2).

The failure this catches sits BELOW the file: the file is correct and complete on disk, the session opened it,
and only part of it reached the model (a `limit`/`offset` slice, or the Read tool's 2000-line cap on a long
pointer). The session then reports finished work as pending, or acts on a superseded "Next concrete action".
No hash on disk can flag that — the proof has to come from the read itself.

.claude/settings.json (scripts/install.py --claude-hooks writes this):
  "PostToolUse": [{"matcher": "Read", "hooks": [{"type": "command", "command": "python .githooks/check_pointer_read.py"}]}]

For the files that carry state an agent acts on — `workflow-state/current-pointer.md`, `.goal/inbox/*.md`,
CLAUDE.md / AGENTS.md / PROTOCOL.md, any SKILL.md — it injects one line of PROOF after every read:

  READ IN FULL ✓ <rel>: <lines> lines · <bytes> B · sha256 <12>
  PARTIAL READ ⚠ <rel>: lines a–b of N … + the pointer's `Next concrete action:` line VERBATIM

so a partial read is visible in the transcript (and in the guard log) instead of silently looking complete,
and the load-bearing line reaches the model even when the slice missed it. Fails OPEN by design: a Read that
already happened cannot be un-read; this guard makes it honest. `lock.py claim` prints the same proof up front.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _c in (HERE / "lib", HERE.parent / "lib"):
    if (_c / "lockpath.py").is_file():
        sys.path.insert(0, str(_c))
        break

READ_CAP = 2000  # the Read tool's default line window when no limit is given

WATCH_SUFFIXES = ("workflow-state/current-pointer.md", "/CLAUDE.md", "/AGENTS.md", "/PROTOCOL.md", "/SKILL.md")
WATCH_EXACT = {"CLAUDE.md", "AGENTS.md", "PROTOCOL.md", "SKILL.md"}


def _watched(rel: str) -> bool:
    if rel in WATCH_EXACT or rel.endswith(WATCH_SUFFIXES):
        return True
    return ("/.goal/inbox/" in rel or rel.startswith(".goal/inbox/")) and rel.endswith(".md")


def _emit(text: str) -> None:
    sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                                         "additionalContext": text}}, ensure_ascii=False) + "\n")


def main() -> int:
    try:
        inp = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0
    if inp.get("tool_name") != "Read":
        return 0
    ti = inp.get("tool_input") or {}
    fp = ti.get("file_path")
    if not fp:
        return 0
    import lockpath as lp
    rel = lp.to_rel(fp)
    if rel is None or not _watched(rel):
        return 0
    p = Path(fp)
    try:
        b = p.read_bytes()
    except OSError:
        return 0
    lines = b.decode("utf-8", "replace").splitlines()
    total = len(lines)
    try:
        offset = int(ti.get("offset") or 1)
    except (TypeError, ValueError):
        offset = 1
    try:
        limit = int(ti.get("limit")) if ti.get("limit") else None
    except (TypeError, ValueError):
        limit = None
    start = max(1, offset)
    end = min(total, start + (limit if limit else READ_CAP) - 1)
    full = start <= 1 and end >= total
    sha = hashlib.sha256(b).hexdigest()[:12]
    ctx = {"guard": "pointer_read", "session_id": inp.get("session_id", ""), "rel": rel, "lines": total,
           "bytes": len(b), "sha": sha, "from": start, "to": end, "full": full}
    try:
        lp.guard_log(ctx)
    except Exception:
        pass
    if full:
        _emit(f"READ IN FULL ✓ {rel}: {total} lines · {len(b)} B · sha256 {sha}")
        return 0
    missed = total - max(0, end - start + 1)
    msg = [f"PARTIAL READ ⚠ {rel}: lines {start}–{end} of {total} reached you ({len(b)} B on disk, sha256 {sha}); "
           f"{missed} line(s) did not. Do not report this file's state or act on it as read until the whole file has "
           f"reached you (Read again without offset/limit, or in consecutive slices covering 1–{total})."]
    if rel.endswith("current-pointer.md"):
        for i, l in enumerate(lines, 1):
            if l.startswith("Next concrete action:"):
                msg.append(f"Next concrete action (verbatim, L{i}): {l}")
                break
        else:
            msg.append("This pointer carries NO `Next concrete action:` line — invalid grammar (PROTOCOL §3); fix it before signoff.")
        for i, l in enumerate(lines, 1):
            if l.startswith("Resume handle:"):
                msg.append(f"Resume handle (L{i}): {l}")
                break
    _emit("\n".join(msg))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)  # a read already happened; never turn a proof hook into a blocker
