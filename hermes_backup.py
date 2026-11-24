#!/usr/bin/env python3
"""hermes-backup - backup, restore and verify Hermes Agent data.

Single file, standard library only, Python 3.8+.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import socket
import sys
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath

TOOL = "hermes-backup"
VERSION = "0.1.0"
FORMAT = 1
MANIFEST = "manifest.json"

# --- what a profile contains -------------------------------------------------

# Small, portable, human-meaningful state (tens of MB).
DATA_ITEMS = [
    "config.yaml", "SOUL.md", "auth.json", "install_id",
    "state.db", "kanban.db", "projects.db", "channel_directory.json",
    "gateway_state.json", "gateway-connections.json",
    "sessions", "memories", "skills", "plugins", "hooks", "desktop-plugins",
    "platforms", "pairing", "cron", "bot_relay", "kanban",
]

# The source checkout: lets a new machine skip the slowest network step.
SOURCE_ITEMS = ["hermes-agent"]

# Never worth archiving: caches, logs, binaries that the installer re-provisions.
EXCLUDE_DIRS = {
    ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache",
    "node_modules", ".git-rewrite", ".idea",
}
EXCLUDE_NAMES = {"Thumbs.db", ".DS_Store"}

PROFILES = {
    "data": DATA_ITEMS,
    "source": SOURCE_ITEMS,
    "full": DATA_ITEMS + SOURCE_ITEMS,
}


def log(msg: str) -> None:
    print(msg, flush=True)


def warn(msg: str) -> None:
    print("warning: " + msg, file=sys.stderr, flush=True)


def fail(msg: str, code: int = 1) -> "NoReturn":
    print("error: " + msg, file=sys.stderr, flush=True)
    raise SystemExit(code)


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return str(n)


def default_home() -> Path:
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env).expanduser()
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(base) / "hermes"
    if system == "Darwin":
        return Path.home() / ".hermes"
    return Path.home() / ".hermes"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def iter_selected(home: Path, items, extra_exclude):
    """Yield (abs_path, rel_posix_path) for every file selected by items."""
    seen = set()
    for item in items:
        top = home / item
        if not top.exists():
            continue
        if top.is_file():
            rel = top.relative_to(home).as_posix()
            if rel not in seen:
                seen.add(rel)
                yield top, rel
            continue
        for dirpath, dirnames, filenames in os.walk(str(top)):
            dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDE_DIRS)
            for name in sorted(filenames):
                if name in EXCLUDE_NAMES:
                    continue
                p = Path(dirpath) / name
                rel = p.relative_to(home).as_posix()
                if rel in seen:
                    continue
                if extra_exclude and any(fnmatch(rel, pat) for pat in extra_exclude):
                    continue
                seen.add(rel)
                yield p, rel


def fnmatch(name: str, pattern: str) -> bool:
    import fnmatch as _fn
    return _fn.fnmatch(name, pattern)


def hermes_version(home: Path) -> str:
    """Best-effort version string; never fatal."""
    candidates = [
        (home / "hermes-agent" / "pyproject.toml", r'(?m)^\s*version\s*=\s*"([^"]+)"'),
        (home / "hermes-build.json", r'"version"\s*:\s*"([^"]+)"'),
    ]
    import re
    for path, pattern in candidates:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = re.search(pattern, text)
        if m:
            version = m.group(1)
            if version and version != "0.0.0":
                return version
    return "unknown"


def build_manifest(entries, profile, home, with_secrets, hashed=True):
    return {
        "tool": TOOL,
        "tool_version": VERSION,
        "format": FORMAT,
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "profile": profile,
        "includes_secrets": bool(with_secrets),
        "hermes_version": hermes_version(home),
        "host": {
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
        "hermes_home": str(home),
        "entries": entries,
        "totals": {
            "files": len(entries),
            "bytes": sum(e["size"] for e in entries),
        },
    }


def write_archive(out: Path, pairs, manifest, hashed=True, compresslevel=6):
    tmp = out.with_suffix(out.suffix + ".part")
    if tmp.exists():
        tmp.unlink()
    with zipfile.ZipFile(str(tmp), "w", zipfile.ZIP_DEFLATED, compresslevel=compresslevel) as zf:
        for i, (src, rel) in enumerate(pairs, 1):
            zf.write(str(src), rel)
            if i % 500 == 0:
                log("  archived %d files..." % i)
        zf.writestr(MANIFEST, json.dumps(manifest, indent=2, ensure_ascii=False))
    os.replace(str(tmp), str(out))


def read_manifest(zf: zipfile.ZipFile):
    try:
        raw = zf.read(MANIFEST)
    except KeyError:
        fail("archive has no %s; not a hermes-backup archive" % MANIFEST)
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        fail("manifest.json is not valid JSON: %s" % exc)


def safe_rel(rel: str) -> str:
    p = PurePosixPath(rel)
    if p.is_absolute() or ".." in p.parts or not p.parts:
        fail("refusing unsafe path in archive: %r" % rel)
    return rel


def collect(home: Path, items, extra_exclude, do_hash, quiet=False):
    pairs, entries = [], []
    total = 0
    unreadable = []
    for src, rel in iter_selected(home, items, extra_exclude):
        try:
            stat = src.stat()
            size = stat.st_size
        except OSError as exc:
            warn("cannot stat %s: %s" % (src, exc))
            unreadable.append(rel)
            continue
        entry = {"path": rel, "size": size}
        if do_hash:
            try:
                entry["sha256"] = sha256_file(src)
            except OSError as exc:
                warn("cannot read %s: %s" % (src, exc))
                unreadable.append(rel)
                continue
        entries.append(entry)
        pairs.append((src, rel))
        total += size
    return pairs, entries, total, unreadable


def cmd_backup(args) -> int:
    home = Path(args.home).expanduser() if args.home else default_home()
    if not home.is_dir():
        fail("Hermes home not found: %s (use --home to point at it)" % home)
    items = list(PROFILES[args.profile])
    if not args.with_secrets:
        items = [i for i in items if i != "auth.json"]
    log("backup: %s profile from %s" % (args.profile, home))
    log("hashing: %s" % ("off (--no-hash)" if args.no_hash else "sha256"))
    pairs, entries, total, unreadable = collect(home, items, args.exclude, not args.no_hash)
    if not pairs:
        fail("nothing to back up; check --home and the profile")
    secrets = any(e["path"].split("/")[-1] == "auth.json" for e in entries)
    if secrets:
        warn("auth.json is included: the archive holds API keys and tokens")
    manifest = build_manifest(entries, args.profile, home, secrets, not args.no_hash)
    out = Path(args.out).expanduser() if args.out else Path.cwd() / (
        "hermes-backup-%s-%s.zip" % (args.profile, time.strftime("%Y%m%d-%H%M%S")))
    if out.exists() and not args.force:
        fail("%s exists; pass --force to overwrite" % out)
    log("archiving %d files, %s..." % (len(entries), human(total)))
    write_archive(out, pairs, manifest)
    if getattr(args, "dry_run", False):
        log("dry-run: would write %s files, %s raw" % (len(entries), human(total)))
        return 0
    log("")
    log("written: %s (%s)" % (out, human(out.stat().st_size)))
    log("files: %d   raw: %s   secrets: %s" % (len(entries), human(total), "yes" if secrets else "no"))
    if unreadable:
        warn("%d file(s) could not be read and were skipped" % len(unreadable))
        return 2
    return 0


def verify_archive(archive: Path, only=None, quiet=False) -> int:
    with zipfile.ZipFile(str(archive)) as zf:
        man = read_manifest(zf)
        bad = 0
        missing = 0
        checked = 0
        for entry in man.get("entries", []):
            rel = entry["path"]
            if only and not rel.startswith(only):
                continue
            try:
                info = zf.getinfo(rel)
            except KeyError:
                missing += 1
                warn("missing from archive: %s" % rel)
                continue
            expected = entry.get("sha256")
            if not expected:
                checked += 1
                continue
            h = hashlib.sha256()
            with zf.open(info) as fh:
                for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                    h.update(chunk)
            if h.hexdigest() != expected:
                bad += 1
                warn("hash mismatch: %s" % rel)
            checked += 1
    if not quiet:
        log("package: %s" % archive)
        log("profile: %s   created: %s   hermes: %s" % (
            man.get("profile"), man.get("created_utc"), man.get("hermes_version")))
        log("entries: %d   files checked: %d" % (len(man.get("entries", [])), checked))
        log("result: %s" % ("OK" if not (bad or missing) else "FAILED"))
    return 0 if not (bad or missing) else 1


def cmd_verify(args) -> int:
    return verify_archive(Path(args.archive).expanduser(), args.only)


def cmd_list(args) -> int:
    with zipfile.ZipFile(str(Path(args.archive).expanduser())) as zf:
        man = read_manifest(zf)
        log("package:      %s" % args.archive)
        log("tool:         %s %s (format %s)" % (man.get("tool"), man.get("tool_version"), man.get("format")))
        log("profile:      %s" % man.get("profile"))
        log("created:      %s" % man.get("created_utc"))
        log("hermes:       %s" % man.get("hermes_version"))
        log("from:         %s (%s)" % (man.get("hermes_home"), (man.get("host") or {}).get("platform")))
        log("secrets:      %s" % ("yes" if man.get("includes_secrets") else "no"))
        totals = man.get("totals") or {}
        log("contents:     %s files, %s" % (totals.get("files"), human(totals.get("bytes", 0))))
        tops = {}
        for entry in man.get("entries", []):
            top = entry["path"].split("/")[0]
            tops[top] = tops.get(top, 0) + 1
        log("top level:")
        for name in sorted(tops):
            log("  %-28s %d file(s)" % (name, tops[name]))
    return 0


def cmd_restore(args) -> int:
    archive = Path(args.archive).expanduser()
    target = Path(args.into).expanduser() if args.into else default_home()
    with zipfile.ZipFile(str(archive)) as zf:
        man = read_manifest(zf)
        if man.get("format") != FORMAT:
            fail("unsupported archive format: %r" % man.get("format"))
        entries = [e for e in man.get("entries", []) if not args.only or e["path"].startswith(args.only)]
        if not entries:
            fail("nothing selected to restore")
        names = set(zf.namelist())
        conflicts = []
        for entry in entries:
            rel = safe_rel(entry["path"])
            if rel not in names:
                fail("archive is missing %s; run verify first" % rel)
            if (target / rel).exists():
                conflicts.append(rel)
        if conflicts and not args.force:
            warn("%d file(s) already exist under %s" % (len(conflicts), target))
            for rel in conflicts[:10]:
                warn("  " + rel)
            fail("refusing to overwrite; pass --force (or --into a fresh directory)")
        if not args.skip_verify:
            bad = 0
            for entry in entries:
                expected = entry.get("sha256")
                if not expected:
                    continue
                h = hashlib.sha256()
                with zf.open(entry["path"]) as fh:
                    for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                        h.update(chunk)
                if h.hexdigest() != expected:
                    bad += 1
                    warn("hash mismatch: %s" % entry["path"])
            if bad:
                fail("%d file(s) failed verification; restore aborted" % bad)
        if args.dry_run:
            log("would restore %d file(s) into %s" % (len(entries), target))
            return 0
        target.mkdir(parents=True, exist_ok=True)
        restored = 0
        for entry in entries:
            rel = safe_rel(entry["path"])
            dest = target / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".hermes-restore-part")
            with zf.open(rel) as src, open(str(tmp), "wb") as out:
                shutil.copyfileobj(src, out, 1024 * 1024)
            os.replace(str(tmp), str(dest))
            restored += 1
            if restored % 500 == 0:
                log("  restored %d files..." % restored)
    log("restored %d file(s) into %s" % (restored, target))
    log("next: run the Hermes installer/update so venv, tools and services match this data")
    return 0


def cmd_selftest(args) -> int:
    tmp = Path(tempfile.mkdtemp(prefix="hermes-backup-selftest-"))
    try:
        home = tmp / "home"
        (home / "sessions").mkdir(parents=True)
        (home / "skills" / "demo").mkdir(parents=True)
        (home / "config.yaml").write_text("model:\n  default: test\n", encoding="utf-8")
        (home / "auth.json").write_text('{"token":"x"}', encoding="utf-8")
        (home / "sessions" / "a.json").write_text("hello", encoding="utf-8")
        (home / "skills" / "demo" / "SKILL.md").write_text("# demo\n", encoding="utf-8")
        (home / "cache").mkdir()
        (home / "cache" / "huge.bin").write_bytes(b"x" * 1024)
        archive = tmp / "out.zip"
        rc = cmd_backup(argparse.Namespace(home=str(home), profile="data", out=str(archive),
                                           force=False, no_hash=False, with_secrets=True, exclude=None))
        if rc != 0:
            return rc
        rc = verify_archive(archive, quiet=True)
        if rc != 0:
            fail("selftest: verify failed")
        dest = tmp / "restored"
        rc = cmd_restore(argparse.Namespace(archive=str(archive), into=str(dest), force=False,
                                            only=None, skip_verify=False, dry_run=False))
        if rc != 0:
            return rc
        for rel in ("config.yaml", "auth.json", "sessions/a.json", "skills/demo/SKILL.md"):
            a = (home / rel).read_bytes()
            b = (dest / rel).read_bytes()
            if a != b:
                fail("selftest: content mismatch for %s" % rel)
        if (dest / "cache").exists():
            fail("selftest: cache should not be archived")
        log("selftest: OK")
        return 0
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hermes-backup",
        description="Backup, verify and restore Hermes Agent data (data / source / full).")
    p.add_argument("--version", action="version", version="%s %s" % (TOOL, VERSION))
    sub = p.add_subparsers(dest="command")

    b = sub.add_parser("backup", help="create an archive")
    b.add_argument("--profile", choices=sorted(PROFILES), default="data")
    b.add_argument("--out", help="output .zip path")
    b.add_argument("--home", help="Hermes home (default: %%LOCALAPPDATA%%\\hermes or ~/.hermes)")
    b.add_argument("--with-secrets", action="store_true", help="include auth.json (API keys)")
    b.add_argument("--no-hash", action="store_true", help="skip sha256 (faster, weaker verify)")
    b.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                   help="extra relative glob(s) to skip, repeatable")
    b.add_argument("--dry-run", action="store_true", help="report what would be archived, write nothing")
    b.add_argument("--force", action="store_true", help="overwrite an existing archive")
    b.set_defaults(func=cmd_backup)

    v = sub.add_parser("verify", help="check an archive against its manifest")
    v.add_argument("archive")
    v.add_argument("--only", help="verify only paths under this prefix")
    v.set_defaults(func=cmd_verify)

    l = sub.add_parser("list", help="show what an archive contains")
    l.add_argument("archive")
    l.set_defaults(func=cmd_list)

    r = sub.add_parser("restore", help="extract an archive")
    r.add_argument("archive")
    r.add_argument("--into", help="target directory (default: detected Hermes home)")
    r.add_argument("--only", help="restore only paths under this prefix")
    r.add_argument("--force", action="store_true", help="overwrite existing files")
    r.add_argument("--skip-verify", action="store_true", help="do not re-check hashes")
    r.add_argument("--dry-run", action="store_true", help="report, write nothing")
    r.set_defaults(func=cmd_restore)

    t = sub.add_parser("selftest", help="run an offline round-trip test")
    t.set_defaults(func=cmd_selftest)
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
