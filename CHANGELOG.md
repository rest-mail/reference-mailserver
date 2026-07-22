# Changelog

All notable changes to `reference-mailserver` are recorded here.
Versioning is calver: `YYYY.MM.DD`, with a `.N` suffix for multiple releases on the same day.

## [Unreleased]

### Fixed
- **Every service launched on the same (wrong) image.** Each `tasks/<svc>.yml`
  declared a self-referencing `IMAGE` var; Task flattens all included taskfiles'
  vars into one shared namespace, so the five `IMAGE` defaults collided and a
  single image ran for all services (`postgres`/`dovecot`/`rspamd`/`fail2ban`
  came up as `postfix` or `fail2ban` depending on load order — postgres had no
  `pg_isready`, postfix had no DB, etc.). Images are now bound per include via
  uniquely-named `POSTGRES_IMAGE`/`POSTFIX_IMAGE`/`DOVECOT_IMAGE`/`RSPAMD_IMAGE`/
  `FAIL2BAN_IMAGE` vars, mirroring how per-service `IP`s are already passed.
  Regression from `448ce6d` (per-service `tasks/*.yml` split).
- rspamd container reported `unhealthy` forever — the image's baked healthcheck
  shells out to `curl`, which it doesn't ship. The rspamd task now overrides it
  with `rspamc stat`.

### Changed
- **Reference mail servers now actually deliver mail.** With the completed
  reference-postfix image, the per-instance env (`configs/mail1`, `configs/mail2`)
  is wired for the full Postfix+Dovecot(+rspamd) topology:
  - `POSTFIX_DB_HOST` / `DOVECOT_DB_HOST` point at the instance's postgres
    container (`mailref-<config>-postgres`) instead of the unresolvable bare
    `postgres` — all instances share one mailnet, so the container name is the
    stable address.
  - `POSTFIX_VIRTUAL_TRANSPORT=lmtp:inet:<config>-dovecot:24` hands delivery to
    Dovecot LMTP; `POSTFIX_SASL_PATH` authenticates submission against Dovecot;
    `POSTFIX_MILTERS` filters through rspamd — replacing the never-consumed
    `POSTFIX_DOVECOT_HOST`/`POSTFIX_RSPAMD_HOST` stubs.
  - Verified end-to-end: SMTP receipt → LMTP → Dovecot maildir → IMAP retrieval.
  - `POSTFIX_IMAGE_TAG` pins reference-postfix to `2026.07.22` (the calver that
    ships the DB maps + flow wiring), independent of the shared `IMAGE_TAG` the
    other reference images still use.

## [2026.04.28] — Initial release

### Added
- `docker-compose.yml` parameterized by `CONFIG_DIR`, joining `mailnet` as external.
- `Taskfile.yml` with `up`, `down`, `destroy`, `logs`, `ps`, `config`, and `pair:*` convenience tasks.
- Per-instance config dirs `configs/mail1/` and `configs/mail2/` with env, postgres schema, seed users, and overlay placeholders for postfix/dovecot/rspamd/fail2ban.
- README documenting the testbed contract, quick-start, and "add a third instance" recipe.
- `MIT` license, calver tagging, lint workflow.
- Daemon images pinned to `ghcr.io/rest-mail/reference-{postfix,dovecot,rspamd,fail2ban}:2026.04.28`; postgres pinned to `postgres:16-alpine`.
