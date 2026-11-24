import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _helper import hb, make_home


class InteractiveTest(unittest.TestCase):
    def run_menu(self, answers):
        it = iter(answers)
        with mock.patch("builtins.input", lambda *a, **k: next(it)):
            return hb.run_interactive()

    def test_quit(self):
        self.assertEqual(self.run_menu(["0"]), 0)

    def test_invalid_then_quit(self):
        self.assertEqual(self.run_menu(["9", "0"]), 0)

    def test_selftest_then_quit(self):
        self.assertEqual(self.run_menu(["6", "", "0"]), 0)

    def test_backup_flow(self):
        tmp = tempfile.TemporaryDirectory()
        home = make_home(Path(tmp.name))
        out = Path(tmp.name) / "menu.zip"
        os.environ["HERMES_HOME"] = str(home)
        try:
            rc = self.run_menu(["1", "data", str(out), "", "0"])
            wrote = out.exists()
        finally:
            os.environ.pop("HERMES_HOME", None)
            tmp.cleanup()
        self.assertEqual(rc, 0)
        self.assertTrue(wrote, "menu backup did not write an archive")

    def test_verify_flow(self):
        tmp = tempfile.TemporaryDirectory()
        home = make_home(Path(tmp.name))
        out = Path(tmp.name) / "v.zip"
        os.environ["HERMES_HOME"] = str(home)
        try:
            self.assertEqual(hb.cmd_backup(hb_args(home, out)), 0)
            rc = self.run_menu(["3", str(out), "", "0"])
        finally:
            os.environ.pop("HERMES_HOME", None)
            tmp.cleanup()
        self.assertEqual(rc, 0)


def hb_args(home, out):
    from _helper import backup_args
    return backup_args(home, out)


if __name__ == "__main__":
    unittest.main()
