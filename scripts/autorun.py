"""Autorun — the post-signoff loop: work the board until nothing is left that an agent may do (PROTOCOL §13).

    python scripts/autorun.py --runner "claude --print --permission-mode acceptEdits" [--once] [--owner-prefix P] [--detach]

Per workfolder that owns a `ready` item: skip while a fresh LOCK.yaml / .firing.lock is present (§1 unchanged) ->
pick the folder's own pointer item first, else the oldest ready item -> set in_progress, write .firing.lock with a
minted agent id -> fire ONE fresh agent process with the prompt on stdin (ICM_WINDOW / ICM_FOLDER / AUTORUN_ITEM*
in its env) -> read the item back (the agent's own signoff moves it) -> three no-progress fires make it
waiting_owner with an auto question. Repeat passes until no ready item exists anywhere. No iteration cap.

The runner is any command that reads a prompt on stdin and works the item as an agent would (see
tests/autorun_stub_agent.py for the smallest possible one). Blocker rules come from templates/blockers.md —
copy it next to your registry and edit; the loop pastes it into every prompt.
"""
from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lib"))
import lockpath as lp  # noqa: E402
import items  # noqa: E402
import autorun_log as alog  # noqa: E402
import mint  # noqa: E402

ROOT = lp.ROOT
SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__", ".next", "dist", "build", ".githooks"}
FAIL_THRESHOLD = 3
BLOCKERS = [p for p in (ROOT / ".folder-lock" / "blockers.md", ROOT / "blockers.md", HERE.parent / "templates" / "blockers.md") if p.exists()]


def log(msg: str) -> None:
    print(f"{datetime.now().isoformat(timespec='seconds')}  {msg}", flush=True)


def scan() -> list:
    """Derived rows: every folder pointer + every staged/fired handoff note (the board's raw material)."""
    rows = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP]
        d = Path(dirpath)
        rel = d.relative_to(ROOT).as_posix()
        if d.name == "workflow-state" and "current-pointer.md" in filenames:
            folder = d.parent.relative_to(ROOT).as_posix()
            rows.append({"folder": folder, "kind": "pointer", "ref": "workflow-state/current-pointer.md",
                         "title": items.pointer_line(folder) or "(pointer has no action line)", "line": items.pointer_line(folder)})
        if d.name == "inbox" and d.parent.name == ".goal":
            folder = d.parent.parent.relative_to(ROOT).as_posix()
            for f in sorted(x for x in filenames if x.endswith(".md")):
                title, src = f, ""
                for raw in (d / f).read_text(encoding="utf-8", errors="replace").splitlines()[:10]:
                    if raw.startswith("task:"):
                        title = raw[5:].strip().strip('"')
                    if raw.startswith("from:"):
                        src = raw[5:].strip().strip('"')
                rows.append({"folder": folder, "kind": "handoff", "ref": f, "title": title, "from": src})
    return rows


def folder_locked(folder: str) -> str:
    gdir = ROOT / folder / ".goal"
    for name, fresh in (("LOCK.yaml", lp.LOCK_FRESH), (".firing.lock", lp.FIRING_FRESH)):
        p = gdir / name
        if p.exists():
            age = time.time() - p.stat().st_mtime
            if age <= fresh.total_seconds():
                return f"{name} fresh ({int(age)}s)"
            if name == ".firing.lock":
                p.unlink(missing_ok=True)
    return ""


def set_firing_lock(folder: str, key: str) -> str:
    gdir = ROOT / folder / ".goal"
    gdir.mkdir(parents=True, exist_ok=True)
    agent_id = mint.agent("autorun")
    (gdir / ".firing.lock").write_text(f'holder: fired\nwindow: "{agent_id}"\nstatus: open\ntask: "autorun {key}"\n'
                                       f'stream: "{folder}"\nbranch: "main"\nstarted: "{mint.timestamp()}"\n', encoding="utf-8")
    return agent_id


def prompt_for(folder: str, item: dict, agent_id: str) -> str:
    blockers = BLOCKERS[0].read_text(encoding="utf-8") if BLOCKERS else "(no blockers.md found — treat every item as executable)"
    where = (f"the staged handoff `{folder}/.goal/inbox/{item['ref']}` (consume = do it, record the outcome in the folder's progress log, delete the file)"
             if item["kind"] == "handoff" else f"the `Next concrete action:` line of `{folder}/workflow-state/current-pointer.md`")
    return (f"[AUTORUN · fresh agent · one item]\n\nYou are a fresh agent (empty context) fired to work exactly ONE item in the workfolder `{folder}`. "
            f"Identity: ICM_WINDOW={agent_id} (in your env); the folder's .goal/.firing.lock is yours.\n\n"
            f"ITEM key: {item['key']}\n     title: {item.get('title', '')}\n     created_by: {item.get('created_by', '?')}  owner: {folder}\n     where: {where}\n\n"
            f"Read cold: `{folder}/CONTEXT.md`, the pointer, `{folder}/memory.md` (if present), then the item. Edit ONLY inside `{folder}`; "
            f"other folders get a handoff (`python scripts/handoff.py --to <folder> --task \"...\" --from {folder}`; DUPLICATE = extend the existing item).\n\n"
            f"HUMAN BLOCKER RULES (PROTOCOL §12 — this installation's list):\n{blockers.strip()}\n\n"
            f"If blocked: `python lib/items.py wait \"{item['key']}\" --question \"<question>. Options: (a) …; (b) …; (c) …. Recommendation: <x>.\"` "
            f"and write the pointer line as `Next concrete action: WHEN owner <answers> → <what you will do>`.\n"
            f"When done: `python lib/items.py done \"{item['key']}\"`, then sign off (§9): lock.py close → pointer (typed line) + progress section → "
            f"commit your own paths → lock.py release → `python lib/items.py from-pointer {folder}` → "
            f"`python lib/autorun_log.py append {folder} --item \"<title>\" --status <ready|waiting_owner|done> --decisions \"<defaults decided>\" --commit <sha>`.\n"
            f"One item only. Do not start another. End with one line: DONE | WAITING_OWNER | FAILED: <reason>.\n")


