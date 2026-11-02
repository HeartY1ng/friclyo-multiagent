"""B128 补登：真实 CLI、共享账本锁、坏行容错和跨来源时间排序。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_audit_receipts import BIN, ReceiptCase, audit, ledger


class ReceiptExceptionTests(ReceiptCase):
    @property
    def sidecar(self):
        return self.paths.ledger.parent / '回执例外.jsonl'

    def cli(self, *args):
        return subprocess.run([str(BIN / 'aw'), 'ledger', *args], cwd=BIN.parent,
                              env=self.env, capture_output=True, text=True, timeout=15)

    def write_rows(self, rows):
        self.sidecar.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n'
                                       for row in rows), encoding='utf-8')

    def row(self, reason='宿主已验收', stamp='2026-01-02T00:00:00Z', task=None):
        return {'时间': stamp, '任务': task or self.task, '原因': reason}

    def test_closed_card_cli_preserves_ledger_and_reports_warning(self):
        self.card()
        before = self.paths.ledger.read_bytes()
        proc = self.cli('回执例外', self.task, '宿主已验收，补登记说明')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.paths.ledger.read_bytes(), before)
        self.assertEqual(ledger.fold(ledger.read_events(self.paths.ledger))[self.task]['状态'], '完成')
        row = json.loads(self.sidecar.read_text())
        self.assertEqual(set(row), {'时间', '任务', '原因', '轮次清单'})
        self.assertEqual(row['任务'], self.task)
        self.assertEqual(row['轮次清单'], [])
        self.assertEqual(row['原因'], '宿主已验收，补登记说明')
        self.assertRegex(row['时间'], r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$')
        report = audit.check()
        self.assertEqual(report['问题'], [])
        self.assertEqual(report['核对卡数'], 1)
        self.assertTrue(any('⚠️ 回执例外已说明' in line for line in report['提醒']))
        shown = self.cli('查', self.task)
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn('来源：回执例外.jsonl', shown.stdout)
        self.assertIn(row['原因'], shown.stdout)

    def test_all_opened_states_accept_english_alias(self):
        self.card(done=False)
        initial = self.paths.ledger.read_bytes()
        for action, fields in ((None, {}), ('卡住', {'原因': '等输入'}),
                               ('完成', {'结论': '未通过', '摘要': ''}),
                               ('放弃', {'原因': '取消'})):
            with self.subTest(action=action):
                self.paths.ledger.write_bytes(initial)
                if action:
                    with ledger.LedgerLock(self.paths.ledger) as lock:
                        lock.append([ledger.event(action, self.task, **fields)])
                before = self.paths.ledger.read_bytes()
                proc = self.cli('receipt-exception', self.task, '补登 ' + str(action))
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(self.paths.ledger.read_bytes(), before)
        self.assertEqual(len(self.sidecar.read_text().splitlines()), 4)

    def test_empty_and_missing_reason_do_not_write(self):
        self.card()
        before = self.paths.ledger.read_bytes()
        for extra in ([], [''], [' \t\n　']):
            with self.subTest(extra=extra):
                proc = self.cli('回执例外', self.task, *extra)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertFalse(self.sidecar.exists())
                self.assertEqual(self.paths.ledger.read_bytes(), before)

    def test_unknown_task_rejected_with_or_without_ledger(self):
        proc = self.cli('receipt-exception', 'CL-20000101-99', '原因')
        self.assertEqual(proc.returncode, 1)
        self.assertIn('任务不存在', proc.stderr)
        self.assertFalse(self.paths.ledger.exists())
        self.assertFalse(self.sidecar.exists())
        self.card()
        before = self.paths.ledger.read_bytes()
        proc = self.cli('回执例外', 'CL-20000101-99', '原因')
        self.assertEqual(proc.returncode, 1)
        self.assertIn('任务不存在', proc.stderr)
        self.assertEqual(self.paths.ledger.read_bytes(), before)
        self.assertFalse(self.sidecar.exists())

    def test_malformed_ledger_remains_strict(self):
        self.card()
        with self.paths.ledger.open('ab') as fh:
            fh.write((json.dumps(ledger.event('备注', self.task, 内容='收卡后非法备注')) + '\n').encode())
        before = self.paths.ledger.read_bytes()
        proc = self.cli('回执例外', self.task, '原因')
        self.assertEqual(proc.returncode, 1)
        self.assertIn('操作了已结束的卡', proc.stderr)
        self.assertEqual(self.paths.ledger.read_bytes(), before)
        self.assertFalse(self.sidecar.exists())

    def test_concurrent_appends_preserve_both_records(self):
        self.card()
        before = self.paths.ledger.read_bytes()
        reasons = ['并发 A：' + '中文' * 5000, '并发 B：' + 'text' * 5000]
        processes = [subprocess.Popen([str(BIN / 'aw'), 'ledger', command, self.task, reason],
                     cwd=BIN.parent, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                     for command, reason in zip(('回执例外', 'receipt-exception'), reasons)]
        try:
            for proc in processes:
                out, err = proc.communicate(timeout=15)
                self.assertEqual(proc.returncode, 0, out + err)
        finally:
            for proc in processes:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate()
        rows = [json.loads(line) for line in self.sidecar.read_text().splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertEqual({row['原因'] for row in rows}, set(reasons))
        self.assertEqual(self.paths.ledger.read_bytes(), before)
        self.assertFalse((self.sidecar.parent / '.账本.lock').exists())

    def test_append_waits_for_existing_ledger_transaction(self):
        self.card()
        proc = None
        try:
            with ledger.LedgerLock(self.paths.ledger):
                proc = subprocess.Popen([str(BIN / 'aw'), 'ledger', '回执例外', self.task, '共享锁'],
                       cwd=BIN.parent, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                time.sleep(0.3)
                self.assertIsNone(proc.poll())
                self.assertFalse(self.sidecar.exists())
            out, err = proc.communicate(timeout=15)
            self.assertEqual(proc.returncode, 0, out + err)
            self.assertEqual(json.loads(self.sidecar.read_text())['原因'], '共享锁')
        finally:
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.communicate()

    def test_bad_lines_are_skipped_with_one_report_warning(self):
        self.card()
        bad = [b'{broken', b'\xff', b'null', b'[]', b'', b'{}',
               b'[' * 2000 + b'0' + b']' * 2000,
               b'{"extra":' + b'[' * 2000 + b'0' + b']' * 2000 + b'}',
               json.dumps(self.row(reason='  ')).encode(),
               json.dumps(self.row(stamp='2026-02-30T00:00:00Z')).encode(),
               json.dumps(self.row(stamp='2026-1-2T00:00:00Z')).encode(),
               json.dumps(dict(self.row(), 原因=123)).encode(),
               json.dumps(dict(self.row(), 任务=' ')).encode()]
        data = b'\n'.join(bad + [json.dumps(self.row()).encode()]) + b'\n'
        self.sidecar.write_bytes(data)
        report = audit.check()
        self.assertEqual(report['问题'], [])
        self.assertEqual(report['核对卡数'], 1)
        warning = [line for line in report['提醒'] if '已跳过' in line]
        self.assertEqual(len(warning), 1)
        self.assertIn('第 ' + '、'.join(str(n) for n in range(1, len(bad) + 1)) + ' 行', warning[0])
        self.assertTrue(any('回执例外已说明：宿主已验收' in line for line in report['提醒']))
        self.assertEqual(self.sidecar.read_bytes(), data)
        proc = self.cli('show', self.task)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('宿主已验收', proc.stdout)
        self.assertIn('已跳过', proc.stderr)

    def test_bad_rows_alone_do_not_suppress_receipt_problem(self):
        self.card()
        self.write_rows([self.row(reason=' '), self.row(task='CL-20000101-99')])
        report = audit.check()
        self.assertEqual(len(report['问题']), 1)
        self.assertEqual(len(report['提醒']), 1)
        self.assertIn('没有交付轮次', report['问题'][0])

    def test_torn_tail_is_preserved_and_separated_before_append(self):
        self.card()
        bad = b'{torn'
        self.sidecar.write_bytes(bad)
        proc = self.cli('回执例外', self.task, '新有效原因')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self.sidecar.read_bytes().startswith(bad + b'\n'))
        rows, warnings = ledger.read_receipt_exceptions(self.paths.ledger)
        self.assertEqual([row['原因'] for row in rows], ['新有效原因'])
        self.assertEqual(len(warnings), 1)
        self.assertEqual(self.report()[1], 0)

    def test_latest_time_across_both_sources_and_empty_notes(self):
        self.card(['回执例外：账本最新', '回执例外：账本较旧', '回执例外：  ', '普通备注'])
        events = ledger.read_events(self.paths.ledger)
        for event, stamp in zip(events[1:4], ('2026-01-04T00:00:00Z',
                                            '2026-01-02T00:00:00Z', '2026-01-09T00:00:00Z')):
            event['时间'] = stamp
        self.paths.ledger.write_text(''.join(json.dumps(e, ensure_ascii=False) + '\n' for e in events))
        cases = [([self.row('旁文件旧', '2026-01-03T00:00:00Z')], '账本最新'),
                 ([self.row('旁文件最新', '2026-01-05T00:00:00Z'),
                   self.row('末行较旧', '2026-01-01T00:00:00Z'),
                   self.row('  ', '2026-01-10T00:00:00Z')], '旁文件最新'),
                 ([], '账本最新')]
        for rows, expected in cases:
            with self.subTest(expected=expected):
                self.write_rows(rows)
                lines, count = self.report()
                self.assertEqual(count, 0)
                exception = [line for line in lines if '回执例外已说明' in line]
                self.assertEqual(len(exception), 1)
                self.assertIn('回执例外已说明：' + expected + '（', exception[0])

    def test_equal_times_use_last_sidecar_record_and_show_both_sources(self):
        self.card(['回执例外：账本原因'])
        stamp = ledger.read_events(self.paths.ledger)[1]['时间']
        self.write_rows([self.row('旁文件首次', stamp), self.row('旁文件末次', stamp)])
        self.assertIn('回执例外已说明：旁文件末次', self.report()[0][0])
        out = self.cli('查', self.task)
        self.assertEqual(out.returncode, 0, out.stderr)
        for content in ('来源：账本备注', '来源：回执例外.jsonl', '账本原因', '旁文件首次', '旁文件末次'):
            self.assertIn(content, out.stdout)

    def test_missing_sidecar_stays_read_only_and_healthy_receipt_stays_success(self):
        self.card()
        self.delivery('r1')
        self.assertEqual(self.report()[1], 0)
        self.assertEqual(self.cli('show', self.task).returncode, 0)
        self.assertFalse(self.sidecar.exists())
        self.assertEqual(self.cli('回执例外', self.task, '历史原因').returncode, 0)
        self.assertTrue(self.report()[0][0].startswith('✅'))
        self.assertEqual(audit.check()['提醒'], [])

    def test_utf8_separators_are_not_jsonl_line_breaks(self):
        self.card()
        reason = '宿主\u2028验收\u2029通过\x85说明\n第二行'
        proc = self.cli('回执例外', self.task, reason)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows, warnings = ledger.read_receipt_exceptions(self.paths.ledger)
        self.assertEqual(warnings, [])
        self.assertEqual(rows[0]['原因'], reason)
        self.assertEqual(self.report()[1], 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
