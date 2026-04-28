# reference-mailserver

A thin composer that assembles `reference-postfix` + `reference-dovecot` + Postgres + `reference-rspamd` + `reference-fail2ban` into one traditional mail server. Parameterized by config dir — the same compose file spins up as `mail1`, `mail2`, or anything else by pointing it at a different directory.

This repo contains **no daemon code and no Dockerfiles**. It's just a `docker-compose.yml`, a `Taskfile.yml`, and per-instance config directories. All the daemon logic lives in the upstream `rest-mail/reference-*` images.

Part of the [rest-mail](https://github.com/rest-mail) decomposition.

## What you get per instance

| Service    | Image                                        | Purpose                              |
| ---------- | -------------------------------------------- | ------------------------------------ |
| `postgres` | `postgres:16-alpine`                         | virtual users, mailboxes, aliases    |
| `postfix`  | `ghcr.io/rest-mail/reference-postfix:2026.04.28`  | SMTP MTA on 25/465/587               |
| `dovecot`  | `ghcr.io/rest-mail/reference-dovecot:2026.04.28`  | IMAP/POP3 + LMTP delivery            |
| `rspamd`   | `ghcr.io/rest-mail/reference-rspamd:2026.04.28`   | spam scoring, DKIM signing           |
| `fail2ban` | `ghcr.io/rest-mail/reference-fail2ban:2026.04.28` | brute-force protection on auth logs  |

## Contract with the testbed

This composer joins `mailnet` as **external**. It does not create the network, does not run dnsmasq, and does not generate TLS certs. Those concerns belong to [`rest-mail/testbed`](https://github.com/rest-mail/testbed):

- `mailnet` (default name `testbed_mailnet`) — bridge network with subnet `10.99.0.0/16`. dnsmasq sits at `10.99.0.10` and resolves `*.test` to peer containers.
- `certs` (default name `testbed_certs`) — shared volume populated by `reference-certgen` with `mail1.test.crt`, `mail1.test.key`, `mail2.test.crt`, `mail2.test.key`, and the shared CA bundle.

If you don't want to depend on the testbed (e.g. you're plugging this into a different substrate), override at run time:

```bash
MAILNET_NAME=mynet CERTS_VOLUME=mycerts task up CONFIG=mail1
```

## Quick start

```bash
# 1. Bring the testbed up first (provides mailnet + certs).
git clone https://github.com/rest-mail/testbed.git
cd testbed && task up

# 2. Bring up one mail server.
cd ../reference-mailserver
task up CONFIG=mail1

# 3. Or both, for the standard pair.
task pair:up
```

Verify:

```bash
task ps CONFIG=mail1
docker run --rm --network testbed_mailnet --dns 10.99.0.10 alpine \
  sh -c 'apk add --no-cache netcat-openbsd >/dev/null && echo QUIT | nc mail1.test 25'
```

You should see `220 mail1.test ESMTP ...`.

Tear down:

```bash
task pair:down       # stop containers, keep volumes
task pair:destroy    # also drop postgres-data / maildir volumes
```

## Tasks

```
task up CONFIG=mail1          # bring up one instance
task down CONFIG=mail1        # stop one instance
task destroy CONFIG=mail1     # stop and drop volumes
task logs CONFIG=mail1 SERVICE=postfix
task ps CONFIG=mail1
task config CONFIG=mail1      # render resolved compose file (debug)
task pair:up                  # mail1 + mail2
task pair:down
task pair:destroy
```

Each `CONFIG=<name>` invocation uses `COMPOSE_PROJECT_NAME=mailref-<name>`, so `mail1` and `mail2` produce distinct container names, networks-aliases, and named volumes.

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

Calver, matching the rest-mail org convention. Daemon images are pinned to `:2026.04.28` in `docker-compose.yml`. Bump the tags here in lockstep when the upstream reference images publish a new calver release; do not float on `:latest`.

## License

MIT — see [LICENSE](./LICENSE).
