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
from frclyo import ledger
class MutationLedgerTests(unittest.TestCase):
    def test_non_object_event_is_reported_as_ledger_error(self):
        # 合法 JSON 数组也不是事件，必须给出账本错误而不是泄漏 AttributeError。
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'ledger.jsonl'; path.write_text('[]\n')
            with self.assertRaisesRegex(ledger.LedgerError, '第 1 行'): ledger.read_events(path)
    def test_error_line_number_is_one_based(self):
        # 第二行损坏时错误定位必须是第二行，帮助人工修复而非错改相邻记录。
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'ledger.jsonl'
            first = ledger.event('备注', 'fixture', 内容='ok')
            path.write_text(json.dumps(first) + '\n{broken}\n')
            with self.assertRaisesRegex(ledger.LedgerError, '第 2 行'): ledger.read_events(path)
    def test_default_guard_never_creates_lock_file(self):
        # 只读消费者在没有辅助锁文件时保持目录不变。
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'ledger.jsonl'
            with ledger.guard(path): self.assertEqual(list(path.parent.iterdir()), [])
            self.assertEqual(list(path.parent.iterdir()), [])
