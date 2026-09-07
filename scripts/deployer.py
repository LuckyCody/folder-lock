"""The single deployer — one process deploys, everyone else requests (PROTOCOL.md §11).

    python scripts/deployer.py                 # one pass over every unit — run it from any scheduler every few minutes
    python scripts/deployer.py status          # pending / failed / blocked / last deploy per unit
    python scripts/deployer.py --dry-run       # plan only, nothing runs
    python scripts/deployer.py --unit <u>      # one unit only
    python scripts/deployer.py --retry-now     # ignore the failure back-off this pass

Per deploy unit (.folder-lock/deploy-units.yaml), one pass:
  1. reads every pending request in .goal/deploy/requests/<unit>/*.yaml
  2. gates: HEAD on the unit's branch · no tracked source file under the unit dirty (dirty_ignore excepted) ·
     at least one request's commit contained in HEAD. Gate fails -> BLOCKED: state/blocked/<unit>.yaml written
     (board renders it), requests stay pending, each requesting workflow gets ONE `blocked` line (again only
     when the reason changes).
  3. HEAD equals the last deployed commit -> requests complete as `already-deployed`, no build.
  4. runs the unit's `deploy:` command ONCE for all pending requests (they collapse); output -> logs/<unit>/<run>.log;
     the deployer lock is heart-beaten while it runs.
  5. success -> {event: deployed, deployed_commit, deployed_at, build{run, log, duration_s, collapsed, requests}}
     appended to <workflow>/workflow-state/deploys.jsonl and written to <workflow>/workflow-state/last-deploy.yaml
     for EVERY requesting workflow; requests move to done/<unit>/; flags cleared; state/last/<unit>.yaml updated.
     busy exit code -> nothing changes, retry next pass.
     failure -> requests stay pending (attempts+1), `failed` line per workflow, state/failed/<unit>.yaml
     {error_tail, log, attempts, retry_after} — the board renders it. Back-off 30 min × attempts (cap 6 h);
     a NEWER request (dropped after the failure) retries at once.

Concurrency: one YAML lock (.goal/deploy/deployer.lock) with a minted agent id, PID and a 60-s heartbeat;
stale after 15 min without heartbeat or when the PID is gone. Use your scheduler's "ignore new instance"
setting as a belt on top.

Deploying the WORKING TREE at HEAD is the contract: a dirty tree under the unit blocks rather than ships
someone's half-finished edit — that is the §11 invariant made executable.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lib"))
import deployunits as U  # noqa: E402
import mint  # noqa: E402

LOCK = U.BASE / "deployer.lock"
LOCK_STALE = timedelta(minutes=15)
LOG = U.BASE / "deployer.log"
LOG_MAX_LINES = 2000
BACKOFF_STEP = timedelta(minutes=30)
BACKOFF_CAP = timedelta(hours=6)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass


def log(msg: str) -> None:
    line = f"{datetime.now().strftime('%Y-%m-%dT%H:%M:%S')}  {msg}"
    print(line, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        lines = LOG.read_text(encoding="utf-8", errors="replace").splitlines() if LOG.exists() else []
        lines.append(line)
        LOG.write_text("\n".join(lines[-LOG_MAX_LINES:]) + "\n", encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------- tiny yaml (flat request/flag files; no PyYAML dependency)

def read_yaml(p: Path) -> dict:
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return {}
    out: dict = {}
    cur_list = None
    for raw in text.splitlines():
        if raw.startswith("  - ") and cur_list is not None:
            cur_list.append(_unq(raw[4:].strip()))
            continue
        m = raw.split(":", 1)
        if len(m) == 2 and m[0] and not m[0].startswith(" "):
            key, val = m[0].strip(), m[1].strip()
            if val == "":
                out[key] = []
                cur_list = out[key]
            elif val.startswith("{"):
                try:
                    out[key] = json.loads(val)
                except ValueError:
                    out[key] = val
                cur_list = None
            else:
                out[key] = _unq(val)
                cur_list = None
    return out


def _unq(v: str):
    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
        return json.loads(v) if v.startswith('"') else v[1:-1]
    if v.isdigit():
        return int(v)
    return v


def _q(v) -> str:
    if isinstance(v, (int, float)):
        return str(v)
    return json.dumps(str(v), ensure_ascii=False)


def write_yaml(p: Path, data: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for k, v in data.items():
        if isinstance(v, list):
            lines.append(f"{k}:")
            lines += [f"  - {_q(x)}" for x in v]
        elif isinstance(v, dict):
            lines.append(f"{k}: {json.dumps(v, ensure_ascii=False)}")
        else:
            lines.append(f"{k}: {_q(v)}")
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


def now_ts() -> str:
    return mint.timestamp()


def parse_ts(s) -> datetime | None:
    try:
        return datetime.strptime(str(s or "")[:16], U.TS_FMT)
    except ValueError:
        return None


# ---------------------------------------------------------------- deployer lock (runner .firing.lock pattern)

def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True)
        return str(pid) in (r.stdout or "")
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_lock() -> str | None:
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    if LOCK.exists():
        info = read_yaml(LOCK)
        age = datetime.now() - datetime.fromtimestamp(LOCK.stat().st_mtime)
        pid = int(info.get("pid") or 0)
        if age < LOCK_STALE and _pid_alive(pid):
            log(f"deployer already running (pid {pid}, window {info.get('window')}, heartbeat {int(age.total_seconds())}s ago) — skipping")
            return None
        log(f"stale deployer lock cleared (age {int(age.total_seconds())}s, pid {pid} alive={_pid_alive(pid)})")
    agent_id = mint.agent("deployer")
    write_yaml(LOCK, {"holder": "fired", "window": agent_id, "status": "open", "task": "deploy pass",
                      "stream": "deploy", "branch": U.head_branch(), "started": now_ts(), "pid": os.getpid()})
    return agent_id


def release_lock() -> None:
    try:
        LOCK.unlink()
    except OSError:
        pass


class Heartbeat:
    def __init__(self, path: Path, every: float = 60.0):
        self.path, self.every = path, every
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.wait(self.every):
            try:
                os.utime(self.path, None)
            except OSError:
                pass

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *a):
        self._stop.set()


# ---------------------------------------------------------------- workflow-state writers

def ws_append(workflow: str, record: dict) -> None:
    if not workflow:
        return
    d = U.ROOT / workflow / "workflow-state"
    try:
        d.mkdir(parents=True, exist_ok=True)
        with (d / "deploys.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as e:
        log(f"  could not write workflow-state for {workflow}: {e}")


def ws_last(workflow: str, record: dict) -> None:
    if workflow:
        try:
            write_yaml(U.ROOT / workflow / "workflow-state" / "last-deploy.yaml", record)
        except OSError as e:
            log(f"  could not write last-deploy for {workflow}: {e}")


# ---------------------------------------------------------------- requests + flags

def pending_requests(unit: str) -> list:
    d = U.REQUESTS / unit
    out = []
    for p in sorted(d.glob("*.yaml")) if d.is_dir() else []:
        data = read_yaml(p)
        if data:
            data["_path"] = p
            out.append(data)
    return out


def save_request(req: dict) -> None:
    write_yaml(req["_path"], {k: v for k, v in req.items() if not k.startswith("_")})


def complete_request(req: dict, result: dict) -> None:
    p: Path = req["_path"]
    data = {k: v for k, v in req.items() if not k.startswith("_")}
    data["status"] = "done"
    data["result"] = result
    write_yaml(U.DONE / req["unit"] / p.name, data)
    try:
        p.unlink()
    except OSError:
        pass


def is_ancestor(commit: str, head: str) -> bool:
    return bool(commit) and U.git("merge-base", "--is-ancestor", commit, head).returncode == 0


def flag(kind: str, unit: str) -> Path:
    return U.STATE / kind / f"{unit}.yaml"


def clear_flag(kind: str, unit: str) -> None:
    try:
        flag(kind, unit).unlink()
    except OSError:
        pass


def set_blocked(unit: U.Unit, reqs: list, reason: str, detail: list) -> None:
    prev = read_yaml(flag("blocked", unit.name))
    changed = prev.get("reason") != reason or list(prev.get("detail") or []) != list(detail)
    write_yaml(flag("blocked", unit.name), {"unit": unit.name, "reason": reason, "detail": detail,
                                           "since": prev.get("since") or now_ts(), "checked_at": now_ts(),
                                           "requests": [r["_path"].name for r in reqs]})
    if changed:
        log(f"  BLOCKED {unit.name}: {reason} {detail[:5]}")
        for r in reqs:
            ws_append(str(r.get("workflow", "")), {"event": "blocked", "unit": unit.name, "request": r["_path"].name,
                                                  "reason": reason, "detail": detail[:20], "at": now_ts()})


# ---------------------------------------------------------------- one unit

def process_unit(unit: U.Unit, dry_run: bool, retry_now: bool, run_window: str) -> None:
    reqs = pending_requests(unit.name)
    if not reqs:
        return
    head = U.head_commit()
    branch = U.head_branch()
    log(f"{unit.name}: {len(reqs)} pending request(s); HEAD {head[:7]} on {branch}")
    if branch != unit.branch:
        set_blocked(unit, reqs, f"HEAD is on '{branch}', deployer deploys '{unit.branch}' only", [])
        return
    dirty = U.dirty_source_paths(unit)
    if dirty:
        set_blocked(unit, reqs, "tracked source files under the unit are uncommitted (HEAD is not the tree)", dirty)
        return
    ready = [r for r in reqs if is_ancestor(str(r.get("commit") or ""), head)]
    for r in reqs:
        if r not in ready and r.get("status") != "waiting-for-commit":
            r["status"] = "waiting-for-commit"
            save_request(r)
            log(f"  {r['_path'].name}: commit {str(r.get('commit'))[:7]} not in HEAD yet — waits")
    if not ready:
        set_blocked(unit, reqs, "no pending request's commit is contained in HEAD (branch not merged?)",
                    [str(r.get("commit"))[:7] for r in reqs])
        return
    clear_flag("blocked", unit.name)

    f = read_yaml(flag("failed", unit.name))
    if f and not retry_now:
        ra, failed_at = parse_ts(f.get("retry_after")), parse_ts(f.get("failed_at"))
        newer = any((parse_ts(r.get("requested_at")) or datetime.min) > (failed_at or datetime.min) for r in ready)
        if ra and datetime.now() < ra and not newer:
            log(f"  {unit.name}: last deploy failed at {f.get('failed_at')} — backing off until {f.get('retry_after')} "
                f"(attempt {f.get('attempts')}); --retry-now overrides")
            return

    last = read_yaml(flag("last", unit.name))
    if last.get("deployed_commit") == head:
        log(f"  {unit.name}: HEAD {head[:7]} already deployed at {last.get('deployed_at')} — completing "
            f"{len(ready)} request(s) without a new deploy")
        if dry_run:
            return
        for r in ready:
            res = {"event": "already-deployed", "unit": unit.name, "request": r["_path"].name,
                   "requested_commit": r.get("commit"), "deployed_commit": head,
                   "deployed_at": last.get("deployed_at"), "build": last.get("build"), "at": now_ts()}
            ws_append(str(r.get("workflow", "")), res)
            ws_last(str(r.get("workflow", "")), res)
            complete_request(r, res)
        return

    run_id = f"{mint.compact()}-{head[:7]}"
    log_path = U.LOGS / unit.name / f"{run_id}.log"
    log(f"  DEPLOY {unit.name} @ {head[:7]} for {len(ready)} request(s) [{', '.join(r['_path'].name for r in ready)}]"
        f"{' (dry-run)' if dry_run else ''}")
    if dry_run:
        return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    with log_path.open("w", encoding="utf-8", errors="replace") as fh:
        fh.write(f"# deployer run {run_id} · unit {unit.name} · HEAD {head} · {now_ts()} · window {run_window}\n"
                 f"# requests: {', '.join(r['_path'].name for r in ready)}\n# cmd: {unit.deploy}\n\n")
        fh.flush()
        try:
            rc = subprocess.run(unit.deploy, shell=True, cwd=U.ROOT, stdout=fh, stderr=subprocess.STDOUT,
                                timeout=unit.timeout_s,
                                env={**os.environ, "ICM_WINDOW": run_window, "DEPLOYER_RUN": run_id,
                                     "PYTHONIOENCODING": "utf-8"}).returncode
        except subprocess.TimeoutExpired:
            fh.write(f"\n# TIMEOUT after {unit.timeout_s}s\n")
            rc = 124
    duration = round(time.time() - t0, 1)
    try:
        tail = "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-12:])
    except OSError:
        tail = ""
    build = {"run": run_id, "log": log_path.relative_to(U.ROOT).as_posix(), "duration_s": duration, "rc": rc,
             "collapsed": len(ready), "requests": [r["_path"].name for r in ready], "cmd": unit.deploy}

    if rc == 0:
        deployed_at = now_ts()
        log(f"  OK {unit.name} deployed {head[:7]} in {duration}s (collapsed {len(ready)} request(s))")
        write_yaml(flag("last", unit.name), {"unit": unit.name, "deployed_commit": head, "deployed_at": deployed_at, "build": build})
        clear_flag("failed", unit.name)
        for r in ready:
            res = {"event": "deployed", "unit": unit.name, "request": r["_path"].name, "workflow": r.get("workflow"),
                   "requested_commit": r.get("commit"), "requested_at": r.get("requested_at"),
                   "deployed_commit": head, "deployed_at": deployed_at, "build": build}
            ws_append(str(r.get("workflow", "")), res)
            ws_last(str(r.get("workflow", "")), res)
            complete_request(r, res)
        return
    if rc in unit.busy_exit_codes:
        log(f"  BUSY {unit.name}: exit {rc} (another deploy in flight) — requests stay pending, retry next pass")
        return
    attempts = 1 + max([int(r.get("attempts") or 0) for r in ready] + [0])
    failed_at = now_ts()
    retry_after = (datetime.now() + min(BACKOFF_STEP * attempts, BACKOFF_CAP)).strftime(U.TS_FMT)
    log(f"  FAILED {unit.name}: exit {rc} after {duration}s — requests stay pending (attempt {attempts}, retry after {retry_after}); log {build['log']}")
    write_yaml(flag("failed", unit.name), {"unit": unit.name, "failed_at": failed_at, "attempts": attempts,
                                          "retry_after": retry_after, "rc": rc, "commit": head, "log": build["log"],
                                          "error_tail": tail[-600:], "requests": build["requests"]})
    for r in ready:
        r["attempts"] = int(r.get("attempts") or 0) + 1
        r["status"] = "failed"
        r["last_failed_at"] = failed_at
        r["last_error"] = tail[-600:]
        save_request(r)
        ws_append(str(r.get("workflow", "")), {"event": "failed", "unit": unit.name, "request": r["_path"].name,
                                              "requested_commit": r.get("commit"), "commit": head, "rc": rc,
                                              "attempt": r["attempts"], "log": build["log"], "error_tail": tail[-600:],
                                              "at": failed_at, "retry_after": retry_after})


# ---------------------------------------------------------------- status / main

def status() -> int:
    units = U.load_units()
    if not units:
        print(f"no deploy units ({U.UNITS_FILE} missing) — see templates/deploy-units.yaml")
        return 0
    for u in units.values():
        pend = pending_requests(u.name)
        last, failed, blocked = read_yaml(flag("last", u.name)), read_yaml(flag("failed", u.name)), read_yaml(flag("blocked", u.name))
        print(f"{u.name}: {len(pend)} pending" + (f" · last deploy {last.get('deployed_at')} @ {str(last.get('deployed_commit'))[:7]}" if last else " · never deployed by the queue"))
        for r in pend:
            print(f"    {r['_path'].name}  {r.get('status')}  commit {str(r.get('commit'))[:7]}  for {r.get('workflow')}"
                  + (f"  attempts {r.get('attempts')}" if int(r.get('attempts') or 0) else ""))
        if failed:
            print(f"    FAILED {failed.get('failed_at')} rc={failed.get('rc')} attempts={failed.get('attempts')} retry_after={failed.get('retry_after')} log={failed.get('log')}")
        if blocked:
            print(f"    BLOCKED since {blocked.get('since')}: {blocked.get('reason')} {list(blocked.get('detail') or [])[:5]}")
    if LOCK.exists():
        info = read_yaml(LOCK)
        print(f"deployer lock: pid {info.get('pid')} window {info.get('window')} since {info.get('started')}")
    return 0


def main(argv: list) -> int:
    if argv and argv[0] == "status":
        return status()
    dry_run, retry_now = "--dry-run" in argv, "--retry-now" in argv
    only = argv[argv.index("--unit") + 1] if "--unit" in argv else ""
    units = U.load_units()
    if only and only not in units:
        print(f"unknown unit {only!r}", file=sys.stderr)
        return 2
    if dry_run:
        for u in units.values():
            if not only or u.name == only:
                process_unit(u, True, retry_now, "dry-run")
        return 0
    window = acquire_lock()
    if not window:
        return 0
    try:
        with Heartbeat(LOCK):
            for u in units.values():
                if only and u.name != only:
                    continue
                try:
                    process_unit(u, False, retry_now, window)
                except Exception as e:  # noqa: BLE001 — one unit's crash never stops the others
                    log(f"  {u.name}: deployer error {e!r}")
    finally:
        release_lock()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
