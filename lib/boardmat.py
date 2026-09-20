r"""boardmat.py — the MATERIALIZED board: written when state changes, read when the owner looks (v5.2).

Why: a bare board render used to be a multi-step live derivation — a full tree walk, `items.sync`, a state-store
read, the digest, the signpost. Seconds in a fresh window, sometimes a failure mid-render. The board is a pure
VIEW over the state documents and the lock tree, so it is materialized by every WRITER of that state and a
`glance` only reads it.

The shape (the generic half of the 2026-09-19 rulings):

  1. Home of the materialized board = the state store, objects `board/board.json` + `board/board.md`, written
     through `statestore.raw_put` (no merge, no outbox — a materialized view is derived, last writer wins).
  2. `source_stamps` inside the object say which `items` etag and which LOCK IDENTITY it was derived from; a
     glance compares them and falls back to a live render when they moved. For locks the stamp is a fingerprint
     of the lock RECORDS (folder, kind, window, status) — never a lock file's mtime or etag: a firing lock is
     heart-beaten every minute, so a time-based stamp would leave the board stale for as long as any agent runs.
  3. A glance runs no entry guards and takes no lock; it binds at most a READER identity, and its ONE state
     write is the touch (the digest window). Entry guards live in the claim verb (`lock.py claim`).
  4. Answering a waiting_owner item is LOCKLESS: `board.py answer` files a handoff of type `answer` into the
     item's folder, re-readies the asking item (`items.answer`), and materializes.
  5. A render never kicks the loop and never claims a folder.
  6. Materialize failures never block the caller — one stderr line and the caller goes on.

API:
  materialize(trigger, v=None, actor="")   derive (board.build_view + items.sync — nothing is re-implemented),
                                           write board.json + board.md, cache locally, guard-log
  glance(touch=True, want_json=False)      GET board.json; stamps fresh -> print the md verbatim; stale ->
                                           materialize (`render_fallback`) and print; ONE state-store write (touch)
  answer(handle, text) / answer_batch(path)   the lockless answer path
"""
from __future__ import annotations

import functools
import json
import os
import re
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lockpath as lp  # noqa: E402
import mint  # noqa: E402

BOARD_JSON = "board/board.json"
BOARD_MD = "board/board.md"
SCHEMA_VERSION = 1
TRIGGERS = ("signoff", "claim", "handoff", "answer", "release", "new", "complete", "rename", "autorun",
            "render_fallback")
# what a materialized board says about kicking: it never does (`board.py menu` is the manual kick)
RENDER_NOTE = "a render never kicks the loop (§13; `python scripts/board.py menu` kicks it by hand)"
ANCHOR_RE = re.compile(r"^\s*<!--\s*answer:\s*([A-Za-z0-9._~-]+)\s*-->\s*$")
CACHE_JSON = lp.STATE_ROOT / "cache" / "board.json"
HOST = (os.environ.get("COMPUTERNAME") or socket.gethostname() or "host")
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".next", "dist", "build", ".githooks", "_archive"}


def _say(msg: str) -> None:
    print(f"board: {msg}", file=sys.stderr)


def _actor(explicit: str = "") -> str:
    if explicit:
        return explicit
    try:
        me = lp.identity()
        if me:
            return me.window
    except Exception:  # noqa: BLE001
        pass
    return (os.environ.get("FOLDER_LOCK_WINDOW") or "").strip()


ABSENT = "absent"          # a document that does not exist yet has no etag — a stable stamp instead of None


def _safe_etag(fn, *a):
    try:
        v = fn(*a)
        return v if v else ABSENT
    except Exception as e:  # noqa: BLE001
        return f"unavailable:{type(e).__name__}"


# ----------------------------------------------------------------------------- freshness stamps

_FP_MEMO: dict = {}


