#!/usr/bin/env python3
"""hermes-backup - backup, restore, verify and migrate Hermes Agent data.

Design rules
------------
* Standard library only, Python 3.8+, Windows / macOS / Linux.
* The default archive contains NO credentials: secret-bearing paths are skipped
  and config-like files are scrubbed.
* Credentials can be archived only on request (--with-secrets), and then the
  archive must be encrypted (pure-Python, PBKDF2 + HMAC-SHA256 keystream).
"""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import json
import os
import platform
import re
import shutil
import socket
import struct
import sys
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath

TOOL = "hermes-backup"
VERSION = "0.2.0"
FORMAT = 1
MANIFEST = "manifest.json"
MAGIC = b"HBZ1"
KDF_ITERS = 210000
CHUNK = 1024 * 1024

# --- profiles ---------------------------------------------------------------

DATA_ITEMS = [
    "config.yaml", "SOUL.md", "auth.json", "install_id",
    "state.db", "kanban.db", "projects.db", "channel_directory.json",
    "gateway_state.json", "gateway-connections.json",
    "sessions", "memories", "skills", "plugins", "hooks", "desktop-plugins",
    "platforms", "pairing", "cron", "bot_relay", "kanban",
]
SOURCE_ITEMS = ["hermes-agent"]
PROFILES = {
    "data": DATA_ITEMS,
    "source": SOURCE_ITEMS,
    "full": DATA_ITEMS + SOURCE_ITEMS,
}

# Paths that can carry credentials; skipped unless --with-secrets.
SECRET_ITEMS = [
    "auth.json",
    "gateway-connections.json",
    "gateway_state.json",
    "channel_directory.json",
    "platforms",
    "pairing",
    "bot_relay",
]

# Files scrubbed in place when they are archived (value replacement, not deletion).
SCRUB_NAMES = {"config.yaml", "channel_directory.json"}
SCRUB_SUFFIXES = (".env", ".env.local", ".env.production")

EXCLUDE_DIRS = {
    ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache",
    "node_modules", ".git-rewrite", ".idea",
}
EXCLUDE_NAMES = {"Thumbs.db", ".DS_Store"}

SECRET_ASSIGN = re.compile(
    r"(?im)^(\s*[\w.\-]*(?:token|secret|api[_-]?key|apikey|password|passwd|"
    r"access[_-]?key|bearer|authorization)[\w.\-]*\s*[:=]\s*)(.+?)(\s*(?:#.*)?)$")
SECRET_INLINE = re.compile(r"(?i)\b(sk-[A-Za-z0-9_\-]{12,}|Bearer\s+[A-Za-z0-9._\-]{12,})")


def log(msg: str) -> None:
    print(msg, flush=True)


def warn(msg: str) -> None:
    print("warning: " + msg, file=sys.stderr, flush=True)


def fail(msg: str, code: int = 1):
    print("error: " + msg, file=sys.stderr, flush=True)
    raise SystemExit(code)


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return str(n)


def default_home() -> Path:
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env).expanduser()
    if platform.system() == "Windows":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(base) / "hermes"
    return Path.home() / ".hermes"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_secret_path(rel: str) -> bool:
    parts = PurePosixPath(rel).parts
    for item in SECRET_ITEMS:
        if rel == item or (parts and parts[0] == item):
            return True
    return False


BOOLISH = re.compile(r"(?i)^(true|false|null|none|~|-?\d+(?:\.\d+)?)$")
ENVREF = re.compile(r"^(\$\{[^}]+\}|\$[A-Za-z_][A-Za-z0-9_]*|env:[A-Za-z_][A-Za-z0-9_]*)$")


def scrub_text(text: str) -> str:
    def repl(m):
        value = m.group(2).strip().strip('"').strip("'")
        # Booleans, numbers and ${ENV} references are configuration, not secrets:
        # redacting them would change behaviour after restore.
        if BOOLISH.match(value) or ENVREF.match(value):
            return m.group(0)
        if len(value) < 8:
            return m.group(0)
        return m.group(1) + '"<<REDACTED>>"' + m.group(3)

    text = SECRET_ASSIGN.sub(repl, text)
    text = SECRET_INLINE.sub(lambda m: "<<REDACTED>>", text)
    return text


