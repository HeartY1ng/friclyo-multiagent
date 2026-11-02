"""兼容性诊断的离线回归；不启动 CLI/沙箱/编译器，不执行 Git 提交。"""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'tests/host'))
import sandbox_doctor as doctor


class ToolCompatibilityTests(unittest.TestCase):
    def test_both_workers_keep_denials_and_required_tools(self):
        manifest = {'targets': {'根外用户文档': '/fake/denied'}, 'temporary': '/fake/tmp'}
        for worker in ('codex', 'opencode'):
            with self.subTest(worker=worker), mock.patch.object(doctor.shutil, 'which', return_value='/fake/node'):
                cases = doctor.cases(Path('/fake/run'), manifest, worker)
            for name in ('根外哨兵', '完整隔离探针', '逐级上层目录stat和chdir',
                         'private-home', 'zsh-heredoc', 'bash-heredoc', 'mktemp', 'sort-large',
                         'git-init', 'git-add', 'git-commit', 'git-status', 'git-diff', 'copy-back',
                         'python-tempfile', 'node-tmpdir'):
                self.assertIn(name, cases)
            swift = cases['Swift编译运行和ASCII临时目录']
            self.assertEqual('private-cwd' in swift, worker == 'codex')
            self.assertEqual('私有目录上层可遍历' in cases, worker == 'codex')
            self.assertEqual(cases['git-init'][-1], worker)
            self.assertEqual(list(cases)[-1], '完整隔离探针')
        with mock.patch.object(doctor.shutil, 'which', return_value=None):
            self.assertNotIn('node-tmpdir', doctor.common_tool_cases(Path('/fake/run'), manifest))

    def run_tool(self, name, responses, worker='opencode'):
        with mock.patch.object(sys, 'argv', ['probe', name, '/fake/run', '/fake/tmp', worker]), \
             mock.patch.object(subprocess, 'run', side_effect=responses) as launch, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as exit_info:
                exec(doctor.TOOLS_CASE, {})
        return exit_info.exception.code, json.loads(output.getvalue().partition('=')[2]), launch

    def test_heredoc_checks_output_and_keeps_warning(self):
        for name in ('zsh-heredoc', 'bash-heredoc'):
            rc, result, launch = self.run_tool(name, [subprocess.CompletedProcess([], 0, 'HEREDOC_OK\n', 'warning\n')])
            self.assertEqual(rc, 0)
            self.assertTrue(result['ok'])
            self.assertTrue(result['warning'])
            self.assertEqual(result['stderr_tail'], ['warning'])
            argv = launch.call_args.args[0]
            self.assertEqual(argv[:2], ['/bin/' + name.split('-')[0], '-lc'])
            self.assertIn("<<'PY'", argv[2])
        rc, result, _ = self.run_tool('zsh-heredoc', [subprocess.CompletedProcess([], 0, 'WRONG', '')])
        self.assertEqual(rc, 1)
        self.assertFalse(result['ok'])

    def test_errno_exit_and_timeout_are_distinct_failures(self):
        responses = ([PermissionError(13, 'synthetic denial')],
                     [subprocess.CompletedProcess([], 7, '', 'tool failed\n')],
                     [subprocess.TimeoutExpired('fake', 65, stderr=b'timeout details\n')])
        for response in responses:
            rc, row, _ = self.run_tool('zsh-heredoc', response)
            self.assertEqual(rc, 1)
            self.assertFalse(row['ok'])
            self.assertEqual(row['errno'], 13 if isinstance(response[0], PermissionError) else None)
            if not isinstance(response[0], PermissionError):
                self.assertTrue(row['stderr_tail'])

    def test_mktemp_uses_worker_specific_first_invocation(self):
        manifest = {'temporary': '/fake/tmp'}
        for worker, name, command in (
            ('codex', 'mktemp', ['mktemp']),
            ('opencode', 'mktemp-explicit', ['mktemp', '-p', '/fake/tmp']),
        ):
            cases = doctor.common_tool_cases(Path('/fake/run'), manifest, worker)
            self.assertEqual(cases['mktemp'][4], name)
            with mock.patch.object(Path, 'resolve', lambda p: p), \
                 mock.patch.object(Path, 'is_file', return_value=True), \
                 mock.patch.object(Path, 'unlink'):
                rc, row, launch = self.run_tool(name, [subprocess.CompletedProcess([], 0, '/fake/tmp/new\n', '')])
            self.assertEqual(rc, 0)
            self.assertEqual(launch.call_args.args[0], command)
            self.assertEqual(row['invocation'], command)
            self.assertEqual(launch.call_count, 1)

    def test_external_read_denial_is_failure_only_for_codex(self):
        for worker in ('codex', 'opencode'):
            for failure in (None, PermissionError(13, 'synthetic denial'), FileNotFoundError(2, 'missing')):
                opened = mock.mock_open(read_data=b'x')
                if failure:
                    opened.side_effect = failure
                with mock.patch.object(sys, 'argv', ['probe', '根外哨兵', '/fake/file', '/fake/run', worker]), \
                     mock.patch('builtins.open', opened), contextlib.redirect_stdout(io.StringIO()) as output:
                    with self.assertRaises(SystemExit) as exit_info:
                        exec(doctor.CASE, {})
                row = json.loads(output.getvalue().partition('=')[2])
                expected = failure is None if worker == 'codex' else isinstance(failure, PermissionError)
                self.assertIs(row['ok'], expected)
                self.assertEqual(exit_info.exception.code, 0 if expected else 1)
                if worker == 'codex':
                    self.assertIn('按方案 A，Codex 不做根外禁读', row['note'])

    def test_ancestor_policy_does_not_require_protected_directory_content(self):
        run = Path('/Users/fixture/AgentWork/交付/T/r1')
        manifest = {'root': '/Users/fixture/AgentWork', 'home': '/Users/fixture'}
        codex = doctor.ancestor_expectations(run, manifest, 'codex')
        opencode = doctor.ancestor_expectations(run, manifest, 'opencode')
        self.assertEqual(codex['/Users']['stat'], 'allow')
        self.assertEqual(codex[manifest['root']]['stat'], 'deny')
        self.assertEqual(opencode['/Users']['stat'], 'allow')
        self.assertEqual(opencode['/Users']['chdir'], 'allow-or-permission-denied')
        for policy in (codex, opencode):
            self.assertEqual(set(policy[str(run)].values()), {'allow'})
            self.assertEqual(set(policy['/'].values()), {'allow'})

    def test_ancestor_getcwd_error_is_separate_and_unexpected_errno_fails(self):
        run = Path('/Users/fixture/AgentWork/交付/T/r1')
        manifest = {'root': '/Users/fixture/AgentWork', 'home': '/Users/fixture'}
        expected = doctor.ancestor_expectations(run, manifest, 'opencode')
        for number, ok in ((13, True), (2, False)):
            current = [run]
            def enter(path):
                current[0] = path
            def cwd():
                if current[0] == Path('/Users'):
                    raise OSError(number, 'synthetic getcwd failure')
                return str(current[0])
            with mock.patch.object(sys, 'argv', ['probe', str(run), json.dumps(expected)]), \
                 mock.patch.object(os, 'stat'), mock.patch.object(os, 'chdir', side_effect=enter), \
                 mock.patch.object(os, 'getcwd', side_effect=cwd), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                with self.assertRaises(SystemExit):
                    exec(doctor.ANCESTOR_CASE, {})
            row = json.loads(output.getvalue().partition('=')[2])
            self.assertIs(row['ok'], ok)
            ancestor = next(item for item in row['ancestors'] if item['path'] == '/Users')
            self.assertTrue(ancestor['stat']['ok'])
            self.assertTrue(ancestor['chdir']['ok'])
            self.assertEqual(ancestor['getcwd']['errno'], number)
            self.assertTrue(row['warning'])

    def test_git_commit_is_local_and_verified_without_executing_it(self):
        for worker, directory in (('codex', '/fake/tmp'), ('opencode', '/fake/run')):
            rc, row, launch = self.run_tool('git-commit', [
                subprocess.CompletedProcess([], 0, 'committed', ''),
                subprocess.CompletedProcess([], 0, 'sandbox doctor local fixture\n', '')], worker)
            self.assertEqual(rc, 0)
            for call in launch.call_args_list:
                self.assertEqual(call.args[0][:3], ['git', '--git-dir=' + directory + '/doctor-git/.git',
                                                   '--work-tree=' + directory + '/doctor-git'])
                self.assertEqual(call.kwargs['cwd'], Path(directory) / 'doctor-git')
        rc, _, _ = self.run_tool('git-status', [subprocess.CompletedProcess([], 0, ' M sample.txt\n', '')])
        self.assertEqual(rc, 1)

    def test_codex_git_init_starts_from_private_tmp(self):
        with mock.patch.object(Path, 'write_text'):
            rc, _, launch = self.run_tool('git-init', [subprocess.CompletedProcess([], 0, '', '')] * 4, 'codex')
        self.assertEqual(rc, 0)
        self.assertEqual(launch.call_args_list[0].args[0], ['git', 'init', '/fake/tmp/doctor-git'])
        self.assertEqual(launch.call_args_list[0].kwargs['cwd'], Path('/fake/tmp'))

    def test_copy_back_verifies_source_and_patch_and_rejects_bad_copy(self):
        for copied in (b'content', b'changed'):
            with mock.patch.object(Path, 'mkdir'), mock.patch.object(doctor.shutil, 'copy2') as copy, \
                 mock.patch.object(Path, 'read_bytes', side_effect=[copied, b'content', b'content', b'content']), \
                 mock.patch.object(Path, 'read_text', side_effect=['after\n', '-before\n+after\n']):
                rc, row, launch = self.run_tool('copy-back', [], 'codex')
            self.assertEqual(rc, 0 if copied == b'content' else 1)
            self.assertEqual(copy.call_args_list[0].args, (
                Path('/fake/tmp/doctor-git/sample.txt'), Path('/fake/run/doctor-copy-back/sample.txt')))
            launch.assert_not_called()

    def test_swift_codex_compiles_from_private_tmp_and_copies_back(self):
        values = dict(TMPDIR='/fake/tmp', TMP='/fake/tmp', TEMP='/fake/tmp', TMPPREFIX='/fake/tmp/zsh',
                      CLANG_MODULE_CACHE_PATH='/fake/tmp/clang-module-cache',
                      SWIFT_MODULECACHE_PATH='/fake/tmp/swift-module-cache')
        with mock.patch.dict(os.environ, values, clear=True), \
             mock.patch.object(sys, 'argv', ['probe', '/fake/run', '/fake/tmp', 'private-cwd']), \
             mock.patch.object(Path, 'write_text'), mock.patch.object(Path, 'resolve', lambda p: p), \
             mock.patch.object(Path, 'read_bytes', return_value=b'artifact'), \
             mock.patch.object(doctor.shutil, 'copy2') as copy, \
             mock.patch.object(subprocess, 'run', side_effect=[
                 subprocess.CompletedProcess([], 0, '', 'compiler warning\n'),
                 subprocess.CompletedProcess([], 0, 'ok\n', '')]) as launch, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as exit_info:
                exec(doctor.SWIFT_CASE, {})
        self.assertEqual(exit_info.exception.code, 0)
        self.assertEqual(launch.call_args_list[0].kwargs['cwd'], '/fake/tmp')
        self.assertEqual(launch.call_args_list[0].args[0][1], '/fake/tmp/doctor.swift')
        self.assertEqual(copy.call_count, 2)
        result = json.loads(output.getvalue().partition('=')[2])
        self.assertTrue(result['warning'])
        self.assertTrue(result['copy_back'])

    def test_status_does_not_mistake_log_markers_for_warnings(self):
        for stderr, warning, status in (('', False, '通过'), ('a warning\n', False, '通过（有警告）'),
                                        ('', True, '通过（有警告）')):
            row = json.dumps(dict(ok=True, warning=warning, stderr_tail=[]))
            log = ('FRCLYO_KERNEL_COMMAND_EXIT=0\n--- child stdout ---\nFRCLYO_DOCTOR_RESULT=' + row
                   + '\n--- child stderr ---\n' + stderr)
            with mock.patch.object(Path, 'read_text', return_value=log):
                result = doctor.case_result(Path('/fake/log'), 0, structured=True)
            self.assertTrue(result['ok'])
            self.assertEqual(result['status'], status)
        with mock.patch.object(Path, 'read_text', return_value=log):
            self.assertFalse(doctor.case_result(Path('/fake/log'), 125, structured=True)['ok'])


if __name__ == '__main__':
    unittest.main()
