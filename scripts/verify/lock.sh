#!/usr/bin/env bash
# One verify run at a time against a stack.
#
# Sourced by all.sh and lib.sh, never executed. Every run shares the $PREFIX
# collection names, the /tmp/vfy_*.json scratch files and the fixtures folder,
# which all.sh deletes and rebuilds when it starts. Two runs at once overwrite
# each other: one run's upload finds its fixture gone, or its chunks land in the
# other run's recreated collection (#95).
#
# The first script to source this holds the lock for its whole process tree:
# it exports RAG_VERIFY_LOCK_HELD and the lock's path, RAG_VERIFY_LOCK, so the
# suites all.sh starts don't try again. An inherited RAG_VERIFY_LOCK_HELD is
# trusted only while the lock's pid is this shell or one of its ancestors
# (#184). A suite orphaned by a run that died still carries the variable; it
# exits 3 here instead of acting on a later run's verify project, and never
# takes the dead holder's lock over. lib.sh's API helpers repeat the check
# before every request, for a suite that was already past this point.
#
# The lock is a directory holding a pid file. mkdir is atomic, so only one run
# can create it. A lock whose holder has died is taken over, but only under a
# second mutex ($lock.takeover, also a mkdir) and only if the pid is still the
# dead one, so of several runs that find the same stale lock, exactly one clears
# it and the rest retry. A lock with no pid file yet is treated as held: its
# owner is between mkdir and writing the pid. Only once it is over a minute old,
# still checked under the mutex, is it taken over as a run that died there.
# The lock is only ever removed file by file (pid, then rmdir), never with
# rm -rf, because its path can come from the environment.

_rag_lock_release() {
  [ -n "${RAG_VERIFY_LOCK:-}" ] || return 0
  [ "$(cat "$RAG_VERIFY_LOCK/pid" 2>/dev/null)" = "$$" ] || return 0
  rm -f "$RAG_VERIFY_LOCK/pid"
  rmdir "$RAG_VERIFY_LOCK" 2>/dev/null || true
}

# Succeeds only when the lock's pid is this shell ($$) or an ancestor of it.
# A missing, symlinked or pid-less lock, or a process table that can't be
# read, counts as not ours. The pid == $$ case needs no ps, which the API
# container lacks.
_rag_lock_owner_ok() {
  local lock="${RAG_VERIFY_LOCK:-/tmp/rag-verify.lock}" owner pid steps=0
  [ ! -L "$lock" ] || return 1
  owner=$(cat "$lock/pid" 2>/dev/null) || return 1
  case "$owner" in ''|*[!0-9]*) return 1 ;; esac
  pid=$$
  while [ "$steps" -lt 64 ]; do
    [ "$pid" = "$owner" ] && return 0
    [ "$pid" -gt 1 ] || return 1
    pid=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')
    case "$pid" in ''|*[!0-9]*) return 1 ;; esac
    steps=$((steps + 1))
  done
  return 1
}

_rag_lock_leftover() {
  local lock="${RAG_VERIFY_LOCK:-/tmp/rag-verify.lock}" owner
  owner=$(cat "$lock/pid" 2>/dev/null | LC_ALL=C tr -cd '0-9' | cut -c1-20)
  if [ -n "$owner" ]; then owner="its pid is $owner"; else owner="it has no pid"; fi
  printf '\nThe verify lock %s is not held by this run or its parents (%s). This is a leftover\nof an earlier verify run, so it stops here.\n\n' \
    "$lock" "$owner" >&2
}