def scrub_bytes(data: bytes) -> bytes:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data
    return scrub_text(text).encode("utf-8")


def should_scrub(rel: str) -> bool:
    name = PurePosixPath(rel).name
    return name in SCRUB_NAMES or name.endswith(SCRUB_SUFFIXES)


# --- crypto (PBKDF2-HMAC-SHA256 + HMAC keystream, encrypt-then-MAC) ---------

def derive_keys(password: str, salt: bytes, iters: int):
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iters, dklen=64)
    return dk[:32], dk[32:]


def keystream(key: bytes, nonce: bytes, length: int, counter: int = 0) -> bytes:
    out = bytearray()
    while len(out) < length:
        out += hmac.new(key, nonce + struct.pack(">Q", counter), hashlib.sha256).digest()
        counter += 1
    return bytes(out[:length])


def is_encrypted(path: Path) -> bool:
    try:
        with open(str(path), "rb") as fh:
            return fh.read(4) == MAGIC
    except OSError:
        return False


def encrypt_file(src: Path, dst: Path, password: str) -> None:
    salt = os.urandom(16)
    nonce = os.urandom(16)
    enc_key, mac_key = derive_keys(password, salt, KDF_ITERS)
    header = json.dumps({
        "v": 1, "kdf": "pbkdf2-sha256", "iters": KDF_ITERS,
        "salt": base64.b64encode(salt).decode("ascii"),
        "nonce": base64.b64encode(nonce).decode("ascii"),
    }).encode("utf-8")
    tmp = dst.with_suffix(dst.suffix + ".part")
    macd = hmac.new(mac_key, header, hashlib.sha256)
    counter = 0
    with open(str(src), "rb") as fin, open(str(tmp), "wb") as fout:
        fout.write(MAGIC)
        fout.write(struct.pack(">I", len(header)))
        fout.write(header)
        while True:
            chunk = fin.read(CHUNK)
            if not chunk:
                break
            ks = keystream(enc_key, nonce, len(chunk), counter)
            counter += (len(chunk) + 31) // 32
            ct = bytes(a ^ b for a, b in zip(chunk, ks))
            macd.update(ct)
            fout.write(ct)
        fout.write(macd.digest())
    os.replace(str(tmp), str(dst))


def decrypt_file(src: Path, dst: Path, password: str) -> None:
    with open(str(src), "rb") as fin:
        magic = fin.read(4)
        if magic != MAGIC:
            fail("not an encrypted hermes-backup archive: %s" % src)
        raw_len = fin.read(4)
        if len(raw_len) != 4:
            fail("truncated archive header")
        hlen = struct.unpack(">I", raw_len)[0]
        header = fin.read(hlen)
        try:
            meta = json.loads(header.decode("utf-8"))
        except ValueError:
            fail("corrupted archive header")
        salt = base64.b64decode(meta["salt"])
        nonce = base64.b64decode(meta["nonce"])
        iters = int(meta.get("iters", KDF_ITERS))
        enc_key, mac_key = derive_keys(password, salt, iters)
        total = os.fstat(fin.fileno()).st_size
        body = total - 4 - 4 - hlen - 32
        macd = hmac.new(mac_key, header, hashlib.sha256)
        tmp = dst.with_suffix(dst.suffix + ".part")
        counter = 0
        remaining = body
        with open(str(tmp), "wb") as fout:
            while remaining > 0:
                chunk = fin.read(min(CHUNK, remaining))
                if not chunk:
                    fail("truncated archive body")
                remaining -= len(chunk)
                macd.update(chunk)
                ks = keystream(enc_key, nonce, len(chunk), counter)
                counter += (len(chunk) + 31) // 32
                fout.write(bytes(a ^ b for a, b in zip(chunk, ks)))
            expected = fin.read(32)
        if not hmac.compare_digest(macd.digest(), expected):
            tmp.unlink()
            fail("wrong passphrase or corrupted archive (authentication failed)")
        os.replace(str(tmp), str(dst))