def lock_rows(root=None, fresh: bool = False) -> list:
    """[{folder, kind, window, status, fresh}] for every lock in the lock tree — ONE pruned walk, memoized per
    process (a glance asks twice: once for the stamps, once to check them). The twin keeps its locks as files in
    the lock tree (`<folder>/.goal/LOCK.yaml`, `<folder>/.goal/.firing.lock`); the board shows only who holds
    which folder in which state, which is exactly what this list carries."""
    root = Path(root or lp.LOCK_TREE)
    key = str(root)
    if not fresh and key in _FP_MEMO:
        return _FP_MEMO[key]
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        d = Path(dirpath)
        if d.name != ".goal":
            continue
        dirnames[:] = []
        for name, kind in (("LOCK.yaml", "interactive"), (".firing.lock", "fired")):
            if name not in filenames:
                continue
            try:
                li = lp.read_lock(d / name, kind)
            except Exception:  # noqa: BLE001 — an unreadable lock is a lock the board cannot show
                continue
            out.append({"folder": d.parent.relative_to(root).as_posix() or ".", "kind": kind,
                        "window": li.window, "status": li.status, "fresh": li.fresh})
    out.sort(key=lambda r: (r["folder"], r["kind"]))
    _FP_MEMO[key] = out
    return out


def lock_fingerprint(rows: list | None = None, fresh: bool = False) -> str:
    """The lock store's IDENTITY stamp: sha1 over (folder, kind, window, status) of every record.

    Not a file etag or mtime — a firing lock is heart-beaten every minute, so anything time-based would leave the
    board stale for as long as any agent runs (i.e. always). What the board shows about a lock is who holds which
    folder in which state; that is what is stamped."""
    import hashlib
    try:
        rows = lock_rows(fresh=fresh) if rows is None else rows
    except Exception as e:  # noqa: BLE001
        return f"unavailable:{type(e).__name__}"
    parts = [f"{r['folder']}|{r['kind']}|{r.get('window', '')}|{r.get('status', '')}" for r in rows]
    return "locks:" + hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:20]


# ----------------------------------------------------------------------------- derivation helpers

def pointer_tail(folder: str, n: int = 2) -> list:
    """The last `n` LIVE lines of the folder's pointer — non-empty, not a `> `-folded history line, not a fence
    marker. What a triage needs to see without opening the folder."""
    p = lp.ROOT / folder / "workflow-state" / "current-pointer.md"
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out, fence = [], False
    for raw in text.splitlines():
        s = raw.strip()
        if s.startswith("```"):
            fence = not fence
            continue
        if fence or not s or s.startswith(">") or s.startswith("#"):
            continue
        out.append(" ".join(s.split())[:240])
    return out[-n:]


def autorun_excerpt(folder: str, n: int = 5) -> list:
    try:
        import autorun_log
        return [" ".join(l.split())[:240] for l in autorun_log.lines(folder)[-n:]]
    except Exception:  # noqa: BLE001
        return []


def enrich_waiting(v: dict) -> None:
    """Inline context under every waiting_owner row (in place): the handle the answer anchors key on, the last
    live pointer lines, the last autorun-log lines — so the paste can be triaged without opening a folder."""
    import items as _items
    for owner, lst in ((v.get("items") or {}).get("waiting_owner") or {}).items():
        for r in lst:
            if "pointer_tail" in r:
                continue
            key = r.get("key") or ""
            folder = key.split("|", 1)[0] or owner
            r["folder"] = folder
            r["handle"] = r.get("handle") or _items.handle(key)
            r["pointer_tail"] = pointer_tail(folder)
            r["autorun_excerpt"] = autorun_excerpt(owner)


