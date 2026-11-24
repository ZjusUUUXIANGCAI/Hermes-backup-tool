import unittest

from _helper import hb


class PathSafetyTest(unittest.TestCase):
    def test_accepts_plain_relative_paths(self):
        for rel in ("a/b.txt", "state.db", "sessions/a.json", "a b/c.txt"):
            self.assertEqual(hb.validate_rel(rel), rel)

    def test_rejects_escapes_and_windows_traps(self):
        bad = ["../x", "/abs", "C:/x", "C:\\\\x", "a\\\\b", "CON", "a..",
               "x/../../y", "", "a/./b", "nul.txt", "a/b.", "a/b "]
        for rel in bad:
            with self.assertRaises(SystemExit, msg=rel):
                hb.validate_rel(rel)

    def test_audit_flags_unsafe_manifest_path(self):
        import argparse, tempfile, zipfile
        from pathlib import Path
        from _helper import make_home, backup_args
        with tempfile.TemporaryDirectory() as tmp:
            home = make_home(Path(tmp))
            out = Path(tmp) / "a.zip"
            self.assertEqual(hb.cmd_backup(backup_args(home, out)), 0)
            with zipfile.ZipFile(str(out)) as zf:
                man = hb.read_manifest(zf)
                man["entries"].append({"path": "../evil.txt", "size": 1, "sha256": "0" * 64})
                _, problems = hb.audit_archive(zf, man)
            self.assertTrue(any("unsafe path" in p for p in problems), problems)


if __name__ == "__main__":
    unittest.main()
