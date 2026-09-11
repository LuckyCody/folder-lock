---
name: folder-lock
description: Session discipline for running many AI agents on one repo without conflicting saves — and the guards that make it unskippable. One lock per workfolder (not per task, not per project, not per deploy unit — per module, with a shared core lock) with status open|closing, minted identifiers bound to the Claude session, handoffs instead of cross-folder edits, a per-folder resume pointer, agent-initiated signoff, a deploy queue (sessions request, one deployer ships HEAD; "HEAD is always deployable"), and three fail-closed guards: a PreToolUse edit guard, a model-agnostic pre-commit guard (foreign locks, unclaimed/unguarded folders, missing identity, broken wiring, plain commits on main; warns on untested deploy units), and a Stop hook that refuses to end a turn holding a closing lock. Use at session start ("claim <folder>", "what's open"), when work drifts into another folder, when the task is done (sign off yourself, request the deploy), and when installing or proving the guards ("install the lock guard", "prove the hook works", "run the conflict test", "prove the deploy queue").
---

# /folder-lock — many agents, one repo, no conflicting saves

Read `PROTOCOL.md` once; it is one page and it is the contract. This file tells you what to DO at each moment.

## The one rule that explains everything

**The unit of coordination is the folder.** Files collide inside folders, so the lock lives on the folder, and the folder carries its own resume state. Everything a program can check about that rule, a program checks (PROTOCOL §10) — so when a guard refuses you, it is the protocol talking, not a bug to route around.

## At session start — before editing anything

1. Resolve the folder **by the path you are about to touch**: `.folder-lock/registry.yaml` `owns:` globs if the repo has one, nearest folder with a front door otherwise. Inside a multi-module app the lock is the MODULE folder (or `core/` for wiring) — never "the whole app" (PROTOCOL §1, `templates/registry.yaml`).
2. `python <skill>/scripts/lock.py check <folder>`
   - `LOCKED` (fresh, not yours) → **stop editing there**, but do not park the work: the report names the holder's peer address (`Holder: <name> · live`) — **message them first** ("release ETA, or hand it over?", PROTOCOL §16). Stage a handoff only when the holder is unreachable (unbound here / headless) or says "not soon". `Holder: … GONE` = orphaned lock → ask the owner (§1). `python <skill>/scripts/lock.py who` lists every lock with its holder.
   - exit 5 `CONFLICT COPY` → a divergent sync conflict copy of a load-bearing file sits outside this folder (PROTOCOL §15). Claim the folder that owns the canonical and fold it there; never read the copy as the rule.
   - `SIGNING OFF` (fresh, `status: closing`) → the holder is mid-signoff, not stale. Never offer takeover.
   - `STALE` → say so, ask. Never silently proceed (`--force-stale` only after the owner agreed).
   - `AGENT HOLDS` → a headless run is live there. Wait or stage a handoff.
   - `FREE` → `python <skill>/scripts/lock.py claim <folder> --task "<one line>" --stream "<id>" --hint <word>` — mints the window ID, writes `status: open`, binds the ID to this Claude session (that binding IS your identity for the edit guard, the pre-commit guard and the Stop hook; no env var needed). Suggest the owner rename the terminal to the minted ID.
3. Read `<folder>/workflow-state/current-pointer.md` and `<folder>/.goal/inbox/*.staged.md` **in full** — the claim printed the pointer's size, sha256 and its action line; the PostToolUse hook answers every Read with `READ IN FULL ✓` or `PARTIAL READ ⚠` (PROTOCOL §15). Never report a file's state after a ⚠ line. Start at `Next concrete action:`. If the claim printed `FIRST ACT … fold:`, fold that conflict copy before anything else.
4. A pre-guard hand-written lock that is yours: `python <skill>/scripts/lock.py adopt <folder>`.

Fresh window, nothing claimed: `python <skill>/scripts/board.py menu` first (it is the §13 gate: with ready items it shows the digest, what waits on the owner and who holds what; with none, the full board), then claim or drop. A menu-only session is a browsing session — the Stop hook lets it end; it gets a reader identity, never a lock.

## While working

- **Edit only inside your locked folder.** The edit guard denies anything else — write `python <skill>/scripts/handoff.py --to <folder> --task "..."` and carry on. Never argue with the denial; it is §2.
- **Stage explicit paths.** Never `git add -A`. Uncommitted changes you did not make → stop and report.
- **Commit as yourself**: inside Claude Code identity is automatic; plain terminals prefix `ICM_WINDOW=<window>`. On a feature branch in a worktree. A deliberate small solo commit on main is `MAIN_COMMIT_OK=1 git commit ...` with the reason in the message.
- **Never format an identifier by hand.** Window IDs, timestamps, handoff and drop names come from `lib/mint.py` or the writers that call it.
- If a guard blocks you, read its message: it names the lock, the holder, the paths and the remedy. `ICM_LOCK_BYPASS=1` only when the owner said so in this conversation.
- **Commit only complete, tested states to `main`** (PROTOCOL §11). Anyone's finish deploys everyone's committed work. Piecewise work → feature flag or a short-lived branch merged as a unit. Before committing inside a deploy unit: `python <skill>/scripts/test_unit.py <unit>`.
- **Never run a deploy script yourself.** Drop a request; the single deployer ships HEAD. `python <skill>/scripts/deployer.py status` to look.

## Signoff — yours to run, never the owner's to request

