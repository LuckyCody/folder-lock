# Folder-lock protocol — many agents, one repo, no conflicting saves

One page. Every workfolder's front door (CLAUDE.md / AGENTS.md / README) references this file with one line; the rules are never pasted per folder. Written for humans and agents alike. **Every rule below that a program can check IS checked by a program** (§10) — a rule that lives only as text gets skipped the moment a task feels small.

The core idea: **the unit of coordination is the folder, not the task, not the project, not the agent.** A task is too small (five tasks in one folder would mean five locks). A project is too big (one project spans folders other streams need). A folder is where files actually collide, so that is where the lock lives.

## 1. The folder lock — strict, one per folder

One mutual-exclusion domain per workfolder, two carrier files inside the folder's `.goal/` dir (`lock.py claim` creates it):

- `LOCK.yaml` — interactive sessions. Stale after **24 h**.
- `.firing.lock` — headless agents launched by a runner. Stale after **15 min**.

They exclude each other: a runner skips any folder holding a fresh `LOCK.yaml`; an interactive session treats a fresh `.firing.lock` as "an agent holds this folder".

**Which folder?** By the PATH being touched, never by keyword. With a registry (`.folder-lock/registry.yaml`, `workflows: - id / owns: [globs]`) the path resolves to its workflow's HOME folder (fixed prefix of the first glob; nested workflows → longest match). Top-level files, `.claude/**`, `.github/**` → the repo-root lock `<repo>/.goal/LOCK.yaml`. Unregistered paths → nearest ancestor with `.goal/`; none → **unguarded**, and every guard refuses until the folder is claimed. One implementation: `lib/lockpath.py::resolve` — every guard imports it, so they cannot disagree.

**Session first act in a workfolder:** `python scripts/lock.py check <folder>`.

