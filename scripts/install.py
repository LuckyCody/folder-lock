"""Install the folder-lock guards into a repo and PROVE they hold.

  python scripts/install.py [<repo path>] [--claude-hooks]

  1. copies hooks/* and lib/*.py into <repo>/.githooks/ (+ .githooks/lib/)
  2. sets `git config core.hooksPath .githooks` — and tells you if it pointed elsewhere
     (a stale hooksPath is exactly how a hook silently stops running)
  3. adds `**/.goal/` to .gitignore (locks, sessions, guard log = runtime state)
  4. --claude-hooks: merges the PreToolUse edit guard + Stop guard into <repo>/.claude/settings.json
     (existing hooks kept; prints the JSON otherwise so you can wire it by hand)
  5. runs `check_locks.py --self-test` — a hook you have not tried to break is a hook you are
     assuming works. Install is not done until it says PASS.

Re-run in EVERY clone and EVERY worktree: core.hooksPath is per-repo config, it does not travel.
"""
from __future__ import annotations

import json
import shutil
import stat
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent

CLAUDE_HOOKS = {
    "PreToolUse": [{"matcher": "Edit|Write|MultiEdit|NotebookEdit",
                    "hooks": [{"type": "command", "command": "python .githooks/require_lock.py", "timeout": 20}]}],
    "Stop": [{"hooks": [{"type": "command", "command": "python .githooks/require_signoff.py", "timeout": 20}]}],
}


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace")


def main(argv: list) -> int:
    claude = "--claude-hooks" in argv
    args = [a for a in argv if not a.startswith("--")]
    target = Path(args[0]).resolve() if args else Path.cwd()
    top = git(target, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        print(f"ERROR: {target} is not inside a git repo", file=sys.stderr)
        return 2
    repo = Path(top.stdout.strip())
    dest = repo / ".githooks"
    (dest / "lib").mkdir(parents=True, exist_ok=True)
    n = 0
    for f in (SKILL / "hooks").iterdir():
        if f.is_file():
            shutil.copy2(f, dest / f.name); n += 1
            (dest / f.name).chmod((dest / f.name).stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    for f in (SKILL / "lib").glob("*.py"):
        shutil.copy2(f, dest / "lib" / f.name); n += 1
    print(f"copied {n} files -> {dest}")

    prev = git(repo, "config", "--get", "core.hooksPath").stdout.strip()
    if prev and prev not in (".githooks", str(dest)):
        print(f"NOTE: core.hooksPath was '{prev}' - a hook there or in .git/hooks/ was NOT running. Now: .githooks")
    elif not prev and (repo / ".git" / "hooks" / "pre-commit").is_file():
        print("NOTE: an existing .git/hooks/pre-commit stops running once core.hooksPath is set. Merge it into .githooks/pre-commit if needed.")
    git(repo, "config", "core.hooksPath", ".githooks")
    print("set core.hooksPath = .githooks")

    gi = repo / ".gitignore"
    text = gi.read_text(encoding="utf-8") if gi.is_file() else ""
    if "**/.goal/" not in text.splitlines():
        with gi.open("a", encoding="utf-8") as fh:
            if text and not text.endswith("\n"):
                fh.write("\n")
            fh.write("**/.goal/\n")
        print("added **/.goal/ to .gitignore")

    settings = repo / ".claude" / "settings.json"
    if claude:
        data = {}
        if settings.is_file():
            try:
                data = json.loads(settings.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                print(f"ERROR: {settings} is not valid JSON ({e}) - not touching it", file=sys.stderr)
                return 3
        hooks = data.setdefault("hooks", {})
        for event, entries in CLAUDE_HOOKS.items():
            cur = hooks.setdefault(event, [])
            for entry in entries:
                cmd = entry["hooks"][0]["command"]
                if not any(cmd in json.dumps(e) for e in cur):
                    cur.append(entry)
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"merged PreToolUse + Stop guards into {settings.relative_to(repo).as_posix()} (Claude Code reloads hooks live)")
    else:
        print("\nClaude Code edit-time + Stop guards are NOT wired (pass --claude-hooks, or add to .claude/settings.json):")
        print(json.dumps({"hooks": CLAUDE_HOOKS}, indent=2))

    print("\nself-test (the guard has to be broken on purpose before it counts as installed):", flush=True)
    rc = subprocess.run([sys.executable, str(dest / "check_locks.py"), "--self-test"], cwd=repo).returncode
    print("\nINSTALLED and PROVEN. Repeat in every clone/worktree." if rc == 0 else "\nINSTALL INCOMPLETE - the guard did not hold.",
          file=sys.stdout if rc == 0 else sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
