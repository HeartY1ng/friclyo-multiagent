import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

"""B130/B132 离线契约：只执行合成单进程脚本，沙箱和进程表使用明确替身。"""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from frclyo import repro

TRACE = False


class Fixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=Path(os.environ['TMPDIR']).resolve())
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / 'data'
        self.run = self.root / '交付/TEST-repro/r1'
        self.scripts = self.run / '交付/复现测试'
        self.scripts.mkdir(parents=True)
        for part, value in (('输入', '1'), ('交付', '0')):
            tree = self.run / part / '框架'
            tree.mkdir(parents=True)
            (tree / 'status').write_text(value)
        self.tmp = self.base / 'tmp'
        self.tmp.mkdir()
        self.home = self.base / 'home'
        self.home.mkdir()
        patch = mock.patch.dict(os.environ, {'TMPDIR': str(self.tmp), 'HOME': str(self.home),
                                            'FRCLYO_HOME': str(self.root)})
        patch.start()
        self.addCleanup(patch.stop)
        self.launches = []
        self.processes = []
        self.popen = subprocess.Popen

    def script(self, name, body):
        (self.scripts / name).write_text(body, encoding='utf-8')

    def invoke(self, *args, platform='darwin'):
        out, err = io.StringIO(), io.StringIO()
        original = Path.is_file

        def is_file(path):
            return True if str(path) == repro.SANDBOX else original(path)

        def launch(command, **kwargs):
            if command[0] == '/bin/ps':
                return self.popen(command, **kwargs)
            self.launches.append(command)
            if command[0] == repro.SANDBOX:
                self.assertEqual(command[1], '-p')
                self.assertIn('(deny network*)', command[2])
                command = command[3:]
            process = self.popen(command, **kwargs)
            self.processes.append(process)
            return process

        def snapshot(command, **kwargs):
            # 本文件脚本不派生进程；替换宿主 ps 边界，不声称验证内核后代发现。
            self.assertEqual(command[:2], ['/bin/ps', '-axo'])
            rows = ['1 0 1 S fixture-init']
            for process in self.processes:
                if process.poll() is None:
                    rows.append('%d %d %d S fixture-start' %
                                (process.pid, os.getpid(), process.pid))
            if command[2] == 'pgid=,stat=':
                rows = ['1 S'] + ['%d S' % p.pid for p in self.processes if p.poll() is None]
            return subprocess.CompletedProcess(command, 0, '\n'.join(rows))

        with mock.patch.object(repro.sys, 'platform', platform), \
                mock.patch.object(Path, 'is_file', is_file), \
                mock.patch.object(repro.subprocess, 'Popen', side_effect=launch), \
                mock.patch.object(repro.execution.subprocess, 'run', side_effect=snapshot), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = repro.main(['TEST-repro', 'r1', *args])
            except SystemExit as exc:
                code = exc.code
        if TRACE:
            print('调用 aw repro TEST-repro r1 ' + ' '.join(args), flush=True)
            print('返回码：' + str(code) + '\n' + out.getvalue() + err.getvalue(), flush=True)
        # 参数不受支持是本卡目标故障；其他准备/权限失败必须报测试错误。
        if code == 1 and '复现未完成：' in err.getvalue():
            raise RuntimeError(err.getvalue())
        for result in self.results():
            for row in json.loads(result.read_text()):
                for attempt in row['attempts']:
                    if attempt.get('launch_error') or attempt.get('cleanup_error'):
                        raise RuntimeError(str(attempt))
        return code, out.getvalue(), err.getvalue()

    def results(self):
        return sorted((self.root / '运行时/复现/TEST-repro-r1').glob('run-*/results.json'))


class B130Tests(Fixture):
    def probe(self):
        self.script('test_tree.py', '''import os
from pathlib import Path
tree = Path(os.environ['FRCLYO_REPRO_ROOT'])
assert tree.is_relative_to(Path.cwd())
for name in ('REVIEW_SOURCE', 'REVIEW_REPO', 'AW_REVIEW_REPO', 'REVIEW_FRAMEWORK',
             'FRCLYO_REVIEW_ROOT', 'REPRO_FRAMEWORK'):
    assert Path(os.environ[name]) == tree
assert not list(tree.rglob('.git'))
assert not list(tree.rglob('__pycache__'))
value = int((tree / 'status').read_text())
(tree / 'status').write_text('copied only')
raise SystemExit(value)
''')

    def test_default_input_delivery_and_path(self):
        self.probe()
        external = self.base / '开发 tree'
        external.mkdir()
        (external / 'status').write_text('0')
        for tree in (external, self.run / '输入/框架', self.run / '交付/框架'):
            for name in ('.git', '__pycache__'):
                (tree / name).mkdir()
                (tree / name / 'ignored').write_text('ignored')
        cases = [((), self.run / '输入/框架', '复现'),
                 (('--框架', '输入'), self.run / '输入/框架', '复现'),
                 (('--framework', '交付'), self.run / '交付/框架', '没复现'),
                 (('--框架', str(external)), external, '没复现')]
        for args, source, expected in cases:
            with self.subTest(args=args):
                code, out, err = self.invoke(*args)
                self.assertEqual(code, 0, out + err)
                self.assertIn('框架：', out)
                self.assertIn(str(source), out)
                self.assertIn('\t' + expected + '\t', out)
                if expected == '没复现':
                    self.assertIn('修复版应全部没复现', out)
                    self.assertIn('仍复现 0', out)
                self.assertIn((source / 'status').read_text(), ('0', '1'))

    def test_delivery_exit_one_is_still_reproduced(self):
        self.script('test_remaining.py', 'raise SystemExit(1)')
        code, out, err = self.invoke('--框架', '交付')
        self.assertEqual(code, 0, out + err)
        self.assertIn('exit=1\t仍复现\t', out)
        self.assertIn('仍复现 1', out)
        self.assertIn('修复版应全部没复现', out)


