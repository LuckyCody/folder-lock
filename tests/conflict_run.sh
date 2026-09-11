#!/usr/bin/env bash
# Conflict test — proves which guardrail is active in each environment (PROTOCOL.md §10).
#   bash tests/conflict_run.sh
# Throwaway fixture repo with its own registry AND its own state root (§14 — never the live store); copies the live
# guards + scripts in; drives the violations the guards exist to stop. One line per scenario, PASS/FAIL, plus the
# refusal text. Hook stdin/stdout for scenarios 1, 6, 9 printed in full.
# 1-12 = the v2/v3 ladder · 13 = handoff sidecar · 14 = literal lock after a registry remap · 15-16 = state store
# (write race merges; offline -> outbox -> replay) · 17-19 = §15 proofs (conflict copies, rules hash, read coverage)
# · 20 = §16 lock-holder view · 21 = reader identity. Result -> <skill's STATE_ROOT>/conflict_last.{json,txt}
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; SK="$(cd "$HERE/.." && pwd)"
PY=python
OUTDIR_W="$(cd "$SK" && $PY -c "import sys; sys.path.insert(0, 'lib'); import lockpath; print(lockpath.STATE_ROOT)")"
OUTDIR="$(cygpath -u "$OUTDIR_W" 2>/dev/null || echo "$OUTDIR_W")"; mkdir -p "$OUTDIR"; TR="$OUTDIR/conflict_last.txt"; : > "$TR"
RESULTS=()
log() { printf '%s\n' "$*" | tee -a "$TR"; }
section() { log ""; log "== $* =="; }

