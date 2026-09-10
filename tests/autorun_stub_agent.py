"""Stub 'fired agent' for tests/autorun_run.py — the smallest runner that behaves like a disciplined agent.

Reads the prompt on stdin and the item from AUTORUN_ITEM* env. Title conventions used by the test:
  "CROSS-FOLDER-> <folder>: <task>"  -> creates that item via scripts/handoff.py (dedup applies), then completes its own
  "DECIDE-OWNER …"                   -> hits a §12 blocker: items.py wait --question, pointer WHEN owner …
  anything else                      -> executes (touches autorun_done.txt), closes pointer / deletes handoff, marks done
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lib"))
import lockpath as lp  # noqa: E402

ROOT = lp.ROOT
PY = sys.executable


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([PY, *args], cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace")


def main() -> int:
    sys.stdin.read()
    folder, key = os.environ["ICM_FOLDER"], os.environ["AUTORUN_ITEM"]
    kind, ref, title, window = os.environ["AUTORUN_ITEM_KIND"], os.environ["AUTORUN_ITEM_REF"], os.environ.get("AUTORUN_ITEM_TITLE", ""), os.environ.get("ICM_WINDOW", "stub")
    items_py, log_py, handoff_py = str(HERE.parent / "lib" / "items.py"), str(HERE.parent / "lib" / "autorun_log.py"), str(HERE.parent / "scripts" / "handoff.py")
    ptr = ROOT / folder / "workflow-state" / "current-pointer.md"
    if "DECIDE-OWNER" in title:
        run(items_py, "wait", key, "--question", "Which colour does the fixture use? Options: (a) keep blue; (b) switch to green; (c) drop colours. Recommendation: (a).")
        ptr.parent.mkdir(parents=True, exist_ok=True)
        ptr.write_text(f"# Current pointer — {folder}\n\nNext concrete action: WHEN owner answers the colour question → apply it and close.\n", encoding="utf-8")
        run(log_py, "append", folder, "--item", title[:80], "--status", "waiting_owner", "--decisions", "blocked per §12", "--commit", "-", "--by", window)
        print("WAITING_OWNER"); return 0
    decisions = "none"
    m = re.search(r"CROSS-FOLDER->\s*([^:]+):\s*(.+)$", title)
    if m:
        r = run(handoff_py, "--to", m.group(1).strip(), "--task", m.group(2).strip(), "--from", folder)
        decisions = f"created cross-folder item (rc={r.returncode})"
    with (ROOT / folder / "autorun_done.txt").open("a", encoding="utf-8") as fh:
        fh.write(f"{window}: {title}\n")
    if kind == "handoff":
        (ROOT / folder / ".goal" / "inbox" / ref).unlink(missing_ok=True)
    else:
        ptr.write_text(f"# Current pointer — {folder}\n\nNext concrete action: NONE — fixture item done by {window}.\n", encoding="utf-8")
    run(items_py, "done", key)
    run(log_py, "append", folder, "--item", title[:80], "--status", "done", "--decisions", decisions, "--commit", "-", "--by", window)
    print("DONE"); return 0


if __name__ == "__main__":
    sys.exit(main())
