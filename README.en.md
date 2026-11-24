# hermes-backup

Backup, verify, restore and migrate [Hermes Agent](https://hermes-agent.nousresearch.com) data.
Single file, standard library only, Python 3.8+, Windows / macOS / Linux.

## Why this instead of copying the folder

`%LOCALAPPDATA%\hermes` is ~4.9 GB; the meaningful state is ~66 MB and compresses to ~19 MB.
A manual copy drags in caches, tools and venvs, and leaks `auth.json` / platform tokens into
a plaintext zip. This tool skips 8 credential paths by default, scrubs config values, records
a sha256 per file, optionally encrypts the whole archive, and can keep `.git` so a new machine
skips the slowest clone. It does not reinstall Python or the gateway service, and it does not
make SQLite snapshots consistent while Hermes is running.

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