def run_runner(runner: str, prompt: str, env_extra: dict, timeout: int) -> dict:
    cmd = [t.strip('"') for t in shlex.split(runner, posix=(os.name != "nt"))]
    env = dict(os.environ); env.update(env_extra)
    try:
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           env=env, cwd=str(ROOT), timeout=timeout)
        return {"exit_code": r.returncode, "stdout": r.stdout, "stderr": r.stderr}
    except subprocess.TimeoutExpired:
        return {"_error": f"runner timed out after {timeout}s"}
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {e}"}


def autorun(runner: str, owner_prefix: str = "", once: bool = False, timeout: int = 3600) -> dict:
    totals = {"passes": 0, "fired": 0, "done": 0, "waiting_owner": 0, "failed": 0, "locked": 0}
    while True:
        totals["passes"] += 1
        items.sync(scan())
        ready = [it for it in items.ready_items() if not owner_prefix or it["owner"].startswith(owner_prefix)]
        if not ready:
            log("autorun: queue empty — nothing left that an agent may do"); break
        by_owner: dict = {}
        for it in ready:
            by_owner.setdefault(it["owner"], []).append(it)
        fired = 0
        for owner in sorted(by_owner, key=lambda o: min(x.get("created", "") for x in by_owner[o])):
            reason = folder_locked(owner)
            if reason:
                totals["locked"] += 1; log(f"SKIP {owner}: {reason}"); continue
            cands = by_owner[owner]
            own = [c for c in cands if c["kind"] == "pointer" and c["key"].split("|", 1)[0] == owner]
            item = own[0] if own else sorted(cands, key=lambda c: c.get("created", ""))[0]
            key, before = item["key"], items.norm_title(item.get("title", ""))
            items.set_status(key, "in_progress")
            agent_id = set_firing_lock(owner, key)
            log(f"FIRE {owner} :: {item.get('title', '')[:80]} as {agent_id}")
            try:
                res = run_runner(runner, prompt_for(owner, item, agent_id),
                                 {"ICM_WINDOW": agent_id, "ICM_FOLDER": owner, "AUTORUN_ITEM": key, "AUTORUN_ITEM_KIND": item["kind"],
                                  "AUTORUN_ITEM_REF": item["ref"], "AUTORUN_ITEM_TITLE": item.get("title", "")}, timeout)
            finally:
                (ROOT / owner / ".goal" / ".firing.lock").unlink(missing_ok=True)
            fired += 1; totals["fired"] += 1
            err = res.get("_error") or (f"exit {res.get('exit_code')}" if res.get("exit_code") not in (0, None) else "")
            if res.get("_error"):
                # transport-level failure (runner missing / crashed before any agent ran): infrastructure, never the item's fault
                items.set_status(key, "ready"); log(f"  INFRA ERROR — {key} back to ready, pass aborted: {res['_error'][:160]}")
                return totals
            e = items.load()["items"].get(key, {})
            if e.get("status") == "in_progress":
                items.set_status(key, "ready"); items.sync(scan())
                e = items.load()["items"].get(key, {})
                if e.get("status") == "ready" and items.norm_title(e.get("title", "")) == before:
                    e2 = items.record_fail(key, err or "agent returned without moving the item", FAIL_THRESHOLD)
                    totals["failed"] += 1
                    alog.append(owner, item.get("title", "")[:80], e2.get("status", "ready"), f"resumer: no progress ({err[:100]})", "-", agent_id)
                    log(f"  NO PROGRESS on {key} (fails={e2.get('fails')}) -> {e2.get('status')}")
                    continue
            st = e.get("status", "?")
            totals["done" if st == "done" else "waiting_owner" if st == "waiting_owner" else "fired"] += (1 if st in ("done", "waiting_owner") else 0)
            log(f"  item -> {st}")
        if once or fired == 0:
            break
    log(f"autorun summary: {totals}")
    return totals


def main() -> int:
    ap = argparse.ArgumentParser(description="folder-lock autorun loop (PROTOCOL §13)")
    ap.add_argument("--runner", default=os.environ.get("AUTORUN_RUNNER", ""), help="command that plays the fired agent (reads the prompt on stdin)")
    ap.add_argument("--once", action="store_true"); ap.add_argument("--owner-prefix", default="")
    ap.add_argument("--timeout", type=int, default=3600); ap.add_argument("--detach", action="store_true")
    a = ap.parse_args()
    if not a.runner:
        print("ERROR: --runner (or AUTORUN_RUNNER) is required, e.g. --runner \"claude --print --permission-mode acceptEdits\"", file=sys.stderr)
        return 2
    if a.detach:
        argv = [x for x in sys.argv[1:] if x != "--detach"]
        kw = {"cwd": str(ROOT), "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if os.name == "nt":
            kw["creationflags"] = 0x00000008 | 0x00000200
        else:
            kw["start_new_session"] = True
        subprocess.Popen([sys.executable, str(Path(__file__).resolve())] + argv, **kw)
        print("autorun: detached pass started"); return 0
    autorun(a.runner, a.owner_prefix, a.once, a.timeout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
