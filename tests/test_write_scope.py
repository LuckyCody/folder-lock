"""lifecycle.shell_write_targets — the shell write guard's tokenizer (Cody ruling 2026-09-14: "operators and numeric
redirection targets are never filenames"). Corpus = the false positives the guard logged on 2026-09-14
(the workspace's guard log: 85 non-harness denials, most of them `>` inside Python/regex/prose the command
merely carried) plus the true positives the guard exists for.

  python -m pytest -q tests/test_write_scope.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

# Never the live store, and never AHEAD of a sibling suite that pins its own throwaway roots at import
# (test_v43.py): defaults only, and `lockpath` is imported lazily — at the first test, after collection.
_TMP = Path(tempfile.mkdtemp(prefix="folder-lock-tokenizer-"))
os.environ.setdefault("FOLDER_LOCK_STATE_ROOT", str(_TMP / "state"))
os.environ.setdefault("FOLDER_LOCK_STATE_BACKEND", "file")
os.environ.setdefault("FOLDER_LOCK_LOCK_TREE", str(_TMP / "tree"))
lc = None


@pytest.fixture(autouse=True, scope="module")
def _load():
    global lc
    import lifecycle
    lc = lifecycle


def T(cmd: str) -> list:
    return lc.shell_write_targets(cmd)


# ── true positives: what the guard is for ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("cmd, want", [
    ("echo x > file.txt", ["file.txt"]),
    ("echo x >> ../notes.md", ["../notes.md"]),
    ("cat a | tee -a log.txt", ["log.txt"]),
    ("cat a | tee --append out/run.log", ["out/run.log"]),
    ("echo x > 'my file.txt'", ["my file.txt"]),
    ('echo x > "my file.txt"', ["my file.txt"]),
    ("python x.py 2> err.log", ["err.log"]),
    (r"echo x > C:\Data\x.txt", [r"C:\Data\x.txt"]),
    ("echo x > ~/x.json", ["~/x.json"]),
    ("Get-Content a | Out-File -FilePath out.json -Encoding utf8", ["out.json"]),
    ("Set-Content -Path notes.md 'x'", ["notes.md"]),
    ("Add-Content report.txt 'x'", ["report.txt"]),
    ("Get-Content a | Out-File 'my out.json'", ["my out.json"]),
    ("echo x > .gitignore", [".gitignore"]),
    ("echo x > .scratch-audit.py", [".scratch-audit.py"]),
    ("echo x > memory.md && echo y > sub/dir/file", ["memory.md", "sub/dir/file"]),
    ("echo x >recon_log.txt", ["recon_log.txt"]),
    # quoted data BEFORE the redirection does not hide the redirection after it
    ('python -c "print(1 > 0)" > out.txt', ["out.txt"]),
    ("printf '%s\\n' 'a > b' > out.txt", ["out.txt"]),
])
def test_true_write_targets_are_found(cmd, want):
    assert T(cmd) == want


# ── false positives: 2026-09-14 guard-log corpus ─────────────────────────────────────────────────────────
PY_HEREDOC = """python - <<'PY'
import json
for r in rows:
    if r.get('n', 0) > 6 and x >= 7:
        print(f"{v:>9.2f}", f"{k:>10,.2f}", 4_000_000, before)
    m = re.match(r'(.*?)', s)
    out = best[int(m.group(1))][0]
    since.timestamp() > _time.time()
# -> secrets_env.py · => memory.md
PY
"""
PY_HEREDOC_UNQUOTED = """python - <<EOF
x = [i for i in range(3) if i > 0]
print(x)
EOF
"""
PY_HEREDOC_DASH = """cat <<-EOF
	a > b
	EOF
"""
PS_HERESTRING = """$body = @'
Hallo, das Ergebnis ist > 5% (siehe 2026-09-13T18:00:00Z).
'@
Write-Host $body
"""
PS_HERESTRING_DQ = """$s = @"
x > y.txt
"@
"""


@pytest.mark.parametrize("cmd", [
    PY_HEREDOC, PY_HEREDOC_UNQUOTED, PY_HEREDOC_DASH, PS_HERESTRING, PS_HERESTRING_DQ,
    'python -c "print(1 if a > 0.5 else 0)"',
    "python -c 'x = {\"k\": 1}; print(x[\"k\"] > 0)'",
    "grep -E 'a > b\\.txt' file",
    "echo 'value > 2h' | cat",
    'git commit -m "fix: x > y.md handled"',
    "awk '$3 > 100 {print}' data.csv",
    # bare operators / numbers / dates / flags outside quotes (a `>` in prose the model typed into a command)
    "test $a -gt 0 && x >= 7:",
    "x > = y",
    "x > 6",
    "x > 2026-09-14",
    "x > 2026-09-14T04:52",
    "x > 4_000_000:",
    "x > =0.005]",
    "x > -Encoding utf8",
    "x > FALLBACK.",
    "x > 5}_002_*.pdf",
    "x > 9}\\",
    "x > 2}/19",
    "x > }\\n",
    "x > 0`.",
    "x > 12,}",
    "x > 5%",
    "x > ]+?\\.(?:bat",
    "x > newest.get(cid,",
    "x > (`openrouteservice.json`,",
    "x > {inline(ln[3:].strip())}",
    "x -> file.md",            # arrow, not a redirection
    "x => file.md",
    "x >= 20",
    "x 2>&1",
    "x > /dev/null",
    "x > $null",
    "x > NUL",
    "x >&2",
    "x > out",                 # no path shape (pre-existing behaviour kept)
    "x > a,b.txt",             # a list, not a file
    "x > *.pdf",               # a glob is never a write target
])
def test_data_operators_and_prose_are_not_targets(cmd):
    assert T(cmd) == [], (cmd, T(cmd))


def test_heredoc_body_is_stripped_but_a_real_redirection_around_it_still_counts():
    cmd = "python - <<'PY' > result.txt\nprint(1 > 0)\nPY\n"
    assert T(cmd) == ["result.txt"]
    cmd2 = "cat <<EOF >> notes.md\nx > y\nEOF\n"
    assert T(cmd2) == ["notes.md"]


def test_unbalanced_quote_falls_back_to_best_effort():
    assert T("echo don't > file.txt") in ([], ["file.txt"])          # never raises; either reading is acceptable
    assert T("echo x > file.txt; echo it's") == ["file.txt"]


def test_strip_keeps_operand_quotes_and_blanks_other_strings():
    s = lc._strip_non_command_text('python -c "a > b" > "out dir/x.txt"')
    assert '"out dir/x.txt"' in s and "a > b" not in s
