# Changelog

## 0.3.5 - 2026-11-25

### Fixed
- Windows console QuickEdit is disabled while the tool runs: a click inside the
  console window used to switch it into select mode, pausing the program and
  swallowing all keyboard input until Esc, which made the interactive menu look
  dead (the window title gained a "Select" prefix). Trade-off: right-button
  paste is gone with QuickEdit; use Ctrl+V instead.

## 0.3.4 - 2026-11-25

### Fixed
- UnicodeEncodeError on Windows consoles whose code page is GBK/cp1252: the tool now
  switches the console to UTF-8 and reconfigures stdout/stderr, and falls back to an
  ASCII menu and "OK" markers when UTF-8 is unavailable.
- The packaged .exe no longer crashes at `selftest` on such consoles.
- Windows CI jobs (unit tests) no longer fail on non-UTF-8 output encoding.

## 0.3.3 - 2026-11-24

### Changed
- Console output is colorized (green success, yellow warnings, red errors, cyan headings)
  with automatic fallback when the terminal does not support ANSI.
- `selftest` now prints six concise steps instead of the internal backup log.
- The interactive menu is colorized and grouped.

## 0.3.2 - 2026-11-24

### Added
- Interactive console: double-clicking the executable now opens a menu
  (backup / encrypted backup / verify / list / restore / selftest) instead of printing help.
  Every action stays on screen until Enter is pressed.

### Changed
- `--version` now reports 0.3.2.

## 0.3.1 - 2026-11-24

### Fixed
- Double-clicking the .exe (no arguments) no longer closes the console instantly:
  it prints the help text and waits for Enter. Set `HERMES_BACKUP_NO_PAUSE=1` to restore
  the old behaviour for scripts.

### Builds
- Windows single-file build now extracts its temporary files next to the executable
  (`--runtime-tmpdir .`), which also works when `%TEMP%` is restricted.

## 0.3.0 - 2026-11-24

安全性与可靠性版本。

### Added
- Archive audit (verify/restore) now checks: duplicate zip entries, duplicate manifest paths,
  entries missing from the archive, files not listed in the manifest, size mismatches,
  sha256 mismatches, unsafe paths and manifest totals.
- SQLite consistency: `state.db`, `kanban.db`, `projects.db`, `cron/executions.db` are
  snapshotted through the SQLite backup API and pass `PRAGMA integrity_check` before archiving
  (`--no-db-snapshot` opts out).
- Encrypted format v2: AES-256-CTR + HMAC-SHA256 (encrypt-then-MAC), PBKDF2-SHA256.
  Requires the optional `cryptography` package; v1 archives remain readable without it.
- Path validation on create, verify and restore: drive letters, UNC/backslash paths,
  `..`/`.` segments, control characters, trailing dots/spaces and reserved device names.
- Restore refuses to write through symlinks/junctions and refuses to overwrite them.

### Changed
- Manifest format 2 (readers still accept format 1 archives from 0.2.x).
- Hashing happens while writing the archive, so the manifest always describes stored bytes.
- KDF iterations are bounded (50k..2M) and passphrase length is capped.
- Archive header length is capped before parsing.

### Fixed
- A file modified between hashing and compression can no longer desynchronise the manifest.
- Restore previously could overwrite a symlink target outside the destination.

## 0.2.0 - 2026-11-24

- Credentials excluded by default; config scrubbing; optional encrypted archives (v1).
- `data`/`source`/`full` profiles, `verify`/`list`/`restore`/`selftest`.

## 0.1.0 - 2026-11-24

- Initial backup/restore/verify with manifest and sha256.
