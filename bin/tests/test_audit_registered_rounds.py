"""B140：例外覆盖登记时已有轮次，两种来源与混合新旧失败。"""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_audit_receipts import ReceiptCase, audit, ledger


class RegisteredRoundsTests(ReceiptCase):
    def history(self, source, newer='执行完成', timestamps=True):
        def event(action, day, **fields):
            return dict(ledger.event(action, self.task, **fields),
                        时间='2026-01-%02dT00:00:00Z' % day)
        events = [event('开卡', 1, 标题='历史轮次', 项目='测试', 来源='CL', 派活人='', 父任务='')]
        if source == 'note':
            events.append(event('备注', 2, 内容='回执例外：旧轮已验收'))
        events.extend([event('完成', 2, 结论='通过', 摘要='旧轮'),
                       event('恢复', 3, 轮次='new-round'),
                       event('完成', 4, 结论='通过', 摘要='新轮')])
        with ledger.LedgerLock(self.paths.ledger) as lock:
            lock.append(events)
        if source == 'sidecar':
            ledger.receipt_exception_path(self.paths.ledger).write_text(json.dumps(dict(
                时间='2026-01-02T00:00:00Z', 任务=self.task, 原因='旧轮已验收')) + '\n')
        for rnd, day, status in [('old-round', 1, '失败'), ('new-round', 3, newer)]:
            run = self.delivery(rnd, status)
            if timestamps:
                (run / '运行信息.json').write_text(json.dumps(dict(开始='2026-01-%02dT00:00:00Z' % day)))

    def test_old_failed_named_round_stays_exempt_after_new_success(self):
        for source in ('note', 'sidecar'):
            with self.subTest(source=source):
                # 每个来源用独立夹具，避免两种例外互相掩盖。
                case = RegisteredRoundsTests()
                case.setUp()
                try:
                    case.history(source)
                    lines, count = case.report()
                    self.assertEqual(count, 0, lines)
                    self.assertIn('旧轮已验收', '\n'.join(lines))
                    self.assertIn('old-round（', '\n'.join(lines))
                    self.assertEqual(audit.check()['问题'], [])
                finally:
                    case.doCleanups()

    def test_new_failed_round_is_reported_beside_exempt_history(self):
        for source in ('note', 'sidecar'):
            with self.subTest(source=source):
                case = RegisteredRoundsTests()
                case.setUp()
                try:
                    case.history(source, newer='失败')
                    report = audit.check()
                    self.assertEqual(len(report['问题']), 1, report)
                    self.assertIn('new-round（', report['问题'][0])
                    self.assertNotIn('old-round（', report['问题'][0])
                    self.assertTrue(any('old-round（' in line for line in report['提醒']))
                finally:
                    case.doCleanups()

    def test_legacy_startless_rounds_use_recorded_recovery_identity(self):
        self.history('note', newer='失败', timestamps=False)
        lines, count = self.report()
        self.assertEqual(count, 1, lines)
        self.assertTrue(any(line.startswith('⚠️') and 'old-round（' in line for line in lines))
        self.assertTrue(any(line.startswith('❌') and 'new-round（' in line for line in lines))

    def test_new_round_start_blocks_old_exception_without_restore(self):
        self.card(['回执例外：历史原因'])
        run = self.delivery('future-round', '失败')
        path = run / '回执.json'
        data = json.loads(path.read_text())
        data['开始'] = '2099-01-01T00:00:00Z'
        path.write_text(json.dumps(data))
        self.assertEqual(self.report()[1], 1)

    def test_later_exception_covers_all_already_started_rounds(self):
        self.history('note', newer='失败')
        ledger.receipt_exception_path(self.paths.ledger).write_text(json.dumps(dict(
            时间='2026-01-05T00:00:00Z', 任务=self.task, 原因='新旧轮均已核对')) + '\n')
        lines, count = self.report()
        self.assertEqual(count, 0, lines)
        self.assertIn('其中 2 轮有问题', lines[0])
        self.assertIn('新旧轮均已核对', lines[0])

    def test_same_second_generic_restore_does_not_reuse_earlier_note(self):
        self.history('note', newer='失败')
        events = ledger.read_events(self.paths.ledger)
        events[1]['时间'] = '2026-01-03T00:00:00Z'
        events[2] = dict(ledger.event('卡住', self.task, 原因='等输入'),
                         时间='2026-01-03T00:00:00Z')
        events[3].pop('轮次')
        self.paths.ledger.write_text(''.join(json.dumps(e) + '\n' for e in events))
        report = audit.check()
        self.assertEqual(len(report['问题']), 1, report)
        self.assertIn('new-round（', report['问题'][0])
        self.assertNotIn('old-round（', report['问题'][0])

    def test_numeric_latest_round_uses_its_own_start(self):
        self.card(['回执例外：历史原因'])
        self.delivery('r1', '失败')
        run = self.delivery('r2', '失败')
        (run / '运行信息.json').write_text('{"开始":"2099-01-01T00:00:00Z"}')
        lines, count = self.report()
        self.assertEqual(count, 1, lines)
        self.assertIn('r2（', lines[0])
        self.assertNotIn('r1（', lines[0])


if __name__ == '__main__':
    unittest.main(verbosity=2)
