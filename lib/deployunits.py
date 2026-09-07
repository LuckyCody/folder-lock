"""Deploy units — which repo paths belong to which independently deployable artefact (PROTOCOL.md §11).

Shared by scripts/deploy_request.py, scripts/deployer.py, scripts/test_unit.py, the commit guard
(hooks/check_locks.py, warn-only) and scripts/board.py.

Units file: <repo>/.folder-lock/deploy-units.yaml

    units:
      web-app:
        description: "the customer-facing app"
        paths:                        # repo-relative globs, same syntax as registry.yaml owns:
          - apps/web/**
          - libs/shared/**
        deploy: python apps/web/deploy.py      # run from the repo root; exit 0 = deployed
        busy_exit_codes: [2]                   # "another deploy is in flight" -> retry next pass, not a failure
        tests: python -m pytest apps/web -q    # run by scripts/test_unit.py; leaves the marker the guard looks for
        dirty_ignore: ["**/*.log"]             # tracked files that may be dirty without blocking
        branch: main
        timeout_s: 3600

A deploy unit is NOT a lock domain: several lock domains (module folders) live inside one unit.
Folder locks protect the files being edited; the unit says which artefact needs a deploy.

Runtime state, all under <repo>/.goal/deploy/ (gitignored via **/.goal/):
    requests/<unit>/<minted>.yaml       pending requests
    done/<unit>/<minted>.yaml           completed requests + their result
    state/last/<unit>.yaml              last successful deploy
    state/failed/<unit>.yaml            last deploy FAILED — the board renders it until the next success
    state/blocked/<unit>.yaml           deploy BLOCKED (dirty tree / wrong branch / no commit in HEAD)
    state/tests/<unit>--<window>.json   test markers per session window
    deployer.lock                       single-deployer lock (PID + heartbeat, stale 15 min)
    logs/<unit>/<run>.log               captured deploy output
"""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import lockpath as lp  # noqa: E402  (glob semantics identical to the registry resolver)

ROOT = lp.ROOT
UNITS_FILE = ROOT / ".folder-lock" / "deploy-units.yaml"
BASE = lp.STATE / "deploy"
REQUESTS = BASE / "requests"
DONE = BASE / "done"
STATE = BASE / "state"
LOGS = BASE / "logs"
TS_FMT = lp.TS_FMT
TEST_MARKER_FRESH = timedelta(hours=24)


@dataclass
class Unit:
    name: str
    description: str = ""
    paths: list = field(default_factory=list)
    deploy: str = ""
    tests: str = ""
    busy_exit_codes: list = field(default_factory=list)
    dirty_ignore: list = field(default_factory=list)
    branch: str = "main"
    timeout_s: int = 3600

    @property
    def prefixes(self) -> list:
        out = []
        for g in self.paths:
            p = lp._fixed_prefix(g)
            if p and p not in out:
                out.append(p)
        return out


def _parse_units_text(text: str) -> dict:
    """PyYAML when available; otherwise a tolerant line parser for the documented shape
    (the commit guard must never depend on a third-party import)."""
    try:
        import yaml  # type: ignore
        return (yaml.safe_load(text) or {}).get("units") or {}
    except ImportError:
        pass
    import re
    units: dict = {}
    cur = None
    key = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip() if not raw.strip().startswith("-") or "#" not in raw else raw.rstrip()
        if not line.strip():
            continue
        m = re.match(r"^  ([A-Za-z0-9_.-]+):\s*$", line)
        if m:
            cur = units.setdefault(m.group(1), {})
            key = None
            continue
        if cur is None:
            continue
        m = re.match(r"^    ([a-z_]+):\s*(.*)$", line)
        if m:
            key, val = m.group(1), m.group(2).strip()
            if val.startswith("["):
                cur[key] = [v.strip().strip("'\"") for v in val.strip("[]").split(",") if v.strip()]
            elif val:
                cur[key] = val.strip("'\"")
            else:
                cur[key] = []
            continue
        m = re.match(r"^      - (.+)$", line)
        if m and key:
            cur.setdefault(key, []).append(m.group(1).strip().strip("'\""))
    return units


