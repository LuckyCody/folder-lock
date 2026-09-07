# folder-lock — many agents, one repo, no conflicting saves

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
[lock-guard] self-test PASS: 6/6

INSTALLED and PROVEN. Repeat in every clone/worktree.
```

Then prove the whole ladder: `bash .claude/skills/folder-lock/tests/conflict_run.sh` — 12 scenarios, each ending in a visible refusal (or a visible pass where a pass is the point), with hook stdin/stdout printed for the edit, Stop and no-identity cases.

## Daily shape

```bash
python scripts/board.py                                    # what's in flight (locks show status: closing = signing off)
python scripts/lock.py claim finance/payroll --task "August close" --hint payroll
# ... edit only inside finance/payroll — the edit guard denies anything else ...
python scripts/handoff.py --to finance/datev --task "re-export EXTF after close"
git add <your paths> && git commit -m "..."                # identity comes from the session binding
python scripts/lock.py close finance/payroll               # task done -> closing; Stop hook now insists on the rest
#     write finance/payroll/workflow-state/current-pointer.md, commit
python scripts/lock.py release finance/payroll             # verifies pointer, tree, handoffs; deletes the lock
```

Inside Claude Code no env var is needed: `claim` binds the minted window to `CLAUDE_CODE_SESSION_ID`, which the Bash tool exposes and hooks receive as `session_id`. Plain terminals and headless agents set `ICM_WINDOW=<window>`.

## What each guard catches (PROTOCOL §10 has the full table and the environment matrix)

| Guard | Catches |
|---|---|
| `require_lock.py` (PreToolUse) | editing without a lock, under someone else's lock, in a folder with no `.goal/` — before files tangle |
| `check_locks.py` (pre-commit) | staged paths under another window's lock, unclaimed/unguarded folders, `git add -A` sweeps, missing identity, a hook that is not actually wired |
| `protect_main.py` (pre-commit) | plain commits on `main`/`master` |
| `require_signoff.py` (Stop) | ending a turn holding a `closing` lock with the pointer stale or the lock not released; ending with no identity |

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

## Companion skills

- [signoff](https://github.com/LuckyCody/signoff) · [workflow-builder](https://github.com/LuckyCody/workflow-builder) · [audit-skill](https://github.com/LuckyCody/audit-skill)

MIT — Lucky Office GmbH.
