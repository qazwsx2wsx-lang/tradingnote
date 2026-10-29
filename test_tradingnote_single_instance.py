import tempfile
import unittest
from pathlib import Path

from PySide6 import QtCore, QtNetwork

from tradingnote_single_instance import _lock_key, ensure_single_instance


class SingleInstanceTests(unittest.TestCase):
    def setUp(self):
        self.app = QtCore.QCoreApplication.instance() or QtCore.QCoreApplication([])
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_second_call_against_same_data_dir_is_blocked(self):
        server = ensure_single_instance(self.data_dir)
        self.assertIsNotNone(server)
        try:
            blocked = ensure_single_instance(self.data_dir)
            self.assertIsNone(blocked)
        finally:
            server.close()
            QtNetwork.QLocalServer.removeServer(_lock_key(self.data_dir))

    def test_different_data_dirs_do_not_block_each_other(self):
        other_dir = Path(tempfile.mkdtemp())
        server_a = ensure_single_instance(self.data_dir)
        server_b = ensure_single_instance(other_dir)
        try:
            self.assertIsNotNone(server_a)
            self.assertIsNotNone(server_b)
        finally:
            server_a.close()
            server_b.close()
            QtNetwork.QLocalServer.removeServer(_lock_key(other_dir))

    def test_lock_is_released_after_clean_shutdown(self):
        server = ensure_single_instance(self.data_dir)
        server.close()
        QtNetwork.QLocalServer.removeServer(_lock_key(self.data_dir))
        reacquired = ensure_single_instance(self.data_dir)
        self.assertIsNotNone(reacquired)
        reacquired.close()
        QtNetwork.QLocalServer.removeServer(_lock_key(self.data_dir))


if __name__ == "__main__":
    unittest.main()