- Fresh lock, not yours → **stop**. Report who/what/since when (it's in the lock). Never work around it.
- Fresh lock with `status: closing` → the holder is **signing off, not stale**. Wait or stage a handoff; never offer takeover.
- Stale lock → say so and ask the owner. **Never silently proceed** (`--force-stale` only after they agreed).
- Free → `python scripts/lock.py claim <folder> --task "<one line>" [--stream S] [--hint word]`:

```yaml
holder: interactive        # interactive | fired
window: "<minted window ID>"   # lib/mint.py — doubles as commit identity (ICM_WINDOW)
status: open               # open | closing (§9)
task: "<one line>"
stream: "<stream/workflow id>"
branch: "feat/<stream>"
started: "YYYY-MM-DDTHH:MM"   # minted
```

**Session last act** is the agent-initiated signoff (§9): `close` → pointer → commit → `release`. Locks are runtime state: gitignored (`**/.goal/`), never committed. One session may hold a second folder's lock only when the owner's task explicitly spans both — `claim` then reuses the session's window so every commit stays attributable; drift into a third folder is still a handoff (§2).

## 1b. Identity — minted, bound, never hand-formatted

`lib/mint.py` is the ONLY place an identifier is formatted: window IDs (`<hint>-<yymmdd>-<4>`), lock timestamps, handoff names, drop names, agent IDs. Agents call it or go through the writers that call it (`scripts/lock.py`, `scripts/handoff.py`). An agent that types an ID by hand is a defect.

| Environment | Identity carrier | Set by |
|---|---|---|
| Claude Code (VS Code / CLI), interactive | `<repo>/.goal/sessions/<CLAUDE_CODE_SESSION_ID>.yaml` → `window:` | `lock.py claim` / `adopt`. Claude Code exposes `CLAUDE_CODE_SESSION_ID` to Bash and hands hooks the same value as `session_id` — the binding IS the export, no env var needed |
| Headless agent launched by a runner | `ICM_WINDOW` (+ `ICM_FOLDER`) in the process env | the runner, from the agent id it wrote into `.firing.lock` |
| Plain terminal (human, other runner) | `ICM_WINDOW` env var | you |

Both carriers present and different → every guard refuses. **Neither present is an ERROR state**: the edit guard denies, the commit guard refuses, the Stop hook blocks once per prompt with "no window identity". A hand-written pre-guard lock is bound with `python scripts/lock.py adopt <folder>`.

## 2. Boundary-crossing = handoff, never drift

When your work crosses into another folder: **the lock says stop, the handoff says what to do instead.** Do not edit across the boundary (the edit guard refuses anyway):

```
python scripts/handoff.py --to <target folder> --task "<one line>" [--mode stage|fire] [--body <file|->]
```

- **stage** (default): lands in `<target>/.goal/inbox/<minted>.staged.md`; whoever next claims the target reads its inbox first.
- **fire**: additionally writes `<target>/.goal/state.yaml` (`status: in_progress`) for a headless runner. Refuses to clobber a live goal and refuses PARKED folders.
- File-based invocation ONLY. Driving another agent's window with keystrokes is banned.
- Every handoff is recorded on the writer's session binding; `lock.py release` refuses while one is orphaned (§9).

## 3. Per-folder resume — the current pointer

Every active workfolder keeps `workflow-state/current-pointer.md`:

```
# Current pointer — <workflow-id>
**<current phase>** (authorizing date / ruling)
Next concrete action: <one executable step>
After this: <remaining phases, one line each>
Resume handle: read this file + workflow.yaml. Domain contract lives in <front door / canon files>.
```

`Next concrete action:` is **typed**: plain → **actionable**; `WHEN <condition> → <action>` → **tripwire** (never counts as procrastination); `PARKED (<date>, owner) — <reason>. Resume: <condition>` → **parked** (off-board, runners never touch it, only the owner parks); `NONE — <what closed>` → **closed**; missing → **mute** (a defect). The pointer's mtime is the Stop hook's proof that signoff step 2 happened.

## 4. Two git lanes — defaults, not rulings

- **Feature lane**: worktree → `feat/*` → PR → squash-merge → delete the branch. Run `python scripts/install.py` in every clone and worktree; `core.hooksPath` does not travel with the code.
- **Solo lane**: a deliberate small commit on `main` — `MAIN_COMMIT_OK=1`, explicit paths only. `hooks/protect_main.py` refuses a plain commit on `main`/`master`: a "never on main" rule that lives in a context window is skipped the moment a task feels small.

## 5. Foreign-changes rule — ENFORCED by the commit guard

A session encountering uncommitted changes it did not make **stops and reports** — never builds on them, never `git add -A` / `git add .` across streams. Stage explicit paths only.

`hooks/pre-commit` → `check_locks.py` refuses, and says why, when: a staged path sits under another window's fresh lock · under a guarded folder with no fresh lock of yours ("claim first") · in an unguarded folder ("claim it, which creates `.goal/`") · identity is missing or conflicting · the registry exists but is unreadable · **the hook is not actually wired** (git's hook dir ≠ the guard's dir — the silently-disabled case) · any internal error. Commit identity is automatic inside Claude Code (session binding); plain terminals prefix `ICM_WINDOW=<window> git commit …`. Foreign paths in your index → `git restore --staged <path>`. Owner-approved override only: `ICM_LOCK_BYPASS=1` (say so). `check_locks.py --self-test` proves the refusal in a fixture and records `.goal/selftest_last.json`.

## 6. Dispatcher mode — fresh window, no folder claimed

`python scripts/board.py` — every lock (with `status`), every pointer (typed), every staged handoff. Resolve the ask to a folder by path, then **claim-and-become** (lock → pointer → rename the window to the minted ID → work) or **drop** (stage/fire into the target's `.goal` inbox). Wrong-folder landings self-correct via §2 — the edit guard will not let you improvise in place.

## 7. Drops

One sanctioned drop location (e.g. `_inbox/`), names minted (`python lib/mint.py drop <label>`). Consuming a drop MEANS filing it into the owning folder, recording the move, deleting the drop.

## 8. Canon hygiene

Cite rulings by file + number, never paraphrase. Nothing retired goes bannerless. Knowledge is canon-with-a-home or code-with-a-pointer, never pointer-only.

## 9. Signoff is agent-initiated

The agent, not the owner, decides that a shift is over. It sets `status: closing` (`python scripts/lock.py close <folder>`) and runs the signoff when ANY of:

- a) the lock's `task:` is complete;
- b) the owner signals done — "that's it", "thanks", "next task", "new task", or equivalent;
- c) it is about to write a cross-folder handoff and no work remains in this folder.

