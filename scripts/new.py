"""new.py <slug> — scaffold a workfolder, register it, claim it, put it on the board: one step (PROTOCOL.md §17).

  python scripts/new.py <slug> [--goal "<one paragraph>"] [--at <parent dir>] [--no-commit] [--dry-run]
  python scripts/new.py <slug> --unarchive          # restore _archive/<slug> instead of scaffolding a duplicate

What it does, in order (every step fails closed; nothing is deleted, ever):
  1. slug check (`^[a-z0-9][a-z0-9-]{1,48}$`), archive check — a matching `_archive/<slug>` (or an `archived: true`
     registry entry) makes this an OFFER to unarchive (exit 6), never a second folder with the same name
  2. registry overlap check: the new `owns:` glob `<rel>/**` must not be covered by, or cover, any existing entry
  3. files from `templates/workfolder/`: `.goal/goal.md` (goal paragraph + `complete: false`), `memory.md`,
     `progress.md`, `workflow-state/current-pointer.md` (typed `Next concrete action:` so /next can route back)
  4. registry entry appended to `.folder-lock/registry.yaml` (bytes-safe, the file's own EOL), parsed back and
     the resolver asked: `<rel>/memory.md` must resolve to the new home
  5. `python scripts/lock.py claim <rel>` — the lock is this window's from the first byte (`.goal/LOCK.yaml`)
  6. board item `<rel>|pointer|workflow-state/current-pointer.md` (status ready) + codename = the slug
  7. commit `<rel>/**` + the registry entry as one commit (the commit guard accepts a registry change whose entries'
     homes are held by the committer — `lifecycle.registry_change_is_own`)
  8. prints a summary that ends in the terminal-block shape (status / held / next)

--unarchive: `git mv _archive/<slug> <rel>` (untracked leftovers moved too), the entry's globs rewritten back to
`<rel>/`, `archived:` lines dropped, then steps 5–8. The archived `memory.md` / `progress.md` come back intact.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lib"))
import lockpath as lp  # noqa: E402
import lifecycle as lc  # noqa: E402
import mint  # noqa: E402

ROOT = lp.ROOT
TREE = lp.LOCK_TREE
TEMPLATES = Path(__file__).resolve().parents[1] / "templates" / "workfolder"
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,48}$")
FILES = [".goal/goal.md", "memory.md", "progress.md", "workflow-state/current-pointer.md"]


def _git(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def _eol(data: bytes) -> str:
    return "\r\n" if b"\r\n" in data else "\n"


def _write_bytes_keep_eol(p: Path, text: str, eol: str) -> None:
    p.write_bytes(text.replace("\r\n", "\n").replace("\n", eol).encode("utf-8"))


def _git_bytes(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True)


def registry_rel() -> str:
    try:
        return lp.REGISTRY.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return ".folder-lock/registry.yaml"


def registry_apply(transform, reg_path: Path | None = None) -> str:
    """Apply `transform(text) -> text` to the LIVE registry (its own EOL kept) AND stage transform(HEAD text) as the
    index blob. The commit then carries exactly this entry's change even when the working tree holds someone else's
    uncommitted registry edit (§5 foreign-changes rule) — the index-only trick `git_sync.py` uses. Falls back to a
    plain `git add` when the registry is not tracked at HEAD or the transform does not apply to HEAD's text.
    Returns a one-line note."""
    reg_path = reg_path or lp.REGISTRY
    rel = registry_rel()
    live_bytes = reg_path.read_bytes()
    eol = _eol(live_bytes)
    live = live_bytes.decode("utf-8").replace("\r\n", "\n")
    _write_bytes_keep_eol(reg_path, transform(live), eol)
    r = _git_bytes("show", f"HEAD:{rel}")
    if r.returncode != 0:
        _git("add", "--", rel)
        return "registry staged whole (not tracked at HEAD)"
    head_bytes = r.stdout
    head_eol = _eol(head_bytes)
    try:
        new_head = transform(head_bytes.decode("utf-8").replace("\r\n", "\n"))
    except (KeyError, ValueError) as e:
        _git("add", "--", rel)
        return f"registry staged whole (transform not applicable to HEAD: {e})"
    blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=ROOT, input=new_head.replace("\n", head_eol).encode("utf-8"),
                          capture_output=True).stdout.decode().strip()
    u = _git("update-index", "--add", "--cacheinfo", f"100644,{blob},{rel}")
    if u.returncode != 0:
        _git("add", "--", rel)
        return f"registry staged whole (update-index failed: {u.stderr.strip()[:80]})"
    foreign = _git("diff", "--quiet", "--", rel).returncode != 0
    return "registry staged index-only: HEAD + this entry" + (" (a foreign uncommitted edit stays in the working tree, §5)" if foreign else "")


def registry_entry(slug: str, rel: str, goal: str, date: str) -> str:
    purpose = " ".join(goal.split())[:400].replace("'", "''")
    return (
        f"- id: {slug}\n"
        f"  purpose: '{purpose}'\n"
        f"  owns:\n"
        f"  - {rel}/**\n"
        f"  entrypoints:\n"
        f"  - '{rel}/.goal/goal.md                     # the goal (complete: false|true) — the signoff (scripts/signoff.py) archives at complete: true + 0 open items'\n"
        f"  - {rel}/workflow-state/current-pointer.md\n"
        f"  - '{rel}/memory.md                         # why the folder exists + dated decisions'\n"
        f"  created: '{date}'\n"
        f"  created_by: /new\n"
    )


def overlap(rel: str, flows: list) -> list:
    """Existing entries whose globs cover the new folder, or whose home the new glob would cover."""
    hits = []
    probe = rel + "/__probe__"
    for w in flows:
        for g in w.globs:
            if lp._glob_match(probe, g):
                hits.append(f"{w.id} owns {g} (covers {rel}/)")
                break
        else:
            if w.home and lp._glob_match(w.home + "/__probe__", rel + "/**"):
                hits.append(f"{w.id} home {w.home} would fall under {rel}/**")
    return hits


def render(name: str, **vars) -> str:
    text = (TEMPLATES / name).read_text(encoding="utf-8")
    for k, v in vars.items():
        text = text.replace("{" + k + "}", str(v))
    return text


def _archive_match(slug: str, flows: list) -> tuple:
    """(archive_path, entry) when the slug is archived: a folder under _archive/ named <slug>[-<date>] or an
    `archived: true` entry whose id is the slug."""
    for w in flows:
        if getattr(w, "archived", False) and (w.id == slug or (w.home or "").split("/")[-1] == slug):
            return w.home, w
    arch = TREE / lc.ARCHIVE_ROOT
    if arch.is_dir():
        for d in sorted(arch.iterdir()):
            if d.is_dir() and (d.name == slug or re.fullmatch(re.escape(slug) + r"-\d{8}", d.name)) and (d / ".goal").is_dir():
                return lp.lock_rel(d), None
    return "", None


def _move_tree(src: Path, dst: Path) -> list:
    """git mv the tracked files, then move every untracked leftover (locks, runtime) — nothing is left behind, nothing
    is deleted. Returns the notes."""
    notes = []
    dst.parent.mkdir(parents=True, exist_ok=True)
    tracked = _git("ls-files", "--", lp.lock_rel(src)).stdout.split()
    if tracked:
        r = _git("mv", "-k", lp.lock_rel(src), lp.lock_rel(dst))
        notes.append(f"git mv {len(tracked)} tracked file(s): rc={r.returncode} {r.stderr.strip()[:120]}")
    if src.exists():
        dst.mkdir(parents=True, exist_ok=True)
        for item in list(src.iterdir()):
            target = dst / item.name
            if target.exists():
                # merge directories (git mv created part of the tree already)
                if item.is_dir() and target.is_dir():
                    for sub in list(item.iterdir()):
                        shutil.move(str(sub), str(target / sub.name))
                    item.rmdir()
                    continue
                notes.append(f"kept both: {item} vs {target}")
                continue
            shutil.move(str(item), str(target))
        try:
            src.rmdir()
        except OSError as e:
            notes.append(f"source dir not empty after move: {e}")
    return notes


def rewrite_entry_paths(text: str, wf_id: str, old_prefix: str, new_prefix: str, archived: bool, ts: str = "") -> str:
    """Within ONE registry entry: rewrite in-repo path lines old_prefix/ -> new_prefix/, set/drop the archived lines."""
    blocks = lc.registry_blocks(text)
    if wf_id not in blocks:
        raise KeyError(f"registry entry {wf_id} not found")
    out_lines = []
    for raw in blocks[wf_id].split("\n"):
        if re.match(r"^  archived(?:_at|_from)?:", raw):
            continue
        # path-bearing lines: `  - <path>` (owns / entrypoints / writes), quoted or not
        m = re.match(r"^(  - )('?)(.*)$", raw)
        if m and (m.group(3).startswith(old_prefix + "/") or m.group(3) == old_prefix):
            raw = m.group(1) + m.group(2) + new_prefix + m.group(3)[len(old_prefix):]
        out_lines.append(raw)
    while out_lines and not out_lines[-1].strip():
        out_lines.pop()
    if archived:
        out_lines += ["  archived: true", f"  archived_at: '{ts}'", f"  archived_from: {old_prefix}"]
    blocks[wf_id] = "\n".join(out_lines) + "\n"
    # re-assemble in the original order
    order = [""] + [k for k in re.findall(r"^- id:\s*(\S+)", text.replace("\r\n", "\n"), re.M)]
    order = [k.strip("'\"") for k in order]
    pieces = []
    for k in order:
        b = blocks.get(k, "")
        pieces.append(b if b.endswith("\n") or not b else b + "\n")
    return "".join(pieces)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("slug")
    ap.add_argument("--goal", default="", help="one paragraph: what done looks like")
    ap.add_argument("--at", default="", help="parent directory (default: the workspace root)")
    ap.add_argument("--unarchive", action="store_true")
    ap.add_argument("--no-commit", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--origin", default="cody", help="§13 origin of the folder's first board item: cody (default — the owner asked for the folder) | decomposition (with --parent) | repair | agent")
    ap.add_argument("--parent", default="", help="decomposition: the item this folder was split off from")
    a = ap.parse_args()

    slug = a.slug.strip().lower()
    if not SLUG_RE.match(slug):
        print(f"ERROR: slug {a.slug!r} — lowercase letters, digits, hyphens, 2–49 chars, starts alphanumeric", file=sys.stderr)
        return 2
    parent = a.at.replace("\\", "/").strip("/")
    rel = f"{parent}/{slug}" if parent else slug
    try:
        flows = lp.load_registry()
    except lp.RegistryUnreadable as e:
        print(f"ERROR: registry unreadable — {e}", file=sys.stderr)
        return 4
    reg_path = lp.REGISTRY
    if not reg_path.is_file():
        reg_path.parent.mkdir(parents=True, exist_ok=True)
        reg_path.write_text("workflows:\n", encoding="utf-8")
        print(f"  registry: created {lp.lock_rel(reg_path)} (the repo had none)")
    reg_bytes = reg_path.read_bytes()
    eol = _eol(reg_bytes)
    reg_text = reg_bytes.decode("utf-8").replace("\r\n", "\n")
    ts = mint.timestamp()
    date = ts[:10]

    arch_rel, arch_entry = _archive_match(slug, flows)
    if arch_rel and not a.unarchive:
        print(f"ARCHIVED {slug} exists at {arch_rel} (memory.md + progress.md intact). Not creating a duplicate.")
        print(f"  unarchive instead: python scripts/new.py {slug} --unarchive" + (f" --at {parent}" if parent else ""))
        print(f"  or pick another slug.")
        return 6
    if a.unarchive and not arch_rel:
        print(f"ERROR: nothing archived under the slug {slug!r}", file=sys.stderr)
        return 2

    if a.unarchive:
        if arch_entry is not None:
            m = re.search(r"^  archived_from:\s*(\S+)", lc.registry_blocks(reg_text).get(arch_entry.id, ""), re.M)
            if m and not a.at:
                rel = m.group(1).strip().strip("'\"")
        if (TREE / rel).exists():
            print(f"ERROR: {rel} already exists — cannot restore {arch_rel} over it", file=sys.stderr)
            return 3
        print(f"RESTORE {arch_rel} -> {rel}")
        if a.dry_run:
            return 0
        notes = _move_tree(TREE / arch_rel, TREE / rel)
        for n in notes:
            print(f"  {n}")
        if arch_entry is not None:
            note = registry_apply(lambda t: rewrite_entry_paths(t, arch_entry.id, arch_rel, rel, archived=False), reg_path)
            print(f"  registry: {arch_entry.id} owns {rel}/** again (archived: dropped) · {note}")
        else:
            entry = registry_entry(slug, rel, a.goal or f"restored from {arch_rel}", date)
            note = registry_apply(lambda t: t.rstrip("\n") + "\n" + entry, reg_path)
            print(f"  registry: + {slug} (owns {rel}/**) — the archive had no entry · {note}")
        # a stale lock may have travelled with the archived folder — the claim below sees it; a lock of this window is reused
        goal_line = a.goal or f"resume {slug} (unarchived {date})"
    else:
        if (TREE / rel).exists():
            print(f"ERROR: {rel} already exists — claim it via lock.py (python scripts/lock.py claim {rel} --task \"...\")", file=sys.stderr)
            return 3
        if any(w.id == slug for w in flows):
            print(f"ERROR: registry already has an entry with id {slug!r} — pick another slug or claim that workflow via lock.py", file=sys.stderr)
            return 3
        hits = overlap(rel, flows)
        if hits:
            print(f"ERROR: owns: glob {rel}/** overlaps the registry:", file=sys.stderr)
            for h in hits:
                print(f"  - {h}", file=sys.stderr)
            return 3
        goal_line = a.goal or f"{slug}: goal not written yet — replace this paragraph in .goal/goal.md"
        print(f"CREATE {rel}/  (registry id {slug}, owns {rel}/**)")
        if a.dry_run:
            return 0
        (TREE / rel / ".goal").mkdir(parents=True, exist_ok=True)   # the folder exists before the registry names it
        entry = registry_entry(slug, rel, goal_line, date)
        note = registry_apply(lambda t: t.rstrip("\n") + "\n" + entry, reg_path)
        print(f"  registry: + {slug} (owns {rel}/**), no overlap · {note}")

    # validate the registry: both parsers + the resolver
    try:
        import yaml
        yaml.safe_load(reg_path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: registry no longer parses as YAML after the edit: {e} — restoring the previous bytes", file=sys.stderr)
        reg_path.write_bytes(reg_bytes); _git("reset", "-q", "--", registry_rel())
        return 5
    flows2 = lp.load_registry()
    res = lp.resolve(rel + "/memory.md", flows2)
    if res.folder != rel:
        print(f"ERROR: {rel}/memory.md resolves to {res.folder!r} ({res.kind}), not the new home — registry restored", file=sys.stderr)
        reg_path.write_bytes(reg_bytes); _git("reset", "-q", "--", registry_rel())
        return 5
    print(f"  resolver: {rel}/memory.md -> {res.folder} ({res.workflow})")

    # claim (the lock is this window's from the first byte)
    task = " ".join(goal_line.split())[:110]
    r = subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "lock.py"), "claim", rel,
                        "--task", task, "--stream", slug, "--hint", slug], cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    sys.stdout.write(r.stdout)
    if r.returncode != 0:
        sys.stderr.write(r.stderr)
        print(f"ERROR: claim of {rel} failed (rc={r.returncode}) — folder + registry entry are in place; claim by hand and commit", file=sys.stderr)
        return 7
    me = lp.identity(session_id=(os.environ.get("CLAUDE_CODE_SESSION_ID") or "").strip())
    window = me.window if me else "?"
    if not a.unarchive:
        # template files AFTER the claim: the pointer's mtime is then >= the lock start (lock.py release checks that)
        vars_ = {"slug": slug, "rel": rel, "goal": goal_line, "date": date, "ts": ts, "window": window}
        for f in FILES:
            p = TREE / rel / f
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(render(Path(f).name if f != ".goal/goal.md" else "goal.md", **vars_), encoding="utf-8")
            print(f"  + {rel}/{f}")

    # board item + codename so /next routes back here
    try:
        import items as _items
        title = _items.pointer_line(rel) or task
        try:
            k = _items.upsert(rel, "pointer", "workflow-state/current-pointer.md", title, created_by=rel, status="ready", force=True)
        except Exception as e:  # noqa: BLE001
            k = f"(item not written: {e})"
        print(f"  board: item {k} ready")
    except Exception as e:  # noqa: BLE001
        print(f"  board: not updated ({e}) — the next render sweeps the pointer in")

    sha = ""
    if not a.no_commit:
        _git("add", "-A", "--", rel)   # the registry is already staged index-only by registry_apply; git mv staged the old path's deletions
        msg = (f"{slug}: {'unarchived' if a.unarchive else 'new workfolder'} via new.py — {rel}/ + registry entry")
        r = _git("commit", "-m", msg)
        if r.returncode != 0:
            print(f"  commit: FAILED rc={r.returncode}\n{r.stdout}\n{r.stderr}".rstrip())
            print("  (folder, registry entry and lock are in place; fix the guard's complaint and commit by hand)")
        else:
            sha = _git("rev-parse", "--short", "HEAD").stdout.strip()
            print(f"  commit: {sha}")

    try:
        import items as _items2
        nxt = _items2.pointer_line(rel)
    except Exception:  # noqa: BLE001
        nxt = ""
    print()
    print(f"{'RESTORED' if a.unarchive else 'CREATED'} {rel} · window={window} · commit={sha or '(not committed)'}")
    print(lc.format_terminal_block("done", rel, nxt or f"write the first step into {rel}/workflow-state/current-pointer.md and start"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
