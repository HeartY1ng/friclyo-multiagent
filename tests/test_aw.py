# 跨模块离线测试：假 HOME、假 CLI、假工人，不调用模型、钥匙串或 launchd。
import contextlib
import importlib.machinery
import importlib.util
import io
import errno
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1]
CHOICE = {'worker': 'codex', 'model': 'offline-model', 'level': 'medium'}


def load_aw(path):
    """加载 bash/Python 双入口；不运行 main，方便在公共边界注入离线替身。"""
    loader = importlib.machinery.SourceFileLoader('aw_test_module', str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def snapshot(home):
    """同时比较目录、普通文件字节和链接目标，不能只数文件个数。"""
    result = {}
    for path in home.rglob('*'):
        relative = str(path.relative_to(home))
        result[relative] = ('link', os.readlink(path)) if path.is_symlink() else ('directory',) if path.is_dir() else ('file', path.read_bytes())
    return result


class AwTests(unittest.TestCase):
    """真实进程用于工人执行；公共库使用真实实现，系统命令和模型执行使用离线替身。"""
    def setUp(self):
        # 临时文件都留在本轮交付树，测试完成自动清理。
        self.temp = tempfile.TemporaryDirectory(prefix='.aw-test-', dir=SOURCE)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.home, self.repo = self.base / 'home', self.base / 'repo space & 中文'
        self.home.mkdir()
        self.repo.mkdir()
        for name in ('install.sh', 'uninstall.sh', 'bin', 'demo', 'tests'):
            source, target = SOURCE / name, self.repo / name
            shutil.copytree(source, target) if source.is_dir() else shutil.copy2(source, target)
        fixture = self.repo / 'tests/fixtures'
        for name in ('codex-worker.sh', 'opencode-worker.sh'):
            shutil.copy2(fixture / 'fake_worker.py', self.repo / 'bin' / name)
            (self.repo / 'bin' / name).chmod(0o755)
        shutil.copytree(SOURCE / 'workers', self.repo / 'workers')
        for name in ('install.sh', 'uninstall.sh', 'bin/aw'):
            (self.repo / name).chmod(0o755)
        skill = self.repo / 'adapters/claude/SKILL.md'
        skill.parent.mkdir(parents=True)
        shutil.copy2(SOURCE / 'adapters/claude/SKILL.md', skill)
        # 看板替身只监听临时端口；开浏览器用假 open，不能操作真实桌面。
        self.port = 18789
        board = self.repo / 'board/board.py'
        board.parent.mkdir()
        board.write_text('# 离线替身不绑定 socket，端口状态由测试注入。\n')
        self.fakebin = self.base / 'fakebin'
        self.fakebin.mkdir()
        for name, body in (('codex', 'echo "codex-cli 0.0-test"\n'), ('open', 'exit 0\n'), ('launchctl', 'exit 99\n')):
            script = self.fakebin / name
            script.write_text('#!/bin/bash\n' + body)
            script.chmod(0o755)
        shutil.copy2(fixture / 'security.py', self.fakebin / 'security')
        (self.fakebin / 'security').chmod(0o755)
        self.cfg = self.base / 'config.json'
        self.cfg.write_text(json.dumps({'board_port': self.port}), encoding='utf-8')
        self.env = mock.patch.dict(os.environ, {'HOME': str(self.home), 'FRCLYO_HOME': str(self.home / 'work'), 'FRCLYO_CONFIG': str(self.cfg),
                                   'FRCLYO_LAUNCHD_DRY_RUN': '1', 'PYTHONDONTWRITEBYTECODE': '1',
                                   'PATH': str(self.fakebin) + ':/usr/bin:/bin:/usr/sbin:/sbin'})
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop('CODEX_HOME', None)
        self.old_path = list(sys.path)
        self.addCleanup(lambda: sys.path.__setitem__(slice(None), self.old_path))
        for name in list(sys.modules):
            if name == 'frclyo' or name.startswith('frclyo.'):
                del sys.modules[name]
        self.aw = load_aw(self.repo / 'bin/aw')
        port_patch = mock.patch.object(self.aw, 'port_open', return_value=True)
        port_patch.start()
        self.addCleanup(port_patch.stop)
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.output)
        self.redirect.__enter__()
        self.addCleanup(lambda: self.redirect.__exit__(None, None, None))

    def test_public_subcommand_help_has_no_side_effects(self):
        before = snapshot(self.home)
        for command in ('key', '密钥', '钥匙', 'board', '看板', 'run', '运行',
                        'doctor', '自检', 'demo', '演示', 'calibrate', '校准', 'uninstall', '卸载'):
            with self.subTest(command=command):
                result = self.terminal(command, '--help')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('用法', result.stdout)
        self.assertEqual(snapshot(self.home), before)

    def test_runtime_failure_and_usage_exit_codes(self):
        self.cfg.write_text('{broken-json')
        for command in ('key', '密钥', 'board', '看板'):
            result = self.terminal(command, 'list' if command in ('key', '密钥') else 'status')
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn('配置读取失败', result.stderr)
        for args in (['unknown'], ['key'], ['board', 'unknown'], ['run'],
                     ['doctor', '--unknown'], ['calibrate', '--yes', '--no-input']):
            self.assertEqual(self.terminal(*args).returncode, 2, args)

    def test_help_does_not_validate_data_root(self):
        root = self.home / 'work'
        (root / '.git').mkdir(parents=True)
        for args in (['help'], ['帮助'], ['--help'], ['doctor', '--help'], ['ledger', '--help']):
            result = self.terminal(*args)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('Traceback', result.stderr)
        result = self.terminal('key', 'list')
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('Traceback', result.stderr)

    def test_ledger_help_and_source_usage_through_aliases(self):
        for command, action in (('ledger', 'new'), ('账本', '开卡')):
            for tail, expected in ((['--help'], 0), ([action, '--help'], 0), ([action], 2)):
                result = self.terminal(command, *tail)
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assertIn('aw ' + command, result.stdout + result.stderr)
                self.assertNotIn('usage: -c', result.stdout + result.stderr)
            for source in ([], ['--来源', '中文'], ['--来源', 'cx']):
                with mock.patch.dict(os.environ, AGENT_BRAIN=''):
                    result = self.terminal(command, action, '--项目', '项目', '--标题', '标题', *source)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn('指定两个大写字母的来源', result.stderr)
                self.assertFalse((self.home / 'work/账本.jsonl').exists())

    def test_doctor_login_failure_is_not_a_usable_candidate(self):
        actual = self.aw.capture
        for failure in (1, self.aw.UserError('fixture timeout')):
            def capture(argv, **kwargs):
                if list(argv) == ['codex', 'login', 'status']:
                    if isinstance(failure, Exception):
                        raise failure
                    return subprocess.CompletedProcess(argv, failure, b'', b'')
                return actual(argv, **kwargs)
            with mock.patch.object(self.aw, 'capture', side_effect=capture), \
                 mock.patch.object(self.aw, 'model_inventory', return_value=[CHOICE]):
                self.assertEqual(self.aw.doctor([]), 1)
            self.assertIn('codex login', self.output.getvalue())
        # 另一个工人可用时，未登录的 Codex 不阻止离线自检成功。
        other = dict(CHOICE, worker='opencode')
        with mock.patch.object(self.aw, 'tools_report', return_value={'opencode': 'fixture'}), \
             mock.patch.object(self.aw, 'model_inventory', return_value=[CHOICE, other]):
            self.assertEqual(self.aw.doctor([]), 0)

    def test_calibrate_and_live_interrupt_close_open_cards(self):
        for live in (False, True):
            for boundary in ('prepare_worker', 'invoke_with_fallback'):
                with self.subTest(live=live, boundary=boundary), \
                     mock.patch.object(self.aw, 'model_inventory', return_value=[CHOICE]), \
                     mock.patch.object(self.aw.runtime('models'), 'rank', side_effect=lambda rows: list(rows)), \
                     mock.patch.object(self.aw, boundary, side_effect=KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        if live:
                            self.aw.doctor(['--live', '--yes'])
                        else:
                            self.aw.calibrate(['--yes'])
                cards = self.aw.runtime('ledger').fold(self.events())
                self.assertTrue(cards)
                self.assertTrue(all(card['状态'] == '卡住' and card['原因'] == '用户中断'
                                    for card in cards.values()), cards)

    def test_failed_write_does_not_poison_reinstall(self):
        aw = self.aw
        aw.journal_begin()
        destination = aw.HOME / 'Library/LaunchAgents/com.frclyo.board.plist'
        data = b'complete installation fixture'
        # 在字节流层注入短写后 ENOSPC，既覆盖当前 Path.open，
        # 也覆盖修复时常用的 tempfile + os.fdopen 原子发布写法。
        original_open, original_fdopen = io.open, os.fdopen

        class FailAfterPrefix:
            def __init__(self, stream):
                self.stream = stream
            def __getattr__(self, name):
                return getattr(self.stream, name)
            def __enter__(self):
                return self
            def write(self, value):
                self.stream.write(value[:8])
                self.stream.flush()
                raise OSError(errno.ENOSPC, 'injected disk full')
            def __exit__(self, *args):
                self.stream.close()

        def wrap(opener):
            def opened(file, mode='r', *args, **kwargs):
                stream = opener(file, mode, *args, **kwargs)
                return FailAfterPrefix(stream) if 'b' in mode and ('w' in mode or 'x' in mode) else stream
            return opened

        with mock.patch.object(io, 'open', wrap(original_open)), mock.patch.object(os, 'fdopen', wrap(original_fdopen)):
            with self.assertRaises(OSError) as raised:
                aw.install_file(destination, data)
            self.assertEqual(raised.exception.errno, errno.ENOSPC)
        leftover = destination.read_bytes() if destination.exists() else None
        self.assertIsNone(leftover)
        self.assertEqual(list(destination.parent.iterdir()), [])
        try:
            aw.install_file(destination, data)
            retry_error = None
        except aw.UserError as exc:
            retry_error = str(exc).split('：', 1)[0]
        output = io.StringIO()
        with mock.patch.object(aw, 'path_of', return_value=self.base / 'data'), contextlib.redirect_stdout(output):
            uninstall_rc = aw.uninstall(['--yes'])
        print('partial=%r; retry_error=%r; uninstall_rc=%d; retained=%s' % (leftover, retry_error, uninstall_rc, destination.exists()), flush=True)
        self.assertIsNone(retry_error, '首次短写留下的半文件阻止恢复后的正常重装')
        self.assertEqual(uninstall_rc, 0)
        self.assertFalse(destination.exists())

    def test_unicode_install_journal_roundtrip(self):
        self.aw.journal_begin()
        for separator in ('\u2028', '\u2029', '\x85'):
            target = self.base / ('源码' + separator + '副本')
            target.write_text('fixture')
            self.aw.install_file(self.home / ('aw' + separator), target=target)
        before = self.aw.JOURNAL.read_bytes()
        expected = [json.loads(line) for line in before.decode().split('\n') if line]
        self.assertEqual(self.aw.read_journal(), expected)
        self.assertEqual(self.aw.JOURNAL.read_bytes(), before)
        with self.aw.JOURNAL.open('ab') as stream:
            stream.write(b'\n')
        with self.assertRaises(self.aw.UserError):
            self.aw.read_journal()

    def test_install_fsync_failure_removes_stage(self):
        aw = self.aw
        aw.journal_begin()
        destination = self.home / 'installed'
        real_fsync = os.fsync
        def fail_stage(fd):
            # 安装记录正常落盘；仅对暂存安装文件注入故障。
            if os.fstat(fd).st_ino != aw.JOURNAL.stat().st_ino:
                raise OSError(errno.ENOSPC, 'injected fsync failure')
            return real_fsync(fd)
        with mock.patch.object(aw.os, 'fsync', side_effect=fail_stage):
            with self.assertRaises(OSError):
                aw.install_file(destination, b'ours')
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.home.glob('.installed.*')), [])

    def test_install_atomic_publish_failure_and_modes(self):
        aw = self.aw
        aw.journal_begin()
        destination = self.home / 'installed'
        real_link = os.link
        def competing_link(source, target):
            destination.write_bytes(b'other owner')
            return real_link(source, target)
        with mock.patch.object(aw.os, 'link', side_effect=competing_link):
            with self.assertRaises(FileExistsError):
                aw.install_file(destination, b'ours')
        self.assertEqual(destination.read_bytes(), b'other owner')
        self.assertEqual(list(self.home.glob('.installed.*')), [])
        destination.unlink()
        for category, mode in (('config', 0o600), ('install', 0o644)):
            aw.install_file(destination, b'ours', category=category)
            self.assertEqual(destination.stat().st_mode & 0o777, mode)
            self.assertFalse(aw.install_file(destination, b'ours', category=category))
            destination.unlink()

    def terminal(self, *args, input=''):
        """从真实 bash 启动，检查符号链接、参数及 stdin 都没有被入口破坏。"""
        return subprocess.run(['/bin/bash', str(self.repo / 'bin/aw'), *args], input=input,
                              text=True, capture_output=True, timeout=20)

    def events(self):
        path = self.home / 'work/账本.jsonl'
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]

    def run_demo(self, bad=''):
        """模型候选在内存中固定，三关、派活和账本都跑真实实现。"""
        with mock.patch.object(self.aw, 'model_inventory', return_value=[CHOICE]), mock.patch.dict(os.environ, {'FAKE_BAD': bad, 'FAKE_REQUIRE_PARALLEL': '1'}):
            return self.aw.demo(['--yes'])

    def test_bash_entry_and_chinese_help(self):
        result = self.terminal('帮助')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('三关验收', result.stdout)
        self.assertIn('[--框架 输入|交付|路径] [--不套沙箱]', result.stdout)
        self.assertIn('--framework、--no-sandbox', result.stdout)

    def test_install_doctor_uninstall_existing_config(self):
        """已有配置场景完整撤销，账本与配置使用生产实现。"""
        before = snapshot(self.home)
        self.assertEqual(self.aw.install(['--yes']), 0)
        self.assertTrue((self.home / '.local/bin/aw').is_symlink())
        skill = self.home / '.claude/skills/frclyo-brain/SKILL.md'
        self.assertIn(str(self.repo), skill.read_text())
        self.assertNotIn('{{REPO}}', skill.read_text())
        with mock.patch.object(self.aw, 'model_inventory', return_value=[CHOICE]):
            self.assertEqual(self.aw.doctor([]), 0)
        self.assertEqual(self.aw.uninstall(['--yes']), 0)
        self.assertTrue(self.cfg.exists())
        self.assertTrue(self.aw.JOURNAL.exists())
        self.assertFalse((self.home / '.local/bin/aw').exists())

    def test_b113_versioned_install_uses_stable_skill_and_manual_paths(self):
        stable = self.home / 'work/框架'
        version = stable.parent / '框架-版本' / ('a' * 40)
        version.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.repo, version)
        self.aw = load_aw(version / 'bin/aw')
        stable.symlink_to(version, target_is_directory=True)
        (version / '大脑手册.md').write_text('old manual')
        self.assertEqual(self.aw.install(['--yes']), 0)
        content = (self.home / '.claude/skills/frclyo-brain/SKILL.md').read_text()
        self.assertIn(str(stable / '大脑手册.md'), content)
        self.assertNotIn(str(version), content)
        self.assertNotIn('{{REPO}}', content)
        self.assertEqual(os.readlink(self.home / '.local/bin/aw'), str(stable / 'bin/aw'))
        replacement = version.parent / ('b' * 40)
        replacement.mkdir()
        (replacement / '大脑手册.md').write_text('new manual')
        stable.unlink()
        stable.symlink_to(replacement, target_is_directory=True)
        self.assertEqual((stable / '大脑手册.md').read_text(), 'new manual')

    def test_clean_install_and_uninstall_real_config(self):
        self.cfg.unlink()
        before = snapshot(self.home)
        self.assertEqual(self.aw.install(['--yes']), 0)
        self.assertEqual(self.cfg.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.aw.uninstall(['--yes']), 0)
        self.assertTrue(self.cfg.exists())
        self.assertTrue(self.aw.JOURNAL.exists())
        self.assertFalse((self.home / '.local/bin/aw').exists())

    def test_clean_home_install_discover_demo_board_and_uninstall(self):
        """全链路使用真实公共库，只有钥匙串、模型 CLI 与系统服务替换为离线边界。"""
        self.cfg.unlink()
        opencode = self.fakebin / 'opencode'
        opencode.write_text('#!/bin/bash\n# 离线版本查询，不启动模型。\necho opencode-0.0-test\n')
        opencode.chmod(0o755)
        # 预置的候选模型属于用户已有 CLI；卸载不能删这些用户资源。
        cache = self.home / '.codex/models_cache.json'
        cache.parent.mkdir()
        cache.write_text(json.dumps({'models': [{'slug': 'offline-model', 'visibility': 'list',
                           'supported_reasoning_levels': [{'effort': 'medium'}]}]}))
        before = snapshot(self.home)
        self.assertEqual(self.aw.install(['--yes']), 0)
        result = self.terminal('key', 'add', 'deepseek', input='y\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.aw.doctor([]), 0)
        pool = self.aw.model_inventory()
        self.assertEqual(pool[0]['source'], 'models_cache')
        self.assertTrue(any(row['source'] == 'worker_config' and row['provider'] == 'deepseek' for row in pool))
        self.assertEqual(self.aw.demo(['--yes']), 0)
        # 加载真实看板的数据汇总，检查完成卡和健康信息，不需要监听端口。
        with mock.patch.dict(os.environ, {'BOARD_NO_BACKGROUND': '1', 'BOARD_BALANCE': '0'}):
            spec = importlib.util.spec_from_file_location('integrated_board', SOURCE / 'board/board.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.refresh_health('audit')
            state = module.build_state()
        self.assertEqual(len(state['最近完成']), 3)
        self.assertEqual(state['健康']['日审']['核对卡数'], 3)
        self.assertEqual(state['健康']['日审']['问题'], [])
        self.assertEqual(self.aw.uninstall(['--yes']), 0)
        self.assertTrue(self.cfg.exists())
        self.assertEqual(self.aw.runtime('keys').list_providers(), [])
        self.assertTrue(self.aw.JOURNAL.exists())
        self.assertFalse((self.home / '.local/bin/aw').exists())

    def test_no_input_does_not_install_optional_files(self):
        self.cfg.unlink()
        self.assertEqual(self.aw.install(['--no-input']), 0)
        self.assertFalse((self.home / '.local').exists())
        self.assertTrue((self.home / 'work/任务卡').exists())
        self.assertFalse(self.cfg.exists())

    def test_fake_home_codex_home_fallback_key_demo_and_uninstall(self):
        """复现本机六条问题：独立 Codex HOME、回退 key、DM、B 工人和配置清理。"""
        self.cfg = self.home / '.frclyo/config.json'
        os.environ['FRCLYO_CONFIG'] = str(self.cfg)
        codex_home = self.base / 'real Codex 中文'
        codex_home.mkdir()
        cache = codex_home / 'models_cache.json'
        cache.write_text(json.dumps({'models': [{'slug': 'offline-model', 'visibility': 'list',
                         'supported_reasoning_levels': [{'effort': 'medium'}]}]}))
        os.environ['CODEX_HOME'] = str(codex_home)
        fallback = self.base / 'legacy.env'
        fallback.write_text('DEEPSEEK_API_KEY=FALLBACK-SECRET-SENTINEL\n')
        for name in ('opencode', 'claude'):
            script = self.fakebin / name
            script.write_text('#!/bin/bash\necho offline-cli\n')
            script.chmod(0o755)
        before = snapshot(self.home)
        self.assertEqual(self.aw.install(['--yes']), 0)
        plist = self.home / 'Library/LaunchAgents/com.frclyo.board.plist'
        self.assertEqual(self.aw.plistlib.loads(plist.read_bytes())['EnvironmentVariables']['CODEX_HOME'], str(codex_home))
        current = self.aw.config()
        current.update(key_env_file=str(fallback), board_port=19789)
        self.aw.runtime('config').save(current)
        with mock.patch.object(self.aw, 'port_open', return_value=False):
            self.assertEqual(self.aw.doctor([]), 0)
        self.assertTrue(all('-w' not in call for call in self.security_calls()))
        self.assertIn('⚠️ 看板端口 19789', self.output.getvalue())
        self.assertIn('key deepseek：回退文件：' + str(fallback), self.output.getvalue())
        self.assertNotIn('❌', self.output.getvalue())
        listed = self.terminal('key', 'list')
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(listed.stdout.splitlines(), ['deepseek：回退文件：' + str(fallback),
                         'moonshotai-cn：无可用 key', 'xiaomi：无可用 key', 'zai-coding-plan：无可用 key', 'zhipuai：无可用 key'])
        self.assertEqual(self.aw.demo(['--yes']), 0)
        events = [json.loads(line) for line in self.aw.path_of('ledger_file').read_text().splitlines()]
        opened = [row for row in events if row['动作'] == '开卡']
        self.assertEqual(len(opened), 3)
        self.assertTrue(all(row['任务'].startswith('DM-') for row in opened))
        performance = [json.loads(line) for line in self.aw.path_of('perf_file').read_text().splitlines()]
        self.assertEqual(performance[1]['工人'], 'opencode')
        self.assertTrue(performance[1]['模型'].startswith('deepseek/'))
        self.assertTrue(all(row['通过验收'] for row in performance))
        self.assertEqual(self.aw.uninstall(['--yes']), 0)
        self.assertTrue(self.cfg.exists())
        self.assertTrue(self.aw.path_of('ledger_file').exists())
        self.assertTrue(cache.exists())
        self.assertTrue(fallback.exists())
        self.assertNotIn('FALLBACK-SECRET-SENTINEL', self.output.getvalue() + listed.stdout + listed.stderr)

    def test_install_is_idempotent(self):
        self.aw.install(['--yes'])
        first = snapshot(self.home)
        self.aw.install(['--yes'])
        second = snapshot(self.home)
        first.pop('work/配置/安装记录.jsonl')
        second.pop('work/配置/安装记录.jsonl')
        self.assertEqual(first, second)

    def test_dry_run_has_no_effect(self):
        self.aw.install(['--yes'])
        before = snapshot(self.home)
        self.assertEqual(self.aw.uninstall(['--dry-run']), 0)
        self.assertEqual(before, snapshot(self.home))

    def test_default_uninstall_preserves_data(self):
        self.aw.install(['--yes'])
        user_data = self.home / 'work/keep.txt'
        user_data.write_text('user data')
        with mock.patch('builtins.input', side_effect=['y', '', '']):
            self.assertEqual(self.aw.uninstall([]), 0)
        self.assertTrue(user_data.exists())
        self.assertTrue(self.cfg.exists())
        self.assertFalse((self.home / '.local/bin/aw').exists())

    def test_uninstall_cannot_delete_data_via_bad_journal_category(self):
        # 损坏或伪造的安装分类不能绕过“无显式删除则保留数据”的边界。
        self.aw.install(['--yes'])
        data = self.home / 'work/keep.txt'
        data.write_text('SENTINEL')
        for category in ('unknown', 'install'):
            self.aw.record('plan', id='bad-' + category, kind='file', path=str(data),
                           category=category, expected=self.aw.fingerprint(data))
        self.assertEqual(self.aw.uninstall(['--yes']), 1)
        self.assertEqual(data.read_text(), 'SENTINEL')

    def test_delete_data_requires_exact_path_even_with_yes(self):
        # 显式删除也不能由 --yes 代替完整路径确认；错误、缩写和 EOF 都保留。
        self.aw.install(['--yes'])
        data = self.home / 'work/keep.txt'
        data.write_text('SENTINEL')
        for answer in ('y', '~/work', str(data.parent) + ' ', ''):
            with mock.patch('builtins.input', return_value=answer):
                self.assertEqual(self.aw.uninstall(['--yes', '--delete-data']), 0)
            self.assertEqual(data.read_text(), 'SENTINEL')
        with mock.patch('builtins.input', return_value=str(data.parent)):
            self.assertEqual(self.aw.uninstall(['--yes', '--delete-data']), 0)
        self.assertFalse(data.parent.exists())
        # 测试专用外部配置不属于数据根，不得顺手删掉。
        self.assertTrue(self.cfg.exists())

    def test_delete_data_no_input_and_dry_run_are_inert(self):
        self.aw.install(['--yes'])
        data = self.home / 'work/keep.txt'
        data.write_text('SENTINEL')
        before = snapshot(self.home)
        with mock.patch('builtins.input', side_effect=AssertionError('禁止索取输入')):
            self.assertEqual(self.aw.uninstall(['--delete-data', '--no-input']), 0)
            self.assertEqual(self.aw.uninstall(['--delete-data', '--dry-run', '--yes']), 0)
        self.assertEqual(snapshot(self.home), before)

    def test_both_uninstall_entries_preserve_then_delete_with_full_path(self):
        # 两个公开入口都经过真实 shell 与 stdin，不能只验证内部 Python 方法。
        self.aw.install(['--yes'])
        data = self.home / 'work/keep.txt'
        data.write_text('SENTINEL')
        result = subprocess.run(['/bin/bash', str(self.repo / 'uninstall.sh'), '--yes'],
                                text=True, input='', capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(data.exists())
        result = self.terminal('uninstall', '--delete-data', '--yes', input=str(data.parent) + '\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(data.parent.exists())

    def test_installer_contract_layout_config_journal_and_doctor_migration(self):
        # 新配置没有测试覆盖时写在根内，重复安装保持旧字节和已有数据。
        os.environ.pop('FRCLYO_CONFIG')
        self.aw.install(['--yes'])
        root = self.home / 'work'
        for name in ('框架', '框架-开发', '配置', '任务卡', '交付', '素材', '项目',
                     '运行时/opencode', '台账/备份', '台账/归档'):
            self.assertTrue((root / name).is_dir())
        cfg = root / '配置/config.json'
        self.assertTrue((root / '配置/安装记录.jsonl').is_file())
        original = cfg.read_bytes()
        self.assertNotIn('work_dir', json.loads(original))
        self.aw.install(['--yes'])
        self.assertEqual(cfg.read_bytes(), original)
        plist = self.home / 'Library/LaunchAgents/com.frclyo.board.plist'
        self.assertEqual(self.aw.plistlib.loads(plist.read_bytes())['EnvironmentVariables']['FRCLYO_HOME'], str(root))
        legacy = self.home / '.frclyo/config.json'
        legacy.parent.mkdir()
        legacy.write_bytes(original)
        cfg.unlink()
        with mock.patch.object(self.aw, 'model_inventory', return_value=[CHOICE]):
            self.assertEqual(self.aw.doctor([]), 0)
        self.assertIn('请迁移', self.output.getvalue())
        self.aw.install(['--yes'])
        self.assertFalse(cfg.exists())
        self.assertEqual(legacy.read_bytes(), original)

    def test_modified_skill_is_preserved(self):
        self.aw.install(['--yes'])
        skill = self.home / '.claude/skills/frclyo-brain/SKILL.md'
        skill.write_text('user changes')
        self.assertEqual(self.aw.uninstall(['--yes']), 1)
        self.assertEqual(skill.read_text(), 'user changes')
        self.assertTrue(self.aw.JOURNAL.exists())

    def test_uninstall_preserves_modified_config_and_journal(self):
        self.cfg = self.home / '.frclyo/config.json'
        os.environ['FRCLYO_CONFIG'] = str(self.cfg)
        self.aw.install(['--yes'])
        current = self.aw.config()
        current['board_port'] += 1
        self.aw.runtime('config').save(current)
        self.assertEqual(self.aw.uninstall(['--yes']), 0)
        self.assertTrue(self.cfg.exists())
        self.assertTrue(self.aw.JOURNAL.exists())
        self.assertTrue(self.aw.STATE.exists())

    def test_uninstall_preserves_preexisting_config_and_unowned_files(self):
        self.cfg = self.home / '.frclyo/config.json'
        self.cfg.parent.mkdir()
        self.cfg.write_text(json.dumps({'work_dir': str(self.home / 'work')}))
        os.environ['FRCLYO_CONFIG'] = str(self.cfg)
        unowned = self.cfg.parent / 'user-notes.txt'
        unowned.write_text('keep-user-notes')
        cache = self.home / '.cache/opencode/models.json'
        cache.parent.mkdir(parents=True)
        cache.write_text('keep-opencode-cache')
        self.aw.install(['--yes'])
        self.assertEqual(self.aw.uninstall(['--yes']), 0)
        self.assertTrue(self.cfg.exists())
        self.assertTrue(self.aw.JOURNAL.exists())
        self.assertEqual(unowned.read_text(), 'keep-user-notes')
        self.assertEqual(cache.read_text(), 'keep-opencode-cache')

    def test_uninstall_keeps_modified_config_without_delete_confirmation(self):
        self.cfg.unlink()
        self.aw.install(['--yes'])
        current = self.aw.config()
        current['board_port'] += 1
        self.aw.runtime('config').save(current)
        before = self.cfg.read_bytes()
        with mock.patch('builtins.input', side_effect=['y', '', '']):
            self.assertEqual(self.aw.uninstall([]), 0)
        self.assertEqual(self.cfg.read_bytes(), before)
        self.assertTrue(self.aw.JOURNAL.exists())

    def test_preexisting_link_is_not_overwritten(self):
        link = self.home / '.local/bin/aw'
        link.parent.mkdir(parents=True)
        link.symlink_to('elsewhere')
        with self.assertRaisesRegex(self.aw.UserError, '未覆盖'):
            self.aw.install(['--yes'])
        self.assertEqual(os.readlink(link), 'elsewhere')

    def test_symlink_parent_outside_home_rejected(self):
        (self.home / '.local').symlink_to(self.base)
        with self.assertRaisesRegex(self.aw.UserError, 'HOME 外'):
            self.aw.install_file(self.home / '.local/bin/aw', target=self.repo / 'bin/aw')

    def test_broken_journal_stops_uninstall(self):
        self.aw.journal_begin()
        self.aw.JOURNAL.write_text('{broken')
        before = snapshot(self.home)
        with self.assertRaisesRegex(self.aw.UserError, '记录损坏'):
            self.aw.uninstall(['--yes'])
        self.assertEqual(before, snapshot(self.home))

    def test_demo_two_codex_workers_pass(self):
        self.assertEqual(self.run_demo(), 0)
        rows = self.events()
        self.assertEqual(sum(row['动作'] == '开卡' for row in rows), 3)
        self.assertEqual(sum(row['动作'] == '完成' for row in rows), 3)
        self.assertEqual(sum(bool(row.get('父任务')) for row in rows), 2)
        self.assertIn('第三关', self.output.getvalue())
        self.assertEqual(self.aw.runtime('audit').check()['问题'], [])
        self.assertEqual(len(self.aw.path_of('perf_file').read_text().splitlines()), 2)

    def test_demo_prefers_opencode_for_b(self):
        other = {'worker': 'opencode', 'model': 'deepseek/offline', 'level': ''}
        self.assertEqual(self.aw.choose_pair([CHOICE, other]), (CHOICE, other))
        with mock.patch.object(self.aw, 'model_inventory', return_value=[CHOICE, other]):
            self.assertEqual(self.aw.demo(['--yes']), 0)

    def test_demo_uses_dm_without_consuming_cx_sequence(self):
        first = self.aw.ledger('开卡', '--项目', 'test', '--标题', 'before demo', '--来源', 'CX')
        self.assertEqual(self.run_demo(), 0)
        opened = [row for row in self.events() if row['动作'] == '开卡' and row['任务'] != first]
        self.assertEqual(len(opened), 3)
        self.assertTrue(all(row['任务'].startswith('DM-') for row in opened))
        last = self.aw.ledger('开卡', '--项目', 'test', '--标题', 'after demo', '--来源', 'CX')
        self.assertEqual(int(last.rsplit('-', 1)[1]), int(first.rsplit('-', 1)[1]) + 1)

    def test_demo_board_reuses_configured_listener_without_stopping_it(self):
        with mock.patch.object(self.aw, 'port_open', return_value=True) as port, \
                mock.patch.object(self.aw.subprocess, 'Popen') as start, \
                mock.patch.object(self.aw.os, 'killpg') as kill, \
                mock.patch.object(self.aw, 'board') as board:
            with self.aw.demo_board():
                pass
        start.assert_not_called()
        kill.assert_not_called()
        port.assert_called_once_with(self.port)
        board.assert_called_once_with(['open'])
        self.assertIn('复用已有看板：http://127.0.0.1:' + str(self.port), self.output.getvalue())
        self.assertNotIn('临时看板已结束', self.output.getvalue())

    def test_demo_board_starts_and_cleans_temporary_board_even_on_failure(self):
        child = mock.Mock(pid=12345)
        child.poll.return_value = None
        with mock.patch.object(self.aw, 'port_open', side_effect=[False, True]), \
                mock.patch.object(self.aw.subprocess, 'Popen', return_value=child) as start, \
                mock.patch.object(self.aw.os, 'killpg') as kill, \
                mock.patch.object(self.aw, 'board') as board:
            with self.assertRaisesRegex(RuntimeError, 'demo failed'):
                with self.aw.demo_board():
                    raise RuntimeError('demo failed')
        self.assertEqual(start.call_count, 1)
        self.assertEqual(start.call_args.args[0], [sys.executable, str(self.repo / 'board/board.py')])
        board.assert_called_once_with(['open'])
        kill.assert_called_once_with(child.pid, self.aw.signal.SIGTERM)
        child.wait.assert_called_once_with(timeout=3)
        self.assertIn('aw board start', self.output.getvalue())

    def test_demo_board_already_exited_still_prints_start_hint(self):
        child = mock.Mock()
        child.poll.return_value = 1
        with mock.patch.object(self.aw, 'port_open', return_value=False), \
                mock.patch.object(self.aw.subprocess, 'Popen', return_value=child), \
                mock.patch.object(self.aw.os, 'killpg') as kill, \
                mock.patch.object(self.aw, 'board'):
            with self.aw.demo_board():
                pass
        kill.assert_not_called()
        self.assertIn('aw board start', self.output.getvalue())

    def test_demo_has_clear_missing_worker_message(self):
        with self.assertRaisesRegex(self.aw.UserError, '没有可用工人'):
            self.aw.choose_pair([])

    def test_demo_declined_has_no_ledger(self):
        with mock.patch.object(self.aw, 'model_inventory', return_value=[CHOICE]):
            self.assertEqual(self.aw.demo(['--no-input']), 0)
        self.assertFalse((self.home / 'work').exists())

    def test_demo_bad_sample_blocks_all_cards(self):
        self.assert_demo_failure('sample')

    def test_demo_failed_tests_blocks_all_cards(self):
        self.assert_demo_failure('tests')

    def test_demo_zero_tests_is_failure(self):
        self.assert_demo_failure('zero-tests')

    def test_demo_bad_e2e_blocks_all_cards(self):
        self.assert_demo_failure('e2e')

    def test_demo_receipt_hash_failure(self):
        self.assert_demo_failure('receipt')

    def test_demo_missing_receipt_failure(self):
        self.assert_demo_failure('missing-receipt')

    def test_demo_worker_exit_failure(self):
        self.assert_demo_failure('exit')

    def assert_demo_failure(self, kind):
        with self.assertRaises(self.aw.UserError):
            self.run_demo(kind)
        rows = self.events()
        self.assertEqual(sum(row['动作'] == '卡住' for row in rows), 3)
        self.assertFalse(any(row['动作'] == '完成' for row in rows))

    def test_key_sources_without_values(self):
        result = self.terminal('密钥', '列表')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ['deepseek：无可用 key',
                         'moonshotai-cn：无可用 key', 'xiaomi：无可用 key', 'zai-coding-plan：无可用 key', 'zhipuai：无可用 key'])

    def configure_external_key(self):
        cfg = self.aw.config()
        cfg['keychain_entries'] = {'deepseek': {'service': 'my-deepseek', 'account': 'me'}}
        self.aw.runtime('config').save(cfg)
        return self.base / 'fake-keychain.json'

    def security_calls(self):
        path = self.base / 'fake-security-calls.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_external_source_priority_and_fallback_without_password_queries(self):
        chain = self.configure_external_key()
        external = {'service': 'my-deepseek', 'account': 'me'}
        default = {'service': 'frclyo-deepseek', 'account': 'frclyo'}
        fallback = self.base / 'fallback.env'
        fallback.write_text('DEEPSEEK_API_KEY="ENV-SENTINEL"\n')
        cfg = self.aw.config()
        cfg['key_env_file'] = str(fallback)
        self.aw.runtime('config').save(cfg)
        keys = self.aw.runtime('keys')
        for entries, expected in [([external, default], dict(kind='keychain', external=True, **external)),
                                  ([default], dict(kind='keychain', external=False, **default)),
                                  ([], dict(kind='env_file', path=str(fallback)))]:
            with self.subTest(expected=expected):
                chain.write_text(json.dumps(entries))
                (self.base / 'fake-security-calls.jsonl').unlink(missing_ok=True)
                self.assertEqual(keys.key_source('deepseek'), expected)
                with mock.patch.object(keys, 'get_key', side_effect=AssertionError('must not read key')):
                    self.assertEqual(keys.list_providers(), ['deepseek'])
                listed = self.terminal('key', 'list')
                self.assertEqual(listed.returncode, 0, listed.stderr)
                self.assertIn(keys.source_label(expected), listed.stdout)
                self.assertNotIn('SENTINEL', listed.stdout + listed.stderr)
                self.assertTrue(all('-w' not in call for call in self.security_calls()))
                (self.base / 'fake-security-calls.jsonl').unlink()
                self.assertEqual(keys.get_key('deepseek'), 'OFFLINE-SENTINEL' if entries else 'ENV-SENTINEL')
                calls = self.security_calls()
                self.assertEqual(calls[0], ['find-generic-password', '-s', 'my-deepseek', '-a', 'me', '-w'])
                self.assertEqual(len(calls), 1 if expected.get('external') else 2)
                if len(calls) == 2:
                    self.assertEqual(calls[1], ['find-generic-password', '-s', 'frclyo-deepseek', '-a', 'frclyo', '-w'])
        for content in ('', 'DEEPSEEK_API_KEY=\n', 'DEEPSEEK_API_KEY=""\n', "DEEPSEEK_API_KEY=''\n"):
            fallback.write_text(content)
            self.assertEqual(keys.key_source('deepseek'), {'kind': 'none'})
            self.assertIsNone(keys.get_key('deepseek'))
        chain.write_text(json.dumps([dict(external, account='someone-else')]))
        self.assertEqual(keys.key_source('deepseek'), {'kind': 'none'})

    def test_external_key_add_refused_and_unregistered_remove_refused(self):
        chain = self.configure_external_key()
        entries = [{'service': 'my-deepseek', 'account': 'me'},
                   {'service': 'frclyo-deepseek', 'account': 'frclyo'}]
        chain.write_text(json.dumps(entries))
        result = self.terminal('key', 'add', 'deepseek', input='y\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('my-deepseek（账户 me）', result.stdout + result.stderr)
        self.assertIn('keychain_entries', result.stdout + result.stderr)
        result = self.terminal('key', 'remove', 'deepseek', input='y\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(chain.read_text()), entries)
        self.assertFalse(self.aw.JOURNAL.exists())
        self.assertTrue(all(call[0] == 'find-generic-password' and '-w' not in call
                            for call in self.security_calls()))

    def test_key_add_migrates_fallback_and_records_default(self):
        fallback = self.base / 'fallback.env'
        fallback.write_text('XIAOMI_API_KEY=ENV-SENTINEL\n')
        cfg = self.aw.config()
        cfg['key_env_file'] = str(fallback)
        self.aw.runtime('config').save(cfg)
        result = self.terminal('key', 'add', 'xiaomi', input='y\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('当前从回退文件读取；存入钥匙串后优先用钥匙串', result.stdout)
        self.assertNotIn('SENTINEL', result.stdout + result.stderr)
        self.assertEqual(self.aw.runtime('keys').key_source('xiaomi')['kind'], 'keychain')
        self.assertEqual(fallback.read_text(), 'XIAOMI_API_KEY=ENV-SENTINEL\n')
        self.assertEqual([row['provider'] for row in self.aw.read_journal() if row.get('kind') == 'key'], ['xiaomi'])
        again = self.terminal('key', 'add', 'xiaomi', input='y\n')
        self.assertEqual(again.returncode, 0)
        self.assertIn('aw key remove xiaomi', again.stdout)
        self.assertEqual(sum(call[0] == 'add-generic-password' for call in self.security_calls()), 1)

    def test_zai_coding_plan_key_add_uses_fake_keychain(self):
        listed = self.terminal('key', 'list')
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn('zai-coding-plan：无可用 key', listed.stdout)
        fallback = self.base / 'fallback.env'
        fallback.write_text('ZHIPU_API_KEY=ENV-SENTINEL\n')
        cfg = self.aw.config()
        cfg['key_env_file'] = str(fallback)
        self.aw.runtime('config').save(cfg)
        result = self.terminal('key', 'add', 'zai-coding-plan', input='y\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('当前从回退文件读取；存入钥匙串后优先用钥匙串', result.stdout)
        self.assertNotIn('SENTINEL', result.stdout + result.stderr)
        self.assertEqual(self.aw.runtime('keys').key_source('zai-coding-plan')['kind'], 'keychain')
        self.assertEqual(fallback.read_text(), 'ZHIPU_API_KEY=ENV-SENTINEL\n')
        self.assertEqual([row['provider'] for row in self.aw.read_journal() if row.get('kind') == 'key'], ['zai-coding-plan'])
        again = self.terminal('key', 'add', 'zai-coding-plan', input='y\n')
        self.assertEqual(again.returncode, 0)
        self.assertIn('aw key remove zai-coding-plan', again.stdout)
        self.assertEqual(sum(call[0] == 'add-generic-password' for call in self.security_calls()), 1)

    def test_zhipuai_key_add_uses_fake_keychain(self):
        listed = self.terminal('key', 'list')
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn('zhipuai：无可用 key', listed.stdout)
        fallback = self.base / 'fallback.env'
        fallback.write_text('ZHIPU_API_KEY=ENV-SENTINEL\n')
        cfg = self.aw.config()
        cfg['key_env_file'] = str(fallback)
        self.aw.runtime('config').save(cfg)
        result = self.terminal('key', 'add', 'zhipuai', input='y\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('当前从回退文件读取；存入钥匙串后优先用钥匙串', result.stdout)
        self.assertNotIn('SENTINEL', result.stdout + result.stderr)
        self.assertEqual(self.aw.runtime('keys').key_source('zhipuai')['kind'], 'keychain')
        self.assertEqual(self.aw.runtime('keys').key_source('zhipuai')['service'], 'frclyo-zhipuai')
        self.assertEqual(self.aw.runtime('keys').key_source('zai-coding-plan')['kind'], 'env_file')
        self.assertEqual(fallback.read_text(), 'ZHIPU_API_KEY=ENV-SENTINEL\n')
        self.assertEqual([row['provider'] for row in self.aw.read_journal() if row.get('kind') == 'key'], ['zhipuai'])
        again = self.terminal('key', 'add', 'zhipuai', input='y\n')
        self.assertEqual(again.returncode, 0)
        self.assertIn('aw key remove zhipuai', again.stdout)
        self.assertEqual(sum(call[0] == 'add-generic-password' for call in self.security_calls()), 1)

    def test_unregistered_default_add_requests_manual_handling(self):
        chain = self.base / 'fake-keychain.json'
        original = [{'service': 'frclyo-deepseek', 'account': 'frclyo'}]
        chain.write_text(json.dumps(original))
        for revoked in (False, True):
            if revoked:
                self.aw.journal_begin()
                self.aw.record('plan', id='old', kind='key', provider='deepseek')
                self.aw.record('done', id='old')
                self.aw.record('undone', id='old')
            result = self.terminal('key', 'add', 'deepseek', input='y\n')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('这个条目不是本工具登记的', result.stderr)
            self.assertIn('security 命令手动处理', result.stderr)
            self.assertNotIn('aw key remove', result.stdout + result.stderr)
            self.assertEqual(json.loads(chain.read_text()), original)
        self.assertTrue(all(call[0] == 'find-generic-password' and '-w' not in call
                            for call in self.security_calls()))

    def test_external_read_51_does_not_use_default_or_file(self):
        chain = self.configure_external_key()
        chain.write_text(json.dumps([{'service': 'my-deepseek', 'account': 'me'},
                                     {'service': 'frclyo-deepseek', 'account': 'frclyo'}]))
        fallback = self.base / 'fallback.env'
        fallback.write_text('DEEPSEEK_API_KEY=ENV-SENTINEL\n')
        cfg = self.aw.config()
        cfg['key_env_file'] = str(fallback)
        self.aw.runtime('config').save(cfg)
        (self.base / 'fake-security-results.json').write_text(json.dumps([
            {'service': 'my-deepseek', 'account': 'me', 'read': True, 'code': 51}]))
        keys = self.aw.runtime('keys')
        self.assertEqual(keys.key_source('deepseek')['kind'], 'keychain')
        (self.base / 'fake-security-calls.jsonl').unlink()
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertIsNone(keys.get_key('deepseek'))
        self.assertIn('钥匙串读取失败', err.getvalue())
        self.assertNotIn('SENTINEL', err.getvalue())
        self.assertEqual(self.security_calls(), [
            ['find-generic-password', '-s', 'my-deepseek', '-a', 'me', '-w']])

    def test_doctor_checks_sources_and_models_without_password_reads(self):
        chain = self.configure_external_key()
        fallback = self.base / 'fallback.env'
        fallback.write_text('DEEPSEEK_API_KEY=ENV-SENTINEL\n')
        cfg = self.aw.config()
        cfg['key_env_file'] = str(fallback)
        self.aw.runtime('config').save(cfg)
        opencode = self.fakebin / 'opencode'
        opencode.write_text('#!/bin/bash\necho offline-cli\n')
        opencode.chmod(0o755)
        for entries in ([{'service': 'my-deepseek', 'account': 'me'}],
                        [{'service': 'frclyo-deepseek', 'account': 'frclyo'}], []):
            chain.write_text(json.dumps(entries))
            with mock.patch.object(self.aw.runtime('keys'), 'get_key', side_effect=AssertionError('must not read key')):
                self.assertEqual(self.aw.doctor([]), 0)
        self.assertTrue(self.security_calls())
        self.assertTrue(all('-w' not in call for call in self.security_calls()))
        self.assertNotIn('SENTINEL', self.output.getvalue())

    def test_unknown_source_lists_and_refuses_add(self):
        self.configure_external_key()
        for external in (True, False):
            service, account = ('my-deepseek', 'me') if external else ('frclyo-deepseek', 'frclyo')
            (self.base / 'fake-security-results.json').write_text(json.dumps([
                {'service': service, 'account': account, 'read': False, 'code': 36}]))
            listed = self.terminal('key', 'list')
            self.assertEqual(listed.returncode, 0)
            self.assertIn('deepseek：无法确认（钥匙串锁定或拒绝访问）', listed.stdout)
            added = self.terminal('key', 'add', 'deepseek', input='y\n')
            self.assertNotEqual(added.returncode, 0)
            self.assertIn('无法确认', added.stderr)
            if external:
                self.assertIn('keychain_entries', added.stderr)
            keys = self.aw.runtime('keys')
            replies = ([subprocess.CompletedProcess([], 44)] if not external else [])
            replies += [subprocess.TimeoutExpired('security', 10)]
            for action in ('list', 'add'):
                with mock.patch.object(keys.subprocess, 'run', side_effect=replies * (3 if action == 'list' else 1)) as run:
                    if action == 'list':
                        # 此用例只列这一家，避免把第二家的查询算成下一层。
                        with mock.patch.object(self.aw, 'config', return_value={'providers': {'deepseek': {}}}):
                            self.aw.key_command(['list'])
                        self.assertIn('无法确认', self.output.getvalue())
                    else:
                        with self.assertRaisesRegex(self.aw.UserError, '无法确认'), \
                                mock.patch.object(keys, 'set_key', side_effect=AssertionError('must not write')):
                            self.aw.key_command(['add', 'deepseek'])
                    self.assertTrue(all('-w' not in call.args[0] for call in run.call_args_list))
        self.assertFalse(self.aw.JOURNAL.exists())
        self.assertTrue(all(call[0] == 'find-generic-password' and '-w' not in call
                            for call in self.security_calls()))

    def test_remove_and_uninstall_preserve_external_and_unregistered_defaults(self):
        for action in ('remove', 'uninstall'):
            with self.subTest(action=action):
                self.cfg.write_text('{}')
                chain = self.base / 'fake-keychain.json'
                chain.write_text('[]')
                with mock.patch('builtins.input', return_value='y'):
                    self.assertEqual(self.aw.key_command(['add', 'deepseek']), 0)
                self.configure_external_key()
                remaining = [{'service': 'my-deepseek', 'account': 'me'},
                             {'service': 'frclyo-xiaomi', 'account': 'frclyo'}]
                chain.write_text(json.dumps([{'service': 'frclyo-deepseek', 'account': 'frclyo'}, *remaining]))
                (self.base / 'fake-security-calls.jsonl').unlink()
                with mock.patch('builtins.input', return_value='y'):
                    result = (self.aw.key_command(['remove', 'deepseek']) if action == 'remove'
                              else self.aw.uninstall(['--yes']))
                self.assertEqual(result, 0)
                self.assertEqual(json.loads(chain.read_text()), remaining)
                self.assertEqual(self.security_calls(), [
                    ['delete-generic-password', '-s', 'frclyo-deepseek', '-a', 'frclyo']])
        # 已撤销登记不能授权删除后来由其他工具创建的默认条目。
        chain.write_text(json.dumps([{'service': 'frclyo-deepseek', 'account': 'frclyo'}]))
        with self.assertRaises(self.aw.UserError):
            self.aw.key_command(['remove', 'deepseek'])
        self.assertEqual(len(json.loads(chain.read_text())), 1)

    def test_keychain_entries_validation(self):
        config = self.aw.runtime('config')
        for field in ('service', 'account'):
            for value in ('', '   ', '-option', 'bad\nname', 'bad\x00name', 'bad\x7fname', 'bad\x85name', None, 2):
                with self.subTest(field=field, value=value):
                    entry = {'service': 'my-deepseek', 'account': 'me', field: value}
                    self.cfg.write_text(json.dumps({'keychain_entries': {'deepseek': entry}}))
                    with self.assertRaises(ValueError):
                        config.load()
        for entries in ([], None, {'unknown': {'service': 'x', 'account': 'y'}},
                        {'deepseek': {}}, {'deepseek': 'bad'}):
            self.cfg.write_text(json.dumps({'keychain_entries': entries}))
            with self.assertRaises(ValueError):
                config.load()
        self.assertEqual(self.security_calls(), [])

    def test_key_add_requires_confirmation(self):
        result = self.terminal('key', 'add', 'deepseek', input='n\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = self.aw.read_journal()
        undone = {r.get('id') for r in rows if r['action'] == 'undone'}
        self.assertTrue(all(r['id'] in undone for r in rows if r.get('kind') == 'key'))

    def test_unknown_flags_rejected(self):
        result = self.terminal('__install', '--yes', '--no-input')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.aw.JOURNAL.exists())

    def test_doctor_reports_empty_cache_as_no_candidates(self):
        result = self.terminal('doctor')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('无可用模型', result.stdout)

    def test_doctor_unstarted_board_is_warning_and_success(self):
        with mock.patch.object(self.aw, 'port_open', return_value=False), \
                mock.patch.object(self.aw, 'model_inventory', return_value=[CHOICE]):
            self.assertEqual(self.aw.doctor([]), 0)
        self.assertIn('⚠️ 看板端口', self.output.getvalue())
        self.assertNotIn('❌ 看板端口', self.output.getvalue())

    def test_doctor_board_warning_does_not_hide_model_failure(self):
        with mock.patch.object(self.aw, 'port_open', return_value=False), \
                mock.patch.object(self.aw, 'model_inventory', return_value=[]):
            self.assertEqual(self.aw.doctor([]), 1)
        self.assertIn('⚠️ 看板端口', self.output.getvalue())
        self.assertIn('❌ 无可用模型', self.output.getvalue())

    def test_calibrate_records_real_performance(self):
        with mock.patch.object(self.aw, 'model_inventory', return_value=[CHOICE]):
            self.assertEqual(self.aw.calibrate(['--yes']), 0)
        rows = [json.loads(line) for line in self.aw.path_of('perf_file').read_text().splitlines()]
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row['通过验收'] for row in rows))
        self.assertTrue(all(row['费用美元'] is None for row in rows))
        self.assertEqual(self.aw.runtime('audit').check()['问题'], [])

    def model_cache(self, *names):
        """给真实命令入口提供候选，排序与过滤使用生产公共库。"""
        cache = self.home / '.codex/models_cache.json'
        cache.parent.mkdir(exist_ok=True)
        cache.write_text(json.dumps({'client_version': 'app-other-version', 'models': [
            {'slug': name, 'visibility': 'list', 'supported_reasoning_levels': [{'effort': 'medium'}]}
            for name in names]}))

    def availability_rows(self):
        return json.loads((self.home / 'work/模型可用性.json').read_text())['records']

    def test_terminal_demo_unsupported_first_candidate_switches_and_passes(self):
        self.model_cache('candidate-one', 'candidate-two')
        with mock.patch.dict(os.environ, {'FAKE_UNUSABLE': 'candidate-one'}):
            result = self.terminal('demo', '--yes')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn('candidate-one 用不了，已换成 candidate-two', result.stdout)
        self.assertIn('三关全部通过', result.stdout)
        records = {r['model']: r['status'] for r in self.availability_rows()}
        self.assertEqual(records, {'candidate-one': 'unusable', 'candidate-two': 'ok'})
        runs = list(self.aw.path_of('deliver_dir').glob('*-*/r2/回执.json'))
        self.assertEqual(len(runs), 2)
        self.assertTrue(all(json.loads(p.read_text())['模型'] == 'candidate-two' for p in runs))
        rows = [json.loads(line) for line in self.aw.path_of('perf_file').read_text().splitlines()]
        self.assertEqual(len(rows), 4)
        self.assertEqual(sum(r['通过验收'] for r in rows), 2)
        # 下一次直接避开已拒绝候选；旧轮次的证据仍存在。
        self.assertEqual([r['model'] for r in self.aw.model_inventory()], ['candidate-two'])
        self.assertTrue(all(p.parent.parent.joinpath('r1/events.jsonl').exists() for p in runs))

    def test_terminal_demo_failure_is_nonzero_and_other_errors_do_not_retry(self):
        self.model_cache('candidate-one', 'candidate-two')
        with mock.patch.dict(os.environ, {'FAKE_BAD': 'exit'}):
            result = self.terminal('demo', '--yes')
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('演示未通过', result.stderr)
        self.assertNotIn('已换成', result.stdout)
        self.assertFalse(list(self.aw.path_of('deliver_dir').glob('*/r2')))
        self.assertFalse((self.home / 'work/模型可用性.json').exists())

    def test_terminal_demo_gate_failure_is_nonzero_but_model_is_ok(self):
        self.model_cache('candidate-one')
        with mock.patch.dict(os.environ, {'FAKE_BAD': 'sample'}):
            result = self.terminal('demo', '--yes')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('已换成', result.stdout)
        self.assertEqual(self.availability_rows()[0]['status'], 'ok')

    def test_fallback_is_limited_to_three_replacements(self):
        names = ['candidate-%d' % i for i in range(6)]
        self.model_cache(*names)
        pool = self.aw.model_inventory()
        task = self.aw.new_task('校准上限测试')
        job = self.aw.prepare_worker(task, pool[0], '# 校准任务 sort', SOURCE / 'demo/calibrate')
        with mock.patch.dict(os.environ, {'FAKE_UNUSABLE': ','.join(names)}):
            self.aw.invoke_with_fallback(job, pool)
        self.assertEqual(job['round'], 'r4')
        self.assertEqual(len(job['attempts']), 3)
        self.assertIn('上限', job['failure'])
        with self.assertRaises(self.aw.UserError):
            self.aw.verify_receipt(job)
        self.assertEqual(len(self.availability_rows()), 4)

    def test_terminal_demo_all_candidates_unusable_fails(self):
        self.model_cache('candidate-one')
        with mock.patch.dict(os.environ, {'FAKE_UNUSABLE': 'candidate-one'}):
            result = self.terminal('demo', '--yes')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('无剩余候选', result.stderr)
        self.assertFalse(list(self.aw.path_of('deliver_dir').glob('*/r2')))

    def test_calibrate_fallback_report_retains_failed_combo_without_duplicate_questions(self):
        self.model_cache('candidate-one', 'candidate-two')
        with mock.patch.dict(os.environ, {'FAKE_UNUSABLE': 'candidate-one'}):
            result = self.terminal('calibrate', '--yes')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('candidate-one 用不了，已换成 candidate-two', result.stdout)
        report = (self.home / 'work/路由建议.md').read_text()
        self.assertIn('candidate-one | 0/1', report)
        self.assertIn('candidate-two | 3/3', report)
        self.assertIn('模型不被当前账号或 CLI 支持', report)
        self.assertIn('/r1', report)
        rows = [json.loads(line) for line in self.aw.path_of('perf_file').read_text().splitlines()]
        self.assertEqual(len(rows), 4)
        self.assertEqual([r['status'] for r in sorted(self.availability_rows(), key=lambda r: r['model'])], ['unusable', 'ok'])

    def test_calibrate_failed_grades_report_and_exit_zero_without_retry(self):
        self.model_cache('candidate-one')
        with mock.patch.dict(os.environ, {'FAKE_CALIBRATE_BAD': 'grade'}):
            result = self.terminal('calibrate', '--yes')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('已换成', result.stdout)
        report = (self.home / 'work/路由建议.md').read_text()
        self.assertIn('candidate-one | 0/3', report)
        self.assertIn('失败轮次：3', report)
        self.assertIn('评分或执行失败', report)
        self.assertEqual(self.availability_rows()[0]['status'], 'ok')

    def test_doctor_live_records_availability_and_retries_with_remaining_candidates(self):
        self.model_cache('candidate-one', 'candidate-two')
        with mock.patch.dict(os.environ, {'FAKE_UNUSABLE': 'candidate-one'}):
            result = self.terminal('doctor', '--live', '--yes')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('已换成 candidate-two', result.stdout)
        self.assertEqual({r['model']: r['status'] for r in self.availability_rows()},
                         {'candidate-one': 'unusable', 'candidate-two': 'ok'})
        self.assertTrue((self.home / 'work/自检报告.md').exists())

    def test_open_code_pair_uses_availability_order(self):
        pool = [dict(worker='opencode', model='provider-b/good', level='', availability='ok'),
                dict(worker='opencode', model='deepseek/unknown', level='', availability='unknown')]
        self.assertEqual(self.aw.choose_pair(pool), tuple(pool))

    def test_opencode_fallback_can_change_provider_without_colliding_with_peer(self):
        choices = [dict(worker='opencode', model=name, level='') for name in
                   ('provider-a/first', 'provider-b/second', 'provider-c/third')]
        task = self.aw.new_task('OpenCode 候选切换')
        job = self.aw.prepare_worker(task, choices[0], '# 校准任务 sort', SOURCE / 'demo/calibrate')
        providers = {task: 'provider-a', 'peer': 'provider-b'}
        with mock.patch.dict(os.environ, {'FAKE_UNUSABLE': 'provider-a/first'}):
            self.aw.invoke_with_fallback(job, choices, providers)
        self.assertEqual(job['choice']['model'], 'provider-c/third')
        self.assertEqual(job['round'], 'r2')
        self.aw.verify_receipt(job)
        self.assertEqual(providers, {'peer': 'provider-b'})

    def test_key_add_remove_and_uninstall_real_interfaces(self):
        self.aw.install(['--yes'])
        with mock.patch('builtins.input', return_value='y'):
            self.assertEqual(self.aw.key_command(['add', 'deepseek']), 0)
        plans = [row for row in self.aw.read_journal() if row.get('kind') == 'key']
        self.assertEqual(len(plans), 1)
        self.assertEqual(self.aw.runtime('keys').list_providers(), ['deepseek'])
        with mock.patch('builtins.input', return_value='y'):
            self.aw.key_command(['remove', 'deepseek'])
        self.assertEqual(self.aw.runtime('keys').list_providers(), [])
        with mock.patch('builtins.input', return_value='y'):
            self.aw.key_command(['add', 'deepseek'])
        self.assertEqual(self.aw.uninstall(['--yes']), 0)
        self.assertEqual(self.aw.runtime('keys').list_providers(), [])

    def test_calibrate_decline_has_no_side_effect(self):
        with mock.patch.object(self.aw, 'model_inventory', return_value=[CHOICE]):
            self.assertEqual(self.aw.calibrate(['--no-input']), 0)
        self.assertFalse((self.home / 'work').exists())

    def test_run_forwards_exact_arguments(self):
        arguments = ['任务一', 'r1', '带 空格.txt', 'model', '', '240', '$(never-run)']
        with mock.patch.object(self.aw.os, 'execv', side_effect=SystemExit) as execute:
            with self.assertRaises(SystemExit):
                self.aw.main(['运行', 'opencode', *arguments])
        self.assertEqual(execute.call_args.args[1][1:], arguments)

    def test_ledger_forwards_exact_arguments(self):
        arguments = ['备注', 'CX-20261013-01', '中文 空格 ; $(never-run)']
        with mock.patch.object(self.aw.runtime('ledger'), 'main', return_value=0) as execute:
            self.assertEqual(self.aw.main(['账本', *arguments]), 0)
        execute.assert_called_once_with(arguments, prog='aw 账本')


if __name__ == '__main__':
    unittest.main()
