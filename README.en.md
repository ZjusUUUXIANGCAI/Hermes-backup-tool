# hermes-backup

Backup, verify, restore and migrate [Hermes Agent](https://hermes-agent.nousresearch.com) data.
Single file, dependency-free by default (Python 3.8+), Windows / macOS / Linux.

## Safe by default

- credential paths skipped (`auth.json`, `platforms/`, `pairing/`, `bot_relay/`,
  `gateway-connections.json`, `gateway_state.json`, `channel_directory.json`);
- config values that look like secrets are replaced with `<<REDACTED>>`;
- caches, logs, tools, venvs and lock files are never archived;
- paths are validated on create, verify and restore (drive letters, UNC, backslash,
  `..`/`.`, control chars, reserved device names).

## Consistency

- SQLite files (`state.db`, `kanban.db`, `projects.db`, `cron/executions.db`) are snapshotted
  through the backup API and pass `PRAGMA integrity_check` before archiving.
- Archives are hashed while being written, so the manifest always matches the stored bytes.

## Audit

`verify` and `restore` reject duplicate zip entries, duplicate manifest paths, missing or
unlisted files, size/hash mismatches, unsafe paths and inconsistent totals.

## Encryption

`--with-secrets` requires `--encrypt`:

- **v2 (default)**: PBKDF2-HMAC-SHA256 + AES-256-CTR + HMAC-SHA256 (encrypt-then-MAC).
  Needs the optional `cryptography` package.
- **v1**: stdlib-only HMAC keystream, kept for environments without dependencies.
- Both formats stay readable; a wrong passphrase is rejected before any file is written.

```bash
python hermes_backup.py backup --with-secrets --encrypt --password-env HB_PASS
python hermes_backup.py verify hermes-backup-XXXX.hbz --password-env HB_PASS
```

## Commands

```bash
python hermes_backup.py backup --profile data --out hermes.zip
python hermes_backup.py verify hermes.zip
python hermes_backup.py list hermes.zip
python hermes_backup.py restore hermes.zip --into /tmp/staging --dry-run
python hermes_backup.py selftest
```

Profiles: `data`, `source` (checkout incl. `.git`), `full`.

## Tests

```bash
python -m unittest discover -s tests
```

Covers path safety, archive audit, v1/v2 crypto, SQLite snapshots, backup and restore.

License: MIT.
