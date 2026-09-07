---
name: folder-lock
description: Session discipline for running many AI agents on one repo without conflicting saves — and the guards that make it unskippable. One lock per workfolder (not per task, not per project) with status open|closing, minted identifiers bound to the Claude session, handoffs instead of cross-folder edits, a per-folder resume pointer, agent-initiated signoff, and three fail-closed guards: a PreToolUse edit guard, a model-agnostic pre-commit guard (foreign locks, unclaimed/unguarded folders, missing identity, broken wiring, plain commits on main), and a Stop hook that refuses to end a turn holding a closing lock. Use at session start ("claim <folder>", "what's open"), when work drifts into another folder, when the task is done (sign off yourself), and when installing or proving the guards ("install the lock guard", "prove the hook works", "run the conflict test").
---

# /folder-lock — many agents, one repo, no conflicting saves

Read `PROTOCOL.md` once; it is one page and it is the contract. This file tells you what to DO at each moment.

## The one rule that explains everything

**The unit of coordination is the folder.** Files collide inside folders, so the lock lives on the folder, and the folder carries its own resume state. Everything a program can check about that rule, a program checks (PROTOCOL §10) — so when a guard refuses you, it is the protocol talking, not a bug to route around.

## At session start — before editing anything

1. Resolve the folder **by the path you are about to touch**: `.folder-lock/registry.yaml` `owns:` globs if the repo has one, nearest folder with a front door otherwise.
2. `python <skill>/scripts/lock.py check <folder>`
   - `LOCKED` (fresh, not yours) → **stop**. Say who/what/since when. Offer a handoff.
   - `SIGNING OFF` (fresh, `status: closing`) → the holder is mid-signoff, not stale. Never offer takeover.
   - `STALE` → say so, ask. Never silently proceed (`--force-stale` only after the owner agreed).
   - `AGENT HOLDS` → a headless run is live there. Wait or stage a handoff.
   - `FREE` → `python <skill>/scripts/lock.py claim <folder> --task "<one line>" --stream "<id>" --hint <word>` — mints the window ID, writes `status: open`, binds the ID to this Claude session (that binding IS your identity for the edit guard, the pre-commit guard and the Stop hook; no env var needed). Suggest the owner rename the terminal to the minted ID.
3. Read `<folder>/workflow-state/current-pointer.md` and `<folder>/.goal/inbox/*.staged.md`. Start at `Next concrete action:`.
4. A pre-guard hand-written lock that is yours: `python <skill>/scripts/lock.py adopt <folder>`.

Fresh window, nothing claimed: `python <skill>/scripts/board.py` first, then claim or drop.

## While working

- **Edit only inside your locked folder.** The edit guard denies anything else — write `python <skill>/scripts/handoff.py --to <folder> --task "..."` and carry on. Never argue with the denial; it is §2.
- **Stage explicit paths.** Never `git add -A`. Uncommitted changes you did not make → stop and report.
- **Commit as yourself**: inside Claude Code identity is automatic; plain terminals prefix `ICM_WINDOW=<window>`. On a feature branch in a worktree. A deliberate small solo commit on main is `MAIN_COMMIT_OK=1 git commit ...` with the reason in the message.
- **Never format an identifier by hand.** Window IDs, timestamps, handoff and drop names come from `lib/mint.py` or the writers that call it.
- If a guard blocks you, read its message: it names the lock, the holder, the paths and the remedy. `ICM_LOCK_BYPASS=1` only when the owner said so in this conversation.

## Signoff — yours to run, never the owner's to request

Run it when **any** of: the lock's task is complete · the owner signals done ("that's it", "thanks", "next task") · you are about to hand off cross-folder with nothing left here. Task merely shifted → `lock.py reopen --task "<new>"` and keep going.

1. `python <skill>/scripts/lock.py close <folder>` — `status: closing`. From here the Stop hook refuses to end the turn until steps 2–4 are done; you cannot half-sign-off.
2. Write `<folder>/workflow-state/current-pointer.md` with a typed `Next concrete action:` (PROTOCOL §3).
3. Commit your own paths. Any handoff you wrote must exist, be registered, and be committed if tracked.
4. `python <skill>/scripts/lock.py release <folder>` — it verifies pointer mtime, working tree and handoffs, then deletes the lock. A refusal lists what is missing; fix it, don't force it.

## Installing and proving the guards

```
python <skill>/scripts/install.py [<repo>] --claude-hooks
```

Copies `hooks/*` + `lib/*` to `<repo>/.githooks/`, sets `core.hooksPath`, gitignores `**/.goal/`, merges the PreToolUse + Stop hooks into `.claude/settings.json` (omit the flag to just print the JSON), then **runs `check_locks.py --self-test`**. Install is not done until it says PASS. Re-run in every clone and worktree. When the owner asks "does the hook actually work": `python <skill>/scripts/selftest.py` and `bash <skill>/tests/conflict_run.sh` (12 scenarios, real refusals, hook stdin/stdout for 1, 6, 9) — paste the result, never answer from the fact that the file exists.

## Behaviours this skill forbids

- Working in a folder without checking its lock first, however small the task.
- Editing across a folder boundary instead of writing a handoff.
- Sweeping another stream's in-flight edits into your commit.
- Deleting `LOCK.yaml` by hand — `lock.py release` is the only exit.
- Waiting for the owner to type "sign off". Ending a turn on a completed task with the lock still `open`.
- Formatting a window ID, timestamp, or handoff name yourself.
- Claiming a hook is installed without having tried to break it.

## Layout

```
SKILL.md                     what to do at each moment (this file)
PROTOCOL.md                  the one-page contract — cite it, never paraphrase it
lib/lockpath.py              ONE resolver + lock reader + identity, imported by every guard
lib/mint.py                  every identifier: window, agent, handoff, drop, timestamp
hooks/pre-commit             shim: check_locks + protect_main; no python = no commit
hooks/check_locks.py         commit guard, fail closed; --self-test, --verify-wiring
hooks/protect_main.py        refuse plain commits on main/master (MAIN_COMMIT_OK=1)
hooks/require_lock.py        PreToolUse edit guard (Edit/Write/MultiEdit/NotebookEdit)
hooks/require_signoff.py     Stop hook: no ending a turn with a closing lock
scripts/lock.py              claim | adopt | close | reopen | release | check | whoami | mine | status
scripts/handoff.py           stage | fire a task into another folder's .goal inbox (minted names)
scripts/board.py             one screen: locks (with status), pointers (typed), staged handoffs
scripts/install.py           copy hooks+lib, hooksPath, .gitignore, --claude-hooks, self-test
scripts/selftest.py          commit-guard self-test + protected-branch cases
tests/conflict_run.sh        12-scenario proof of which guardrail is active where
templates/                   LOCK.yaml, current-pointer.md
```
