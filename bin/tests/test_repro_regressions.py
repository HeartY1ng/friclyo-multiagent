"""B104/B121/B122：确定性协议测试；进程表替身不代表宿主内核验证。"""
import contextlib
import io
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from frclyo import repro


class ReproRegressionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=Path(os.environ['TMPDIR']).resolve())
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name).resolve()
        (self.work / '交付').mkdir()
        self.logs = self.work / 'logs'
        self.logs.mkdir()

    def test_b121_only_exit_code_classifies_logged_exceptions(self):
        log = self.logs / 'evidence.log'
        evidence = (
            b'Traceback (most recent call last):\nValueError: product fault\n',
            b'FAIL: observed\nERROR: captured\nFAILED (failures=1, errors=1)\n',
            b'sandbox-exec: captured denial\nPermissionError: evidence\n',
            b'\xff\nKeyboardInterrupt\nSystemExit: 2\n',
            b'ModuleNotFoundError: another module\n',
        )
        for text in evidence:
            log.write_bytes(text)
            output = repro.inspect_output(log)
            for code, expected in ((0, '没复现'), (1, '复现'), (2, '测试本身出错'),
                                   (3, '测试本身出错'), (-9, '测试本身出错')):
                with self.subTest(text=text, code=code):
                    result = dict(exit_code=code, timed_out=False, launch_error='')
                    self.assertEqual(repro.verdict(result, output)[0], expected)
            self.assertEqual(log.read_bytes(), text)

    def test_executor_failures_are_not_script_results(self):
        for key, value in (('timed_out', True), ('launch_error', 'launch failed'),
                           ('cleanup_error', 'cleanup unconfirmed')):
            for code in (0, 1):
                result = dict(exit_code=code, timed_out=False, launch_error='', cleanup_error='')
                result[key] = value
                self.assertEqual(repro.verdict(result, {'first': '', 'error': ''})[0], '测试本身出错')

    def run_fixture(self, names, code=1, retry=False):
        # 仅替换启动与等待边界；真实 run_test/日志创建/输出读取/判定均运行。
        def attempt(test, cwd, env, profile, log, seconds):
            with log.open('xb') as stream:
                if retry and cwd == self.work:
                    stream.write(b"ModuleNotFoundError: No module named 'frclyo'\n")
                else:
                    stream.write(b'Traceback (most recent call last):\nValueError: observed\nTARGET_RAN\n')
            return dict(cwd=str(cwd), log=log.name,
                        exit_code=2 if retry and cwd == self.work else code,
                        timed_out=False, launch_error='', cleanup_error='')
        rows = []
        with mock.patch.object(repro, 'attempt', side_effect=attempt):
            for name in names:
                rows.append(repro.run_test(self.work / name, self.work, {}, 'fixture', self.logs))
        return rows

    def test_b121_run_test_preserves_evidence_and_exit_zero_or_one(self):
        for code, expected in ((0, '没复现'), (1, '复现')):
            row = self.run_fixture(['test_evidence_%d.py' % code], code=code)[0]
            self.assertEqual(row['verdict'], expected)
            self.assertEqual(row['exit_code'], code)
            self.assertIn('ValueError: observed', (self.logs / row['attempts'][0]['log']).read_text())

    def test_b122_long_names_have_distinct_bounded_logs_and_original_mapping(self):
        names = ['test_' + 'a' * 242 + '.py', 'test_' + 'a' * 241 + 'b.py',
                 'test_' + '测' * 80 + '.py', 'test_' + '测' * 79 + '试.py']
        rows = self.run_fixture(names)
        actual = []
        for name, row in zip(names, rows):
            self.assertEqual(row['test'], name)
            self.assertEqual(row['verdict'], '复现')
            log = row['attempts'][0]['log']
            actual.append(log)
            self.assertLessEqual(len(log.encode('utf-8')), 240)
            self.assertTrue((self.logs / log).is_file())
            self.assertIn('TARGET_RAN', (self.logs / log).read_text())
        self.assertEqual(len(set(actual)), len(names))

    def test_b122_retry_keeps_two_distinct_logs(self):
        row = self.run_fixture(['test_' + 'a' * 242 + '.py'], retry=True)[0]
        self.assertEqual(len(row['attempts']), 2)
        logs = [a['log'] for a in row['attempts']]
        self.assertNotEqual(*logs)
        self.assertTrue(all(len(name.encode('utf-8')) <= 240 for name in logs))
        self.assertIn('ModuleNotFoundError', (self.logs / logs[0]).read_text())
        self.assertIn('TARGET_RAN', (self.logs / logs[1]).read_text())

    def test_b122_short_name_and_byte_boundary(self):
        self.assertEqual(repro.log_name('test_ok.py', 1), 'test_ok.py.attempt-1.log')
        for count in (225, 226, 227, 242):
            name = 'test_' + 'a' * count + '.py'
            for index in (1, 2):
                log = repro.log_name(name, index)
                self.assertLessEqual(len(log.encode()), 240)
                self.assertEqual(log, repro.log_name(name, index))
                self.assertTrue(log.endswith('.attempt-%d.log' % index))

    def lifecycle(self, ending, stubborn=False, profile='fixture'):
        # 真实 Descendants/reap_group，替换 ps 和信号边界；含独立 session 孙进程及无关 PID。
        state = {'parent': True, 'child': True, 'grandchild': True, 'waits': 0}
        process = mock.Mock(spec=['pid', 'poll', 'wait', 'returncode'], pid=100, returncode=None)
        process.poll.side_effect = lambda: None if state['parent'] else process.returncode
        signals = []

        def wait(timeout=None):
            state['waits'] += 1
            if state['waits'] == 1:
                if ending == 'timeout':
                    raise subprocess.TimeoutExpired('fixture', timeout)
                if ending == 'interrupt':
                    raise KeyboardInterrupt
                if ending == 'terminate':
                    signal.raise_signal(signal.SIGTERM)
                state['parent'] = False
                process.returncode = 0
            return process.returncode
        process.wait.side_effect = wait

        def snapshot(command, **kwargs):
            self.assertEqual(command[:2], ['/bin/ps', '-axo'])
            rows = ['1 0 1 S init', '900 1 900 S unrelated']
            if state['parent']:
                rows.append('100 1 100 S parent')
            if state['child']:
                rows.append('200 %d 200 S child' % (100 if state['parent'] else 1))
            if state['grandchild']:
                rows.append('300 %d 300 S grandchild' % (200 if state['child'] else 1))
            return subprocess.CompletedProcess(command, 0, '\n'.join(rows))

        def kill(pid, sig):
            self.assertIn(pid, (200, 300))
            signals.append((pid, sig))
            if sig == signal.SIGKILL or (sig == signal.SIGTERM and not stubborn):
                state['child' if pid == 200 else 'grandchild'] = False

        def group(child, sig):
            self.assertIs(child, process)
            if sig in (signal.SIGTERM, signal.SIGKILL):
                state['parent'] = False
                if process.returncode is None:
                    process.returncode = -sig
            return state['parent']

        reap = repro.execution.reap_group
        previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        with mock.patch.object(repro.subprocess, 'Popen', return_value=process), \
                mock.patch.object(repro.execution.subprocess, 'run', side_effect=snapshot), \
                mock.patch.object(repro.execution.os, 'kill', side_effect=kill), \
                mock.patch.object(repro.execution, 'signal_group', side_effect=group), \
                mock.patch.object(repro.execution, 'group_running', side_effect=lambda p: state['parent']), \
                mock.patch.object(repro.execution, 'reap_group', side_effect=lambda p: reap(p, grace=0)):
            args = (self.work / 'test_probe.py', self.work, {}, profile,
                    self.logs / ('process-%s-%s.log' % (ending, profile is None)),
                    0 if ending == 'timeout' else 10)
            if ending in ('interrupt', 'terminate'):
                with self.assertRaises(KeyboardInterrupt if ending == 'interrupt' else SystemExit) as exc:
                    repro.attempt(*args)
                if ending == 'terminate':
                    self.assertEqual(exc.exception.code, 143)
            else:
                result = repro.attempt(*args)
                self.assertEqual(result['timed_out'], ending == 'timeout')
                self.assertEqual(result['cleanup_error'], '')
                if ending == 'normal':
                    self.assertEqual(result['exit_code'], 0)
        self.assertFalse(state['parent'] or state['child'] or state['grandchild'])
        for pid in (200, 300):
            self.assertIn((pid, signal.SIGKILL if stubborn else signal.SIGTERM), signals)
        self.assertEqual(previous, {sig: signal.getsignal(sig) for sig in previous})

    def test_b104_timeout_reaps_detached_descendants(self):
        self.lifecycle('timeout')

    def test_b104_normal_exit_reaps_tracked_orphans(self):
        self.lifecycle('normal')

    def test_b104_interrupt_reaps_descendants_and_restores_handlers(self):
        self.lifecycle('interrupt')

    def test_b104_sigterm_reaps_descendants_and_preserves_signal_exit(self):
        self.lifecycle('terminate')

    def test_b104_stubborn_descendants_receive_sigkill(self):
        self.lifecycle('timeout', stubborn=True)

    def test_b132_no_sandbox_keeps_timeout_interrupt_and_descendant_cleanup(self):
        for ending in ('normal', 'timeout', 'interrupt', 'terminate'):
            with self.subTest(ending=ending):
                self.lifecycle(ending, profile=None)

    def test_b104_unconfirmed_cleanup_is_reported(self):
        process = mock.Mock(pid=100, returncode=0)
        with mock.patch.object(repro.subprocess, 'Popen', return_value=process), \
                mock.patch.object(repro.execution, 'Descendants'), \
                mock.patch.object(repro.execution, 'reap_group', return_value=False), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            log = self.logs / 'unconfirmed.log'
            result = repro.attempt(self.work / 'test_x.py', self.work, {}, 'fixture', log, 10)
        self.assertIn('未能确认', result['cleanup_error'])
        self.assertIn(result['cleanup_error'], log.read_text())
        self.assertIn(result['cleanup_error'], err.getvalue())
        self.assertEqual(repro.verdict(result, repro.inspect_output(log))[0], '测试本身出错')

    def test_b104_wait_samples_before_parent_exit(self):
        process = mock.Mock(pid=100, returncode=0)
        process.wait.side_effect = [subprocess.TimeoutExpired('fixture', 0.05), 0]
        with mock.patch.object(repro.subprocess, 'Popen', return_value=process), \
                mock.patch.object(repro.execution, 'Descendants') as tracker, \
                mock.patch.object(repro.execution, 'reap_group', return_value=True):
            result = repro.attempt(self.work / 'test_x.py', self.work, {}, 'fixture',
                                   self.logs / 'sample.log', 10)
        self.assertEqual(tracker.return_value.sample.call_count, 2)
        self.assertFalse(result['timed_out'])

    def test_b104_signal_during_cleanup_is_deferred_not_lost(self):
        previous = signal.getsignal(signal.SIGTERM)
        reached = []
        with self.assertRaises(SystemExit) as exc:
            with repro.interruption_handlers() as defer:
                defer()
                signal.raise_signal(signal.SIGTERM)
                reached.append('cleanup completed')
        self.assertEqual(exc.exception.code, 143)
        self.assertEqual(reached, ['cleanup completed'])
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)

    def test_b104_repeated_signal_preserves_original_interrupt(self):
        with self.assertRaises(KeyboardInterrupt):
            with repro.interruption_handlers() as defer:
                try:
                    raise KeyboardInterrupt
                finally:
                    defer()
                    signal.raise_signal(signal.SIGTERM)


if __name__ == '__main__':
    unittest.main(verbosity=2)
