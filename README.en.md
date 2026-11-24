# hermes-backup

Backup, verify, restore and migrate [Hermes Agent](https://hermes-agent.nousresearch.com) data.
Single file, standard library only, Python 3.8+, Windows / macOS / Linux.

## Safe by default

- credential-bearing paths (`auth.json`, `platforms/`, `pairing/`, `bot_relay/`,
  `gateway-connections.json`, `gateway_state.json`, `channel_directory.json`) are skipped;
- `config.yaml` / `.env` values that look like secrets are replaced with `<<REDACTED>>`
  (booleans, numbers and `${ENV}` references are preserved);
- caches, logs, tools, venvs and lock files are never archived.

## Encrypted archives

`--with-secrets` requires `--encrypt`:

```bash
python hermes_backup.py backup --with-secrets --encrypt --password-env HB_PASS
python hermes_backup.py verify hermes-backup-data-XXXX.hbz --password-env HB_PASS
```

Crypto: PBKDF2-HMAC-SHA256 (210k iterations) + HMAC-SHA256 keystream,
encrypt-then-MAC. Dependency-free and self-contained; for adversarial threat models,
wrap the archive with `age` or `gpg` instead.

## Commands

```bash
python hermes_backup.py backup --profile data --out hermes.zip
python hermes_backup.py verify hermes.zip
python hermes_backup.py list hermes.zip
python hermes_backup.py restore hermes.zip --into /tmp/staging --dry-run
python hermes_backup.py selftest
```

Profiles: `data`, `source` (checkout incl. `.git`), `full`.

License: MIT.
