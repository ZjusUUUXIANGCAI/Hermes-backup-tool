import argparse
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

from _helper import hb, backup_args, make_home


def restore_args(archive, into, **over):
    base = dict(archive=str(archive), into=str(into), force=False, only=None,
                skip_verify=False, dry_run=False, password_env=None, password_file=None)
    base.update(over)
    return argparse.Namespace(**base)


class RestoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = make_home(self.root)
        self.zip = self.root / "a.zip"
        self.assertEqual(hb.cmd_backup(backup_args(self.home, self.zip)), 0)

    def tearDown(self):
        self.tmp.cleanup()

    def test_roundtrip(self):
        dest = self.root / "restored"
        self.assertEqual(hb.cmd_restore(restore_args(self.zip, dest)), 0)
        self.assertEqual((dest / "sessions" / "a.json").read_text(encoding="utf-8"), "hello")
        self.assertTrue((dest / "state.db").exists())

    def test_refuses_conflict_without_force(self):
        dest = self.root / "restored"
        self.assertEqual(hb.cmd_restore(restore_args(self.zip, dest)), 0)
        with self.assertRaises(SystemExit):
            hb.cmd_restore(restore_args(self.zip, dest))
        self.assertEqual(hb.cmd_restore(restore_args(self.zip, dest, force=True)), 0)

    def test_dry_run_writes_nothing(self):
        dest = self.root / "dry"
        self.assertEqual(hb.cmd_restore(restore_args(self.zip, dest, dry_run=True)), 0)
        self.assertFalse(dest.exists())

    def test_refuses_unsafe_archive(self):
        bad = self.root / "bad.zip"
        with zipfile.ZipFile(str(self.zip)) as zin, zipfile.ZipFile(str(bad), "w") as zout:
            for name in zin.namelist():
                zout.writestr(name, zin.read(name))
            zout.writestr("../evil.txt", "x")
        with zipfile.ZipFile(str(bad), "a") as zf:
            pass
        # manually patch manifest to declare the traversal entry
        with zipfile.ZipFile(str(bad)) as zf:
            man = hb.read_manifest(zf)
        man["entries"].append({"path": "../evil.txt", "size": 1,
                               "sha256": hb.sha256_bytes(b"x")})
        rebuilt = self.root / "bad2.zip"
        with zipfile.ZipFile(str(bad)) as zin, zipfile.ZipFile(str(rebuilt), "w") as zout:
            for name in zin.namelist():
                if name == hb.MANIFEST:
                    continue
                zout.writestr(name, zin.read(name))
            zout.writestr(hb.MANIFEST, __import__("json").dumps(man))
        dest = self.root / "unsafe"
        with self.assertRaises(SystemExit):
            hb.cmd_restore(restore_args(rebuilt, dest, force=True))
        self.assertFalse((self.root / "evil.txt").exists())

    def test_refuses_symlink_parent(self):
        dest = self.root / "linked"
        dest.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        try:
            os.symlink(str(outside), str(dest / "sessions"), target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest("cannot create symlink here: %s" % exc)
        with self.assertRaises(SystemExit):
            hb.cmd_restore(restore_args(self.zip, dest, force=True))
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
