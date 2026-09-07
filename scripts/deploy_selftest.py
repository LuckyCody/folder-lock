"""Prove the deploy queue holds by running it against a throwaway repo (PROTOCOL.md §11).

    python scripts/deploy_selftest.py

Scenarios, each a visible PASS/FAIL:
  1. two requests on the same HEAD collapse into ONE deploy; both requesting folders receive the same
     deployed_commit in workflow-state/deploys.jsonl; requests moved to done/
  2. a request whose commit HEAD already carries completes as already-deployed without a new run
  3. a failing deploy command leaves the request pending (attempts=1), writes state/failed/<unit>.yaml
     and a `failed` line into the requesting workflow-state
  4. a dirty tracked file under the unit BLOCKS the deploy (state/blocked/<unit>.yaml), requests stay pending
  5. the commit guard WARNS (does not refuse) when a commit touches a unit without a test marker, and stays
     silent once scripts/test_unit.py has run in that window
Exit 0 only if every scenario behaves.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
PY = sys.executable


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="folderlock-deploy-"))
    repo = tmp / "repo"
    results = []

    def case(name: str, ok: bool, detail: str = ""):
        results.append(ok)
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"\n        {detail[:400]}" if detail and not ok else ""))

    try:
        (repo / ".githooks" / "lib").mkdir(parents=True)
        (repo / ".folder-lock").mkdir()
        for f in (SKILL / "hooks").iterdir():
            if f.is_file():
                shutil.copy2(f, repo / ".githooks" / f.name)
        for f in (SKILL / "lib").glob("*.py"):
            shutil.copy2(f, repo / ".githooks" / "lib" / f.name)
        env = {k: v for k, v in os.environ.items() if k not in ("ICM_WINDOW", "ICM_LOCK_BYPASS", "CLAUDE_CODE_SESSION_ID", "MAIN_COMMIT_OK")}
        env["FOLDER_LOCK_ROOT"] = str(repo)

        def run(*args, **kw):
            e = dict(env); e.update(kw.pop("env", {}))
            return subprocess.run(list(args), cwd=repo, env=e, capture_output=True, text=True, encoding="utf-8", errors="replace")

        def script(name, *args, **kw):
            return run(PY, str(SKILL / "scripts" / name), *args, **kw)

        run("git", "init", "-q", "-b", "main")
        run("git", "config", "user.email", "t@x.invalid"); run("git", "config", "user.name", "deploy-selftest")
        run("git", "config", "commit.gpgsign", "false"); run("git", "config", "core.hooksPath", str(repo / ".githooks"))
        (repo / ".gitignore").write_text("**/.goal/\n", encoding="utf-8")
        marker = repo / "deployed.txt"
        (repo / ".folder-lock" / "deploy-units.yaml").write_text(
            "units:\n"
            "  app:\n    paths:\n      - app/**\n"
            f"    deploy: {PY} -c \"open(r'{marker}','a').write('x')\"\n"
            "    tests: " + PY + " -c \"print('tests ok')\"\n"
            "    dirty_ignore:\n      - \"**/*.log\"\n"
            "  bad:\n    paths:\n      - bad/**\n"
            f"    deploy: {PY} -c \"import sys; print('boom'); sys.exit(1)\"\n", encoding="utf-8")
        for d in ("app", "app/billing", "app/core", "bad"):
            (repo / d).mkdir(parents=True, exist_ok=True)
            (repo / d / ".goal").mkdir(exist_ok=True)
        (repo / "app" / "a.py").write_text("print(1)\n"); (repo / "bad" / "b.py").write_text("print(2)\n")
        (repo / "app" / "app.log").write_text("log\n")
        run("git", "add", "app/a.py", "bad/b.py", "app/app.log", ".gitignore", ".folder-lock/deploy-units.yaml")
        r = run("git", "commit", "-q", "-m", "base", env={"ICM_LOCK_BYPASS": "1", "MAIN_COMMIT_OK": "1"})
        assert r.returncode == 0, r.stdout + r.stderr
        head = run("git", "rev-parse", "HEAD").stdout.strip()

        # 1. two requests collapse
        r1 = script("deploy_request.py", "--unit", "app", "--workflow", "app/billing", env={"ICM_WINDOW": "win-a"})
        r2 = script("deploy_request.py", "--unit", "app", "--workflow", "app/core", env={"ICM_WINDOW": "win-b"})
        d = script("deployer.py")
        done = list((repo / ".goal" / "deploy" / "done" / "app").glob("*.yaml"))
        logs = list((repo / ".goal" / "deploy" / "logs" / "app").glob("*.log"))
        rec_b = [json.loads(l) for l in (repo / "app/billing/workflow-state/deploys.jsonl").read_text().splitlines()] if (repo / "app/billing/workflow-state/deploys.jsonl").exists() else []
        rec_c = [json.loads(l) for l in (repo / "app/core/workflow-state/deploys.jsonl").read_text().splitlines()] if (repo / "app/core/workflow-state/deploys.jsonl").exists() else []
        ok = (r1.returncode == 0 and r2.returncode == 0 and len(done) == 2 and len(logs) == 1
              and marker.exists() and marker.read_text() == "x"
              and rec_b and rec_c and rec_b[0]["event"] == "deployed" and rec_b[0]["deployed_commit"] == head
              and rec_c[0]["deployed_commit"] == head and rec_b[0]["build"]["collapsed"] == 2)
        case("two requests on one HEAD collapse into ONE deploy; both workflow-states get deployed_commit", ok,
             r1.stdout + r2.stdout + d.stdout + d.stderr)

        # 2. already-deployed short-circuit
        script("deploy_request.py", "--unit", "app", "--workflow", "app/billing", env={"ICM_WINDOW": "win-a"})
        d2 = script("deployer.py")
        rec_b = [json.loads(l) for l in (repo / "app/billing/workflow-state/deploys.jsonl").read_text().splitlines()]
        case("request on an already-deployed HEAD completes without a new run", marker.read_text() == "x"
             and rec_b[-1]["event"] == "already-deployed" and not list((repo / ".goal/deploy/requests/app").glob("*.yaml")),
             d2.stdout)

        # 3. failing deploy -> pending + failed flag + workflow-state line
        script("deploy_request.py", "--unit", "bad", "--workflow", "bad", env={"ICM_WINDOW": "win-a"})
        d3 = script("deployer.py")
        failed = repo / ".goal/deploy/state/failed/bad.yaml"
        pend = list((repo / ".goal/deploy/requests/bad").glob("*.yaml"))
        rec_bad = [json.loads(l) for l in (repo / "bad/workflow-state/deploys.jsonl").read_text().splitlines()] if (repo / "bad/workflow-state/deploys.jsonl").exists() else []
        case("failing deploy: request stays pending (attempts=1), failed flag written, `failed` line in workflow-state",
             failed.exists() and len(pend) == 1 and "attempts: 1" in pend[0].read_text() and rec_bad and rec_bad[-1]["event"] == "failed",
             d3.stdout + d3.stderr)
        d3b = script("deployer.py")
        case("failure back-off: the next pass does not retry immediately", "backing off" in d3b.stdout, d3b.stdout)

        # 4. dirty tree blocks
        (repo / "app" / "a.py").write_text("print(3)\n")
        script("deploy_request.py", "--unit", "app", "--workflow", "app/core", env={"ICM_WINDOW": "win-b"})
        d4 = script("deployer.py")
        blocked = repo / ".goal/deploy/state/blocked/app.yaml"
        case("dirty tracked file under the unit BLOCKS the deploy; request stays pending", blocked.exists()
             and "uncommitted" in blocked.read_text() and len(list((repo / ".goal/deploy/requests/app").glob("*.yaml"))) == 1,
             d4.stdout)
        (repo / "app" / "app.log").write_text("more log\n")
        run("git", "add", "app/a.py")
        run("git", "commit", "-q", "-m", "a.py change", env={"ICM_LOCK_BYPASS": "1", "MAIN_COMMIT_OK": "1"})
        d4b = script("deployer.py")
        case("committed -> unblocked: a dirty log (dirty_ignore) does not block, the new HEAD deploys, blocked flag clears",
             not blocked.exists() and "OK app" in d4b.stdout and marker.read_text() == "xx", d4b.stdout)

        # 5. commit guard warns without a test marker, silent after test_unit
        (repo / "app" / "core" / ".goal" / "LOCK.yaml").write_text(
            'holder: interactive\nwindow: "win-c"\nstatus: open\ntask: "t"\nstream: "s"\nbranch: "main"\n'
            f'started: "{__import__("datetime").datetime.now().strftime("%Y-%m-%dT%H:%M")}"\n', encoding="utf-8")
        (repo / "app" / "core" / "c.py").write_text("print(4)\n")
        run("git", "add", "app/core/c.py")
        g1 = run("git", "commit", "-q", "-m", "core change", env={"ICM_WINDOW": "win-c", "MAIN_COMMIT_OK": "1"})
        case("commit guard WARNS (exit 0) when a commit touches a unit with no test marker for the window",
             g1.returncode == 0 and "WARN" in (g1.stdout + g1.stderr) and "test_unit.py app" in (g1.stdout + g1.stderr),
             g1.stdout + g1.stderr)
        t = script("test_unit.py", "app", env={"ICM_WINDOW": "win-c"})
        (repo / "app" / "core" / "c.py").write_text("print(5)\n")
        run("git", "add", "app/core/c.py")
        g2 = run("git", "commit", "-q", "-m", "core change 2", env={"ICM_WINDOW": "win-c", "MAIN_COMMIT_OK": "1"})
        case("after scripts/test_unit.py the same commit is silent", t.returncode == 0 and g2.returncode == 0
             and "WARN" not in (g2.stdout + g2.stderr), t.stdout + g2.stdout + g2.stderr)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    ok = all(results)
    print(f"[deploy-queue] self-test {'PASS' if ok else 'FAIL'}: {sum(results)}/{len(results)}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
