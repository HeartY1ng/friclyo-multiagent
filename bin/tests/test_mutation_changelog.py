"""变异回归：仅使用临时文件和离线替身，不接触宿主账号。"""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from frclyo import changelog
class MutationChangelogTests(unittest.TestCase):
    def test_first_history_row_participates_in_maximum(self):
        # 没有表头的历史台账，首行最大编号也必须参与分配。
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(changelog.config, 'root_dir', return_value=Path(tmp)):
            path = Path(tmp) / '台账/改动台账.md'
            path.parent.mkdir()
            old = '| 9 | previous | undo |\n'
            path.write_text(old)
            self.assertEqual(changelog.append('next', 'undo'), 10)
            self.assertTrue(path.read_text().startswith(old))
    def test_append_creates_missing_root_ancestors(self):
        # 首次记账时，多级数据目录不存在也应创建完整目录。
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'new' / 'data'
            with mock.patch.object(changelog.config, 'root_dir', return_value=root):
                self.assertEqual(changelog.append('first', 'undo'), 1)
            self.assertIn('| 1 | first | undo |', (root / '台账/改动台账.md').read_text())
    def test_cli_requires_subcommand(self):
        # 空命令必须由参数校验报告用法错误，不得进入追加处理。
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            changelog.main([])
        self.assertEqual(raised.exception.code, 2)
