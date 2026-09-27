"""Standalone server/database regression; never writes the running project's DB."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from backend import Store, Invalid


class BackendTest(unittest.TestCase):
    def test_transaction_validation_and_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(__file__).resolve().parent, Path(tmp) / 'test.sqlite3')
            before = store.read()
            response, status = store.save({'revision': before['revision'], 'data': before['data']}, 'test')
            self.assertEqual(status, 200)
            self.assertEqual(store.save({'revision': before['revision'], 'data': before['data']})[1], 409)
            with self.assertRaises((Invalid, TypeError, KeyError)):
                store.save({'revision': response['revision'], 'data': None})
            self.assertEqual(store.read()['revision'], response['revision'])
            self.assertEqual(Store(store.root, store.path).read()['data'], before['data'])
            with store.connect() as db:
                self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                self.assertEqual(db.execute('SELECT count(*) FROM audit').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
