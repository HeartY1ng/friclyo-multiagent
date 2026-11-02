"""B134/B135：恢复边界、两种例外来源、证据与卡状态的区分。"""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_audit_receipts import ReceiptCase, audit, ledger


class ExceptionScopeTests(ReceiptCase):
    def history(self, source, new_exception=False, same_second=False):
        old, reopened, later = ('2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z',
                                 '2026-01-03T00:00:00Z')
        def event(action, task, stamp, **fields):
            return dict(ledger.event(action, task, **fields), 时间=stamp)
        events = [event('开卡', self.task, stamp=old, 标题='周期测试', 项目='测试',
                               来源='CL', 派活人='', 父任务='')]
        if source == 'note':
            events.append(event('备注', self.task, stamp=reopened if same_second else old,
                                       内容='回执例外：旧轮原因'))
        events.extend([event('完成', self.task, stamp=old, 结论='通过', 摘要='r1'),
                       event('恢复', self.task, stamp=reopened, 轮次='r2')])
        if source == 'note' and new_exception:
            events.append(event('备注', self.task, stamp=reopened if same_second else later,
                                       内容='回执例外：新轮原因'))
        events.append(event('完成', self.task, stamp=later, 结论='通过', 摘要='r2'))
        with ledger.LedgerLock(self.paths.ledger) as lock:
            lock.append(events)
        if source == 'sidecar':
            rows = [dict(时间=reopened if same_second else old, 任务=self.task, 原因='旧轮原因')]
            if new_exception:
                rows.append(dict(时间=reopened if same_second else later, 任务=self.task, 原因='新轮原因'))
            ledger.receipt_exception_path(self.paths.ledger).write_text(
                ''.join(json.dumps(row) + '\n' for row in rows))
        self.delivery('r2', '失败')

    def test_old_note_does_not_cover_reopened_round_same_second(self):
        self.history('note', same_second=True)
        self.assertEqual(self.report()[1], 1)
        self.assertTrue(self.report()[0][0].startswith('❌'))

    def test_old_sidecar_does_not_cover_reopened_round(self):
        self.history('sidecar')
        self.assertEqual(self.report()[1], 1)

    def test_same_second_sidecar_is_not_proof_of_new_exception(self):
        self.history('sidecar', new_exception=True, same_second=True)
        self.assertEqual(self.report()[1], 1)

    def test_legacy_note_same_second_is_not_proof_of_new_exception(self):
        self.history('note', new_exception=True, same_second=True)
        lines, count = self.report()
        self.assertEqual(count, 1, lines)
        self.assertTrue(lines[0].startswith('❌'), lines)

    def test_new_sidecar_after_recovery_covers_new_round(self):
        self.history('sidecar', new_exception=True)
        lines, count = self.report()
        self.assertEqual(count, 0, lines)
        self.assertIn('回执例外已说明：新轮原因', lines[0])

    def exception(self, source):
        if source == 'note':
            self.card(['回执例外：宿主验收'])
        else:
            self.card()
            ledger.append_receipt_exception(self.paths.ledger, self.task, '宿主验收')

    def child_case(self, source, closed=False):
        self.exception(source)
        child = self.task + '-A'
        events = [ledger.event('开卡', child, 标题='子卡', 项目='测试', 来源='CL',
                               派活人='', 父任务=self.task)]
        if closed:
            events.append(ledger.event('放弃', child, 原因='取消'))
        with ledger.LedgerLock(self.paths.ledger) as lock:
            lock.append(events)
        lines, count = self.report()
        self.assertEqual(count, 0 if closed else 1, lines)
        self.assertTrue(lines[0].startswith('✅' if closed else '❌'), lines)
        if not closed:
            self.assertIn('子卡尚未收卡', lines[0])

    def test_children_not_exempted_by_note(self):
        self.child_case('note')

    def test_children_not_exempted_by_sidecar(self):
        self.child_case('sidecar')

    def test_closed_children_still_pass(self):
        self.child_case('sidecar', closed=True)

    def evidence_cases(self, source):
        self.exception(source)
        self.assertEqual(self.report()[1], 0)  # 没有交付轮次
        run = self.delivery('r1')
        if source == 'sidecar':
            ledger.append_receipt_exception(self.paths.ledger, self.task, '宿主验收')
        receipt = run / '回执.json'
        good = json.loads(receipt.read_text())
        variants = [None, [], dict(good, 任务编号='other'), dict(good, 轮次编号='other'),
                    dict(good, 状态='失败'), dict(good, 结果sha256='bad')]
        for data in variants:
            with self.subTest(source=source, data=data):
                if data is None:
                    receipt.unlink()
                else:
                    receipt.write_text(json.dumps(data))
                lines, count = self.report()
                self.assertEqual(count, 0, lines)
                self.assertTrue(lines[0].startswith('⚠️ 回执例外已说明'), lines)
        receipt.write_text(json.dumps(good))
        self.assertTrue(self.report()[0][0].startswith('✅'))
        self.delivery('named-round', '失败')
        if source == 'sidecar':
            ledger.append_receipt_exception(self.paths.ledger, self.task, '宿主验收')
        lines, count = self.report()
        self.assertEqual(count, 0, lines)
        self.assertIn('其中 1 轮有问题', lines[0])
        self.assertTrue(lines[0].startswith('⚠️ 回执例外已说明'))

    def test_receipt_evidence_exempted_by_note(self):
        self.evidence_cases('note')

    def test_receipt_evidence_exempted_by_sidecar(self):
        self.evidence_cases('sidecar')

    def test_unrelated_discovery_error_not_exempted(self):
        self.exception('note')
        with patch.object(audit, 'delivery_rounds', side_effect=ledger.LedgerError('目录结构错误')):
            lines, count = self.report()
        self.assertEqual(count, 1, lines)
        self.assertIn('❌', lines[0])


if __name__ == '__main__':
    unittest.main(verbosity=2)
