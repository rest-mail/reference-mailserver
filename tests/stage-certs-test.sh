#!/bin/sh
# Checks, without Docker, that postfix and dovecot are given only their own
# certificates (#9). A stub `docker` on PATH logs every call and runs the
# staging container's script against scratch directories standing in for
# volumes, so `task postfix:up` and `task dovecot:up` run for real up to the
# point a daemon would start. The real-container check is in
# .github/workflows/certs.yml.
#
#   tests/stage-certs-test.sh     (from the repository root)
set -eu

root=$(cd "$(dirname "$0")/.." && pwd)
scratch=$(mktemp -d)
trap 'chmod -R u+w "$scratch" 2>/dev/null; rm -rf "$scratch"' EXIT
mkdir -p "$scratch/bin" "$scratch/volumes/issuing"

cat >"$scratch/bin/docker" <<'EOF'
#!/usr/bin/env python3
# Stub docker: logs the call; runs a staging container's script with its
# /from and /to mounts mapped onto $STUB_VOLUMES/<volume>.
import os, re, subprocess, sys
args = sys.argv[1:]
vols = os.environ["STUB_VOLUMES"]
with open(os.environ["STUB_LOG"], "a") as log:
    log.write(" ".join(args) + "\n")
if args[:2] == ["volume", "inspect"]:
    sys.exit(0 if os.path.isdir(os.path.join(vols, args[2])) else 1)
if args[:1] != ["run"] or "alpine:3.20" not in args:
    sys.exit(0)
mounts = {}
for i, a in enumerate(args):
    if a == "-v":
        src, dst = args[i + 1].split(":")[:2]
        mounts[dst] = os.path.join(vols, src)
i = args.index("alpine:3.20")
script, rest = args[i + 3], args[i + 4:]
os.makedirs(mounts["/to"], exist_ok=True)
subprocess.run(["chmod", "-R", "u+w", mounts["/to"]], check=True)
script = re.sub(r"/from\b", mounts["/from"], script)
script = re.sub(r"/to\b", mounts["/to"], script)
sys.exit(subprocess.run(["sh", "-euc", script] + rest).returncode)
EOF
chmod +x "$scratch/bin/docker"

export PATH="$scratch/bin:$PATH" STUB_VOLUMES="$scratch/volumes" STUB_LOG="$scratch/docker.log"
export RUN_ROOT="$scratch/run"
for config in mail1 mail2; do
  (cd "$root" && task config:snapshot CONFIG="$config")
done
me="$(id -u):$(id -g)"
fail() { echo "FAIL: $*" >&2; exit 1; }

issue() {
  for f in ca.crt ca.key mail1.test.crt mail1.test.key mail2.test.crt mail2.test.key; do
    echo "$f" >"$scratch/volumes/issuing/$f"
  done
}

# up <service> <config> runs `task <service>:up` against the stub; prints its exit code.
up() {
  : >"$STUB_LOG"
  (cd "$root" && task "$1:up" CONFIG="$2" HOSTNAME="$2.test" CERTS_VOLUME=issuing \
    CERTS_OWNER="$me" >"$scratch/out" 2>&1) && echo 0 || echo $?
}

# Staging refuses unsafe names before touching Docker.
stage=$root/tasks/stage-certs.sh
for bad in "../ca.key" "ca.key" "" "-rf" "sub/x.crt" ".hidden"; do
  : >"$STUB_LOG"
  if sh "$stage" own "$me" issuing ca.crt "$bad" 2>/dev/null; then fail "staged '$bad'"; fi
  [ ! -s "$STUB_LOG" ] || fail "'$bad' reached docker"
done
if sh "$stage" issuing "$me" issuing ca.crt 2>/dev/null; then fail "staged a volume into itself"; fi
for bad in root 1: :1 1:2:3 a:b 1 ""; do
  if sh "$stage" own "$bad" issuing ca.crt 2>/dev/null; then fail "took owner '$bad'"; fi
