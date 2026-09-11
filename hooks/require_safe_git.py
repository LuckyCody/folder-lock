"""Destructive-git guard — Claude Code PreToolUse hook on Bash / PowerShell (PROTOCOL §10 ladder, v4.3).

Headless agents run with bypassPermissions, so the `permissions.ask` rules in .claude/settings.json never fire for them —
`git clean` and `git stash -u|-a` (which DELETE every gitignored/untracked file under the tree: run data, exports, staged
handoff notes, the deploy queue) were outside every guard. Hooks run in every permission mode, so this one is the fence.

Wired by `scripts/install.py --claude-hooks`:
  {"matcher": "Bash|PowerShell", "hooks": [{"type": "command", "command": "python .githooks/require_safe_git.py"}]}

Decision (TEXT-based over the whole command line; a guard that cannot evaluate fails CLOSED):
  `git clean` (any form, unless --dry-run / -n only)                          -> DENY
  `git stash` with -u / --include-untracked / -a / --all (push/save/bare)     -> DENY
  `git checkout -- .` / `git checkout .` / `git restore .` / `git restore --worktree .`
     (whole-tree discard of live edits on a shared tree)                      -> DENY
  anything else                                                               -> allow (silent)
  ICM_ALLOW_DESTRUCTIVE_GIT=1 in the process env (owner said so, this shell)  -> allow, logged

Text-based means a commit message, an `echo` payload or a heredoc that quotes one of the forms is refused too — build
test inputs by string concatenation ("git " + "clean -fdx") and word commit messages without `git` before the form.

Output on deny: JSON permissionDecision deny + exit 2. Every decision that is not a silent allow is appended to
<STATE_ROOT>/guard_log.jsonl (lockpath.guard_log, §14).
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _c in (_HERE / "lib", _HERE.parent / "lib"):      # installed: .githooks/lib ; in the skill repo: hooks/../lib
    if _c.is_dir():
        sys.path.insert(0, str(_c))
        break

SHELL_TOOLS = {"Bash", "PowerShell"}
# one git invocation = "git" + options + subcommand + its args, up to the next shell separator
_GIT_CALL = re.compile(r"(?<![\w.-])git\s+((?:-{1,2}[\w-]+(?:=\S+|\s+[^-\s]\S*)?\s+)*)(clean|stash|checkout|restore)\b([^;&|\n]*)", re.I)
_UNTRACKED = re.compile(r"(?:^|\s)(?:-[a-z]*[ua][a-z]*|--include-untracked|--all)(?=\s|$)", re.I)
_DRY = re.compile(r"(?:^|\s)(?:-n|--dry-run)(?=\s|$)")


def _log(ctx: dict) -> None:
    try:
        import lockpath as lp
        lp.guard_log(ctx)
    except Exception:
        pass


def _deny(reason: str, ctx: dict) -> int:
    ctx.update({"decision": "deny", "reason": reason})
    _log(ctx)
    sys.stdout.write(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": f"[require_safe_git] {reason}"}}) + "\n")
    sys.stderr.write(f"[require_safe_git] {reason}\n")
    return 2


def verdict(command: str) -> str:
    """'' when the command is fine, else the reason it is refused. Pure function (tests)."""
    for m in _GIT_CALL.finditer(command or ""):
        sub, rest = m.group(2).lower(), m.group(3) or ""
        if sub == "clean":
            if _DRY.search(rest) and not re.search(r"(?:^|\s)-[a-z]*f[a-z]*(?=\s|$)|--force", rest, re.I):
                continue                                      # a dry run lists, deletes nothing
            return ("`git clean` deletes every untracked/ignored file under the tree (run data, exports, staged notes, the "
                    "deploy queue) — refused for every session. Delete named paths explicitly instead.")
        if sub == "stash":
            if _UNTRACKED.search(rest):
                return ("`git stash -u/-a` moves untracked + ignored files off the shared tree — refused. "
                        "Set work aside with a temporary WIP commit, or `git stash push -m <tag> -- <tracked paths>`.")
            continue
        if sub in ("checkout", "restore"):
            args = rest.strip()
            if re.search(r"(?:^|\s)--\s+\.\s*$|(?:^|\s)\.\s*$", args) and not re.search(r"(?:^|\s)--staged(?=\s|$)", args):
                return (f"`git {sub} … .` discards EVERY live edit under the current directory of the shared tree — refused. "
                        "Name the paths you mean (PROTOCOL §5: foreign edits are never reverted).")
    return ""


def main() -> int:
    raw = sys.stdin.read()
    ctx: dict = {"guard": "require_safe_git"}
    try:
        inp = json.loads(raw or "{}")
    except json.JSONDecodeError as e:
        return _deny(f"hook input unparseable ({e}); refusing rather than guessing", ctx)
    tool = inp.get("tool_name", "")
    if tool not in SHELL_TOOLS:
        return 0
    cmd = str((inp.get("tool_input") or {}).get("command") or "")
    ctx.update({"session_id": inp.get("session_id", ""), "tool": tool, "command": cmd[:300],
                "window": os.environ.get("ICM_WINDOW", "")})
    why = verdict(cmd)
    if not why:
        return 0
    if os.environ.get("ICM_ALLOW_DESTRUCTIVE_GIT") == "1":
        ctx.update({"decision": "allow", "reason": "ICM_ALLOW_DESTRUCTIVE_GIT=1 (owner-approved shell)"})
        _log(ctx)
        return 0
    return _deny(why, ctx)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:  # fail CLOSED
        sys.stdout.write(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason": f"[require_safe_git] guard error {e!r} — fails closed"}}) + "\n")
        sys.exit(2)
