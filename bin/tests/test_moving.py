"""关口一回归：假根隔离、旧配置兼容、只读映射和工人边界。"""
import contextlib
import errno
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import warnings

BIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BIN))
from frclyo import config, isolation, ledger


class MovingTests(unittest.TestCase):
    def setUp(self):
        # HOME、根目录与测试配置全部独立，禁止继承操作者生产路径。
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.home, self.root = self.base / 'home', self.base / '中文 root'
        self.home.mkdir()
        patch = mock.patch.dict(os.environ, HOME=str(self.home), FRCLYO_HOME=str(self.root))
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop('FRCLYO_CONFIG', None)
        os.environ.pop('CODEX_HOME', None)

    def test_root_and_every_derived_path_ignore_overrides(self):
        # 旧字段甚至格式错误也只能警告忽略，不能把数据挪到另一位置。
        target = self.root / '配置/config.json'
        target.parent.mkdir(parents=True)
        fields = {'work_dir': {}, 'cards_dir': '/wrong', 'perf_file': None,
                  'runtime_dir': '/wrong', 'assets_dir': '/wrong', 'ledger_file': '/wrong'}
        target.write_text(json.dumps(fields))
        with warnings.catch_warnings(record=True) as seen:
            warnings.simplefilter('always')
            cfg = config.load()
        self.assertEqual(len(seen), 3)
        for name, suffix in {'work_dir': '', 'deliver_dir': '交付', 'cards_dir': '任务卡',
                             'ledger_file': '账本.jsonl', 'perf_file': '表现记录.jsonl',
                             'availability_file': '模型可用性.json', 'runtime_dir': '运行时/opencode',
                             'assets_dir': '素材', 'projects_dir': '项目', 'records_dir': '台账',
                             'backup_dir': '台账/备份', 'archive_dir': '台账/归档'}.items():
            self.assertEqual(cfg[name], str(self.root / suffix))
        self.assertNotIn('/wrong', ''.join(str(w.message) for w in seen))

    def test_legacy_fallback_new_precedence_and_test_override(self):
        legacy = self.home / '.frclyo/config.json'
        legacy.parent.mkdir()
        legacy.write_text('{"board_port":12345}')
        before = legacy.read_bytes()
        self.assertTrue(config.migration_needed())
        self.assertEqual(config.load()['board_port'], 12345)
        self.assertFalse(self.root.exists())
        config.save({'board_port': 12346})
        self.assertFalse(config.migration_needed())
        self.assertEqual(config.load()['board_port'], 12346)
        self.assertEqual(legacy.read_bytes(), before)
        with mock.patch.dict(os.environ, FRCLYO_CONFIG=str(self.base / 'missing.json')):
            self.assertEqual(config.load()['board_port'], 8789)
            self.assertFalse(config.migration_needed())

    def test_default_root_and_invalid_root_locations(self):
        with mock.patch.dict(os.environ):
            del os.environ['FRCLYO_HOME']
            self.assertEqual(config.root_dir(), self.home / 'AgentWork')
        for path in (self.home / 'Desktop/data', self.home / 'Documents/data', self.home / 'Library/Mobile Documents/data'):
            with mock.patch.dict(os.environ, FRCLYO_HOME=str(path)), self.assertRaises(ValueError):
                config.load()
        (self.root / '.git').mkdir(parents=True)
        with self.assertRaises(ValueError):
            config.load()

    def test_data_symlink_cannot_move_data_outside_root(self):
        self.root.mkdir()
        (self.root / '素材').symlink_to(self.home, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, '符号链接'):
            config.load()

    def test_runs_mapping_is_read_only_and_rejects_escape(self):
        self.root.mkdir()
        book = self.root / '账本.jsonl'
        row = ledger.event('开卡', 'CL-20990101-01', 标题='迁移', 项目='测试', 来源='CL', 派活人='测试', 父任务='', 卡片文件='runs/card.txt')
        book.write_text(json.dumps(row, ensure_ascii=False) + '\n')
        before = book.read_bytes()
        events = ledger.read_events(book)
        card = ledger.fold(events)[row['任务']]
        self.assertEqual(card['卡片文件'], str(self.root / '任务卡/card.txt'))
        self.assertEqual(events[0]['卡片文件'], 'runs/card.txt')
        self.assertEqual(book.read_bytes(), before)
        row['卡片文件'] = 'runs/../outside'
        with self.assertRaises(ledger.LedgerError):
            ledger.fold([row])

    def test_b05_runtime_handoff_documentation_is_present(self):
        # 文档修复的守护只断言交接约定，不能替代宿主隔离验收。
        for name in ('README.md', '大脑手册.md'):
            text = (BIN.parent / name).read_text(encoding='utf-8')
            for term in ('运行时/opencode/rounds/', '运行时/控制/', '临时',
                         'WORKER_INPUT_MAX_BYTES', 'WORKER_INPUT_MAX_FILES',
                         'WORKER_RESULT_MAX_BYTES', '64 MiB', '准备失败',
                         'keychain_entries', '反向代理', 'aw clean'):
                with self.subTest(file=name, term=term):
                    self.assertIn(term, text)
            self.assertIn('确认进程组回收后', text)
        isolation_doc = (BIN.parent / 'docs/隔离验收.md').read_text(encoding='utf-8')
        self.assertIn('(运行目录与边界.md)', isolation_doc)
        self.assertIn('公开日志在结束且确认进程组回收后发布', isolation_doc)
        detail = (BIN.parent / 'docs/运行目录与边界.md').read_text(encoding='utf-8')
        for term in ('WORKER_INPUT_MAX_BYTES', 'WORKER_INPUT_MAX_FILES',
                     'WORKER_RESULT_MAX_BYTES', 'keychain_entries', 'aw clean'):
            self.assertIn(term, detail)

    def injected_short_card(self):
        # 合成目录先规范化，不读取宿主配置或执行真实工人。
        self.root.mkdir(exist_ok=True)
        card, target = self.base / 'short.txt', self.root / '任务.txt'
        card.write_text('只交回结论。', encoding='utf-8')
        isolation.inject_task(card, target)
        return target.read_text(encoding='utf-8').split('\n\n---\n\n', 1)[0]

    def test_b16_injected_rules_explain_codex_build_cwd(self):
        text = self.injected_short_card()
        self.assertIn('在首次调用前直接进入入口提供的 `$TMPDIR`', text)
        self.assertIn('复制本轮所需输入后做 git 操作或构建', text)
        self.assertIn('最终源码、补丁、可执行文件及日志必须拷回本轮目录', text)

    def test_b26_injected_rules_explain_git_ancestor_failure(self):
        text = self.injected_short_card()
        self.assertIn('本轮目录的受保护上层会让 git 路径解析失败', text)
        self.assertIn('在其中新建工作子目录', text)
        self.assertIn('不能改用公共临时目录', text)
        self.assertIn('此约定不授权 commit 或 push', text)

    def test_b43_injected_rules_distinguish_test_errors_and_allow_input_copies(self):
        text = self.injected_short_card()
        self.assertIn('你发起的动作被沙箱拒绝时立即停止该动作', text)
        self.assertIn('被测程序或测试自己报出的权限错误属于运行结果', text)
        self.assertIn('跳过这一项，继续做其余不依赖它的部分', text)
        self.assertIn('本轮 `输入/` 里大脑提供的副本可以读', text)

    def test_injection_retains_exact_card_bytes_and_digest(self):
        self.root.mkdir()
        source, target = self.base / 'card', self.root / '任务.txt'
        card = b'original card\r\nkeep bytes\x00'
        source.write_bytes(card)
        sha = isolation.inject_task(source, target)
        rules = (BIN.parent / '工人守则.md').read_bytes()
        location = f'\n本轮目录（交付文件只在这里读写，临时操作按守则第 3 条）：{self.root.resolve()}'.encode('utf-8')
        self.assertEqual(target.read_bytes(), rules + location + b'\n\n---\n\n' + card)
        self.assertEqual(sha, hashlib.sha256(rules).hexdigest())

    def test_injection_resolves_relative_symlink_parent_and_keeps_digest(self):
        directory = self.base / '中文 round'
        directory.mkdir()
        alias = self.base / 'alias'
        alias.symlink_to(directory, target_is_directory=True)
        source = self.base / 'card'
        source.write_bytes(b'# Original title\r\n')
        rules = (BIN.parent / '工人守则.md').read_bytes()
        for parent in (directory, alias):
            target = Path(os.path.relpath(parent / '任务.txt'))
            sha = isolation.inject_task(source, target)
            prefix, card = target.read_bytes().split(b'\n\n---\n\n', 1)
            self.assertEqual(prefix, rules + f'\n本轮目录（交付文件只在这里读写，临时操作按守则第 3 条）：{directory.resolve()}'.encode('utf-8'))
            self.assertEqual(card, source.read_bytes())
            self.assertEqual(sha, hashlib.sha256(rules).hexdigest())

    def test_shared_permissions_and_symlink_run_rejection(self):
        run = self.root / '交付/TASK/r1'
        run.mkdir(parents=True)
        runtime = self.root / '运行时/opencode'
        permission = isolation.codex_permission(str(self.root), str(run), [], str(runtime))
        self.assertIn(json.dumps(str(self.root), ensure_ascii=False) + '="deny"', permission)
        self.assertIn(json.dumps(str(run), ensure_ascii=False) + '="write"', permission)
        for name in isolation.SECRET_NAMES:
            self.assertIn('~/' + name, permission)
        run.rmdir()
        run.symlink_to(self.home, target_is_directory=True)
        with self.assertRaises(ValueError):
            isolation.codex_permission(str(self.root), str(run), [], str(runtime))

    def test_probe_never_counts_missing_or_readable_as_blocked(self):
        # 探针自身在沙箱外必定失败；缺文件不是权限拒绝，防止假阳性。
        script = BIN.parent / 'tests/host/isolation_probe.py'
        manifest = self.base / 'manifest.json'
        exists = self.base / 'readable'
        exists.write_text('FAKE')
        allowed = self.base / 'allowed.txt'
        allowed.write_text('ISOLATION_FAKE_SENTINEL')
        temporary = self.base / 'tmp'
        temporary.mkdir()
        manifest.write_text(json.dumps({
            'run': str(self.base),
            'targets': {'可读': str(exists), '缺失': str(self.base / 'missing')},
            'list_targets': {}, 'write_targets': {},
            'allowed_read': [str(allowed)],
            'allowed_write': [str(temporary / 'state.txt')],
            'temporary': str(temporary),
            'tools': {'python3': [sys.executable, '-I', '-c', 'print(1)']},
        }))
        # 新正向对照包含 getcwd 与私有临时环境；启动位置必须与 run 一致。
        env = dict(os.environ, TMPDIR=str(temporary), TMP=str(temporary), TEMP=str(temporary))
        result = subprocess.run([sys.executable, '-I', str(script), str(manifest)],
                                cwd=self.base, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        data = json.loads(result.stdout)
        self.assertFalse(data['可读']['blocked'])
        self.assertFalse(data['缺失']['blocked'])
        self.assertIsNone(data['可读']['errno'])
        self.assertEqual(data['缺失']['errno'], errno.ENOENT)
        self.assertTrue(data['本轮读写']['ok'], data['本轮读写'])
        for name in ('python3', 'getcwd', '/dev/null', '/dev/zero', '/dev/urandom', '私有临时环境'):
            self.assertTrue(data['本轮读写']['tools'][name], name)

    def test_opencode_script_injects_rules_and_receipt_without_network(self):
        # 完整执行生产 shell；只替换系统沙箱、模型 CLI、代理监听和钥匙串边界。
        repo = self.base / 'repo'
        shutil.copytree(BIN, repo / 'bin', ignore=shutil.ignore_patterns('tests', '__pycache__'))
        shutil.copytree(BIN.parent / 'workers', repo / 'workers')
        shutil.copy2(BIN.parent / '工人守则.md', repo / '工人守则.md')
        commands = self.base / 'commands'
        commands.mkdir()
        programs = {
            'security': '#!/bin/bash\n# 假凭据仅供进程注入。\nprintf FAKE_OFFLINE_KEY\n',
            'sandbox-exec': '#!/bin/bash\n# 离线不宣称内核隔离；真实验证由主机探针负责。\nshift 2\nexec "$@"\n',
            'opencode': '#!/usr/bin/env python3\n# 假模型记录收到的完整输入。\nimport sys,json,pathlib,os\nr=pathlib.Path(sys.argv[sys.argv.index("--dir")+1]);(r/"received.txt").write_text(sys.stdin.read());(r/"environment.json").write_text(json.dumps({name:os.environ.get(name) for name in ("HOME","TMPDIR","TMP","TEMP","CLANG_MODULE_CACHE_PATH","SWIFT_MODULECACHE_PATH","XDG_CACHE_HOME")}));print(json.dumps({"type":"text","part":{"text":"OFFLINE_RESULT"}}))\n'}
        for name, content in programs.items():
            if name == 'opencode':
                content = content.replace('"HOME","TMPDIR"',
                    '"HOME","ZDOTDIR","TMPPREFIX","NPM_CONFIG_CACHE","PIP_CACHE_DIR","TMPDIR"')
                content = content.replace('(r/"received.txt")',
                    '(r/"argv.json").write_text(json.dumps(sys.argv));(r/"received.txt")')
            path = commands / name
            path.write_text(content)
            path.chmod(0o700)
        (repo / 'bin/allowlist-proxy.py').write_text('# 假代理不绑定端口，只测试回执生命周期。\nimport signal,time,sys\ndef stop(*args):\n print(\'{"拒绝次数":0}\',flush=True);sys.exit(0)\nsignal.signal(signal.SIGTERM,stop)\nprint(42345,flush=True)\nwhile True: time.sleep(.1)\n')
        catalog = self.base / 'models.json'
        catalog.write_text(json.dumps({'deepseek': {'api': 'https://api.deepseek.com', 'npm': '@ai-sdk/openai-compatible', 'models': {'deepseek-flash': {'id': 'deepseek-flash'}}}}))
        config.save({'models_snapshot_source': str(catalog)})
        task = self.base / 'card.txt'
        task.write_bytes('项目：OpenCode-测试\r\nCARD_ORIGINAL\r\n'.encode())
        env = dict(os.environ, PATH=str(commands) + os.pathsep + os.environ['PATH'])
        env.pop('OPENCODE_WORKER_DEBUG', None)
        result = subprocess.run(['/bin/bash', str(repo / 'bin/opencode-worker.sh'), 'TASK', 'r1', str(task), 'deepseek/deepseek-flash', 'nothink', '10'], env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        run = self.root / '交付/TASK/r1'
        rules = (repo / '工人守则.md').read_bytes()
        location = f'\n本轮目录（交付文件只在这里读写，临时操作按守则第 3 条）：{run.resolve()}'.encode('utf-8')
        self.assertEqual((run / '任务.txt').read_bytes(), rules + location + b'\n\n---\n\n' + task.read_bytes())
        self.assertTrue((run / 'received.txt').read_text().startswith(rules.decode()))
        environment = json.loads((run / 'environment.json').read_text())
        expected = isolation.temp_environment(self.root, run)
        self.assertEqual({name: environment[name] for name in expected}, expected)
        self.assertTrue(environment['TMPDIR'].isascii())
        self.assertEqual(environment['HOME'], str(self.root / '运行时/opencode/rounds/TASK/r1/home'))
        self.assertNotEqual(environment['HOME'], str(self.home))
        self.assertEqual(environment['ZDOTDIR'], environment['HOME'])
        network = json.loads((run / '网络信息.json').read_text())
        control = Path(network['规则'])
        self.assertTrue(control.is_relative_to(self.root / '运行时/控制/opencode'))
        self.assertFalse(control.is_relative_to(self.root / '运行时/opencode'))
        receipt = json.loads((run / '回执.json').read_text())
        self.assertEqual(receipt['项目'], 'OpenCode-测试')
        self.assertEqual(result.stdout, '【项目 OpenCode-测试】TASK r1 执行完成（回执：%s）\n' % (run / '回执.json'))
        self.assertEqual(receipt['守则sha256'], hashlib.sha256(rules).hexdigest())
        self.assertEqual(receipt['状态'], '执行完成')
        self.assertNotIn('FAKE_OFFLINE_KEY', result.stdout + result.stderr + (run / 'events.jsonl').read_text())

        default_args = json.loads((run / 'argv.json').read_text())
        for flag in ('--print-logs', '--log-level', 'DEBUG'):
            self.assertNotIn(flag, default_args)
        for index, value in enumerate(('1', '0', 'true', '')):
            round_id = 'debug-' + str(index)
            result = subprocess.run(['/bin/bash', str(repo / 'bin/opencode-worker.sh'),
                'TASK', round_id, str(task), 'deepseek/deepseek-flash', 'nothink', '10'],
                env=dict(env, OPENCODE_WORKER_DEBUG=value), capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            args = json.loads((self.root / '交付/TASK' / round_id / 'argv.json').read_text())
            if value == '1':
                self.assertIn('--print-logs', args)
                self.assertEqual(args[args.index('--log-level') + 1], 'DEBUG')
                args = [arg for arg in args if arg not in ('--print-logs', '--log-level', 'DEBUG')]
            # 除轮次路径、title 和显式 DEBUG 参数之外，实际调用参数保持相同。
            self.assertEqual([arg.replace(round_id, 'r1') for arg in args], default_args)

        # F06：同一入口准备失败必须留下回执，不能启动替身或创建本轮状态。
        source = self.base / 'bad-input'
        source.mkdir()
        (source / 'bad').symlink_to(task)
        command = ['/bin/bash', str(repo / 'bin/opencode-worker.sh'), 'TASK', 'r2', str(task),
                   'deepseek/deepseek-flash', 'nothink', '10']
        bad_result = subprocess.run(command, env=dict(env, WORKER_INPUT_DIR=str(source)),
                                    capture_output=True, text=True, timeout=20)
        bad_run = self.root / '交付/TASK/r2'
        self.assertEqual(bad_result.returncode, 2, bad_result.stderr)
        self.assertEqual(json.loads((bad_run / '回执.json').read_text())['状态'], '准备失败')
        self.assertFalse((bad_run / 'received.txt').exists())
        self.assertFalse((bad_run / '输入').exists())
        self.assertFalse((self.root / '运行时/opencode/rounds/TASK/r2').exists())
        # F03：OpenCode 从可信文本生成结果前，也必须拒绝工人预放的结果链接。
        sentinel = self.base / 'sentinel'
        sentinel.write_text('UNCHANGED')
        code = '#!/usr/bin/env python3\nimport sys,json,pathlib\n'
        code += 'if "--version" in sys.argv: print("fake test"); sys.exit(0)\n'
        code += 'r=pathlib.Path(sys.argv[sys.argv.index("--dir")+1])\n'
        code += '(r/"结果.txt").symlink_to(' + repr(str(sentinel)) + ')\n'
        code += '(r/"回执.json").symlink_to(' + repr(str(sentinel)) + ')\n'
        code += 'print(json.dumps({"type":"text","part":{"text":"HOST_TEXT"}}))\n'
        (commands / 'opencode').write_text(code)
        command[3] = 'r3'
        link_result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=20)
        link_run = self.root / '交付/TASK/r3'
        self.assertNotEqual(link_result.returncode, 0)
        self.assertEqual(sentinel.read_text(), 'UNCHANGED')
        self.assertEqual(json.loads((link_run / '回执.json').read_text())['状态'], '失败')
        self.assertFalse((link_run / '回执.json').is_symlink())


if __name__ == '__main__':
    unittest.main()