def load_units() -> dict:
    if not UNITS_FILE.is_file():
        return {}
    data = _parse_units_text(UNITS_FILE.read_text(encoding="utf-8"))
    units = {}
    for name, cfg in data.items():
        cfg = cfg or {}
        units[name] = Unit(
            name=name,
            description=str(cfg.get("description") or ""),
            paths=[str(p) for p in (cfg.get("paths") or [])],
            deploy=str(cfg.get("deploy") or ""),
            tests=str(cfg.get("tests") or ""),
            busy_exit_codes=[int(x) for x in (cfg.get("busy_exit_codes") or [])],
            dirty_ignore=[str(p) for p in (cfg.get("dirty_ignore") or [])],
            branch=str(cfg.get("branch") or "main"),
            timeout_s=int(cfg.get("timeout_s") or 3600),
        )
    return units


def units_for_path(rel: str, units: Optional[dict] = None) -> list:
    units = units if units is not None else load_units()
    rel = rel.replace("\\", "/").strip("/")
    return [u.name for u in units.values() if any(lp._glob_match(rel, g) for g in u.paths)]


def units_for_paths(rels: list, units: Optional[dict] = None) -> dict:
    units = units if units is not None else load_units()
    out: dict = {}
    for rel in rels:
        for name in units_for_path(rel, units):
            out.setdefault(name, []).append(rel)
    return out


def units_for_folder(folder: str, units: Optional[dict] = None) -> list:
    return units_for_path(folder.replace("\\", "/").strip("/") + "/__probe__", units)


def git(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def head_commit() -> str:
    return git("rev-parse", "HEAD").stdout.strip()


def head_branch() -> str:
    return git("symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip() or "detached"


def dirty_source_paths(unit: Unit) -> list:
    """Tracked files under the unit that differ from HEAD, minus dirty_ignore. Untracked files never
    count — they are not in HEAD, so they cannot make HEAD undeployable."""
    if not unit.prefixes:
        return []
    out = git("status", "--porcelain", "-z", "--", *unit.prefixes).stdout
    dirty = []
    for entry in out.split("\0"):
        if len(entry) < 4:
            continue
        code, path = entry[:2], entry[3:]
        if code.strip() == "??":
            continue
        if any(lp._glob_match(path, g) for g in unit.dirty_ignore):
            continue
        dirty.append(path)
    return dirty


# ---------------------------------------------------------------- test markers

def test_marker_path(unit: str, window: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in (window or "no-window"))[:80]
    return STATE / "tests" / f"{unit}--{safe}.json"


def read_test_marker(unit: str, window: str) -> Optional[dict]:
    import json
    p = test_marker_path(unit, window)
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    try:
        ran = datetime.strptime(d.get("ran_at", "")[:16], TS_FMT)
        d["fresh"] = datetime.now() - ran < TEST_MARKER_FRESH
    except ValueError:
        d["fresh"] = False
    return d


def write_test_marker(unit: str, window: str, result: str, cmd: str, rc: int, detail: str = "") -> Path:
    import json
    p = test_marker_path(unit, window)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "unit": unit, "window": window or "", "result": result, "rc": rc, "cmd": cmd,
        "commit": head_commit(), "ran_at": datetime.now().strftime(TS_FMT), "detail": detail[-2000:],
    }, indent=1), encoding="utf-8")
    return p


def main(argv: list) -> int:
    units = load_units()
    if not units:
        print(f"no deploy units ({UNITS_FILE} missing or empty) — see templates/deploy-units.yaml")
        return 0
    if not argv:
        for u in units.values():
            print(f"{u.name}: {u.description}")
            for g in u.paths:
                print(f"    {g}")
            print(f"    deploy: {u.deploy}\n    tests:  {u.tests or '(none)'}")
        return 0
    if argv[0] == "for" and len(argv) > 1:
        hits = units_for_path(argv[1], units)
        print(", ".join(hits) if hits else "(no deploy unit covers this path)")
        return 0
    if argv[0] == "dirty" and len(argv) > 1 and argv[1] in units:
        d = dirty_source_paths(units[argv[1]])
        print("\n".join(d) if d else "(clean)")
        return 1 if d else 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
