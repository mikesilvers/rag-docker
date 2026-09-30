#!/usr/bin/env bash
# Tests scripts/verify/lock.sh's mutual-exclusion behaviour (#95, #97).
#
# T1-T4, T6-T8 need no stack: they exercise lock.sh directly through a scratch
# RAG_VERIFY_LOCK path, never the real /tmp/rag-verify.lock. T5 needs a live
# stack (01_infrastructure.sh calls require_stack) and is skipped without one.
#
# Run: bash scripts/tests/test_verify_lock.sh
set -uo pipefail
cd "$(dirname "$0")/../.." || exit 2
REPO_ROOT="$(pwd)"
VERIFY="$REPO_ROOT/scripts/verify"

TESTLOCK_HOME=$(mktemp -d "${TMPDIR:-/tmp}/rag-lock-test.XXXXXX")
cleanup() { rm -rf "$TESTLOCK_HOME"; }
trap cleanup EXIT

# Borrow lib.sh's check/check_eq/skip/section/summary helpers without taking
# the real lock: point RAG_VERIFY_LOCK at a scratch path first, then drop what
# sourcing it acquired so the tests below start from a clean slate.
# The lock a real verify run uses; T5 takes it.
REAL_LOCK="${RAG_VERIFY_LOCK:-/tmp/rag-verify.lock}"
export RAG_VERIFY_LOCK="$TESTLOCK_HOME/harness.lock"
# shellcheck disable=SC1091
. "$VERIFY/lib.sh"
unset RAG_VERIFY_LOCK_HELD
rm -rf "$RAG_VERIFY_LOCK"

section "verify/lock.sh — mutual exclusion"

# --- T1: a second run is refused with exit 3, naming the holder's pid. -----
L1="$TESTLOCK_HOME/t1.lock"
RAG_VERIFY_LOCK="$L1" bash -c '. "'"$VERIFY"'/lock.sh"; sleep 5' &
holder_pid=$!
waited=0
while [ ! -f "$L1/pid" ] && [ "$waited" -lt 50 ]; do sleep 0.1; waited=$((waited + 1)); done
held_pid=$(cat "$L1/pid" 2>/dev/null || true)
check_eq "T1: lock file records the holder's own pid" "$held_pid" "$holder_pid"

out1=$(RAG_VERIFY_LOCK="$L1" bash -c '. "'"$VERIFY"'/lock.sh"; echo ran' 2>&1)
rc1=$?
check_eq "T1: a second run while the lock is held exits 3" "$rc1" "3"
case "$out1" in
  *"$held_pid"*) named=0 ;;
  *) named=1 ;;
esac
check "T1: the refusal names the holder's pid" "$named" "expected pid $held_pid in: $out1"
case "$out1" in
  *ran*) leaked=1 ;;
  *) leaked=0 ;;
esac
check "T1: the second run's own commands never execute" "$leaked" "output: $out1"

wait "$holder_pid" 2>/dev/null

# --- T2: a child suite started under an already-held lock still runs. ------
L2="$TESTLOCK_HOME/t2.lock"
out2=$(RAG_VERIFY_LOCK="$L2" bash -c '
  . "'"$VERIFY"'/lock.sh"
  RAG_VERIFY_LOCK="'"$L2"'" bash -c ". \"'"$VERIFY"'/lock.sh\"; echo child-ran"
' 2>&1)
rc2=$?
check_eq "T2: a suite sourced under an already-held lock (RAG_VERIFY_LOCK_HELD) exits 0" "$rc2" "0"
case "$out2" in
  *child-ran*) ran=0 ;;
  *) ran=1 ;;
esac
check "T2: the child suite's own commands ran (not refused as a second holder)" "$ran" "output: $out2"

# --- T3: the lock is released when the holder exits. ------------------------
L3="$TESTLOCK_HOME/t3.lock"
RAG_VERIFY_LOCK="$L3" bash -c '. "'"$VERIFY"'/lock.sh"; exit 0'
[ ! -e "$L3" ]
check "T3: the lock directory is gone after the holder exits normally" "$?" "still present: $L3"

