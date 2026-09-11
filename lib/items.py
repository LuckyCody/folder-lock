"""Board items — the status overlay that turns the folder-lock board into a work queue (PROTOCOL §12–§13, v4).

The board derives items from files: each workfolder's `workflow-state/current-pointer.md` (one item per
folder), staged handoffs in `<folder>/.goal/inbox/`. Those files stay the source of the WORK; this module
adds the fields the autorun loop needs, in the state-store document `items` (lib/statestore.py, PROTOCOL §14 —
conditional writes, per-record merge on a lost race, cache + outbox when the store is unreachable):

    status        ready | in_progress | waiting_owner | done   (+ waiting_world for tripwires that wait on
                                                                something that is not the owner)
    question      required when waiting_owner: the concrete question, 2-3 options, the agent's recommendation
    blocked_since minted timestamp when the item entered waiting_owner
    created_by    workfolder that created the item
    owner         workfolder that must execute it — the LOCK HOME of the target path (lockpath.resolve), by path only

Key = "<folder>|<kind>|<ref>". Dedup: no open item may be created with the same owner + normalized title
(`Duplicate`). The blocker definition that decides waiting_owner is a TEMPLATE (templates/blockers.md);
each installation fills in its own list — the code only enforces that a waiting_owner item has a question.

    python lib/items.py list [--status S] [--owner F] [--json]
    python lib/items.py show <key>
    python lib/items.py upsert --folder F --kind pointer|handoff --ref R --title T [--created-by F] [--status S] [--question Q] [--force]
    python lib/items.py ready|in-progress|done <key>
    python lib/items.py wait <key> --question "<Q. Options: a/b/c. Recommendation: x>"
    python lib/items.py from-pointer <folder> [--created-by F]
    python lib/items.py check-dup --owner F --title T        # exit 4 on a duplicate

v4.3: `timed_due(condition)` — `WHEN <YYYY-MM-DD[ HH:MM]> [Berlin] has passed -> <action>` is waiting_world until the
instant and ready from then on (never early, never re-armed); a resurrected `done` handoff file is deleted + logged once.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mint  # noqa: E402
import lockpath as lp  # noqa: E402

ROOT = lp.ROOT
STATUSES = ("ready", "in_progress", "waiting_owner", "done", "waiting_world", "parked")
OWNER_RE = re.compile(r"\b(owner|ruling|rules?\s+on|approv\w*|decision|decides?|confirm\w*|says)\b", re.I)
_ARROW = re.compile(r"\s*(?:→|->)\s*")
# TIMED tripwire condition (v4.3): an ISO instant (date, optional HH:MM, optional zone word) followed by a "has passed"
# cue, or the condition IS just that instant (`WHEN 2026-09-12 14:15 Berlin → …`). The zone word is documentary — the
# clock is Europe/Berlin (PROTOCOL §3); set FOLDER_LOCK_TZ to another IANA zone for an installation elsewhere.
_ISO = r"(?P<date>\d{4}-\d{2}-\d{2})(?:[T ](?P<hh>\d{1,2}):(?P<mm>\d{2}))?"
_ZONE = r"(?:\s*\(?(?:Europe/)?(?:Berlin|CES?T|MES?Z|UTC(?:[+-]\d{1,2}(?::?\d{2})?)?)\)?)?"
_CUE = r"(?:has\s+passed|is\s+(?:past|over|reached)|passed|reached|elapsed|arrives?|ist\s+(?:vorbei|erreicht|abgelaufen)|vorbei|erreicht|abgelaufen)"
_INSTANT_RE = re.compile(_ISO + _ZONE + r"(?P<cue>\s*(?:" + _CUE + r"))?", re.I)
_LEAD_RE = re.compile(r"^\W*(?:(?:the\s+)?(?:time|date|clock|now|it)\s+(?:is\s+)?(?:>=|≥|past|after|at\s+or\s+after)?\s*)?", re.I)


def timed_due(condition: str):
    """The instant a TIMED tripwire waits for (tz-aware), or None when the condition is not timed.
    Timed = the instant is followed by a 'has passed' cue, OR the whole condition is just the instant (+ lead-in).
    A date inside prose ("X replies to the 2026-09-10 16:08 request") is NOT a timer.
    Date without a time = 00:00 of that day (the date 'has passed' once it starts)."""
    cond = condition or ""
    import datetime as _dt
    for m in _INSTANT_RE.finditer(cond):
        whole = _LEAD_RE.sub("", cond[:m.start()]).strip() == "" and cond[m.end():].strip(" .!?;,") == ""
        if not (m.group("cue") or whole):
            continue
        try:
            d = _dt.date.fromisoformat(m.group("date"))
            hh, mm = int(m.group("hh") or 0), int(m.group("mm") or 0)
            if not (0 <= hh < 24 and 0 <= mm < 60):
                return None
            return _dt.datetime.combine(d, _dt.time(hh, mm), tzinfo=_tz())
        except ValueError:
            return None
    return None


def _tz():
    import os as _os
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(_os.environ.get("FOLDER_LOCK_TZ") or "Europe/Berlin")
    except Exception:                                # no tzdata on this host: the local clock is the installation's clock
        import datetime as _dt
        return _dt.datetime.now().astimezone().tzinfo


def now_tz():
    import datetime as _dt
    return _dt.datetime.now(_tz())


class Duplicate(Exception):
    def __init__(self, key: str, title: str):
        super().__init__(f"duplicate of open item {key!r} ({title})")
        self.key, self.title = key, title


def load() -> dict:
    """Document `items` from the state store (PROTOCOL §14: conditional writes, cache when offline)."""
    import statestore
    data = statestore.load("items", {"items": {}})
    if not isinstance(data, dict):
        data = {}
    data.setdefault("items", {})
    return data


def save(data: dict) -> None:
    """Conditional write; 3-way per-record merge on a lost race; outbox when offline; no-op in read-only renders."""
    import statestore
    statestore.save("items", data)


def norm_title(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())[:160]


def key_of(folder: str, kind: str, ref: str) -> str:
    return f"{folder.replace(chr(92), '/').strip('/')}|{kind}|{ref.replace(chr(92), '/')}"


def owner_of(folder: str) -> str:
    rel = folder.replace("\\", "/").strip("/")
    try:
        res = lp.resolve(rel + "/workflow-state/current-pointer.md")
        if res.kind in ("unguarded", "outside"):
            return rel
        return res.folder or rel
    except Exception:
        return rel


def find_dup(data: dict, owner: str, title: str, exclude_key: str | None = None) -> str | None:
    want = norm_title(title)
    if not want:
        return None
    for k, e in data["items"].items():
        if k == exclude_key or e.get("status") == "done":
            continue
        if e.get("owner") == owner and norm_title(e.get("title", "")) == want:
            return k
    return None


def _apply_status(e: dict, status: str, question: str | None, now: str) -> None:
    prev = e.get("status")
    e["status"] = status
    if status == "waiting_owner":
        e["question"] = (question or e.get("question") or "").strip()
        if prev != "waiting_owner" or not e.get("blocked_since"):
            e["blocked_since"] = now
    else:
        if question:
            e["question"] = question.strip()
        if status in ("ready", "in_progress"):
            e.pop("blocked_since", None)
    if status == "done":
        e["done_at"] = now


def upsert(folder: str, kind: str, ref: str, title: str, created_by: str = "", status: str = "ready",
           question: str | None = None, force: bool = False) -> str:
    data = load()
    folder = folder.replace("\\", "/").strip("/")
    owner = owner_of(folder)
    k = key_of(folder, kind, ref)
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    if status == "waiting_owner" and not (question or "").strip():
        raise ValueError("waiting_owner requires a decision-ready question (question, 2-3 options, recommendation)")
    dup = find_dup(data, owner, title, exclude_key=k)
    if dup and not force:
        raise Duplicate(dup, data["items"][dup].get("title", ""))
    now = mint.timestamp()
    e = data["items"].get(k) or {"created": now, "created_by": created_by or folder, "fails": 0}
    e.update({"owner": owner, "kind": kind, "ref": ref.replace("\\", "/"), "title": (title or "")[:300], "updated": now})
    if created_by:
        e["created_by"] = created_by.replace("\\", "/").strip("/")
    _apply_status(e, status, question, now)
    data["items"][k] = e
    save(data)
    return k


def set_status(key: str, status: str, question: str | None = None, error: str | None = None) -> dict:
    data = load()
    e = data["items"].get(key)
    if e is None:
        raise KeyError(f"unknown item {key!r}")
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    if status == "waiting_owner" and not (question or e.get("question") or "").strip():
        raise ValueError("waiting_owner requires a question")
    _apply_status(e, status, question, mint.timestamp())
    if error:
        e["last_error"] = error[:400]
    if status == "ready":
        e.pop("stuck", None); e["fails"] = 0; e.pop("last_error", None)
    save(data)
    return e


def record_fail(key: str, error: str, threshold: int = 3) -> dict:
    """An agent came back without moving the item. After `threshold` consecutive fails the item becomes
    waiting_owner with an auto-drafted question — a repeatedly failing item is a human blocker, not a loop."""
    data = load()
    e = data["items"][key]
    e["fails"] = int(e.get("fails") or 0) + 1
    e["last_error"] = (error or "")[:400]
    now = mint.timestamp()
    if e["fails"] >= threshold:
        _apply_status(e, "waiting_owner",
                      f"Autorun fired this item {e['fails']}x without progress (last: {e['last_error'][:160]}). "
                      f"Options: (a) rewrite the task so an agent can execute it; (b) do it yourself; (c) drop it. "
                      f"Recommendation: (a).", now)
        e["question_auto"] = True
        e["stuck"] = True        # sync() keeps it on the owner; `items.py ready <key>` clears it
    else:
        _apply_status(e, "ready", None, now)
    save(data)
    return e


# ---------------------------------------------------------------- derivation

def classify_pointer_line(line: str, now=None) -> tuple[str, str | None]:
    """(status, auto question) for a pointer's `Next concrete action:` text (PROTOCOL §3 grammar).
    `now` (tz-aware) only for tests of timed tripwires."""
    low = (line or "").strip().lower()
    if not low:
        return "ready", None                      # mute pointer: fixing it IS the work
    if low.startswith("none"):
        return "done", None
    if low.startswith("parked"):
        return "parked", None
    if low.startswith("when ") or low.startswith("when:"):
        body = line.strip()[4:].lstrip(": ")
        parts = _ARROW.split(body, 1)
        cond, act = parts[0].strip(), (parts[1].strip() if len(parts) > 1 else "")
        if OWNER_RE.search(cond):
            return "waiting_owner", (f"{cond.rstrip('.?')}? Options: (a) yes -> then: {act or 'continue per the pointer'}; "
                                     f"(b) not yet -> keep waiting; (c) drop the item. Recommendation: (a) once true, else (b).")
        due = timed_due(cond)
        if due is not None:
            # TIMED tripwire (v4.3): armed until the instant, READY from then on — the action line is the work;
            # the loop fires it at the instant, never early, and the agent rewrites the pointer (never re-arm)
            now = now or now_tz()
            return ("ready" if now >= due else "waiting_world"), None
        return "waiting_world", None
    return "ready", None


def pointer_line(folder: str) -> str:
    p = ROOT / folder / "workflow-state" / "current-pointer.md"
    try:
        for raw in p.read_text(encoding="utf-8", errors="replace").splitlines():
            if raw.strip().lower().startswith("next concrete action:"):
                return raw.strip()[len("next concrete action:"):].strip()
    except OSError:
        pass
    return ""


def _resurrected(folder: str, ref: str, e: dict, now: str) -> None:
    """A consumed handoff note that came back (sync re-scan / rollback wave; v4.3): keep it done, remove the file,
    log ONE line in the owner folder's autorun log. Never fires. Renders marked READONLY leave the file alone."""
    import statestore
    e["updated"] = now
    if statestore.READONLY:
        return
    p = lp.LOCK_TREE / folder / ".goal" / "inbox" / ref
    if e.get("resurrected") == ref and not p.exists():
        return
    e["resurrected"] = ref
    gone = ""
    try:
        if p.is_file():
            p.unlink()
            gone = "file deleted"
    except OSError as ex:
        gone = f"file left in place: {ex}"
    try:
        import autorun_log
        autorun_log.append(folder, e.get("title", ref), "done", commit="-",
                           decisions=f"resurrected, skipped — already done ({e.get('outcome') or 'state store'}); {gone or 'no file'}")
    except Exception as ex:  # noqa: BLE001 — a log failure must not break a render
        print(f"items: resurrected note {folder}/{ref} — log line failed: {ex}", file=sys.stderr)


