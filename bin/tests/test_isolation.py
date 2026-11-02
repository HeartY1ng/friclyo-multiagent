"""权限生成离线回归：不启动沙箱、不访问实际用户配置或 CLI 安装目录。"""
import json
import contextlib
import os
from pathlib import Path
import re
import sys
import unittest
from unittest import mock
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from frclyo import isolation


def filesystem(text):
    # 生成器输出的子集：JSON 字符串键值组成的 TOML inline table。
    string = r'"(?:[^"\\]|\\.)*"'
    return {json.loads(key): json.loads(value) for key, value in
            re.findall('(' + string + ')=(' + string + ')', text)}


class IsolationTests(unittest.TestCase):
    def setUp(self):
        self.home = '/Users/fake 用户 "quote"\\slash'
        self.root = self.home + '/AgentWork'
        self.run = self.root + '/交付/TASK/r1'
        self.runtime = self.root + '/运行时/opencode/rounds/TASK/r1'
        self.control = self.root + '/运行时/控制/opencode/TASK/r1'
        self.binary = self.home + '/tools/opencode'
        self.snapshot = self.root + '/运行时/opencode/models.snapshot.json'
        self.template = self.root + '/运行时/opencode/worker-TASK-r1.json'
        self.env = mock.patch.dict(os.environ, {'HOME': self.home, 'CODEX_HOME': self.home + '/private-codex'}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        # realpath 仅在虚拟路径上测试转义；不检查/读取任何真实用户目录。
        self.canonical = mock.patch.object(isolation.os.path, 'realpath', side_effect=lambda p, **kwargs: os.path.abspath(p))
        self.canonical.start()
        self.addCleanup(self.canonical.stop)
        self.binaries = mock.patch.object(isolation, 'runtime_read_paths', return_value=[self.binary])
        self.binaries.start()
        self.addCleanup(self.binaries.stop)

    def profile(self, **kwargs):
        return isolation.seatbelt(self.home, self.root, self.run, self.runtime,
                                  self.control, 12345, [], [self.root],
                                  [self.snapshot, self.template], **kwargs)

    def test_seatbelt_denies_each_user_data_root_with_precise_exceptions(self):
        text = self.profile()
        for root in isolation.USER_DATA_ROOTS + (self.home, self.root):
            self.assertIn('(require-all (subpath ' + json.dumps(root, ensure_ascii=False) + ')', text)
        for literal in (self.binary, self.template, self.snapshot):
            self.assertIn('(require-not (literal ' + json.dumps(literal, ensure_ascii=False) + '))', text)
        self.assertNotIn('(subpath ' + json.dumps(str(Path(self.binary).parent), ensure_ascii=False) + ')', text)
        self.assertIn('(allow default)', text)
        self.assertIn('(deny network*)', text)
        self.assertIn('(remote tcp4 "localhost:12345")', text)

    def test_seatbelt_write_boundary_and_secret_denies_unchanged(self):
        text = self.profile()
        quote = lambda p: json.dumps(p, ensure_ascii=False)
        write = '(deny file-write* (require-all (require-not (subpath %s)) (require-not (subpath %s)) (require-not (subpath %s)) (require-not (literal "/dev/null"))))' % (quote(self.run), quote(self.runtime), quote(str(isolation.round_tmp(self.root, self.run))))
        self.assertIn(write, text)
        self.assertIn('(subpath ' + quote(self.home + '/private-codex') + ')', text)
        self.assertIn('(deny file-read-data file-read-xattr file-write* (subpath ' + quote(self.control) + '))', text)
        self.assertNotIn('(deny file-write* )', isolation.seatbelt(
            self.home, self.root, self.run, self.runtime, self.control, 9, [], []))

    def test_codex_only_run_private_tmp_and_null_are_explicitly_writable(self):
        paths = filesystem(isolation.codex_permission(self.root, self.run, [], self.runtime))
        for root in (self.root, self.runtime):
            self.assertEqual(paths[root], 'deny')
        for root in ('/Users', '/Volumes', self.home):
            self.assertNotIn(root, paths)
        for root in ('/private/tmp', '/private/var/folders', '/tmp', '/var/folders'):
            self.assertEqual(paths[root], 'read')
        temporary = str(isolation.codex_tmp(self.root, self.run))
        self.assertEqual({p for p, mode in paths.items() if mode == 'write'}, {self.run, temporary, '/dev/null'})
        self.assertEqual(paths['/'], 'read')
        # 精确覆盖继承键；只写 /tmp="read" 不能移除特殊根的写权限。
        for key in (':tmpdir', ':slash_tmp'):
            self.assertEqual(paths[key], 'read')
        self.assertNotIn(self.binary, paths)  # 根外工具继承 / 的只读，不给数据根内工具开例外。
        self.assertEqual(paths[self.home + '/private-codex'], 'deny')
        self.assertNotIn(str(Path(self.binary).parent), paths)
        setting = filesystem(re.sub(r'([A-Z_]+)=', r'"\1"=', isolation.codex_temp_environment(self.root, self.run)))
        self.assertEqual(setting, isolation.codex_command_environment(self.root, self.run))
        self.assertEqual(setting['HOME'], temporary + '/home')
        self.assertEqual(setting['ZDOTDIR'], setting['HOME'])
        self.assertEqual(setting['TMPPREFIX'], temporary + '/zsh')
        self.assertNotIn('CODEX_HOME', setting)
        self.assertEqual(os.environ['HOME'], self.home)
        self.assertEqual(os.environ['CODEX_HOME'], self.home + '/private-codex')


    def test_codex_network_does_not_change_filesystem_or_opencode(self):
        baseline = isolation.codex_permission(self.root, self.run, [], self.runtime)
        opencode = self.profile()
        for value in ('1', '0', 'true', ''):
            with mock.patch.dict(os.environ, CODEX_WORKER_NET=value):
                profile = isolation.codex_permission(self.root, self.run, [], self.runtime)
                self.assertIn('network={enabled=' + ('true' if value == '1' else 'false') + '}', profile)
                self.assertEqual(filesystem(profile), filesystem(baseline))
                self.assertEqual(self.profile(), opencode)

    def test_metadata_is_allowed_without_content_or_xattr_reading(self):
        profile = self.profile()
        self.assertNotIn('(deny file-read*', profile)
        self.assertNotIn('(deny file-read-metadata', profile)
        # default 的元数据许可不覆盖内容/xattr 禁令；秘密、控制目录也保留内容禁读。
        self.assertEqual(profile.count('(deny file-read-data file-read-xattr'), 3)
        self.assertNotIn('(allow file-read*', profile)
        self.assertNotIn('(subpath "/dev")', profile)
        self.assertIn('(literal "/dev/null")', profile)
        paths = filesystem(isolation.codex_permission(self.root, self.run, [], self.runtime))
        for name in ('/dev/urandom', '/dev/zero', '/dev/random'):
            self.assertEqual(paths[name], 'read')
        self.assertNotIn('/dev', paths)
        self.assertNotIn('xcrun_db', profile)
        self.assertEqual(paths['/private/var/folders'], 'read')

    def test_plan_a_keeps_data_root_and_secret_denials_with_external_reads(self):
        sensitive = [self.home + '/credentials.env']
        paths = filesystem(isolation.codex_permission(self.root, self.run, sensitive, self.runtime))
        def mode(path):
            candidates = [(key, value) for key, value in paths.items()
                          if key.startswith('/') and Path(path).is_relative_to(key)]
            return max(candidates, key=lambda pair: len(pair[0]))[1]
        for path in ('/Users', self.home, self.home + '/Documents/file',
                     '/private/tmp/public', '/private/var/folders/public'):
            self.assertEqual(mode(path), 'read')
        for path in (self.root + '/账本.jsonl', self.root + '/配置/config.json',
                     self.root + '/交付/OTHER/r9/file', self.root + '/素材/file',
                     self.root + '/运行时/控制/events', self.home + '/private-codex/auth.json',
                     self.home + '/.ssh/file', sensitive[0]):
            self.assertEqual(mode(path), 'deny')
        self.assertEqual(mode(self.run + '/result'), 'write')
        self.assertEqual(mode(str(isolation.codex_tmp(self.root, self.run)) + '/state'), 'write')
        with mock.patch.object(isolation, 'runtime_read_paths', return_value=[self.root + '/tools/git']) as discover:
            self.assertEqual(paths, filesystem(isolation.codex_permission(self.root, self.run, sensitive, self.runtime)))
            discover.assert_not_called()

    def test_nested_secret_denial_remains_more_specific(self):
        sensitive = [self.run + '/private']
        paths = filesystem(isolation.codex_permission(self.root, self.run, sensitive, self.runtime))
        self.assertEqual(paths[sensitive[0]], 'deny')
        self.assertEqual(paths[self.run], 'write')

    def test_secret_directory_never_gets_binary_or_round_exception(self):
        with mock.patch.object(isolation, 'runtime_read_paths', return_value=[self.home + '/private-codex/bin/codex']):
            with self.assertRaises(ValueError):
                self.profile()
            paths = filesystem(isolation.codex_permission(self.root, self.run, [], self.runtime))
            self.assertNotIn(self.home + '/private-codex/bin/codex', paths)
            self.assertEqual(paths[self.home + '/private-codex'], 'deny')
        with mock.patch.dict(os.environ, {'CODEX_HOME': self.root}):
            with self.assertRaises(ValueError):
                isolation.codex_permission(self.root, self.run, [], self.runtime)
        for generator in (
            lambda: isolation.codex_permission(self.root, self.run, ['/dev'], self.runtime),
            lambda: isolation.seatbelt(self.home, self.root, self.run, self.runtime,
                                       self.control, 9, ['/dev'], []),
        ):
            with self.assertRaises(ValueError):
                generator()

    def test_control_characters_rejected_in_both_formats(self):
        for char in ('\n', '\r', '\x00', '\x7f'):
            with mock.patch.object(isolation, 'runtime_read_paths', return_value=[self.binary + char]):
                with self.assertRaises(ValueError):
                    self.profile()
            with self.assertRaises(ValueError):
                isolation.codex_permission(self.root, self.run, [self.home + char], self.runtime)

    def test_runtime_discovery_grants_files_never_install_parent(self):
        with mock.patch.object(isolation.shutil, 'which', side_effect=lambda name: self.home + '/apps/' + name), \
             mock.patch.object(isolation.sys, 'executable', '/usr/bin/python3'):
            # 直接调用原函数，撤销 setUp 的 mock。
            self.binaries.stop()
            try:
                paths = isolation.runtime_read_paths('opencode')
            finally:
                self.binaries.start()
        self.assertEqual(len(paths), 7)
        self.assertIn(self.home + '/apps/bun', paths)
        self.assertNotIn(self.home + '/apps', paths)

    def test_private_tmp_creation_uses_safe_exclusive_directories(self):
        temporary = isolation.codex_tmp(self.root, self.run)
        with mock.patch('frclyo.execution.private_directory') as parent, \
             mock.patch('frclyo.execution.exclusive_directory') as create:
            self.assertEqual(isolation.prepare_codex_tmp(self.root, self.run), str(temporary))
            parent.assert_called_once_with(temporary.parent)
            self.assertEqual(create.call_args_list, [mock.call(temporary)] + [mock.call(temporary / name) for name in (
                'clang-module-cache', 'swift-module-cache', 'cache', 'npm-cache', 'pip-cache',
                'home', 'home/.config', 'home/.local', 'home/.local/share', 'home/.local/state')])
        with mock.patch('frclyo.execution.private_directory'), \
             mock.patch('frclyo.execution.exclusive_directory', side_effect=FileExistsError), \
             self.assertRaises(FileExistsError):
            isolation.prepare_codex_tmp(self.root, self.run)

    def test_codex_tmp_is_external_ascii_unique_and_not_opencode_tmp(self):
        home = '/Users/fixture'
        root = home + '/AgentWork'
        run = root + '/交付/TASK/r1'
        temporary = isolation.codex_tmp(root, run, home)
        self.assertEqual(temporary.parent, Path(home) / '.frclyo-work')
        self.assertFalse(temporary.is_relative_to(root))
        self.assertTrue(str(temporary).isascii())
        self.assertNotEqual(temporary, isolation.round_tmp(root, run))
        for other_root, task, round_id in ((root + '-other', 'TASK', 'r1'),
                                           (root, 'TASK', 'r2'), (root, 'TASK-r1', 'r1')):
            self.assertNotEqual(temporary, isolation.codex_tmp(
                other_root, other_root + '/交付/' + task + '/' + round_id, home))
        for other_home in (self.home, root):
            other = isolation.codex_tmp(root, run, other_home)
            self.assertTrue(str(other).isascii())
            self.assertFalse(other.is_relative_to(root))
            self.assertEqual(other.parent, Path('/private/tmp') / ('frclyo-work-%s' % os.getuid()))
        with self.assertRaises(ValueError):
            isolation.codex_tmp('/', '/交付/T/r1', home)
        for task in ('中文', 'with space', 'bad.name'):
            with self.assertRaises(ValueError):
                isolation.codex_tmp(root, root + '/交付/' + task + '/r1', home)

    def test_private_parent_rejects_wrong_owner_mode_and_symlinks(self):
        from frclyo import execution
        for uid, mode, allowed in ((123, 0o40700, True), (123, 0o40755, False),
                                   (124, 0o40700, False)):
            for exists in (False, True):
                with mock.patch.object(execution, 'exclusive_directory',
                                       side_effect=FileExistsError if exists else None) as create, \
                     mock.patch.object(execution, 'directory', return_value=contextlib.nullcontext(9)), \
                     mock.patch.object(execution.os, 'getuid', return_value=123), \
                     mock.patch.object(execution.os, 'fstat', return_value=SimpleNamespace(st_uid=uid, st_mode=mode)):
                    if allowed:
                        execution.private_directory('/fake/private')
                    else:
                        with self.assertRaises(ValueError):
                            execution.private_directory('/fake/private')
                    create.assert_called_once_with('/fake/private')
        with mock.patch.object(execution, 'exclusive_directory', side_effect=FileExistsError), \
             mock.patch.object(execution, 'directory', side_effect=OSError('NOFOLLOW')):
            with self.assertRaises(OSError):
                execution.private_directory('/fake/link')

    def test_inherited_special_write_roots_are_overridden_without_losing_reads(self):
        paths = {':tmpdir': 'write', ':slash_tmp': 'write', self.run: 'write'}
        paths.update(filesystem(isolation.codex_permission(self.root, self.run, [], self.runtime)))
        self.assertEqual({key for key, value in paths.items() if value == 'write'},
                         {self.run, str(isolation.codex_tmp(self.root, self.run)), '/dev/null'})
        sibling = isolation.codex_tmp(self.root, self.root + '/交付/TASK/r2')
        ancestors = [(key, value) for key, value in paths.items()
                     if key.startswith('/') and sibling.is_relative_to(key)]
        self.assertEqual(max(ancestors, key=lambda pair: len(pair[0]))[1], 'read')

    def test_codex_worker_sets_private_environment_before_supervision(self):
        source = (Path(__file__).resolve().parents[1] / 'codex-worker.sh').read_text()
        self.assertIn('from frclyo.isolation import prepare_codex_tmp', source)
        exported = source.index('export TMPDIR="$ROUND_TMP" TMP="$ROUND_TMP" TEMP="$ROUND_TMP"')
        self.assertLess(exported, source.index('PERM=$(python3'))
        self.assertLess(exported, source.index('supervise codex'))
        self.assertIn('-c "$TEMP_ENV"', source)
        self.assertIn('export TMPPREFIX="$ROUND_TMP/zsh"', source)
        self.assertNotIn('export HOME=', source)
        self.assertIn('shell_environment_policy.inherit="core"', source)

    def test_ascii_paths_fallback_and_round_identity(self):
        ascii_root = '/Users/test/AgentWork'
        self.assertEqual(isolation.round_tmp(ascii_root, ascii_root + '/交付/T/r1'),
                         Path(ascii_root + '/tmp/T/r1'))
        result = isolation.round_tmp(self.root, self.run)
        self.assertTrue(str(result).isascii())
        self.assertEqual(result.parent, Path('/private/tmp'))
        for root, task, run in ((self.root + '-other', 'TASK', 'r1'),
                               (self.root, 'TASK', 'r2'), (self.root, 'TASK-r1', 'r1')):
            self.assertNotEqual(result, isolation.round_tmp(root, root + '/交付/' + task + '/' + run))
        for task in ('中文', 'with space', 'bad.name'):
            with self.assertRaises(ValueError):
                isolation.round_tmp(self.root, self.root + '/交付/' + task + '/r1')

    def test_opencode_home_is_created_privately_without_changing_secret_home(self):
        with mock.patch('frclyo.execution.exclusive_directory') as create:
            isolation.prepare_opencode_tmp(self.root, self.run, self.runtime)
        self.assertEqual(create.call_args_list[-1], mock.call(Path(self.runtime) / 'home'))
        text = self.profile()
        self.assertIn(json.dumps(self.home + '/.ssh', ensure_ascii=False), text)
        self.assertNotIn(json.dumps(self.runtime + '/home/.ssh', ensure_ascii=False), text)

    def test_opencode_supervisor_keeps_only_new_runtime_variables(self):
        from frclyo.worker import worker_environment
        values = isolation.temp_environment(self.root, self.run)
        result = worker_environment(dict(values, HOME=self.runtime + '/home', ZDOTDIR=self.runtime + '/home',
                                         ANOTHER_KEY='forbidden', HTTPS_PROXY='forbidden'))
        self.assertEqual({key: result[key] for key in values}, values)
        self.assertNotIn('forbidden', repr(result))
        self.assertEqual(result['HOME'], self.runtime + '/home')
        self.assertEqual(result['ZDOTDIR'], result['HOME'])
        script = (Path(__file__).resolve().parents[1] / 'opencode-worker.sh').read_text()
        self.assertLess(script.index('seatbelt(home,'), script.index('export HOME="$RT/home"'))

    def test_invalid_round_rejected(self):
        with self.assertRaises(ValueError):
            isolation.codex_tmp(self.root, self.root + '/交付/TASK')


if __name__ == '__main__':
    unittest.main()