class B132Tests(Fixture):
    def test_nested_failure_classified_without_retry(self):
        self.script('test_nested.py', '''print('sandbox-exec: sandbox_apply: Operation not permitted')
print("ModuleNotFoundError: No module named 'frclyo'")
raise SystemExit(1)
''')
        code, out, err = self.invoke()
        self.assertEqual(code, 1, out + err)
        self.assertIn('环境受限：沙箱不能嵌套', out)
        self.assertIn('--不套沙箱', out)
        self.assertIn('--no-sandbox', out)
        self.assertEqual(len(self.launches), 1)

    def test_no_sandbox_both_aliases_keep_copy_logs_and_cleanup(self):
        self.script('test_direct.py', '''from pathlib import Path
assert Path(__file__).is_relative_to(Path.cwd())
(Path.cwd() / '输入/框架/status').write_text('copy')
print('direct test completed')
''')
        for flag in ('--不套沙箱', '--no-sandbox'):
            code, out, err = self.invoke(flag, platform='linux')
            self.assertEqual(code, 0, out + err)
            self.assertIn('测试代码将直接在本机运行', out)
            self.assertIn('exit=0\t没复现', out)
            self.assertEqual(self.launches[-1][:2], [sys.executable, '-B'])
            self.assertEqual(list(self.tmp.iterdir()), [])
            self.assertEqual((self.run / '输入/框架/status').read_text(), '1')
        for result in self.results():
            row = json.loads(result.read_text())[0]
            self.assertIn('direct test completed',
                          (result.parent / row['attempts'][0]['log']).read_text())

    def test_nested_signature_only_overrides_failed_exits(self):
        log = self.base / 'diagnostic.log'
        for text, nested in [('sandbox_apply: Operation not permitted\n', True),
                             ('sandbox-exec: sandbox_apply: Operation not permitted\n', True),
                             ('AssertionError: 71 != 0 : sandbox-exec: sandbox_apply: Operation not permitted\n', True),
                             ('PermissionError: denied\n', False),
                             ('example sandbox_apply: Operation not permitted\n', False)]:
            log.write_text(text)
            for code in (0, 1, 2):
                expected = ('环境受限：沙箱不能嵌套' if nested and code != 0 else
                            {0: '没复现', 1: '复现', 2: '测试本身出错'}[code])
                with self.subTest(text=text, code=code):
                    result = dict(exit_code=code, timed_out=False, launch_error='')
                    self.assertEqual(repro.verdict(result, repro.inspect_output(log))[0], expected)


class SelectionBoundaryTests(Fixture):
    def test_missing_selected_tree_never_falls_back_to_input(self):
        self.script('test_ok.py', 'pass')
        for source in ('交付', str(self.base / 'absent')):
            if source == '交付':
                import shutil
                shutil.rmtree(self.run / '交付/框架')
            with self.subTest(source=source), self.assertRaisesRegex(RuntimeError, '所选框架必须是普通目录'):
                self.invoke('--框架', source)
        self.assertEqual(self.launches, [])

    def test_external_copy_rejects_links_and_special_files(self):
        self.script('test_ok.py', 'pass')
        external = self.base / 'external'
        external.mkdir()
        node = external / 'node'
        node.symlink_to(self.run / '输入/框架/status')
        with self.assertRaisesRegex(RuntimeError, '符号链接或特殊文件'):
            self.invoke('--框架', str(external))
        node.unlink()
        os.mkfifo(node)
        with self.assertRaisesRegex(RuntimeError, '符号链接或特殊文件'):
            self.invoke('--framework', str(external))
        self.assertEqual(self.launches, [])

    def test_run_metadata_records_selected_tree_and_actual_mode(self):
        self.script('test_ok.py', 'pass')
        for extra, sandbox in (((), True), (('--no-sandbox',), False)):
            before = set(self.results())
            code, out, err = self.invoke('--框架', '交付', *extra)
            self.assertEqual(code, 0, out + err)
            result, = set(self.results()) - before
            meta = json.loads((result.parent / 'run.json').read_text())
            self.assertEqual(meta['framework_source'], str(self.run / '交付/框架'))
            self.assertEqual(meta['sandbox'], sandbox)
            self.assertEqual(meta['expectation'], '修复版应全部没复现')
            self.assertEqual((result.parent / 'sandbox.sb').exists(), sandbox)

    def test_no_sandbox_keeps_retry_budget_and_later_tests_after_timeout(self):
        self.script('test_a_retry.py', '''from pathlib import Path
if Path.cwd().name != '交付':
    print("ModuleNotFoundError: No module named 'frclyo'")
    raise SystemExit(2)
print('retried')
''')
        self.script('test_b_sleep.py', 'import time\ntime.sleep(10)')
        self.script('test_c_ok.py', 'print("next")')
        with mock.patch.object(repro, 'TIMEOUT', 0.4):
            code, out, err = self.invoke('--不套沙箱')
        self.assertEqual(code, 1, out + err)
        rows = json.loads(self.results()[0].read_text())
        self.assertEqual(len(rows[0]['attempts']), 2)
        self.assertEqual(rows[0]['verdict'], '没复现')
        self.assertTrue(rows[1]['attempts'][0]['timed_out'])
        self.assertEqual(rows[1]['verdict'], '测试本身出错')
        self.assertEqual(rows[2]['verdict'], '没复现')
        self.assertTrue(all(command[0] == sys.executable for command in self.launches))
        self.assertTrue(all(process.poll() is not None for process in self.processes))


if __name__ == '__main__':
    unittest.main(verbosity=2)
