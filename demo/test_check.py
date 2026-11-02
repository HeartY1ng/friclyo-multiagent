"""离线回归仅用合成材料；替身测试不代表 macOS 内核隔离验证。"""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('demo_checker_regression', ROOT / 'demo/check.py')
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


class CheckerRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT, prefix='.check-test-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.work = self.base / 'work'
        self.work.mkdir()
        (self.work / 'solve.py').write_text('print("ok")\n')

    @staticmethod
    def completed(argv, rc=0):
        # 沙箱内可信启动器的完成凭据；替身不声称验证内核隔离。
        Path(argv[7]).write_text(str(rc))

    def test_worker_runs_in_sandbox_copy_with_clean_environment(self):
        child = mock.Mock(pid=987654321, returncode=0)
        child.communicate.return_value = (b'ok\n', b'')
        def start(argv, **kwargs):
            self.completed(argv)
            self.assertEqual(argv[0], '/usr/bin/sandbox-exec')
            self.assertNotEqual(Path(kwargs['cwd']), self.work)
            self.assertEqual((Path(kwargs['cwd']) / 'solve.py').read_text(), 'print("ok")\n')
            profile = argv[argv.index('-p') + 1]
            self.assertIn('(deny network*)', profile)
            self.assertIn('(deny file-write*)', profile)
            for name in ('.hermes', '.codex', '.ssh', '.aws', 'Library/Keychains'):
                self.assertIn(name, profile)
            self.assertNotIn('FAKE_API_KEY', kwargs['env'])
            self.assertNotIn('PYTHONPATH', kwargs['env'])
            return child
        with mock.patch.dict(os.environ, {'FAKE_API_KEY': 'synthetic', 'PYTHONPATH': '/fake'}), \
                mock.patch.object(check.subprocess, 'Popen', side_effect=start), \
                mock.patch.object(check.os, 'killpg'):
            self.assertEqual(check.execute([sys.executable, self.work / 'solve.py'], self.work)[0], 0)

    def test_inputs_and_outputs_use_copies(self):
        source, destination = self.base / 'input.csv', self.base / 'output.csv'
        source.write_bytes(b'original')
        child = mock.Mock(pid=987654321, returncode=0)
        child.communicate.return_value = (b'', b'')
        def start(argv, **kwargs):
            self.completed(argv)
            staged_source, staged_destination = map(Path, argv[-2:])
            self.assertNotEqual(staged_source, source)
            self.assertNotEqual(staged_destination, destination)
            self.assertEqual(staged_source.read_bytes(), b'original')
            self.assertEqual(staged_source.name, source.name)
            staged_source.write_bytes(b'changed copy')
            staged_destination.write_bytes(b'result')
            return child
        with mock.patch.object(check.subprocess, 'Popen', side_effect=start), mock.patch.object(check.os, 'killpg'):
            check.execute([sys.executable, self.work / 'solve.py', source, destination], self.work,
                          inputs=(source,), outputs=(destination,))
        self.assertEqual(source.read_bytes(), b'original')
        self.assertEqual(destination.read_bytes(), b'result')
        self.assertEqual((self.work / 'solve.py').read_text(), 'print("ok")\n')

    def test_missing_sandbox_never_falls_back(self):
        with mock.patch.object(check.subprocess, 'Popen', side_effect=FileNotFoundError) as start:
            with self.assertRaisesRegex(check.CheckError, '沙箱'):
                check.execute([sys.executable, self.work / 'solve.py'], self.work)
        self.assertEqual(start.call_count, 1)
        self.assertEqual(start.call_args.args[0][0], '/usr/bin/sandbox-exec')

    def test_sandbox_denial_or_execution_error_never_passes_invalid_case(self):
        question = {'id': 'regression', 'cases': [], 'invalid': ['bad']}
        for kind in ('denied', 'missing-status', 'signal', 'exception'):
            with self.subTest(kind=kind):
                child = mock.Mock(pid=987654321, returncode=1 if kind == 'denied' else 0)
                child.communicate.return_value = (b'', b'Traceback (most recent call last):' if kind == 'exception' else b'denied')
                def start(argv, **kwargs):
                    if kind in ('signal', 'exception'):
                        self.completed(argv, -9 if kind == 'signal' else 1)
                    return child
                with mock.patch.object(check.subprocess, 'Popen', side_effect=start), mock.patch.object(check.os, 'killpg'):
                    with self.assertRaises(check.CheckError):
                        check.grade(self.work, question)

    def test_timeout_is_failure(self):
        child = mock.Mock(pid=987654321)
        child.communicate.side_effect = [subprocess.TimeoutExpired('sandbox', 30), (b'', b'')]
        with mock.patch.object(check.subprocess, 'Popen', return_value=child), mock.patch.object(check.os, 'killpg'):
            with self.assertRaisesRegex(check.CheckError, '超时'):
                check.execute([sys.executable, self.work / 'solve.py'], self.work)

    def test_success_without_output_is_failure(self):
        source = self.base / 'input.csv'
        source.write_bytes(b'input')
        with mock.patch.object(check, 'execute', return_value=(0, b'', b'')):
            with self.assertRaises(check.CheckError):
                check.run_csv(self.work, 'solve.py', source, self.base / 'output.csv')

    def test_link_in_artifacts_rejected_before_launch(self):
        (self.work / 'linked').symlink_to(self.base / 'synthetic-secret')
        with mock.patch.object(check.subprocess, 'Popen') as start:
            with self.assertRaises(check.CheckError):
                check.execute([sys.executable, self.work / 'solve.py'], self.work)
        start.assert_not_called()

    def test_output_link_is_not_read_by_host(self):
        child = mock.Mock(pid=987654321, returncode=0)
        child.communicate.return_value = (b'', b'')
        target = self.base / 'synthetic-secret'
        target.write_text('fixture')
        destination = self.base / 'output'
        def start(argv, **kwargs):
            self.completed(argv)
            Path(argv[-1]).symlink_to(target)
            return child
        with mock.patch.object(check.subprocess, 'Popen', side_effect=start), mock.patch.object(check.os, 'killpg'):
            with self.assertRaises(check.CheckError):
                check.execute([sys.executable, self.work / 'solve.py', destination], self.work,
                              outputs=(destination,))
        self.assertFalse(destination.exists())

    def test_bad_e2e_result_is_rejected_after_first_two_gates(self):
        a, b = self.base / 'a', self.base / 'b'
        a.mkdir(); b.mkdir()
        for directory, name, module in ((a, 'normalized', 'test_normalize'),
                                         (b, 'summary', 'test_summarize')):
            (directory / (name + '.sample.csv')).write_bytes((ROOT / 'demo' / (name + '.expected.csv')).read_bytes())
            (directory / (module + '.py')).write_text('# synthetic')
        def csv(directory, script, source, destination):
            if source.name == 'empty.csv':
                return b'name,category,amount\n' if directory == a else b'category,count,total\n'
            if source.name == 'e2e-normalized.csv':
                return b'category,count,total\n'
            return (ROOT / 'demo' / ('normalized.expected.csv' if directory == a else 'summary.expected.csv')).read_bytes()
        def execute(argv, *args, **kwargs):
            return (1, b'', b'line 2: invalid') if kwargs.get('inputs') else (0, b'', b'')
        with mock.patch.object(check, 'run_csv', side_effect=csv), mock.patch.object(check, 'execute', side_effect=execute):
            with self.assertRaisesRegex(check.CheckError, '端到端结果'):
                check.demo_gates(a, b, ROOT / 'demo', self.base / 'checks')

    def runner_status(self, body):
        a, b = self.base / 'a', self.base / 'b'
        a.mkdir(); b.mkdir()
        for directory, name in ((a, 'normalized'), (b, 'summary')):
            (directory / (name + '.sample.csv')).write_bytes((ROOT / 'demo' / (name + '.expected.csv')).read_bytes())
        (a / 'test_normalize.py').write_text(body)
        captured = []
        class Captured(Exception):
            pass
        def capture(argv, *args, **kwargs):
            captured.append(argv)
            raise Captured()
        def csv(directory, script, *args, **kwargs):
            return (ROOT / 'demo' / ('normalized.expected.csv' if script == 'normalize.py' else 'summary.expected.csv')).read_bytes()
        with mock.patch.object(check, 'run_csv', side_effect=csv), mock.patch.object(check, 'execute', side_effect=capture):
            with self.assertRaises(Captured):
                check.demo_gates(a, b, ROOT / 'demo', self.base / 'checks')
        return subprocess.run([str(x) for x in captured[0]], capture_output=True).returncode

    def test_all_skipped_rejected(self):
        self.assertNotEqual(self.runner_status('import unittest\n@unittest.skip("synthetic")\nclass T(unittest.TestCase):\n' + ''.join(' def test_%d(self): pass\n' % i for i in range(4))), 0)

    def test_zero_tests_rejected(self):
        self.assertNotEqual(self.runner_status('import unittest\n'), 0)

    def test_four_real_tests_pass(self):
        self.assertEqual(self.runner_status('import unittest\nclass T(unittest.TestCase):\n' + ''.join(' def test_%d(self): pass\n' % i for i in range(4))), 0)

    def test_skips_do_not_supply_missing_fourth_test(self):
        self.assertNotEqual(self.runner_status('import unittest\nclass T(unittest.TestCase):\n' + ''.join(' def test_%d(self): pass\n' % i for i in range(3)) + ' @unittest.skip("synthetic")\n def test_skip(self): pass\n'), 0)


if __name__ == '__main__':
    unittest.main()
