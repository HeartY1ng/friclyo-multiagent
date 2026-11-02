"""Signal/evidence control flow with an explicit reaper double (no process scan).

These tests do not establish that OS descendants were reaped; that is covered
by test_b131_interruption.py on a host with process-snapshot permissions.
"""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('verify_b131_state', ROOT / 'tests/host/verify_all.py')
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)


class SignalStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(os.environ['TMPDIR']).resolve())
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()

    def test_first_signal_wins_and_handlers_restore(self):
        before = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
        with verify.interrupt_scope() as state:
            os.kill(os.getpid(), signal.SIGINT)
            os.kill(os.getpid(), signal.SIGTERM)
            self.assertEqual(state.returncode, 130)
            with verify.interrupt_scope() as nested:
                self.assertIs(nested, state)
        for sig, handler in before.items():
            self.assertEqual(signal.getsignal(sig), handler)
        with verify.interrupt_scope() as state:
            self.assertIsNone(state.signal)

    def test_exception_restores_handlers(self):
        before = signal.getsignal(signal.SIGTERM)
        with self.assertRaisesRegex(RuntimeError, 'fixture'):
            with verify.interrupt_scope():
                raise RuntimeError('fixture')
        self.assertEqual(signal.getsignal(signal.SIGTERM), before)
        self.assertIsNone(verify._interruption)

    def test_interrupted_before_launch_does_not_spawn(self):
        with verify.interrupt_scope() as state, mock.patch.object(verify.subprocess, 'Popen') as spawn:
            state.receive(signal.SIGTERM, None)
            row = verify.run_check('pending', ['unused'], 'exit', self.base / 'case', 1)
        spawn.assert_not_called()
        self.assertEqual(row['status'], '被中断')
        self.assertEqual(row['signal'], signal.SIGTERM)
        self.assertTrue((self.base / 'case/result.json').is_file())

    def test_signal_during_spawn_still_reaps_assigned_process(self):
        proc = mock.Mock(pid=12345, args=['fixture'])
        proc.poll.return_value = None
        with verify.interrupt_scope() as state:
            def spawn(*args, **kwargs):
                state.receive(signal.SIGTERM, None)
                return proc

            with mock.patch.object(verify.subprocess, 'Popen', side_effect=spawn), \
                 mock.patch.object(verify, 'Descendants') as descendants, \
                 mock.patch.object(verify, 'reap_group', return_value=True) as reap:
                row = verify.run_check('pending', ['fixture'], 'exit', self.base / 'case', 1,
                                       shutdown_grace=0)
        reap.assert_called_once_with(proc, grace=0.3)
        descendants.return_value.sample.assert_called()
        proc.send_signal.assert_called_once_with(signal.SIGTERM)
        self.assertEqual(row['status'], '被中断')
        self.assertTrue(row['group_reaped'])

    def test_incomplete_reaping_cannot_report_success(self):
        with mock.patch.object(verify, 'Descendants'), \
             mock.patch.object(verify, 'reap_group', return_value=False):
            row = verify.run_check('fixture', [sys.executable, '-B', '-c', 'pass'],
                                   'exit', self.base / 'case', 3)
        self.assertEqual(row['returncode'], 0)
        self.assertEqual(row['status'], '失败')
        self.assertFalse(row['group_reaped'])

    def test_completed_results_survive_both_signals(self):
        for sig in (signal.SIGTERM, signal.SIGINT):
            output = self.base / str(sig)
            def check(name, command, kind, directory, timeout):
                row = dict(name=name, status='通过', reason='', seconds=0, returncode=0)
                if name == 'pending':
                    verify._interruption.receive(sig, None)
                    row.update(status='被中断', interrupted=True, signal=sig)
                return row

            jobs = [(name, ['fixture'], 'exit') for name in ('completed', 'pending', 'later')]
            with mock.patch.object(verify, 'plan', return_value=jobs), \
                 mock.patch.object(verify, 'run_check', side_effect=check) as launch, \
                 contextlib.redirect_stdout(io.StringIO()):
                rc = verify.main(['--output', str(output)])
            self.assertEqual(rc, 128 + sig)
            self.assertEqual(launch.call_count, 2)
            rows = json.loads((output / 'summary.json').read_text())
            self.assertEqual([row['status'] for row in rows[:3]], ['通过', '被中断', '跳过'])
            self.assertIn('结论：被中断', (output / 'summary.txt').read_text())

    def test_signal_between_checks_stops_next_launch(self):
        def check(*args):
            verify._interruption.receive(signal.SIGTERM, None)
            return dict(name='completed', status='通过', reason='', seconds=0, returncode=0)

        with mock.patch.object(verify, 'plan', return_value=[('a', [], 'exit'), ('b', [], 'exit')]), \
             mock.patch.object(verify, 'run_check', side_effect=check) as launch, \
             contextlib.redirect_stdout(io.StringIO()):
            rc = verify.main(['--output', str(self.base / 'evidence')])
        self.assertEqual(rc, 143)
        self.assertEqual(launch.call_count, 1)
        self.assertIn('结论：被中断', (self.base / 'evidence/summary.txt').read_text())

    def test_signal_while_writing_final_summary(self):
        output = self.base / 'summary'
        output.mkdir()
        original = Path.write_text
        rows = [dict(name='completed', status='通过', reason='', seconds=0)]
        with verify.interrupt_scope() as state:
            def write(path, text, *args, **kwargs):
                if path.name == 'summary.txt':
                    state.receive(signal.SIGINT, None)
                return original(path, text, *args, **kwargs)

            with mock.patch.object(Path, 'write_text', write):
                report = verify.write_summary(rows, output, state)
        self.assertIn('结论：被中断', report)
        self.assertTrue(json.loads((output / 'summary.json').read_text())[-1]['interrupted'])
        self.assertEqual(rows[0]['status'], '通过')

    def test_run_all_keeps_running_after_normal_failure(self):
        runner_spec = importlib.util.spec_from_file_location('runner_b131_state', ROOT / 'tests/run_all.py')
        runner = importlib.util.module_from_spec(runner_spec)
        runner_spec.loader.exec_module(runner)
        output = self.base / 'offline-evidence'
        output.mkdir()
        state = verify.Interruption()

        def check(name, *args, **kwargs):
            return dict(name=name, status='失败' if name == 'bin/tests' else '通过',
                        reason='', seconds=0, returncode=1 if name == 'bin/tests' else 0)

        with mock.patch.object(runner, 'run_check', side_effect=check) as launch, \
             mock.patch.object(runner.tempfile, 'mkdtemp', return_value=str(output)), \
             mock.patch.dict(os.environ, {'FRCLYO_E2E': '0'}), \
             contextlib.redirect_stdout(io.StringIO()):
            # mkdtemp also serves TemporaryDirectory; keep that fixture separate.
            with mock.patch.object(runner.tempfile, 'TemporaryDirectory') as temporary:
                temporary.return_value.__enter__.return_value = str(self.base / 'isolated')
                (self.base / 'isolated').mkdir()
                rc = runner.run_groups(ROOT, state)
        self.assertEqual(rc, 1)
        self.assertEqual(launch.call_count, 3)
        rows = json.loads((output / 'summary.json').read_text())
        self.assertEqual([row['status'] for row in rows], ['失败', '通过', '通过'])

    def test_normal_command_logs_preserved_with_reaper_double(self):
        with mock.patch.object(verify, 'Descendants'), \
             mock.patch.object(verify, 'reap_group', return_value=True):
            row = verify.run_check('fixture', [sys.executable, '-B', '-c', "print('normal')"],
                                   'exit', self.base / 'case', 3)
        self.assertEqual(row['status'], '通过')
        self.assertEqual(Path(row['log']).read_text(), 'normal\n')


if __name__ == '__main__':
    unittest.main(verbosity=2)