# --- T4: a stale lock (dead pid) is taken over, not refused. ---------------
L4="$TESTLOCK_HOME/t4.lock"
mkdir "$L4"
bash -c 'exit 0' &
dead_pid=$!
wait "$dead_pid" 2>/dev/null
echo "$dead_pid" >"$L4/pid"
out4=$(RAG_VERIFY_LOCK="$L4" bash -c '. "'"$VERIFY"'/lock.sh"; echo took-over; cat "'"$L4"'/pid"' 2>&1)
rc4=$?
check_eq "T4: sourcing lock.sh against a stale (dead-pid) lock exits 0" "$rc4" "0"
case "$out4" in
  *took-over*) took=0 ;;
  *) took=1 ;;
esac
check "T4: the run proceeds past the stale lock instead of being refused" "$took" "output: $out4"
new_pid=$(printf '%s\n' "$out4" | tail -n1)
[ "$new_pid" != "$dead_pid" ] && [ -n "$new_pid" ]
check "T4: the pid file is rewritten with the new holder's pid" "$?" "old=$dead_pid new=$new_pid"

# --- T5: 01_infrastructure.sh sets its own EXIT trap after lock.sh's, so it
# releases the lock itself; a standalone run must leave no lock behind, and a
# next run must go ahead. Needs a live stack, and runs the real 01 (which
# drops Vfy* collections), so it holds the real verify lock throughout: it is
# skipped while a real verify run holds it, and a run started during T5 waits
# for it instead of losing its collections.
section "verify/lock.sh vs. 01_infrastructure.sh's own EXIT trap"
API="${RAG_API:-http://localhost:8080/api}"
health_code=$(curl -s -o /dev/null -m 10 -w '%{http_code}' "$API/health" 2>/dev/null)
L5="$TESTLOCK_HOME/t5.lock"
T5_OUT="$TESTLOCK_HOME/t5.out"
if [ "$health_code" != "200" ]; then
  skip "T5: 01_infrastructure.sh EXIT-trap interaction" "no live stack at $API (HTTP $health_code)"
else
  # The outer shell takes the real lock, then runs 01 as a standalone suite on
  # its own scratch lock. 01 inspects the stack's containers through compose,
  # which would otherwise name the project after whatever folder this is in.
  held5=$(RAG_VERIFY_LOCK="$REAL_LOCK" RAG_API="$API" COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-rag-docker}" \
    bash -c '. "'"$VERIFY"'/lock.sh" 2>/dev/null
      echo held
      env -u RAG_VERIFY_LOCK_HELD RAG_VERIFY_LOCK="'"$L5"'" bash "'"$VERIFY"'/01_infrastructure.sh" >"'"$T5_OUT"'" 2>&1
      echo "rc=$?"')
fi
if [ "$health_code" = "200" ] && [ "$held5" = "${held5#held}" ]; then
  skip "T5: 01_infrastructure.sh EXIT-trap interaction" "a real verify run holds $REAL_LOCK"
elif [ "$health_code" = "200" ]; then
  rc5=${held5##*rc=}
  [ "$rc5" = 0 ]
  check "T5: 01_infrastructure.sh run standalone completes" "$?" "exit $rc5; see $T5_OUT"
  [ ! -e "$L5" ]
  check "T5: 01's own EXIT trap releases the lock" "$?" "expected $L5 to be gone"
  out5=$(RAG_VERIFY_LOCK="$L5" bash -c '. "'"$VERIFY"'/lock.sh"; echo second-run-ok' 2>&1)
  rc5b=$?
  check_eq "T5: a second run afterwards succeeds" "$rc5b" "0"
  case "$out5" in
    *second-run-ok*) ok5=0 ;;
    *) ok5=1 ;;
  esac
  check "T5: the second run actually proceeds" "$ok5" "output: $out5"
fi

# --- T6: a symlinked lock path is refused, and never followed or removed. --
# The PR's security fix for "rm -rf on a path from the environment": the lock
# is never deleted wholesale, and a symlink at RAG_VERIFY_LOCK is refused
# outright rather than mkdir/rm-ing through it.
section "verify/lock.sh — symlink and pre-existing-folder refusal"
L6_TARGET="$TESTLOCK_HOME/t6_target"
mkdir "$L6_TARGET"
echo sentinel >"$L6_TARGET/keepme"
L6="$TESTLOCK_HOME/t6.lock"
ln -s "$L6_TARGET" "$L6"
out6=$(RAG_VERIFY_LOCK="$L6" bash -c '. "'"$VERIFY"'/lock.sh"; echo ran' 2>&1)
rc6=$?
check_eq "T6: sourcing lock.sh against a symlinked lock path exits 3" "$rc6" "3"
case "$out6" in
  *ran*) leaked6=1 ;;
  *) leaked6=0 ;;
