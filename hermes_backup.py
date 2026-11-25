#!/usr/bin/env python3
"""hermes-backup - backup, restore, verify and migrate Hermes Agent data.

Design rules
------------
* Standard library only by default; AES-256-CTR (v2 archives) needs the optional
  'cryptography' package, and v1 archives stay readable without it.
* The default archive contains no credentials: secret-bearing paths are skipped
  and config-like files are scrubbed.
* Archives are written and hashed in a single pass, so the manifest always
  describes the bytes that are actually stored.
* Paths are validated on archive creation, verification and restore.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import getpass
import hashlib
import hmac
import json
import os
import platform
import re
import shutil
import socket
import sqlite3
import struct
import sys
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath

TOOL = "hermes-backup"
VERSION = "0.3.4"
FORMAT = 2
MANIFEST = "manifest.json"
MAGIC1 = b"HBZ1"
MAGIC2 = b"HBZ2"
CHUNK = 1024 * 1024
MAX_ENTRY_PATH = 4096
MAX_HEADER = 8192
MAX_PASSWORD = 1024
MIN_ITERS, MAX_ITERS, DEFAULT_ITERS = 50_000, 2_000_000, 210_000

try:  # optional: only needed for v2 (AES-256-CTR) archives
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    HAVE_AES = True
except Exception:  # pragma: no cover - exercised where cryptography is absent
    HAVE_AES = False

# --- profiles ---------------------------------------------------------------
DATA_ITEMS = [
    "config.yaml", "SOUL.md", "auth.json", "install_id",
    "state.db", "kanban.db", "projects.db", "channel_directory.json",
    "gateway_state.json", "gateway-connections.json",
    "sessions", "memories", "skills", "plugins", "hooks", "desktop-plugins",
    "platforms", "pairing", "cron", "bot_relay", "kanban",
]
SOURCE_ITEMS = ["hermes-agent"]
PROFILES = {"data": DATA_ITEMS, "source": SOURCE_ITEMS, "full": DATA_ITEMS + SOURCE_ITEMS}

SECRET_ITEMS = [
    "auth.json", "gateway-connections.json", "gateway_state.json",
    "channel_directory.json", "platforms", "pairing", "bot_relay",
]
SCRUB_NAMES = {"config.yaml", "channel_directory.json"}
SCRUB_SUFFIXES = (".env", ".env.local", ".env.production")
EXCLUDE_DIRS = {".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache",
                "node_modules", ".git-rewrite", ".idea"}
EXCLUDE_NAMES = {"Thumbs.db", ".DS_Store"}
EXCLUDE_SUFFIXES = (".lock", ".pid")

WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)],
                    *[f"LPT{i}" for i in range(1, 10)]}

SECRET_ASSIGN = re.compile(
    r"(?im)^(\s*[\w.\-]*(?:token|secret|api[_-]?key|apikey|password|passwd|"
    r"access[_-]?key|bearer|authorization)[\w.\-]*\s*[:=]\s*)(.+?)(\s*(?:#.*)?)$")
SECRET_INLINE = re.compile(r"(?i)\b(sk-[A-Za-z0-9_\-]{12,}|Bearer\s+[A-Za-z0-9._\-]{12,})")
BOOLISH = re.compile(r"(?i)^(true|false|null|none|~|-?\d+(?:\.\d+)?)$")
ENVREF = re.compile(r"^(\$\{[^}]+\}|\$[A-Za-z_][A-Za-z0-9_]*|env:[A-Za-z_][A-Za-z0-9_]*)$")



_QUIET = False
_SUPPRESS_FAILS = False
_COLOR = bool(getattr(sys.stdout, "isatty", lambda: False)() and not os.environ.get("NO_COLOR"))


def _enable_vt() -> None:
    if os.name != "nt" or not _COLOR:
        return
    try:
        import ctypes
        kernel = ctypes.windll.kernel32
        kernel.SetConsoleMode(kernel.GetStdHandle(-11), 7)
    except Exception:
        pass


def paint(code: str, text: str) -> str:
    return ("\033[" + code + "m" + text + "\033[0m") if _COLOR else text


def green(text: str) -> str:
    return paint("32", text)


def red(text: str) -> str:
    return paint("31;1", text)


def yellow(text: str) -> str:
    return paint("33", text)


def cyan(text: str) -> str:
    return paint("36", text)


def bold(text: str) -> str:
    return paint("1", text)


_ASCII_ONLY = False


def _setup_console() -> None:
    """Force UTF-8 console output; fall back to ASCII when that is impossible."""
    global _ASCII_ONLY
    if os.name == "nt":
        try:
            import ctypes
            kernel = ctypes.windll.kernel32
            kernel.SetConsoleOutputCP(65001)
            kernel.SetConsoleCP(65001)
        except Exception:
            pass
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    encoding = (getattr(sys.stdout, "encoding", "") or "").lower()
    _ASCII_ONLY = "utf" not in encoding


def _emit(text: str, err: bool = False) -> None:
    stream = sys.stderr if err else sys.stdout
    try:
        print(text, file=stream, flush=True)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode("ascii"), file=stream, flush=True)


_setup_console()


def log(msg: str) -> None:
    if _QUIET:
        return
    _emit(msg)


def warn(msg: str) -> None:
    if _QUIET:
        return
    _emit(yellow("warning: ") + msg, err=True)


def fail(msg: str, code: int = 1):
    if not _SUPPRESS_FAILS:
        _emit(red("error: ") + msg, err=True)
    raise SystemExit(code)


def step(msg: str) -> None:
    mark = "OK" if _ASCII_ONLY else "✓"
    _emit("  " + green(mark) + " " + msg)


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


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- path safety ------------------------------------------------------------

def validate_rel(rel: str) -> str:
    """Reject anything that could escape the extraction root."""
    if not rel or len(rel) > MAX_ENTRY_PATH:
        fail("invalid archive path (empty or too long): %r" % (rel[:80],))
    if "\\" in rel:
        fail("invalid archive path (backslash): %r" % rel)
    if rel.startswith("/"):
        fail("invalid archive path (absolute): %r" % rel)
    if re.match(r"^[A-Za-z]:", rel):
        fail("invalid archive path (drive letter): %r" % rel)
    if any(ord(ch) < 32 or ch == "\x7f" for ch in rel):
        fail("invalid archive path (control character): %r" % rel)
    segments = rel.split("/")
    if any(seg in ("", ".", "..") for seg in segments):
        fail("invalid archive path (relative segment): %r" % rel)
    parts = PurePosixPath(rel).parts
    if not parts:
        fail("invalid archive path: %r" % rel)
    for part in parts:
        if part != part.rstrip(" ."):
            fail("invalid archive path (trailing dot/space): %r" % rel)
        if part.split(".")[0].upper() in WINDOWS_RESERVED:
            fail("invalid archive path (reserved device name): %r" % rel)
    return rel


def is_reparse(path: Path) -> bool:
    """True for symlinks and Windows junction/reparse points."""
    try:
        st = os.lstat(str(path))
    except OSError:
        return False
    if path.is_symlink():
        return True
    return bool(getattr(st, "st_file_attributes", 0) & 0x400)


def ensure_safe_target(target: Path, rel: str) -> Path:
    """Walk the parents of rel under target and refuse reparse points."""
    cur = target
    for part in PurePosixPath(rel).parts[:-1]:
        cur = cur / part
        if cur.exists() and is_reparse(cur):
            fail("refusing to write through a symlink/reparse point: %s" % cur)
    dest = target / rel
    if dest.exists() and is_reparse(dest):
        fail("refusing to overwrite a symlink/reparse point: %s" % dest)
    return dest


# --- scrubbing --------------------------------------------------------------

def is_secret_path(rel: str) -> bool:
    parts = PurePosixPath(rel).parts
    for item in SECRET_ITEMS:
        if rel == item or (parts and parts[0] == item):
            return True
    return False


def scrub_text(text: str) -> str:
    def repl(m):
        value = m.group(2).strip().strip('"').strip("'")
        if BOOLISH.match(value) or ENVREF.match(value):
            return m.group(0)
        if len(value) < 8:
            return m.group(0)
        return m.group(1) + '"<<REDACTED>>"' + m.group(3)

    text = SECRET_ASSIGN.sub(repl, text)
    return SECRET_INLINE.sub(lambda m: "<<REDACTED>>", text)


def scrub_bytes(data: bytes) -> bytes:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data
    return scrub_text(text).encode("utf-8")


def should_scrub(rel: str) -> bool:
    name = PurePosixPath(rel).name
    return name in SCRUB_NAMES or name.endswith(SCRUB_SUFFIXES)


# --- sqlite snapshots -------------------------------------------------------

def is_sqlite(path: Path) -> bool:
    try:
        with open(str(path), "rb") as fh:
            return fh.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def snapshot_sqlite(src: Path, workdir: Path) -> Path:
    """Consistent snapshot through the SQLite backup API, then integrity check."""
    dst = workdir / (src.name + ".snapshot")
    if dst.exists():
        dst.unlink()
    row = None
    src_con = sqlite3.connect("file:%s?mode=ro" % src.as_posix(), uri=True, timeout=30)
    try:
        dst_con = sqlite3.connect(str(dst))
        try:
            src_con.backup(dst_con)
            row = dst_con.execute("PRAGMA integrity_check").fetchone()
        finally:
            dst_con.close()
    finally:
        src_con.close()
    if not row or str(row[0]).lower() != "ok":
        fail("integrity_check failed for %s: %r" % (src, row))
    return dst


# --- crypto -----------------------------------------------------------------

def derive_keys(password: str, salt: bytes, iters: int):
    if not password or len(password) > MAX_PASSWORD:
        fail("passphrase must be 1..%d characters" % MAX_PASSWORD)
    if not (MIN_ITERS <= iters <= MAX_ITERS):
        fail("KDF iterations out of range: %d" % iters)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iters, dklen=64)
    return dk[:32], dk[32:]


def keystream_v1(key: bytes, nonce: bytes, length: int, counter: int = 0) -> bytes:
    out = bytearray()
    while len(out) < length:
        out += hmac.new(key, nonce + struct.pack(">Q", counter), hashlib.sha256).digest()
        counter += 1
    return bytes(out[:length])


def encrypted_version(path: Path) -> int:
    try:
        with open(str(path), "rb") as fh:
            magic = fh.read(4)
    except OSError:
        return 0
    if magic == MAGIC2:
        return 2
    if magic == MAGIC1:
        return 1
    return 0


def is_encrypted(path: Path) -> bool:
    return encrypted_version(path) > 0


def encrypt_file_v1(src: Path, dst: Path, password: str) -> None:
    salt, nonce = os.urandom(16), os.urandom(16)
    enc_key, mac_key = derive_keys(password, salt, DEFAULT_ITERS)
    header = json.dumps({
        "v": 1, "kdf": "pbkdf2-sha256", "iters": DEFAULT_ITERS,
        "salt": base64.b64encode(salt).decode("ascii"),
        "nonce": base64.b64encode(nonce).decode("ascii"),
    }).encode("utf-8")
    tmp = dst.with_suffix(dst.suffix + ".part")
    macd = hmac.new(mac_key, header, hashlib.sha256)
    counter = 0
    with open(str(src), "rb") as fin, open(str(tmp), "wb") as fout:
        fout.write(MAGIC1)
        fout.write(struct.pack(">I", len(header)))
        fout.write(header)
        while True:
            chunk = fin.read(CHUNK)
            if not chunk:
                break
            ks = keystream_v1(enc_key, nonce, len(chunk), counter)
            counter += (len(chunk) + 31) // 32
            ct = bytes(a ^ b for a, b in zip(chunk, ks))
            macd.update(ct)
            fout.write(ct)
        fout.write(macd.digest())
    os.replace(str(tmp), str(dst))


def decrypt_file_v1(src: Path, dst: Path, password: str) -> None:
    with open(str(src), "rb") as fin:
        if fin.read(4) != MAGIC1:
            fail("not a v1 encrypted archive: %s" % src)
        hlen = struct.unpack(">I", fin.read(4))[0]
        if hlen > MAX_HEADER:
            fail("archive header too large: %d bytes" % hlen)
        header = fin.read(hlen)
        try:
            meta = json.loads(header.decode("utf-8"))
        except ValueError:
            fail("corrupted archive header")
        salt = base64.b64decode(meta["salt"])
        nonce = base64.b64decode(meta["nonce"])
        if len(salt) != 16 or len(nonce) != 16:
            fail("corrupted archive header (salt/nonce)")
        iters = int(meta.get("iters", DEFAULT_ITERS))
        enc_key, mac_key = derive_keys(password, salt, iters)
        total = os.fstat(fin.fileno()).st_size
        body = total - 4 - 4 - hlen - 32
        if body < 0:
            fail("truncated archive")
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
                ks = keystream_v1(enc_key, nonce, len(chunk), counter)
                counter += (len(chunk) + 31) // 32
                fout.write(bytes(a ^ b for a, b in zip(chunk, ks)))
            expected = fin.read(32)
        if not hmac.compare_digest(macd.digest(), expected):
            tmp.unlink()
            fail("wrong passphrase or corrupted archive (authentication failed)")
        os.replace(str(tmp), str(dst))


def encrypt_file_v2(src: Path, dst: Path, password: str) -> None:
    if not HAVE_AES:
        fail("v2 archives need the optional 'cryptography' package "
             "(pip install cryptography); use --crypto v1 to stay dependency-free")
    salt, nonce = os.urandom(16), os.urandom(16)
    enc_key, mac_key = derive_keys(password, salt, DEFAULT_ITERS)
    header = json.dumps({
        "v": 2, "alg": "aes-256-ctr+hmac-sha256", "kdf": "pbkdf2-sha256",
        "iters": DEFAULT_ITERS,
        "salt": base64.b64encode(salt).decode("ascii"),
        "nonce": base64.b64encode(nonce).decode("ascii"),
    }).encode("utf-8")
    encryptor = Cipher(algorithms.AES(enc_key), modes.CTR(nonce)).encryptor()
    tmp = dst.with_suffix(dst.suffix + ".part")
    macd = hmac.new(mac_key, header, hashlib.sha256)
    with open(str(src), "rb") as fin, open(str(tmp), "wb") as fout:
        fout.write(MAGIC2)
        fout.write(struct.pack(">I", len(header)))
        fout.write(header)
        while True:
            chunk = fin.read(CHUNK)
            if not chunk:
                break
            ct = encryptor.update(chunk)
            macd.update(ct)
            fout.write(ct)
        tail = encryptor.finalize()
        if tail:
            macd.update(tail)
            fout.write(tail)
        fout.write(macd.digest())
    os.replace(str(tmp), str(dst))


def decrypt_file_v2(src: Path, dst: Path, password: str) -> None:
    if not HAVE_AES:
        fail("this archive needs the optional 'cryptography' package "
             "(pip install cryptography)")
    with open(str(src), "rb") as fin:
        if fin.read(4) != MAGIC2:
            fail("not a v2 encrypted archive: %s" % src)
        raw_len = fin.read(4)
        if len(raw_len) != 4:
            fail("truncated archive header")
        hlen = struct.unpack(">I", raw_len)[0]
        if hlen > MAX_HEADER:
            fail("archive header too large: %d bytes" % hlen)
        header = fin.read(hlen)
        try:
            meta = json.loads(header.decode("utf-8"))
        except ValueError:
            fail("corrupted archive header")
        if meta.get("alg") != "aes-256-ctr+hmac-sha256":
            fail("unsupported cipher: %r" % meta.get("alg"))
        salt = base64.b64decode(meta.get("salt", ""))
        nonce = base64.b64decode(meta.get("nonce", ""))
        if len(salt) != 16 or len(nonce) != 16:
            fail("corrupted archive header (salt/nonce)")
        enc_key, mac_key = derive_keys(password, salt, int(meta.get("iters", DEFAULT_ITERS)))
        total = os.fstat(fin.fileno()).st_size
        body = total - 4 - 4 - hlen - 32
        if body < 0:
            fail("truncated archive")
        macd = hmac.new(mac_key, header, hashlib.sha256)
        decryptor = Cipher(algorithms.AES(enc_key), modes.CTR(nonce)).decryptor()
        tmp = dst.with_suffix(dst.suffix + ".part")
        remaining = body
        with open(str(tmp), "wb") as fout:
            while remaining > 0:
                chunk = fin.read(min(CHUNK, remaining))
                if not chunk:
                    fail("truncated archive body")
                remaining -= len(chunk)
                macd.update(chunk)
                fout.write(decryptor.update(chunk))
            fout.write(decryptor.finalize())
            expected = fin.read(32)
        if not hmac.compare_digest(macd.digest(), expected):
            tmp.unlink()
            fail("wrong passphrase or corrupted archive (authentication failed)")
        os.replace(str(tmp), str(dst))


def resolve_password(args, confirm: bool = False) -> str:
    direct = getattr(args, "password", None)
    if direct:
        if len(direct) > MAX_PASSWORD:
            fail("passphrase too long")
        return direct
    if getattr(args, "password_env", None):
        value = os.environ.get(args.password_env)
        if not value:
            fail("environment variable %s is empty" % args.password_env)
        if len(value) > MAX_PASSWORD:
            fail("passphrase too long")
        return value
    if getattr(args, "password_file", None):
        value = Path(args.password_file).expanduser().read_text(encoding="utf-8").strip()
        if not value:
            fail("password file is empty")
        if len(value) > MAX_PASSWORD:
            fail("passphrase too long")
        return value
    if not sys.stdin.isatty():
        fail("no passphrase available: pass --password-env or --password-file "
             "(stdin is not a terminal)")
    pw = getpass.getpass("passphrase: ")
    if not pw:
        fail("empty passphrase")
    if len(pw) > MAX_PASSWORD:
        fail("passphrase too long")
    if confirm and getpass.getpass("repeat passphrase: ") != pw:
        fail("passphrases do not match")
    return pw


# --- selection --------------------------------------------------------------

def fnmatch(name: str, pattern: str) -> bool:
    import fnmatch as _fn
    return _fn.fnmatch(name, pattern)


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
                if name in EXCLUDE_NAMES or name.endswith(EXCLUDE_SUFFIXES):
                    continue
                p = Path(dirpath) / name
                rel = p.relative_to(home).as_posix()
                if rel in seen:
                    continue
                if extra_exclude and any(fnmatch(rel, pat) for pat in extra_exclude):
                    continue
                seen.add(rel)
                yield p, rel


def collect(home: Path, items, extra_exclude, with_secrets, scrub,
            db_snapshot, workdir: Path):
    files, skipped, snapshots = [], 0, 0
    for src, rel in iter_selected(home, items, extra_exclude):
        if not with_secrets and is_secret_path(rel):
            skipped += 1
            continue
        if db_snapshot and src.name.lower().endswith((".db", ".sqlite", ".sqlite3")) and is_sqlite(src):
            try:
                src = snapshot_sqlite(src, workdir)
                snapshots += 1
            except SystemExit:
                raise
            except Exception as exc:
                fail("cannot snapshot %s: %s" % (rel, exc))
        data = None
        if scrub and should_scrub(rel) and src.stat().st_size <= 2 * 1024 * 1024:
            try:
                data = scrub_bytes(src.read_bytes())
            except OSError as exc:
                warn("cannot read %s: %s" % (rel, exc))
                continue
        files.append({"src": src, "rel": rel, "data": data})
    return files, skipped, snapshots


# --- archive writing --------------------------------------------------------

def hermes_version(home: Path) -> str:
    candidates = [
        (home / "hermes-agent" / "pyproject.toml", r'(?m)^\s*version\s*=\s*"([^"]+)"'),
        (home / "hermes-build.json", r'"version"\s*:\s*"([^"]+)"'),
    ]
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


def build_manifest(entries, profile, home, secrets, anon, crypto):
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
        "encrypted": bool(crypto),
        "crypto": crypto,
        "anonymized": bool(anon),
        "hermes_version": hermes_version(home),
        "host": host,
        "hermes_home": home_label,
        "entries": entries,
        "totals": {"files": len(entries), "bytes": sum(e["size"] for e in entries)},
    }


def write_archive(out: Path, files, make_manifest):
    """Stream every file into the zip while hashing the exact stored bytes."""
    tmp = out.with_suffix(out.suffix + ".part")
    entries = []
    with zipfile.ZipFile(str(tmp), "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for i, item in enumerate(files, 1):
            rel = item["rel"]
            if item["data"] is not None:
                data = item["data"]
                zf.writestr(rel, data)
                entries.append({"path": rel, "size": len(data), "sha256": sha256_bytes(data)})
            else:
                digest = hashlib.sha256()
                size = 0
                with open(str(item["src"]), "rb") as fin, zf.open(rel, "w") as fout:
                    while True:
                        chunk = fin.read(CHUNK)
                        if not chunk:
                            break
                        fout.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                entries.append({"path": rel, "size": size, "sha256": digest.hexdigest()})
            if i % 500 == 0:
                log("  archived %d files..." % i)
        manifest = make_manifest(entries)
        zf.writestr(MANIFEST, json.dumps(manifest, indent=2, ensure_ascii=False))
    os.replace(str(tmp), str(out))
    return manifest


def read_manifest(zf: zipfile.ZipFile):
    try:
        raw = zf.read(MANIFEST)
    except KeyError:
        fail("archive has no %s; not a hermes-backup archive" % MANIFEST)
    try:
        man = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        fail("manifest.json is not valid JSON: %s" % exc)
    if man.get("tool") != TOOL:
        fail("archive was not produced by %s" % TOOL)
    if man.get("format") not in (1, FORMAT):
        fail("unsupported archive format: %r" % man.get("format"))
    return man


@contextlib.contextmanager
def open_archive(path: Path, args):
    version = encrypted_version(path)
    if version:
        password = resolve_password(args)
        fd, tmp_name = tempfile.mkstemp(prefix="hermes-backup-dec-", suffix=".zip")
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            if version == 2:
                decrypt_file_v2(path, tmp, password)
            else:
                decrypt_file_v1(path, tmp, password)
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


# --- audit ------------------------------------------------------------------

def audit_archive(zf: zipfile.ZipFile, man, only=None):
    """Structural + content audit. Returns (checked, problems)."""
    from collections import Counter

    problems = []
    names = zf.namelist()
    lowered = Counter(n.lower() for n in names)
    for name, count in lowered.items():
        if count > 1:
            problems.append("duplicate zip entry: %s" % name)

    entries = man.get("entries")
    if not isinstance(entries, list):
        problems.append("manifest.entries is not a list")
        return 0, problems

    seen = Counter()
    declared = set()
    for entry in entries:
        rel = entry.get("path")
        if not isinstance(rel, str):
            problems.append("manifest entry without a string path")
            continue
        seen[rel.lower()] += 1
        try:
            validate_rel(rel)
        except SystemExit:
            problems.append("manifest entry has an unsafe path: %r" % rel[:80])
            continue
        declared.add(rel)
    for name, count in seen.items():
        if count > 1:
            problems.append("duplicate manifest path: %s" % name)

    checked = 0
    for entry in entries:
        rel = entry.get("path")
        if only and not str(rel).startswith(only):
            continue
        if not isinstance(rel, str) or rel not in names:
            problems.append("missing from archive: %s" % rel)
            continue
        info = zf.getinfo(rel)
        expected_size = entry.get("size")
        if isinstance(expected_size, int) and info.file_size != expected_size:
            problems.append("size mismatch: %s (manifest %d, archive %d)"
                            % (rel, expected_size, info.file_size))
        expected_hash = entry.get("sha256")
        if expected_hash:
            digest = hashlib.sha256()
            try:
                with zf.open(info) as fh:
                    for chunk in iter(lambda: fh.read(CHUNK), b""):
                        digest.update(chunk)
            except (zipfile.BadZipFile, RuntimeError) as exc:
                problems.append("cannot read %s: %s" % (rel, exc))
                continue
            if digest.hexdigest() != expected_hash:
                problems.append("hash mismatch: %s" % rel)
        checked += 1

    for name in names:
        if name == MANIFEST or name in declared:
            continue
        problems.append("not listed in manifest: %s" % name)

    totals = man.get("totals") or {}
    if isinstance(totals.get("files"), int) and totals["files"] != len(entries):
        problems.append("manifest totals.files does not match entries")
    total_bytes = sum(e.get("size", 0) for e in entries if isinstance(e.get("size"), int))
    if isinstance(totals.get("bytes"), int) and totals["bytes"] != total_bytes:
        problems.append("manifest totals.bytes does not match entries")

    return checked, problems


# --- commands ---------------------------------------------------------------

def cmd_backup(args) -> int:
    home = Path(args.home).expanduser() if args.home else default_home()
    if not home.is_dir():
        fail("Hermes home not found: %s (use --home to point at it)" % home)
    crypto = None
    if args.encrypt:
        crypto = args.crypto
        if crypto == "v2" and not HAVE_AES:
            fail("--crypto v2 needs the optional 'cryptography' package; "
                 "use --crypto v1 or install it")
    if args.with_secrets and not crypto and not args.allow_plaintext_secrets:
        fail("--with-secrets would write credentials in plaintext; add --encrypt "
             "(or --allow-plaintext-secrets if you really mean it)")
    password = resolve_password(args, confirm=True) if crypto else None

    workdir = Path(tempfile.mkdtemp(prefix="hermes-backup-work-"))
    try:
        files, skipped, snapshots = collect(
            home, PROFILES[args.profile], args.exclude, args.with_secrets,
            not args.no_scrub, (not args.dry_run) and args.db_snapshot, workdir)
        if not files:
            fail("nothing to back up; check --home and the profile")
        secrets = any(is_secret_path(f["rel"]) for f in files)
        if secrets and not crypto and not args.allow_plaintext_secrets:
            fail("this selection would include credentials in plaintext")

        log("backup: %s profile from %s" % (args.profile, home))
        log("secrets: %s   scrub: %s   db-snapshot: %s   crypto: %s" % (
            "included" if args.with_secrets else "excluded",
            "off" if args.no_scrub else "on",
            "off" if not args.db_snapshot else ("%d db(s) snapshotted" % snapshots if not args.dry_run else "planned"),
            crypto or "no"))
        if skipped:
            log("skipped %d credential-bearing item(s)" % skipped)

        if args.dry_run:
            total = sum(f["src"].stat().st_size if f["data"] is None else len(f["data"]) for f in files)
            log("dry-run: would archive %d files, %s raw" % (len(files), human(total)))
            return 0

        suffix = ".hbz" if crypto else ".zip"
        out = Path(args.out).expanduser() if args.out else (
            Path.cwd() / ("hermes-backup-%s-%s%s" % (
                args.profile, time.strftime("%Y%m%d-%H%M%S"), suffix)))
        if args.encrypt and out.suffix.lower() != ".hbz":
            out = out.with_suffix(".hbz")
        if out.exists() and not args.force:
            fail("%s exists; pass --force to overwrite" % out)

        def make_manifest(entries):
            return build_manifest(entries, args.profile, home, secrets, args.anonymize, crypto)

        if crypto:
            fd, tmp_name = tempfile.mkstemp(prefix="hermes-backup-enc-", suffix=".zip")
            os.close(fd)
            plain = Path(tmp_name)
            try:
                log("archiving %d files..." % len(files))
                manifest = write_archive(plain, files, make_manifest)
                log("encrypting (%s, pbkdf2-sha256 %d)..." % (crypto, DEFAULT_ITERS))
                if crypto == "v2":
                    encrypt_file_v2(plain, out, password or "")
                else:
                    encrypt_file_v1(plain, out, password or "")
            finally:
                try:
                    plain.unlink()
                except OSError:
                    pass
        else:
            log("archiving %d files..." % len(files))
            manifest = write_archive(out, files, make_manifest)

        log("")
        log("written: %s (%s)" % (green(str(out)), human(out.stat().st_size)))
        log("files: %d   raw: %s   secrets: %s   crypto: %s" % (
            len(manifest["entries"]), human(manifest["totals"]["bytes"]),
            "yes" if secrets else "no", crypto or "no"))
        return 0
    finally:
        shutil.rmtree(str(workdir), ignore_errors=True)


def verify_archive(archive: Path, args, only=None, quiet=False) -> int:
    with open_archive(archive, args) as zf:
        man = read_manifest(zf)
        checked, problems = audit_archive(zf, man, only)
    if not quiet:
        log("package: %s%s" % (archive, " (encrypted %s)" % (man.get("crypto") or "v?")
                              if man.get("encrypted") else ""))
        log("profile: %s   created: %s   hermes: %s   format: %s" % (
            man.get("profile"), man.get("created_utc"),
            man.get("hermes_version"), man.get("format")))
        log("secrets: %s   anonymized: %s" % (
            "yes" if man.get("includes_secrets") else "no",
            "yes" if man.get("anonymized") else "no"))
        log("entries: %d   checked: %d   problems: %d" % (
            len(man.get("entries", [])), checked, len(problems)))
        for p in problems[:20]:
            warn(p)
        if len(problems) > 20:
            warn("... and %d more problem(s)" % (len(problems) - 20))
        log("result: %s" % (green("OK") if not problems else red("FAILED")))
    return 0 if not problems else 1


def cmd_verify(args) -> int:
    return verify_archive(Path(args.archive).expanduser(), args, args.only)


def cmd_list(args) -> int:
    archive = Path(args.archive).expanduser()
    with open_archive(archive, args) as zf:
        man = read_manifest(zf)
        log("package:      %s%s" % (args.archive,
                                    " (encrypted %s)" % (man.get("crypto") or "v?")
                                    if man.get("encrypted") else ""))
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
            top = str(entry.get("path", "")).split("/")[0]
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
        checked, problems = audit_archive(zf, man, args.only)
        if problems:
            for p in problems[:20]:
                warn(p)
            fail("archive failed its integrity audit (%d problem(s)); restore aborted"
                 % len(problems))
        entries = [e for e in man.get("entries", [])
                   if not args.only or str(e["path"]).startswith(args.only)]
        if not entries:
            fail("nothing selected to restore")

        conflicts = []
        for entry in entries:
            dest = ensure_safe_target(target, validate_rel(entry["path"]))
            if dest.exists():
                conflicts.append(entry["path"])
        if conflicts and not args.force:
            warn("%d file(s) already exist under %s" % (len(conflicts), target))
            for rel in conflicts[:10]:
                warn("  " + rel)
            fail("refusing to overwrite; pass --force (or --into a fresh directory)")

        if args.dry_run:
            log("would restore %d file(s) into %s" % (len(entries), target))
            return 0

        target.mkdir(parents=True, exist_ok=True)
        restored = 0
        for entry in entries:
            rel = validate_rel(entry["path"])
            dest = ensure_safe_target(target, rel)
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
    global _QUIET, _SUPPRESS_FAILS
    tmp = Path(tempfile.mkdtemp(prefix="hermes-backup-selftest-"))
    _QUIET = True
    try:
        home = tmp / "home"
        (home / "sessions").mkdir(parents=True)
        (home / "skills" / "demo").mkdir(parents=True)
        (home / "platforms").mkdir()
        (home / "cache").mkdir()
        (home / "config.yaml").write_text(
            "model:\n  default: test\n  api_key: sk-abcdef1234567890xyz\n"
            "  redact_secrets: true\n"
            "telegram_token: 123456:AAAAsecret\n", encoding="utf-8")
        (home / "auth.json").write_text('{"token":"super-secret"}', encoding="utf-8")
        (home / "platforms" / "creds.json").write_text('{"t":"x"}', encoding="utf-8")
        (home / "sessions" / "a.json").write_text("hello", encoding="utf-8")
        (home / "skills" / "demo" / "SKILL.md").write_text("# demo\n", encoding="utf-8")
        (home / "cache" / "huge.bin").write_bytes(b"x" * 4096)
        con = sqlite3.connect(str(home / "state.db"))
        con.execute("CREATE TABLE t (v TEXT)")
        con.execute("INSERT INTO t VALUES ('hello')")
        con.commit()
        con.close()

        print(bold("自检") + "  " + cyan("hermes-backup " + VERSION))
        base = dict(home=str(home), profile="data", force=False, no_hash=False,
                    exclude=[], dry_run=False, no_scrub=False, anonymize=False,
                    db_snapshot=True, allow_plaintext_secrets=False,
                    password_env=None, password_file=None, password=None)

        plain = tmp / "plain.zip"
        args_p = argparse.Namespace(with_secrets=False, encrypt=False, crypto="v2",
                                    out=str(plain), **base)
        if cmd_backup(args_p) != 0:
            fail("自检失败：默认备份未完成")
        with zipfile.ZipFile(str(plain)) as zf:
            names = set(zf.namelist())
            man = read_manifest(zf)
            if "auth.json" in names or "platforms/creds.json" in names:
                fail("自检失败：凭据泄漏")
            cfg = zf.read("config.yaml").decode("utf-8")
            if "sk-abcdef" in cfg or "AAAAsecret" in cfg:
                fail("自检失败：配置未脱敏")
            if "redact_secrets: true" not in cfg:
                fail("自检失败：布尔值被误脱敏")
            checked, problems = audit_archive(zf, man)
            if problems:
                fail("自检失败：审计报错 %r" % problems[:3])
            if not zf.read("state.db").startswith(b"SQLite format 3\x00"):
                fail("自检失败：数据库未快照")
        step("默认备份：凭据已排除、配置已脱敏、数据库已快照")

        bad = tmp / "bad.zip"
        import warnings as _warnings
        with _warnings.catch_warnings():
            _warnings.simplefilter("ignore")
            with zipfile.ZipFile(str(bad), "w") as zf:
                zf.writestr("safe.txt", "x")
                zf.writestr("../evil.txt", "x")
                zf.writestr("safe.txt", "y")
        with zipfile.ZipFile(str(bad)) as zf:
            man = {"tool": TOOL, "format": FORMAT, "entries": [], "totals": {"files": 0, "bytes": 0}}
            _, problems = audit_archive(zf, man)
        joined = " ".join(problems)
        if "duplicate zip entry" not in joined or "not listed in manifest" not in joined:
            fail("自检失败：重复条目或未登记条目未被识别")
        step("归档审计：重复条目与未登记文件都能识别")

        _SUPPRESS_FAILS = True
        try:
            for evil in ("../x", "/abs", "C:/x", "a\\b", "CON", "a..", "x/../../y"):
                try:
                    validate_rel(evil)
                except SystemExit:
                    continue
                fail("自检失败：不安全路径被接受 %r" % evil)
        finally:
            _SUPPRESS_FAILS = False
        step("路径校验：盘符、UNC、反斜杠、.. 与保留名全部拒绝")

        crypto = "v2" if HAVE_AES else "v1"
        enc = tmp / "enc.hbz"
        os.environ["HB_SELFTEST_PW"] = "correct horse battery staple"
        args_e = argparse.Namespace(with_secrets=True, encrypt=True, crypto=crypto,
                                    out=str(enc), **base)
        args_e.password_env = "HB_SELFTEST_PW"
        if cmd_backup(args_e) != 0:
            fail("自检失败：加密备份未完成")
        if encrypted_version(enc) != (2 if HAVE_AES else 1):
            fail("自检失败：加密版本不符")
        if b"super-secret" in enc.read_bytes():
            fail("自检失败：密文里出现明文")
        args_v = argparse.Namespace(password_env="HB_SELFTEST_PW", password_file=None, password=None)
        if verify_archive(enc, args_v, quiet=True) != 0:
            fail("自检失败：加密包校验失败")
        step("加密往返：%s 包可校验、可解密" % crypto)

        dest = tmp / "restored"
        args_r = argparse.Namespace(archive=str(enc), into=str(dest), force=False, only=None,
                                    skip_verify=False, dry_run=False,
                                    password_env="HB_SELFTEST_PW", password_file=None, password=None)
        if cmd_restore(args_r) != 0:
            fail("自检失败：加密包还原失败")
        if (dest / "auth.json").read_text(encoding="utf-8") != '{"token":"super-secret"}':
            fail("自检失败：还原内容不一致")
        if not (dest / "state.db").read_bytes().startswith(b"SQLite format 3\x00"):
            fail("自检失败：还原的数据库不是 SQLite")
        step("还原：数据一致、数据库完整")

        os.environ["HB_SELFTEST_PW"] = "wrong passphrase"
        _SUPPRESS_FAILS = True
        try:
            rc = verify_archive(enc, argparse.Namespace(password_env="HB_SELFTEST_PW",
                                                        password_file=None, password=None), quiet=True)
        except SystemExit:
            rc = 1
        finally:
            _SUPPRESS_FAILS = False
        if rc == 0:
            fail("自检失败：错误口令被接受")
        step("安全：错误口令被拒绝")
        print("  " + green(bold("全部通过")) + "  " + cyan("（共 6 项）"))
        return 0
    finally:
        _QUIET = False
        _SUPPRESS_FAILS = False
        shutil.rmtree(str(tmp), ignore_errors=True)


# --- interactive console ----------------------------------------------------

def print_menu() -> None:
    line = "=" * 60
    if _ASCII_ONLY:
        items = [("1", "backup (no secrets)"), ("2", "backup + encrypt"),
                 ("3", "verify an archive"), ("4", "list an archive"),
                 ("5", "restore"), ("6", "selftest"), ("0", "quit")]
        print()
        print(line)
        print("  hermes-backup  " + VERSION)
        print(line)
        for num, text in items:
            print("   " + num + ") " + text)
        print(line)
        return
    items = [
        ("1", "备份数据" + cyan("（默认，不含密钥）")),
        ("2", "备份并加密" + cyan("（可包含密钥，需要口令）")),
        ("3", "校验备份包"),
        ("4", "查看备份包内容"),
        ("5", "还原备份"),
        ("6", "自检"),
        ("0", "退出"),
    ]
    print()
    print(cyan(line))
    print("  " + bold("hermes-backup") + "  " + cyan(VERSION))
    print(cyan(line))
    for num, text in items:
        print("   " + yellow(num + ")") + " " + text)
    print(cyan(line))


def _ask(prompt: str, default: str = "") -> str:
    """input() wrapper: EOF (Ctrl+Z / closed stdin) exits the menu."""
    try:
        value = input(prompt)
    except EOFError:
        raise SystemExit(0)
    value = value.strip().strip('"').strip("'")
    return value if value else default


def _pause() -> None:
    try:
        input("\n按回车返回菜单...")
    except EOFError:
        raise SystemExit(0)


def _ns(**kw):
    base = dict(home=None, profile="data", out=None, force=False, with_secrets=False,
                encrypt=False, crypto="v2", allow_plaintext_secrets=False,
                no_scrub=False, db_snapshot=True, anonymize=False, exclude=[],
                dry_run=False, password=None, password_env=None, password_file=None,
                only=None, into=None, skip_verify=False)
    base.update(kw)
    return argparse.Namespace(**base)


def _menu_backup(with_secrets: bool) -> None:
    profile = _ask("profile [data/source/full]（默认 data）: ", "data")
    if profile not in PROFILES:
        warn("未知 profile，返回 data")
        profile = "data"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    default_out = str(Path.cwd() / ("hermes-backup-%s-%s%s" % (
        profile, stamp, ".hbz" if with_secrets else ".zip")))
    out = _ask("输出文件（默认 %s）: " % default_out, default_out)
    password = None
    if with_secrets:
        if not HAVE_AES:
            warn("未安装 cryptography，将使用 v1（纯标准库）加密")
        pw1 = getpass.getpass("设置口令: ")
        pw2 = getpass.getpass("再输一次: ")
        if pw1 != pw2 or not pw1:
            fail("两次口令不一致或为空")
        password = pw1
    args = _ns(profile=profile, out=out, with_secrets=with_secrets,
               encrypt=with_secrets, password=password, force=False)
    cmd_backup(args)


def _menu_verify() -> None:
    archive = _ask("备份包路径: ")
    if not archive:
        return
    cmd_verify(_ns(archive=archive))


def _menu_list() -> None:
    archive = _ask("备份包路径: ")
    if not archive:
        return
    cmd_list(_ns(archive=archive))


def _menu_restore() -> None:
    archive = _ask("备份包路径: ")
    if not archive:
        return
    target = _ask("还原到（默认自动识别 Hermes 数据目录）: ", "")
    into = target or None
    log("先做一次预演（不会写文件）...")
    cmd_restore(_ns(archive=archive, into=into, dry_run=True))
    if _ask("确认还原？(y/N): ", "n").lower() != "y":
        log("已取消")
        return
    try:
        cmd_restore(_ns(archive=archive, into=into, force=False))
    except SystemExit:
        if _ask("存在冲突文件，覆盖？(y/N): ", "n").lower() == "y":
            cmd_restore(_ns(archive=archive, into=into, force=True))
        else:
            log("已取消")


def run_interactive() -> int:
    handlers = {
        "1": lambda: _menu_backup(False),
        "2": lambda: _menu_backup(True),
        "3": _menu_verify,
        "4": _menu_list,
        "5": _menu_restore,
        "6": lambda: cmd_selftest(_ns()),
    }
    print_menu()
    while True:
        choice = _ask("\n" + cyan("请选择 [0-6]: "), "")
        if choice in ("0", "q", "quit", "exit"):
            log("再见。")
            return 0
        handler = handlers.get(choice)
        if handler is None:
            warn("无效选项：%s" % choice)
            continue
        try:
            handler()
        except SystemExit as exc:
            if exc.code not in (0, None):
                warn("操作未完成（退出码 %s）" % exc.code)
        except KeyboardInterrupt:
            log("\n已中断。")
            return 0
        _pause()


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
    b.add_argument("--crypto", choices=["v1", "v2"], default="v2",
                   help="v2 = AES-256-CTR + HMAC (needs cryptography); v1 = stdlib only")
    b.add_argument("--allow-plaintext-secrets", action="store_true",
                   help="allow --with-secrets without encryption (not recommended)")
    b.add_argument("--no-scrub", action="store_true",
                   help="do not redact secret-like values inside config files")
    b.add_argument("--no-db-snapshot", action="store_false", dest="db_snapshot", default=True,
                   help="archive SQLite files as-is instead of a consistent snapshot")
    b.add_argument("--anonymize", action="store_true", help="hide hostname and home path in the manifest")
    b.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                   help="extra relative glob(s) to skip, repeatable")
    b.add_argument("--dry-run", action="store_true", help="report what would be archived, write nothing")
    b.add_argument("--force", action="store_true", help="overwrite an existing archive")
    secret_flags(b)
    b.set_defaults(func=cmd_backup)

    v = sub.add_parser("verify", help="audit an archive against its manifest")
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
    r.add_argument("--skip-verify", action="store_true", help="(deprecated) audit always runs")
    r.add_argument("--dry-run", action="store_true", help="report, write nothing")
    secret_flags(r)
    r.set_defaults(func=cmd_restore)

    t = sub.add_parser("selftest", help="run an offline round-trip test")
    t.set_defaults(func=cmd_selftest)
    return p


def _pause_if_interactive() -> None:
    """Keep the console open on double-click (no args) instead of flashing."""
    if os.environ.get("HERMES_BACKUP_NO_PAUSE"):
        return
    try:
        if sys.stdin and sys.stdin.isatty():
            input("\n按回车键退出...")
    except (EOFError, OSError):
        pass


def main(argv=None) -> int:
    _enable_vt()
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        if exc.code not in (0, None):
            _pause_if_interactive()
        raise
    if not getattr(args, "command", None):
        if sys.stdin and sys.stdin.isatty():
            return run_interactive()
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
