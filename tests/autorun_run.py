"""Autorun acceptance run (PROTOCOL §13). Needs a repo root (lockpath.ROOT) — run from any folder-lock installation.

Seeds two fixture workfolders `_autorun-test-alpha/` and `_autorun-test-beta/` at the repo root (each with .goal/
so they are their own lock domains) and three items: a plain ready pointer, a handoff that creates a cross-folder
item for beta, and a pointer that must end waiting_owner. Runs scripts/autorun.py with tests/autorun_stub_agent.py
as the runner and asserts statuses, the question, created_by, one autorun-log line per item, and the dedup refusal.

    python tests/autorun_run.py [--keep]
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
# §14: the acceptance run uses a THROWAWAY state root (items, inbox index, seen stamp, bindings) — never the live one.
# Set before lockpath is imported so the in-process items.load() and every subprocess agree on it.
os.environ.setdefault("FOLDER_LOCK_STATE_ROOT", tempfile.mkdtemp(prefix="folderlock-autorun-state-"))
sys.path.insert(0, str(HERE.parent / "lib"))
import lockpath as lp  # noqa: E402
import items  # noqa: E402

ROOT = lp.ROOT
ALPHA, BETA = "_autorun-test-alpha", "_autorun-test-beta"
PY = sys.executable
# fixture handoffs must not land on the running session's binding (they are deleted with the fixture)
TEST_ENV = {k: v for k, v in os.environ.items() if k != 'CLAUDE_CODE_SESSION_ID'}
STUB = f'"{PY}" "{HERE / "autorun_stub_agent.py"}"'


def sh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([PY, *args], cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace", env=TEST_ENV)


def rmtree(p: Path) -> None:
    for _ in range(6):
        shutil.rmtree(p, ignore_errors=True)
        if not p.exists():
            return
        time.sleep(0.5)


def main(argv: list[str]) -> int:
    for f in (ALPHA, BETA):
        rmtree(ROOT / f)
        (ROOT / f / ".goal" / "inbox").mkdir(parents=True, exist_ok=True)
        for stale in (ROOT / f / ".goal" / "inbox").glob("*.md"):
            stale.unlink()
        (ROOT / f / "workflow-state").mkdir(parents=True, exist_ok=True)
        for stale in (ROOT / f / "workflow-state").glob("*.md"):
            stale.unlink()
        (ROOT / f / "autorun_done.txt").unlink(missing_ok=True)
        (ROOT / f / "CONTEXT.md").write_text(f"# {f} — autorun fixture\n", encoding="utf-8")
    (ROOT / ALPHA / "workflow-state" / "current-pointer.md").write_text("# Current pointer — alpha\n\nNext concrete action: write hello.txt (fixture: plain ready item).\n", encoding="utf-8")
    (ROOT / BETA / "workflow-state" / "current-pointer.md").write_text("# Current pointer — beta\n\nNext concrete action: DECIDE-OWNER which colour the fixture uses.\n", encoding="utf-8")
    d = items.load()
    for k in [k for k in d["items"] if k.startswith("_autorun-test-")]:
        del d["items"][k]
    items.save(d)
    r = sh(str(HERE.parent / "scripts" / "handoff.py"), "--to", ALPHA, "--task", f"CROSS-FOLDER-> {BETA}: write world.txt into beta", "--from", "tests")
    assert r.returncode == 0, r.stdout + r.stderr
    r = sh(str(HERE.parent / "scripts" / "autorun.py"), "--runner", STUB, "--owner-prefix", "_autorun-test-")
    print(r.stdout[-1500:])
    fx = {k: e for k, e in items.load()["items"].items() if k.startswith("_autorun-test-")}
    fails = []
    def expect(c: bool, m: str) -> None:
        print(("  PASS  " if c else "  FAIL  ") + m)
        if not c:
            fails.append(m)
    a_ptr = fx.get(f"{ALPHA}|pointer|workflow-state/current-pointer.md", {})
    b_ptr = fx.get(f"{BETA}|pointer|workflow-state/current-pointer.md", {})
    a_h = [e for k, e in fx.items() if k.startswith(f"{ALPHA}|handoff|")]
    b_h = [e for k, e in fx.items() if k.startswith(f"{BETA}|handoff|")]
    expect(a_ptr.get("status") == "done", "alpha pointer item done")
    expect(bool(a_h) and all(e["status"] == "done" for e in a_h), "alpha cross-folder-creator handoff done")
    expect(bool(b_h) and all(e["status"] == "done" for e in b_h), "cross-folder item created for beta and done")
    expect(bool(b_h) and b_h[0].get("created_by") == ALPHA, "created_by on the cross-folder item = alpha")
    expect(b_ptr.get("status") == "waiting_owner" and "Recommendation:" in b_ptr.get("question", "") and b_ptr.get("blocked_since"), "beta waiting_owner with decision-ready question + blocked_since")
    for f, n in ((ALPHA, 2), (BETA, 2)):
        p = ROOT / f / "workflow-state" / "autorun-log.md"
        got = sum(1 for l in p.read_text(encoding="utf-8").splitlines() if l.startswith("- ")) if p.exists() else 0
        expect(got == n, f"{f} autorun-log has {n} lines (got {got})")
    sh(str(HERE.parent / "scripts" / "handoff.py"), "--to", BETA, "--task", "dedup fixture: same title twice", "--from", ALPHA)
    r3 = sh(str(HERE.parent / "scripts" / "handoff.py"), "--to", BETA, "--task", "dedup fixture: same title twice", "--from", ALPHA)
    expect(r3.returncode == 4, f"duplicate handoff refused with exit 4 (got {r3.returncode})")
    print("\nRESULT:", "PASS" if not fails else f"FAIL: {fails}")
    if "--keep" not in argv:
        d = items.load()
        for k in [k for k in d["items"] if k.startswith("_autorun-test-")]:
            del d["items"][k]
        items.save(d)
        for f in (ALPHA, BETA):
            rmtree(ROOT / f)
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
