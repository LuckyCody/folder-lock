"""v5.4 unit ladder — foreground vs. background routing (PROTOCOL §18). Plain `python tests/test_v54.py` or pytest.
Own throwaway state root + lock tree; never the live store.

  channel      origin_channel(): fired-* window or ICM_FOLDER -> background; bound interactive window -> window;
               ICM_ORIGIN_CHANNEL=background is the verbal override; no identity -> background
  item         items.upsert(origin_channel=, minted_by=) persists both; an absent field reads as background
  guard        window_item_guard: open same-folder window item refuses; done / waiting_on_cody / fired_at pass;
               a background item never refuses; another window's item never refuses
  handoff      scripts/handoff.py from an interactive window writes `origin_channel: "window"` and refuses a target
               this window holds (rc 12); --channel background is the queue
  sweep        autorun.py's ready filter drops window items
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
SK = HERE.parents[1]
sys.path.insert(0, str(SK / "lib"))

_TMP = Path(tempfile.mkdtemp(prefix="folder-lock-v54-"))
os.environ["FOLDER_LOCK_STATE_ROOT"] = str(_TMP / "state")
os.environ["FOLDER_LOCK_STATE_BACKEND"] = "file"
os.environ.pop("FOLDER_LOCK_STATE_OFFLINE", None)
os.environ["FOLDER_LOCK_LOCK_TREE"] = str(_TMP / "tree")
os.environ.pop("ICM_WINDOW", None)
os.environ.pop("ICM_FOLDER", None)
os.environ.pop("ICM_ORIGIN_CHANNEL", None)
(_TMP / "tree").mkdir(parents=True, exist_ok=True)

import channel as ch  # noqa: E402
import items  # noqa: E402


def test_channel_derivation():
    assert ch.origin_channel({"ICM_WINDOW": "fired-forge-260929-ab12"}) == "background"
    assert ch.origin_channel({"ICM_WINDOW": "alpha-260929-ab12", "ICM_FOLDER": "alpha"}) == "background"
    assert ch.origin_channel({"ICM_WINDOW": "alpha-260929-ab12"}) == "window"
    assert ch.origin_channel({"ICM_WINDOW": "alpha-260929-ab12", "ICM_ORIGIN_CHANNEL": "background"}) == "background"
    assert ch.origin_channel({}) == "background"
    assert ch.channel_of({}) == "background" and ch.channel_of({"origin_channel": "window"}) == "window"
    assert ch.window_tier({"AUTORUN_TIER": "routine"}) == "routine"
    assert ch.window_tier({"ANTHROPIC_MODEL": "claude-opus-5"}) == "default"


def test_item_persists_channel_and_minted_by():
    k = items.upsert("alpha", "handoff", "n1.staged.md", "do the thing", origin_channel="window", minted_by="alpha-260929-ab12")
    e = items.load()["items"][k]
    assert e["origin_channel"] == "window" and e["minted_by"] == "alpha-260929-ab12"
    k2 = items.upsert("alpha", "handoff", "n2.staged.md", "another thing")
    assert ch.channel_of(items.load()["items"][k2]) == "background"
    items.set_waiting_on_cody(k, "alpha is held by another session; the owner decides.")
    assert items.load()["items"][k]["waiting_on_cody"].startswith("alpha is held")


def test_window_item_guard():
    me = "beta-260929-cd34"
    data = {"items": {
        "beta|handoff|a.md": {"owner": "beta", "status": "ready", "origin_channel": "window", "minted_by": me},
        "beta|handoff|b.md": {"owner": "beta", "status": "done", "origin_channel": "window", "minted_by": me},
        "beta|handoff|c.md": {"owner": "beta", "status": "ready", "origin_channel": "window", "minted_by": me,
                              "waiting_on_cody": "beta is held by x; the owner decides."},
        "gamma|handoff|d.md": {"owner": "gamma", "status": "ready", "origin_channel": "window", "minted_by": me},
        "gamma|handoff|e.md": {"owner": "gamma", "status": "ready", "origin_channel": "window", "minted_by": me,
                               "fired_at": "2026-09-29T10:00:00"},
        "gamma|handoff|f.md": {"owner": "gamma", "status": "in_progress", "origin_channel": "window", "minted_by": me},
        "beta|handoff|g.md": {"owner": "beta", "status": "ready", "origin_channel": "background", "minted_by": me},
        "beta|handoff|h.md": {"owner": "beta", "status": "ready", "origin_channel": "window", "minted_by": "other-260929-zz99"},
    }}
    out = ch.window_item_guard(me, ["beta"], data)
    assert len(out) == 2, out
    assert any("beta|handoff|a.md" in o and "this window holds" in o for o in out)
    assert any("gamma|handoff|d.md" in o and "not been claimed" in o for o in out)
    assert ch.window_item_guard("other-260929-zz99", ["beta"], data) and len(ch.window_item_guard("other-260929-zz99", ["beta"], data)) == 1


def test_handoff_writes_channel_and_refuses_same_folder():
    tree = _TMP / "tree"
    (tree / "held" / ".goal").mkdir(parents=True, exist_ok=True)
    (tree / "other" / ".goal").mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, ICM_WINDOW="held-260929-ef56", PYTHONIOENCODING="utf-8", ICM_NO_DIRECT_FIRE="1")
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    # a background mint (explicit): plain STAGED, no direct fire
    r = subprocess.run([sys.executable, str(SK / "scripts" / "handoff.py"), "--to", "other", "--task", "queue this one",
                        "--from", "held", "--channel", "background"], capture_output=True, text=True, env=env, cwd=str(SK))
    assert r.returncode == 0, r.stdout + r.stderr
    note = next((tree / "other" / ".goal" / "inbox").glob("*.staged.md"))
    assert 'origin_channel: "background"' in note.read_text(encoding="utf-8")
    e = items.load()["items"][f"other|handoff|{note.name}"]
    assert ch.channel_of(e) == "background"
    # a window mint aimed at a folder this window holds is refused (rc 12); nothing written
    import lockpath as lp
    (tree / "held" / ".goal" / "LOCK.yaml").write_text(
        f'holder: interactive\nwindow: "held-260929-ef56"\nstatus: open\ntask: "t"\nstream: "held"\nbranch: "main"\n'
        f'started: "{__import__("mint").timestamp()}"\n', encoding="utf-8")
    before = sorted(p.name for p in (tree / "held" / ".goal").glob("inbox/*.md"))
    r = subprocess.run([sys.executable, str(SK / "scripts" / "handoff.py"), "--to", "held", "--task", "finish here",
                        "--from", "held"], capture_output=True, text=True, env=env, cwd=str(SK))
    assert r.returncode == ch.RC_SAME_FOLDER, r.stdout + r.stderr
    assert "foreground work continues here" in r.stderr
    assert sorted(p.name for p in (tree / "held" / ".goal").glob("inbox/*.md")) == before


def test_sweep_filter_shape():
    rows = [{"key": "a|handoff|x", "owner": "a", "origin_channel": "window"}, {"key": "b|handoff|y", "owner": "b"}]
    left = [it for it in rows if ch.channel_of(it) != ch.WINDOW]
    assert [r["key"] for r in left] == ["b|handoff|y"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("v5.4 ladder green")
