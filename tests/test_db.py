import sqlite3
import tempfile
import unittest
from pathlib import Path

from _helper import hb


class SqliteSnapshotTest(unittest.TestCase):
    def test_wal_snapshot_contains_committed_rows(self):
        tmp = Path(tempfile.mkdtemp())
        db = tmp / "state.db"
        con = sqlite3.connect(str(db))
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE t (v INTEGER)")
        for i in range(200):
            con.execute("INSERT INTO t VALUES (?)", (i,))
        con.commit()
        self.assertTrue((tmp / "state.db-wal").exists(), "WAL file should exist before snapshot")
        snap = hb.snapshot_sqlite(db, tmp)
        con.close()
        check = sqlite3.connect(str(snap))
        try:
            self.assertEqual(check.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(check.execute("SELECT count(*) FROM t").fetchone()[0], 200)
        finally:
            check.close()

    def test_is_sqlite_detection(self):
        tmp = Path(tempfile.mkdtemp())
        fake = tmp / "notadb.db"
        fake.write_bytes(b"not a database")
        self.assertFalse(hb.is_sqlite(fake))


if __name__ == "__main__":
    unittest.main()