esac
check "T6: the second run's own commands never execute" "$leaked6" "output: $out6"
[ -L "$L6" ] && [ -f "$L6_TARGET/keepme" ]
check "T6: the symlink and its target are left untouched (no rm -rf through it)" "$?" "symlink or target file missing"

# --- T7: an existing non-lock folder (files, no pid) is refused, not wiped. -
L7="$TESTLOCK_HOME/t7_existing"
mkdir "$L7"
echo important-data >"$L7/dont-delete-me"
out7=$(RAG_VERIFY_LOCK="$L7" bash -c '. "'"$VERIFY"'/lock.sh"; echo ran' 2>&1)
rc7=$?
check_eq "T7: sourcing lock.sh against an existing folder with no pid file exits 3" "$rc7" "3"
case "$out7" in
  *ran*) leaked7=1 ;;
  *) leaked7=0 ;;
esac
check "T7: the run is refused, never proceeds" "$leaked7" "output: $out7"
[ -f "$L7/dont-delete-me" ]
check "T7: the folder's own file is left untouched" "$?" "expected $L7/dont-delete-me to survive"

# --- T8: many racers over one stale lock — exactly one wins the takeover. --
# The PR's fix for "two runs could take over the same stale lock" relies on a
# second mutex ($lock.takeover); this exercises it under real contention
# instead of trusting the one-at-a-time scenarios in T1-T5.
section "verify/lock.sh — concurrent stale-lock takeover"
RACERS=12
ROUNDS=3
race_bad=0
for round in $(seq 1 "$ROUNDS"); do
  L8="$TESTLOCK_HOME/t8_round$round.lock"
  mkdir "$L8"
  bash -c 'exit 0' &
  dead8=$!
  wait "$dead8" 2>/dev/null
  echo "$dead8" >"$L8/pid"

  WIN8="$TESTLOCK_HOME/t8_win.$round"
  ERR8="$TESTLOCK_HOME/t8_err.$round"
  : >"$WIN8"; : >"$ERR8"
  racer_pids=()
  for i in $(seq 1 "$RACERS"); do
    (
      # The winner must hold the lock past the other racers' attempts, or a
      # racer still cycling through its retry loop can win it fresh *after*
      # the first winner already released it — a second, legitimate,
      # sequential acquisition, not a broken mutex. Without this hold this
      # test cannot tell "two winners of one contended instant" (a real bug)
      # apart from "one winner, then another after the first let go" (not a
      # bug at all).
      racer_out=$(RAG_VERIFY_LOCK="$L8" bash -c '. "'"$VERIFY"'/lock.sh"; echo won; sleep 0.4' 2>&1)
      racer_rc=$?
      oneline=$(printf '%s' "$racer_out" | tr '\n' ' ')
      if [ "$racer_rc" = 0 ]; then
        printf '%s\n' "$oneline" >>"$WIN8"
      else
        printf '%s\n' "$oneline" >>"$ERR8"
      fi
    ) &
    racer_pids+=($!)
  done
  for p in "${racer_pids[@]}"; do wait "$p"; done

  winners=$(wc -l <"$WIN8" | tr -d ' ')
  refused=$(wc -l <"$ERR8" | tr -d ' ')
  if [ "$winners" != "1" ] || [ "$((winners + refused))" != "$RACERS" ] || [ -d "$L8.takeover" ]; then
    race_bad=1
    printf '    round %s: winners=%s refused=%s total=%s/%s takeover_leaked=%s\n' \
      "$round" "$winners" "$refused" "$((winners + refused))" "$RACERS" "$([ -d "$L8.takeover" ] && echo yes || echo no)" >&2
  fi
done
check "T8: every round of $RACERS racers on one stale lock had exactly one winner, all accounted for, no leaked takeover mutex ($ROUNDS rounds)" "$race_bad" "see stderr for the failing round(s)"