def build_board(v: dict, trigger: str, stamps: dict, actor: str, md: str) -> dict:
    """board.json from a `board.build_view()` structure — the machine view AND the human text in one object
    (one GET on the glance path)."""
    import items as _items
    live = (v.get("items") or {}).get("live") or {}
    locks_by_folder: dict = {}
    for lk in v.get("locks") or []:
        locks_by_folder.setdefault(lk.get("folder", ""), []).append(lk)
    waiting = []
    for owner, lst in ((v.get("items") or {}).get("waiting_owner") or {}).items():
        for r in lst:
            waiting.append({"item_key": r.get("key", ""), "handle": r.get("handle", "") or _items.handle(r.get("key", "")),
                            "owner": owner, "folder": r.get("folder", ""), "kind": r.get("kind", ""),
                            "title": r.get("title", ""), "question": r.get("question", ""),
                            "auto_drafted": bool(r.get("question_auto")), "blocked_since": r.get("blocked_since", ""),
                            "origin": r.get("origin", ""), "pointer_tail": r.get("pointer_tail", []),
                            "autorun_excerpt": r.get("autorun_excerpt", [])})
    in_progress = []
    for k, e in live.items():
        if e.get("status") != "in_progress":
            continue
        folder = k.split("|", 1)[0]
        lks = locks_by_folder.get(folder) or []
        in_progress.append({"item_key": k, "handle": _items.handle(k), "folder": folder, "owner": e.get("owner", ""),
                            "holder": (lks[0].get("window", "") if lks else ""), "locks": len(lks),
                            "title": e.get("title", ""), "since": e.get("updated", "")})
    ready = [dict(key=k, folder=k.split("|", 1)[0], **e) for k, e in live.items() if e.get("status") == "ready"]
    ready.sort(key=lambda e: (0 if e.get("kind") == "pointer" else 1, e.get("created", "")))
    ready_by_folder: dict = {}
    for r in ready:
        ready_by_folder[r["folder"]] = ready_by_folder.get(r["folder"], 0) + 1
    digest = list(v.get("digest") or [])
    for d in list(getattr(_items, "RESURRECTED_THIS_RUN", [])):
        digest.append({"ts": d.get("ts", ""), "folder": d.get("folder", ""), "item": d.get("title", d.get("ref", "")),
                       "status": "done", "decisions":
                           f"resurrected staged note deleted ({d.get('ref', '')}) — already {d.get('outcome', 'done')}"})
    slim = ("kind", "status", "ref", "title", "owner", "question", "question_auto", "blocked_since", "origin",
            "created", "updated", "fails", "stuck", "parked", "answered_at", "answer_ref")

    def _slim(k: str, e: dict) -> dict:
        d = {f: e.get(f) for f in slim if f in e}
        d["item_key"] = k
        d["folder"] = k.split("|", 1)[0]
        d["handle"] = _items.handle(k)
        lks = locks_by_folder.get(d["folder"]) or []
        d["locked"] = bool(lks)
        if lks:
            d["locked_by"] = {"window": lks[0].get("window", ""), "kind": lks[0].get("kind", ""),
                              "status": lks[0].get("status", "")}
        return d

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": mint.timestamp(),
        "trigger": trigger if trigger in TRIGGERS else "render_fallback",
        "host": HOST,
        "window_or_agent_id": actor,
        "source_stamps": stamps,
        "counts": {
            "ready": len(ready),
            "ready_unlocked": sum(1 for r in ready if not (locks_by_folder.get(r["folder"]) or [])),
            "waiting_owner": len(waiting),
            "waiting_world": sum(1 for e in live.values() if e.get("status") == "waiting_world"),
            "in_progress": len(in_progress),
            "parked": sum(1 for e in live.values() if e.get("status") == "parked"),
            "filed": sum(1 for e in live.values() if e.get("status") == "filed"),
            "locks": len(v.get("locks") or []),
            "deploys": len([d for d in (v.get("deploys") or []) if d.get("pending") or d.get("failed") or d.get("blocked")]),
            "digest": len(digest),
        },
        "waiting_owner": waiting,
        "in_progress": in_progress,
        "deploys": [d for d in (v.get("deploys") or []) if d.get("pending") or d.get("failed") or d.get("blocked")],
        "digest_since_last_seen": digest,
        "ready_by_folder": dict(sorted(ready_by_folder.items())),
        "ready": [_slim(r["key"], r) for r in ready],
        "open": [_slim(k, e) for k, e in sorted(live.items())],
        "locks": [{f: lk.get(f, "") for f in ("folder", "kind", "age", "window", "status", "holder_label")}
                  for lk in (v.get("locks") or [])],
        "touch": {"tick": [r.get("title", "") for r in ready], "reported": [d.get("item", "") for d in digest]},
        "md": md,
    }


# ----------------------------------------------------------------------------- materialize

def _last_seen_ts() -> str:
    """The digest window (`last_seen`) as a stamp — the third thing a board is derived from. Read-only."""
    import statestore as ss
    was = ss.READONLY
    ss.READONLY = True
    try:
        return str((ss.load("last_seen", {}) or {}).get("ts") or "")
    finally:
        ss.READONLY = was


