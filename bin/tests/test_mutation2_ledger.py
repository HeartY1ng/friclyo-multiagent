"""第二轮账本回归：独立临时账本，检查创建权限和追加字节边界。"""
import contextlib
import io
import json
import os
from types import SimpleNamespace
from unittest import mock
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from frclyo import ledger

class Mutation2LedgerTests(unittest.TestCase):
    def test_ledger_creation_keeps_private_mode_under_normal_umask(self):
        # 普通 umask 下也必须显式创建 0600，不能依赖入口的严 umask 遮住缺陷。
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'ledger.jsonl'
            previous=os.umask(0o022)
            try:
                with ledger.LedgerLock(path) as lock:
                    lock.append([{'fixture':'only synthetic data'}])
            finally:os.umask(previous)
            self.assertEqual(path.stat().st_mode & 0o777,0o600)

    def test_read_only_entry_returns_explicit_success_without_creating_ledger(self):
        # 进程内调用和 CLI 共用成功返回约定，空列表也不创建账本。
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'missing'/'ledger.jsonl'
            with mock.patch.object(ledger,'Paths',return_value=SimpleNamespace(ledger=path)), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(ledger.main(['列表']),0)
            self.assertIn('任务号',output.getvalue())
            self.assertFalse(path.parent.exists())

    def test_default_append_does_not_silently_repair_partial_tail(self):
        # 账本不同于表现记录：默认不补 LF，损坏证据必须仍被严格读取器发现。
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'ledger.jsonl'
            first=ledger.event('备注','T',内容='old')
            second=ledger.event('备注','T',内容='new')
            prefix=json.dumps(first,ensure_ascii=False).encode('utf-8')
            path.write_bytes(prefix)
            with ledger.LedgerLock(path) as lock:lock.append([second])
            self.assertEqual(path.read_bytes(),prefix+(json.dumps(second,ensure_ascii=False)+'\n').encode('utf-8'))
            with self.assertRaises(ledger.LedgerError):ledger.read_events(path)

if __name__=='__main__':unittest.main()