def sync(rows: list, data: dict | None = None) -> dict:
    """Reconcile the overlay with derived rows [{folder, kind, ref, title, line?, from?}]. Absent -> done.
    Never overrides in_progress; never replaces an agent's own question with an auto-drafted one.
    A `done` handoff whose file reappears stays done (v4.3, `_resurrected`). `data` only for tests."""
    data = data if data is not None else load()
    live = {}
    now = mint.timestamp()
    for r in rows:
        folder = r["folder"].replace("\\", "/").strip("/")
        kind, ref, title = r["kind"], r["ref"], r.get("title") or r["ref"]
        derived, auto_q = classify_pointer_line(r.get("line", "")) if kind == "pointer" else ("ready", None)
        k = key_of(folder, kind, ref)
        e = data["items"].get(k)
        if e is None:
            e = {"created": now, "created_by": (r.get("from") or folder), "fails": 0, "owner": owner_of(folder),
                 "kind": kind, "ref": ref, "title": title[:300]}
            _apply_status(e, derived, auto_q, now)
            if derived == "waiting_owner":
                e["question_auto"] = True
            data["items"][k] = e
        else:
            changed = norm_title(e.get("title", "")) != norm_title(title)
            e["title"], e["owner"] = title[:300], owner_of(folder)
            cur = e.get("status")
            if cur == "done" and kind == "handoff" and not changed:
                _resurrected(folder, ref, e, now)      # consumed note came back: stays done, file goes, one log line
                live[k] = e
                continue
            if cur == "in_progress" or (e.get("stuck") and not changed) or (cur == "waiting_owner" and not e.get("question_auto") and not changed):
                pass
            elif cur != derived or changed:
                keep_q = derived == "waiting_owner" and cur == "waiting_owner" and e.get("question") and not e.get("question_auto")
                _apply_status(e, derived, None if keep_q else auto_q, now)
                if derived == "waiting_owner" and not keep_q and auto_q:
                    e["question"], e["question_auto"] = auto_q, True
                if derived == "ready":
                    e.pop("question_auto", None)
        e["updated"] = now
        live[k] = e
    for k, e in data["items"].items():
        if k not in live and e.get("status") != "done":
            _apply_status(e, "done", None, now)
            e["outcome"] = e.get("outcome") or "left the board (pointer closed / handoff consumed)"
    save(data)
    return live


