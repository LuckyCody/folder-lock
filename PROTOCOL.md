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

**Lock granularity = the files being edited, never "the deploy unit".** One deployable app that hosts unrelated modules is NOT one lock — three sessions queuing on one lock while editing files that never touch each other is a structural collision, not a real one. Give each module folder (`<app>/<module>/**`) its own registry workflow and `.goal/`, and put the wiring — router registration, the job-runner and its job-type registry, base templates/static, the deploy script, shared auth — in `<app>/core/**` with its own lock, expected to be held rarely and briefly. The resolver works per FILE (longest matching glob), so a session touching only `<app>/billing/` acquires only that lock, touching `core/` acquires core, and a commit spanning two modules needs both — that is the point. Modules register into core (routers, job types); core never imports module internals. Serializing the deploy is §11's job, not the lock's. Example: `templates/registry.yaml`.

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

## 11. Deploy queue — HEAD is always deployable

A single lock on a deployable app conflates two concerns: **edit conflicts** (real — need a lock at the granularity of the files touched, §1) and **deploy races** (a serialization problem — deploy is idempotent, "deploy HEAD" twice = once). Keep them apart.

**Deploy units** are declared in `.folder-lock/deploy-units.yaml` (paths → deploy command → tests → dirty-ignore; example: `templates/deploy-units.yaml`). A unit is NOT a lock domain; several lock domains (modules) sit inside one unit. **Sessions never run a unit's deploy script directly.**

**Request, don't deploy.** A finished session commits its own paths and drops a request — `python scripts/deploy_request.py --for <workfolder>` → `.goal/deploy/requests/<unit>/<minted>.yaml {workflow, commit, requested_at, window}`. The signoff does this as its last commit-side step; a folder outside every unit gets "no deploy unit covers" and exit 0.

**One deployer.** `python scripts/deployer.py`, run every few minutes by any scheduler (cron, schtasks, a CI timer), holds one lock (`.goal/deploy/deployer.lock`: minted agent id + PID + 60-s heartbeat, stale 15 min — the runner `.firing.lock` pattern), takes ALL pending requests of a unit, deploys HEAD **once**, writes `{event: deployed, deployed_commit, deployed_at, build{run, log, duration_s, collapsed, requests}}` into every requesting folder's `workflow-state/deploys.jsonl` (+ `last-deploy.yaml`), and moves the requests to `.goal/deploy/done/`. Concurrent requests collapse into one deploy; a request whose commit HEAD already carries completes as `already-deployed` without a build. **Failure** → requests stay pending with `attempts+1`, a `failed` line per workflow-state, `.goal/deploy/state/failed/<unit>.yaml` — `scripts/board.py` renders it until the next success; back-off 30 min × attempts (cap 6 h), a newer request retries at once, `--retry-now` overrides. `python scripts/deployer.py status` shows the queue.

**The invariant this introduces: HEAD must always be deployable** — anyone's finish deploys everyone's committed work.
- **Commit only complete, tested states to `main`.** Work that must land in pieces goes behind a feature flag, or on a short-lived branch merged as a unit (§4 feature lane). A half-finished edit on `main` is somebody else's outage.
- **Tests before the commit:** `python scripts/test_unit.py <unit>` runs the unit's tests and leaves a per-window marker. The commit guard **WARNS — never refuses** (best effort) when a commit touches a unit without a fresh PASS marker from this session.
- **The deployer deploys the working tree at HEAD and BLOCKS** (`.goal/deploy/state/blocked/<unit>.yaml`, requests wait, board shows it) while a tracked source file under the unit is dirty, HEAD is off the unit's branch, or no pending request's commit is in HEAD. An uncommitted edit is not deployable state, and nobody else's request may ship it. Logs and scheduler state listed in `dirty_ignore` are exempt.
- **Rollback is a revert commit plus a new request** — never a hand-run deploy.

Proof: `python scripts/deploy_selftest.py` — collapse, already-deployed, failure + back-off, dirty-tree block, guard warning, in a throwaway repo.

## 12. Human blocker — the ONLY reasons an item waits on the owner [v4]

An item is `waiting_owner` only for: a missing secret/credential/access; spending money or a contract; an irreversible external data side-effect (production push to an accounting/ERP system, unrecoverable delete/overwrite); a business decision with no spec and no precedent in the folder's `memory.md`; two materially different spec readings where a wrong pick costs more than asking. Explicitly NOT blockers: external e-mail, assigning work to another folder (handoff), ambiguity a reasonable default resolves — decide, log one dated line in `memory.md`, continue. The list is a template (`templates/blockers.md`); each installation edits its own copy. A `waiting_owner` item MUST carry a decision-ready `question` (question, 2–3 options, recommendation) — `lib/items.py wait` refuses without one.

## 13. Autorun — the board is a work queue; the owner's view is the exit condition [v4]

**Schema.** Every board item (a folder's pointer, a staged handoff) carries in `<state>/items.yaml` (`lib/items.py`): `status` (`ready` | `in_progress` | `waiting_owner` | `done`, plus `waiting_world` for tripwires on the world and `parked`), `question`, `blocked_since`, `created_by`, `owner` (lock home of the target path, by path only). Files stay the source of the work; the overlay is re-derived on every scan (`actionable` → ready, `WHEN <owner-condition>` → waiting_owner with an auto-drafted question until an agent writes a real one, `WHEN <world>` → waiting_world, `NONE` → done). Only the resumer sets `in_progress`; an agent's own question is never overwritten by an auto-drafted one.

**Dedup.** No open item with the same `owner` + normalized title. `scripts/handoff.py` refuses (exit 4); `lib/items.py upsert` raises `Duplicate`. `--force` is the owner's.

**Signoff writes the next step as an item.** After commit + release: `python lib/items.py from-pointer <folder>` (pointer → ready or waiting_owner + question), one line in `<folder>/workflow-state/autorun-log.md` (`lib/autorun_log.py append`: timestamp, folder, item, decisions, commit, status), then kick the loop (`scripts/autorun.py --detach`). The session does not render the owner's menu and does not continue working — one item per fired agent; process boundaries, not compaction.

**Resumer loop** (`scripts/autorun.py --runner "<agent command>"`): per folder owning a `ready` item — skip while a fresh `LOCK.yaml`/`.firing.lock` exists (§1) → own pointer item first, else oldest ready → `in_progress` + `.firing.lock` with a minted agent id → fire ONE fresh agent process (prompt on stdin, `ICM_WINDOW`/`ICM_FOLDER`/`AUTORUN_ITEM*` in its env) → the agent works the item, applies §12, runs its own signoff → read back: unchanged → `fails+1`, three no-progress fires → `waiting_owner` with an auto question. Repeat until no `ready` item exists anywhere; no iteration/time/item cap. A dead runner is an infrastructure error, never counted against an item.

**Exit condition.** The owner is shown the board only when `ready == 0`: `waiting_owner` items grouped by owner with question + `blocked_since`, plus the autorun-log digest since they last looked (`lib/autorun_log.py digest`). Invariants unchanged: HEAD always deployable (every fired agent starts from and commits to a clean state), one lock per folder routed by path, deploys via the §11 queue only.

