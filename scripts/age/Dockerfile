# The age tool for scripts/backup.sh and scripts/restore.sh (#848).
#
# The scripts build this image on first use, tagged rag-docker-age:<the age
# package version below>-<the first 12 hex digits of a SHA-256 of this file's
# parser directives and instruction lines (other comments and blank lines left
# out) and of every other file in this folder (rb_age_text in
# scripts/backup-common.sh)>, so a change of any of them builds a new image;
# the build needs the network once. Any parser directive goes above this
# comment: Docker reads directives only at the very top.
# package-offline.sh builds it too and carries it in the offline bundle. Every
# backup and restore runs age and tar in a container of it, so nothing
# unencrypted leaves the container.
#
# The base is the alpine the README's 1.1.0 upgrade blocks pin, by version and
# digest. Alpine keeps only the newest revision of a package in a release, so
# when it replaces this one the build fails, and no backup is written: bump the
# pin to the revision `apk policy age` shows in this alpine.
FROM alpine:3.24.4@sha256:f59abe656b692d901e184be3445e084786602108599d2a51d229c0f5a48c71bf
RUN apk add --no-cache age=1.3.2-r1
