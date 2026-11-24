import tempfile
import unittest
import zipfile
from pathlib import Path

from _helper import hb, backup_args, make_home, rewrite_zip


class VerifyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = make_home(Path(self.tmp.name))
        self.zip = Path(self.tmp.name) / "ok.zip"
        self.assertEqual(hb.cmd_backup(backup_args(self.home, self.zip)), 0)

    def tearDown(self):
        self.tmp.cleanup()

    def test_clean_archive_passes(self):
        with zipfile.ZipFile(str(self.zip)) as zf:
            man = hb.read_manifest(zf)
            checked, problems = hb.audit_archive(zf, man)
        self.assertGreater(checked, 0)
        self.assertEqual(problems, [])

    def test_detects_unlisted_extra_entry(self):
        bad = Path(self.tmp.name) / "extra.zip"
        rewrite_zip(self.zip, bad, lambda name, data: data)
        with zipfile.ZipFile(str(bad), "a") as zf:
            zf.writestr("sneaky.txt", "x")
        with zipfile.ZipFile(str(bad)) as zf:
            man = hb.read_manifest(zf)
            _, problems = hb.audit_archive(zf, man)
        self.assertTrue(any("not listed" in p for p in problems), problems)

    def test_detects_duplicate_zip_entry(self):
        bad = Path(self.tmp.name) / "dup.zip"
        with zipfile.ZipFile(str(bad), "w") as zf:
            zf.writestr("a.txt", "1")
            zf.writestr("a.txt", "2")
        with zipfile.ZipFile(str(bad)) as zf:
            man = {"tool": hb.TOOL, "format": hb.FORMAT, "entries": [],
                   "totals": {"files": 0, "bytes": 0}}
            _, problems = hb.audit_archive(zf, man)
        self.assertTrue(any("duplicate zip entry" in p for p in problems), problems)

    def test_detects_size_and_hash_mismatch(self):
        with zipfile.ZipFile(str(self.zip)) as zf:
            man = hb.read_manifest(zf)
            entry = next(e for e in man["entries"] if e["path"] == "config.yaml")
            entry["size"] += 1
            entry["sha256"] = "0" * 64
            _, problems = hb.audit_archive(zf, man)
        self.assertTrue(any("size mismatch" in p for p in problems), problems)
        self.assertTrue(any("hash mismatch" in p for p in problems), problems)


if __name__ == "__main__":
    unittest.main()
