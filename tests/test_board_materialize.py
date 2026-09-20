"""Board materialization v5.2 - the generic half, ported from the workspace acceptance list
(_icm/tests/test_board_materialize.py) minus the Postgres case: the twin has no history line.

  bm-1  materialize writes board/board.json + board/board.md; the waiting_owner row carries the handle, the
        question, the pointer tail, the autorun excerpt, and an `<!-- answer: <handle> -->` anchor in the md
  bm-2  fresh-path budget: a glance on a fresh board = ONE raw GET (board.json), the stamp checks (`items` etag +
        the lock fingerprint, both local), ONE document GET + ONE write (the touch); NO build_view, NO subprocess
  bm-3  stale detection: move `items` after the materialize -> the glance falls back to a render (+ materialize)
  bm-4  idempotency + concurrent writers: N materializes in parallel all succeed and leave a parseable board whose
        stamps match the store; two sequential ones differ only in generated_at
  bm-6  a resurrected staged note the render deleted appears in the next board's digest
  bm-7  answer is LOCKLESS (no lock is taken): item waiting_owner -> ready with answered_at/answer_ref, the note is
        FILED (type `answer`) in the folder's inbox and read as `filed` by the next render, which HOLDS the item
        ready; a non-waiting item is refused (rc 3), an unknown handle rc 4, no text rc 2; the batch parser skips
        empty anchors
  bm-8  the env fuse: a monkeypatched `statestore.BACKEND` can never reach the live store, and `items._live_store()`
        is the ONE predicate a destructive path asks (deletion refused, file left in place, no log line)

Nothing here ever touches a real store: throwaway STATE_ROOT + lock tree, file backend, set BEFORE the imports.
`python tests/test_board_materialize.py` or `python -m pytest tests/test_board_materialize.py -q`.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from pathlib import Path

import pytest

HERE = Path(__file__).resolve()
SK = HERE.parents[1]
sys.path.insert(0, str(SK / "lib"))
sys.path.insert(0, str(SK / "scripts"))      # `board` is what boardmat renders with

_TMP = Path(tempfile.mkdtemp(prefix="fl-boardmat-"))
_TREE = _TMP / "tree"
_TREE.mkdir(parents=True, exist_ok=True)

# NO lib import here. pytest collects modules alphabetically, and test_v43.py — which imports lockpath at module
# scope and asserts `lp.LOCK_TREE` is ITS throwaway tree — runs AFTER this file, so a lib import here would freeze
# lockpath to THIS tree first and test_v43 would fail. The imports + seed happen in _setup(), and the frozen root
# globals are pinned to this tree for the duration of THIS module's tests (_pin/_unpin): a lazy import alone is not
# enough, because when test_v43 imported lockpath first, `import lockpath` here is a no-op and LOCK_TREE still points
# at test_v43's tree — which these tests must not touch.
_ENV_VARS = {
    "FOLDER_LOCK_STATE_ROOT": str(_TMP / "state"),
    "FOLDER_LOCK_STATE_BACKEND": "file",
    "FOLDER_LOCK_ROOT": str(_TREE),
    "FOLDER_LOCK_LOCK_TREE": str(_TREE),
    "FOLDER_LOCK_STATE_OFFLINE": None,
}
_PREV_ENV = {k: os.environ.get(k) for k in _ENV_VARS}

POINTER_REF = "workflow-state/current-pointer.md"
ASKING = "alpha"          # the folder whose handoff waits on the owner
WORKING = "beta"          # a folder with plain agent work
ASK_Q = "Which option? Options: a/b/c. Recommendation: a"
NOTE = "x.staged.md"
N = chr(10)


def _folder(rel: str, action: str) -> Path:
    f = _TREE / rel
    (f / ".goal" / "inbox").mkdir(parents=True, exist_ok=True)
    (f / "workflow-state").mkdir(parents=True, exist_ok=True)
    (f / "workflow-state" / "current-pointer.md").write_text(
        "# Current pointer - " + rel + N + N + "second live context line" + N + N
        + "Next concrete action: " + action + N, encoding="utf-8")
    return f


def _note(folder: Path, name: str, task: str, ntype: str = "task") -> Path:
    p = folder / ".goal" / "inbox" / name
    p.write_text("---" + N + "mode: stage" + N + "type: " + ntype + N
                 + 'task: "' + task + '"' + N + "---" + N + N + "body" + N, encoding="utf-8")
    return p


def _items_upsert(folder: str, ref: str, title: str) -> str:
    return items.upsert(folder, "handoff", ref, title, force=True)


def _seed() -> None:
    a = _folder(ASKING, "work the alpha item")
    b = _folder(WORKING, "wire the beta second step")
    _note(a, NOTE, "Do the asking thing")
    _note(b, "y.staged.md", "Do the beta thing")
    (a / "workflow-state" / "autorun-log.md").write_text(
        "# Autorun log — alpha" + N + N
        + "- 2026-09-19T01:00 · alpha · fixture item · waiting_owner · - · by fired-x · decisions: asked the owner" + N,
        encoding="utf-8")
    _items_upsert(ASKING, NOTE, "Do the asking thing")
    _items_upsert(WORKING, "y.staged.md", "Do the beta thing")


def _ask(key: str, q: str = ASK_Q) -> None:
    items.set_status(key, "waiting_owner", question=q)


def _handle(key: str) -> str:
    return items.handle(key)


def _doc() -> dict:
    return items.load()["items"]


def _board() -> dict:
    raw, _etag = statestore.raw_get(boardmat.BOARD_JSON)
    assert raw is not None, "board.json is not in the store"
    return json.loads(raw.decode("utf-8"))


def _md() -> str:
    raw, _etag = statestore.raw_get(boardmat.BOARD_MD)
    return (raw or b"").decode("utf-8")


def _apply_env() -> None:
    for k, v in _ENV_VARS.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _restore_env() -> None:
    for k, prev in _PREV_ENV.items():
        if prev is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = prev


def _pin() -> list:
    """Re-point the frozen root globals of every already-imported lib module to THIS fixture's tree.

    lockpath/statestore/items/autorun_log/boardmat freeze ROOT/LOCK_TREE/STATE_ROOT/CACHE/FILESTORE/BACKEND at
    import. In a combined pytest run a sibling (test_v43) imported lockpath first, so the imports in _setup() were
    no-ops and every derived path still points at the sibling's tree; re-derive them from the CURRENT env and
    remember the old values so _unpin() can hand the sibling its tree back EXACTLY (never re-derive at teardown —
    the sibling never re-set the env)."""
    root = Path(_ENV_VARS["FOLDER_LOCK_ROOT"]).resolve()
    lock_tree = Path(_ENV_VARS["FOLDER_LOCK_LOCK_TREE"]).resolve()
    state_root = Path(_ENV_VARS["FOLDER_LOCK_STATE_ROOT"])       # lockpath._detect_state_root: unresolved
    backend = (_ENV_VARS["FOLDER_LOCK_STATE_BACKEND"] or "file").strip().lower()
    offline = (_ENV_VARS["FOLDER_LOCK_STATE_OFFLINE"] or "") not in ("", "0", "false")
    values = {
        "ROOT": root, "LOCK_TREE": lock_tree,
        "ROOT_LOCK_DIR": lock_tree / ".goal", "STATE": lock_tree / ".goal",
        "LEGACY_SESSIONS": lock_tree / ".goal" / "sessions",
        "LEGACY_INBOX_INDEX": lock_tree / ".goal" / "inboxes.txt",
        "REGISTRY": root / ".folder-lock" / "registry.yaml",
        "STATE_ROOT": state_root, "SESSIONS": state_root / "sessions",
        "CACHE": state_root / "cache", "OUTBOX": state_root / "outbox",
        "FILESTORE": state_root / "store", "BACKEND": backend, "OFFLINE": offline,
        "CACHE_JSON": state_root / "cache" / "board.json",
    }
    snap = []
    for name in ("lockpath", "statestore", "items", "autorun_log", "boardmat"):
        mod = sys.modules.get(name)
        if mod is None:
            continue
        for attr, val in values.items():
            if hasattr(mod, attr):
                snap.append((mod, attr, getattr(mod, attr)))
                setattr(mod, attr, val)
    return snap


def _unpin(snap) -> None:
    for mod, attr, old in reversed(snap):
        setattr(mod, attr, old)


def _setup():
    """Import the lib modules under this fixture's env, pin their frozen roots, seed the store, and hand the
    tests their globals. Returns the snapshot _teardown() restores."""
    _apply_env()
    import items  # noqa: E402
    import lockpath as lp  # noqa: E402
    import statestore  # noqa: E402
    import boardmat  # noqa: E402
    globals().update(items=items, lp=lp, statestore=statestore, boardmat=boardmat)
    snap = _pin()
    _seed()
    globals()["ASK_KEY"] = items.key_of(ASKING, "handoff", NOTE)
    globals()["ASK_HANDLE"] = _handle(globals()["ASK_KEY"])
    return snap


def _teardown(snap) -> None:
    _unpin(snap)
    _restore_env()


@pytest.fixture(autouse=True, scope="module")
def _fixture_roots():
    snap = _setup()
    yield
    _teardown(snap)


# ---------------------------------------------------------------- bm-1

def test_bm1_materialize_writes_the_board_with_inline_context_and_anchors():
    _ask(ASK_KEY)
    b = boardmat.materialize("signoff", actor="test-window")
    assert b and b["trigger"] == "signoff" and b["window_or_agent_id"] == "test-window"
    stored = _board()
    assert stored["schema_version"] == boardmat.SCHEMA_VERSION
    assert stored["counts"]["waiting_owner"] == 1
    assert stored["counts"]["ready"] >= 1
    assert stored["counts"]["locks"] == 0
    assert stored["counts"]["filed"] == 0
    row = stored["waiting_owner"][0]
    assert row["handle"] == ASK_HANDLE and row["item_key"] == ASK_KEY and row["owner"] == ASKING
    assert row["question"] == ASK_Q
    assert any("Next concrete action:" in ln for ln in row["pointer_tail"]), row["pointer_tail"]
    assert any("asked the owner" in ln for ln in row["autorun_excerpt"]), row["autorun_excerpt"]
    md = _md()
    assert "<!-- answer: " + ASK_HANDLE + " -->" in md
    assert ASK_Q in md
    assert "↳ pointer:" in md and "↳ autorun:" in md
    assert "answer <handle>" in md
    # the stored md IS the object the glance prints - one GET, no second render
    assert stored["md"] == md
    assert boardmat.RENDER_NOTE not in md, "the kick note must never be baked into the materialized md"


def test_bm1_handles_are_deterministic_and_anchor_safe():
    assert _handle(ASK_KEY) == ASK_HANDLE                      # stable across calls
    assert _handle(ASK_KEY) == _handle(ASK_KEY)
    assert boardmat.ANCHOR_RE.match("<!-- answer: " + ASK_HANDLE + " -->")
    assert boardmat.ANCHOR_RE.match("<!--answer:" + ASK_HANDLE + "-->")
    assert not boardmat.ANCHOR_RE.match("<!-- answer: a b -->")
    data = items.load()
    assert items.resolve_handle(data, ASK_KEY) == ASK_KEY                        # by full key
    assert items.resolve_handle(data, ASK_HANDLE) == ASK_KEY                     # exact handle
    assert items.resolve_handle(data, ASK_HANDLE[:4]) == ASK_KEY                 # unique prefix
    assert items.resolve_handle(data, "definitely-not-a-handle") == ""
    assert items.resolve_handle(data, "") == ""


# ---------------------------------------------------------------- bm-2

def test_bm2_fresh_glance_budget(capsys):
    boardmat.materialize("signoff")
    gets, etags, saves, loads = [], [], [], []
    real_raw, real_etag = statestore.raw_get, statestore.doc_etag
    real_save, real_load = statestore.save, statestore.load

    def _raw(path):
        gets.append(path)
        return real_raw(path)

    def _etag(name):
        etags.append(name)
        return real_etag(name)

    def _save(name, obj):
        saves.append(name)
        return real_save(name, obj)

    def _load(name, default=None):
        loads.append(name)
        return real_load(name, default)

    def _boom(*a, **k):
        raise AssertionError("a fresh glance must not build a view")

    statestore.raw_get, statestore.doc_etag = _raw, _etag
    statestore.save, statestore.load = _save, _load
    import board as _board
    real_view, _board.build_view = _board.build_view, _boom
    try:
        rc = boardmat.glance(touch=True)
    finally:
        statestore.raw_get, statestore.doc_etag = real_raw, real_etag
        statestore.save, statestore.load = real_save, real_load
        _board.build_view = real_view
    out = capsys.readouterr().out
    assert rc == 0
    assert gets == [boardmat.BOARD_JSON], gets
    assert etags == ["items"], etags
    assert loads == [], loads                     # mark_seen writes; it does not read first
    assert saves == ["last_seen"], saves
    assert "waiting on the owner" in out and "<!-- answer: " + ASK_HANDLE + " -->" in out


def test_bm2_no_touch_writes_nothing(capsys):
    boardmat.materialize("signoff")
    saves = []
    real_save = statestore.save
    statestore.save = lambda name, obj: (saves.append(name), real_save(name, obj))[1]
    try:
        rc = boardmat.glance(touch=False, want_json=True)
    finally:
        statestore.save = real_save
    out = capsys.readouterr().out
    assert rc == 0 and saves == []
    assert json.loads(out)["counts"]["waiting_owner"] == 1


# ---------------------------------------------------------------- bm-3

def test_bm3_stale_board_falls_back_to_a_render(capsys):
    boardmat.materialize("signoff")
    assert boardmat.is_fresh(boardmat.fetch()[0]) == (True, "stamps match")
    WORK = items.key_of(WORKING, "handoff", "y.staged.md")
    items.set_status(WORK, "in_progress")               # the store moved under the board
    b, _etag = boardmat.fetch()
    fresh, why = boardmat.is_fresh(b)
    assert not fresh and "items moved" in why, why
    rc = boardmat.glance(touch=False)
    cap = capsys.readouterr()                       # ONE drain: a second readouterr() would see empty streams
    assert rc == 0 and "rendering now" in cap.err
    assert boardmat.is_fresh(boardmat.fetch()[0])[0], "the fallback did not refresh the board"
    items.set_status(WORK, "ready")


def test_bm3_lock_movement_never_touches_the_board():
    boardmat.materialize("signoff")
    before, _e = boardmat.fetch()
    assert boardmat.is_fresh(before)[0]
    lp.LOCK_TREE.mkdir(parents=True, exist_ok=True)
    lock = lp.LOCK_TREE / WORKING / ".goal" / "LOCK.yaml"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("holder: interactive" + N + 'window: "window-x-260920-aaaa"' + N + "status: open" + N
                    + 'task: "beta work"' + N + 'stream: "root"' + N + 'branch: "main"' + N
                    + 'started: "2026-09-20T01:00:00"' + N, encoding="utf-8")
    try:
        fresh, why = boardmat.is_fresh(before)
        assert not fresh and "locks moved" in why, why      # a lock is an IDENTITY fingerprint, and it moved
        fp1 = boardmat.lock_fingerprint(fresh=True)
        lock.write_text(lock.read_text(encoding="utf-8").replace("status: open", "status: closing"), encoding="utf-8")
        assert boardmat.lock_fingerprint(fresh=True) != fp1, "status is part of the identity"
    finally:
        lock.unlink()


# ---------------------------------------------------------------- bm-4

def test_bm4_idempotent_and_safe_under_concurrent_writers():
    def strip(d):
        return {k: v for k, v in d.items() if k not in ("generated_at", "md")}

    a = boardmat.materialize("signoff")
    c = boardmat.materialize("signoff")
    assert strip(a) == strip(c), "same state -> same board (only the mint differs)"
    seen, errors = [], []

    def _one(i):
        try:
            seen.append(boardmat.materialize("signoff", actor="w" + str(i)))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=_one, args=(i,)) for i in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(60)
    assert not errors, errors
    assert len(seen) == 6 and all(s for s in seen)
    b = _board()                                     # parseable, and its stamps match the store right now
    assert b["trigger"] == "signoff", b["trigger"]
    assert boardmat.is_fresh(b) == (True, "stamps match")


# ---------------------------------------------------------------- bm-6

def test_bm6_resurrected_deletion_lands_in_the_next_digest(monkeypatch):
    statestore.READONLY = False
    note = _note(_TREE / WORKING, "y.staged.md", "Do the beta thing")   # a consumed note that CAME BACK
    key = items.key_of(WORKING, "handoff", "y.staged.md")
    items.set_status(key, "done")
    data = items.load()
    data["items"][key]["outcome"] = "consumed by the fire"
    items.save(data)
    assert _doc()[key]["status"] == "done"
    monkeypatch.setattr(items, "_live_store", lambda: True)      # the deletion path, through the ONE predicate
    monkeypatch.setattr(items, "RESURRECTED_THIS_RUN", [])
    b = boardmat.materialize("handoff")
    assert b, "the board must be written"
    assert not note.exists(), "the resurrected note was deleted by items.sync"
    hits = [d for d in b["digest_since_last_seen"] if "resurrected" in (d.get("decisions") or "")]
    assert len(hits) == 1, [d.get("decisions") for d in b["digest_since_last_seen"]][-5:]
    assert "y.staged.md" in hits[0]["decisions"] and hits[0]["status"] == "done"
    assert items.RESURRECTED_THIS_RUN == [], "the list is consumed by the materialize that reported it"
    assert _doc()[key]["status"] == "done", "a resurrected note stays done"


# ---------------------------------------------------------------- bm-7

def test_bm7_answer_is_lockless_and_re_readies_the_item(capsys):
    _ask(ASK_KEY)
    boardmat.materialize("signoff")
    assert lp.locks_at(lp.LOCK_TREE / ASKING / ".goal") == [], "answer must never take a lock"
    rc = boardmat.answer(ASK_HANDLE, "go with option b")
    assert rc == 0
    e = _doc()[ASK_KEY]
    assert e["status"] == "ready" and e["answered_at"]
    assert e["answer"] == "go with option b" and e["answer_ref"].endswith(".staged.md")
    assert "blocked_since" not in e
    notes = sorted((lp.LOCK_TREE / ASKING / ".goal" / "inbox").glob("*-answer-*.staged.md"))
    assert len(notes) == 1, notes
    txt = notes[0].read_text(encoding="utf-8")
    assert "type: answer" in txt and "go with option b" in txt and ASK_Q in txt
    assert "waiting_owner" not in txt.split("---")[1]
    note_key = items.key_of(ASKING, "handoff", notes[0].name)
    assert _doc()[note_key]["status"] == "filed", "the answer note must be FILED, not ready"
    assert lp.locks_at(lp.LOCK_TREE / ASKING / ".goal") == [], "still no lock after the answer"
    b = boardmat.materialize("answer")
    assert b["counts"]["waiting_owner"] == 0
    assert _doc()[ASK_KEY]["status"] == "ready", "the answered item came back as ready"
    assert _doc()[ASK_KEY]["answered_at"], "the answered_at hold was dropped by the render"
    assert _doc()[note_key]["status"] == "filed", "the render re-readied a filed note"
    ready = {r["item_key"] for r in b["ready"]}
    assert ASK_KEY in ready and note_key not in ready


def test_bm7_refusals(capsys):
    _ask(ASK_KEY)
    boardmat.materialize("signoff")
    assert boardmat.answer(ASK_HANDLE, "go with option b") == 0
    assert boardmat.answer(ASK_HANDLE, "again") == 3, "an answer to a non-waiting item is refused"
    assert "not waiting_owner" in capsys.readouterr().err
    assert boardmat.answer("no-such-handle-at-all", "x") == 4
    assert "UNKNOWN" in capsys.readouterr().err
    assert boardmat.answer(ASK_HANDLE, "   ") == 2
    assert "needs text" in capsys.readouterr().err


def test_bm7_batch_parser_skips_empty_anchors(capsys):
    _ask(ASK_KEY)
    batch = ("# Answers" + N + N
             + "<!-- answer: " + ASK_HANDLE + " -->" + N + "go with option b." + N + N
             + "<!-- answer: nobody-at-all -->" + N + "orphan text" + N + N
             + "<!-- answer: " + ASK_HANDLE + "-empty -->" + N)
    pairs = boardmat.parse_batch(batch)
    assert [p[0] for p in pairs] == [ASK_HANDLE, "nobody-at-all", ASK_HANDLE + "-empty"]
    assert pairs[0][1] == "go with option b."
    assert pairs[1][1] == "orphan text"
    assert pairs[2][1] == "", "an anchor with no text owns nothing"
    _ask(ASK_KEY)                                        # the resolved pair needs an item actually waiting
    boardmat.materialize("signoff")
    p = lp.LOCK_TREE / "batch.md"
    p.write_text("<!-- answer: " + ASK_HANDLE + " -->" + N + "go with option b." + N
                 + "<!-- answer: " + ASK_HANDLE + "-empty -->" + N, encoding="utf-8")
    rc = boardmat.answer_batch(str(p))
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "1 landed" in out and "1 skipped" in out and "0 refused" in out, out
    assert "no text under its anchor" in out, out
    assert _doc()[ASK_KEY]["status"] == "ready"


# ---------------------------------------------------------------- bm-8

def test_bm8_env_fuse_and_live_store_predicate(monkeypatch):
    import statestore
    assert statestore._env_backend() == "file"
    monkeypatch.setattr(statestore, "BACKEND", "blob")          # a fixture patching the ATTRIBUTE ...
    try:
        statestore._container()
        raise AssertionError("a patched BACKEND attribute reached the store")
    except statestore.Unavailable as e:
        assert "non-blob backend" in str(e)
    assert items._live_store() is False, "a patched BACKEND must never look live"
    monkeypatch.setattr(statestore, "BACKEND", "file")
    assert items._live_store() is True
    assert statestore._container is not None                    # the env fuse is the only door to a blob container


def test_bm8_destructive_path_refuses_a_local_note(monkeypatch):
    statestore.READONLY = False                   # a render sets it; `_resurrected` leaves the file alone under it
    key = items.key_of(WORKING, "handoff", "y.staged.md")
    items.set_status(key, "done")
    note = _note(_TREE / WORKING, "y.staged.md", "Do the beta thing")   # own the fixture
    assert note.is_file()
    monkeypatch.setattr(items, "RESURRECTED_THIS_RUN", [])
    monkeypatch.setattr(items, "_live_store", lambda: False)
    items._resurrected(WORKING, "y.staged.md", _doc()[key], items.mint.timestamp())
    assert note.is_file(), "a non-live store must never delete the note"
    assert items.RESURRECTED_THIS_RUN == [], "nothing was deleted -> nothing lands in a digest"
    monkeypatch.setattr(items, "_live_store", lambda: True)
    items._resurrected(WORKING, "y.staged.md", _doc()[key], items.mint.timestamp())
    assert not note.is_file(), "the live store's own note is deleted"
    assert [d["ref"] for d in items.RESURRECTED_THIS_RUN] == ["y.staged.md"]


# ---------------------------------------------------------------- runner

class _MP:
    """A stand-in for pytest's `monkeypatch`, so the plain runner exercises the same tests."""
    def __init__(self):
        self._undo = []

    def setattr(self, obj, name, val):
        self._undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, val)

    def undo(self):
        for obj, name, old in reversed(self._undo):
            setattr(obj, name, old)
        self._undo = []