Run it when **any** of: the lock's task is complete · the owner signals done ("that's it", "thanks", "next task") · you are about to hand off cross-folder with nothing left here. Task merely shifted → `lock.py reopen --task "<new>"` and keep going.

1. `python <skill>/scripts/lock.py close <folder>` — `status: closing`. From here the Stop hook refuses to end the turn until steps 2–4 are done; you cannot half-sign-off.
2. Write `<folder>/workflow-state/current-pointer.md` with a typed `Next concrete action:` (PROTOCOL §3).
3. Commit your own paths. Any handoff you wrote must exist, be registered, and be committed if tracked. If the guard prints `WARN (PROTOCOL §11)`, run the named `python <skill>/scripts/test_unit.py <unit>` first — HEAD must stay deployable.
3b. **Request the deploy, never run it** (PROTOCOL §11): `python <skill>/scripts/deploy_request.py --for <folder>`. If the folder is in a deploy unit (`.folder-lock/deploy-units.yaml`) this drops a request the single deployer collapses with everyone else's and ships as one deploy of HEAD; the result lands in `<folder>/workflow-state/deploys.jsonl`, a failure shows on the board. Outside every unit it prints "no deploy unit covers" and exits 0 — run it unconditionally.
4. `python <skill>/scripts/lock.py release <folder>` — it verifies pointer mtime, working tree and handoffs, then deletes the lock. A refusal lists what is missing; fix it, don't force it.

## After release — hand the next step to the loop (v4, PROTOCOL §13)

```
python lib/items.py from-pointer <folder>          # pointer -> board item: ready, or waiting_owner + your question
python lib/autorun_log.py append <folder> --item "<title>" --status <ready|waiting_owner|done> --decisions "<defaults you decided>" --commit <sha>
python scripts/autorun.py --runner "<agent command>" --detach
```

Do not render the owner's menu and do not start another item in this session: the loop fires a fresh agent per item. An item waits on the owner ONLY for the reasons in `blockers.md` (§12) — and then with a decision-ready question. Ambiguity with a reasonable default: decide, one dated line in the folder's `memory.md`, continue.

## Installing and proving the guards

```
python <skill>/scripts/install.py [<repo>] --claude-hooks
```

Copies `hooks/*` + `lib/*` to `<repo>/.githooks/`, sets `core.hooksPath`, gitignores `**/.goal/`, merges the PreToolUse + Stop + PostToolUse(Read) hooks into `.claude/settings.json` (omit the flag to just print the JSON), then **runs `check_locks.py --self-test`** (7 cases). Install is not done until it says PASS. Re-run in every clone and worktree. Runtime state (bindings, guard log, the state store) lives in `%LOCALAPPDATA%\folder-lock\<repo-hash>` or `FOLDER_LOCK_STATE_ROOT` — never in the tree (PROTOCOL §14); a repo upgraded from ≤4.1 runs `python <skill>/lib/statestore.py migrate --purge` once. When the owner asks "does the hook actually work": `python <skill>/scripts/selftest.py` and `bash <skill>/tests/conflict_run.sh` (21 scenarios, real refusals, hook stdin/stdout for 1, 6, 9) — paste the result, never answer from the fact that the file exists.

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
lib/statestore.py            coordination documents (items, inboxes, last_seen, rules_hash): ETag writes, merge, cache/outbox; file | blob
lib/conflicts.py             sync conflict copies: classify, fold the harmless, gate the claim / the edit on a divergent one (§15)
lib/rules_hash.py            rules-set bytes per host -> store; RULES DIVERGE names the file (§15, reports only)
lib/peers.py                 lock -> window -> session -> holder's peer name + liveness (§16)
hooks/check_pointer_read.py  PostToolUse on Read: READ IN FULL / PARTIAL READ + the pointer's action line verbatim (§15)
hooks/pre-commit             shim: check_locks + protect_main; no python = no commit
hooks/check_locks.py         commit guard, fail closed; --self-test, --verify-wiring
hooks/protect_main.py        refuse plain commits on main/master (MAIN_COMMIT_OK=1)
hooks/require_lock.py        PreToolUse edit guard (Edit/Write/MultiEdit/NotebookEdit)
hooks/require_signoff.py     Stop hook: no ending a turn with a closing lock
lib/deployunits.py           deploy units (.folder-lock/deploy-units.yaml): path -> unit, dirty check, test markers
scripts/lock.py              claim | adopt | close | reopen | release | check | consume | reader | who | whoami | mine | status
scripts/handoff.py           stage | fire a task into another folder's .goal inbox (minted names)
scripts/board.py             menu (the §13 gate) | json | bare full board | --signpost; renders never write
scripts/deploy_request.py    drop a deploy request (--for <folder> | --unit | --changed) — never deploys
scripts/deployer.py          THE deployer: collapse pending requests -> one deploy of HEAD -> results into workflow-state
scripts/test_unit.py         run a unit's tests, leave the per-window marker the commit guard looks for
scripts/deploy_selftest.py   prove the queue: collapse, already-deployed, failure+back-off, dirty-tree block, guard warning
scripts/install.py           copy hooks+lib, hooksPath, .gitignore, --claude-hooks, self-test
scripts/selftest.py          commit-guard self-test + protected-branch cases
tests/conflict_run.sh        21-scenario proof of which guardrail is active where (own throwaway state root)
tests/autorun_run.py         seeded acceptance run of the loop (own throwaway state root)
templates/                   LOCK.yaml, current-pointer.md, registry.yaml (per-module + core), deploy-units.yaml
```
