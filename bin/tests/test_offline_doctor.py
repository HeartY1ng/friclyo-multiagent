"""离线诊断自身回归：不启动真实 CLI、不监听端口、不读宿主配置。"""
import io
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import types
import tempfile
import shlex
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'tests/host'))
from frclyo import isolation
import offline_opencode as offline
import sandbox_doctor as doctor
import production_opencode as production
import codex_tmp_smoke


class OfflineDoctorTests(unittest.TestCase):
    def setUp(self):
        self.home = '/Users/fixture'
        self.root = self.home + '/AgentWork'
        self.run = self.root + '/交付/T/r1'
        self.runtime = self.root + '/runtime'
        self.control = self.root + '/control'
        patch = mock.patch.dict(os.environ, HOME=self.home, CODEX_HOME=self.home + '/.codex')
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(isolation, 'runtime_read_paths', return_value=['/usr/bin/python3'])
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(offline, 'runtime_read_paths', return_value=['/usr/bin/python3'])
        patch.start()
        self.addCleanup(patch.stop)
        self.profile = isolation.seatbelt(self.home, self.root, self.run, self.runtime,
                                         self.control, 12345, [], [self.root])

    def test_matrix_only_changes_one_content_rule_and_adds_guard(self):
        original = self.profile.splitlines()
        for category in ('home', 'tmp', 'folders'):
            text = offline.diagnostic_profile(self.profile, category, self.home, self.root,
                                               ['/private/tmp/fixture'])
            lines = text.splitlines()
            self.assertEqual(lines[:2], original[:2])
            self.assertEqual(lines[3:-1], original[3:])  # secrets/control/write/network unchanged
            self.assertIn('(subpath "/Users")', lines[-1])
            self.assertIn('(subpath "/private/tmp")', lines[-1])
            self.assertIn('(subpath "/private/var/folders")', lines[-1])
            self.assertIn('(require-not (subpath "/private/tmp/fixture"))', lines[-1])
            self.assertNotIn('(require-not (subpath "/Users"))', text)
            self.assertEqual(len(list(offline._forms(text))), len(lines))
            if category != 'home':
                target = '/private/tmp' if category == 'tmp' else '/private/var/folders'
                kept = '/private/var/folders' if category == 'tmp' else '/private/tmp'
                self.assertNotIn('(require-all (subpath "' + target + '")', lines[2])
                self.assertIn('(require-all (subpath "' + kept + '")', lines[2])
            else:
                self.assertIn('(require-not (subpath "' + self.home + '"))', lines[2])
                root_form = next(form for form in offline._forms(lines[2][len('(deny file-read-data file-read-xattr '):-1])
                                 if form.startswith('(require-all (subpath "' + self.root + '")'))
                self.assertNotIn('(require-not (subpath "' + self.home + '"))', root_form)
        with self.assertRaises(ValueError):
            offline.diagnostic_profile(self.profile, 'all', self.home, self.root, [])

    def test_profile_parser_ignores_parentheses_in_quoted_paths(self):
        text = '(subpath "a(b)\\\"c") (literal "d")'
        self.assertEqual(list(offline._forms(text)), ['(subpath "a(b)\\\"c")', '(literal "d")'])
        with self.assertRaises(ValueError):
            list(offline._forms('(broken'))

    def test_success_needs_text_event_not_debug_log_or_echoed_prompt(self):
        for text in ('', 'error: Unexpected', offline.TOKEN, json.dumps({'type': 'error', 'message': offline.TOKEN})):
            self.assertFalse(offline.response_seen(text))
        self.assertTrue(offline.response_seen(json.dumps({'type': 'text', 'part': {'text': offline.TOKEN}})))

    def test_production_fixture_uses_identical_entry_and_private_fake_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for layout in ('zh', 'ascii'):
                for variant in ('baseline', 'working-directory', 'home'):
                    argv, env, caller, run, control, manifest = production.build_fixture(
                        root / (layout + '-' + variant), sys.executable, layout, variant)
                    repo = Path(argv[1]).parents[1]
                    self.assertEqual(Path(argv[1]).read_bytes(), (production.REPO / 'bin/opencode-worker.sh').read_bytes())
                    self.assertEqual((repo / 'bin/allowlist-proxy.py').read_bytes(),
                                     (production.REPO / 'bin/allowlist-proxy.py').read_bytes())
                    self.assertNotIn('--pure', Path(argv[1]).read_text())
                    self.assertNotIn(offline.TOKEN, Path(argv[4]).read_text())
                    self.assertNotIn(production.FAKE_KEY, repr(env))
                    self.assertTrue(Path(env['FRCLYO_CONFIG']).is_relative_to(root))
                    self.assertFalse(caller.is_relative_to(run))
                    code = (repo / 'bin/frclyo/execution.py').read_text()
                    self.assertIn(production.CWD_LINE if variant == 'working-directory' else 'cwd=None', code)
                    if layout == 'ascii':
                        self.assertEqual(run.relative_to(Path(env['FRCLYO_HOME'])).parts, ('delivery', 'DOCTOR', 'r1'))
                        self.assertIn('/runtime/control/', str(control))
                    else:
                        self.assertIn('/交付/', str(run))
                        self.assertIn('/运行时/控制/', str(control))
                    helper = subprocess.run([sys.executable, '-I',
                        str(repo / 'tests/host/production_opencode.py'), '--help'],
                        capture_output=True, text=True, timeout=5)
                    self.assertEqual(helper.returncode, 0, helper.stderr)
                    # 真正执行启动准备的路径计算，不能仅凭 --help 判定夹具可用。
                    prepare = subprocess.run([sys.executable, '-I', '-c',
                        'import sys; sys.path.insert(0, sys.argv[1]); '
                        'from frclyo.worker import prepare; '
                        'from frclyo.isolation import validate_run; '
                        'from frclyo.execution import capture_directory; '
                        'settings = prepare("deepseek/deepseek-flash"); '
                        'validate_run(settings["root"], sys.argv[2]); '
                        'assert str(capture_directory("opencode", sys.argv[2])) == sys.argv[3]',
                        str(repo / 'bin'), str(run), str(control)],
                        env=env, cwd=caller, capture_output=True, text=True, timeout=10)
                    self.assertEqual(prepare.returncode, 0, prepare.stderr)
                    self.assertEqual(env['OPENCODE_WORKER_DEBUG'], '1')

    def test_matrix_never_combines_different_language_groups_into_success(self):
        for baseline, fixed in (((False, False), (True, True)), ((True, True), (True, True)),
                                ((False, True), (True, False)), ((False, False), (False, False))):
            values = iter(baseline + fixed)
            def row(*args):
                ok = next(values)
                return dict(ok=ok, launcher_exit=0 if ok else 1, command_exit=0 if ok else 1,
                            requests=int(ok), response=ok, old_failure_reproduced=not ok,
                            tails={'stderr': []})
            with mock.patch.object(doctor.shutil, 'which', return_value='/fake/opencode'), \
                 mock.patch.object(production, 'run_case', side_effect=row) as launch, \
                 mock.patch.object(Path, 'write_text'), mock.patch('sys.stdout', new_callable=io.StringIO):
                result = doctor.opencode_matrix(Path('/fixture'))
            self.assertEqual(launch.call_count, 4)
            self.assertEqual(result['ok'], all(fixed))
            self.assertEqual(result['reproduced'], not all(baseline))
            self.assertEqual(result['cwd_evidence'], {'zh': not baseline[0] and fixed[0],
                                                     'ascii': not baseline[1] and fixed[1]})

    def test_other_failure_never_claims_historical_error_reproduced(self):
        row = dict(ok=False, launcher_exit=2, command_exit=None, requests=0, response=False,
                   old_failure_reproduced=False, tails={'stderr': ['fixture setup failed']})
        with mock.patch.object(doctor.shutil, 'which', return_value='/fake/opencode'), \
             mock.patch.object(production, 'run_case', return_value=row), \
             mock.patch.object(Path, 'write_text'), mock.patch('sys.stdout', new_callable=io.StringIO):
            result = doctor.opencode_matrix(Path('/fixture'))
        self.assertFalse(result['reproduced'])
        self.assertFalse(any(result['cwd_evidence'].values()))
        self.assertFalse(result['ok'])

    def test_codex_smoke_generates_valid_swift_commands_for_separate_modes(self):
        # 只验证生成的任务代码；不调用 Codex 或编译器。
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for mode in ('tmp', 'run', 'root', None):
                with mock.patch.object(codex_tmp_smoke.tempfile, 'mkdtemp', return_value=str(root)), \
                     mock.patch.object(sys, 'argv', ['smoke'] + (['--swift-cwd', mode] if mode else [])), \
                     mock.patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 1)), \
                     mock.patch('sys.stdout', new_callable=io.StringIO):
                    self.assertEqual(codex_tmp_smoke.main(), 1)
                prompt = (root / 'task.txt').read_text()
                code = shlex.split(prompt.rpartition('python3 -I -c ')[2])[0]
                ast.parse(code)
                self.assertIn("checks['swift_compile']", code)
                self.assertIn("cwd='/'" if mode == 'root' else 'cwd=str(work)', code)
                self.assertIn('work = run' if mode in ('run', 'root') else 'work = pathlib.Path(expected)', code)
                self.assertIn("checks['copy_back']", code)

    def handler(self, payload, path='/deepseek/chat/completions'):
        body = json.dumps(payload).encode()
        handler = object.__new__(offline.FakeCompletion)
        handler.path = path
        handler.connection = mock.Mock()
        handler.headers = {'Content-Length': str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler.wfile = io.BytesIO()
        handler.server = types.SimpleNamespace(requests=0)
        for method in ('send_response', 'send_header', 'end_headers', 'send_error'):
            setattr(handler, method, mock.Mock())
        return handler

    def test_fake_response_handles_both_streaming_and_nonstreaming_without_forwarding(self):
        for stream in (True, False):
            handler = self.handler(dict(model='deepseek-flash', stream=stream))
            handler.do_POST()
            handler.send_response.assert_called_once_with(200)
            self.assertEqual(handler.server.requests, 1)
            body = handler.wfile.getvalue().decode()
            self.assertIn(offline.TOKEN, body)
            if stream:
                self.assertTrue(body.endswith('data: [DONE]\n\n'))
            else:
                self.assertEqual(json.loads(body)['choices'][0]['finish_reason'], 'stop')
        handler = self.handler({}, '/external')
        handler.do_POST()
        handler.send_error.assert_called_once_with(404)
        self.assertEqual(handler.server.requests, 0)

    def test_fake_upstream_closes_listener_on_exit_exception_and_requires_fake_pipe(self):
        for fail in (False, True):
            server = mock.Mock()
            server.server_address = ('127.0.0.1', 12345)
            if fail:
                server.handle_request.side_effect = RuntimeError('fixture failure')
            stdin = types.SimpleNamespace(buffer=io.BytesIO((production.FAKE_KEY + '\n').encode()))
            argv = ['upstream', '--provider', 'deepseek', '--log', '/fixture/control/log', '--parent-pid', '55']
            with mock.patch.object(production, 'ThreadingHTTPServer', return_value=server), \
                 mock.patch.object(production.os, 'getppid', side_effect=[55, 0]), \
                 mock.patch.object(production.signal, 'signal'), \
                 mock.patch.object(sys, 'argv', argv), mock.patch.object(sys, 'stdin', stdin), \
                 mock.patch.object(Path, 'write_text'), mock.patch.object(Path, 'write_bytes'), \
                 mock.patch('sys.stdout', new_callable=io.StringIO):
                try:
                    self.assertEqual(production.fake_upstream_main(), 0)
                except RuntimeError:
                    self.assertTrue(fail)
            server.server_close.assert_called_once_with()
        with mock.patch.object(production, 'ThreadingHTTPServer') as create, \
             mock.patch.object(sys, 'argv', argv), \
             mock.patch.object(sys, 'stdin', types.SimpleNamespace(buffer=io.BytesIO(b'wrong\n'))):
            self.assertEqual(production.fake_upstream_main(), 2)
        create.assert_not_called()

    def test_timeout_and_success_only_signal_verified_child_group(self):
        for wait in ([0, 0, 0], [subprocess.TimeoutExpired('fake', 90), 0, 0]):
            proc = mock.Mock(pid=12345)
            proc.wait.side_effect = wait
            log = mock.Mock()
            log.open.return_value.__enter__ = mock.Mock(return_value=io.BytesIO())
            log.open.return_value.__exit__ = mock.Mock(return_value=False)
            with mock.patch.object(offline.subprocess, 'Popen', return_value=proc) as spawn, \
                 mock.patch.object(offline.os, 'getpgid', return_value=12345), \
                 mock.patch.object(offline.os, 'getsid', return_value=12345), \
                 mock.patch.object(offline.os, 'killpg') as kill:
                rc = offline.execute(['fake'], {}, '/fixture', log, 90)
            self.assertIn(rc, (0, 125))
            self.assertTrue(spawn.call_args.kwargs['start_new_session'])
            self.assertEqual({call.args[0] for call in kill.call_args_list}, {12345})

    def test_mismatched_group_never_signals_callers_group(self):
        proc = mock.Mock(pid=12345)
        proc.poll.return_value = None
        log = mock.Mock()
        log.open.return_value.__enter__ = mock.Mock(return_value=io.BytesIO())
        log.open.return_value.__exit__ = mock.Mock(return_value=False)
        with mock.patch.object(offline.subprocess, 'Popen', return_value=proc), \
             mock.patch.object(offline.os, 'getpgid', return_value=os.getpgrp()), \
             mock.patch.object(offline.os, 'getsid', return_value=12345), \
             mock.patch.object(offline.os, 'killpg') as kill:
            with self.assertRaises(RuntimeError):
                offline.execute(['fake'], {}, '/fixture', log, 90)
        kill.assert_not_called()
        proc.terminate.assert_called_once_with()

    def test_real_bash_caller_continues_after_success_failure_timeout_and_exception(self):
        # 外层测试本身独立 session：回归失败也不能把 unittest/启动它的 shell 杀掉。
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            driver = root / 'driver.py'
            driver.write_text('import os, sys\nfrom pathlib import Path\n'
                + 'sys.path.insert(0, ' + repr(str(production.REPO / 'bin')) + ')\n'
                + 'sys.path.insert(0, ' + repr(str(production.REPO / 'tests/host')) + ')\n'
                + 'import offline_opencode as offline\n'
                + 'mode = sys.argv[1]\n'
                + 'command = [sys.executable, "-I", "-c", "import time; time.sleep(30)" if mode == "timeout" else "import sys; sys.exit(7)" if mode == "failure" else "pass"]\n'
                + 'if mode == "exception": command = ["/nonexistent/frclyo-diagnostic"]\n'
                + 'rc = offline.execute(command, dict(os.environ), Path(__file__).parent, Path(__file__).with_suffix(".log"), .1 if mode == "timeout" else 5)\n'
                + 'print("DIAG_RC=" + str(rc), flush=True)\n'
                + 'sys.exit(rc)\n')
            for mode, expected in (('success', 0), ('failure', 7), ('timeout', 125), ('exception', 125)):
                shell = (shlex.quote(sys.executable) + ' -B ' + shlex.quote(str(driver)) + ' ' + mode
                         + '\nprintf "CALLER_CONTINUED\\n"\n')
                proc = subprocess.run(['/bin/bash', '--noprofile', '--norc', '-c', shell],
                    capture_output=True, text=True, timeout=20, start_new_session=True)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn('DIAG_RC=' + str(expected), proc.stdout)
                self.assertIn('CALLER_CONTINUED', proc.stdout)
                if mode != 'exception':
                    line = next(line for line in driver.with_suffix('.log').read_text().splitlines()
                                if line.startswith('FRCLYO_DIAGNOSTIC_GROUP='))
                    group = json.loads(line.partition('=')[2])
                    self.assertEqual(group['child_pid'], group['child_pgid'])
                    self.assertEqual(group['child_pid'], group['child_sid'])
                    self.assertNotEqual(group['child_pid'], group['caller_pgid'])

    def test_swift_check_requires_environment_compile_and_executable_output(self):
        manifest = {'targets': {'根外用户文档': '/fake/sentinel'}, 'temporary': '/fixture/tmp'}
        cases = doctor.cases(Path('/fixture/run'), manifest)
        self.assertIn('完整隔离探针', cases)
        self.assertIn(doctor.SWIFT_CASE, cases['Swift编译运行和ASCII临时目录'])
        calls = []
        def launch(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, 'ok\n' if len(calls) == 2 else '', '')
        values = dict(TMPDIR='/fixture/tmp', TMP='/fixture/tmp', TEMP='/fixture/tmp',
                      TMPPREFIX='/fixture/tmp/zsh',
                      CLANG_MODULE_CACHE_PATH='/fixture/tmp/clang-module-cache',
                      SWIFT_MODULECACHE_PATH='/fixture/tmp/swift-module-cache')
        with mock.patch.dict(os.environ, values, clear=True), \
             mock.patch.object(sys, 'argv', ['probe', '/fixture/run', '/fixture/tmp']), \
             mock.patch.object(Path, 'write_text'), mock.patch.object(Path, 'resolve', lambda p: p), \
             mock.patch.object(subprocess, 'run', side_effect=launch), \
             mock.patch('sys.stdout', new_callable=io.StringIO) as output:
            with self.assertRaises(SystemExit) as result:
                exec(doctor.SWIFT_CASE, {})
        self.assertEqual(result.exception.code, 0)
        self.assertEqual(calls[0][0], 'swiftc')
        self.assertEqual(len(calls), 2)
        self.assertTrue(json.loads(output.getvalue().split('=', 1)[1])['ok'])

    def test_swift_failure_or_wrong_output_never_counts_as_success(self):
        for compiled, executed, output in ((133, 0, 'ok\n'), (0, 1, 'ok\n'), (0, 0, 'wrong\n')):
            responses = [subprocess.CompletedProcess([], compiled, '', ''),
                         subprocess.CompletedProcess([], executed, output, '')]
            values = dict(TMPDIR='/fixture/tmp', TMP='/fixture/tmp', TEMP='/fixture/tmp',
                          TMPPREFIX='/fixture/tmp/zsh',
                          CLANG_MODULE_CACHE_PATH='/fixture/tmp/clang-module-cache',
                          SWIFT_MODULECACHE_PATH='/fixture/tmp/swift-module-cache')
            with mock.patch.dict(os.environ, values, clear=True), \
                 mock.patch.object(sys, 'argv', ['probe', '/fixture/run', '/fixture/tmp']), \
                 mock.patch.object(Path, 'write_text'), mock.patch.object(Path, 'resolve', lambda p: p), \
                 mock.patch.object(subprocess, 'run', side_effect=responses) as launch, \
                 mock.patch('sys.stdout', new_callable=io.StringIO) as captured:
                with self.assertRaises(SystemExit) as result:
                    exec(doctor.SWIFT_CASE, {})
            self.assertEqual(result.exception.code, 1)
            self.assertFalse(json.loads(captured.getvalue().split('=', 1)[1])['ok'])
            self.assertEqual(launch.call_count, 1 if compiled else 2)


if __name__ == '__main__':
    unittest.main()