class _Cap:
    """A stand-in for pytest's `capsys` (real redirection, so the assertions mean something)."""
    def __init__(self, out, err):
        self.out, self.err = out, err

    def readouterr(self):
        o, e = self.out.getvalue(), self.err.getvalue()
        self.out.seek(0); self.out.truncate(0)
        self.err.seek(0); self.err.truncate(0)
        return type("R", (), {"out": o, "err": e})()


def _run() -> int:
    import contextlib
    import inspect
    import io
    import traceback
    snap = _setup()
    try:
        tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
        bad = 0
        for name, fn in tests:
            out, err = io.StringIO(), io.StringIO()
            mp, cap = _MP(), _Cap(out, err)
            params = inspect.signature(fn).parameters
            kwargs = {p: (mp if p == "monkeypatch" else cap) for p in params}
            try:
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    fn(**kwargs)
                print("PASS  " + name)
            except Exception:  # noqa: BLE001
                bad += 1
                print("FAIL  " + name)
                traceback.print_exc()
            finally:
                mp.undo()
        print(str(len(tests) - bad) + "/" + str(len(tests)) + (" ALL PASS" if not bad else " FAILED"))
        return 1 if bad else 0
    finally:
        _teardown(snap)


if __name__ == "__main__":
    sys.exit(_run())
