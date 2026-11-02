#!/usr/bin/env python3
"""离线回归：不登录、不调用模型，也不证明宿主内核隔离。"""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import isolation_check as check
import isolation_probe
import sandbox_doctor as doctor


class IsolationCheckTests(unittest.TestCase):
    def setUp(self):
        binaries = mock.patch('frclyo.isolation.runtime_read_paths', return_value=['/usr/bin/python3'])
        binaries.start()
        self.addCleanup(binaries.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.log = self.base / 'kernel.log'
        self.first = next(line for line in (check.REPO / '工人守则.md').read_text().splitlines()
                          if line.startswith('1. '))

    def test_command_uses_shared_permissions_and_sandbox_profile(self):
        home, root, run, runtime, control = check.build_area(self.base, 'codex')
        sensitive = [str(home / '.codex'), str(root / '配置/config.json')]
        command = ['python3', '-I', str(run / 'probe.py')]
        argv = check.codex_probe_argv('codex', root, run, sensitive, runtime, command)
        self.assertEqual(argv[:4], ['codex', 'sandbox', '-C', str(run)])
        configs = [argv[i + 1] for i, arg in enumerate(argv) if arg == '-c']
        fixture_secrets = [str(home / name) for name in check.SECRET_NAMES]
        self.assertIn(check.codex_permission(str(root), str(run), sensitive + fixture_secrets, str(runtime), temporary_home=home), configs)
        self.assertEqual(argv[argv.index('-P') + 1], 'frclyo_worker')
        self.assertIn('--include-managed-config', argv)
        self.assertEqual(argv[argv.index('--') + 1:], command)
        for flag in ('--ignore-user-config', '--ignore-rules', 'macos', '-s', '--full-auto'):
            self.assertNotIn(flag, argv)
        worker = (check.REPO / 'bin/codex-worker.sh').read_text()
        self.assertIn(check.codex_temp_environment(root, run, home), configs)
        for config in configs[1:-1]:
            self.assertIn(config, worker)

    def test_both_probe_manifests_keep_original_and_add_f01_controls(self):
        for worker in ('codex', 'opencode'):
            home, root, run, runtime, control = check.build_area(self.base, worker, self.base)
            manifest = json.loads((run / 'manifest.json').read_text())
            for name in ('账本', '配置', '偏好', '别的轮次', '素材', '.hermes', '.ssh', '符号链接',
                         '根外用户文档', '公共临时文件', '系统临时文件', 'CODEX_HOME/auth.json', '其他轮次ASCII临时目录'):
                self.assertIn(name, manifest['targets'])
            self.assertEqual(set(manifest['tools']), {'python3', 'git', 'bash'})
            self.assertIn('HOME外新文件', manifest['write_targets'])
            self.assertEqual(set(manifest['list_targets']), {'根外文档', '别的轮次',
                                                          '数据根目录', '交付父目录', '任务父目录'})
            self.assertTrue(manifest['allowed_write'])
            auth = Path(manifest['targets']['CODEX_HOME/auth.json'])
            self.assertEqual(auth.read_text(), '{}')
            self.assertFalse(auth.is_relative_to(run))
            if worker == 'codex':
                self.assertEqual(manifest['temporary'], str(check.codex_tmp(root, run, home)))
                self.assertEqual(set(check.readable_names(manifest)),
                                 {'根外用户文档', '公共临时文件', '系统临时文件', '列目录 根外文档', '其他轮次ASCII临时目录'})
                self.assertEqual(manifest['read_policy_note'], check.PLAN_A_NOTE)
            else:
                self.assertEqual(len(manifest['allowed_read']), 2)
                self.assertEqual(check.readable_names(manifest), [])

    def test_recitation_without_number_and_with_smart_quotes(self):
        body = self.first.split(' ', 1)[1]
        answer = '①结论\n本次收到的《工人守则》第一条为：“' + body + '”\n②产物清单\n无。'
        self.assertTrue(check.recitation_matches(self.first, answer))

    def test_recitation_number_whitespace_and_punctuation(self):
        for prefix in ('1. ', '１．', '(1) ', '（1）', '1、', ''):
            with self.subTest(prefix=prefix):
                body = self.first.split(' ', 1)[1].replace('；', ';').replace('，', ',').replace('。', '.')
                self.assertTrue(check.recitation_matches(self.first, '\n  ' + prefix + '“' + body + '”  \n'))

    def test_changed_or_incomplete_body_fails(self):
        for answer in (self.first.replace('不换命令', '可以换命令'), self.first[:-8], '', '第一条已收到'):
            with self.subTest(answer=answer):
                self.assertFalse(check.recitation_matches(self.first, answer))

    def test_launch_error_or_empty_output_is_not_isolation_failure(self):
        for text in ('', "error: unexpected argument '--ignore-user-config' found", '{}', '[]', '{broken'):
            with self.subTest(text=text):
                self.log.write_text(text)
                for rc in (0, 1, 125):
                    state, checks = check.kernel_checks(self.log, ['账本'], rc)
                    self.assertEqual(state, '探针未运行')
                    self.assertEqual(list(checks.values()), [None, None, None])

    def test_missing_log_is_not_run(self):
        self.assertEqual(check.kernel_checks(self.log, ['账本'], 125)[0], '探针未运行')

    def test_started_but_incomplete_is_distinct(self):
        self.log.write_text('FRCLYO_ISOLATION_PROBE_STARTED\n{"账本":{"blocked":true}}\n')
        state, checks = check.kernel_checks(self.log, ['账本'], 125)
        self.assertEqual(state, '探针未完成')
        self.assertTrue(all(value is None for value in checks.values()))

    def test_complete_results_and_exit_code_are_independent(self):
        for blocked, positive, rc in ((True, True, 0), (False, True, 1), (True, False, 1), (True, True, 125)):
            with self.subTest(blocked=blocked, positive=positive, rc=rc):
                self.log.write_text('warning\n' + json.dumps({'账本': {'blocked': blocked}, '本轮读写': {'ok': positive}}))
                state, checks = check.kernel_checks(self.log, ['账本'], rc)
                self.assertEqual(state, '已运行')
                self.assertEqual(list(checks.values()), [blocked, positive, rc == 0])

    def test_invalid_boolean_does_not_count_as_evidence(self):
        self.log.write_text(json.dumps({'账本': {'blocked': 1}, '本轮读写': {'ok': True}}))
        self.assertIsNone(check.kernel_checks(self.log, ['账本'], 0)[1]['禁读 账本'])

    def test_plan_a_readable_checks_require_success_not_missing_or_denied(self):
        for row, expected in (({'blocked': False, 'errno': None}, True),
                              ({'blocked': False, 'errno': 2}, False),
                              ({'blocked': False}, False),
                              ({'blocked': True, 'errno': 13}, False)):
            self.log.write_text(json.dumps({'根外用户文档': row, '本轮读写': {'ok': True}}))
            state, values = check.kernel_checks(self.log, ['根外用户文档'], 0, ['根外用户文档'])
            self.assertEqual(state, '已运行')
            self.assertIs(values['可读 根外用户文档'], expected)

    def test_plan_a_probe_allows_reads_but_keeps_write_denials(self):
        target = self.base / 'sentinel'
        target.write_text('ISOLATION_FAKE_SENTINEL')
        manifest = {'run': str(self.base), 'targets': {'根外用户文档': str(target)},
                    'readable_names': ['根外用户文档'], 'read_policy_note': check.PLAN_A_NOTE}
        for mode, expected in (('read', 0), ('write', 1), ('missing', 1)):
            if mode == 'write':
                manifest['write_targets'] = {'根外文件': str(self.base / 'unexpected-write')}
            elif mode == 'missing':
                manifest.pop('write_targets')
                target.unlink()
            path = self.base / 'manifest.json'
            path.write_text(json.dumps(manifest))
            process = subprocess.run([sys.executable, '-I', str(check.REPO / 'tests/host/isolation_probe.py'),
                                      str(path)], cwd=self.base, capture_output=True, text=True)
            self.assertEqual(process.returncode, expected, process.stderr)
            self.assertIn(check.PLAN_A_NOTE, process.stderr)
            data = json.loads(process.stdout)
            if mode == 'write':
                self.assertFalse(data['写入 根外文件']['blocked'])
            elif mode == 'missing':
                self.assertEqual(data['根外用户文档']['errno'], 2)

    def test_positive_write_failure_still_reports_read_results(self):
        manifest = self.base / 'manifest.json'
        manifest.write_text(json.dumps({'run': str(self.base / 'nonexistent'),
                                        'targets': {'缺失': str(self.base / 'missing')}}))
        result = subprocess.run([sys.executable, '-I', str(check.REPO / 'tests/host/isolation_probe.py'),
                                 str(manifest)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        data = json.loads(result.stdout)
        self.assertFalse(data['本轮读写']['ok'])
        self.assertFalse(data['缺失']['blocked'])
        self.assertIn('FRCLYO_ISOLATION_PROBE_STARTED', result.stderr)

    def test_execute_records_launch_exception(self):
        self.assertEqual(check.execute([str(self.base / 'no-command')], {}, self.base, self.log, 1), 125)
        self.assertIn('FileNotFoundError', self.log.read_text())
        self.assertIn('errno=2', self.log.read_text())

    def test_kernel_captures_child_stderr_and_exit_even_if_launcher_is_silent(self):
        def silent_launcher(argv, env, cwd, log, timeout):
            log.write_text('')
            # 模拟 CLI 抛弃标准输出，但子 shell 的本轮文件仍然留下。
            return subprocess.run(argv, cwd=cwd, env=env, capture_output=True).returncode
        command = check.kernel_command(['/bin/bash', '-c', 'printf child-failure >&2; exit 7'], self.base)
        with mock.patch.object(check, 'execute', side_effect=silent_launcher):
            rc = check.execute_kernel(command, {}, self.base, self.log, 2)
        self.assertEqual(rc, 7)
        self.assertIn('child-failure', self.log.read_text())
        diagnosis = check.kernel_diagnosis(self.log, '探针未运行', rc)
        self.assertIn('退出码=7', diagnosis['原因'])
        self.assertIn('shell 启动标记=True', diagnosis['原因'])
        self.assertTrue(any('child-failure' in line for line in diagnosis['stderr末尾']))

    def test_kernel_diagnosis_empty_missing_and_no_cli_are_explicit(self):
        self.assertIn('日志读取失败', check.kernel_diagnosis(self.log, '探针未运行', 125)['原因'])
        self.log.write_text('')
        self.assertIn('stdout/stderr 均为空', check.kernel_diagnosis(self.log, '探针未运行', 0)['原因'])
        rc = check.execute_kernel([], {}, self.base, self.log, 2)
        self.assertEqual(rc, 125)
        self.assertIn('required CLI missing', self.log.read_text())
        self.assertTrue(check.kernel_diagnosis(self.log, '探针未运行', rc)['stderr末尾'])

    def test_doctor_requires_command_exit_and_structured_errno(self):
        self.log.write_text('FRCLYO_KERNEL_COMMAND_EXIT=0\n')
        self.assertFalse(doctor.case_result(self.log, 0, structured=True)['ok'])
        self.log.write_text('FRCLYO_DOCTOR_RESULT={"ok":true,"errno":13}\nFRCLYO_KERNEL_COMMAND_EXIT=0\n')
        result = doctor.case_result(self.log, 0, structured=True)
        self.assertTrue(result['ok'])
        self.assertEqual(result['errno'], 13)
        self.assertFalse(doctor.case_result(self.log, 125, structured=True)['ok'])

    def test_doctor_native_cases_do_not_depend_on_python_and_keep_full_probe(self):
        manifest = {'targets': {'根外用户文档': '/fake/sentinel'}}
        cases = doctor.cases(self.base, manifest)
        self.assertEqual(cases['python3'][0], 'python3')
        self.assertEqual(cases['git'][0], 'git')
        self.assertEqual(cases['bash'][0], 'bash')
        self.assertEqual(cases['dev-null-shell'][0], '/bin/bash')
        self.assertIn('完整隔离探针', cases)

    def test_tool_stderr_and_errno_are_preserved_in_probe(self):
        manifest = {'run': str(self.base), 'targets': {}, 'tools': {'python3': ['python3']}}
        with mock.patch.object(isolation_probe.subprocess, 'run',
                               return_value=subprocess.CompletedProcess([], 2, '', 'xcrun sample failure\n')), \
             mock.patch.object(isolation_probe.os, 'getcwd', side_effect=PermissionError(1, 'synthetic denial')), \
             contextlib.redirect_stdout(io.StringIO()) as out, \
             contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(isolation_probe.probe(manifest), 1)
        data = json.loads(out.getvalue())['本轮读写']
        self.assertFalse(data['ok'])
        self.assertEqual(data['diagnostics']['python3']['exit'], 2)
        self.assertEqual(data['diagnostics']['getcwd']['errno'], 1)
        self.assertIn('xcrun sample failure', err.getvalue())

    def test_doctor_continues_independent_cases_and_both_workers_after_python_failure(self):
        directories = [self.base / name for name in ('evidence', 'public', 'system')]
        for path in directories:
            path.mkdir()
        def execute(argv, env, run, log, timeout, executor=None):
            expected = run.parents[3] if 'codex' in run.parts else run.parents[2] / '运行时/opencode/rounds/ISOLATION/r1/home'
            self.assertEqual(env['HOME'], str(expected))
            if 'codex' in run.parts:
                self.assertIn('HOME=' + str(check.codex_tmp(run.parents[2], run, run.parents[3]) / 'home'), argv)
            self.assertNotIn('DEEPSEEK_API_KEY', env)
            rc = 127 if 'print("PYTHON_PATH_OK")' in argv else 0
            text = 'FRCLYO_KERNEL_SHELL_STARTED\nFRCLYO_KERNEL_COMMAND_EXIT=' + str(rc) + '\n'
            if any(case in argv for case in (doctor.CASE, doctor.SWIFT_CASE, doctor.ANCESTOR_CASE, doctor.TOOLS_CASE)):
                text += 'FRCLYO_DOCTOR_RESULT={"ok":true,"errno":null}\n'
            if str(run / 'probe.py') in argv:
                manifest = json.loads((run / 'manifest.json').read_text())
                data = {name: {'blocked': name not in check.readable_names(manifest),
                               'errno': None if name in check.readable_names(manifest) else 13}
                        for name in check.probe_names(manifest)}
                data['本轮读写'] = {'ok': True}
                text += json.dumps(data) + '\n'
            log.write_text(text)
            return rc
        with mock.patch.object(doctor.sys, 'platform', 'darwin'), \
             mock.patch.object(doctor.shutil, 'which', side_effect=lambda name: '/fake/' + name), \
             mock.patch.object(doctor.tempfile, 'mkdtemp', side_effect=[str(p) for p in directories]), \
             mock.patch.object(check, 'execute_kernel', side_effect=execute), \
             mock.patch.object(doctor, 'opencode_matrix', return_value={'ok': True}), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(doctor.main([]), 1)
        summary = json.loads((directories[0] / 'summary.json').read_text())
        for worker in ('codex', 'opencode'):
            rows = summary['workers'][worker]
            self.assertFalse(rows['python3']['ok'])
            self.assertTrue(rows['git']['ok'])
            self.assertTrue(rows['完整隔离探针']['ok'])
            self.assertIn('禁读 CODEX_HOME/auth.json', rows['完整隔离探针']['checks'])

    def test_execute_records_timeout(self):
        with mock.patch.object(check.subprocess, 'run', side_effect=subprocess.TimeoutExpired('fake', 1)):
            self.assertEqual(check.execute(['fake'], {}, self.base, self.log, 1), 125)
        self.assertIn('TimeoutExpired', self.log.read_text())

    def test_main_reports_launch_failure_and_runs_recitation_independently(self):
        source = self.base / 'models.json'
        source.write_text(json.dumps({'deepseek': {'npm': '@ai-sdk/deepseek',
            'api': 'https://api.deepseek.com', 'models': {'deepseek-flash': {}}}}))
        evidence = self.base / 'evidence'
        evidence.mkdir()
        kernel_homes = []
        host_config = self.base / 'host-config.json'
        host_config.write_text(json.dumps({'keychain_entries': {
            'deepseek': {'service': 'external-fixture-service', 'account': 'fixture-account'}},
            'models_snapshot_source': str(source)}))

        def execute(argv, env, cwd, log, timeout):
            log.write_text('')
            if log.name == 'kernel.log':
                self.assertNotEqual(env['HOME'], str(self.base))
                if argv[0] == '/fake/codex':
                    kernel_home = Path(env['CODEX_HOME'])
                    self.assertEqual([p.name for p in kernel_home.iterdir()], ['auth.json'])
                    self.assertEqual((kernel_home / 'auth.json').read_text(), '{}')
                    kernel_homes.append(kernel_home)
                    log.write_text("error: unexpected argument\n")
                    return 2
                manifest = json.loads((cwd / 'manifest.json').read_text())
                names = check.probe_names(manifest)
                data = {name: {'blocked': True} for name in names}
                data['本轮读写'] = {'ok': True}
                log.write_text(json.dumps(data))
            elif log.name == 'model-launch.log':
                self.assertEqual(env['CODEX_HOME'], str(self.base / 'original-codex'))
                self.assertNotIn('DEEPSEEK_API_KEY', env)
                self.assertNotIn('FRCLYO_TEST_KEY', env)
                self.assertEqual(env['PATH'], os.environ['PATH'])
                self.assertFalse((log.parent / 'commands/security').exists())
                if 'opencode-worker.sh' in argv[1]:
                    self.assertEqual(env['HOME'], str(self.base))
                    self.assertNotEqual(env['FRCLYO_HOME'], str(self.base / 'host-root'))
                    cfg = json.loads((Path(env['FRCLYO_HOME']) / '配置/config.json').read_text())
                    self.assertEqual(cfg['keychain_entries']['deepseek'], {
                        'service': 'external-fixture-service', 'account': 'fixture-account'})
                    self.assertEqual(cfg['models_snapshot_source'], str(source))
                    from frclyo.worker import snapshot
                    runtime = Path(env['FRCLYO_HOME']) / '运行时/opencode'
                    self.assertFalse((runtime / 'models.snapshot.json').exists())
                    # 走生产快照加载器，不能用假启动成功掩盖哨兵污染。
                    path, catalog = snapshot(dict(cfg, runtime_dir=str(runtime)),
                                             'deepseek', 'deepseek-flash')
                    self.assertEqual(Path(path).read_bytes(), source.read_bytes())
                    self.assertIn('deepseek-flash', catalog['models'])
                output = Path(env['FRCLYO_HOME']) / '交付/ISOLATION/model'
                output.mkdir()
                (output / '结果.txt').write_text('①结论\n“' + self.first.split(' ', 1)[1] + '”')
                (output / '回执.json').write_text(json.dumps({'状态': '执行完成', '守则sha256':
                    hashlib.sha256((check.REPO / '工人守则.md').read_bytes()).hexdigest()}))
            return 0

        with mock.patch.object(check.sys, 'platform', 'darwin'), \
             mock.patch.object(check.sys, 'argv', ['check', '--models-source', str(source)]), \
             mock.patch.object(check.shutil, 'which', side_effect=lambda name: '/fake/' + name), \
             mock.patch.object(check.tempfile, 'mkdtemp', return_value=str(evidence)), \
             mock.patch.object(check, 'execute', side_effect=execute), \
             mock.patch.dict(os.environ, {'HOME': str(self.base), 'FRCLYO_HOME': str(self.base / 'host-root'),
                'FRCLYO_CONFIG': str(host_config), 'CODEX_HOME': str(self.base / 'original-codex'),
                'DEEPSEEK_API_KEY': ''}), \
             contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(check.main(), 1)
        summary = json.loads((evidence / 'summary.json').read_text())
        self.assertEqual(summary['probes']['codex']['状态'], '探针未运行')
        self.assertEqual(summary['probes']['codex']['退出码'], 2)
        self.assertTrue(all(value is None for name, value in summary['checks'].items()
                            if name.startswith('codex ') and '复述' not in name))
        self.assertTrue(summary['checks']['codex 真实守则复述'])
        self.assertTrue(summary['checks']['opencode 真实守则复述'])
        self.assertIn('0 未通过', stdout.getvalue())
        self.assertTrue(any('CODEX_HOME/auth.json' in name for name in summary['checks']))
        self.assertEqual(len(kernel_homes), 1)

    def test_live_config_uses_only_route_keychain_names_and_snapshot(self):
        # 生产加载器用替身；不会读取宿主配置或任何凭据文件。
        cfg = {'providers': {'deepseek': {'opencode_family': 'deepseek',
            'api_host': 'api.deepseek.com', 'balance_api': 'deepseek', 'unexpected': 'DO_NOT_COPY'}},
            'keychain_entries': {'deepseek': {'service': 'external', 'account': 'fixture',
                                            'unexpected': 'DO_NOT_COPY'}},
            'key_env_file': '/never/read/credentials', 'unknown': 'DO_NOT_COPY',
            'models_snapshot_source': str(self.base / 'configured-models.json')}
        with mock.patch.object(check.config, 'load', return_value=cfg):
            result = check.recitation_config()
            self.assertEqual(result, {'providers': {'deepseek': {
                'opencode_family': 'deepseek', 'api_host': 'api.deepseek.com', 'balance_api': 'deepseek'}},
                'keychain_entries': {'deepseek': {'service': 'external', 'account': 'fixture'}},
                'models_snapshot_source': str(self.base / 'configured-models.json')})
            self.assertEqual(check.recitation_config(self.base / 'override.json')['models_snapshot_source'],
                             str(self.base / 'override.json'))
            del cfg['keychain_entries']
            self.assertNotIn('keychain_entries', check.recitation_config())
            cfg['providers'] = {}
            with self.assertRaises(ValueError):
                check.recitation_config()


if __name__ == '__main__':
    unittest.main()
