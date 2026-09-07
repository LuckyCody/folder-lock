---
name: folder-lock
description: Session discipline for running many AI agents on one repo without conflicting saves. One lock per workfolder (not per task, not per project), handoffs instead of cross-folder edits, a per-folder resume pointer, and a model-agnostic git pre-commit guard that refuses commits into another session's locked folder and refuses direct commits to main. Use at session start ("claim <folder>", "take the lock", "what's open"), when work drifts into another folder, at session end ("release", "wrap up"), and when installing or verifying the hooks in a repo ("install the lock guard", "prove the hook works").
---

# /folder-lock — many agents, one repo, no conflicting saves

Read `PROTOCOL.md` once; it is one page and it is the contract. This file tells you what to DO at each moment.

## The one rule that explains everything

**The unit of coordination is the folder.** Not the task (five tasks in one folder = one lock). Not the project (a project spans folders other streams need). Not the agent (agents come and go; folders persist). Files collide inside folders, so the lock lives on the folder, and the folder carries its own resume state.

## At session start — before editing anything

1. Resolve the folder **by the path you are about to touch**, never by keyword similarity. Registry `owns:` globs if the repo has one; nearest ancestor with a front-door doc otherwise.
2. `python <skill>/scripts/lock.py check <folder>`
   - `LOCKED` (fresh, not yours) → **stop**. Tell the owner who/what/since when. Offer a handoff. Do not work around it.
   - `STALE` → say so, ask. Never silently proceed over a stale lock.
   - `AGENT HOLDS` → a headless run is live there. Wait or stage a handoff.
   - `FREE` → take it: `python <skill>/scripts/lock.py take <folder> --window "<terminal name>" --task "<one line>" --stream "<id>"`
3. Read `<folder>/workflow-state/current-pointer.md` and its inbox `<folder>/.goal/inbox/*.staged.md`. Start at the pointer's `Next concrete action:`.
4. Suggest the owner rename the terminal to the `window:` value so "who holds this" matches the taskbar.

Fresh window, nothing claimed yet: `python <skill>/scripts/board.py` first, then claim or drop (PROTOCOL §6).

## While working

- **Edit only inside your locked folder.** Work drifting into another folder → `python <skill>/scripts/handoff.py --to <folder> --task "..." [--body -]` and carry on. Never edit across the boundary "just this once".
- **Stage explicit paths.** Never `git add -A` / `git add .`. Uncommitted changes you did not make → stop and report; do not build on them.
- **Commit as yourself:** `ICM_WINDOW=<window> git commit -m "..."`. On a feature branch in a worktree. A deliberate small solo commit on main is `MAIN_COMMIT_OK=1 ICM_WINDOW=<window> git commit ...` — say why in the message.
- If the guard blocks you, read its message. It names the lock, the holder, the paths, and the remedy. Do not reach for `ICM_LOCK_BYPASS=1` without the owner saying so in this conversation.

## At session end

1. Write the FIRST MOVE into `<folder>/workflow-state/current-pointer.md` using the typed grammar (PROTOCOL §3): actionable / `WHEN … →` tripwire / `PARKED (…)` / `NONE — …`. A pointer with no `Next concrete action:` line is a defect.
2. Commit your own paths (`ICM_WINDOW=<window>`).
3. `python <skill>/scripts/lock.py release <folder> --window "<window>"`.
4. Regenerate the board if the repo keeps one checked in.

## Installing the guard in a repo

```
python <skill>/scripts/install.py [<repo>]
```

Copies `hooks/*` to `<repo>/.githooks/`, sets `core.hooksPath`, gitignores `**/.goal/`, then **runs `selftest.py`** — a throwaway repo in which it commits on main (must block), commits with `MAIN_COMMIT_OK=1` (must pass), commits into another window's fresh lock (must block), as the holder (must pass), under a stale lock (must pass), and checks that `core.hooksPath` points at an executable hook. **Install is not done until the self-test says ALL GOOD.** A hook file that exists is not a hook that runs. Re-run in every clone and worktree; `core.hooksPath` does not travel with the code.

When the owner asks "does the hook actually work" — run `selftest.py`, paste the result. Never answer from the fact that the file exists.

## Behaviours this skill forbids

- Working in a folder without checking its lock first, however small the task.
- Editing across a folder boundary instead of writing a handoff.
- Sweeping another stream's in-flight edits into your commit.
- Driving another agent's terminal with keystrokes to "hand off" work.
- Claiming a hook is installed without having tried to break it.
- Deferring the lock, the pointer update, or the release to "later".

## Layout

```
SKILL.md                    this file - what to do at each moment
PROTOCOL.md                 the one-page contract (cite it, never paraphrase it)
hooks/pre-commit            shim: runs both guards, fails OPEN + loud on infra errors
hooks/check_locks.py        refuse staged paths under another session's fresh LOCK.yaml
hooks/protect_main.py       refuse plain commits on main/master (MAIN_COMMIT_OK=1 to override)
scripts/lock.py             take | check | release | status
scripts/handoff.py          stage | fire a task into another folder's .goal inbox
scripts/board.py            one screen: locks, pointers (typed), staged handoffs
scripts/install.py          copy hooks, set core.hooksPath, gitignore .goal, run selftest
scripts/selftest.py         break the guard on purpose in a temp repo; exit 0 only if it holds
templates/LOCK.yaml         lock shape
templates/current-pointer.md  pointer shape
```
