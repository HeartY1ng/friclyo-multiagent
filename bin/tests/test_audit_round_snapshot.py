"""B146：两种来源的轮次快照，以及旧记录秒级边界。"""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_audit_receipts import ReceiptCase, audit, ledger

STAMP = '2026-01-02T00:00:00Z'


class RoundSnapshotTests(ReceiptCase):
    def failed(self, name, stamp=STAMP):
        run = self.delivery(name, '失败')
        if stamp is not None:
            (run / '运行信息.json').write_text(json.dumps({'开始': stamp}))
        return run

    def register(self, source):
        with patch.object(ledger, 'utc', return_value=STAMP):
            if source == 'note':
                self.assertEqual(ledger.main(['完成', self.task, '--结论', '通过',
                                             '--回执例外', '已核对旧轮']), 0)
                return ledger.read_events(self.paths.ledger)[-2]
            self.assertEqual(ledger.main(['回执例外', self.task, '已核对旧轮']), 0)
            return json.loads(ledger.receipt_exception_path(self.paths.ledger).read_text())

    def snapshot_case(self, source):
        self.card(done=source == 'sidecar')
        self.failed('old-round')
        self.failed('r1', None)
        row = self.register(source)
        self.assertEqual(row['轮次清单'], ['r1', 'old-round'])
        self.assertEqual(ledger.fold(ledger.read_events(self.paths.ledger))[self.task]['状态'], '完成')
        self.failed('new-round')
        self.failed('no-start', None)
        # 时间早于例外仍不能给不在名单内的轮次豁免。
        self.failed('earlier', '2026-01-01T00:00:00Z')
        report = audit.check()
        problems = '\n'.join(report['问题'])
        warnings = '\n'.join(report['提醒'])
        for name in ('new-round', 'no-start', 'earlier'):
            self.assertIn(name + '（', problems)
            self.assertNotIn(name + '（', warnings)
        for name in ('old-round', 'r1'):
            self.assertNotIn(name + '（', problems)
            self.assertIn(name + '（', warnings)
        # 恢复新轮不撤销清单里旧轮的覆盖，即使开始记录缺失。
        with ledger.LedgerLock(self.paths.ledger) as lock:
            lock.append([dict(ledger.event('恢复', self.task, 轮次='new-round'), 时间=STAMP),
                         ledger.event('完成', self.task, 结论='通过', 摘要='')])
        self.assertIn('old-round（', '\n'.join(audit.check()['提醒']))
        self.assertIn('new-round（', '\n'.join(audit.check()['问题']))

    def test_note_snapshot_does_not_cover_later_same_second_round(self):
        self.snapshot_case('note')

    def test_sidecar_snapshot_does_not_cover_later_same_second_round(self):
        self.snapshot_case('sidecar')

    def legacy_case(self, source):
        self.card(done=False)
        if source == 'note':
            with ledger.LedgerLock(self.paths.ledger) as lock:
                lock.append([dict(ledger.event('备注', self.task, 内容='回执例外：旧格式'), 时间=STAMP)])
        else:
            ledger.receipt_exception_path(self.paths.ledger).write_text(json.dumps(
                dict(时间=STAMP, 任务=self.task, 原因='旧格式')) + '\n')
        with ledger.LedgerLock(self.paths.ledger) as lock:
            lock.append([ledger.event('完成', self.task, 结论='通过', 摘要='')])
        for name, stamp in [('before', '2026-01-01T00:00:00Z'), ('same', STAMP),
                            ('after', '2026-01-03T00:00:00Z')]:
            self.failed(name, stamp)
        report = audit.check()
        self.assertIn('before（', '\n'.join(report['提醒']))
        self.assertNotIn('before（', '\n'.join(report['问题']))
        for name in ('same', 'after'):
            self.assertIn(name + '（', '\n'.join(report['问题']))
            self.assertNotIn(name + '（', '\n'.join(report['提醒']))

    def test_legacy_note_strict_time_boundary(self):
        self.legacy_case('note')

    def test_legacy_sidecar_strict_time_boundary(self):
        self.legacy_case('sidecar')

    def test_empty_snapshot_does_not_cover_future_round(self):
        self.card()
        row = self.register('sidecar')
        self.assertEqual(row['轮次清单'], [])
        self.assertEqual(self.report()[1], 0)
        self.failed('new-round', None)
        self.assertEqual(self.report()[1], 1)

    def test_bad_sidecar_snapshot_is_skipped_not_treated_as_legacy(self):
        self.card()
        self.failed('old-round', '2026-01-01T00:00:00Z')
        for names in (None, 'old-round', {}, [1], ['-bad'], ['../old-round'], ['']):
            with self.subTest(names=names):
                ledger.receipt_exception_path(self.paths.ledger).write_text(json.dumps(dict(
                    时间=STAMP, 任务=self.task, 原因='错误清单', 轮次清单=names)) + '\n')
                report = audit.check()
                self.assertEqual(len(report['问题']), 1)
                self.assertTrue(any('已跳过' in line for line in report['提醒']))

    def test_bad_note_snapshot_does_not_fall_back_to_time(self):
        self.card(done=False)
        with ledger.LedgerLock(self.paths.ledger) as lock:
            lock.append([dict(ledger.event('备注', self.task, 内容='回执例外：错误清单',
                                            轮次清单='old-round'), 时间=STAMP),
                         ledger.event('完成', self.task, 结论='通过', 摘要='')])
        self.failed('old-round', '2026-01-01T00:00:00Z')
        self.assertEqual(self.report()[1], 1)

    def test_snapshot_cannot_exempt_unfinished_child(self):
        self.card()
        self.register('sidecar')
        with ledger.LedgerLock(self.paths.ledger) as lock:
            lock.append([ledger.event('开卡', self.task + '-A', 标题='子卡', 项目='测试',
                                       来源='CL', 派活人='', 父任务=self.task)])
        self.assertIn('子卡尚未收卡', '\n'.join(audit.check()['问题']))


if __name__ == '__main__':
    unittest.main(verbosity=2)
