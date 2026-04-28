# reference-mailserver

A thin composer that assembles `reference-postfix` + `reference-dovecot` + Postgres + `reference-rspamd` + `reference-fail2ban` into one traditional mail server. Parameterized by config dir — the same Taskfile spins up as `mail1`, `mail2`, or anything else by pointing it at a different directory.

This repo contains **no daemon code and no Dockerfiles**. It's just a `Taskfile.yml`, per-service task files under `tasks/`, and per-instance config directories. All the daemon logic lives in the upstream `rest-mail/reference-*` images.

Part of the [rest-mail](https://github.com/rest-mail) decomposition.

## What you get per instance

| Service    | Image                                        | Purpose                              |
| ---------- | -------------------------------------------- | ------------------------------------ |
| `postgres` | `postgres:16-alpine`                         | virtual users, mailboxes, aliases    |
| `postfix`  | `ghcr.io/rest-mail/reference-postfix:2026.04.28`  | SMTP MTA on 25/465/587               |
| `dovecot`  | `ghcr.io/rest-mail/reference-dovecot:2026.04.28`  | IMAP/POP3 + LMTP delivery            |
| `rspamd`   | `ghcr.io/rest-mail/reference-rspamd:2026.04.28`   | spam scoring, DKIM signing           |
| `fail2ban` | `ghcr.io/rest-mail/reference-fail2ban:2026.04.28` | brute-force protection on auth logs  |

Each service is driven by its own file under `tasks/` (`postgres.yml`, `postfix.yml`, `dovecot.yml`, `rspamd.yml`, `fail2ban.yml`), and each task file wraps a single `docker run` invocation. This mirrors the per-service layout used by `rest-mail/tasks/`.

## Contract with the testbed

This composer joins `mailnet` as **external**. It does not create the network, does not run dnsmasq, and does not generate TLS certs. Those concerns belong to [`rest-mail/testbed`](https://github.com/rest-mail/testbed):

- `mailnet` (default name `testbed_mailnet`) — bridge network with subnet `10.99.0.0/16`. dnsmasq sits at `10.99.0.10` and resolves `*.test` to peer containers.
- `certs` (default name `testbed_certs`) — shared volume populated by `reference-certgen` with `mail1.test.crt`, `mail1.test.key`, `mail2.test.crt`, `mail2.test.key`, and the shared CA bundle.

DNS registration with the testbed dnsmasq is handled via the `reference-dnsmasq` image's `render-fragment` subcommand — unchanged from the previous compose-based layout.

If you don't want to depend on the testbed (e.g. you're plugging this into a different substrate), override at run time:

```bash
MAILNET_NAME=mynet CERTS_VOLUME=mycerts task up CONFIG=mail1
```

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
task destroy CONFIG=mail1    # also drop postgres-data / maildir volumes
```

## Tasks

```
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
- Volumes: `mailref-<config>-postgres-data` and `mailref-<config>-maildir`.

So `mail1` and `mail2` never collide.

`VERBOSE=1 task <anything>` shows the raw `docker` output for the underlying invocation — handy when debugging argument assembly or container startup.

## Adding a third instance

1. Copy `configs/mail1/` to `configs/mail3/`.
2. In `configs/mail3/env`, replace every `mail1.test` with `mail3.test`.
3. In `configs/mail3/postgres-init/02-seed.sql`, replace the domain and pick distinct local-parts.
4. Make sure the testbed's certgen issues `mail3.test.{crt,key}` and dnsmasq resolves `mail3.test` — without those this composer cannot do anything useful.
5. `task up CONFIG=mail3`.

No code changes anywhere.

## Per-instance config layout

```
configs/<name>/
├── env                       # POSTFIX_*, DOVECOT_*, RSPAMD_*, POSTGRES_* vars
├── dns.env                   # values consumed by reference-dnsmasq render-fragment
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

Calver, matching the rest-mail org convention. Daemon images are pinned to `:2026.04.28` in the per-service task files under `tasks/`. Bump the tags here in lockstep when the upstream reference images publish a new calver release; do not float on `:latest`.

## License

MIT — see [LICENSE](./LICENSE).