def resolve_password(args, confirm: bool = False) -> str:
    if getattr(args, "password_env", None):
        value = os.environ.get(args.password_env)
        if not value:
            fail("environment variable %s is empty" % args.password_env)
        return value
    if getattr(args, "password_file", None):
        return Path(args.password_file).expanduser().read_text(encoding="utf-8").strip()
    if not sys.stdin.isatty():
        fail("no passphrase available: pass --password-env or --password-file "
             "(stdin is not a terminal)")
    pw = getpass.getpass("passphrase: ")
    if not pw:
        fail("empty passphrase")
    if confirm and getpass.getpass("repeat passphrase: ") != pw:
        fail("passphrases do not match")
    return pw


# --- selection and archive writing ------------------------------------------

def iter_selected(home: Path, items, extra_exclude):
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
                if name in EXCLUDE_NAMES or name.endswith((".lock", ".pid")):
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
    import re as _re
    candidates = [
        (home / "hermes-agent" / "pyproject.toml", r'(?m)^\s*version\s*=\s*"([^"]+)"'),
        (home / "hermes-build.json", r'"version"\s*:\s*"([^"]+)"'),
    ]
    for path, pattern in candidates:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = _re.search(pattern, text)
        if m:
            version = m.group(1)
            if version and version != "0.0.0":
                return version
    return "unknown"


def collect(home: Path, items, extra_exclude, do_hash, with_secrets, scrub,
            quiet=False):
    files = []
    total = 0
    skipped = 0
    unreadable = 0
    for src, rel in iter_selected(home, items, extra_exclude):
        if not with_secrets and is_secret_path(rel):
            skipped += 1
            continue
        data = None
        try:
            if scrub and should_scrub(rel) and src.stat().st_size <= 2 * 1024 * 1024:
                data = scrub_bytes(src.read_bytes())
        except OSError as exc:
            warn("cannot read %s: %s" % (rel, exc))
            unreadable += 1
            continue
        size = len(data) if data is not None else src.stat().st_size
        entry = {"path": rel, "size": size}
        if do_hash:
            try:
                entry["sha256"] = sha256_bytes(data) if data is not None else sha256_file(src)
            except OSError as exc:
                warn("cannot hash %s: %s" % (rel, exc))
                unreadable += 1
                continue
        files.append({"src": src, "rel": rel, "data": data, "entry": entry})
        total += size
    return files, total, skipped, unreadable


def build_manifest(entries, profile, home, secrets, anon):
    host = {"platform": platform.platform(), "python": platform.python_version()}
    if anon:
        host["hostname"] = "hidden"
        home_label = "~"
    else:
        host["hostname"] = socket.gethostname()
        home_label = str(home)
    return {
        "tool": TOOL,
        "tool_version": VERSION,
        "format": FORMAT,
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "profile": profile,
        "includes_secrets": bool(secrets),
        "encrypted": False,
        "anonymized": bool(anon),
        "hermes_version": hermes_version(home),
        "host": host,
        "hermes_home": home_label,
        "entries": entries,
        "totals": {"files": len(entries), "bytes": sum(e["size"] for e in entries)},
    }


