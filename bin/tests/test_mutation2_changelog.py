"""第二轮改动台账回归：检查落盘顺序和实际排他锁，不触碰用户台账。"""
import fcntl
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from frclyo import changelog

class Mutation2ChangelogTests(unittest.TestCase):
    def test_new_row_is_flushed_before_fsync_and_fsync_is_required(self):
        # fsync 时另一个描述符必须已能读到新行，且必须真正调用 fsync。
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path=root/'台账/改动台账.md';real_fsync=os.fsync
            def durable(fd):
                self.assertIn('| 1 | synthetic change | synthetic undo |\n',path.read_text())
                real_fsync(fd)
            with mock.patch.object(changelog.config,'root_dir',return_value=root), \
                    mock.patch.object(changelog.os,'fsync',side_effect=durable) as sync:
                self.assertEqual(changelog.append('synthetic change','synthetic undo'),1)
            sync.assert_called_once()

    def test_exclusive_lock_is_held_while_publishing(self):
        # 写入期间独立打开的锁描述符不能再次加锁，保证读号和追加不竞争。
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);real_fsync=os.fsync
            def check_lock(fd):
                with (root/'台账/.改动台账.lock').open('a+b') as other:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(other.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                real_fsync(fd)
            with mock.patch.object(changelog.config,'root_dir',return_value=root), \
                    mock.patch.object(changelog.os,'fsync',side_effect=check_lock):
                self.assertEqual(changelog.append('change','undo'),1)

if __name__=='__main__':unittest.main()
