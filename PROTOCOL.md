# Folder-lock protocol — many agents, one repo, no conflicting saves

One page. Every workfolder's front door (CLAUDE.md / AGENTS.md / README) references this file with one line; the rules are never pasted per folder. Written for humans and agents alike — an agent reads this the same way a new teammate does.

The core idea: **the unit of coordination is the folder, not the task, not the project, not the agent.** A task is too small (five tasks in one folder would mean five locks). A project is too big (one project spans folders other streams need). A folder is where files actually collide, so that is where the lock lives.

## 1. The folder lock — strict, one per folder

One mutual-exclusion domain per workfolder, carried by two files inside the folder's `.goal/` dir (create it if absent):

- `LOCK.yaml` — interactive sessions (a human + an agent in a terminal). Stale after **24 h**.
- `.firing.lock` — auto-fired headless agents. Stale after **15 min**.

They exclude each other: a headless runner skips any folder holding a fresh `LOCK.yaml`; an interactive session treats a fresh `.firing.lock` as "an agent holds this folder".

**Session first act in a workfolder:** check both files (`python scripts/lock.py check <folder>`).

- Fresh lock, not yours → **stop**. Report who/what/since when (it's all in the lock). Never work around it.
- Stale lock → say so and ask the owner. **Never silently proceed** over a stale lock. A lock is not proof of liveness, so this needs a human call.
- No lock → take it (`python scripts/lock.py take <folder> --window <name> --task "..."`):

```yaml
holder: interactive        # interactive | fired
window: "<terminal/window name>"   # so "who holds this" matches what the owner sees on the taskbar
task: "<one line>"
stream: "<stream/workflow id>"
branch: "feat/<stream>"    # or the worktree path
started: "YYYY-MM-DDTHH:MM"
```

**Session last act:** update the folder's `workflow-state/current-pointer.md` (§3) → commit → release the lock. Locks are runtime state: gitignored (`**/.goal/`), never committed.

The lock's `window:` doubles as your **commit identity**: the §5 guard lets a commit touch a locked folder only when `ICM_WINDOW=<that window>` is set on the commit command.

Same-folder parallel sessions are **never intentional**. Top-level files (outside every workfolder) fall under a repo-root `.goal/LOCK.yaml`.

**How the folder is chosen:** by the path you are about to edit, never by keyword similarity. If your repo has a registry of workflows with `owns:` globs, resolve the path against it. If not, the folder is the nearest ancestor that has its own front-door doc. Two workflows that both "touch payroll PDFs" are still two folders.

## 2. Boundary-crossing = handoff, never drift

When your work crosses into another folder's territory: **the lock says stop, the handoff says what to do instead.** Do not edit across the boundary — write a handoff:

```
python scripts/handoff.py --to <target folder> --task "<one line>" [--mode stage|fire] [--body <file|->]
```

- **stage** (default; work that needs a human mid-flight): lands in `<target>/.goal/inbox/*.staged.md`. Whoever next claims the target folder reads its inbox first. If you run a SessionStart hook, inject staged handoffs there.
- **fire** (mechanical, fully specified work): additionally writes `<target>/.goal/state.yaml` (`status: in_progress`) for a headless goal-runner to pick up. Fire refuses to clobber a live goal and refuses PARKED folders.
- File-based invocation ONLY. Driving another agent's window with keystrokes is banned — it is unauditable and it breaks the lock model.

## 3. Per-folder resume — the current pointer

Every active workfolder keeps `workflow-state/current-pointer.md`:

```
# Current pointer — <workflow-id>
**<current phase>** (authorizing date / ruling)
Next concrete action: <one executable step>
After this: <remaining phases, one line each>
Resume handle: read this file + workflow.yaml. Domain contract lives in <front door / canon files>.
```

The `Next concrete action:` line is **typed**; the board classifies every pointer by it:

- `Next concrete action: <executable step>` — **actionable** (the normal case; ages)
- `Next concrete action: WHEN <condition> → <action>` — **tripwire**: an armed wait on the world; never counts as procrastination
- `Next concrete action: PARKED (<date>, owner) — <reason>. Resume: <condition>` — **parked**: owner suspension; leaves the open board; headless runners never touch the folder. Only the owner parks.
- `Next concrete action: NONE — <what closed>` — **closed**: no open work; codename retires
- Missing line — **mute**: a defect; rendered as one collapsed board line until fixed

The end-of-session ritual writes the FIRST MOVE into the folder's pointer. The folder is the carrier of its own resume state, so any window — or any agent, or any model — can pick it up cold.

## 4. Two git lanes — defaults, not rulings

- **Feature lane** (anything someone might review): worktree → `feat/*` branch → PR → squash-merge → delete the branch.
- **Solo lane** (small solo work, docs, run-state): direct commit to main, **explicitly** (`MAIN_COMMIT_OK=1`), staging only your own paths.

The `protect_main.py` hook refuses a plain commit on `main`/`master`. This is deliberate: a "never work on main" rule that lives only as text in a context window gets skipped the moment a task feels small. Git refusing is not a suggestion, and it does not care which model is in the terminal.

## 5. Foreign-changes rule — ENFORCED by the lock guard

A session encountering uncommitted changes it did not make **stops and reports** — never builds on top of them, never `git add -A` / `git add .` across streams. Stage explicit paths only.

**Enforcement:** the `pre-commit` hook (`check_locks.py`) **blocks any commit whose staged paths lie under another session's fresh `LOCK.yaml`**. Commit as yourself with `ICM_WINDOW=<window> git commit …` (must match the lock's `window:`, case-insensitive). Foreign paths in your index → `git restore --staged <path>`. Owner-approved override only: `ICM_LOCK_BYPASS=1` (state it in the commit message). The guard fails OPEN on infrastructure errors — a broken guard must be loud, never silently permissive — and treats a malformed lock as foreign.

**The hook is only installed when it has been broken on purpose.** `scripts/install.py` sets `core.hooksPath` and then runs `selftest.py`, which builds a throwaway repo and attempts every violation the guard exists to stop. A hook file that exists is not a hook that runs: `core.hooksPath` is per-clone config, an old setting pointing elsewhere silently disables everything. Re-run install in every clone and worktree.

## 6. Dispatcher mode — fresh window, no folder claimed

A fresh session that has not claimed a folder does not improvise. It runs the board (`python scripts/board.py`) — every lock, every pointer, every staged handoff — resolves the owner's ask to a folder by path, then either:

- **claim-and-become**: take the folder lock, read its current pointer, rename the window to match, work; or
- **drop**: write the task into the target's `.goal` inbox (stage/fire) and report where it landed.

Wrong-folder landings self-correct: if the lock or the registry says you're in the wrong folder, redirect via §2 — don't edit in place. A dead window costs nothing: fresh window → board → resume from the pointer.

## 7. What this is not

Not a replacement for branch protection on the remote, not a permission layer, not a security control. It is the layer that keeps parallel agents from overwriting each other's in-flight work in one working tree, and it makes "never on main" something git enforces instead of something an agent remembers.
