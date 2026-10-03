"""v5.6 unit ladder — deliverables for the owner (PROTOCOL §9). Plain `python tests/test_v56.py` or pytest.
Own throwaway state root + lock tree; never the live store.

  register     two files in <folder>/deliverables/ -> ONE `deliverable` item, waiting_owner, question "<title> — for
               your review", two files[] rows, two raw objects under deliverables/<folder>/<date>/<sha8>/<file>
  idempotent   a second register writes nothing (same sha256)
  supersede    a changed file after `done`: new row, old one superseded, item re-opened
  never fired  ready_items() drops the kind even when a writer left it `ready`; sync([]) keeps it open
  verbs        later -> parked, reopen -> waiting_owner with the question, done -> done; board_rows orders them
  sidecar      title / summary / bundle group files into cards; for_owner: false is skipped
  closing      the closing message carries the sentence and no path, key or id
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
SK = HERE.parents[1]
sys.path.insert(0, str(SK / "lib"))

_TMP = Path(tempfile.mkdtemp(prefix="folder-lock-v56-"))
os.environ["FOLDER_LOCK_STATE_ROOT"] = str(_TMP / "state")
os.environ["FOLDER_LOCK_STATE_BACKEND"] = "file"
os.environ.pop("FOLDER_LOCK_STATE_OFFLINE", None)
os.environ["FOLDER_LOCK_LOCK_TREE"] = str(_TMP / "tree")
os.environ.pop("ICM_WINDOW", None)
(_TMP / "tree" / "alpha" / ".goal").mkdir(parents=True, exist_ok=True)

import items  # noqa: E402
import deliverables as dlv  # noqa: E402
import lifecycle as lc  # noqa: E402

TREE = _TMP / "tree"
STORE = _TMP / "state" / "store"
FOLDER = "alpha"
XLSX = b"PK\x03\x04fixture"


def _ddir() -> Path:
    d = TREE / FOLDER / "deliverables"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _objects() -> list[Path]:
    base = STORE / "deliverables"
    return sorted(p for p in base.rglob("*") if p.is_file()) if base.exists() else []


def _only() -> tuple[str, dict]:
    data = items.load()
    ks = [k for k, e in data["items"].items() if e.get("kind") == dlv.KIND]
    assert len(ks) == 1, ks
    return ks[0], data["items"][ks[0]]


def _reset() -> None:
    import shutil
    data = items.load()
    data["items"] = {k: e for k, e in data["items"].items() if e.get("kind") != dlv.KIND}
    items.save(data)
    if (TREE / FOLDER / "deliverables").exists():
        shutil.rmtree(TREE / FOLDER / "deliverables")
    if (STORE / "deliverables").exists():
        shutil.rmtree(STORE / "deliverables")


def test_register_idempotent_supersede():
    _reset()
    d = _ddir()
    (d / "report.xlsx").write_bytes(XLSX)
    (d / "README.md").write_text("# r\n", encoding="utf-8")
    out = dlv.register(FOLDER, by="w1", decisions="kept it simple", root=TREE)
    assert len(out) == 1 and out[0]["new"] and out[0]["uploaded"] == 2
    key, e = _only()
    assert key.startswith("alpha|deliverable|") and key.endswith("-alpha")
    assert e["status"] == "waiting_owner" and e["question"].endswith(" — for your review") and e["title"] in e["question"]
    assert e["summary"] == "kept it simple" and e["bundle"] == "alpha" and e["created_by"] == "w1"
    assert [r["filename"] for r in e["files"]] == ["README.md", "report.xlsx"]
    x = next(r for r in e["files"] if r["filename"] == "report.xlsx")
    assert x["blob_path"].startswith("deliverables/alpha/") and x["sha256"][:8] in x["blob_path"] and x["badge"] == "xlsx"
    objs = _objects()
    assert len(objs) == 2 and any(p.name == "report.xlsx" and p.read_bytes() == XLSX for p in objs)
    # idempotent
    out2 = dlv.register(FOLDER, by="w1", root=TREE)
    assert out2[0]["changed"] is False and out2[0]["uploaded"] == 0 and len(_objects()) == 2
    assert items.load()["items"][key]["updated"] == e["updated"]
    # supersede after done
    dlv.mark_done(key, by="owner")
    assert items.load()["items"][key]["status"] == "done"
    (d / "README.md").write_text("# r2\n", encoding="utf-8")
    out3 = dlv.register(FOLDER, by="w2", root=TREE)
    assert out3[0]["changed"] and out3[0]["uploaded"] == 1 and not out3[0]["new"]
    key2, e2 = _only()
    assert key2 == key and e2["status"] == "waiting_owner" and len(e2["files"]) == 3
    assert [r["superseded"] for r in e2["files"] if r["filename"] == "README.md"] == [True, False] and len(_objects()) == 3


def test_never_fired_and_sync_keeps_it():
    _reset()
    (_ddir() / "a.csv").write_text("x\n", encoding="utf-8")
    dlv.register(FOLDER, by="w", root=TREE)
    key, _ = _only()
    data = items.load()
    data["items"][key]["status"] = "ready"
    items.save(data)
    assert key not in {r["key"] for r in items.ready_items()}
    data = items.load()
    data["items"][key]["status"] = "waiting_owner"
    items.save(data)
    items.sync([])                                   # no rows at all: everything else would be closed
    assert items.load()["items"][key]["status"] == "waiting_owner"


def test_verbs_board_rows_closing_message():
    _reset()
    d = _ddir()
    (d / "Petty.xlsx").write_bytes(XLSX)
    (d / "Petty.xlsx.meta.yaml").write_text("title: Petty triage\nsummary: 199 approved.\nbundle: petty\n", encoding="utf-8")
    (d / "notes.md").write_text("# n\n", encoding="utf-8")
    (d / "scratch.log").write_text("noise\n", encoding="utf-8")
    (d / "scratch.log.meta.yaml").write_text("for_owner: false\n", encoding="utf-8")
    out = dlv.register(FOLDER, by="w", root=TREE)
    assert len(out) == 2 and not any(p.name == "scratch.log" for p in _objects())
    data = items.load()
    petty_key = next(k for k, e in data["items"].items() if e.get("kind") == dlv.KIND and e.get("bundle") == "petty")
    assert data["items"][petty_key]["title"] == "Petty triage" and data["items"][petty_key]["summary"] == "199 approved."
    e = dlv.later(petty_key, by="owner")
    assert e["status"] == "parked" and e["park_reason"] == "later (owner)"
    rows = dlv.board_rows()
    assert [r["status"] for r in rows] == ["open", "parked"] and rows[1]["item_key"] == petty_key
    e = dlv.reopen(petty_key, by="owner")
    assert e["status"] == "waiting_owner" and e["question"] == "Petty triage — for your review" and "parked" not in e
    e = dlv.mark_done(petty_key, by="owner", outcome="reviewed")
    assert e["status"] == "done" and dlv.board_rows()[-1]["status"] == "done"
    msg = lc.format_closing_message([data["items"][petty_key]["question"]])
    assert msg == "Waiting on you: Petty triage — for your review" and "|" not in msg and "/" not in msg


if __name__ == "__main__":
    test_register_idempotent_supersede()
    test_never_fired_and_sync_keeps_it()
    test_verbs_board_rows_closing_message()
    print("v5.6: 3/3 ok")
