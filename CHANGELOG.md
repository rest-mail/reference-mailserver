# Changelog

All notable changes to `reference-mailserver` are recorded here.
Versioning is calver: `YYYY.MM.DD`, with a `.N` suffix for multiple releases on the same day.

## [Unreleased]

## [2026.04.28] — Initial release

### Added
- `docker-compose.yml` parameterized by `CONFIG_DIR`, joining `mailnet` as external.
- `Taskfile.yml` with `up`, `down`, `destroy`, `logs`, `ps`, `config`, and `pair:*` convenience tasks.
- Per-instance config dirs `configs/mail1/` and `configs/mail2/` with env, postgres schema, seed users, and overlay placeholders for postfix/dovecot/rspamd/fail2ban.
- README documenting the testbed contract, quick-start, and "add a third instance" recipe.
- `MIT` license, calver tagging, lint workflow.
- Daemon images pinned to `ghcr.io/rest-mail/reference-{postfix,dovecot,rspamd,fail2ban}:2026.04.28`; postgres pinned to `postgres:16-alpine`.