def _state_write(fn):
    """A materialize / answer is a STATE WRITE from start to finish — and `statestore.save` (like `raw_put`) is a
    silent no-op while `READONLY` is on, which `scripts/board.py` sets for every RENDER. Without this the answer would
    flip the item in memory, then the board would re-read the un-flipped document from the store and quietly undo it."""
    @functools.wraps(fn)
    def _w(*a, **k):
        import statestore as ss
        was = ss.READONLY
        ss.READONLY = False
        try:
            return fn(*a, **k)
        finally:
            ss.READONLY = was
    return _w


@_state_write
def materialize(trigger: str = "render_fallback", v: dict | None = None, actor: str = "", quiet: bool = True) -> dict:
    """Derive + write the board. Never raises: a failure is one stderr line and the caller goes on (ruling 6).
    `v` = a `board.build_view()` the caller already has — else one is built now. Returns the board dict ({} when
    even the derivation failed)."""
    import statestore as ss
    import board as _board
    import items as _items
    # BEFORE the render, and NEVER from the memo: a fingerprint read out of the process cache (a lock created and
    # removed by an earlier call in THIS process) would be contradicted by the very next `is_fresh` -> the board
    # would be born stale. The walk is a pruned tree listing, and a lock that moves DURING the render makes the
    # board stale rather than falsely fresh either way.
    stamps = {"locks": lock_fingerprint(fresh=True)}
    try:
        if v is None:
            v = _board.build_view()
        enrich_waiting(v)
    except Exception as e:  # noqa: BLE001
        _say(f"materialize ({trigger}) skipped — derivation failed: {type(e).__name__}: {e}")
        return {}
    # `items` AFTER the render: `items.sync` inside build_view may have saved (its own etag is the truth the glance compares)
    stamps["items"] = ss.loaded_etag("items") or _safe_etag(ss.doc_etag, "items")
    stamps["last_seen"] = _safe_etag(_last_seen_ts)
    act = _actor(actor)
    md = _board.render_menu(v, kick_note=RENDER_NOTE)
    b = build_board(v, trigger, stamps, act, md)
    raw = json.dumps(b, ensure_ascii=False, indent=1, default=str).encode("utf-8")
    was_readonly = ss.READONLY          # raw_put is a SILENT no-op under READONLY; a materialize is a state write
    ss.READONLY = False
    try:
        ss.raw_put(BOARD_JSON, raw, "application/json; charset=utf-8")
        ss.raw_put(BOARD_MD, md.encode("utf-8"), "text/markdown; charset=utf-8")
        wrote = True
    except Exception as e:  # noqa: BLE001
        wrote = False
        _say(f"materialize ({trigger}): board not written to the store ({type(e).__name__}: {e}) — local cache only")
    finally:
        ss.READONLY = was_readonly
    try:
        CACHE_JSON.parent.mkdir(parents=True, exist_ok=True)
        CACHE_JSON.write_bytes(raw)
    except Exception:  # noqa: BLE001
        pass
    _items.RESURRECTED_THIS_RUN[:] = []
    try:
        lp.guard_log({"guard": "board", "event": "materialize", "trigger": b["trigger"], "window": act, "wrote": wrote,
                      "ready": b["counts"]["ready"], "waiting_owner": b["counts"]["waiting_owner"], "stamps": stamps})
    except Exception:  # noqa: BLE001
        pass
    if not quiet:
        print(f"materialized ({b['trigger']}) -> {BOARD_JSON} + {BOARD_MD}" + ("" if wrote else " [store unreachable - cache only]")
              + f" · ready {b['counts']['ready']} ({b['counts']['ready_unlocked']} unlocked) · waiting on the owner {b['counts']['waiting_owner']}"
              + f" · stamps items={str(stamps.get('items'))[:14]} locks={str(stamps.get('locks'))[:14]}")
    return b


def refresh(trigger: str) -> None:
    """Best-effort materialize for a STATE WRITER (`claim`, `handoff`, `signoff`, `new`, `release`) — the board is
    rebuilt where the state moved, never as a side effect of someone looking at it. Never raises, never prints
    unless it fails: a board write must never break the write that triggered it (ruling 6)."""
    try:
        materialize(trigger if trigger in TRIGGERS else "render_fallback", quiet=True)
    except Exception as e:  # noqa: BLE001
        _say(f"board refresh ({trigger}) skipped: {type(e).__name__}: {e}")


