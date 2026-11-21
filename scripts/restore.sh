#!/usr/bin/env bash
# Restore volumes from encrypted backups made by scripts/backup.sh (#848).
#
# Takes each archive with its key file. Every archive is first decrypted with
# its key and read to its end; a missing or wrong key, or a damaged or
# incomplete archive, stops the script before anything is stopped or emptied.
# Only then are the stack's running containers stopped (never removed) and each
# volume replaced by its archive's contents, decrypted while streaming into a
# staging folder in the volume and swapped in only once that has succeeded. See
# README "Backing up and restoring".
#
# Usage: scripts/restore.sh [--prefix <name>] [--yes] <archive.tgz.age> <key> [<archive.tgz.age> <key> ...]
set -u
umask 077
. "$(dirname "$0")/backup-common.sh"

usage() {
  cat <<USAGE
Usage: scripts/restore.sh [--prefix <name>] [--yes] <archive.tgz.age> <key> [<archive.tgz.age> <key> ...]

  --prefix <name>  the compose project whose volumes to restore; detected when not given
  --yes            don't ask for confirmation (needs --prefix)

Each archive restores the volume its name starts with (<volume>-<timestamp>.tgz.age),
which is emptied first. Give every archive with its own key file.
USAGE
}

nothing() {
  echo "$RB_NAME: $*" >&2
  echo "Nothing was stopped, emptied or restored." >&2
  exit 1
}

given_prefix=
while [ $# -gt 0 ]; do
  case "$1" in
    --prefix) [ $# -ge 2 ] || { usage >&2; exit 2; }; given_prefix=$2; shift 2 ;;
    --yes) RB_YES=yes; shift ;;
    -h|--help) usage; exit 0 ;;
    --) shift; break ;;
    -*) usage >&2; exit 2 ;;
    *) break ;;
  esac
done
if [ $# -eq 0 ] || [ $(($# % 2)) -ne 0 ]; then
  echo "$RB_NAME: give each archive followed by its key file." >&2
  usage >&2
  exit 2
fi
[ "$RB_YES" = yes ] && [ -z "$given_prefix" ] && rb_die "--yes needs --prefix <name>, so a detected prefix is never used unconfirmed"

# The pairs, as lines "<volume><tab><archive><tab><key>" with absolute paths.
tab=$(printf '\t')
pairs=
seen=" "
while [ $# -gt 0 ]; do
  archive=$1
  key=$2
  shift 2
  base=$(basename "$archive")
  v=${base%%-*}
  case " $RB_VOLUMES " in *" $v "*) ;; *) nothing "$archive: not a backup archive name (<volume>-<timestamp>.tgz.age, <volume> one of: $RB_VOLUMES)" ;; esac
  case "$base" in "$v"-*.tgz.age) ;; *) nothing "$archive: not a backup archive name (<volume>-<timestamp>.tgz.age)" ;; esac
  case "$seen" in *" $v "*) nothing "two archives for volume $v" ;; esac
  seen="$seen$v "
  [ -f "$archive" ] && [ -r "$archive" ] || nothing "$archive: no such readable file"
  [ -f "$key" ] && [ -r "$key" ] || nothing "key file for $archive missing or unreadable: $key"
  archive=$(rb_abspath "$archive") && key=$(rb_abspath "$key") || nothing "can't resolve the paths of $base and its key"
  case "$archive$key" in *:*) nothing "the paths of an archive and its key can't contain ':' (docker's mount separator)" ;; esac
  case "$archive$key" in *"$tab"*|*"
"*) nothing "the paths of an archive and its key can't contain a tab or a line break" ;; esac
  pairs="$pairs$v$tab$archive$tab$key
"
done

rb_need_docker
rb_image
rb_prefix "$given_prefix"

missing=
for v in $(printf '%s' "$pairs" | cut -f1); do
  rb_exists "$v" || missing="$missing ${RB_PREFIX}_$v"
done
if [ -n "$missing" ]; then
  candidates=$(rb_candidates)
  echo "Projects with rag-docker volumes: ${candidates:-none}" >&2
  nothing "no such volume:$missing. Pass the right --prefix, or create the stack's volumes first with 'docker compose create' in $RB_ROOT (in LAN mode: docker compose -f docker-compose.yml -f docker-compose.lan.yml create)."
fi

echo "Checking each archive with its key..."
printf '%s' "$pairs" | while IFS="$tab" read -r v archive key; do
  # stderr is kept to show it on failure: age's and Docker's messages hold no
  # secret, and stdout (the listing) is discarded.
  if err=$(rb_run -i -v "$key:/run/age.key:ro" "$RB_IMAGE" \
    sh -c 'set -o pipefail; age -d -i /run/age.key | tar tzf - >/dev/null' <"$archive" 2>&1 >/dev/null); then
    echo "  $v: OK"
  else
    echo "$RB_NAME: $archive failed its check with $key: a wrong key, a damaged or incomplete archive, or a Docker error." >&2
    rb_show_error "$err"
    exit 1
  fi
done || nothing "an archive failed its check."

echo "Restore project '$RB_PREFIX'. These volumes will be EMPTIED and replaced:"
printf '%s' "$pairs" | while IFS="$tab" read -r v archive key; do
  echo "  ${RB_PREFIX}_$v  <-  $archive"
done
echo "The stack's running containers will be stopped (not removed)."
rb_confirm "Go on?"

rb_stop

# Each archive is extracted into a new staging folder inside its volume first.
# Only when that has succeeded are the volume's other entries deleted and the
# staged ones moved up by rename (same filesystem: no space needed, nothing
# half-copied), and the volume's root given the archive root's mode and owner,
# as an extraction in place would. A failed extraction removes the staging
# folder and exits 3: the volume is unchanged. A staging folder left by an
# interrupted earlier restore goes with the old contents; a backup skips it
# (#856).
STAGED_RESTORE='set -o pipefail
s=$(mktemp -d /data/.rag-docker-restore.XXXXXX) || exit 3
if ! age -d -i /run/age.key | tar xzf - -C "$s"; then rm -rf "$s"; exit 3; fi
find /data -mindepth 1 -maxdepth 1 ! -path "$s" -exec rm -rf {} \; || exit 4
[ -z "$(find /data -mindepth 1 -maxdepth 1 ! -path "$s")" ] || exit 4
find "$s" -mindepth 1 -maxdepth 1 -exec mv {} /data/ \; || exit 4
chown "$(stat -c %u:%g "$s")" /data && chmod "$(stat -c %a "$s")" /data && rmdir "$s" || exit 4'

printf '%s' "$pairs" | while IFS="$tab" read -r v archive key; do
  rb_run -i -v "${RB_PREFIX}_$v:/data" -v "$key:/run/age.key:ro" "$RB_IMAGE" \
    sh -c "$STAGED_RESTORE" <"$archive"
  rc=$?
  if [ "$rc" = 0 ]; then
    echo "$v: restored"
  elif [ "$rc" = 3 ]; then
    echo "$v: restore FAILED: the archive could not be extracted; this volume is unchanged. The archives are unchanged; fix the cause and run the restore again." >&2
    exit 1
  else
    echo "$v: restore FAILED: this volume may be partly restored. The archives are unchanged; fix the cause and run the restore again." >&2
    exit 1
  fi
done || exit 1

rb_start_hint