TMP="$(mktemp -d)"; FX="$TMP/repo"; mkdir -p "$FX/.githooks/lib" "$FX/.folder-lock" "$FX/_skill"
cp "$SK"/hooks/*.py "$SK"/hooks/pre-commit "$FX/.githooks/"; cp "$SK"/lib/*.py "$FX/.githooks/lib/"
cp -r "$SK/lib" "$SK/scripts" "$FX/_skill/"
printf 'workflows:\n- id: fixture-flow\n  owns:\n  - fixture/**\n- id: other-flow\n  owns:\n  - other/**\n' > "$FX/.folder-lock/registry.yaml"
FXW="$(cygpath -w "$FX" 2>/dev/null || echo "$FX")"; FXP="${FXW//\\//}"
export FOLDER_LOCK_ROOT="$FXW"
export FOLDER_LOCK_STATE_ROOT="$FXW\\.fl-state"; FLSTATE="$FXP/.fl-state"   # §14: fixture state root (bindings, store, guard log)
unset ICM_WINDOW ICM_LOCK_BYPASS CLAUDE_CODE_SESSION_ID ICM_FOLDER MAIN_COMMIT_OK FOLDER_LOCK_STATE_BACKEND FOLDER_LOCK_STATE_OFFLINE FOLDER_LOCK_HOST
cd "$FX"
git init -q -b main . && git config user.email t@x.invalid && git config user.name conflict-test && git config commit.gpgsign false
git config core.hooksPath "$FXW\\.githooks"
printf '.goal/\n_skill/\n.fl-state/\n' > .gitignore
mkdir -p fixture other loose; echo base > fixture/f.txt; echo base > other/o.txt; echo x > loose/x.txt
git add .gitignore fixture/f.txt other/o.txt && ICM_LOCK_BYPASS=1 MAIN_COMMIT_OK=1 git commit -q -m "base (bypass, setup only)" >/dev/null 2>&1
git checkout -q -b feat/work
LOCK="$PY _skill/scripts/lock.py"
log "fixture: $FX  hooksPath: $(git config core.hooksPath)  state root: $FOLDER_LOCK_STATE_ROOT"

hook_edit() { local sid="$1" path="$2"; LAST_IN=$(printf '{"session_id":"%s","hook_event_name":"PreToolUse","tool_name":"Edit","tool_input":{"file_path":"%s","old_string":"a","new_string":"b"}}' "$sid" "$FXP/$path"); LAST_OUT=$(printf '%s' "$LAST_IN" | $PY .githooks/require_lock.py 2>&1); LAST_RC=$?; return $LAST_RC; }
hook_edit_env() { local win="$1" path="$2"; LAST_IN=$(printf '{"session_id":"","hook_event_name":"PreToolUse","tool_name":"Edit","tool_input":{"file_path":"%s"}}' "$FXP/$path"); LAST_OUT=$(printf '%s' "$LAST_IN" | ICM_WINDOW="$win" $PY .githooks/require_lock.py 2>&1); LAST_RC=$?; return $LAST_RC; }
hook_stop() { local sid="$1" pid="$2"; LAST_IN=$(printf '{"session_id":"%s","hook_event_name":"Stop","prompt_id":"%s","stop_hook_active":false}' "$sid" "$pid"); LAST_OUT=$(printf '%s' "$LAST_IN" | $PY .githooks/require_signoff.py 2>&1); LAST_RC=$?; return $LAST_RC; }
commit_as() { local sid="$1"; shift; LAST_OUT=$(CLAUDE_CODE_SESSION_ID="$sid" git commit -q -m "$*" 2>&1); LAST_RC=$?; return $LAST_RC; }
first_line() { printf '%s' "$1" | grep -m1 -E '\[require_lock\]|\[require_signoff\]|\[lock-guard\]|\[folder-lock\]|LOCKED|AGENT HOLDS|SIGNING OFF|REFUSED|RELEASED|CLAIMED|WIRING|READER|RULES|PARTIAL|READ IN FULL' || printf '%s' "$1" | head -1; }
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

section "9. No identity: Edit -> refused; commit -> refused; stop -> PASSES (browsing session, v4.2)"
hook_edit sess-z other/o.txt; r1=$LAST_RC; o1="$LAST_OUT"; in1="$LAST_IN"
echo z > other/o.txt; git add other/o.txt; LAST_OUT=$(git commit -q -m "no identity" 2>&1); r2=$?; o2="$LAST_OUT"; git restore --staged other/o.txt; git checkout -q -- other/o.txt
hook_stop sess-z p9; r3=$LAST_RC; o3="$LAST_OUT"; in3="$LAST_IN"
ok=0; [ $r1 -ne 0 ] && grep -q "no window identity" <<<"$o1" && [ $r2 -ne 0 ] && grep -q "no window identity" <<<"$o2" && [ $r3 -eq 0 ] && ok=1
LAST_OUT="edit $r1 | commit $r2 (both 'no window identity') | stop $r3 (pass: nothing claimed, nothing to sign off)"; record 9 $ok "no identity -> edit denied, commit refused, stop PASSES (menu-only sessions are not an error state)"
log "   [9a Edit] stdin: $in1"; log "   [9a Edit] out: $(first_line "$o1") (exit $r1)"; log "   [9b commit] out: $(first_line "$o2") (exit $r2)"; log "   [9c Stop] stdin: $in3"; log "   [9c Stop] out: $(first_line "$o3") (exit $r3)"
hook_stop sess-z p9; log "   [9c Stop again] exit $LAST_RC (still a pass — no nag to loop-guard)"

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
git reset -q --hard; git checkout -q feat/work   # back to the work branch (fixture pointer lives there)

section "13. Handoff record survives a binding rewrite (sidecar in the state root): release refuses the orphan; consume clears it"
mkdir -p third/.goal; echo t > third/t.txt
CLAUDE_CODE_SESSION_ID=sess-f $LOCK claim fixture --task "F stages a handoff" --hint window-f >>"$TR" 2>&1
oH=$(CLAUDE_CODE_SESSION_ID=sess-f $PY _skill/scripts/handoff.py --to third --task "sidecar test handoff" --mode stage 2>&1); rH=$?
NOTE=$(ls third/.goal/inbox/*.staged.md 2>/dev/null | head -1)
CLAUDE_CODE_SESSION_ID=sess-f $LOCK claim fixture --task "F re-claims (binding rewritten)" >>"$TR" 2>&1
SIDE="$FLSTATE/sessions/sess-f.handoffs.txt"   # §14: bindings + sidecars live in the host-local state root, not the tree
side_ok=0; [ -f "$SIDE" ] && grep -q "^staged third/.goal/inbox/" "$SIDE" && ! grep -q "^handoff:" "$FLSTATE/sessions/sess-f.yaml" && [ ! -d .goal/sessions ] && side_ok=1
rm -f "$NOTE"                                    # consumed by hand, no record -> must be flagged
touch fixture/workflow-state/current-pointer.md  # pointer newer than the lock start, content unchanged (not dirty)
oR1=$(CLAUDE_CODE_SESSION_ID=sess-f $LOCK release fixture 2>&1); rR1=$?
oC=$(CLAUDE_CODE_SESSION_ID=sess-f $LOCK consume "$NOTE" 2>&1); rC=$?
oR2=$(CLAUDE_CODE_SESSION_ID=sess-f $LOCK release fixture 2>&1); rR2=$?
ok=0; [ $rH -eq 0 ] && [ -n "$NOTE" ] && [ $side_ok -eq 1 ] && [ $rR1 -eq 6 ] && grep -q "orphaned handoff" <<<"$oR1" && [ $rC -eq 0 ] && grep -q "^consumed " "$SIDE" && [ $rR2 -eq 0 ] && grep -q "RELEASED" <<<"$oR2" && ok=1
LAST_OUT="stage exit $rH | sidecar in state root + yaml-clean + no tree sessions/: $side_ok | release#1: $(first_line "$oR1") ($rR1) | consume: $(first_line "$oC" | cut -c1-40) | release#2: $(first_line "$oR2") ($rR2)"
record 13 $ok "sidecar (state root) keeps the staged record across the binding rewrite; orphan refused (exit 6); consume -> released"
log "      handoff.py: $(printf '%s' "$oH" | head -1 | cut -c1-120)"

section "14. Folder claimed as its own lock domain, registry then folds it into a locked home -> literal lock honoured"
oG=$(CLAUDE_CODE_SESSION_ID=sess-g $LOCK claim third --task "G owns third" --hint window-g 2>&1); rG=$?
printf '  - third/**\n' >> .folder-lock/registry.yaml       # mid-session registry edit: third now belongs to other-flow (held by B)
oK=$(CLAUDE_CODE_SESSION_ID=sess-g $LOCK check third 2>&1); rK=$?
hook_edit sess-g third/t.txt; rE=$LAST_RC; oE="$LAST_OUT"
hook_edit sess-b third/t.txt; rE2=$LAST_RC                      # B holds the registry home, NOT third's literal lock -> still refused
mkdir -p third/workflow-state; printf 'Next concrete action: NONE\n' > third/workflow-state/current-pointer.md
oL=$(CLAUDE_CODE_SESSION_ID=sess-g $LOCK release third --allow-dirty "fixture test" 2>&1); rL=$?
ok=0; [ $rG -eq 0 ] && grep -q "YOURS third" <<<"$oK" && grep -q "LOCKED other" <<<"$oK" && [ $rE -eq 0 ] && [ $rE2 -ne 0 ] && [ $rL -eq 0 ] && grep -q "RELEASED third" <<<"$oL" && [ ! -f third/.goal/LOCK.yaml ] && ok=1
LAST_OUT="claim $rG | check: YOURS third + LOCKED other: $(grep -c 'YOURS third\|LOCKED other' <<<"$oK")/2 | G edits third: exit $rE | B edits third: exit $rE2 | release: $(first_line "$oL") ($rL)"
record 14 $ok "literal own lock survives a registry remap: check sees it, edit allowed for the holder only, release removes it"

section "15. Two writers race on the state store (§14, file backend): the loser merges on the ETag, no record lost"
rm -f .a_done
$PY - <<'PYEOF' &
import os, sys, time
sys.path.insert(0, "_skill/lib"); import statestore as s
d = s.load("items", {"items": {}})                       # writer B snapshots FIRST (stale etag from here on)
t0 = time.time()
while not os.path.exists(".a_done") and time.time() - t0 < 20:
    time.sleep(0.1)
d["items"]["race-B"] = {"title": "b", "status": "ready", "updated": "2026-09-11T00:02"}
print("B saved", s.save("items", d))
PYEOF
BPID=$!; sleep 1.5
oA=$($PY -c "import sys; sys.path.insert(0,'_skill/lib'); import statestore as s; d=s.load('items',{'items':{}}); d['items']['race-A']={'title':'a','status':'ready','updated':'2026-09-11T00:01'}; print('A saved', s.save('items', d)); open('.a_done','w').close()" 2>&1)
wait $BPID; rm -f .a_done
o15=$($PY -c "import sys; sys.path.insert(0,'_skill/lib'); import statestore as s; print(sorted(k for k in s.load('items',{'items':{}})['items'] if k.startswith('race-')))" 2>&1)
ok=0; grep -q "A saved True" <<<"$oA" && grep -q "race-A" <<<"$o15" && grep -q "race-B" <<<"$o15" && ok=1
LAST_OUT="A: $oA | after both: $o15"; record 15 $ok "stale-etag writer merges instead of overwriting: both race-A and race-B present"

section "16. State store unreachable: the write queues in the outbox, replay lands it (§14)"
oQ=$(FOLDER_LOCK_STATE_OFFLINE=1 $PY -c "import sys; sys.path.insert(0,'_skill/lib'); import statestore as s; d=s.load('items',{'items':{}}); d['items']['off-C']={'title':'c','status':'ready','updated':'2026-09-11T00:03'}; print('offline save ->', s.save('items', d), 'outbox', s.outbox_count())" 2>&1)
oR=$($PY _skill/lib/statestore.py replay 2>&1); oC=$($PY -c "import sys; sys.path.insert(0,'_skill/lib'); import statestore as s; print('off-C' in s.load('items',{'items':{}})['items'], s.outbox_count())" 2>&1)
ok=0; grep -q "offline save -> False outbox 1" <<<"$oQ" && grep -q "replayed 1" <<<"$oR" && grep -q "^True 0" <<<"$oC" && ok=1
LAST_OUT="offline: $(printf '%s' "$oQ" | tail -1) | replay: $oR | after: $oC"; record 16 $ok "offline write -> outbox (1), replay -> record present, outbox empty"

section "17. Sync conflict copies (§15): identical copy folded by claim; divergent copy of a rules file OUTSIDE the folder refuses the claim (5); divergent copy INSIDE = first act, edit denied until folded"
mkdir -p fixture/workflow-state
printf 'Next concrete action: NONE\n' > fixture/workflow-state/current-pointer.md
printf 'Next concrete action: SOMETHING ELSE the copy says\n' > fixture/workflow-state/current-pointer-WIN-ABCDEF123456.md   # divergent, inside fixture
echo mem > fixture/memory.md; cp fixture/memory.md fixture/memory-WIN-ABCDEF123456.md                                            # identical, inside fixture
cp .folder-lock/registry.yaml .folder-lock/registry-WIN-ABCDEF123456.yaml; printf -- '- id: ghost\n  owns: [ghost/**]\n' >> .folder-lock/registry-WIN-ABCDEF123456.yaml   # divergent RULES copy, outside
o17a=$(CLAUDE_CODE_SESSION_ID=sess-h $LOCK claim fixture --task "H claims with conflict copies around" --hint window-h 2>&1); r17a=$?
rm -f .folder-lock/registry-WIN-ABCDEF123456.yaml                                                                                 # "folded" (test: the registry copy was noise)
o17b=$(CLAUDE_CODE_SESSION_ID=sess-h $LOCK claim fixture --task "H claims again" --hint window-h 2>&1); r17b=$?
hook_edit sess-h fixture/workflow-state/current-pointer.md; r17c=$LAST_RC; o17c="$LAST_OUT"                                    # divergent copy beside the pointer -> denied
rm -f fixture/workflow-state/current-pointer-WIN-ABCDEF123456.md
hook_edit sess-h fixture/workflow-state/current-pointer.md; r17d=$LAST_RC                                                       # copy gone -> allowed
ok=0; [ $r17a -eq 5 ] && grep -q "CONFLICT COPY" <<<"$o17a" && [ $r17b -eq 0 ] && grep -q "FIRST ACT" <<<"$o17b" && [ ! -f fixture/memory-WIN-ABCDEF123456.md ] && [ $r17c -ne 0 ] && grep -q "conflict copy beside it" <<<"$o17c" && [ $r17d -eq 0 ] && ok=1
LAST_OUT="claim#1 exit $r17a ($(grep -m1 'CONFLICT COPY' <<<"$o17a" | cut -c1-70)) | claim#2 exit $r17b, identical copy gone: $([ ! -f fixture/memory-WIN-ABCDEF123456.md ] && echo yes || echo NO) | edit beside divergent copy: exit $r17c | after fold: exit $r17d"
record 17 $ok "conflict copies: rules copy outside -> claim refused (5); identical copy folded by claim; divergent copy inside -> first act + edit denied until folded"
CLAUDE_CODE_SESSION_ID=sess-h $LOCK release fixture --allow-dirty "fixture test" >>"$TR" 2>&1

section "18. Rules-set hash across hosts (§15, shared file backend): same names, different bytes -> RULES DIVERGE names the file"
o18a=$(FOLDER_LOCK_HOST=HOSTA $PY _skill/lib/rules_hash.py 2>&1); r18a=$?
printf '\n# drift on host B only\n' >> .githooks/lib/mint.py
o18b=$(FOLDER_LOCK_HOST=HOSTB $PY _skill/lib/rules_hash.py 2>&1); r18b=$?
ok=0; grep -q "^rules: HOSTA" <<<"$o18a" && ! grep -q "DIVERGE" <<<"$o18a" && grep -q "RULES DIVERGE" <<<"$o18b" && grep -q ".githooks/lib/mint.py" <<<"$o18b" && grep -q "HOSTA" <<<"$o18b" && ok=1
LAST_OUT="A: $(first_line "$o18a" | cut -c1-70) | B: $(first_line "$o18b" | cut -c1-160)"
record 18 $ok "rules hash: host B detects the drifted .githooks/lib/mint.py against host A's publish (reports, does not block)"

section "19. Read-coverage guard (§15): a sliced Read of the pointer is reported PARTIAL with the action line verbatim; a full Read is proven"
printf 'header line\nNext concrete action: build the thing\nResume handle: here\n' > fixture/workflow-state/current-pointer.md
LAST_IN="{\"tool_name\":\"Read\",\"session_id\":\"sess-h\",\"tool_input\":{\"file_path\":\"$FXP/fixture/workflow-state/current-pointer.md\",\"offset\":1,\"limit\":1}}"
o19a=$(printf '%s' "$LAST_IN" | $PY .githooks/check_pointer_read.py 2>&1); r19a=$?
o19b=$(printf '{"tool_name":"Read","session_id":"sess-h","tool_input":{"file_path":"%s/fixture/workflow-state/current-pointer.md"}}' "$FXP" | $PY .githooks/check_pointer_read.py 2>&1); r19b=$?
ok=0; grep -q "PARTIAL READ" <<<"$o19a" && grep -q "Next concrete action (verbatim, L2): Next concrete action: build the thing" <<<"$o19a" && grep -q "READ IN FULL" <<<"$o19b" && grep -q "3 lines" <<<"$o19b" && ok=1
LAST_OUT="sliced: $(grep -o 'PARTIAL READ[^;]*' <<<"$o19a" | head -1 | cut -c1-90) + action line injected: $(grep -c 'verbatim, L2' <<<"$o19a") | full: $(grep -o 'READ IN FULL[^"]*' <<<"$o19b" | cut -c1-80)"
record 19 $ok "read coverage: 1-of-3-lines Read -> PARTIAL + action line verbatim; full Read -> READ IN FULL proof"

section "20. Lock-holder view (§16): LOCKED names the holder's peer name + liveness from ~/.claude/sessions; a dead pid reads GONE; lock.py who lists every lock"
PEERS="$TMP/claude-cfg"; mkdir -p "$PEERS/sessions"
CLAUDE_CODE_SESSION_ID=sess-a $LOCK claim fixture --task "A holds fixture for the holder view" --hint window-a20 >>"$TR" 2>&1
LIVE_PID=$(cat /proc/$$/winpid 2>/dev/null || echo $$)   # the WINDOWS pid of this bash (MSYS $$ is not what OpenProcess sees)
printf '{"pid":%s,"sessionId":"sess-a","name":"peer-a","kind":"interactive","cwd":"%s"}' "$LIVE_PID" "$FXP" > "$PEERS/sessions/$LIVE_PID.json"
o20a=$(CLAUDE_CONFIG_DIR="$PEERS" CLAUDE_CODE_SESSION_ID=sess-b $LOCK check fixture 2>&1); r20a=$?
o20c=$(CLAUDE_CONFIG_DIR="$PEERS" CLAUDE_CODE_SESSION_ID=sess-b $LOCK who 2>&1); r20c=$?
printf '{"pid":4194300,"sessionId":"sess-a","name":"peer-a","kind":"interactive"}' > "$PEERS/sessions/$LIVE_PID.json"   # registry says peer-a, but that pid is gone
o20b=$(CLAUDE_CONFIG_DIR="$PEERS" CLAUDE_CODE_SESSION_ID=sess-b $LOCK check fixture 2>&1); r20b=$?
ok=0; [ $r20a -eq 1 ] && grep -q "^LOCKED fixture" <<<"$o20a" && grep -q "Holder: peer-a · live" <<<"$o20a" && grep -q "SendMessage to 'peer-a'" <<<"$o20a" \
  && grep -q "GONE" <<<"$o20b" && grep -q "orphaned" <<<"$o20b" \
  && [ $r20c -eq 0 ] && grep -q "^fixture " <<<"$o20c" && grep -q "peer-a · live" <<<"$o20c" && ok=1
LAST_OUT="live: $(grep -o 'Holder: [^—]*' <<<"$o20a" | head -1 | cut -c1-80) | dead pid: $(grep -o 'Holder: [^—]*' <<<"$o20b" | head -1 | cut -c1-80) | who rows: $(grep -c ' · ' <<<"$o20c")"
record 20 $ok "lock-holder view: LOCKED -> 'peer-a · live' + SendMessage target; dead pid -> GONE/orphaned; lock.py who lists the lock with its holder"
CLAUDE_CODE_SESSION_ID=sess-a $LOCK release fixture --allow-dirty "fixture test" >>"$TR" 2>&1

section "21. Reader identity (menu-only session): lock.py reader binds a window with no folder; Edit still denied; Stop passes; claim upgrades the binding to the real window"
o21a=$(CLAUDE_CODE_SESSION_ID=sess-r $LOCK reader --hint menu 2>&1); r21a=$?
o21w=$(CLAUDE_CODE_SESSION_ID=sess-r $LOCK whoami 2>&1); r21w=$?
hook_edit sess-r fixture/f.txt; r21b=$LAST_RC; o21b="$LAST_OUT"
hook_stop sess-r p21; r21c=$LAST_RC; o21c="$LAST_OUT"
GL="$FLSTATE/guard_log.jsonl"; n21c=$(grep -c "reader identity menu-.*nothing to sign off" "$GL" 2>/dev/null || echo 0)
o21d=$(CLAUDE_CODE_SESSION_ID=sess-r $LOCK claim fixture --task "R claims fixture after browsing" --hint window-r 2>&1); r21d=$?
WIN_R=$(CLAUDE_CODE_SESSION_ID=sess-r $LOCK whoami | sed -n 's/^window=\([^ ]*\).*/\1/p')
ok=0; [ $r21a -eq 0 ] && grep -q "^READER menu-" <<<"$o21a" && grep -q "^READER menu-" <<<"$o21w" \
  && [ $r21b -ne 0 ] && grep -q "reader identity" <<<"$o21b" \
  && [ $r21c -eq 0 ] && [ "$n21c" -ge 1 ] \
  && [ $r21d -eq 0 ] && grep -q "^CLAIMED fixture" <<<"$o21d" && [[ "$WIN_R" == window-r-* ]] && ok=1
LAST_OUT="reader: $(first_line "$o21a" | cut -c1-60) | edit: exit $r21b (reader named: $(grep -c 'reader identity' <<<"$o21b")) | stop: exit $r21c, guard_log pass lines $n21c | claim: exit $r21d -> window $WIN_R"
record 21 $ok "reader identity: READER window, edit denied (reader), stop passes, claim replaces the reader binding with window-r-*"
CLAUDE_CODE_SESSION_ID=sess-r $LOCK release fixture --allow-dirty "fixture test" >>"$TR" 2>&1

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
[ "$ALL" = 1 ] && log "ALL ${#RESULTS[@]} SCENARIOS PASS" || log "SOME SCENARIOS FAILED"
[ "$ALL" = 1 ]
