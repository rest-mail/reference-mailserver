#!/bin/sh
# Gives one daemon the certificates it serves with, and nothing else (#9).
#
# The testbed issues every certificate into one volume (testbed_certs) that also
# holds its CA's private key, ca.key. A daemon that mounted it could sign
# certificates every testbed peer trusts, and read every other instance's key,
# so no daemon does. Instead, right before a daemon starts, this copies the
# files it names out of the issuing volume, which it mounts read-only, into a
# volume of the daemon's own, owned by the user that reads them: keys 0400,
# certificates 0444, the directory 0500. This one-off container is the only
# thing here that mounts the issuing volume. The same approach as
# rest-mail-server's tasks/stage-certs.sh (rest-mail/rest-mail-server#380).
#
#   stage-certs.sh <daemon volume> <uid:gid> <issuing volume> <file> ...
#
# Every file has to be there, as a regular file, or the daemon volume is left as
# it was and this fails, so the daemon never starts on half its material.
# ca.key is refused by name.
set -eu

if [ $# -lt 4 ]; then
  echo "usage: $0 <daemon volume> <uid:gid> <issuing volume> <file> ..." >&2
  exit 2
fi
daemon_volume=$1 owner=$2 issuing_volume=$3
shift 3

name_ok() {
  case $1 in
    '' | [!A-Za-z0-9]* | *[!A-Za-z0-9._-]*) return 1 ;;
  esac
}

for volume in "$daemon_volume" "$issuing_volume"; do
  if ! name_ok "$volume"; then
    echo "stage-certs: '$volume' is not a volume name" >&2
    exit 2
  fi
done
# The daemon volume is emptied before it is filled: never the issuing one.
if [ "$daemon_volume" = "$issuing_volume" ]; then
  echo "stage-certs: refusing to stage $issuing_volume into itself" >&2
  exit 2
fi
case $owner in
  *[!0-9:]* | :* | *: | *:*:* | [!0-9]*) owner_ok= ;;
  *:*) owner_ok=1 ;;
  *) owner_ok= ;;
esac
if [ -z "$owner_ok" ]; then
  echo "stage-certs: '$owner' is not <uid>:<gid>" >&2
  exit 2
fi
for f in "$@"; do
  if ! name_ok "$f"; then
    echo "stage-certs: '$f' is not a plain file name" >&2
    exit 2
  fi
  if [ "$f" = ca.key ]; then
    echo "stage-certs: refusing to stage the CA's private key" >&2
    exit 2
  fi
done

# `docker run -v` would quietly create a missing issuing volume, empty.
if ! docker volume inspect "$issuing_volume" >/dev/null 2>&1; then
  echo "stage-certs: there is no volume $issuing_volume to stage certificates from" >&2
  echo "  bring the testbed up first, or point CERTS_VOLUME at the volume your certificates are in" >&2
  exit 1
fi

docker run --rm --network none \
  -v "$issuing_volume:/from:ro" \
  -v "$daemon_volume:/to" \
  alpine:3.20 sh -euc '
  owner=$1
  shift
  missing=0
  for f in "$@"; do
    if [ ! -f "/from/$f" ] || [ -L "/from/$f" ]; then
      echo "  no $f in the issuing volume" >&2
      missing=1
    fi
  done
  [ "$missing" = 0 ] || exit 1
  find /to -mindepth 1 -delete
  for f in "$@"; do
    cp "/from/$f" "/to/$f"
    case $f in *.key) chmod 0400 "/to/$f" ;; *) chmod 0444 "/to/$f" ;; esac
  done
  chown -R "$owner" /to
  chmod 0500 /to
' _ "$owner" "$@" || {
  echo "  could not stage certificates from $issuing_volume into $daemon_volume" >&2
  echo "  issue them first (the testbed's certs volume needs ca.crt and this instance's leaf)" >&2
  exit 1
}
