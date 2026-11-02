"""仅替身命令；不运行宿主内核、git、编译器或真实模型。"""
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
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('verify_all', ROOT / 'tests/host/verify_all.py')
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)


class VerifyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='.verify-test-', dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def run_code(self, code, timeout=3, kind='exit'):
        return verify.run_check('替身', [sys.executable, '-B', '-c', code], kind,
                                self.base / ('case-%d' % len(list(self.base.iterdir()))), timeout)

    def summary(self):
        return {'checks': {**{name: False for name in verify.RECITATIONS},
                           **{worker + ' ' + label: True for worker in ('codex', 'opencode')
                              for label in ('本轮读写', '探针退出', '禁读 写入 HOME外哨兵')}},
                'probes': {worker: {'状态': '已运行', '退出码': 0} for worker in ('codex', 'opencode')}}

    def test_exit_and_useful_tail(self):
        row = self.run_code("print('ordinary output')")
        self.assertEqual(row['status'], '通过')
        row = self.run_code("print('bad thing'); print('证据目录：relative'); raise SystemExit(3)")
        self.assertEqual(row['status'], '失败')
        self.assertEqual(row['reason'], 'bad thing')
        self.assertEqual(row['returncode'], 3)

    def test_missing_binary_is_failure(self):
        row = verify.run_check('missing', [str(self.base / 'missing')], 'exit', self.base / 'missing-log', 1)
        self.assertEqual(row['status'], '失败')
        self.assertIn('FileNotFoundError', row['reason'])

    def test_isolation_nonzero_contract(self):
        data = self.summary()
        self.assertTrue(verify.isolation_pass(data, 1))
        for rc in (0, 2, -15):
            self.assertFalse(verify.isolation_pass(data, rc))
        for value in (False, None, 1, 'true'):
            data = self.summary()
            data['checks']['codex 禁读 写入 HOME外哨兵'] = value
            self.assertFalse(verify.isolation_pass(data, 1))
        for data in ({}, {'checks': {}}, [], None):
            self.assertFalse(verify.isolation_pass(data, 1))
        data = self.summary()
        del data['checks']['codex 本轮读写']
        self.assertFalse(verify.isolation_pass(data, 1))
        data = self.summary()
        data['probes']['opencode']['状态'] = '探针未运行'
        self.assertFalse(verify.isolation_pass(data, 1))
        data = self.summary()
        data['checks']['codex 真实守则复述'] = True
        self.assertFalse(verify.isolation_pass(data, 1))

    def test_structured_evidence_and_log_archive(self):
        evidence = self.base / 'fixture'
        evidence.mkdir()
        (evidence / 'summary.json').write_text(json.dumps(self.summary()))
        (evidence / 'inner.log').write_text('nested evidence')
        (evidence / 'auth.json').write_text('must not copy')
        code = 'print(%r); raise SystemExit(1)' % ('证据目录：' + str(evidence))
        row = self.run_code(code, kind='isolation')
        self.assertEqual(row['status'], '通过')
        archive = Path(row['log']).parent / 'evidence-00'
        self.assertEqual((archive / 'inner.log').read_text(), 'nested evidence')
        self.assertFalse((archive / 'auth.json').exists())
        (evidence / 'summary.json').write_text('not json')
        self.assertEqual(self.run_code(code, kind='isolation')['status'], '失败')

    def test_self_termination_cannot_kill_next_check(self):
        code = ('import os,signal; '
                'assert os.getpgrp()==os.getpid(); '
                'os.killpg(os.getpgrp(),signal.SIGTERM)')
        row = self.run_code(code)
        self.assertEqual(row['returncode'], -signal.SIGTERM)
        self.assertEqual(self.run_code("print('next survived')")['status'], '通过')

    def test_timeout_kills_descendant_even_if_term_ignored(self):
        marker = self.base / 'escaped'
        ready = self.base / 'ready'
        child = ('import signal,time,pathlib; signal.signal(signal.SIGTERM,signal.SIG_IGN); '
                 'pathlib.Path(%r).write_text("ready"); time.sleep(1.2); '
                 'pathlib.Path(%r).write_text("bad")') % (str(ready), str(marker))
        code = ('import subprocess,sys,time,pathlib; '
                'subprocess.Popen([sys.executable,"-c",%r]); '
                'time.sleep(20)') % child
        row = self.run_code(code, timeout=0.6)
        self.assertTrue(ready.exists())
        self.assertEqual(row['status'], '失败')
        self.assertIn('超时', row['reason'])
        time.sleep(0.8)
        self.assertFalse(marker.exists())
        self.assertEqual(self.run_code('pass')['status'], '通过')

    def test_main_one_screen_and_exit_status(self):
        jobs = [('ok', [sys.executable, '-c', "print('CHILD_NOISE')"], 'exit'),
                ('bad', [sys.executable, '-c', "print('reason'); exit(1)"], 'exit')]
        stdout = io.StringIO()
        output = self.base / 'output'
        with mock.patch.object(verify, 'plan', return_value=jobs), contextlib.redirect_stdout(stdout):
            rc = verify.main(['--output', str(output)])
        self.assertEqual(rc, 1)
        self.assertLessEqual(len(stdout.getvalue().splitlines()), 30)
        self.assertNotIn('CHILD_NOISE', stdout.getvalue())
        self.assertIn('bad | 失败', stdout.getvalue())
        self.assertEqual(len(json.loads((output / 'summary.json').read_text())), 4)
        with mock.patch.object(verify, 'plan', return_value=jobs[:1]), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(verify.main(['--output', str(self.base / 'passing')]), 0)

    def test_default_output_uses_data_root(self):
        data = self.base / 'data'
        with mock.patch.object(verify.config, 'root_dir', return_value=data), \
             mock.patch.object(verify, 'plan', return_value=[]), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(verify.main([]), 0)
        outputs = list((data / '运行时/验收').iterdir())
        self.assertEqual(len(outputs), 1)
        self.assertTrue((outputs[0] / 'summary.json').is_file())

    def fake_smoke(self, name, command, kind, directory, timeout):
        directory.mkdir()
        self.assertTrue(command[2].startswith('TEST-'))
        worker = 'opencode' if 'opencode-worker.sh' in command[1] else 'codex'
        expected = ('deepseek/deepseek-flash', 'nothink') if worker == 'opencode' else ('gpt-6-astra', 'low')
        self.assertEqual(tuple(command[5:7]), expected)
        self.assertTrue(Path(command[4]).is_file())
        run = self.base / '交付' / command[2] / 'r1'
        run.mkdir(parents=True)
        (run / 'model-sentinel.txt').write_bytes(b'MODEL_API_OK\n')
        (run / '回执.json').write_text(json.dumps({'状态': '执行完成'}))
        for filename in verify.CODEX_FILES:
            (run / filename).write_text('fixture')
        (run / 'swift-smoke').chmod(0o700)
        (run / 'tools.txt').write_text('\n'.join('EXIT ' + step + '=0' for step in verify.TOOL_STEPS))
        return dict(name=name, status='通过', seconds=0, reason='', returncode=0)

    def test_paid_runs_each_card_once_and_cleans_sentinels(self):
        output = self.base / 'paid'
        with mock.patch.object(verify.config, 'root_dir', return_value=self.base), \
             mock.patch.object(verify, 'plan', return_value=[]), \
             mock.patch.object(verify, 'run_check', side_effect=self.fake_smoke) as launch, \
             contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(verify.main(['--paid', '--output', str(output)]), 0)
        self.assertEqual(launch.call_count, 2)
        self.assertIn('会花钱', stdout.getvalue())
        rows = json.loads((output / 'summary.json').read_text())
        for row in rows:
            self.assertTrue(row['evidence_verified'])
            self.assertFalse((Path(row['run']) / 'model-sentinel.txt').exists())
        self.assertFalse((self.base / '账本.jsonl').exists())

    def test_smoke_rejects_bad_evidence_and_always_cleans(self):
        mutations = [lambda run: (run / 'model-sentinel.txt').write_text('bad'),
                     lambda run: (run / '回执.json').write_text('{broken'),
                     lambda run: (run / '回执.json').write_text('{"状态":"失败"}'),
                     lambda run: (run / 'calc.py').unlink()]
        for step in verify.TOOL_STEPS:
            mutations.append(lambda run, step=step: (run / 'tools.txt').write_text(
                (run / 'tools.txt').read_text().replace('EXIT ' + step + '=0', 'EXIT ' + step + '=1')))
        mutations += [lambda run: (run / 'tools.txt').write_text(''),
                      lambda run: (run / 'swift-smoke').chmod(0o600)]
        for index, mutate in enumerate(mutations):
            def launch(*args):
                row = self.fake_smoke(*args)
                mutate(self.base / '交付' / args[1][2] / 'r1')
                return row
            with mock.patch.object(verify.config, 'root_dir', return_value=self.base), \
                 mock.patch.object(verify, 'run_check', side_effect=launch):
                row = verify.run_smoke(verify.SMOKES[1], self.base / ('bad-%d' % index), 3)
            self.assertEqual(row['status'], '失败')
            self.assertFalse((Path(row['run']) / 'model-sentinel.txt').exists())

    def test_failed_or_interrupted_launch_cleans_sentinel(self):
        for index, interrupted in enumerate((False, True)):
            def launch(*args):
                row = self.fake_smoke(*args)
                row.update(status='失败', interrupted=interrupted)
                return row
            with mock.patch.object(verify.config, 'root_dir', return_value=self.base), \
                 mock.patch.object(verify, 'run_check', side_effect=launch):
                row = verify.run_smoke(verify.SMOKES[0], self.base / ('fail-%d' % index), 3)
            self.assertEqual(row['status'], '失败')
            self.assertFalse((Path(row['run']) / 'model-sentinel.txt').exists())

    def test_existing_output_never_overwritten(self):
        marker = self.base / 'preserved'
        marker.write_text('keep')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(verify.main(['--output', str(self.base)]), 1)
        self.assertEqual(marker.read_text(), 'keep')

    def test_default_plan_is_unpaid_without_duplicate_wrappers(self):
        commands = [' '.join(argv) for _, argv, _ in verify.plan()]
        self.assertEqual(len(commands), 8)
        self.assertTrue(any('isolation_check.sh --offline-only' in cmd for cmd in commands))
        self.assertFalse(any('--live' in cmd or 'codex_tmp_smoke.py' in cmd or 'network_check.sh' in cmd
                             for cmd in commands))

    def test_aw_aliases_only_forward(self):
        import importlib.machinery
        loader = importlib.machinery.SourceFileLoader('verify_aw_test', str(ROOT / 'bin/aw'))
        module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(module)
        for name in ('verify', '验收'):
            with mock.patch.object(module.os, 'execv', side_effect=RuntimeError('exec')) as call:
                with self.assertRaisesRegex(RuntimeError, 'exec'):
                    module.main([name, '--timeout', '12'])
            self.assertEqual(call.call_args.args[1][-3:],
                             [str(ROOT / 'tests/host/verify_all.py'), '--timeout', '12'])


if __name__ == '__main__':
    unittest.main()
