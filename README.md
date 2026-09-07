# folder-lock — many agents, one repo, no conflicting saves

A Claude Code skill (works with any agent runner that reads `SKILL.md`, and the hooks work with **no** agent at all) for running several AI agents in parallel on one working tree without them overwriting each other or dirtying `main`.

Distilled from a live human+agent monorepo where 3–6 sessions run at once across ~40 workfolders, after two real incidents: a parallel session's commit sweep picked up another session's lock-protected in-flight edits, and an agent wrote straight to `main` because a small task "didn't feel like it counted".

## The idea in three sentences

The unit of coordination is the **folder** — not the task, not the project, not the agent — because folders are where files collide. Each session takes **one lock per folder** as its first act, edits only inside it, writes a **handoff** into any other folder it needs touched, and leaves a **typed resume pointer** behind as its last act. A **git pre-commit guard** makes two of those rules unskippable: no commit into another session's fresh lock, no plain commit on `main` — and it is only considered installed once a self-test has tried to break it.

## Why a git hook and not an agent rule

A rule that lives only as text in the context window gets skipped the second a task feels low-stakes. Agent-side hooks (Claude hooks, MCP guards) are better, but they are tied to one runner. `git` refusing is model-agnostic, runner-agnostic, and works when a human forgets too.

## Install

```bash
# as a skill (project-level)
git clone https://github.com/LuckyCody/folder-lock .claude/skills/folder-lock

# or user-level (all projects)
git clone https://github.com/LuckyCody/folder-lock ~/.claude/skills/folder-lock

# then install + PROVE the hooks in a repo (re-run in every clone and worktree)
python .claude/skills/folder-lock/scripts/install.py
```

Expected tail of the install output:

```
  PASS  hooksPath points at the hooks under test and pre-commit is executable
  PASS  direct commit to main is BLOCKED
  PASS  commit to main with MAIN_COMMIT_OK=1 PASSES
  PASS  commit on feat/x PASSES
  PASS  commit into another window's FRESH lock is BLOCKED
  PASS  same commit as the lock holder (ICM_WINDOW, case-insensitive) PASSES
  PASS  commit to an unlocked path while a lock exists elsewhere PASSES
  PASS  commit under a STALE (25h) lock PASSES (guard enforces fresh locks only)

ALL GOOD: 8/8 cases behaved.
```

If you only want the hooks, copy `hooks/` anywhere and `git config core.hooksPath <that dir>`. Then run `python scripts/selftest.py <that dir>`.

## Daily shape

```bash
python scripts/board.py                                   # what's in flight, what's waiting
python scripts/lock.py take finance/payroll --window payroll-close --task "August close"
# ... work only inside finance/payroll ...
python scripts/handoff.py --to finance/datev --task "re-export EXTF for 60900 after close"
ICM_WINDOW=payroll-close git commit -m "..."              # guard lets your own lock through
python scripts/lock.py release finance/payroll --window payroll-close
```

The full contract is one page: [PROTOCOL.md](PROTOCOL.md). What an agent does at each moment: [SKILL.md](SKILL.md).

## The pointer grammar (why "small" resumes work)

Every folder's `workflow-state/current-pointer.md` has a typed `Next concrete action:` line — plain (actionable), `WHEN <cond> →` (tripwire, never counts as procrastination), `PARKED (…)` (owner-suspended, runners never touch it), `NONE — …` (closed). The board classifies folders by it, so a fresh window — any model, any runner — reads one screen and resumes cold.

## Escape hatches (deliberate, loud, in the commit message)

| Var | Effect |
|---|---|
| `ICM_WINDOW=<name>` | you are the lock holder — your own locked paths pass |
| `MAIN_COMMIT_OK=1` | deliberate solo-lane commit on `main` |
| `ICM_LOCK_BYPASS=1` | skip the lock guard entirely — owner-approved only |
| `git config folderlock.protected "main,release"` | change the protected-branch list |

Both guards **fail open** on infrastructure errors (missing python, git plumbing failure) with a warning: a guard must never brick commits, it must be loud instead. A malformed lock fails **closed** for the paths it guards.

## Companion skills

- [signoff](https://github.com/LuckyCody/signoff) — the end-of-session ritual that writes the pointer and releases the lock.
- [workflow-builder](https://github.com/LuckyCody/workflow-builder) — the `workflow-state/` tree the pointer lives in.
- [audit-skill](https://github.com/LuckyCody/audit-skill) — read-only evidence → rulings → ledger-verified execution; an audit is a locked session like any other.

MIT — Lucky Office GmbH.