# ----------------------------------------------------------------------------- glance

def fetch() -> tuple:
    """(board dict | None, etag | None) — ONE GET. Never raises (None on an unreachable store)."""
    import statestore as ss
    try:
        raw, etag = ss.raw_get(BOARD_JSON)
    except Exception as e:  # noqa: BLE001
        _say(f"board.json not readable ({type(e).__name__}: {e})")
        return None, None
    if raw is None:
        return None, None
    try:
        return json.loads(raw.decode("utf-8")), etag
    except (ValueError, UnicodeDecodeError) as e:
        _say(f"board.json unreadable ({e})")
        return None, None


def is_fresh(b: dict | None) -> tuple:
    """(fresh, why): the board's `source_stamps` against the CURRENT stamps of `items` and the lock tree.
    The lock walk is re-done here (`fresh=True`): a memoized walk is what a materialize wants, and a freshness check that
    reused it would be answering its own question."""
    if not b or not isinstance(b.get("source_stamps"), dict):
        return False, "no board in the store"
    import statestore as ss
    st = b["source_stamps"]
    for doc in ("items", "locks"):
        want = st.get(doc)
        if not want or str(want).startswith("unavailable:"):
            return False, f"{doc}: stamp missing"
        try:
            cur = lock_fingerprint(fresh=True) if doc == "locks" else (ss.doc_etag(doc) or ABSENT)
        except Exception as e:  # noqa: BLE001
            return False, f"{doc}: etag unavailable ({type(e).__name__})"
        if cur != want:
            return False, f"{doc} moved ({str(want)[:14]} -> {str(cur)[:14]})"
    return True, "stamps match"


def _touch(b: dict) -> bool:
    """The owner looked: close the digest window in ONE state-store write."""
    try:
        import autorun_log as alog
        import statestore as ss
        was = ss.READONLY
        ss.READONLY = False
        try:
            alog.mark_seen()
        finally:
            ss.READONLY = was
        return True
    except Exception as e:  # noqa: BLE001
        _say(f"touch skipped ({type(e).__name__}: {e})")
        return False


@_state_write
def glance(touch: bool = True, want_json: bool = False) -> int:
    """The glance: prints board.md (or board.json) — fresh from the store, else a fallback render.

    Budget of the fresh path: ONE raw GET (board.json), the stamp checks (`items` etag + the lock fingerprint, both
    local), and ONE state-store write (the touch). No build_view, no signpost, no subprocess."""
    import statestore as ss
    window = ""
    if touch:
        try:
            import board as _board
            window = _board._reader_identity()      # a reader binding: the Stop hook needs an identity; NO lock, NO folder
        except Exception:  # noqa: BLE001
            window = ""
    b, _etag = fetch()
    fresh, why = is_fresh(b)
    was_readonly = ss.READONLY
    ss.READONLY = False                             # the touch and the fallback materialize write; a glance is not a render
    try:
        if not fresh:
            _say(f"board stale or missing ({why}) — rendering now")
            b = materialize("render_fallback", actor=window) or b or {}
        text = json.dumps({k: v for k, v in b.items() if k != "md"}, ensure_ascii=False, indent=1, default=str) if want_json \
            else str(b.get("md") or "(no board — the store is unreachable and nothing is cached)")
        print(text)
        touched = _touch(b) if (touch and b) else False
    finally:
        ss.READONLY = was_readonly
    try:
        lp.guard_log({"guard": "board", "event": "glance", "fresh": fresh, "why": why, "window": window,
                      "touch": touched, "generated_at": b.get("generated_at", ""), "trigger": b.get("trigger", "")})
    except Exception:  # noqa: BLE001
        pass
    return 0


# ----------------------------------------------------------------------------- answer (lockless)

def _resolve(name: str) -> tuple:
    """(item_key, entry, handle) — by full item key, by handle, or by a unique handle prefix."""
    import items as _items
    data = _items.load()
    key = _items.resolve_handle(data, name)
    if not key:
        return None, None, ""
    return key, data["items"].get(key), _items.handle(key)


