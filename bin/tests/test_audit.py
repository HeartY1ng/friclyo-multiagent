"""证据核对只看账本；测试坏数据、过滤规则和只读性质。"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

# 使用项目公共库，不读取用户账本。
BIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BIN))
from frclyo import audit, ledger


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="audit-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.cfg = self.root / "config.json"
        self.cfg.write_text(json.dumps({"work_dir": str(self.root / "work")}))
        self.env = dict(os.environ, FRCLYO_HOME=str(self.root / "work"), FRCLYO_CONFIG=str(self.cfg))
        with patch.dict(os.environ, self.env):
            self.paths = ledger.Paths()

    def record(self, verdict="通过"):
        """在临时账本构造符合既有事件格式的完成卡。"""
        events = [ledger.event("开卡", "CL-20000101-01", 标题="证据", 项目="测试", 来源="CL", 派活人="测试", 父任务=""),
                  ledger.event("完成", "CL-20000101-01", 结论=verdict, 摘要="测试")]
        with ledger.LedgerLock(self.paths.ledger) as lock:
            lock.append(events)

    def record_delivery(self, task, rnd, data=b"evidence"):
        """为已完成卡补一份可核对的最新轮次证据。"""
        directory = self.paths.deliver / task / rnd
        directory.mkdir(parents=True)
        (directory / "结果.txt").write_bytes(data)
        (directory / "回执.json").write_text(
            json.dumps({"任务编号": task, "轮次编号": rnd, "状态": "执行完成", "结果sha256": hashlib.sha256(data).hexdigest()}))

    def open_parent(self, parent):
        """开一张完成通过的总卡，父任务为空。"""
        events = [ledger.event("开卡", parent, 标题="总卡", 项目="测试", 来源="CL", 派活人="测试", 父任务=""),
                  ledger.event("完成", parent, 结论="通过", 摘要="测试")]
        with ledger.LedgerLock(self.paths.ledger) as lock:
            lock.append(events)

    def add_child(self, parent, kid, done=True):
        """给总卡追加一张子卡；done 为 False 时停在开卡状态。"""
        events = [ledger.event("开卡", kid, 标题="子卡", 项目="测试", 来源="CL", 派活人="测试", 父任务=parent)]
        if done:
            events.append(ledger.event("完成", kid, 结论="通过", 摘要="测试"))
        with ledger.LedgerLock(self.paths.ledger) as lock:
            lock.append(events)

    def test_result_limit_matches_executor(self):
        self.record()
        data = b"x" * (17 * 1024 * 1024)
        self.record_delivery("CL-20000101-01", "r1", data)
        with patch.dict(os.environ, WORKER_RESULT_MAX_BYTES=str(32 * 1024 * 1024)):
            self.assertEqual(audit.audit(self.paths)[1], 0)
        with patch.dict(os.environ, WORKER_RESULT_MAX_BYTES=str(16 * 1024 * 1024)):
            self.assertEqual(audit.audit(self.paths)[1], 1)

    def test_receipt_identity_must_match_task_and_latest_round(self):
        """B66：即使结果哈希正确，其他任务、旧轮次及缺标识回执都不能通过日审。"""
        self.record()
        task = 'CL-20000101-01'
        self.record_delivery(task, 'r9')
        self.record_delivery(task, 'r10')
        receipt_path = self.paths.deliver / task / 'r10' / '回执.json'
        original = json.loads(receipt_path.read_text())
        self.assertEqual(audit.audit(self.paths)[1], 0)
        for field, wrong in (('任务编号', 'CL-20000101-02'), ('轮次编号', 'r9')):
            for value in (wrong, None, '', 10):
                with self.subTest(field=field, value=value):
                    receipt = dict(original, **{field: value})
                    receipt_path.write_text(json.dumps(receipt))
                    before = receipt_path.read_bytes()
                    lines, bad = audit.audit(self.paths)
                    self.assertEqual(bad, 1)
                    self.assertIn('回执' + field + '不匹配', lines[0])
                    self.assertEqual(receipt_path.read_bytes(), before)
            receipt = dict(original)
            del receipt[field]
            receipt_path.write_text(json.dumps(receipt))
            lines, bad = audit.audit(self.paths)
            self.assertEqual(bad, 1)
            self.assertIn('回执' + field + '不匹配', lines[0])
        receipt_path.write_text(json.dumps(original))
        self.assertEqual(audit.audit(self.paths)[1], 0)

    def test_result_limit_lower_boundary_and_invalid_setting(self):
        self.record()
        self.record_delivery("CL-20000101-01", "r1", b"1234")
        for limit, expected in (("4", 0), ("3", 1), ("0", 1), ("invalid", 1)):
            with self.subTest(limit=limit), patch.dict(os.environ, WORKER_RESULT_MAX_BYTES=limit):
                self.assertEqual(audit.audit(self.paths)[1], expected)

    def test_missing_ledger_is_read_only_empty(self):
        lines, bad = audit.audit(self.paths)
        self.assertEqual((lines, bad), (["已核对完成卡 0 张，问题 0 个"], 0))
        self.assertFalse(self.paths.ledger.parent.exists())

    def test_failed_cards_and_unregistered_delivery_not_audited(self):
        self.record("未通过")
        (self.paths.deliver / "UNREGISTERED" / "r1").mkdir(parents=True)
        before = self.paths.ledger.read_bytes()
        lines, bad = audit.audit(self.paths)
        self.assertEqual((lines[-1], bad), ("已核对完成卡 0 张，问题 0 个", 0))
        self.assertEqual(self.paths.ledger.read_bytes(), before)
        self.assertFalse((self.paths.ledger.parent / ".账本.lock").exists())

    def test_receipt_wrong_shape_and_multiple_latest_round_failures(self):
        self.record()
        for rnd in ("r2", "r10"):
            directory = self.paths.deliver / "CL-20000101-01" / rnd
            directory.mkdir(parents=True)
            (directory / "结果.txt").write_bytes(b"evidence")
            (directory / "回执.json").write_text(json.dumps({"任务编号": "CL-20000101-01", "轮次编号": rnd, "状态": "执行完成", "结果sha256": hashlib.sha256(b"evidence").hexdigest()}))
        before = self.paths.ledger.read_bytes()
        self.assertEqual(audit.audit(self.paths)[1], 0)
        for invalid in ("[]", "null", "{broken", '{"状态":"执行完成"}'):
            (directory / "回执.json").write_text(invalid)
            self.assertEqual(audit.audit(self.paths)[1], 1)
        self.assertEqual(self.paths.ledger.read_bytes(), before)

    def test_parent_with_all_children_received_passes(self):
        self.open_parent("CL-20000101-01")
        self.add_child("CL-20000101-01", "CL-20000101-01-A")
        self.add_child("CL-20000101-01", "CL-20000101-01-B")
        self.record_delivery("CL-20000101-01-A", "r1")
        self.record_delivery("CL-20000101-01-B", "r1")
        lines, bad = audit.audit(self.paths)
        self.assertEqual(bad, 0)
        self.assertTrue(any("总卡" in line and "证据在子卡" in line for line in lines))

    def test_parent_with_unfinished_child_reports_problem(self):
        self.open_parent("CL-20000101-01")
        self.add_child("CL-20000101-01", "CL-20000101-01-A")
        self.add_child("CL-20000101-01", "CL-20000101-01-B", done=False)
        self.record_delivery("CL-20000101-01-A", "r1")
        lines, bad = audit.audit(self.paths)
        self.assertEqual(bad, 1)
        self.assertTrue(any("CL-20000101-01-B" in line and "开卡" in line for line in lines))

    def test_no_rounds_and_no_children_keeps_original_problem(self):
        self.record()
        lines, bad = audit.audit(self.paths)
        self.assertEqual(bad, 1)
        self.assertTrue(any("没有交付轮次，也没有回执" in line for line in lines))

    def test_result_fifo_reports_problem_without_hanging(self):
        self.record()
        directory = self.paths.deliver / "CL-20000101-01" / "r1"
        directory.mkdir(parents=True)
        (directory / "回执.json").write_text(json.dumps({"任务编号": "CL-20000101-01", "轮次编号": "r1", "状态": "执行完成", "结果sha256": ""}))
        os.mkfifo(str(directory / "结果.txt"))
        start = time.monotonic()
        lines, bad = audit.audit(self.paths)
        elapsed = time.monotonic() - start
        self.assertEqual(bad, 1)
        self.assertLess(elapsed, 5)

    def test_result_symlink_reports_problem_without_reading_target(self):
        self.record()
        directory = self.paths.deliver / "CL-20000101-01" / "r1"
        directory.mkdir(parents=True)
        (directory / "回执.json").write_text(json.dumps({"任务编号": "CL-20000101-01", "轮次编号": "r1", "状态": "执行完成", "结果sha256": ""}))
        sentinel = self.root / "哨兵.txt"
        sentinel.write_text("不应被读取的内容")
        (directory / "结果.txt").symlink_to(sentinel)
        lines, bad = audit.audit(self.paths)
        self.assertEqual(bad, 1)
        self.assertEqual(sentinel.read_text(), "不应被读取的内容")

    def test_bad_ledger_and_bad_config_cli_nonzero(self):
        self.paths.ledger.parent.mkdir()
        self.paths.ledger.write_text("坏行\n")
        command = [sys.executable, "-m", "frclyo.audit"]
        proc = subprocess.run(command, cwd=BIN, env=self.env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("第 1 行", proc.stdout)
        self.cfg.write_text("null")
        proc = subprocess.run(command, cwd=BIN, env=self.env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("配置", proc.stdout)


if __name__ == "__main__":
    unittest.main()
