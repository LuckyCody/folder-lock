#!/usr/bin/env bash
# Conflict test — proves which guardrail is active in each environment (PROTOCOL.md §10).
#   bash tests/conflict_run.sh
# Throwaway fixture repo with its own registry; copies the live guards + scripts in; drives the
# violations the guards exist to stop. One line per scenario, PASS/FAIL, plus the refusal text.
# Hook stdin/stdout for scenarios 1, 6, 9 printed in full. Result -> <skill>/.selftest/conflict_last.{json,txt}
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; SK="$(cd "$HERE/.." && pwd)"
PY=python; OUTDIR="$SK/.selftest"; mkdir -p "$OUTDIR"; TR="$OUTDIR/conflict_last.txt"; : > "$TR"
RESULTS=()
log() { printf '%s\n' "$*" | tee -a "$TR"; }
section() { log ""; log "== $* =="; }

TMP="$(mktemp -d)"; FX="$TMP/repo"; mkdir -p "$FX/.githooks/lib" "$FX/.folder-lock" "$FX/_skill"
cp "$SK"/hooks/*.py "$SK"/hooks/pre-commit "$FX/.githooks/"; cp "$SK"/lib/*.py "$FX/.githooks/lib/"
cp -r "$SK/lib" "$SK/scripts" "$FX/_skill/"
printf 'workflows:\n- id: fixture-flow\n  owns:\n  - fixture/**\n- id: other-flow\n  owns:\n  - other/**\n' > "$FX/.folder-lock/registry.yaml"
FXW="$(cygpath -w "$FX" 2>/dev/null || echo "$FX")"; FXP="${FXW//\\//}"
export FOLDER_LOCK_ROOT="$FXW"; unset ICM_WINDOW ICM_LOCK_BYPASS CLAUDE_CODE_SESSION_ID ICM_FOLDER MAIN_COMMIT_OK
cd "$FX"
git init -q -b main . && git config user.email t@x.invalid && git config user.name conflict-test && git config commit.gpgsign false
git config core.hooksPath "$FXW\\.githooks"
printf '.goal/\n_skill/\n' > .gitignore
mkdir -p fixture other loose; echo base > fixture/f.txt; echo base > other/o.txt; echo x > loose/x.txt
git add .gitignore fixture/f.txt other/o.txt && ICM_LOCK_BYPASS=1 MAIN_COMMIT_OK=1 git commit -q -m "base (bypass, setup only)" >/dev/null 2>&1
git checkout -q -b feat/work
LOCK="$PY _skill/scripts/lock.py"
log "fixture: $FX  hooksPath: $(git config core.hooksPath)"

hook_edit() { local sid="$1" path="$2"; LAST_IN=$(printf '{"session_id":"%s","hook_event_name":"PreToolUse","tool_name":"Edit","tool_input":{"file_path":"%s","old_string":"a","new_string":"b"}}' "$sid" "$FXP/$path"); LAST_OUT=$(printf '%s' "$LAST_IN" | $PY .githooks/require_lock.py 2>&1); LAST_RC=$?; return $LAST_RC; }
hook_edit_env() { local win="$1" path="$2"; LAST_IN=$(printf '{"session_id":"","hook_event_name":"PreToolUse","tool_name":"Edit","tool_input":{"file_path":"%s"}}' "$FXP/$path"); LAST_OUT=$(printf '%s' "$LAST_IN" | ICM_WINDOW="$win" $PY .githooks/require_lock.py 2>&1); LAST_RC=$?; return $LAST_RC; }
hook_stop() { local sid="$1" pid="$2"; LAST_IN=$(printf '{"session_id":"%s","hook_event_name":"Stop","prompt_id":"%s","stop_hook_active":false}' "$sid" "$pid"); LAST_OUT=$(printf '%s' "$LAST_IN" | $PY .githooks/require_signoff.py 2>&1); LAST_RC=$?; return $LAST_RC; }
commit_as() { local sid="$1"; shift; LAST_OUT=$(CLAUDE_CODE_SESSION_ID="$sid" git commit -q -m "$*" 2>&1); LAST_RC=$?; return $LAST_RC; }
first_line() { printf '%s' "$1" | grep -m1 -E '\[require_lock\]|\[require_signoff\]|\[lock-guard\]|\[folder-lock\]|LOCKED|AGENT HOLDS|SIGNING OFF|REFUSED|RELEASED|CLAIMED|WIRING' || printf '%s' "$1" | head -1; }
record() { local n="$1" ok="$2" name="$3" line; line="$(first_line "$LAST_OUT")"; RESULTS+=("$n|$([ "$ok" = 1 ] && echo PASS || echo FAIL)|$name|$line"); log "$([ "$ok" = 1 ] && echo PASS || echo FAIL)  $n. $name"; log "      -> ${line:0:220}"; }
show_io() { log "   hook stdin : $LAST_IN"; log "   hook output:"; printf '%s\n' "$LAST_OUT" | sed 's/^/      /' | tee -a "$TR"; log "   exit: $LAST_RC"; }

section "1. A holds fixture; B tries to Edit under it -> edit guard refuses"
CLAUDE_CODE_SESSION_ID=sess-a $LOCK claim fixture --task "A works on fixture" --hint window-a >>"$TR" 2>&1
CLAUDE_CODE_SESSION_ID=sess-b $LOCK claim other --task "B works on other" --hint window-b >>"$TR" 2>&1
hook_edit sess-b fixture/f.txt; ok=0; [ $LAST_RC -ne 0 ] && grep -q "held by another session" <<<"$LAST_OUT" && ok=1
record 1 $ok "B edits under A's fresh lock -> require_lock denies"; show_io

section "2. B commits a staged path under A's lock -> commit guard refuses"
echo b > fixture/f.txt; git add fixture/f.txt; commit_as sess-b "B into fixture"; ok=0; [ $LAST_RC -ne 0 ] && grep -q "FOREIGN fixture" <<<"$LAST_OUT" && ok=1
record 2 $ok "B commit with fixture path staged -> FOREIGN refused"; git restore --staged fixture/f.txt

section "3. B runs git add -A (sweeps A's paths), commits -> refused; own path only -> passes"
echo b > other/o.txt; git add -A; commit_as sess-b "sweep"; ok=0; [ $LAST_RC -ne 0 ] && grep -q "FOREIGN fixture" <<<"$LAST_OUT" && ok=1
record 3 $ok "git add -A across streams -> refused (loose/ flagged UNGUARDED too)"
git reset -q; git checkout -q -- fixture/f.txt; git add other/o.txt; commit_as sess-b "B own path"; log "      (control: only other/o.txt staged -> exit $LAST_RC)"

section "4. Fresh .firing.lock (headless agent) on fixture; interactive claim -> refused, agent named"
rm -f fixture/.goal/LOCK.yaml; AGENT=$($PY _skill/lib/mint.py agent forge)
printf 'holder: fired\nwindow: "%s"\nstatus: open\ntask: "goal resume fixture"\nstarted: "%s"\n' "$AGENT" "$($PY _skill/lib/mint.py timestamp)" > fixture/.goal/.firing.lock
LAST_OUT=$(CLAUDE_CODE_SESSION_ID=sess-c $LOCK claim fixture --task "C wants fixture" 2>&1); LAST_RC=$?
ok=0; [ $LAST_RC -eq 3 ] && grep -q "AGENT HOLDS" <<<"$LAST_OUT" && grep -q "$AGENT" <<<"$LAST_OUT" && ok=1
record 4 $ok "claim over fresh .firing.lock -> exit 3, agent id reported"; rm -f fixture/.goal/.firing.lock

section "5. Fresh LOCK.yaml on fixture; headless agent (ICM_WINDOW env) tries to Edit -> refused"
CLAUDE_CODE_SESSION_ID=sess-a $LOCK claim fixture --task "A again" >>"$TR" 2>&1
hook_edit_env "$AGENT" fixture/f.txt; ok=0; [ $LAST_RC -ne 0 ] && grep -q "held by another session" <<<"$LAST_OUT" && ok=1
record 5 $ok "agent with env identity respects the interactive lock -> denied"

section "6. Closing lock, pointer not updated, stop -> Stop guard blocks with reason"
mkdir -p fixture/workflow-state; printf '# ptr\n\nNext concrete action: old\n' > fixture/workflow-state/current-pointer.md
touch -d "2 hours ago" fixture/workflow-state/current-pointer.md
CLAUDE_CODE_SESSION_ID=sess-a $LOCK close fixture >>"$TR" 2>&1
hook_stop sess-a p6; ok=0; [ $LAST_RC -ne 0 ] && grep -q "before the lock start" <<<"$LAST_OUT" && ok=1
record 6 $ok "Stop with closing lock + stale pointer -> blocked: pointer not updated"; show_io

section "7. Open lock, pointer not updated, stop -> passes"
CLAUDE_CODE_SESSION_ID=sess-a $LOCK reopen fixture >>"$TR" 2>&1; hook_stop sess-a p7; ok=0; [ $LAST_RC -eq 0 ] && ok=1
record 7 $ok "Stop with open lock -> passes"

section "8. Closing, pointer updated, committed, released -> Stop passes"
CLAUDE_CODE_SESSION_ID=sess-a $LOCK close fixture >>"$TR" 2>&1
printf '# ptr\n\nNext concrete action: NONE - fixture done\n' > fixture/workflow-state/current-pointer.md
git add fixture/workflow-state/current-pointer.md; commit_as sess-a "A signs off"; log "      (commit as A exit $LAST_RC)"
LAST_OUT=$(CLAUDE_CODE_SESSION_ID=sess-a $LOCK release fixture 2>&1); REL=$?; log "      release: $(first_line "$LAST_OUT") (exit $REL)"
hook_stop sess-a p8; ok=0; [ $REL -eq 0 ] && [ $LAST_RC -eq 0 ] && [ ! -f fixture/.goal/LOCK.yaml ] && ok=1
LAST_OUT="release exit $REL; stop exit $LAST_RC"; record 8 $ok "close -> pointer -> commit -> release -> Stop passes"

section "9. No identity: Edit -> refused; commit -> refused; stop -> blocked once"
hook_edit sess-z other/o.txt; r1=$LAST_RC; o1="$LAST_OUT"; in1="$LAST_IN"
echo z > other/o.txt; git add other/o.txt; LAST_OUT=$(git commit -q -m "no identity" 2>&1); r2=$?; o2="$LAST_OUT"; git restore --staged other/o.txt; git checkout -q -- other/o.txt
hook_stop sess-z p9; r3=$LAST_RC; o3="$LAST_OUT"; in3="$LAST_IN"
ok=0; [ $r1 -ne 0 ] && grep -q "no window identity" <<<"$o1" && [ $r2 -ne 0 ] && grep -q "no window identity" <<<"$o2" && [ $r3 -ne 0 ] && grep -q "no window identity" <<<"$o3" && ok=1
LAST_OUT="edit $r1 | commit $r2 | stop $r3"; record 9 $ok "no identity -> edit denied, commit refused, stop blocked"
log "   [9a Edit] stdin: $in1"; log "   [9a Edit] out: $(first_line "$o1") (exit $r1)"; log "   [9b commit] out: $(first_line "$o2") (exit $r2)"; log "   [9c Stop] stdin: $in3"; log "   [9c Stop] out: $(first_line "$o3") (exit $r3)"
hook_stop sess-z p9; log "   [9c Stop again] exit $LAST_RC (loop guard: one block per prompt)"

section "10. Unguarded folder (no .goal/): Edit -> refused; commit -> refused"
hook_edit sess-a loose/x.txt; r1=$LAST_RC; o1="$LAST_OUT"; git add loose/x.txt; commit_as sess-a "loose"; r2=$LAST_RC; o2="$LAST_OUT"; git restore --staged loose/x.txt
ok=0; [ $r1 -ne 0 ] && grep -qi "unguarded" <<<"$o1" && [ $r2 -ne 0 ] && grep -q "UNGUARDED" <<<"$o2" && ok=1
LAST_OUT="edit: $(first_line "$o1" | cut -c1-100) | commit: $(first_line "$o2" | cut -c1-80)"; record 10 $ok "unguarded folder -> edit denied + commit refused"

section "11. Hook wiring broken (hooksPath elsewhere) -> --verify-wiring fires non-zero"
git config core.hooksPath "$TMP\\elsewhere"; echo w > other/o.txt; git add other/o.txt
CLAUDE_CODE_SESSION_ID=sess-b git commit -q -m "hook disabled" >/dev/null 2>&1 && log "      (silent-disable demonstrated: this commit went THROUGH)"
LAST_OUT=$($PY .githooks/check_locks.py --verify-wiring 2>&1); LAST_RC=$?; ok=0; [ $LAST_RC -ne 0 ] && grep -q "NOT running" <<<"$LAST_OUT" && ok=1
record 11 $ok "--verify-wiring detects the disabled hook loudly"; git config core.hooksPath "$FXW\\.githooks"

section "12. Plain commit on main -> protect_main refuses"
git checkout -q main; echo m > other/o.txt; git add other/o.txt; commit_as sess-b "on main"; ok=0; [ $LAST_RC -ne 0 ] && grep -q "protected branch" <<<"$LAST_OUT" && ok=1
record 12 $ok "direct commit to main -> refused (MAIN_COMMIT_OK=1 to override deliberately)"

cd "$SK"; rm -rf "$TMP"
section "summary"; ALL=1
for r in "${RESULTS[@]}"; do IFS='|' read -r n ok name line <<<"$r"; log "$ok  $n. $name"; [ "$ok" = PASS ] || ALL=0; done
$PY - "$OUTDIR/conflict_last.json" "$ALL" "${RESULTS[@]}" <<'PYEOF'
import json, sys, datetime
out, allok, rows = sys.argv[1], sys.argv[2] == "1", sys.argv[3:]
res = [dict(zip(("n", "result", "name", "refusal"), r.split("|", 3))) for r in rows]
json.dump({"ts": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), "all_pass": allok, "scenarios": res}, open(out, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
print(f"recorded -> {out}")
PYEOF
[ "$ALL" = 1 ] && log "ALL 12 SCENARIOS PASS" || log "SOME SCENARIOS FAILED"
[ "$ALL" = 1 ]