done
: >"$STUB_LOG"
if sh "$stage" own "$me" nosuchvolume ca.crt 2>/dev/null; then fail "staged from a missing volume"; fi
! grep -q '^run' "$STUB_LOG" || fail "a missing issuing volume reached docker run"
if sh "$stage" own "$me" issuing 2>/dev/null; then fail "staged no files"; fi

issue
for svc in postfix dovecot; do
  for config in mail1 mail2; do
    own="$scratch/volumes/mailref-$config-$svc-certs"
    code=$(up "$svc" "$config")
    [ "$code" = 0 ] || { cat "$scratch/out" >&2; fail "$svc:up $config exited $code"; }
    [ "$(cd "$own" && printf '%s ' *)" = "ca.crt $config.test.crt $config.test.key " ] ||
      fail "$svc $config staged: $(cd "$own" && echo *)"
    [ "$(stat -c %a "$own/$config.test.key")" = 400 ] || fail "$svc $config key mode"
    [ "$(stat -c %a "$own/ca.crt")" = 444 ] || fail "$svc $config ca.crt mode"
    [ "$(stat -c %a "$own")" = 500 ] || fail "$svc $config directory mode"
    daemon=$(grep '^run -d' "$STUB_LOG") || fail "$svc $config never started"
    echo "$daemon" | grep -q -- "-v mailref-$config-$svc-certs:/certs:ro " ||
      fail "$svc $config does not mount its own volume read-only: $daemon"
    ! echo "$daemon" | grep -q -- "-v issuing:" || fail "$svc $config mounts the issuing volume"
    grep -q -- "-v issuing:/from:ro " "$STUB_LOG" || fail "staging mounts the issuing volume writable"
  done
done

# A reissued certificate reaches the daemon on its next up; nothing stale stays.
echo reissued >"$scratch/volumes/issuing/mail1.test.crt"
touch "$scratch/volumes/mailref-mail1-postfix-certs/stale" 2>/dev/null ||
  { chmod u+w "$scratch/volumes/mailref-mail1-postfix-certs"; touch "$scratch/volumes/mailref-mail1-postfix-certs/stale"; }
[ "$(up postfix mail1)" = 0 ] || fail "postfix:up after reissue"
[ "$(cat "$scratch/volumes/mailref-mail1-postfix-certs/mail1.test.crt")" = reissued ] || fail "reissued cert not staged"
[ ! -e "$scratch/volumes/mailref-mail1-postfix-certs/stale" ] || fail "stale file survived restaging"

# Missing material: nothing starts, and the last good copy is left alone.
rm "$scratch/volumes/issuing/mail2.test.key"
for svc in postfix dovecot; do
  code=$(up "$svc" mail2)
  [ "$code" != 0 ] || fail "$svc:up mail2 succeeded without its key"
  ! grep -q '^run -d' "$STUB_LOG" || fail "$svc mail2 started without its key"
  [ -f "$scratch/volumes/mailref-mail2-$svc-certs/mail2.test.key" ] || fail "$svc mail2 lost its last good key"
done
issue

# An env file pointing outside /certs stages nothing and starts nothing.
cp -r "$root/configs/mail1" "$scratch/stagetest"
sed -i 's|^POSTFIX_TLS_KEY=.*|POSTFIX_TLS_KEY=/etc/ca.key|' "$scratch/stagetest/env"
python3 "$root/scripts/config_snapshot.py" snapshot --config stagetest \
  --source "$scratch/stagetest" --run-root "$RUN_ROOT" >/dev/null
: >"$STUB_LOG"
if (cd "$root" && task -t tasks/postfix.yml up CONFIG=stagetest PROJECT=mailref-stagetest \
  CERTS_VOLUME=issuing CERTS_OWNER="$me" >"$scratch/out" 2>&1); then
  fail "postfix:up staged a key outside /certs"
fi
! grep -q '^run -d' "$STUB_LOG" || fail "postfix started with a key outside /certs"

echo "stage-certs: all checks passed"
