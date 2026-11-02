"""复现命令的离线测试：全部路径位于 resolve 后的临时目录，沙箱入口使用替身。"""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

BIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BIN))
from frclyo import repro


class ReproTests(unittest.TestCase):
    def setUp(self):
        # 不用宿主账本或配置；假数据根、假 HOME 与输入都在本测试的临时目录。
        temporary = tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve())
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / 'data'
        self.run = self.root / '交付/TEST-repro/r1'
        self.scripts = self.run / '交付/复现测试'
        self.scripts.mkdir(parents=True)
        (self.run / '输入/框架/bin').mkdir(parents=True)
        (self.run / '输入/框架/bin/sentinel').write_text('unchanged')
        self.tmp = self.base / 'tmp'
        self.tmp.mkdir()
        self.home = self.base / 'home'
        self.home.mkdir()
        patch = mock.patch.dict(os.environ, {'TMPDIR': str(self.tmp), 'HOME': str(self.home),
                                            'FRCLYO_HOME': str(self.root)})
        patch.start()
        self.addCleanup(patch.stop)
        self.launches = []
        self.popen = subprocess.Popen

    def script(self, name, body):
        # 仅此处生成的合成脚本会被替身执行，不执行交付中的未知代码。
        path = self.scripts / name
        path.write_text(body, encoding='utf-8')
        return path

    def fake_sandbox(self, command, **kwargs):
        # 检查生产入口确实请求沙箱，再用普通 Python 执行测试自己写的短脚本。
        if command[0] == '/bin/ps':
            return self.popen(command, **kwargs)
        self.assertEqual(command[:2], [repro.SANDBOX, '-p'])
        self.assertIn('(deny network*)', command[2])
        self.assertIn('(deny file-write*', command[2])
        self.assertEqual(command[3:5], [sys.executable, '-B'])
        self.launches.append((command, kwargs))
        return self.popen(command[3:], **kwargs)

    def invoke(self, args=None, launch=None):
        # 不运行真实 sandbox-exec；平台与启动器均在此明确替换。
        out, err = io.StringIO(), io.StringIO()
        original = Path.is_file
        def is_file(path):
            return True if str(path) == repro.SANDBOX else original(path)
        with mock.patch.object(repro.sys, 'platform', 'darwin'), \
                mock.patch.object(Path, 'is_file', is_file), \
                mock.patch.object(repro.subprocess, 'Popen', side_effect=launch or self.fake_sandbox), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = repro.main(args or ['TEST-repro', 'r1'])
        return code, out.getvalue(), err.getvalue()

    def results(self):
        return sorted((self.root / '运行时/复现/TEST-repro-r1').glob('run-*/results.json'))

    def digest(self):
        # 对原轮次所有文件做字节指纹，确认测试只修改临时副本。
        return {str(p.relative_to(self.run)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.run.rglob('*') if p.is_file()}

    def test_standard_layout_environment_and_original_untouched(self):
        self.script('test_standard.py', '''import os
from pathlib import Path
work = Path.cwd()
assert Path(os.environ['TMPDIR']) == work
assert Path(__file__).parent == work / '交付/复现测试'
source = work / '输入/框架'
for name in ('FRCLYO_REPRO_ROOT', 'REVIEW_SOURCE', 'REVIEW_REPO', 'AW_REVIEW_REPO',
             'REVIEW_FRAMEWORK', 'FRCLYO_REVIEW_ROOT', 'REPRO_FRAMEWORK'):
    assert Path(os.environ[name]) == source
assert 'FAKE_API_KEY' not in os.environ
assert 'PYTHONPATH' not in os.environ
(source / 'bin/sentinel').write_text('modified copy')
print('layout verified')
''')
        before = self.digest()
        with mock.patch.dict(os.environ, {'FAKE_API_KEY': 'test-only', 'PYTHONPATH': '/unused'}):
            code, out, err = self.invoke()
        self.assertEqual((code, err), (0, ''))
        self.assertIn('exit=0\t没复现', out)
        self.assertEqual(before, self.digest())
        self.assertEqual(list(self.tmp.iterdir()), [])
        result = self.results()[0]
        rows = json.loads(result.read_text())
        log = result.parent / rows[0]['attempts'][0]['log']
        self.assertEqual(log.read_text(), 'layout verified\n')

    def test_fail_error_pass_and_arbitrary_exit_are_distinct(self):
        scripts = {
            'test_assert.py': 'assert False, "expected behavior"',
            'test_fail.py': 'import unittest\nclass T(unittest.TestCase):\n def test_x(self): self.fail("regression")\nunittest.main()',
            'test_error.py': 'import unittest\nclass T(unittest.TestCase):\n def test_x(self): raise RuntimeError("fixture broken")\nunittest.main()',
            'test_pass.py': 'print("ok")',
            'test_exit.py': 'raise SystemExit(1)',
            'test_badexit.py': 'raise SystemExit(2)',
            'test_syntax.py': 'if invalid syntax',
        }
        for name, body in scripts.items():
            self.script(name, body)
        code, out, _ = self.invoke()
        self.assertEqual(code, 1)
        rows = {r['test']: r for r in json.loads(self.results()[0].read_text())}
        self.assertEqual(list(rows), sorted(scripts))
        for name in ('assert', 'fail', 'exit', 'error', 'syntax'):
            self.assertEqual(rows['test_' + name + '.py']['verdict'], '复现')
        for name in ('badexit',):
            self.assertEqual(rows['test_' + name + '.py']['verdict'], '测试本身出错')
        self.assertEqual(rows['test_pass.py']['verdict'], '没复现')
        self.assertIn('AssertionError: expected behavior', out)
        self.assertTrue(rows['test_fail.py']['first_failure'].startswith('FAIL:'))

    def test_missing_module_retries_only_from_delivery_and_keeps_both_logs(self):
        self.script('test_legacy.py', '''from pathlib import Path
if Path.cwd().name != '交付':
    print("ModuleNotFoundError: No module named 'frclyo'")
    raise SystemExit(2)
assert False, 'legacy reproduced'
''')
        code, out, _ = self.invoke()
        self.assertEqual(code, 0)
        self.assertIn('复现', out)
        row = json.loads(self.results()[0].read_text())[0]
        self.assertEqual(len(row['attempts']), 2)
        self.assertTrue(row['attempts'][1]['cwd'].endswith('/交付'))
        self.assertIn("No module named 'frclyo'", (self.results()[0].parent / row['attempts'][0]['log']).read_text())
        self.assertIn('legacy reproduced', (self.results()[0].parent / row['attempts'][1]['log']).read_text())

    def test_unrelated_import_error_is_not_retried(self):
        self.script('test_other.py', 'import definitely_missing_fixture_module')
        self.assertEqual(self.invoke()[0], 0)
        self.assertEqual(len(self.launches), 1)

    def test_missing_module_exit_one_is_preserved_without_retry(self):
        self.script('test_missing.py', 'raise ModuleNotFoundError("No module named \'frclyo\'")')
        self.assertEqual(self.invoke()[0], 0)
        row = json.loads(self.results()[0].read_text())[0]
        self.assertEqual(row['verdict'], '复现')
        self.assertEqual(len(row['attempts']), 1)

    def test_selected_name_and_repeated_logs(self):
        self.script('test_one.py', 'raise SystemExit(1)')
        self.script('test_two.py', 'raise RuntimeError("must not execute")')
        for flag, name in (('--测试', 'test_one'), ('--test', 'test_one.py')):
            code, out, _ = self.invoke(['TEST-repro', 'r1', flag, name])
            self.assertEqual(code, 0)
            self.assertNotIn('test_two', out)
        self.assertEqual(len(self.results()), 2)

    def test_no_tests_or_missing_selection_does_not_succeed(self):
        for args in (['TEST-repro', 'r1'], ['TEST-repro', 'r1', '--测试', 'test_absent']):
            code, _, err = self.invoke(args)
            self.assertEqual(code, 1)
            self.assertIn('没有找到匹配', err)
        self.assertEqual(self.launches, [])

    def test_missing_inputs_and_round_fail_without_launch(self):
        for relative in ('交付/TEST-repro/r1/输入/框架', '交付/TEST-repro/r1/输入', '交付/TEST-repro/r1'):
            path = self.root / relative
            if path.is_dir():
                import shutil
                shutil.rmtree(path)
            self.assertEqual(self.invoke()[0], 1)
        self.assertEqual(self.launches, [])

    def test_rejects_symlink_and_fifo_without_following_them(self):
        target = self.base / 'outside'
        target.write_text('do not read')
        link = self.run / '输入/link'
        link.symlink_to(target)
        self.assertIn('符号链接或特殊文件', self.invoke()[2])
        link.unlink()
        os.mkfifo(link)
        self.assertIn('符号链接或特殊文件', self.invoke()[2])
        self.assertEqual(target.read_text(), 'do not read')
        self.assertEqual(self.launches, [])

    def test_rejects_symlinked_round_and_logs(self):
        self.script('test_ok.py', 'pass')
        moved = self.base / 'moved'
        self.run.rename(moved)
        self.run.symlink_to(moved, target_is_directory=True)
        self.assertIn('不能包含符号链接', self.invoke()[2])
        self.run.unlink()
        moved.rename(self.run)
        (self.root / '运行时').symlink_to(self.tmp, target_is_directory=True)
        self.assertIn('不能包含符号链接', self.invoke()[2])
        self.assertEqual(self.launches, [])

    def test_temp_inside_round_is_rejected_without_changes(self):
        self.script('test_ok.py', 'pass')
        before = self.digest()
        with mock.patch.dict(os.environ, {'TMPDIR': str(self.run)}):
            self.assertIn('不能位于原轮次', self.invoke()[2])
        self.assertEqual(before, self.digest())

    def test_start_failure_does_not_fall_back_to_unsandboxed_python(self):
        self.script('test_ok.py', 'pass')
        calls = []
        def denied(command, **kwargs):
            calls.append(command)
            raise PermissionError('synthetic denial')
        code, out, _ = self.invoke(launch=denied)
        self.assertEqual(code, 1)
        self.assertIn('测试本身出错', out)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], repro.SANDBOX)

    def test_temp_inside_home_does_not_weaken_home_write_denial(self):
        # 用户家目录不能因 TMPDIR 设置而变成写入例外，也不自动换位置重试。
        self.script('test_ok.py', 'pass')
        with mock.patch.dict(os.environ, {'TMPDIR': str(self.home)}):
            code, _, err = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn('需要家目录外的宿主临时目录', err)
        self.assertEqual(list(self.home.iterdir()), [])
        self.assertEqual(self.launches, [])

    def test_explicit_tmpdir_never_probes_fallback_directories(self):
        # 显式 TMPDIR 已给定时，不调用会尝试其他系统目录的默认选择器。
        self.script('test_ok.py', 'pass')
        with mock.patch.object(repro.tempfile, 'gettempdir', side_effect=AssertionError('fallback called')):
            self.assertEqual(self.invoke()[0], 0)
            with mock.patch.dict(os.environ, {'TMPDIR': ''}):
                self.assertIn('TMPDIR 不能为空', self.invoke()[2])

    def test_timeout_is_error_and_next_test_runs(self):
        self.script('test_a_sleep.py', 'import time\nprint("started", flush=True)\ntime.sleep(10)')
        self.script('test_b_ok.py', 'print("next test")')
        start = time.monotonic()
        with mock.patch.object(repro, 'TIMEOUT', 0.4):
            code, out, _ = self.invoke()
        self.assertLess(time.monotonic() - start, 5)
        self.assertEqual(code, 1)
        self.assertIn('超时', out)
        rows = json.loads(self.results()[0].read_text())
        self.assertTrue(rows[0]['attempts'][0]['timed_out'])
        self.assertEqual(rows[1]['verdict'], '没复现')

    def test_retry_uses_remaining_total_budget(self):
        test = self.script('test_budget.py', 'pass')
        logs = self.base / 'logs'
        logs.mkdir()
        budgets = []
        def fake_attempt(test, cwd, env, profile, log, seconds):
            budgets.append(seconds)
            log.write_text("ModuleNotFoundError: No module named 'frclyo'\n")
            return {'cwd': str(cwd), 'log': log.name, 'exit_code': 2,
                    'timed_out': False, 'launch_error': ''}
        with mock.patch.object(repro, 'attempt', side_effect=fake_attempt), \
                mock.patch.object(repro.time, 'monotonic', side_effect=[0, 1, 180, 181, 200]):
            repro.run_test(test, self.run, {}, 'profile', logs)
        self.assertEqual(budgets, [299, 119])

    def test_keyboard_interrupt_reaps_process(self):
        process = mock.Mock(pid=12345)
        process.wait.side_effect = KeyboardInterrupt()
        log = self.base / 'interrupt.log'
        with mock.patch.object(repro.subprocess, 'Popen', return_value=process), \
                mock.patch.object(repro.execution, 'Descendants') as tracker, \
                mock.patch.object(repro.execution, 'reap_group', return_value=True) as reap:
            with self.assertRaises(KeyboardInterrupt):
                repro.attempt(Path('test_x.py'), self.base, {}, 'profile', log, 300)
        tracker.return_value.sample.assert_called_once()
        self.assertIs(process._frclyo_descendants, tracker.return_value)
        reap.assert_called_once_with(process)

    def test_classifier_mixed_errors_and_invalid_utf8(self):
        log = self.base / 'output.log'
        for output, expected in (
                (b'FAIL: first\nERROR: setup\nFAILED (failures=1, errors=1)\n', '复现'),
                (b'sandbox-exec: sandbox_apply: Operation not permitted\n', '环境受限：沙箱不能嵌套'),
                (b'\xff\nAssertionError: wrong\n', '复现'),
                (b'PermissionError: denied\n', '复现')):
            log.write_bytes(output)
            decision, _ = repro.verdict({'exit_code': 1, 'timed_out': False, 'launch_error': ''}, repro.inspect_output(log))
            self.assertEqual(decision, expected)

    def test_profile_quotes_paths_and_limits_writes(self):
        work = self.base / '目录 "quoted" \\ path'
        work.mkdir()
        profile = repro.sandbox_profile(work)
        self.assertIn(json.dumps(str(work), ensure_ascii=False), profile)
        self.assertIn('(deny network*)', profile)
        self.assertIn('(require-not (literal "/dev/null"))', profile)
        self.assertIn('(deny file-write* (subpath %s))' % json.dumps(str(self.home)), profile)

    def test_non_macos_refuses_without_launch(self):
        with mock.patch.object(repro.sys, 'platform', 'linux'), \
                mock.patch.object(repro.subprocess, 'Popen') as launch, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(repro.main(['TEST-repro', 'r1']), 1)
        launch.assert_not_called()

    def test_cli_aliases_help_and_bad_arguments(self):
        # 子进程只运行帮助/参数校验，不访问真实配置、不进入沙箱。
        for alias in ('复现', 'repro'):
            for args, expected in ((['--help'], 0), ([], 2), (['../escape', 'r1'], 2),
                                   (['TEST-repro', '../r1'], 2),
                                   (['TEST-repro', 'r1', '--测试', '../test_x.py'], 2),
                                   (['TEST-repro', 'r1', '--测试', 'test_*'], 2)):
                proc = subprocess.run([sys.executable, '-B', str(BIN / 'aw'), alias, *args],
                                      cwd=self.base, capture_output=True, text=True)
                self.assertEqual(proc.returncode, expected, proc.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
