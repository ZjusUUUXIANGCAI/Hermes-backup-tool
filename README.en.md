# hermes-backup

Backup, verify, restore and migrate [Hermes Agent](https://hermes-agent.nousresearch.com) data.
Single file, standard library only, Python 3.8+, works on Windows / macOS / Linux.

```bash
python hermes_backup.py backup --profile data --out hermes-20261124.zip
python hermes_backup.py verify hermes-20261124.zip
python hermes_backup.py restore hermes-20261124.zip --into /tmp/staging --dry-run
python hermes_backup.py selftest
```

Profiles: `data` (config, DBs, sessions, memories, skills, plugins), `source` (`hermes-agent/` incl. `.git`), `full`.
Caches, logs, tools, venvs are never archived.

`auth.json` is excluded by default because it holds API keys; pass `--with-secrets` if you really need it.

License: MIT.