@_state_write
def answer(name: str, text: str, materialize_after: bool = True) -> int:
    """`board.py answer <handle> "<text>"` — LOCKLESS. Exit 0 landed · 2 no text · 3 not waiting · 4 unknown ·
    5 the answer note could not be staged."""
    import items as _items
    text = " ".join((text or "").split())
    if not text:
        print("ERROR: an answer needs text", file=sys.stderr)
        return 2
    key, e, handle = _resolve(name)
    if not key or e is None:
        print(f"UNKNOWN: no board item answers to {name!r} (item key, handle or unique handle prefix)", file=sys.stderr)
        return 4
    if e.get("status") != "waiting_owner":
        print(f"REFUSED: `{handle}` ({key}) is {e.get('status') or 'unknown'}, not waiting_owner — "
              f"{str(e.get('title') or '')[:120]}", file=sys.stderr)
        return 3
    owner = str(e.get("owner") or key.split("|", 1)[0])
    question = str(e.get("question") or "")
    body = (f"# Answer from the owner — `{handle}`\n\n"
            f"**Question:** {question or '(the item carried no question text)'}\n\n"
            f"**Answer:** {text}\n\nItem: `{key}` · asked since {e.get('blocked_since', '?')} · answered {mint.timestamp()}.\n\n"
            f"This note is filed (type `answer`): the folder's next fire reads it and acts on the ruling; the asking "
            f"item is `ready` again (`items.answer`). Consuming it means rewriting the pointer past the `WHEN ...` line.")
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8", newline="\n") as fh:
        fh.write(body)
        tmp = fh.name
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import handoff
    rc = handoff.main(["--to", owner, "--task", f"Answer: {handle} — {text[:110]}", "--from", "owner",
                       "--type", "answer", "--body", tmp, "--force"])
    try:
        os.unlink(tmp)
    except OSError:
        pass
    if rc != 0:
        print(f"REFUSED: the answer note could not be staged into {owner} (handoff.py rc={rc}) — nothing re-readied", file=sys.stderr)
        return 5
    note_ref = handoff.LAST_NOTE.name if handoff.LAST_NOTE else ""
    try:
        _items.answer(key, note_ref, text, by=_actor())
    except _items.NotWaiting as ex:
        print(f"REFUSED: {ex}", file=sys.stderr)
        return 3
    print(f"ANSWERED `{handle}` -> {owner} · item {key} waiting_owner -> ready · note {note_ref}")
    if materialize_after:
        materialize("answer")
    return 0


def parse_batch(text: str) -> list:
    """The `board.md` shape as pasted into a chat and annotated: answer text written UNDER each
    `<!-- answer: <handle> -->` anchor, until the next anchor, the next item header, a `Q:`/`↳` context line or a
    fence. Returns [(handle, text)] — anchors with no text are omitted (skipped by the caller)."""
    out, cur, buf = [], None, []

    def _flush():
        if cur is not None:
            t = " ".join(" ".join(buf).split())
            out.append((cur, t))

    for raw in text.splitlines():
        m = ANCHOR_RE.match(raw)
        if m:
            _flush()
            cur, buf = m.group(1), []
            continue
        if cur is None:
            continue
        s = raw.strip()
        if s.startswith("```") or s.startswith(("Q:", "↳", "⏸", "✓", "🔒", "🚀", "⚠", "◦", "▸", "twin (")):
            _flush()
            cur, buf = None, []
            continue
        if s:
            buf.append(s)
    _flush()
    return out


@_state_write
def answer_batch(path: str) -> int:
    text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    pairs = parse_batch(text)
    if not pairs:
        print("batch: no `<!-- answer: <handle> -->` anchor found in the file")
        return 2
    landed = skipped = failed = 0
    for name, t in pairs:
        if not t:
            print(f"skip  {name}: no text under its anchor")
            skipped += 1
            continue
        rc = answer(name, t, materialize_after=False)
        if rc == 0:
            landed += 1
        else:
            failed += 1
    if landed:
        materialize("answer")
    print(f"batch: {landed} landed · {skipped} skipped · {failed} refused")
    return 0 if not failed else 1
