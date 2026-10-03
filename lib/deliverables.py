"""deliverables.py — a workfolder's `deliverables/` folder becomes ONE board item of kind `deliverable`, listed for the
owner's review and never fired (v5.6, PROTOCOL §9).

WHY. Agents report WHAT they produced and not WHERE the owner can open it; a deliverable that lands in a working tree,
a mail or a chat message is a path, and a path is a chore. The convention gives it one home and one surface:

  · every workfolder may have a `deliverables/` folder — anything an agent wants the owner to LOOK AT goes there and
    nowhere else; scratch and intermediate files never. Optional sidecar `<file>.meta.yaml`: `title`, `summary` (one
    plain sentence), `for_owner: true|false` (default true), `bundle` (a name grouping files into one card; default =
    the whole signoff is one card). Without a sidecar: title = the filename (one file) or "<first> (+N more)", summary
    = the signoff's `--decisions` sentence or empty.
  · the signoff SWEEPS it (`scripts/signoff.py`, before `items.py from-pointer`): every new or changed file is written
    to the state store as a raw object `deliverables/<folder>/<yyyy-mm-dd>/<sha8>/<file>` (immutable — a changed file
    is a new object, nothing is overwritten or deleted) and ONE item of kind `deliverable` per bundle is upserted:
    `waiting_owner` while it waits for review, `done` when reviewed, `parked` for "later". Idempotent: the same sha256
    writes nothing; a changed file adds a `files[]` row (the old one `superseded: true`) and re-opens the item.
  · the item is NEVER fireable (`items.ready_items` drops the kind), never closed by a render (`items.sync` keeps a
    store-backed kind), and the closing message names it once as "<title> — for your review" — no path, no id.
  · the record: `key = <folder>|deliverable|<yyyy-mm-dd>-<bundle-slug>`, `files: [{filename, blob_path, sha256,
    content_type, size, created_at, superseded}]`, `summary`, `bundle`, `product` (sidecar only here), `created_by`.

The surface is the board (`board_rows()` → board.json `deliverables`, `board.py`'s "for review" section); an
installation with a web face streams `blob_path` through its own authenticated route — this module never serves bytes.

    python lib/deliverables.py register <folder> [--by <window>] [--decisions "…"] [--backfill] [--dry-run] [--root <tree>]
    python lib/deliverables.py list [--folder <f>] [--status open|parked|done|all] [--json]
    python lib/deliverables.py done|later|reopen <key> [--by <who>]
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import mimetypes
import os
import re
import sys
from pathlib import Path

try:
    import yaml  # type: ignore
except ImportError:  # the sidecar is optional; without PyYAML a sidecar is read as `key: value` lines
    yaml = None

sys.path.insert(0, str(Path(__file__).resolve().parent))
import items as _items  # noqa: E402
import lockpath as lp  # noqa: E402
import mint  # noqa: E402

KIND = _items.DELIVERABLE_KIND
FOLDER_NAME = "deliverables"
SIDECAR_SUFFIX = ".meta.yaml"
BLOB_PREFIX = "deliverables"
DONE_VISIBLE_DAYS = 30.0
QUESTION_SUFFIX = " — for your review"
WAITING = "waiting_owner"
BADGES = {".xlsx": "xlsx", ".xlsm": "xlsx", ".csv": "csv", ".md": "md", ".pdf": "pdf", ".json": "json", ".txt": "txt",
          ".log": "log", ".docx": "docx", ".pptx": "pptx", ".png": "png", ".jpg": "jpg", ".jpeg": "jpg", ".html": "html", ".zip": "zip"}
CONTENT_TYPES = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".csv": "text/csv; charset=utf-8", ".md": "text/markdown; charset=utf-8", ".txt": "text/plain; charset=utf-8",
    ".log": "text/plain; charset=utf-8", ".json": "application/json", ".pdf": "application/pdf",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".html": "text/html; charset=utf-8", ".zip": "application/zip",
}
SKIP_NAMES = {"__pycache__", ".DS_Store", "Thumbs.db", "desktop.ini"}


def _say(msg: str) -> None:
    print(msg, file=sys.stderr)


def slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(s or "").strip().lower()).strip("-")
    return (s or "bundle")[:48]


def badge(filename: str) -> str:
    return BADGES.get(Path(filename).suffix.lower(), "file")


def content_type_for(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext in CONTENT_TYPES:
        return CONTENT_TYPES[ext]
    guess, _ = mimetypes.guess_type(filename)
    return guess or "application/octet-stream"


def question_text(title: str) -> str:
    return f"{' '.join(str(title or '').split())}{QUESTION_SUFFIX}"


def deliverables_dir(folder: str, root: Path | None = None) -> Path:
    return Path(root or lp.ROOT) / folder.replace("\\", "/").strip("/") / FOLDER_NAME


def _sidecar(path: Path) -> dict:
    sc = path.with_name(path.name + SIDECAR_SUFFIX)
    if not sc.is_file():
        return {}
    text = sc.read_text(encoding="utf-8")
    try:
        if yaml is not None:
            d = yaml.safe_load(text) or {}
        else:
            d = {}
            for ln in text.splitlines():
                if ":" in ln and not ln.lstrip().startswith("#"):
                    k, v = ln.split(":", 1)
                    v = v.strip().strip('"').strip("'")
                    d[k.strip()] = {"true": True, "false": False}.get(v.lower(), v)
    except Exception as e:  # noqa: BLE001
        _say(f"(deliverables: sidecar {sc.name} unreadable — {type(e).__name__}: {e}; defaults used)")
        return {}
    return d if isinstance(d, dict) else {}


def scan(folder: str, root: Path | None = None) -> list[dict]:
    base = deliverables_dir(folder, root)
    if not base.is_dir():
        return []
    out = []
    for p in sorted(base.rglob("*")):
        if not p.is_file() or p.name.endswith(SIDECAR_SUFFIX) or p.name.startswith((".", "~$")) or p.name in SKIP_NAMES:
            continue
        if any(part in SKIP_NAMES or part.startswith(".") for part in p.relative_to(base).parts[:-1]):
            continue
        raw = p.read_bytes()
        out.append({"rel": p.relative_to(base).as_posix(), "path": p, "size": len(raw),
                    "mtime": _dt.datetime.fromtimestamp(p.stat().st_mtime), "sha256": hashlib.sha256(raw).hexdigest(),
                    "meta": _sidecar(p)})
    return out


def _default_bundle(folder: str) -> str:
    return slug(folder.replace("\\", "/").strip("/").split("/")[-1])


def bundles(entries: list[dict], folder: str) -> dict:
    groups: dict = {}
    for e in entries:
        m = e.get("meta") or {}
        if m.get("for_owner") is False or m.get("for_cody") is False:
            continue
        groups.setdefault(slug(m.get("bundle") or _default_bundle(folder)), []).append(e)
    return groups


def title_for(group: list[dict]) -> str:
    for e in group:
        t = str((e.get("meta") or {}).get("title") or "").strip()
        if t:
            return t[:200]
    names = [e["rel"] for e in group]
    return names[0][:200] if len(names) == 1 else f"{names[0]} (+{len(names) - 1} more)"[:200]


def summary_for(group: list[dict], decisions: str = "") -> str:
    for e in group:
        s = str((e.get("meta") or {}).get("summary") or "").strip()
        if s:
            return " ".join(s.split())[:400]
    d = " ".join(str(decisions or "").split())
    return "" if d.lower() in ("", "none", "-") else d[:400]


def blob_path(folder: str, date: str, sha256: str, rel: str) -> str:
    return f"{BLOB_PREFIX}/{folder.replace(chr(92), '/').strip('/')}/{date}/{sha256[:8]}/{rel}"


def find_bundle_item(data: dict, folder: str, bundle_slug: str) -> str | None:
    cands = [(k, e) for k, e in data["items"].items()
             if e.get("kind") == KIND and str(e.get("owner") or k.split("|", 1)[0]) == folder and (e.get("bundle") or "") == bundle_slug]
    if not cands:
        return None
    open_first = [k for k, e in cands if e.get("status") in (WAITING, "parked")]
    if open_first:
        return open_first[0]
    cands.sort(key=lambda ke: ke[1].get("created", ""), reverse=True)
    return cands[0][0]


def _put(path: str, raw: bytes, ctype: str, put=None) -> str:
    if put is not None:
        return put(path, raw, ctype)
    import statestore as _ss
    return _ss.raw_put(path, raw, content_type=ctype)


def register(folder: str, by: str = "", decisions: str = "", backfill: bool = False, dry: bool = False,
             root: Path | None = None, put=None) -> list[dict]:
    """The signoff step. One summary dict per bundle: {key, title, status, files, uploaded, changed, new}."""
    folder = folder.replace("\\", "/").strip("/")
    entries = scan(folder, root)
    if not entries:
        return []
    data = _items.load()
    by = (by or os.environ.get("ICM_WINDOW") or "").strip()
    now = mint.timestamp()
    today = now[:10]
    out = []
    for b, group in bundles(entries, folder).items():
        newest = max(e["mtime"] for e in group)
        date = newest.strftime("%Y-%m-%d") if backfill else today
        found = find_bundle_item(data, folder, b)
        e = data["items"].get(found) if found else None
        new = e is None
        if new or not found:
            ref = f"{date}-{b}"
            key = _items.key_of(folder, KIND, ref)
            e = None
        else:
            assert e is not None
            key = str(found)
            ref = str(e.get("ref") or key.split("|", 2)[-1])
        current = {r.get("filename"): r for r in ((e or {}).get("files") or []) if not r.get("superseded")}
        rows = list((e or {}).get("files") or [])
        uploaded, changed = 0, False
        for f in group:
            live = current.get(f["rel"])
            if live and live.get("sha256") == f["sha256"]:
                continue
            ctype = content_type_for(f["rel"])
            bp = blob_path(folder, date if new else today, f["sha256"], f["rel"])
            if not dry:
                _put(bp, f["path"].read_bytes(), ctype, put)
            for r in rows:
                if r.get("filename") == f["rel"] and not r.get("superseded"):
                    r["superseded"], r["superseded_at"] = True, now
            rows.append({"filename": f["rel"], "blob_path": bp, "sha256": f["sha256"], "content_type": ctype, "size": f["size"],
                         "created_at": (f["mtime"].strftime(mint.TS_FMT) if backfill else now), "superseded": False, "badge": badge(f["rel"])})
            uploaded += 1
            changed = True
        title = title_for(group)
        if not changed and not new:
            rec0 = e or {}
            out.append({"key": key, "title": rec0.get("title") or title, "status": rec0.get("status"), "files": len(current),
                        "uploaded": 0, "changed": False, "new": False})
            continue
        if dry:
            out.append({"key": key, "title": title, "status": WAITING, "files": len(group), "uploaded": uploaded, "changed": True,
                        "new": new, "dry_run": True})
            continue
        _items.upsert(folder, KIND, ref, title, created_by=by or folder, status=WAITING, question=question_text(title),
                      force=True, origin_channel="background", minted_by=by or "")
        data = _items.load()
        rec = data["items"][key]
        rec["files"] = rows
        rec["summary"] = summary_for(group, decisions)
        rec["bundle"] = b
        prod = next((str((x.get("meta") or {}).get("product") or "") for x in group if (x.get("meta") or {}).get("product")), "")
        if prod:
            rec["product"] = prod
        rec["deliverable_at"] = now
        if backfill:
            rec["backfilled_at"] = now
            rec["created"] = min(rec.get("created") or now, newest.strftime(mint.TS_FMT))
        rec.pop("outcome", None)
        rec["updated"] = now
        data["items"][key] = rec
        _items.save(data)
        out.append({"key": key, "title": title, "status": WAITING, "files": len([r for r in rows if not r.get("superseded")]),
                    "uploaded": uploaded, "changed": True, "new": new})
    return out


def _review_status(e: dict) -> str:
    st = e.get("status") or ""
    if st == WAITING:
        return "open"
    if st == "parked":
        return "parked"
    if st == "done":
        return "done"
    return st or "open"


def _since(e: dict) -> str:
    st = _review_status(e)
    if st == "open":
        return str(e.get("blocked_since") or e.get("deliverable_at") or e.get("created") or "")
    if st == "parked":
        return str(e.get("parked") or e.get("updated") or "")
    return str(e.get("done_at") or e.get("updated") or "")


def board_rows(data: dict | None = None, done_days: float = DONE_VISIBLE_DAYS, now: _dt.datetime | None = None) -> list[dict]:
    """Open › parked › done (≤ `done_days`), newest first inside each group — the rows board.json carries under
    `deliverables`. Title, folder, summary, since, files and the key the verbs need; no lock, no fire, no budget."""
    data = data if data is not None else _items.load()
    now = now or _dt.datetime.now()
    out = []
    for k, e in data["items"].items():
        if e.get("kind") != KIND:
            continue
        st = _review_status(e)
        if st not in ("open", "parked", "done"):
            continue
        if st == "done":
            try:
                age = (now - _dt.datetime.strptime(str(e.get("done_at") or e.get("updated") or "")[:16], mint.TS_FMT)).total_seconds() / 86400.0
            except ValueError:
                age = 0.0
            if age > done_days:
                continue
        files = [{"n": i, **{kk: r.get(kk) for kk in ("filename", "content_type", "size", "created_at", "superseded", "sha256", "blob_path")},
                  "badge": r.get("badge") or badge(r.get("filename") or "")} for i, r in enumerate(e.get("files") or [])]
        out.append({"item_key": k, "handle": _items.handle(k) if hasattr(_items, "handle") else "", "title": e.get("title") or "",
                    "summary": e.get("summary") or "", "product": e.get("product") or "",
                    "folder": str(e.get("owner") or k.split("|", 1)[0]), "bundle": e.get("bundle") or "", "status": st,
                    "raw_status": e.get("status") or "", "since": _since(e), "created": e.get("created") or "",
                    "created_by": e.get("created_by") or "", "files": files,
                    "current_files": [f for f in files if not f.get("superseded")],
                    "done_at": e.get("done_at") or "", "outcome": e.get("outcome") or "",
                    "parked_at": e.get("parked") or "", "park_reason": e.get("park_reason") or ""})
    order = {"open": 0, "parked": 1, "done": 2}
    out.sort(key=lambda r: r["since"] or "", reverse=True)
    out.sort(key=lambda r: order.get(r["status"], 9))
    return out


def _rec(key: str, data: dict) -> dict:
    e = data["items"].get(key)
    if e is None or e.get("kind") != KIND:
        raise KeyError(f"{key!r} is not a deliverable item")
    return e


def mark_done(key: str, by: str = "", outcome: str = "") -> dict:
    data = _items.load()
    e = _rec(key, data)
    now = mint.timestamp()
    _items._apply_status(e, "done", None, now)
    for k in ("parked", "park_reason", "parked_by"):
        e.pop(k, None)
    e["done_by"] = (by or os.environ.get("ICM_WINDOW") or "owner")[:120]
    if outcome.strip():
        e["outcome"] = " ".join(outcome.split())[:400]
    e["updated"] = now
    _items.save(data)
    return e


def later(key: str, by: str = "", reason: str = "") -> dict:
    data = _items.load()
    e = _rec(key, data)
    now = mint.timestamp()
    _items._apply_status(e, "parked", None, now)
    e["parked"] = now
    e["park_reason"] = (" ".join(reason.split()) or "later (owner)")[:400]
    e["parked_by"] = (by or os.environ.get("ICM_WINDOW") or "owner")[:120]
    e["updated"] = now
    _items.save(data)
    return e


def reopen(key: str, by: str = "") -> dict:
    data = _items.load()
    e = _rec(key, data)
    now = mint.timestamp()
    _items._apply_status(e, WAITING, question_text(e.get("title") or ""), now)
    for k in ("parked", "park_reason", "parked_by", "outcome"):
        e.pop(k, None)
    e["reopened_at"] = now
    e["reopened_by"] = (by or os.environ.get("ICM_WINDOW") or "owner")[:120]
    e["updated"] = now
    _items.save(data)
    return e


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="deliverables.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("register"); r.add_argument("folder"); r.add_argument("--by", default=""); r.add_argument("--decisions", default="")
    r.add_argument("--backfill", action="store_true"); r.add_argument("--dry-run", action="store_true"); r.add_argument("--root", default="")
    ls = sub.add_parser("list"); ls.add_argument("--folder", default=""); ls.add_argument("--status", default="all", choices=("open", "parked", "done", "all"))
    ls.add_argument("--json", action="store_true")
    for verb in ("done", "later", "reopen"):
        p = sub.add_parser(verb); p.add_argument("key"); p.add_argument("--by", default="")
        if verb == "done":
            p.add_argument("--outcome", default="")
        if verb == "later":
            p.add_argument("--reason", default="")
    a = ap.parse_args(argv)
    if a.cmd == "register":
        rows = register(a.folder, by=a.by, decisions=a.decisions, backfill=a.backfill, dry=a.dry_run, root=Path(a.root) if a.root else None)
        if not rows:
            print(f"deliverables: nothing under {a.folder}/{FOLDER_NAME}/")
            return 0
        for x in rows:
            what = "NEW" if x.get("new") else ("CHANGED" if x.get("changed") else "unchanged")
            print(f"deliverables: {what:<9} {x['key']} · {x['title'][:60]} · {x['files']} file(s), {x['uploaded']} uploaded" + (" (dry-run)" if x.get("dry_run") else ""))
        return 0
    if a.cmd == "list":
        rows = board_rows()
        if a.folder:
            rows = [r for r in rows if r["folder"] == a.folder.replace("\\", "/").strip("/")]
        if a.status != "all":
            rows = [r for r in rows if r["status"] == a.status]
        if a.json:
            print(json.dumps(rows, ensure_ascii=False, indent=1))
        else:
            for r in rows:
                print(f"{r['status']:<7} {r['since'][:16]:<16} {r['folder']:<30} {r['title'][:50]:<50} "
                      + (", ".join(f['filename'] for f in r['current_files']) or "-"))
                print(f"        {r['item_key']}")
            if not rows:
                print("no deliverables")
        return 0
    try:
        e = mark_done(a.key, by=a.by, outcome=a.outcome) if a.cmd == "done" else (later(a.key, by=a.by, reason=a.reason) if a.cmd == "later" else reopen(a.key, by=a.by))
    except KeyError as ex:
        print(f"REFUSED: {ex}", file=sys.stderr)
        return 2
    print(f"{a.key}: {e.get('status')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