_rag_lock_acquire() {
  local lock="$1" holder stale tries=0
  if [ -L "$lock" ]; then
    printf '\nThe verify lock %s is a symlink; refusing to use it.\n\n' "$lock" >&2
    return 3
  fi
  while [ "$tries" -lt 50 ]; do
    tries=$((tries + 1))
    if mkdir "$lock" 2>/dev/null; then
      if echo $$ > "$lock/pid.$$" 2>/dev/null && mv "$lock/pid.$$" "$lock/pid" 2>/dev/null; then
        return 0
      fi
      # Couldn't record our pid (a full /tmp, say). Don't leave a lock that
      # nothing could ever recognise as stale.
      rm -f "$lock/pid.$$" 2>/dev/null; rmdir "$lock" 2>/dev/null || true
      printf '\nCould not write the verify lock %s.\n\n' "$lock" >&2
      return 3
    fi
    holder=$(cat "$lock/pid" 2>/dev/null || true)
    if [ -z "$holder" ]; then
      # Normally another run between mkdir and writing its pid, so wait. A
      # pid-less lock older than a minute belongs to a run that died there.
      if [ -z "$(find "$lock" -maxdepth 0 -mmin +1 2>/dev/null)" ]; then
        sleep 0.1; continue
      fi
      holder="none"
    fi
    case "$holder" in
      none) ;;
      *[!0-9]*)
        # Shown, not trusted: printable characters other than the quote, and
        # not much of them.
        holder=$(printf '%s' "$holder" | LC_ALL=C tr -cd '[:print:]' | tr -d '"' | cut -c1-40)
        printf '\nThe verify lock %s holds "%s", not a pid; remove it by hand if no run is using it.\n\n' "$lock" "$holder" >&2
        return 3 ;;
    esac
    if [ "$holder" != none ] && kill -0 "$holder" 2>/dev/null; then
      printf '\nAnother verify run (pid %s) is using this machine. Wait for it to finish.\n\n' "$holder" >&2
      return 3
    fi
    # Stale. Only one run may take it over, so take a second, short-lived
    # mutex first, and re-read the pid under it: another run may already have
    # replaced the stale lock with a live one. For a pid-less lock that means
    # re-checking its age too, since a replacement is pid-less for a moment.
    if mkdir "$lock.takeover" 2>/dev/null; then
      local now
      now=$(cat "$lock/pid" 2>/dev/null || true)
      if [ ! -L "$lock" ] && { [ "$now" = "$holder" ] || { [ "$holder" = none ] && [ -z "$now" ] &&
           [ -n "$(find "$lock" -maxdepth 0 -mmin +1 2>/dev/null)" ]; }; }; then
        rm -f "$lock/pid" "$lock"/pid.* 2>/dev/null
        rmdir "$lock" 2>/dev/null || true
      fi
      rmdir "$lock.takeover" 2>/dev/null || true
    else
      # Another run is taking it over. A takeover mutex older than a minute
      # belongs to a run that died mid-takeover.
      if [ -n "$(find "$lock.takeover" -maxdepth 0 -mmin +1 2>/dev/null)" ]; then
        rmdir "$lock.takeover" 2>/dev/null || true
      fi
      sleep 0.1
    fi
  done
  printf '\nCould not take the verify lock %s.\n\n' "$lock" >&2
  return 3
}

if [ -n "${RAG_VERIFY_LOCK_HELD:-}" ]; then
  _rag_lock_owner_ok || { _rag_lock_leftover; exit 3; }
else
  RAG_VERIFY_LOCK="${RAG_VERIFY_LOCK:-/tmp/rag-verify.lock}"
  _rag_lock_acquire "$RAG_VERIFY_LOCK" || exit 3
  export RAG_VERIFY_LOCK RAG_VERIFY_LOCK_HELD=1
  # Released on exit, after any EXIT trap already set (bash traps replace
  # rather than chain, so keep the earlier one). A suite that sets its own
  # EXIT trap later should call _rag_lock_release in it; if it doesn't, the
  # next run takes the lock over once this pid is gone.
  # `trap -p` prints the command shell-quoted (trap -- '<command>' EXIT), so
  # let the shell unquote it rather than stripping quotes as text, which broke
  # any command that itself contains a quote. A function, not `set --`, so the
  # sourcing script's own arguments are left alone.
  _rag_prev_exit=$(trap -p EXIT)
  _rag_trap_command() { _rag_prev_exit=$3; }
  if [ -n "$_rag_prev_exit" ]; then eval "_rag_trap_command $_rag_prev_exit"; fi
  unset -f _rag_trap_command
  trap "_rag_lock_release; ${_rag_prev_exit:-:}" EXIT
fi
