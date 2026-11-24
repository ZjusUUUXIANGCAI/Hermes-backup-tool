import argparse
import importlib.util
import sqlite3
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load():
    spec = importlib.util.spec_from_file_location("hermes_backup", ROOT / "hermes_backup.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hb = load()


def backup_args(home, out, **over):
    base = dict(home=str(home), profile="data", out=str(out), force=False,
                with_secrets=False, encrypt=False, crypto="v2",
                allow_plaintext_secrets=False, no_scrub=False,
                db_snapshot=True, anonymize=True, exclude=[], dry_run=False,
                password_env=None, password_file=None)
    base.update(over)
    return argparse.Namespace(**base)


def make_home(root: Path):
    home = root / "home"
    (home / "sessions").mkdir(parents=True)
    (home / "platforms").mkdir()
    (home / "cache").mkdir()
    (home / "config.yaml").write_text(
        "model:\n  default: test\n  api_key: sk-abcdef1234567890xyz\n"
        "  redact_secrets: true\ntelegram_token: 123456:AAAAsecret\n", encoding="utf-8")
    (home / "auth.json").write_text('{"token":"super-secret"}', encoding="utf-8")
    (home / "platforms" / "creds.json").write_text('{"t":"x"}', encoding="utf-8")
    (home / "sessions" / "a.json").write_text("hello", encoding="utf-8")
    (home / "cache" / "huge.bin").write_bytes(b"x" * 4096)
    con = sqlite3.connect(str(home / "state.db"))
    con.execute("CREATE TABLE t (v TEXT)")
    con.execute("INSERT INTO t VALUES ('hello')")
    con.commit()
    con.close()
    return home


def rewrite_zip(src: Path, dst: Path, mutate):
    with zipfile.ZipFile(str(src)) as zin, zipfile.ZipFile(str(dst), "w") as zout:
        for name in zin.namelist():
            data = zin.read(name)
            mutated = mutate(name, data)
            if mutated is None:
                continue
            zout.writestr(name, mutated)
