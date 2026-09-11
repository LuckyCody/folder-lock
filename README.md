# folder-lock — many agents, one repo, no conflicting saves

**v4.2.0** — runtime state leaves the working tree: a host-local state root (`%LOCALAPPDATA%\folder-lock\<repo-hash>`) for bindings, guard logs and self-test records, and `lib/statestore.py` for the coordination documents (items, inbox index, digest window) with conditional ETag writes, per-record merge on a lost race, cache + outbox when offline — file backend by default, Azure Blob optional. Plus three proofs before trusting the disk (sync conflict copies gate the claim, rules-set bytes are compared across hosts, a PostToolUse hook proves a pointer was read in full), a lock-holder view (`lock.py who` names the peer to message), a reader identity for menu-only sessions, `board.py menu` as the §13 exit gate, conflict test 21/21. See [What's new in v4.2](#whats-new-in-v42).

**v4.1.0** — the handoff ledger moves to an append-only sidecar (the binding YAML is identity only), `lock.py consume` records a handoff you staged and then did yourself, literal locks survive a registry remap ("the closest existing lock wins"), consumed placeholder notes leave the board, conflict test 14/14. See [What's new in v4.1](#whats-new-in-v41).

**v4.0.0** — the board becomes a work queue: every item has `ready | in_progress | waiting_owner | done`, a decision-ready `question` when it waits on the owner, `created_by`/`owner` routing by path, a dedup rule, a per-folder autorun log, and `scripts/autorun.py` — a loop that fires one fresh agent per ready item until nothing is left that an agent may do. The owner sees the board only then. See [What's new in v4](#whats-new-in-v4). (v3: per-module locks + the deploy queue, [below](#whats-new-in-v3).)

A Claude Code skill (works with any agent runner that reads `SKILL.md`; the git hooks work with **no** agent at all) for running several AI agents in parallel on one working tree without them overwriting each other, dirtying `main`, or "forgetting" the protocol when a task feels small.

Distilled from a live human+agent monorepo where 3–6 sessions run at once across ~40 workfolders, after three real incidents: a parallel session's commit sweep picked up another session's lock-protected in-flight edits; an agent wrote straight to `main` because a small task "didn't feel like it counted"; and a guard that passed silently because there was nothing for it to check.

## The idea in four sentences

The unit of coordination is the **folder** — not the task, not the project, not the agent — because folders are where files collide. Each session **claims one lock per folder** as its first act (a minted window ID bound to the Claude session), edits only inside it, writes a **handoff** into any other folder it needs touched, and **signs itself off** — `closing` → pointer → commit → release — without waiting to be asked. Three **fail-closed guards** make that unskippable: a PreToolUse hook that denies edits outside your lock, a model-agnostic pre-commit that refuses foreign, unclaimed, unguarded or identity-less commits (and plain commits on `main`), and a Stop hook that will not let a turn end while a lock is `closing`. None of it counts as installed until a self-test has tried to break it.

## Why hooks and not rules

A rule that lives only as text in the context window gets skipped the second a task feels low-stakes. A guard that passes when it has nothing to check is not a guard. So: every rule that can be checked by a program is; every guard that cannot evaluate refuses and says what is missing; and the git-level guards work for any agent, any model, and any human who forgets.

## Install

```bash
git clone https://github.com/LuckyCody/folder-lock .claude/skills/folder-lock   # or ~/.claude/skills/folder-lock
python .claude/skills/folder-lock/scripts/install.py --claude-hooks              # re-run in every clone and worktree
```

Expected tail:

```
  PASS  hook wiring: git's hook dir is this directory
  PASS  staged path under a synthetic FOREIGN lock is refused
  PASS  no identity is refused
  PASS  holder (case-insensitive) passes
  PASS  unguarded folder (no .goal/) is refused
  PASS  broken hooksPath is detected loudly by --verify-wiring
  PASS  state store round-trip: stale-etag writer merges, both records present (§14)
[lock-guard] self-test PASS: 7/7

INSTALLED and PROVEN. Repeat in every clone/worktree.
```

Then prove the whole ladder: `bash .claude/skills/folder-lock/tests/conflict_run.sh` — 21 scenarios (plus `python scripts/deploy_selftest.py` for the deploy queue and `python tests/autorun_run.py` for the loop), each ending in a visible refusal (or a visible pass where a pass is the point), with hook stdin/stdout printed for the edit, Stop and no-identity cases. Every fixture uses its own throwaway state root — the live store is never touched.

## Daily shape

```bash
python scripts/board.py menu                               # the owner's gate: ready>0 -> digest + waiting + locks; ready==0 -> full board
python scripts/lock.py who                                 # every lock -> holder's peer name + live/GONE — who to message (§16)
python scripts/lock.py claim finance/payroll --task "August close" --hint payroll   # folds/gates sync conflict copies, prints the pointer proof + rules line (§15)
# ... edit only inside finance/payroll — the edit guard denies anything else ...
python scripts/handoff.py --to finance/datev --task "re-export EXTF after close"
git add <your paths> && git commit -m "..."                # identity comes from the session binding
python scripts/lock.py close finance/payroll               # task done -> closing; Stop hook now insists on the rest
#     write finance/payroll/workflow-state/current-pointer.md, commit
python scripts/deploy_request.py --for finance/payroll     # v3: request the deploy — the single deployer ships HEAD (§11)
python scripts/lock.py release finance/payroll             # verifies pointer, tree, handoffs; deletes the lock
```

Inside Claude Code no env var is needed: `claim` binds the minted window to `CLAUDE_CODE_SESSION_ID`, which the Bash tool exposes and hooks receive as `session_id`. Plain terminals and headless agents set `ICM_WINDOW=<window>`.

## What each guard catches (PROTOCOL §10 has the full table and the environment matrix)

| Guard | Catches |
|---|---|
| `require_lock.py` (PreToolUse) | editing without a lock, under someone else's lock, in a folder with no `.goal/` — before files tangle |
| `check_locks.py` (pre-commit) | staged paths under another window's lock, unclaimed/unguarded folders, `git add -A` sweeps, missing identity, a hook that is not actually wired |
| `protect_main.py` (pre-commit) | plain commits on `main`/`master` |
| `require_signoff.py` (Stop) | ending a turn holding a `closing` lock with the pointer stale or the lock not released (a session with no identity is browsing and passes) |
| `check_pointer_read.py` (PostToolUse on Read) | a sliced read of a pointer / inbox note / rules file presented as complete — injects `READ IN FULL ✓` or `PARTIAL READ ⚠` + the action line verbatim |
| `lock.py claim` gate (§15) | divergent sync conflict copies of load-bearing files (refuses, exit 5), rules-byte drift across hosts (reported) |

Escape hatches, all deliberate and loud: `ICM_WINDOW=<w>` (you are the holder), `MAIN_COMMIT_OK=1` (solo commit on main), `ICM_LOCK_BYPASS=1` (owner-approved only), `git config folderlock.protected "main,release"`.

## Optional registry

Drop `.folder-lock/registry.yaml` in the repo to resolve paths to workflow homes instead of nearest-`.goal/`:

```yaml
workflows:
- id: payroll
  owns:
  - finance/payroll/**
  - .claude/skills/payroll/**        # locks at finance/payroll/.goal — one lock per workflow home
```

## What's new in v4.2

One tree shared by two hosts through a sync tool, several sessions each, plus headless agents — and the runtime state was riding the same sync. This release moves it out and adds the proofs that a synced disk needs.

- **State root** (`lib/lockpath.py`): `STATE_ROOT` = `FOLDER_LOCK_STATE_ROOT` or `%LOCALAPPDATA%\folder-lock\<repo-hash>` (`~/.local/state/…` elsewhere). Session bindings + handoff sidecars, `guard_log.jsonl`, `selftest_last.json`, the conflict-suite record and the signpost copy live there. Bindings still at `.goal/sessions/` are copied over lazily. Locks, inbox notes, `workflow-state/` and the deploy queue stay in the tree on purpose (PROTOCOL §14.4).
- **`lib/statestore.py`**: documents `items`, `inboxes`, `last_seen`, `rules_hash` with ETag-conditional writes, three-way per-record merge on a lost race, cache + outbox when offline, `READONLY` for renders. File backend by default; `FOLDER_LOCK_STATE_BACKEND=blob` + `FOLDER_LOCK_STATE_ACCOUNT` for Azure Blob. `lib/items.py`, `lib/autorun_log.py`, `scripts/handoff.py`, `lock.py release` go through it; `statestore.py status|get|replay|migrate [--purge]`.
- **Three proofs (§15)**: `lib/conflicts.py` — sync conflict copies (`-HOSTNAME`, ` - Copy`, ` (N)`, `.sync-conflict-…`) classified and folded by `lock.py claim`; a divergent copy of a load-bearing file refuses the claim (exit 5) or, inside your folder, becomes the claim's first act; `require_lock` denies an edit beside one. `lib/rules_hash.py` — the rules set hashed per host, published to the store, `RULES DIVERGE ⚠` names the file (reports, never blocks). `hooks/check_pointer_read.py` — PostToolUse on Read: `READ IN FULL ✓` / `PARTIAL READ ⚠ … Next concrete action (verbatim, L…)`; `install.py --claude-hooks` wires it.
- **Lock-holder view (§16)**: `lib/peers.py` + `lock.py who` join `LOCK.yaml` → binding → Claude Code's `~/.claude/sessions/<pid>.json` into the holder's `ListAgents` name with live / GONE / exited / headless. `lock.py check` and the edit guard print `Holder: … — message them first`; GONE = orphaned lock → ask the owner.
- **Reader identity**: `lock.py reader` binds a window with no lock and no folder so a menu-only session's state writes have an author; edits stay denied, the Stop hook passes, `claim` upgrades it. The Stop hook no longer nags a session that never claimed anything (browsing is not an error state).
- **`board.py menu`**: the §13 exit gate — `ready > 0` → digest · waiting-on-owner · locks (with holders) · deploys · one ▸ line (kicks `scripts/autorun.py --detach` when `FOLDER_LOCK_RUNNER` is set); `ready == 0` → the full board. `json` gains `locks`, `items`, `digest`. Renders never write; `--signpost` alone rewrites the tracked `.folder-lock/next-session.md`.
- **Tests**: `check_locks.py --self-test` 7/7 (+ store round-trip); `tests/conflict_run.sh` scenarios 15 (write race merges), 16 (offline → outbox → replay), 17 (conflict copies), 18 (rules hash), 19 (read coverage), 20 (holder view), 21 (reader) — 21/21; `tests/autorun_run.py` on a throwaway state root.

## What's new in v4.1

Two gaps that only show up once several sessions and a live registry share one repo.

- **Handoff sidecar** — `.goal/sessions/<sid>.handoffs.txt`, append-only: `staged <path>` / `consumed <path>`. The binding YAML is regenerated on every claim/release and used to carry the `handoff:` lines too, so a filter bug could drop the ledger. `lib/lockpath.py` gains `record_handoff` / `read_handoffs` (sidecar first, legacy YAML lines after; `write_session` migrates them once). `scripts/handoff.py` writes to the sidecar; `lock.py release` reads it.
- **`lock.py consume <note>`** — a session that staged a note, later claimed the target and did the work itself records the consumption (and deletes the note). Without the record `release` refuses with `orphaned handoff` (exit 6), as before. A note whose target folder is gone is nobody's orphan; a note deleted by the target's current holder is noted, not refused.
- **Literal locks** — a folder claimed as its own lock domain and later folded into another home by a registry edit keeps its `LOCK.yaml`; `lockpath.literal_lock()` finds it and every guard honours it: the holder edits/commits/releases, everyone else (including the home's holder) gets `FOREIGN <folder> (literal lock)`. `lock.py check` shows both locks; `close/reopen/release/adopt` resolve to the literal lock when it is yours.
- **Board hygiene** — `scripts/board.py` ignores inbox notes whose first line is `# CONSUMED …` (placeholders left for the folder's next visitor).
- **Tests** — `tests/conflict_run.sh` scenarios 13 (sidecar survives a binding rewrite → orphan refused → consume → released) and 14 (registry remap → literal lock: check sees both, holder edits, home-holder refused, release removes it). 14/14.

## What's new in v4

**The owner is the last resort, not the scheduler.** After a signoff the system keeps working the board — a fresh agent process per item, one item per agent — until every remaining item genuinely waits on a human.

- `lib/items.py` — status overlay over the file-derived board: `ready | in_progress | waiting_owner | done` (+ `waiting_world`, `parked`), `question` (required for `waiting_owner`: concrete question, 2–3 options, recommendation), `blocked_since`, `created_by`, `owner` (lock home by path). Dedup: no second open item with the same owner + normalized title (`Duplicate`; `scripts/handoff.py` exits 4).
- `templates/blockers.md` — the configurable list of the ONLY reasons an item may wait on the owner (§12). Copy to `<repo>/.folder-lock/blockers.md`; `autorun.py` pastes it into every fired agent's prompt.
- `scripts/autorun.py --runner "<agent command>"` — the loop (§13): per folder, skip if locked, own pointer first, `.firing.lock` with a minted id, fire, read back, three no-progress fires → `waiting_owner`; repeat until the ready queue is empty. `--once`, `--owner-prefix`, `--detach`. A dead runner never counts against an item.
- `lib/autorun_log.py` — one line per autonomously worked item in `<folder>/workflow-state/autorun-log.md`; `digest` since the owner last looked.
- Signoff (SKILL.md) gains three commands after release: `items.py from-pointer`, `autorun_log.py append`, `autorun.py --detach` — and stops inviting the owner's menu.
- `tests/autorun_run.py` — seeded acceptance: plain ready, cross-folder creator, must-end-waiting_owner; asserts statuses, question, `created_by`, one log line per item, dedup refusal.

## What's new in v3

### Locks are per module, deploys are per unit

The v2 failure mode: one deployable app hosting unrelated modules, one lock, three sessions queuing on it while editing files that never touched each other. The lock was protecting "the deploy unit" instead of "the files being edited" — a structural collision, not a real one.

v3 separates the two concerns (PROTOCOL §1 + §11):

- **Edit conflicts** get a lock at the granularity of the files touched. Each module folder is its own registry workflow with its own `.goal/`; the app's wiring (router registration, job-type registry, base templates, deploy script, shared auth) lives in a `core/` workflow that is held rarely and briefly. Resolution is per file, longest glob wins — a session in `apps/web/billing/` takes only the billing lock; a commit spanning two modules needs both. Modules register into core; core never imports module internals. Example: [`templates/registry.yaml`](templates/registry.yaml).
- **Deploy races** are a serialization problem, and deploy is idempotent. So: a deploy queue.

### Deploy queue

```bash
# .folder-lock/deploy-units.yaml — which paths form one deployable artefact, how to deploy + test it
python scripts/deploy_request.py --for apps/web/billing      # a finished session REQUESTS (signoff does this)
python scripts/deployer.py                                    # ONE deployer, from any scheduler every few minutes:
                                                              #   collapses all pending requests of a unit into one deploy of HEAD,
                                                              #   writes the result into each requester's workflow-state/deploys.jsonl
python scripts/deployer.py status                             # pending / FAILED / BLOCKED / last deploy per unit
python scripts/test_unit.py web-app                           # run the unit's tests, leave the marker the commit guard looks for
python scripts/deploy_selftest.py                             # prove it in a throwaway repo (collapse, failure, dirty-tree block, guard warning)
```

**The invariant this introduces: HEAD must always be deployable** — anyone's finish deploys everyone's committed work. Commit only complete, tested states to `main`; piecewise work goes behind a feature flag or on a short-lived branch merged as a unit. Three executable consequences: the commit guard *warns* when a commit touches a unit without a fresh test marker from this session; the deployer *blocks* (flag on the board, requests wait) while a tracked source file under the unit is dirty; a failed deploy stays visible on the board until the next success, with 30-min × attempts back-off that a newer request overrides. Rollback is a revert commit plus a new request — never a hand-run deploy. Example: [`templates/deploy-units.yaml`](templates/deploy-units.yaml).

`scripts/board.py json` now returns `{"folders": [...], "deploys": [...]}` (v2 returned the folder list bare).

## Companion skills

- [signoff](https://github.com/LuckyCody/signoff) · [workflow-builder](https://github.com/LuckyCody/workflow-builder) · [audit-skill](https://github.com/LuckyCody/audit-skill)

MIT — Lucky Office GmbH.