def from_pointer(folder: str, created_by: str = "") -> tuple[str, dict]:
    fkey = folder.replace("\\", "/").strip("/")
    line = pointer_line(fkey)
    status, q = classify_pointer_line(line)
    k = upsert(fkey, "pointer", "workflow-state/current-pointer.md", line or "(pointer has no action line)",
               created_by=created_by or fkey, status=status, question=q, force=True)
    data = load()
    if status == "waiting_owner" and q:
        data["items"][k]["question_auto"] = True
        save(data)
    return k, data["items"][k]


def ready_items(owner: str | None = None) -> list:
    data = load()
    out = [dict(key=k, **e) for k, e in data["items"].items()
           if e.get("status") == "ready" and (owner is None or e.get("owner") == owner)]
    out.sort(key=lambda e: (0 if e.get("kind") == "pointer" else 1, e.get("created", "")))
    return out


def waiting_owner() -> dict:
    out: dict = {}
    for k, e in load()["items"].items():
        if e.get("status") == "waiting_owner":
            out.setdefault(e.get("owner", "?"), []).append(dict(key=k, **e))
    return dict(sorted(out.items()))


def _cli(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="items.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    l = sub.add_parser("list"); l.add_argument("--status"); l.add_argument("--owner"); l.add_argument("--json", action="store_true")
    s = sub.add_parser("show"); s.add_argument("key")
    u = sub.add_parser("upsert")
    for opt in ("--folder", "--kind", "--ref", "--title"):
        u.add_argument(opt, required=True)
    u.add_argument("--created-by", default=""); u.add_argument("--status", default="ready"); u.add_argument("--question", default=None)
    u.add_argument("--force", action="store_true")
    for name in ("ready", "in-progress", "done"):
        p = sub.add_parser(name); p.add_argument("key"); p.add_argument("--error", default=None)
    w = sub.add_parser("wait"); w.add_argument("key"); w.add_argument("--question", required=True)
    fp = sub.add_parser("from-pointer"); fp.add_argument("folder"); fp.add_argument("--created-by", default="")
    cd = sub.add_parser("check-dup"); cd.add_argument("--owner", required=True); cd.add_argument("--title", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "list":
        rows = {k: e for k, e in load()["items"].items()
                if (not a.status or e.get("status") == a.status) and (not a.owner or e.get("owner") == a.owner)}
        if a.json:
            print(json.dumps(rows, ensure_ascii=False, indent=1))
        else:
            for k, e in sorted(rows.items(), key=lambda kv: (kv[1].get("status", ""), kv[1].get("created", ""))):
                print(f"{e.get('status', '?'):<14} {k:<60} {e.get('title', '')[:60]}")
            print(f"({len(rows)} items)")
        return 0
    if a.cmd == "show":
        e = load()["items"].get(a.key)
        if not e:
            print(f"unknown item: {a.key}", file=sys.stderr); return 2
        print(json.dumps(e, ensure_ascii=False, indent=1)); return 0
    if a.cmd == "upsert":
        try:
            print(upsert(a.folder, a.kind, a.ref, a.title, a.created_by, a.status, a.question, a.force)); return 0
        except Duplicate as d:
            print(f"DUPLICATE: {d} — not created", file=sys.stderr); return 4
    if a.cmd in ("ready", "in-progress", "done"):
        set_status(a.key, a.cmd.replace("-", "_"), error=a.error); print(f"{a.key} -> {a.cmd.replace('-', '_')}"); return 0
    if a.cmd == "wait":
        set_status(a.key, "waiting_owner", question=a.question); print(f"{a.key} -> waiting_owner"); return 0
    if a.cmd == "from-pointer":
        k, e = from_pointer(a.folder, a.created_by); print(f"{k} -> {e['status']}"); return 0
    if a.cmd == "check-dup":
        k = find_dup(load(), a.owner.replace("\\", "/").strip("/"), a.title)
        if k:
            print(f"DUPLICATE of open item {k}", file=sys.stderr); return 4
        print("no duplicate"); return 0
    return 2


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