def write_zip(out: Path, files, manifest) -> None:
    tmp = out.with_suffix(out.suffix + ".part")
    with zipfile.ZipFile(str(tmp), "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for i, item in enumerate(files, 1):
            if item["data"] is not None:
                zf.writestr(item["rel"], item["data"])
            else:
                zf.write(str(item["src"]), item["rel"])
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


import contextlib


@contextlib.contextmanager
def open_archive(path: Path, args):
    if is_encrypted(path):
        password = resolve_password(args)
        fd, tmp_name = tempfile.mkstemp(prefix="hermes-backup-dec-", suffix=".zip")
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            decrypt_file(path, tmp, password)
            with zipfile.ZipFile(str(tmp)) as zf:
                yield zf
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
    else:
        with zipfile.ZipFile(str(path)) as zf:
            yield zf


def cmd_backup(args) -> int:
    home = Path(args.home).expanduser() if args.home else default_home()
    if not home.is_dir():
        fail("Hermes home not found: %s (use --home to point at it)" % home)
    if args.with_secrets and not args.encrypt and not args.allow_plaintext_secrets:
        fail("--with-secrets would write credentials in plaintext; add --encrypt "
             "(or --allow-plaintext-secrets if you really mean it)")
    password = resolve_password(args, confirm=True) if args.encrypt else None
    scrub = not args.no_scrub
    items = list(PROFILES[args.profile])
    log("backup: %s profile from %s" % (args.profile, home))
    log("secrets: %s   scrub: %s   hash: %s   encrypt: %s" % (
        "included" if args.with_secrets else "excluded",
        "on" if scrub else "off",
        "on" if not args.no_hash else "off",
        "yes" if args.encrypt else "no"))
    files, total, skipped, unreadable = collect(
        home, items, args.exclude, not args.no_hash, args.with_secrets, scrub)
    if not files:
        fail("nothing to back up; check --home and the profile")
    if skipped:
        log("skipped %d credential-bearing item(s)" % skipped)
    secrets = any(is_secret_path(f["rel"]) for f in files)
    entries = [f["entry"] for f in files]
    out = Path(args.out).expanduser() if args.out else (
        Path.cwd() / ("hermes-backup-%s-%s%s" % (
            args.profile, time.strftime("%Y%m%d-%H%M%S"), ".hbz" if args.encrypt else ".zip")))
    if out.exists() and not args.force:
        fail("%s exists; pass --force to overwrite" % out)
    if args.encrypt and out.suffix.lower() != ".hbz":
        out = out.with_suffix(".hbz")
    if args.dry_run:
        log("dry-run: would archive %d files, %s raw%s" % (
            len(entries), human(total), " (encrypted)" if args.encrypt else ""))
        return 0
    manifest = build_manifest(entries, args.profile, home, secrets, args.anonymize)
    manifest["encrypted"] = bool(args.encrypt)
    log("archiving %d files, %s..." % (len(entries), human(total)))
    if args.encrypt:
        fd, tmp_name = tempfile.mkstemp(prefix="hermes-backup-enc-", suffix=".zip")
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            write_zip(tmp, files, manifest)
            log("encrypting (pbkdf2-sha256, %d iterations)..." % KDF_ITERS)
            encrypt_file(tmp, out, password or "")
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
    else:
        write_zip(out, files, manifest)
    log("")
    log("written: %s (%s)" % (out, human(out.stat().st_size)))
    log("files: %d   raw: %s   secrets: %s   encrypted: %s" % (
        len(entries), human(total), "yes" if secrets else "no",
        "yes" if args.encrypt else "no"))
    if unreadable:
        warn("%d file(s) could not be read and were skipped" % unreadable)
        return 2
    return 0


def verify_archive(archive: Path, args, only=None, quiet=False) -> int:
    with open_archive(archive, args) as zf:
        man = read_manifest(zf)
        bad = missing = checked = 0
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
            if expected:
                h = hashlib.sha256()
                with zf.open(info) as fh:
                    for chunk in iter(lambda: fh.read(CHUNK), b""):
                        h.update(chunk)
                if h.hexdigest() != expected:
                    bad += 1
                    warn("hash mismatch: %s" % rel)
            checked += 1
        if not quiet:
            log("package: %s%s" % (archive, " (encrypted)" if man.get("encrypted") else ""))
            log("profile: %s   created: %s   hermes: %s" % (
                man.get("profile"), man.get("created_utc"), man.get("hermes_version")))
            log("secrets: %s   anonymized: %s" % (
                "yes" if man.get("includes_secrets") else "no",
                "yes" if man.get("anonymized") else "no"))
            log("entries: %d   checked: %d" % (len(man.get("entries", [])), checked))
            log("result: %s" % ("OK" if not (bad or missing) else "FAILED"))
        return 0 if not (bad or missing) else 1


def cmd_verify(args) -> int:
    return verify_archive(Path(args.archive).expanduser(), args, args.only)


def cmd_list(args) -> int:
    archive = Path(args.archive).expanduser()
    with open_archive(archive, args) as zf:
        man = read_manifest(zf)
        log("package:      %s%s" % (args.archive, " (encrypted)" if man.get("encrypted") else ""))
        log("tool:         %s %s (format %s)" % (
            man.get("tool"), man.get("tool_version"), man.get("format")))
        log("profile:      %s" % man.get("profile"))
        log("created:      %s" % man.get("created_utc"))
        log("hermes:       %s" % man.get("hermes_version"))
        log("from:         %s (%s)" % (man.get("hermes_home"), (man.get("host") or {}).get("platform")))
        log("secrets:      %s   anonymized: %s" % (
            "yes" if man.get("includes_secrets") else "no",
            "yes" if man.get("anonymized") else "no"))
        totals = man.get("totals") or {}
        log("contents:     %s files, %s" % (totals.get("files"), human(totals.get("bytes", 0))))
        tops = {}
        for entry in man.get("entries", []):
            top = entry["path"].split("/")[0]
            tops[top] = tops.get(top, 0) + 1
        log("top level:")
        for name in sorted(tops):
            log("  %-30s %d file(s)" % (name, tops[name]))
    return 0


def cmd_restore(args) -> int:
    archive = Path(args.archive).expanduser()
    target = Path(args.into).expanduser() if args.into else default_home()
    with open_archive(archive, args) as zf:
        man = read_manifest(zf)
        if man.get("format") != FORMAT:
            fail("unsupported archive format: %r" % man.get("format"))
        entries = [e for e in man.get("entries", [])
                   if not args.only or e["path"].startswith(args.only)]
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
                    for chunk in iter(lambda: fh.read(CHUNK), b""):
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
                shutil.copyfileobj(src, out, CHUNK)
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
        (home / "platforms").mkdir()
        (home / "config.yaml").write_text(
            "model:\n  default: test\n  api_key: sk-abcdef1234567890xyz\n"
            "  redact_secrets: true\n  base_key: \"${SOME_ENV}\"\n"
            "telegram_token: 123456:AAAAsecret\n",
            encoding="utf-8")
        (home / "auth.json").write_text('{"token":"super-secret"}', encoding="utf-8")
        (home / "platforms" / "creds.json").write_text('{"t":"x"}', encoding="utf-8")
        (home / "sessions" / "a.json").write_text("hello", encoding="utf-8")
        (home / "skills" / "demo" / "SKILL.md").write_text("# demo\n", encoding="utf-8")
        (home / "cache").mkdir()
        (home / "cache" / "huge.bin").write_bytes(b"x" * 4096)

        base = dict(home=str(home), profile="data", force=False, no_hash=False,
                    exclude=[], dry_run=False)
        plain = tmp / "plain.zip"
        args_p = argparse.Namespace(with_secrets=False, encrypt=False, no_scrub=False,
                                    anonymize=False, allow_plaintext_secrets=False,
                                    out=str(plain), password_env=None, password_file=None, **base)
        if cmd_backup(args_p) != 0:
            fail("selftest: plain backup failed")
        with zipfile.ZipFile(str(plain)) as zf:
            names = set(zf.namelist())
            if "auth.json" in names or "platforms/creds.json" in names:
                fail("selftest: credentials leaked into default archive")
            cfg = zf.read("config.yaml").decode("utf-8")
            if "sk-abcdef" in cfg or "AAAAsecret" in cfg:
                fail("selftest: config.yaml was not scrubbed")
            if "redact_secrets: true" not in cfg:
                fail("selftest: boolean setting was redacted by mistake")
            if "${SOME_ENV}" not in cfg:
                fail("selftest: environment reference was redacted by mistake")
            if "cache/huge.bin" in names:
                fail("selftest: cache should not be archived")
        enc = tmp / "enc.hbz"
        args_e = argparse.Namespace(with_secrets=True, encrypt=True, no_scrub=False,
                                    anonymize=True, allow_plaintext_secrets=False,
                                    out=str(enc), password_env="HB_SELFTEST_PW", password_file=None,
                                    **base)
        os.environ["HB_SELFTEST_PW"] = "correct horse battery staple"
        if cmd_backup(args_e) != 0:
            fail("selftest: encrypted backup failed")
        if not is_encrypted(enc):
            fail("selftest: archive is not encrypted")
        if b"super-secret" in enc.read_bytes():
            fail("selftest: plaintext leaked into encrypted archive")
        args_v = argparse.Namespace(password_env="HB_SELFTEST_PW", password_file=None)
        if verify_archive(enc, args_v, quiet=True) != 0:
            fail("selftest: encrypted verify failed")
        dest = tmp / "restored"
        args_r = argparse.Namespace(archive=str(enc), into=str(dest), force=False, only=None,
                                    skip_verify=False, dry_run=False,
                                    password_env="HB_SELFTEST_PW", password_file=None)
        if cmd_restore(args_r) != 0:
            fail("selftest: encrypted restore failed")
        if (dest / "auth.json").read_text(encoding="utf-8") != '{"token":"super-secret"}':
            fail("selftest: restored credentials mismatch")
        os.environ["HB_SELFTEST_PW"] = "wrong passphrase"
        try:
            rc = verify_archive(enc, argparse.Namespace(password_env="HB_SELFTEST_PW",
                                                        password_file=None), quiet=True)
        except SystemExit:
            rc = 1
        if rc == 0:
            fail("selftest: wrong passphrase was accepted")
        log("selftest: OK")
        return 0
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hermes-backup",
        description="Backup, verify, restore and migrate Hermes Agent data. "
                    "Credentials are excluded unless --with-secrets, which requires --encrypt.")
    p.add_argument("--version", action="version", version="%s %s" % (TOOL, VERSION))
    sub = p.add_subparsers(dest="command")

    def secret_flags(sp):
        sp.add_argument("--password-env", metavar="VAR", help="read the passphrase from this environment variable")
        sp.add_argument("--password-file", metavar="FILE", help="read the passphrase from this file")

    b = sub.add_parser("backup", help="create an archive")
    b.add_argument("--profile", choices=sorted(PROFILES), default="data")
    b.add_argument("--out", help="output .zip (or .hbz when encrypting)")
    b.add_argument("--home", help="Hermes home (default: %%LOCALAPPDATA%%/hermes or ~/.hermes)")
    b.add_argument("--with-secrets", action="store_true",
                   help="include credential-bearing files (requires --encrypt)")
    b.add_argument("--encrypt", action="store_true", help="encrypt the archive with a passphrase")
    b.add_argument("--allow-plaintext-secrets", action="store_true",
                   help="allow --with-secrets without encryption (not recommended)")
    b.add_argument("--no-scrub", action="store_true",
                   help="do not redact secret-like values inside config files")
    b.add_argument("--anonymize", action="store_true", help="hide hostname and home path in the manifest")
    b.add_argument("--no-hash", action="store_true", help="skip sha256 (faster, weaker verify)")
    b.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                   help="extra relative glob(s) to skip, repeatable")
    b.add_argument("--dry-run", action="store_true", help="report what would be archived, write nothing")
    b.add_argument("--force", action="store_true", help="overwrite an existing archive")
    secret_flags(b)
    b.set_defaults(func=cmd_backup)

    v = sub.add_parser("verify", help="check an archive against its manifest")
    v.add_argument("archive")
    v.add_argument("--only", help="verify only paths under this prefix")
    secret_flags(v)
    v.set_defaults(func=cmd_verify)

    l = sub.add_parser("list", help="show what an archive contains")
    l.add_argument("archive")
    secret_flags(l)
    l.set_defaults(func=cmd_list)

    r = sub.add_parser("restore", help="extract an archive")
    r.add_argument("archive")
    r.add_argument("--into", help="target directory (default: detected Hermes home)")
    r.add_argument("--only", help="restore only paths under this prefix")
    r.add_argument("--force", action="store_true", help="overwrite existing files")
    r.add_argument("--skip-verify", action="store_true", help="do not re-check hashes")
    r.add_argument("--dry-run", action="store_true", help="report, write nothing")
    secret_flags(r)
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
