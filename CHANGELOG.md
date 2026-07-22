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

## [2026.04.28] — Initial release

### Added
- `docker-compose.yml` parameterized by `CONFIG_DIR`, joining `mailnet` as external.
- `Taskfile.yml` with `up`, `down`, `destroy`, `logs`, `ps`, `config`, and `pair:*` convenience tasks.
- Per-instance config dirs `configs/mail1/` and `configs/mail2/` with env, postgres schema, seed users, and overlay placeholders for postfix/dovecot/rspamd/fail2ban.
- README documenting the testbed contract, quick-start, and "add a third instance" recipe.
- `MIT` license, calver tagging, lint workflow.
- Daemon images pinned to `ghcr.io/rest-mail/reference-{postfix,dovecot,rspamd,fail2ban}:2026.04.28`; postgres pinned to `postgres:16-alpine`.