# --- T9: an EXIT trap set before lock.sh is sourced still runs. -----------
section "verify/lock.sh — EXIT trap chaining and pid-less locks"
L9="$TESTLOCK_HOME/t9.lock"; M9="$TESTLOCK_HOME/t9.marker"
RAG_VERIFY_LOCK="$L9" bash -c 'trap "touch '"$M9"'" EXIT; . "'"$VERIFY"'/lock.sh"; true'
[ -e "$M9" ]
check "T9: an earlier EXIT trap still runs after lock.sh sets its own" "$?"
[ ! -e "$L9" ]
check "T9: and the lock is still released" "$?"

# --- T10: a pid-less lock older than a minute is taken over. --------------
L10="$TESTLOCK_HOME/t10.lock"; mkdir "$L10"
touch -t "$(date -v-5M +%Y%m%d%H%M 2>/dev/null || date -d '-5 min' +%Y%m%d%H%M)" "$L10"
out10=$(RAG_VERIFY_LOCK="$L10" bash -c '. "'"$VERIFY"'/lock.sh"; echo t10-ok' 2>&1)
case "$out10" in *t10-ok*) ok10=0 ;; *) ok10=1 ;; esac
check "T10: a pid-less lock older than a minute is taken over" "$ok10" "output: $out10"

# --- T11: a lock whose pid file isn't a number is refused, not taken over. -
L11="$TESTLOCK_HOME/t11.lock"; mkdir "$L11"; echo "not-a-pid" > "$L11/pid"
RAG_VERIFY_LOCK="$L11" bash -c '. "'"$VERIFY"'/lock.sh"; echo ran' >/dev/null 2>&1
check_eq "T11: a non-numeric pid is refused with exit 3" "$?" "3"
[ "$(cat "$L11/pid" 2>/dev/null)" = "not-a-pid" ]
check "T11: and the lock is left as it was" "$?"

# --- T12: a lock whose pid can't be written is removed and refused. -------
# umask 222 makes the just-created lock directory unwritable to its own owner,
# so the pid write fails without racing anything. (Written by the #105
# testing reviewer.)
L12="$TESTLOCK_HOME/t12.lock"
out12=$(umask 222; RAG_VERIFY_LOCK="$L12" bash -c '. "'"$VERIFY"'/lock.sh"; echo ran' 2>&1)
rc12=$?
check_eq "T12: a lock whose pid can't be written exits 3" "$rc12" "3"
case "$out12" in *ran*) leaked12=1 ;; *) leaked12=0 ;; esac
check "T12: the run is refused, never proceeds" "$leaked12" "output: $out12"
[ ! -e "$L12" ]
check "T12: the unwritable lock directory is removed, not left pid-less" "$?" "expected $L12 to be gone"

# --- T13: an earlier EXIT trap whose command contains quotes still runs. ---
# T9's trap text has no quote in it; here the stored command itself does, and
# the path has a space, which is why such traps quote it.
L13="$TESTLOCK_HOME/t13.lock"; M13="$TESTLOCK_HOME/t13 marker"
out13=$(RAG_VERIFY_LOCK="$L13" M13="$M13" bash -c 'trap "touch '\''$M13'\''; echo \"it'\''s done\"" EXIT; . "'"$VERIFY"'/lock.sh"; echo "args:$*"' _ one two 2>&1)
[ -e "$M13" ]
check "T13: an earlier EXIT trap with quotes in its command still runs" "$?" "output: $out13"
case "$out13" in *"it's done"*) ok13=0 ;; *) ok13=1 ;; esac
check "T13: all of that trap's command runs" "$ok13" "output: $out13"
case "$out13" in *"args:one two"*) args13=0 ;; *) args13=1 ;; esac
check "T13: the sourcing script's own arguments are left alone" "$args13" "output: $out13"
[ ! -e "$L13" ]
check "T13: and the lock is still released" "$?"

# --- T14: a pid-less lock younger than a minute is waited on, not taken. ---
# (The same age check is repeated under the takeover mutex, for a fresh lock
# made in place of an old one between the two checks; that race isn't
# reproducible here without a test hook in lock.sh.)
L14="$TESTLOCK_HOME/t14.lock"; mkdir "$L14"
RAG_VERIFY_LOCK="$L14" bash -c '. "'"$VERIFY"'/lock.sh"; echo ran' >/dev/null 2>&1
rc14=$?
[ "$rc14" != 0 ] && [ -d "$L14" ] && [ ! -e "$L14/pid" ]
check "T14: a fresh pid-less lock is left alone" "$?" "exit $rc14"

summary
