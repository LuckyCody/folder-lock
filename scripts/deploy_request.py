"""Drop a deploy request — the ONLY way a session asks for a deploy (PROTOCOL.md §11).

    python scripts/deploy_request.py --for <workfolder>                 # unit(s) resolved from the folder's path
    python scripts/deploy_request.py --unit web-app --workflow <folder> [--note "..."]
    python scripts/deploy_request.py --changed                          # units touched by HEAD (last commit)

Writes <repo>/.goal/deploy/requests/<unit>/<minted>.yaml {unit, workflow, commit, branch, requested_at,
window, note, status: pending}. The single deployer (scripts/deployer.py, run every few minutes by any
scheduler) deploys HEAD once for every pending request of the unit and writes the result into
<workflow>/workflow-state/deploys.jsonl (+ last-deploy.yaml). Nothing here deploys.

Exit 0 also when `--for` finds no unit — the signoff calls this unconditionally.
Exit 3 = request dropped but the tree under the unit is dirty (the deployer will BLOCK until you commit).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lib"))
import deployunits as U  # noqa: E402
import lockpath as lp  # noqa: E402
import mint  # noqa: E402


def _window() -> str:
    try:
        me = lp.identity()
    except lp.IdentityConflict:
        return ""
    return me.window if me else ""


def _session_folders() -> list:
    sid = (os.environ.get("CLAUDE_CODE_SESSION_ID") or "").strip()
    return [f for f in lp.read_session(sid).get("folders", []) if f] if sid else []


def _q(s: str) -> str:
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def drop(unit: U.Unit, workflow: str, note: str, window: str) -> Path:
    (U.REQUESTS / unit.name).mkdir(parents=True, exist_ok=True)
    p = U.REQUESTS / unit.name / f"{mint.handoff(workflow or unit.name)}.yaml"
    p.write_text(
        f"unit: {unit.name}\nworkflow: {_q(workflow)}\ncommit: {_q(U.head_commit())}\nbranch: {_q(U.head_branch())}\n"
        f"requested_at: {_q(mint.timestamp())}\nwindow: {_q(window)}\nnote: {_q(note)}\nstatus: pending\nattempts: 0\n",
        encoding="utf-8")
    return p


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--for", dest="for_folder", default="")
    ap.add_argument("--unit", default="")
    ap.add_argument("--workflow", default="")
    ap.add_argument("--changed", action="store_true")
    ap.add_argument("--note", default="")
    a = ap.parse_args()
    units = U.load_units()
    workflow = (a.workflow or a.for_folder).replace("\\", "/").strip("/")
    if a.for_folder:
        targets = U.units_for_folder(a.for_folder, units)
        if not targets:
            print(f"no deploy unit covers {a.for_folder} — nothing to request ({U.UNITS_FILE.relative_to(U.ROOT).as_posix()}).")
            return 0
    elif a.unit:
        if a.unit not in units:
            print(f"ERROR: unknown unit {a.unit!r}; known: {', '.join(units) or '(none)'}", file=sys.stderr)
            return 2
        targets = [a.unit]
    elif a.changed:
        files = [f for f in U.git("diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").stdout.splitlines() if f]
        targets = sorted(U.units_for_paths(files, units))
        if not targets:
            print("HEAD touches no deploy unit — nothing to request.")
            return 0
    else:
        ap.error("one of --for, --unit, --changed is required")
    if not workflow:
        folders = _session_folders()
        if len(folders) != 1:
            print(f"ERROR: --workflow <folder> required (this session holds {len(folders)} folder(s))", file=sys.stderr)
            return 2
        workflow = folders[0]
    window = _window()
    rc = 0
    for name in targets:
        unit = units[name]
        if U.head_branch() != unit.branch:
            print(f"WARN {name}: HEAD is on {U.head_branch()!r}; the deployer deploys {unit.branch!r} only — the request waits.")
        dirty = U.dirty_source_paths(unit)
        if dirty:
            print(f"WARN {name}: {len(dirty)} tracked file(s) under the unit are uncommitted — the deployer deploys HEAD, "
                  f"not your tree, and BLOCKS while these are dirty:")
            for d in dirty[:10]:
                print(f"    {d}")
            rc = 3
        p = drop(unit, workflow, a.note, window)
        print(f"REQUESTED {name} · commit {U.head_commit()[:7]} · for {workflow} -> {p.relative_to(U.ROOT).as_posix()}")
    print(f"The deployer deploys HEAD once for all pending requests; result -> {workflow}/workflow-state/deploys.jsonl. "
          f"Status: python scripts/deployer.py status")
    return rc


if __name__ == "__main__":
    sys.exit(main())
