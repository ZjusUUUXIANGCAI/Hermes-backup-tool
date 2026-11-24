import hashlib
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

from _helper import hb, backup_args, make_home


class BackupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = make_home(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_archive_excludes_secrets_and_scrubs_config(self):
        out = Path(self.tmp.name) / "a.zip"
        self.assertEqual(hb.cmd_backup(backup_args(self.home, out)), 0)
        with zipfile.ZipFile(str(out)) as zf:
            names = set(zf.namelist())
            cfg = zf.read("config.yaml").decode("utf-8")
        self.assertNotIn("auth.json", names)
        self.assertNotIn("platforms/creds.json", names)
        self.assertNotIn("cache/huge.bin", names)
        self.assertNotIn("sk-abcdef", cfg)
        self.assertNotIn("AAAAsecret", cfg)
        self.assertIn("redact_secrets: true", cfg)

    def test_manifest_hash_matches_stored_bytes(self):
        out = Path(self.tmp.name) / "b.zip"
        self.assertEqual(hb.cmd_backup(backup_args(self.home, out)), 0)
        with zipfile.ZipFile(str(out)) as zf:
            man = hb.read_manifest(zf)
            for entry in man["entries"]:
                digest = hashlib.sha256(zf.read(entry["path"])).hexdigest()
                self.assertEqual(digest, entry["sha256"], entry["path"])

    def test_sqlite_is_snapshotted_consistently(self):
        out = Path(self.tmp.name) / "c.zip"
        self.assertEqual(hb.cmd_backup(backup_args(self.home, out)), 0)
        target = Path(self.tmp.name) / "db"
        target.mkdir()
        with zipfile.ZipFile(str(out)) as zf:
            (target / "state.db").write_bytes(zf.read("state.db"))
        con = sqlite3.connect(str(target / "state.db"))
        try:
            self.assertEqual(con.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(con.execute("SELECT v FROM t").fetchone()[0], "hello")
        finally:
            con.close()

    def test_dry_run_writes_nothing(self):
        out = Path(self.tmp.name) / "never.zip"
        self.assertEqual(hb.cmd_backup(backup_args(self.home, out, dry_run=True)), 0)
        self.assertFalse(out.exists())

    def test_plaintext_secrets_need_explicit_flag(self):
        out = Path(self.tmp.name) / "x.zip"
        with self.assertRaises(SystemExit):
            hb.cmd_backup(backup_args(self.home, out, with_secrets=True))
        self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
