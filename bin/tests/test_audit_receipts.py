"""B127/B128：临时账本与交付证据，兼作原版/修复版复现断言。"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

BIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BIN))
from frclyo import audit, ledger


class ReceiptCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(os.environ['TMPDIR']).resolve())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        cfg = self.root / 'config.json'
        cfg.write_text('{}')
        self.env = dict(os.environ, FRCLYO_HOME=str(self.root), FRCLYO_CONFIG=str(cfg),
                        PYTHONDONTWRITEBYTECODE='1')
        self.env_patch = patch.dict(os.environ, self.env)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.paths = ledger.Paths()
        self.task = 'CL-20000101-01'

    def card(self, notes=(), done=True):
        events = [ledger.event('开卡', self.task, 标题='证据测试', 项目='测试', 来源='CL', 派活人='测试', 父任务='')]
        events.extend(ledger.event('备注', self.task, 内容=note) for note in notes)
        if done:
            events.append(ledger.event('完成', self.task, 结论='通过', 摘要='已核对'))
        with ledger.LedgerLock(self.paths.ledger) as lock:
            lock.append(events)

    def delivery(self, rnd, status='执行完成'):
        run = self.paths.deliver / self.task / rnd
        run.mkdir(parents=True)
        data = b'evidence'
        (run / '结果.txt').write_bytes(data)
        (run / '回执.json').write_text(json.dumps(dict(任务编号=self.task, 轮次编号=rnd,
            状态=status, 结果sha256=hashlib.sha256(data).hexdigest())))
        return run

    def report(self):
        before = self.paths.ledger.read_bytes()
        result = audit.audit(self.paths)
        self.assertEqual(self.paths.ledger.read_bytes(), before)
        return result


class B127Tests(ReceiptCase):
    def test_named_rounds_all_pass(self):
        self.card()
        for rnd in ('astra-m', 'judge-Q01', 'astra-l-Q01-2', 'A_1'):
            self.delivery(rnd)
        lines, count = self.report()
        self.assertEqual(count, 0, lines)
        self.assertIn('共 4 轮', lines[0])

    def test_named_round_failure_reports_round_and_reason(self):
        self.card()
        self.delivery('astra-m', '失败')
        self.delivery('judge-Q01')
        lines, count = self.report()
        self.assertEqual(count, 1)
        self.assertIn('共 2 轮，其中 1 轮有问题：astra-m（', lines[0])
        self.assertIn('失败', lines[0])

    def test_mixed_rounds_check_old_numeric_evidence_too(self):
        self.card()
        self.delivery('r9', '失败')
        self.delivery('r10')
        self.delivery('judge-Q01')
        lines, count = self.report()
        self.assertEqual(count, 1, lines)
        self.assertIn('共 3 轮，其中 1 轮有问题：r9（', lines[0])

    def test_numeric_only_keeps_latest_and_empty_directory_rule(self):
        self.card()
        self.delivery('r9', '失败')
        self.delivery('r10')
        self.assertEqual(self.report()[1], 0)
        (self.paths.deliver / self.task / 'r11').mkdir()
        lines, count = self.report()
        self.assertEqual(count, 1)
        self.assertIn('最新一轮 r11 没有回执', lines[0])
        self.assertEqual(ledger.round_numbers(self.paths.deliver, self.task),
                         [(9, 'r9'), (10, 'r10'), (11, 'r11')])

    def test_named_requires_evidence_and_legal_name(self):
        self.card()
        self.delivery('astra-m')
        for name in ('notes', 'empty-round'):
            (self.paths.deliver / self.task / name).mkdir()
        for name in ('bad.name', '中文', 'bad name'):
            self.delivery(name, '失败')
        lines, count = self.report()
        self.assertEqual(count, 0, lines)
        self.assertIn('共 1 轮', lines[0])

    def test_runtime_info_without_receipt_is_a_round(self):
        self.card()
        self.delivery('astra-m')
        run = self.paths.deliver / self.task / 'judge-Q01'
        run.mkdir()
        (run / '运行信息.json').write_text('{}')
        lines, count = self.report()
        self.assertEqual(count, 1)
        self.assertIn('共 2 轮，其中 1 轮有问题：judge-Q01（', lines[0])
        self.assertIn('没有回执', lines[0])

    def test_each_round_identity_hash_and_missing_result(self):
        self.card()
        for name in ('a', 'b', 'c', 'd'):
            self.delivery(name)
        base = self.paths.deliver / self.task
        for name, field, value in (('a', '任务编号', 'other'), ('b', '轮次编号', 'other'),
                                   ('c', '结果sha256', 'bad')):
            path = base / name / '回执.json'
            rec = json.loads(path.read_text())
            rec[field] = value
            path.write_text(json.dumps(rec))
        (base / 'd/结果.txt').unlink()
        lines, count = self.report()
        self.assertEqual(count, 1)
        self.assertIn('共 4 轮，其中 4 轮有问题', lines[0])
        for reason in ('任务编号不匹配', '轮次编号不匹配', '校验码不一致', '没有结果.txt'):
            self.assertIn(reason, lines[0])

    def test_error_list_limited_to_five(self):
        self.card()
        for i in range(7):
            self.delivery('round-%d' % i, '失败')
        lines, count = self.report()
        self.assertEqual(count, 1)
        self.assertIn('共 7 轮，其中 7 轮有问题', lines[0])
        for i in range(5):
            self.assertIn('round-%d（' % i, lines[0])
        self.assertNotIn('round-5', lines[0])
        self.assertNotIn('round-6', lines[0])
        self.assertTrue(lines[0].endswith('等'))


class B128Tests(ReceiptCase):
    def test_exception_without_round_is_warning_and_cli_success(self):
        self.card(['回执例外：宿主已验收', '普通补充说明'])
        lines, count = self.report()
        self.assertEqual(count, 0, lines)
        self.assertTrue(lines[0].startswith('⚠️ 回执例外已说明：宿主已验收'), lines)
        self.assertIn(self.task, lines[0])
        report = audit.check()
        self.assertEqual(report['问题'], [])
        self.assertEqual(report['提醒'], lines[:-1])
        proc = subprocess.run([sys.executable, '-B', '-m', 'frclyo.audit'], cwd=BIN,
                              env=self.env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)['提醒'], report['提醒'])

    def test_failed_receipt_with_exception_is_warning(self):
        self.card(['回执例外：收尾快照失败；宿主测试已通过'])
        self.delivery('r1', '失败')
        lines, count = self.report()
        self.assertEqual(count, 0, lines)
        self.assertIn('⚠️ 回执例外已说明：收尾快照失败；宿主测试已通过', lines[0])

    def test_empty_or_embedded_prefix_does_not_suppress_problem(self):
        self.card(['说明：回执例外：尚未验收', '回执例外：  '])
        lines, count = self.report()
        self.assertEqual(count, 1)
        self.assertTrue(lines[0].startswith('❌'))

    def test_healthy_card_with_exception_stays_success(self):
        self.card(['回执例外：历史说明'])
        self.delivery('r1')
        lines, count = self.report()
        self.assertEqual(count, 0)
        self.assertTrue(lines[0].startswith('✅'))
        self.assertEqual(audit.check()['提醒'], [])

    def test_done_cli_chinese_and_english_options(self):
        self.card(done=False)
        before = self.paths.ledger.read_bytes()
        for command, option in (('完成', '--回执例外'), ('done', '--receipt-exception')):
            with self.subTest(option=option):
                self.paths.ledger.write_bytes(before)
                proc = subprocess.run([str(BIN / 'aw'), 'ledger', command, self.task,
                    '--结论', '通过', '--摘要', '宿主已验收', option, '收尾快照失败'],
                    cwd=BIN.parent, env=self.env, capture_output=True, text=True)
                if proc.returncode and 'unrecognized arguments:' not in proc.stderr:
                    raise RuntimeError('CLI 准备或运行错误：' + proc.stderr)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                events = ledger.read_events(self.paths.ledger)
                self.assertEqual([e['动作'] for e in events], ['开卡', '备注', '完成'])
                self.assertEqual(events[-2]['内容'], '回执例外：收尾快照失败')
                self.assertEqual(events[-1]['摘要'], '宿主已验收')
                self.assertEqual(ledger.fold(events)[self.task]['状态'], '完成')
                self.assertTrue(self.paths.ledger.read_bytes().startswith(before))
                self.assertEqual(self.report()[1], 0)

    def test_done_empty_exception_rejected_without_append(self):
        self.card(done=False)
        before = self.paths.ledger.read_bytes()
        proc = subprocess.run([str(BIN / 'aw'), 'ledger', '完成', self.task,
            '--结论', '通过', '--回执例外', '  '], cwd=BIN.parent,
            env=self.env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(self.paths.ledger.read_bytes(), before)


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:], sys.modules[__name__]) if len(sys.argv) > 1 else unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(2 if result.errors or result.skipped else 1 if result.failures else 0)
