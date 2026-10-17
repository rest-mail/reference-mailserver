# reference-mailserver

A thin composer that assembles `reference-postfix` + `reference-dovecot` + Postgres + `reference-rspamd` + `reference-fail2ban` into one traditional mail server. Parameterized by config dir — the same Taskfile spins up as `mail1`, `mail2`, or anything else by pointing it at a different directory.

This repo contains **no daemon code and no Dockerfiles**. It's just a `Taskfile.yml`, per-service task files under `tasks/`, per-instance config directories, and one small consistency check under `scripts/`. All the daemon logic lives in the upstream `rest-mail/reference-*` images.

Part of the [rest-mail](https://github.com/rest-mail) decomposition.

## What you get per instance

| Service    | Image                                        | Purpose                              |
| ---------- | -------------------------------------------- | ------------------------------------ |
| `postgres` | `postgres:16-alpine`                         | virtual users, mailboxes, aliases    |
| `postfix`  | `ghcr.io/rest-mail/reference-postfix:0.1.0`       | SMTP MTA on 25/465/587               |
| `dovecot`  | `ghcr.io/rest-mail/reference-dovecot:0.1.0`       | IMAP/POP3 + LMTP delivery            |
| `rspamd`   | `ghcr.io/rest-mail/reference-rspamd:0.1.0`        | spam scoring, DKIM signing           |
| `fail2ban` | `ghcr.io/rest-mail/reference-fail2ban:0.1.0`      | brute-force protection on auth logs  |

Each service is driven by its own file under `tasks/` (`postgres.yml`, `postfix.yml`, `dovecot.yml`, `rspamd.yml`, `fail2ban.yml`), and each task file wraps a single `docker run` invocation. This mirrors the per-service layout used by `rest-mail/tasks/`.

## Contract with the testbed

This composer joins `mailnet` as **external**. It does not create the network, does not run dnsmasq, and does not generate TLS certs. Those concerns belong to [`rest-mail/testbed`](https://github.com/rest-mail/testbed):

- `mailnet` (default name `testbed_mailnet`) — bridge network with subnet `10.99.0.0/16`. dnsmasq sits at `10.99.0.10` and resolves `*.test` to peer containers.
- `certs` (default name `testbed_certs`) — shared volume populated by `reference-certgen` with `mail1.test.crt`, `mail1.test.key`, `mail2.test.crt`, `mail2.test.key`, and the CA certificate `ca.crt`. It also holds the CA's private key, `ca.key`.

No daemon mounts `certs`: anything that could read `ca.key` could sign certificates every testbed peer trusts. Right before postfix or dovecot starts, `tasks/stage-certs.sh` copies just the files that daemon needs out of it (a one-off container mounting it read-only) into the daemon's own volume, `mailref-<config>-<service>-certs`, which the daemon mounts read-only at `/certs`: the leaf its env file names in `POSTFIX_TLS_CERT`/`POSTFIX_TLS_KEY` (or `DOVECOT_TLS_*`), and `ca.crt`. They're owned by root, which the images run as, keys `0400`, certificates `0444`. If any of them is missing, `up` stops before the daemon starts. Each `up` copies them afresh, so a reissued certificate takes effect on the next `task <service>:restart`.

DNS registration with the testbed dnsmasq is handled via the `reference-dnsmasq` image's `render-fragment` subcommand — unchanged from the previous compose-based layout.

### The API address

Each `configs/<name>/dns.env` carries `API_IP`, the rest-mail API's address on mailnet. The testbed's instance manifest, `configs/restmail/manifest.yml`, allocates that address (the `ip` of its `api` component) and is the authority; the `dns.env` copies must match it. `task check:api-ip` compares every `configs/*/dns.env` `API_IP` with the manifest and fails, naming each file, when they differ. It also fails when it can't check: the manifest is missing or isn't valid YAML, it has no `api` component or more than one, an IP is invalid, or a `dns.env` sets `API_IP` zero times or more than once.

`task dns:register` (and so `task up`) runs the check first, before any docker command. To fix drift, set `API_IP` in the `dns.env` files to the manifest's value.

The check reads the manifest from `../testbed/configs/restmail/manifest.yml`, the testbed clone next to this repo from the quick start below. Point it elsewhere with `TESTBED_MANIFEST`:

```bash
task up CONFIG=mail1 TESTBED_MANIFEST=/path/to/testbed/configs/restmail/manifest.yml
```

It reads local files only; it never fetches the manifest over the network.

### Without the testbed

If you don't want to depend on the testbed (e.g. you're plugging this into a different substrate), override at run time. There is no testbed manifest to check `API_IP` against, so either point `TESTBED_MANIFEST` at your substrate's manifest (same `components:` shape), or skip the check explicitly with `SKIP_API_IP_CHECK=1`, which prints a warning:

```bash
MAILNET_NAME=mynet CERTS_VOLUME=mycerts SKIP_API_IP_CHECK=1 task up CONFIG=mail1
```

`SKIP_API_IP_CHECK` accepts only `1`; any other value is an error, so a typo never skips the check silently.

## Prerequisites

- [Task](https://taskfile.dev) v3 and Docker.
- Python 3 with [PyYAML](https://pypi.org/project/PyYAML/), for `task check:api-ip` (`pip install pyyaml`, or your distribution's `python3-yaml` package).

The check's fixture tests run with the standard library only (plus PyYAML):

```bash
python3 -m unittest discover -s scripts -v
```

Their Taskfile tests swap `docker` for a stub, so they never touch a Docker daemon.

## Quick start

```bash
# 1. Bring the testbed up first (provides mailnet + certs).
git clone https://github.com/rest-mail/testbed.git
cd testbed && task up

# 2. Bring up one mail server (CONFIG defaults to mail1).
cd ../reference-mailserver
task up CONFIG=mail1

# 3. Want a second instance? Run again with a different CONFIG.
task up CONFIG=mail2
```

Each `CONFIG` is independent — bring up as many as you have configs for.

Verify:

```bash
task ps
docker run --rm --network testbed_mailnet --dns 10.99.0.10 alpine \
  sh -c 'apk add --no-cache netcat-openbsd >/dev/null && echo QUIT | nc mail1.test 25'
```

You should see `220 mail1.test ESMTP ...`.

Tear down (per instance):

```bash
task down CONFIG=mail1       # stop containers, keep volumes
task destroy CONFIG=mail1    # also drop postgres-data / maildir / staged-certs volumes
```

## Tasks

```
task check:api-ip               # API_IP in configs/*/dns.env matches the testbed manifest?
task up CONFIG=mail1            # bring up one instance (default CONFIG=mail1)
task down CONFIG=mail1          # stop one instance
task destroy CONFIG=mail1       # stop and drop volumes
task ps                         # list containers for the active CONFIG
task postgres:logs CONFIG=mail1 # per-service logs
task postfix:logs CONFIG=mail1
task dovecot:logs CONFIG=mail1
task rspamd:logs CONFIG=mail1
task fail2ban:logs CONFIG=mail1
```

Each `CONFIG=<name>` invocation produces distinct, namespaced resources:

- Containers: `mailref-<config>-<service>` (e.g. `mailref-mail1-postfix`).
- Volumes: `mailref-<config>-postgres-data`, `mailref-<config>-maildir`, and the staged certificates `mailref-<config>-postfix-certs` and `mailref-<config>-dovecot-certs`.

So `mail1` and `mail2` never collide.

`VERBOSE=1 task <anything>` shows the raw `docker` output for the underlying invocation — handy when debugging argument assembly or container startup.

## Adding a third instance

1. Copy `configs/mail1/` to `configs/mail3/` (keep `API_IP` in `dns.env`: it's the shared API address the check verifies).
2. In `configs/mail3/env`, replace every `mail1.test` with `mail3.test`.
3. In `configs/mail3/postgres-init/02-seed.sql`, replace the domain and pick distinct local-parts.
4. Make sure the testbed's certgen issues `mail3.test.{crt,key}` and dnsmasq resolves `mail3.test` — without those this composer cannot do anything useful.
5. `task up CONFIG=mail3`.

No code changes anywhere.

## Per-instance config layout

```
configs/<name>/
├── env                       # POSTFIX_*, DOVECOT_*, RSPAMD_*, POSTGRES_* vars
├── dns.env                   # values consumed by reference-dnsmasq render-fragment (API_IP checked by check:api-ip)
├── postgres-init/
│   ├── 01-schema.sql         # virtual users / mailboxes / aliases
│   └── 02-seed.sql           # initial accounts
├── postfix-overlay/          # mounted at /etc/postfix-overlay (optional)
├── dovecot-overlay/          # mounted at /etc/dovecot-overlay (optional)
├── rspamd-overlay/           # mounted at /etc/rspamd-overlay (optional)
└── fail2ban-overlay/         # mounted at /etc/fail2ban-overlay (optional)
```

The overlay directories are intentionally empty by default — the published reference images ship working defaults driven by the env file. Drop files into the relevant overlay only when you need to deviate from those defaults.

## Seeded users

Both example instances seed `password123` for every account.

| Instance     | Domain       | Mailboxes                                 | Alias                              |
| ------------ | ------------ | ----------------------------------------- | ---------------------------------- |
| `mail1`      | `mail1.test` | `alice`, `bob`, `postmaster`              | `info@mail1.test → alice@mail1.test` |
| `mail2`      | `mail2.test` | `charlie`, `diana`, `postmaster`          | `info@mail2.test → charlie@mail2.test` |

## Versioning

SemVer, matching the rest-mail org convention. Daemon images are pinned to `:0.1.0` in the per-service task files under `tasks/`. Bump the tags here in lockstep when the reference images publish a new release; do not float on `:latest`.

## License

MIT — see [LICENSE](./LICENSE).
