"""v4.3 unit ladder — plain `python tests/test_v43.py` or pytest. Own throwaway state root + lock tree; never the live store.

  timed tripwire   items.classify_pointer_line: `WHEN <ISO> Berlin has passed -> ...` waiting_world before, ready after
  resurrected note items.sync: a `done` handoff whose file came back stays done, file deleted, ONE log line, never ready
  lock tree        lockpath.LOCK_TREE / lock_rel; lock.py + handoff.py never .relative_to(ROOT) a lock path
  globs            _glob_match `**/x/**`; workflow_for exact-file beats `<dir>/**` at the same prefix
  credential       statestore.credential() order: env -> az CLI -> managed identity last (only with an endpoint)
  safe git         hooks/require_safe_git.verdict: whole-tree wipes refused, named paths + dry runs pass
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
SK = HERE.parents[1]
sys.path.insert(0, str(SK / "lib"))
sys.path.insert(0, str(SK / "hooks"))

_TMP = Path(tempfile.mkdtemp(prefix="folder-lock-v43-"))
os.environ["FOLDER_LOCK_STATE_ROOT"] = str(_TMP / "state")
os.environ["FOLDER_LOCK_STATE_BACKEND"] = "file"
os.environ.pop("FOLDER_LOCK_STATE_OFFLINE", None)
os.environ["FOLDER_LOCK_LOCK_TREE"] = str(_TMP / "tree")
os.environ.pop("FOLDER_LOCK_TZ", None)
(_TMP / "tree").mkdir(parents=True, exist_ok=True)

import items  # noqa: E402
import lockpath as lp  # noqa: E402
import require_safe_git as rsg  # noqa: E402

TZ = items._tz()
G = "git "   # inputs to the text-based guard are built by concatenation (README v4.3)


def _line(cond: str, act: str = "run the acceptance check") -> str:
    return f"WHEN {cond} -> {act}"


# ------------------------------------------------------------------ timed tripwire

def test_timed_future_is_waiting_world():
    now = dt.datetime(2026, 9, 10, 20, 1, tzinfo=TZ)
    assert items.classify_pointer_line(_line("2026-09-11 14:15 Berlin has passed"), now=now) == ("waiting_world", None)


def test_timed_due_becomes_ready():
    now = dt.datetime(2026, 9, 11, 14, 15, tzinfo=TZ)
    assert items.classify_pointer_line(_line("2026-09-11 14:15 Berlin has passed"), now=now) == ("ready", None)


def test_timed_one_minute_early_still_armed():
    now = dt.datetime(2026, 9, 11, 14, 14, tzinfo=TZ)
    assert items.classify_pointer_line(_line("2026-09-11 14:15 Berlin has passed"), now=now)[0] == "waiting_world"


def test_timed_forms():
    assert items.timed_due("2026-09-12 has passed") == dt.datetime(2026, 9, 12, 0, 0, tzinfo=TZ)
    assert items.timed_due("2026-09-12T09:30 Europe/Berlin") == dt.datetime(2026, 9, 12, 9, 30, tzinfo=TZ)
    assert items.timed_due("the clock is past 2026-09-12 09:30 (CEST)") == dt.datetime(2026, 9, 12, 9, 30, tzinfo=TZ)
    assert items.timed_due("2026-09-12 09:30 CET ist vorbei") == dt.datetime(2026, 9, 12, 9, 30, tzinfo=TZ)


def test_not_timed_conditions():
    assert items.timed_due("Bastian replies to the 2026-09-10 16:08 request") is None     # a date INSIDE prose is not a timer
    assert items.timed_due("the vendor delivers") is None
    assert items.timed_due("2026-13-40 has passed") is None                                # invalid date -> not timed
    assert items.classify_pointer_line(_line("Bastian replies to the 2026-09-10 16:08 request"))[0] == "waiting_world"


def test_owner_named_with_date_stays_waiting_owner():
    st, q = items.classify_pointer_line(_line("the owner confirms by 2026-09-12 09:00 has passed"))
    assert st == "waiting_owner" and q


# ------------------------------------------------------------------ globs + tie-break

def test_glob_match_double_star_dir():
    assert lp._glob_match("a/b/workflow-state/c.md", "**/workflow-state/**") is True
    assert lp._glob_match("workflow-state/c.md", "**/workflow-state/**") is True
    assert lp._glob_match("a/b/other/c.md", "**/workflow-state/**") is False


def test_glob_match_fast_path_unchanged():
    assert lp._glob_match("finance/payroll/x", "finance/payroll/**") is True
    assert lp._glob_match("finance/payroll", "finance/payroll/**") is True
    assert lp._glob_match("finance/payrollx/y", "finance/payroll/**") is False


def _flows():
    LR = "apps/dashboard"
    return [lp.Workflow("dashboard", [f"{LR}/**"], LR),
            lp.Workflow("dashboard-core", [f"{LR}/core/**", f"{LR}/server.py", f"{LR}/deploy.py"], f"{LR}/core")]


def test_exact_file_beats_dir_glob_at_same_prefix():
    fl, LR = _flows(), "apps/dashboard"
    assert lp.workflow_for(f"{LR}/server.py", fl).id == "dashboard-core"
    assert lp.workflow_for(f"{LR}/deploy.py", fl).id == "dashboard-core"
    assert lp.workflow_for(f"{LR}/core/x.py", fl).id == "dashboard-core"
    assert lp.workflow_for(f"{LR}/templates/x.html", fl).id == "dashboard"     # folder vs folder: unchanged
    assert lp.workflow_for(f"{LR}/other.py", fl).id == "dashboard"


# ------------------------------------------------------------------ resurrected note

def test_resurrected_done_handoff_is_not_refired():
    import autorun_log
    import statestore
    folder, ref = "fixture/alpha", "20260911-0000-fixture-note-zzzz.staged.md"
    inbox = lp.LOCK_TREE / folder / ".goal" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    note = inbox / ref
    note.write_text("---\nmode: stage\ntask: \"fixture note\"\n---\n", encoding="utf-8")
    key = items.key_of(folder, "handoff", ref)
    data = {"items": {key: {"status": "done", "title": "fixture note", "owner": folder, "kind": "handoff", "ref": ref,
                            "created": "2026-09-11T00:00", "fails": 0, "outcome": "consumed 01:40"}}}
    logged = []
    orig = autorun_log.append
    autorun_log.append = lambda f, item, status, decisions="", commit="-", by="": logged.append((f, status, decisions))
    row = {"folder": folder, "kind": "handoff", "ref": ref, "title": "fixture note", "from": "x"}
    try:
        statestore.READONLY = True
        items.sync([row], data)
        assert data["items"][key]["status"] == "done" and note.exists() and not logged      # a READONLY render touches nothing
        statestore.READONLY = False
        live = items.sync([row], data)
        assert data["items"][key]["status"] == "done" and key in live
        assert not note.exists(), "resurrected note must be deleted"
        assert len(logged) == 1 and logged[0][1] == "done" and "resurrected, skipped" in logged[0][2]
        ref2 = "20260911-0001-fixture-note-two-yyyy.staged.md"
        (inbox / ref2).write_text("---\ntask: \"other\"\n---\n", encoding="utf-8")
        items.sync([{"folder": folder, "kind": "handoff", "ref": ref2, "title": "other", "from": "x"}], data)
        assert data["items"][items.key_of(folder, "handoff", ref2)]["status"] == "ready"    # a NEW note still derives ready
    finally:
        autorun_log.append = orig
        statestore.READONLY = False


# ------------------------------------------------------------------ lock tree

def test_lock_tree_and_lock_rel():
    assert lp.LOCK_TREE == (_TMP / "tree").resolve() and lp.ROOT_LOCK_DIR == lp.LOCK_TREE / ".goal"
    assert lp.lock_rel(lp.LOCK_TREE / "fixture") == "fixture"
    assert lp.lock_rel(lp.LOCK_TREE) == "."
    assert lp.lock_rel(_TMP / "elsewhere" / "fixture").endswith("fixture")     # outside the lock tree: never raises
    res = lp.resolve("fixture/alpha/x.txt", [lp.Workflow("f", ["fixture/**"], "fixture")])
    assert res.lock_dir == lp.LOCK_TREE / "fixture" / ".goal"


def test_scripts_never_relative_to_root_on_lock_paths():
    lock_src = (SK / "scripts" / "lock.py").read_text(encoding="utf-8")
    assert "lock_dir.parent.relative_to" not in lock_src and "lp.LOCK_TREE / h" in lock_src
    ho_src = (SK / "scripts" / "handoff.py").read_text(encoding="utf-8")
    assert "note.relative_to(ROOT)" not in ho_src and "lp.lock_rel(note)" in ho_src
    rl_src = (SK / "hooks" / "require_lock.py").read_text(encoding="utf-8")
    assert "relative_to(lp.ROOT)" not in rl_src


# ------------------------------------------------------------------ credential chain

def test_credential_chain_order_and_mi_last():
    import statestore
    src = (SK / "lib" / "statestore.py").read_text(encoding="utf-8")
    assert "DefaultAzureCredential(" not in src
    assert "def credential()" in src and "cred = credential()" in src
    i_env = src.index("chain = [EnvironmentCredential(),")
    i_cli, i_mi = src.index("AzureCliCredential(process_timeout", i_env), src.index("chain.append(ManagedIdentityCredential())", i_env)
    assert i_env < i_cli < i_mi
    assert 'os.environ.get("IDENTITY_ENDPOINT") or os.environ.get("MSI_ENDPOINT") or os.environ.get("FOLDER_LOCK_STATE_MANAGED_IDENTITY")' in src
    assert statestore.AZ_CLI_TIMEOUT_S == 30


# ------------------------------------------------------------------ safe git

def test_safe_git_refuses_destructive_forms():
    assert rsg.verdict(G + "clean -fdx")
    assert rsg.verdict("cd x && " + G + "clean -fd data/runs")
    assert rsg.verdict(G + "stash -u")
    assert rsg.verdict(G + "stash push -u -m tag")
    assert rsg.verdict(G + "stash --include-untracked")
    assert rsg.verdict(G + "stash save -a")
    assert rsg.verdict(G + "checkout -- .")
    assert rsg.verdict(G + "restore .")
    assert rsg.verdict(G + "-C C:/x checkout .")


def test_safe_git_allows_named_and_read_only_forms():
    assert rsg.verdict(G + "clean -n") == ""
    assert rsg.verdict(G + "clean --dry-run -d") == ""
    assert rsg.verdict(G + "stash push -m tag -- lib/items.py") == ""
    assert rsg.verdict(G + "stash list") == ""
    assert rsg.verdict(G + "checkout -- lib/items.py") == ""
    assert rsg.verdict(G + "restore --staged .") == ""
    assert rsg.verdict(G + "status --short && git diff --stat") == ""


def test_install_wires_the_hook():
    src = (SK / "scripts" / "install.py").read_text(encoding="utf-8")
    assert '"matcher": "Bash|PowerShell"' in src and "require_safe_git.py" in src


if __name__ == "__main__":
    fails = 0
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for n, f in tests:
        try:
            f()
            print(f"PASS  {n}")
        except Exception as e:  # noqa: BLE001
            fails += 1
            print(f"FAIL  {n}: {e!r}")
    print(f"{len(tests) - fails}/{len(tests)} {'ALL PASS' if not fails else 'FAILED'}")
    sys.exit(1 if fails else 0)