Mid-task turns keep `status: open`; the Stop hook lets them end. Task shifted → `lock.py reopen --task "<new line>"`, stay open. Signoff is never something the owner requests; the phrases are signal b), not a command the ritual waits for. Headless agents run the same signoff before they exit; the runner's lock clear is only the crash fallback.

**Order, enforced by `lock.py release`:** `close` → write `workflow-state/current-pointer.md` → commit your own paths → `release`, which refuses while: the pointer is older than the lock start · anything under the folder is uncommitted (`--allow-dirty "<why>"` records the exception) · a handoff this session wrote is gone or unregistered · a tracked handoff is uncommitted. Then the lock goes away and the Stop hook lets the turn end.

## 10. Rationale — why every rule has an executable guard

Two failure classes: (a) rules skipped when a task felt small — signoff forgotten, lock never taken before a "quick" edit; (b) guards that passed silently when they had nothing to check — no `.goal/`, no identity, hook not wired. Text in a context window cannot fix (a); fail-open code cannot fix (b).

**The rule: a guard that cannot evaluate fails CLOSED, never open.** Unreadable registry, missing identity, unguarded folder, broken wiring, internal exception — each refuses and says what is missing. Loud and wrong beats quiet and permissive.

**The ladder — each rung catches what the one above cannot:**

| Guard | Where | Catches | Cannot catch |
|---|---|---|---|
| Edit-time guard `require_lock.py` (PreToolUse on Edit/Write/MultiEdit/NotebookEdit) | Claude Code, before the tool runs | editing without a lock, under someone else's lock, in unguarded folders — **where it happens, before files tangle** | writes via Bash/PowerShell, other runners, humans |
| Commit guard `check_locks.py` (pre-commit) | git — any agent, any model, any human | staged paths under another window's lock, unclaimed/unguarded folders, `git add -A` sweeps — **the model-agnostic last line** | anything that never reaches `git commit`: uncommitted damage, destructive git ops |
| Branch guard `protect_main.py` (pre-commit) | git | plain commits on `main`/`master` | force-pushes from elsewhere; needs remote branch protection too |
| Permission layer (`.claude/settings.json` `permissions.ask`) | Claude Code, before Bash/PowerShell runs | destructive git ops that bypass pre-commit: `reset --hard`, `checkout .`, `restore .`, `stash`, `clean`, `push --force`, `rm -rf` — asks per subcommand, also inside `&&`/`;`, also in auto mode | `git restore <single path>` (indistinguishable from `--staged` by pattern), `bypassPermissions` mode, non-Claude runners |
| Stop hook `require_signoff.py` | Claude Code, end of turn | ending a turn holding a `closing` lock with the pointer stale or the lock not released; ending with no identity (one nag per prompt) | an agent that never sets `closing` — that is the honest mid-task state and the board shows it |

Hook decisions never bypass permission rules, and a blocking hook takes precedence over allow rules (Claude Code docs) — the ladder composes.

**Environment matrix — which guardrail is active where:**

| | Interactive Claude Code | Headless agent (`claude --print`, cwd = repo, `ICM_WINDOW` set by runner) | Plain terminal / other runner |
|---|---|---|---|
| Identity | session binding | env | env or refused |
| Edit-time guard | active | active (project hooks load from cwd) | — |
| Commit + branch guard | active | active | active (git) |
| Permission `ask` | active (also auto mode); off in bypassPermissions | an ask denies (no human) | — |
| Stop hook | active | active | — |
| Proof | `bash tests/conflict_run.sh` (12 scenarios) + `check_locks.py --self-test` | scenarios 4–5 + the same hooks | scenarios 2–3, 9–12 |

Every guard decision is appended to `<repo>/.goal/guard_log.jsonl`.
