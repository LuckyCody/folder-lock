r"""statestore.py — coordination state with conditional writes, never a plain file in the sync root (PROTOCOL §14).

Documents (one JSON document each):
  items       board item status overlay            (was <repo>/.goal/items.yaml)             — lib/items.py
  names       codename map slot (reserved; an installation that names its items keeps them here)
  inboxes     {"folders": [...]} handoff inbox index (was <repo>/.goal/inboxes.txt)          — scripts/handoff.py / lock.py release
  last_seen   {"ts": "..."} the owner's digest window (was <repo>/.goal/autorun_last_seen.txt) — lib/autorun_log.py
  rules_hash  {"hosts": {HOST: {...}}} per-host rules-set hashes (PROTOCOL §15)              — lib/rules_hash.py

Semantics — the reasons this module exists:
  * Conditional writes. `save()` sends the ETag captured by `load()`. A lost race raises no error and loses no
    record: the writer re-reads, three-way-merges per record (base snapshot from load vs. current vs. its own)
    and retries. Same-record collisions resolve by the newer timestamp field.
  * Renders never write. `READONLY = True` (scripts/board.py sets it for every render) turns every `save()` into
    a no-op; callers still get their in-memory result.
  * Offline by design. When the store is unreachable `load()` serves the last cached copy and marks it stale;
    `save()` queues the document in the OUTBOX and returns; the next successful operation replays the outbox
    with the same merge. A session never blocks on the store.
  * Host-local root: lockpath.STATE_ROOT (FOLDER_LOCK_STATE_ROOT, default %LOCALAPPDATA%\folder-lock\<repo-hash>)
    holds cache/, outbox/, store/ (file backend), sessions/, guard_log.jsonl, selftest records. Nothing under the
    working tree, nothing under a file-sync root.

Backends:
  file (DEFAULT)  <STATE_ROOT>/store/<doc>.json with sha1 etags. Right for one machine, or for several machines
                  that each keep their own state root (the tree is shared, the coordination state is not).
  blob            FOLDER_LOCK_STATE_BACKEND=blob + FOLDER_LOCK_STATE_ACCOUNT=<azure storage account>
                  [+ FOLDER_LOCK_STATE_CONTAINER, default folder-lock-state]. Azure Blob with If-Match ETag writes,
                  an EXPLICIT credential chain — env -> az CLI -> managed identity LAST (`credential()`; v4.3). Right for several
                  machines sharing ONE working tree over a sync tool (the case that produced this module).
  FOLDER_LOCK_STATE_OFFLINE=1 forces the offline path (tests).

CLI:
  python lib/statestore.py status                 backend, root, cache/outbox counts, reachability
  python lib/statestore.py get <doc>              print a document
  python lib/statestore.py replay                 replay the outbox now
  python lib/statestore.py migrate [--purge]      one-shot: <repo>/.goal/items*.yaml (incl. sync conflict copies, newest
                                                  record wins), inboxes*.txt, autorun_last_seen.txt, sessions/ -> store +
                                                  state root; --purge deletes the migrated tree files afterwards
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lockpath as lp  # noqa: E402  (STATE_ROOT lives there so the hooks never import azure)

ROOT = lp.ROOT
STATE_ROOT: Path = lp.STATE_ROOT
CACHE = STATE_ROOT / "cache"
OUTBOX = STATE_ROOT / "outbox"
FILESTORE = STATE_ROOT / "store"
BACKEND = (os.environ.get("FOLDER_LOCK_STATE_BACKEND") or "file").strip().lower()
ACCOUNT = os.environ.get("FOLDER_LOCK_STATE_ACCOUNT", "")
CONTAINER = os.environ.get("FOLDER_LOCK_STATE_CONTAINER", "folder-lock-state")
OFFLINE = os.environ.get("FOLDER_LOCK_STATE_OFFLINE", "") not in ("", "0", "false")
READONLY = False                      # board.py sets True for renders
DOCS = ("items", "names", "inboxes", "last_seen", "rules_hash")
TS_FIELDS = ("updated", "renamed", "retired", "ts", "blocked_since", "born", "written", "last_seen", "done_at")

_base: dict = {}      # name -> (etag, deepcopy of the loaded document)  — the 3-way merge base per process
_stale_said: set = set()
_client = None


class Conflict(Exception):
    pass


class Unavailable(Exception):
    pass


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _say(msg: str) -> None:
    print(f"statestore: {msg}", file=sys.stderr)


# ----------------------------------------------------------------------------- backends

def _blob(name: str):
    global _client
    if OFFLINE:
        raise Unavailable("FOLDER_LOCK_STATE_OFFLINE")
    if not ACCOUNT:
        raise Unavailable("FOLDER_LOCK_STATE_BACKEND=blob but FOLDER_LOCK_STATE_ACCOUNT is unset")
    if _client is None:
        try:
            from azure.storage.blob import BlobServiceClient
        except ImportError as e:
            raise Unavailable(f"azure sdk missing (pip install azure-identity azure-storage-blob): {e}")
        cred = credential()
        _client = BlobServiceClient(f"https://{ACCOUNT}.blob.core.windows.net", credential=cred).get_container_client(CONTAINER)
    return _client.get_blob_client(f"{name}.json")


_cred = None
AZ_CLI_TIMEOUT_S = int(os.environ.get("FOLDER_LOCK_STATE_AZ_CLI_TIMEOUT", "30"))


def credential():
    """ONE credential per process, in an EXPLICIT order: EnvironmentCredential -> AzureCliCredential(process_timeout)
    -> ManagedIdentityCredential — last, and only when an identity endpoint exists (IDENTITY_ENDPOINT / MSI_ENDPOINT) or
    FOLDER_LOCK_STATE_MANAGED_IDENTITY=1. NOT DefaultAzureCredential: it probes managed identity FIRST, and on an
    Azure-Arc-enrolled machine that probe fails HARD (ClientAuthenticationError, not CredentialUnavailable) after 20-30 s
    per attempt, so every items/board/lock call took 40-120 s and writes sat in the outbox (v4.3). The az CLI login is
    the working path on a PC; the order — not the endpoint check alone — is the fix (Arc hosts DO set the endpoints)."""
    global _cred
    if _cred is None:
        try:
            from azure.identity import AzureCliCredential, ChainedTokenCredential, EnvironmentCredential, ManagedIdentityCredential
        except ImportError as e:
            raise Unavailable(f"azure sdk missing (pip install azure-identity): {e}")
        chain = [EnvironmentCredential(), AzureCliCredential(process_timeout=AZ_CLI_TIMEOUT_S)]
        if os.environ.get("IDENTITY_ENDPOINT") or os.environ.get("MSI_ENDPOINT") or os.environ.get("FOLDER_LOCK_STATE_MANAGED_IDENTITY"):
            chain.append(ManagedIdentityCredential())
        _cred = ChainedTokenCredential(*chain)
    return _cred


def _backend_get(name: str) -> tuple:
    """-> (obj | None, etag | None). None,None = document does not exist yet."""
    if BACKEND == "file":
        if OFFLINE:
            raise Unavailable("FOLDER_LOCK_STATE_OFFLINE")
        p = FILESTORE / f"{name}.json"
        if not p.is_file():
            return None, None
        raw = p.read_bytes()
        return json.loads(raw.decode("utf-8")), hashlib.sha1(raw).hexdigest()
    try:
        bc = _blob(name)
        dl = bc.download_blob(timeout=20)
        raw = dl.readall()
        return json.loads(raw.decode("utf-8")), dl.properties.etag
    except Unavailable:
        raise
    except Exception as e:  # noqa: BLE001
        if e.__class__.__name__ == "ResourceNotFoundError":
            return None, None
        raise Unavailable(f"{e.__class__.__name__}: {str(e)[:160]}")


def _backend_put(name: str, obj, etag) -> str:
    """Conditional write: etag=None means 'create only'. Raises Conflict on a lost race."""
    raw = (json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True) + "\n").encode("utf-8")
    if BACKEND == "file":
        if OFFLINE:
            raise Unavailable("FOLDER_LOCK_STATE_OFFLINE")
        FILESTORE.mkdir(parents=True, exist_ok=True)
        p = FILESTORE / f"{name}.json"
        cur = hashlib.sha1(p.read_bytes()).hexdigest() if p.is_file() else None
        if cur != etag:
            raise Conflict(f"{name}: etag {etag} != current {cur}")
        tmp = p.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_bytes(raw)
        try:
            tmp.replace(p)
        except OSError as e:           # another writer replaced it between our read and our replace
            tmp.unlink(missing_ok=True)
            raise Conflict(f"{name}: replace lost the race ({e})")
        return hashlib.sha1(raw).hexdigest()
    try:
        from azure.core import MatchConditions
        bc = _blob(name)
        if etag is None:
            props = bc.upload_blob(raw, overwrite=False, timeout=20)
        else:
            props = bc.upload_blob(raw, overwrite=True, etag=etag, match_condition=MatchConditions.IfNotModified, timeout=20)
        return props["etag"]
    except Unavailable:
        raise
    except Exception as e:  # noqa: BLE001
        n = e.__class__.__name__
        if n in ("ResourceModifiedError", "ResourceExistsError") or getattr(e, "status_code", None) == 412:
            raise Conflict(f"{name}: {n}")
        raise Unavailable(f"{n}: {str(e)[:160]}")


# ----------------------------------------------------------------------------- cache / outbox

def _cache_write(name: str, obj, etag) -> None:
    try:
        CACHE.mkdir(parents=True, exist_ok=True)
        (CACHE / f"{name}.json").write_text(json.dumps({"etag": etag, "fetched": _ts(), "data": obj}, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _cache_read(name: str) -> tuple:
    try:
        c = json.loads((CACHE / f"{name}.json").read_text(encoding="utf-8"))
        return c.get("data"), c.get("etag"), c.get("fetched", "?")
    except (OSError, json.JSONDecodeError):
        return None, None, None


def _outbox_add(name: str, obj, base_etag, base) -> Path:
    OUTBOX.mkdir(parents=True, exist_ok=True)
    p = OUTBOX / f"{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}-{name}.json"
    p.write_text(json.dumps({"name": name, "queued": _ts(), "base_etag": base_etag, "base": base, "data": obj}, ensure_ascii=False), encoding="utf-8")
    return p


def outbox_count() -> int:
    return len(list(OUTBOX.glob("*.json"))) if OUTBOX.is_dir() else 0


def replay_outbox() -> int:
    """Apply queued writes with the same 3-way merge. Returns how many were applied."""
    if READONLY or not OUTBOX.is_dir():
        return 0
    done = 0
    for p in sorted(OUTBOX.glob("*.json")):
        try:
            q = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        name = q["name"]
        try:
            for _ in range(6):
                cur, etag = _backend_get(name)
                merged = merge3(q.get("base"), cur, q["data"]) if cur is not None else q["data"]
                try:
                    new_etag = _backend_put(name, merged, etag)
                    _cache_write(name, merged, new_etag)
                    break
                except Conflict:
                    time.sleep(0.2 + random.random() * 0.3)
            else:
                return done
        except Unavailable:
            return done
        p.unlink(missing_ok=True)
        done += 1
    return done


# ----------------------------------------------------------------------------- merge

def _newer(a, b):
    """Pick between two versions of one record: the one whose timestamp field is newer; ties -> b (the writer)."""
    def stamp(r):
        if not isinstance(r, dict):
            return ""
        return max((str(r.get(f, "")) for f in TS_FIELDS), default="")
    return a if stamp(a) > stamp(b) else b


def merge3(base, cur, mine):
    """Three-way merge for the document shapes we use: {"items": {k: rec}}, {"hosts": {k: rec}},
    {"folders": [...]}, {"ts": "..."}. Records changed only by me -> mine; only by them -> cur; by both ->
    newer timestamp. Deletions: a key I deleted and they left alone disappears; a key they deleted and I left
    alone disappears; conflicting -> keep (never lose a record silently)."""
    if cur is None:
        return mine
    if base is None:
        base = {}
    if not (isinstance(cur, dict) and isinstance(mine, dict)):
        return mine
    out = {}
    for key in set(cur) | set(mine):
        b, c, m = base.get(key) if isinstance(base, dict) else None, cur.get(key), mine.get(key)
        if isinstance(c, dict) and isinstance(m, dict) and all(isinstance(v, dict) for v in list(c.values())[:5] + list(m.values())[:5]):
            bb = b if isinstance(b, dict) else {}
            rec = {}
            for k in set(bb) | set(c) | set(m):
                vb, vc, vm = bb.get(k), c.get(k), m.get(k)
                if vm == vb:            # I did not touch it
                    if vc is not None:
                        rec[k] = vc
                elif vc == vb:          # they did not touch it
                    if vm is not None:
                        rec[k] = vm
                else:                   # both changed (or one deleted): keep the newer, never drop
                    keep = _newer(vc, vm) if (vc is not None and vm is not None) else (vc if vm is None else vm)
                    rec[k] = keep
            out[key] = rec
        elif isinstance(c, list) and isinstance(m, list):
            seen = []
            for v in c + m:
                if v not in seen:
                    seen.append(v)
            out[key] = seen
        else:
            out[key] = m if m != b else c
    return out


# ----------------------------------------------------------------------------- public API

def load(name: str, default=None):
    """The document (or default). Remembers etag + snapshot for the following save(). Falls back to the cache
    when the store is unreachable (says so once per process)."""
    if name not in DOCS:
        raise ValueError(f"unknown document {name!r}")
    try:
        obj, etag = _backend_get(name)
        if obj is None:
            obj = copy.deepcopy(default) if default is not None else {}
        else:
            _cache_write(name, obj, etag)
        _base[name] = (etag, copy.deepcopy(obj))
        if outbox_count():
            replay_outbox()
        return obj
    except Unavailable as e:
        obj, etag, fetched = _cache_read(name)
        if obj is None:
            obj = copy.deepcopy(default) if default is not None else {}
            fetched = "never"
        if name not in _stale_said:
            _say(f"store unreachable ({e}); using cached {name} from {fetched} — writes will queue in the outbox")
            _stale_said.add(name)
        _base[name] = (None, copy.deepcopy(obj))
        return obj


def save(name: str, obj) -> bool:
    """Conditional write with 3-way merge on conflict; outbox when offline; no-op when READONLY.
    Returns True when the document reached the store."""
    if name not in DOCS:
        raise ValueError(f"unknown document {name!r}")
    if READONLY:
        return False
    etag, base = _base.get(name, (None, None))
    mine = obj
    for attempt in range(8):
        try:
            new_etag = _backend_put(name, mine, etag)
            _cache_write(name, mine, new_etag)
            _base[name] = (new_etag, copy.deepcopy(mine))
            return True
        except Conflict:
            try:
                cur, etag = _backend_get(name)
            except Unavailable as e:
                _outbox_add(name, mine, etag, base)
                _say(f"store unreachable mid-write ({e}); {name} queued in the outbox")
                return False
            mine = merge3(base, cur, mine)
            time.sleep(0.05 + random.random() * 0.25 * (attempt + 1))
        except Unavailable as e:
            _outbox_add(name, mine, etag, base)
            if name not in _stale_said:
                _say(f"store unreachable ({e}); {name} queued in the outbox ({outbox_count()} pending)")
                _stale_said.add(name)
            _cache_write(name, mine, etag)
            return False
    _outbox_add(name, mine, etag, base)
    _say(f"{name}: 8 conflicts in a row — queued in the outbox for replay")
    return False


def inboxes() -> list:
    return list(load("inboxes", {"folders": []}).get("folders", []))


def register_inbox(rel: str) -> None:
    rel = rel.replace("\\", "/").strip("/") or "."
    d = load("inboxes", {"folders": []})
    if rel not in d["folders"]:
        d["folders"].append(rel)
        save("inboxes", d)


# ----------------------------------------------------------------------------- migration + CLI

def _yaml_docs(pattern: str) -> list:
    import glob
    import yaml
    out = []
    for f in sorted(glob.glob(str(lp.STATE / pattern))):
        try:
            d = yaml.safe_load(Path(f).read_text(encoding="utf-8")) or {}
            if isinstance(d, dict):
                out.append((Path(f), d))
        except Exception as e:  # noqa: BLE001
            _say(f"skip {Path(f).name}: {e}")
    return out


def migrate(purge: bool = False, replace: bool = False) -> int:
    """items: newest record per item key across every yaml copy (canonical + sync conflict copies).
    --replace overwrites the store documents instead of merging into them (repair after a bad migration)."""
    moved = []
    merged: dict = {}
    srcs = _yaml_docs("items*.yaml")
    for p, d in srcs:
        for k, rec in (d.get("items") or {}).items():
            merged[k] = _newer(merged.get(k), rec) if k in merged else rec
        moved.append(p)
    if srcs:
        cur = load("items", {"items": {}})
        if replace:
            cur = {"items": {}}
        for k, rec in merged.items():
            cur.setdefault("items", {})[k] = _newer(cur["items"].get(k), rec) if k in cur["items"] else rec
        ok = save("items", cur)
        print(f"migrate items: {len(merged)} records from {len(srcs)} file(s) -> {'store' if ok else 'outbox'}")
    folders: list = []
    for f in sorted(lp.STATE.glob("inboxes*.txt")):
        for l in f.read_text(encoding="utf-8").splitlines():
            if l.strip() and l.strip() not in folders:
                folders.append(l.strip())
        moved.append(f)
    if folders:
        d = load("inboxes", {"folders": []})
        for x in folders:
            if x not in d["folders"]:
                d["folders"].append(x)
        print(f"migrate inboxes: {len(folders)} folders -> {'store' if save('inboxes', d) else 'outbox'}")
    seen = lp.STATE / "autorun_last_seen.txt"
    if seen.is_file():
        ts = seen.read_text(encoding="utf-8").strip()
        if ts:
            d = load("last_seen", {"ts": ""})
            if ts > str(d.get("ts", "")):
                d["ts"] = ts
                save("last_seen", d)
        moved.append(seen)
        print(f"migrate last_seen: {ts}")
    import shutil
    if lp.LEGACY_SESSIONS.is_dir():
        lp.SESSIONS.mkdir(parents=True, exist_ok=True)
        n = 0
        for f in lp.LEGACY_SESSIONS.iterdir():
            if f.is_file() and not (lp.SESSIONS / f.name).exists():
                shutil.copy2(f, lp.SESSIONS / f.name); n += 1
        print(f"migrate sessions: {n} file(s) copied -> {lp.SESSIONS}")
    for name in ("guard_log.jsonl", "selftest_last.json"):
        old, new = lp.STATE / name, STATE_ROOT / name
        if old.is_file() and not new.exists():
            STATE_ROOT.mkdir(parents=True, exist_ok=True)
            shutil.copy2(old, new); moved.append(old)
            print(f"migrate {name} -> {new}")
    if purge:
        for p in moved:
            try:
                p.unlink(); print(f"purged {p.name}")
            except OSError as e:
                print(f"could not purge {p.name}: {e}")
    return 0


def status() -> int:
    print(f"backend={BACKEND}" + (f" account={ACCOUNT} container={CONTAINER}" if BACKEND == "blob" else "") + f" offline={OFFLINE}")
    print(f"state root={STATE_ROOT} cache={len(list(CACHE.glob('*.json'))) if CACHE.is_dir() else 0} outbox={outbox_count()}")
    for name in DOCS:
        try:
            obj, etag = _backend_get(name)
            size = len(json.dumps(obj)) if obj is not None else 0
            top = next(iter(obj)) if isinstance(obj, dict) and obj else "-"
            n = len(obj[top]) if isinstance(obj, dict) and obj and isinstance(obj[top], (dict, list)) else "-"
            print(f"  {name:<12} {'present' if obj is not None else 'absent':<8} etag={str(etag)[:14]:<14} {top}:{n} ({size} B)")
        except Unavailable as e:
            print(f"  {name:<12} UNREACHABLE ({e})")
    return 0


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status"); g = sub.add_parser("get"); g.add_argument("doc")
    sub.add_parser("replay"); m = sub.add_parser("migrate"); m.add_argument("--purge", action="store_true")
    m.add_argument("--replace", action="store_true", help="overwrite the store documents with the migration result")
    a = ap.parse_args(argv)
    if a.cmd == "status":
        return status()
    if a.cmd == "get":
        print(json.dumps(load(a.doc, {}), ensure_ascii=False, indent=1)); return 0
    if a.cmd == "replay":
        print(f"replayed {replay_outbox()} queued write(s); {outbox_count()} left"); return 0
    if a.cmd == "migrate":
        return migrate(purge=a.purge, replace=a.replace)
    return 2


if __name__ == "__main__":
    sys.exit(main())
