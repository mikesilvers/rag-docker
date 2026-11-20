#!/usr/bin/env bash
# Encrypted backup of this stack's data volumes (#848).
#
# Each volume is streamed through tar and age in a container and written as
# <volume>-<timestamp>.tgz.age, encrypted to a new age X25519 key pair of its
# own; the secret key goes in <volume>-<timestamp>.key beside it (mode 0600).
# Nothing unencrypted is written. The stack's running containers are stopped
# first (never removed: no `down`, no `-v`). See README "Backing up and
# restoring".
#
# Usage: scripts/backup.sh [--prefix <name>] [--dir <folder>] [--yes]
set -u
umask 077
. "$(dirname "$0")/backup-common.sh"

usage() {
  cat <<USAGE
Usage: scripts/backup.sh [--prefix <name>] [--dir <folder>] [--yes]

  --prefix <name>  the compose project whose volumes to back up (the part of the
                   volume names before '_'); detected when not given
  --dir <folder>   where to create the backup folder (default ~/rag-docker-backups)
  --yes            don't ask for confirmation (needs --prefix)
USAGE
}

given_prefix=
dir=
while [ $# -gt 0 ]; do
  case "$1" in
    --prefix) [ $# -ge 2 ] || { usage >&2; exit 2; }; given_prefix=$2; shift 2 ;;
    --dir) [ $# -ge 2 ] || { usage >&2; exit 2; }; dir=$2; shift 2 ;;
    --yes) RB_YES=yes; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done
[ "$RB_YES" = yes ] && [ -z "$given_prefix" ] && rb_die "--yes needs --prefix <name>, so a detected prefix is never used unconfirmed"

rb_need_docker
rb_image
rb_prefix "$given_prefix"

found=
for v in $RB_VOLUMES; do
  if rb_exists "$v"; then
    found="$found $v"
  else
    echo "$v: no volume ${RB_PREFIX}_$v, skipped"
  fi
done
if [ -z "$found" ]; then
  candidates=$(rb_candidates)
  echo "$RB_NAME: no volume of project '$RB_PREFIX'. Nothing was changed." >&2
  echo "Projects with rag-docker volumes: ${candidates:-none}" >&2
  echo "Pass the right one with --prefix <name>." >&2
  exit 1
fi

parent=${dir:-$HOME/rag-docker-backups}
abs=$(rb_abspath "$parent") || rb_die "can't use '$parent' (a '.' or '..' in a folder that doesn't exist yet)"
case "$abs" in *:*) rb_die "the backup folder's path can't contain ':' (docker's mount separator): $abs" ;; esac
case "$abs/" in
  "$RB_ROOT/backups/"*) ;;
  "$RB_ROOT/"*) rb_die "$abs is inside the git checkout; write backups outside it (the default is ~/rag-docker-backups) or under its ignored backups/ folder" ;;
esac
[ ! -e "$abs" ] || [ -d "$abs" ] || rb_die "$abs exists and is not a folder"
ts=$(date -u +%Y%m%dT%H%M%SZ)
run="$abs/${RB_PREFIX}-$ts"
[ ! -e "$run" ] || rb_die "$run already exists"

echo "Back up project '$RB_PREFIX' into $run:"
for v in $found; do
  n=$(rb_count "$v") || rb_die "could not read volume ${RB_PREFIX}_$v"
  rb_volume_list_line "$v" "$n"
done
echo "The stack's running containers will be stopped (not removed)."
rb_confirm "Go on?"

mkdir -p "$abs" && mkdir "$run" || rb_die "could not create $run"
rb_stop

failed=
for v in $found; do
  key="$run/$v-$ts.key"
  arc="$run/$v-$ts.tgz.age"
  tmp="$arc.tmp"
  # A new key pair for this volume only: the secret key goes straight into its
  # file, and only the recipient (the public key) is passed to the next step.
  if (set -o noclobber; rb_run "$RB_IMAGE" age-keygen 2>/dev/null >"$key") &&
    recipient=$(rb_run -i "$RB_IMAGE" age-keygen -y <"$key") &&
    case "$recipient" in age1*) true ;; *) false ;; esac &&
    rb_run -v "${RB_PREFIX}_$v:/data:ro" "$RB_IMAGE" \
      sh -c 'set -o pipefail; tar czf - -C /data . | age -r "$1"' sh "$recipient" >"$tmp" &&
    n=$(rb_run -i -v "$key:/run/age.key:ro" "$RB_IMAGE" \
      sh -c 'set -o pipefail; age -d -i /run/age.key | tar tzf - | wc -l' <"$tmp") &&
    mv "$tmp" "$arc"; then
    echo "$v: $(echo "$n" | tr -d ' ') entries -> $arc, key $key"
  else
    echo "$v: backup FAILED"
    rm -f "$tmp" "$key"
    failed="$failed $v"
  fi
done

cat <<DONE

Backup folder: $run

KEEP THE KEYS APART. Each archive (.tgz.age) opens only with its own key file
(.key). Anyone who has both can read everything in that volume, password hashes
and sessions included. Move the .key files somewhere other than the archives:
another disk, or a password manager. Without its key an archive can't be
restored.

Delete backups you no longer need, keys included, for example:
  rm -r "$run"

To restore, give each archive with its key:
  scripts/restore.sh --prefix $RB_PREFIX <volume>-$ts.tgz.age <volume>-$ts.key ...

DONE
rb_start_hint
if [ -n "$failed" ]; then
  echo "$RB_NAME: backup FAILED for:$failed" >&2
  exit 1
fi
