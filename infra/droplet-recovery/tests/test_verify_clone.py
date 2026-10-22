import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('verify', Path(__file__).parents[1] / 'verify-clone.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class CloneChecks(unittest.TestCase):
    def test_production_and_mismatch_refused(self):
        for droplet, ip, actual, host in [('587384488','8.8.4.4','587384488','dr-rehearsal-test'),
                                         ('999999','64.227.0.147','999999','dr-rehearsal-test'),
                                         ('999999','147.182.206.23','999999','dr-rehearsal-test'),
                                         ('999999','8.8.4.4','888888','dr-rehearsal-test'),
                                         ('999999','8.8.4.4','999999','production')]:
            with self.assertRaises(ValueError):
                m.guard(droplet, ip, actual, ip, host)
        m.guard('999999','8.8.4.4','999999','8.8.4.4','dr-rehearsal-test')

    def test_reads_wal_and_preserves_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'test.sqlite'
            with sqlite3.connect(path) as writer:
                writer.execute('PRAGMA journal_mode=WAL')
                writer.execute('PRAGMA wal_autocheckpoint=0')
                writer.execute('CREATE TABLE example (id integer primary key)')
                writer.execute('INSERT INTO example VALUES (1)')
                writer.commit()
                result = m.validate_sqlite(path)
                self.assertEqual(result['rows'], 1)
                self.assertEqual(writer.execute('SELECT count(*) FROM example').fetchone()[0], 1)

    def test_missing_empty_corrupt_and_bad_foreign_keys_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'test.sqlite'
            with self.assertRaises(ValueError): m.validate_sqlite(path)
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE parent (id integer primary key)')
                db.execute('CREATE TABLE child (parent_id integer references parent(id))')
                db.commit()
                with self.assertRaises(ValueError): m.validate_sqlite(path)
                db.execute('INSERT INTO child VALUES (2)')
            with self.assertRaises(ValueError): m.validate_sqlite(path)
            path.write_bytes(b'not a sqlite database')
            with self.assertRaises(sqlite3.DatabaseError): m.validate_sqlite(path)


if __name__ == '__main__': unittest.main()
