"""真实 bash 入口配合假 Codex CLI，验证参数、配置代理、开关与回执。"""
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

# 假模型只记录调用参数并交固定结果；不运行真实 agent，也不访问网络。
BIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BIN))
from frclyo.isolation import codex_tmp
from frclyo.isolation import codex_command_environment
FAKE = '''#!/usr/bin/env python3
# 测试专用模型程序，保留事件和结果协议。
import json, os, pathlib, sys, time
if '--version' in sys.argv:
    print('codex-cli test')
    sys.exit(0)
args = sys.argv[1:]
if args == ['app-server']:
    if os.environ.get('FAKE_CATALOG') != 'cli':
        sys.exit(1)
    for line in sys.stdin:
        req = json.loads(line)
        if req['method'] == 'initialized':
            continue
        result = {} if req['method'] == 'initialize' else {'data': [
            {'model': 'test-model', 'hidden': False, 'supportedReasoningEfforts': [{'reasoningEffort': 'high'}]}]}
        print(json.dumps({'id': req['id'], 'result': result}), flush=True)
    sys.exit(0)
run = pathlib.Path(args[args.index('-C') + 1])
(run / 'host-environment.json').write_text(json.dumps({k: os.environ.get(k) for k in ('HOME', 'CODEX_HOME', 'TMPPREFIX', 'NPM_CONFIG_CACHE', 'PIP_CACHE_DIR')}))
(run / 'invocation.json').write_text(json.dumps({'args': args, 'temporary': {k: os.environ.get(k) for k in ('TMPDIR', 'TMP', 'TEMP')}, 'cache': {k: os.environ.get(k) for k in ('CLANG_MODULE_CACHE_PATH', 'SWIFT_MODULECACHE_PATH', 'XDG_CACHE_HOME')}, 'proxy': {k: v for k,v in os.environ.items() if 'proxy' in k.lower()}}))
if sys.stdin.read().strip().endswith('timeout'):
    time.sleep(60)
pathlib.Path(args[args.index('-o') + 1]).write_text('MODEL-SENTINEL\\n')
print(json.dumps({'type': 'thread.started', 'thread_id': 'fake-thread'}))
print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 3, 'output_tokens': 2}}))
'''


class CodexWorkerTests(unittest.TestCase):
    def setUp(self):
        # 夹具必须有独立 ASCII HOME；不自动逃到宿主共享临时目录。
        temporary_parent = Path(tempfile.gettempdir()).resolve()
        if not str(temporary_parent).isascii():
            raise RuntimeError('此测试需要 ASCII 物理临时目录；请显式设置 TMPDIR，不回退到共享 /tmp')
        self.temp = tempfile.TemporaryDirectory(prefix='codex-worker-test-', dir=temporary_parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.assertTrue(str(self.root).isascii())
        commands = self.root / "commands"
        commands.mkdir()
        fake = commands / "codex"
        fake.write_text(FAKE)
        fake.chmod(0o700)
        (self.root / ".codex").mkdir()
        (self.root / ".codex/models_cache.json").write_text(json.dumps({"client_version": "test", "models": [
            {"slug": "test-model", "visibility": "list", "supported_reasoning_levels": [{"effort": "high"}, {"effort": "ultra"}],
             "service_tiers": [{"id": "priority"}]}]}))
        self.cfg = self.root / "config.json"
        self.configure()
        self.task = self.root / "task.txt"
        self.task.write_text("normal")
        self.env = dict(os.environ, HOME=str(self.root), FRCLYO_HOME=str(self.root / "work space"), FRCLYO_CONFIG=str(self.cfg),
                        PATH=str(commands) + os.pathsep + os.environ.get("PATH", ""),
                        HTTPS_PROXY="http://inherited.invalid", all_proxy="http://inherited.invalid")
        for name in ("CODEX_HOME", "CODEX_WORKER_NET", "CODEX_WORKER_WEB", "CODEX_WORKER_FAST", "CODEX_WORKER_SUBAGENTS", "WORKER_INPUT_DIR"):
            self.env.pop(name, None)

    def configure(self, **codex):
        """配置使用含空格路径，检查 shell 引号和 Python 路径转义。"""
        self.cfg.write_text(json.dumps({"work_dir": str(self.root / "work space"), "codex": codex}))

    def launch(self, run="r1", effort="high", timeout="10", **environment):
        proc = subprocess.run(["/bin/bash", str(BIN / "codex-worker.sh"), "TEST-01", run,
                               str(self.task), "test-model", effort, timeout],
                              env=dict(self.env, **environment), capture_output=True, text=True, timeout=20)
        directory = self.root / "work space/交付/TEST-01" / run
        if proc.returncode:
            receipt = directory / '回执.json'
            if receipt.is_file():
                # 仅测试夹具回执，帮助区分逻辑失败与宿主进程查询受限。
                proc.stderr += '\nfixture receipt: ' + receipt.read_text()
        return proc, directory

    def test_extra_arguments_and_options_fail_before_preparation(self):
        probe = self.root / 'probe'
        probe.mkdir()
        python = probe / 'python3'
        python.write_text('#!/bin/sh\necho PREPARATION_REACHED >&2\nexit 77\n')
        python.chmod(0o700)
        for worker, model in (('codex', 'test-model'), ('opencode', 'deepseek/test-model')):
            args = ['T', 'r1', str(self.task), model, 'high', '60']
            def call(values):
                return subprocess.run(['/bin/bash', str(BIN / (worker + '-worker.sh')), *values],
                                      env=dict(self.env, PATH=str(probe) + ':/usr/bin:/bin'),
                                      capture_output=True, text=True, timeout=10)
            self.assertIn('PREPARATION_REACHED', call(args).stderr)
            for extra in ('--unknown', '--help', 'extra', ''):
                result = call(args + [extra])
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertNotIn('PREPARATION_REACHED', result.stderr)
            for index in range(6):
                bad = list(args)
                bad[index] = '--unknown'
                result = call(bad)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertNotIn('PREPARATION_REACHED', result.stderr)

    def test_project_receipt_and_single_line_summary(self):
        for index, text in enumerate(('项目：zh2en-语料\nnormal', 'normal')):
            self.task.write_text(text)
            proc, run = self.launch(run='project' + str(index))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            project = 'zh2en-语料' if index == 0 else ''
            self.assertEqual(json.loads((run / '回执.json').read_text())['项目'], project)
            self.assertEqual(proc.stdout, '【项目 %s】TEST-01 project%d 执行完成（回执：%s）\n' %
                             (project, index, run / '回执.json'))

    def test_default_security_flags_receipt_and_input(self):
        source = self.root / "input"
        source.mkdir()
        (source / "sample.txt").write_text("input-sentinel")
        proc, run = self.launch(WORKER_INPUT_DIR=str(source))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        invocation = json.loads((run / "invocation.json").read_text())
        args = invocation["args"]
        self.assertEqual(args[0], "exec")
        for flag in ("--ephemeral", "--ignore-user-config", "--ignore-rules", "--json", "--skip-git-repo-check"):
            self.assertIn(flag, args)
        disabled = [args[i + 1] for i, x in enumerate(args[:-1]) if x == "--disable"]
        for feature in ("hooks", "memories", "apps", "plugins", "computer_use", "browser_use", "browser_use_external", "multi_agent"):
            self.assertIn(feature, disabled)
        for setting in ('default_permissions="frclyo_worker"', 'approvals_reviewer="user"', 'approval_policy="never"',
                        'shell_environment_policy.inherit="core"', 'shell_environment_policy.ignore_default_excludes=false',
                        'history.persistence="none"', 'web_search="disabled"', 'service_tier="priority"'):
            self.assertIn(setting, args)
        permission = next(x for x in args if x.startswith("permissions.frclyo_worker="))
        self.assertIn('"~/.frclyo"="deny"', permission)
        self.assertIn('"~/.local/share/opencode"="deny"', permission)
        self.assertIn(str(self.root / "work space/运行时/opencode"), permission)
        self.assertNotIn("-s", args)
        self.assertIn('":tmpdir"="read"', permission)
        self.assertIn('":slash_tmp"="read"', permission)
        self.assertEqual(invocation["proxy"], {})
        self.assertEqual((run / "输入/sample.txt").read_text(), "input-sentinel")
        receipt = json.loads((run / "回执.json").read_text())
        self.assertEqual(receipt["状态"], "执行完成")
        rules = (BIN.parent / "工人守则.md").read_bytes()
        self.assertEqual(receipt["守则sha256"], hashlib.sha256(rules).hexdigest())
        location = f'\n本轮目录（交付文件只在这里读写，临时操作按守则第 3 条）：{run.resolve()}'.encode('utf-8')
        self.assertEqual((run / "任务.txt").read_bytes(), rules + location + b"\n\n---\n\n" + self.task.read_bytes())
        self.assertEqual(receipt["退出码"], 0)
        self.assertTrue(receipt["快速档"])
        self.assertEqual(receipt["模型清单来源"], "缓存（版本一致）")
        self.assertFalse(receipt["网页搜索"])
        self.assertEqual(receipt["结果sha256"], hashlib.sha256((run / "结果.txt").read_bytes()).hexdigest())
        again, _ = self.launch()
        self.assertEqual(again.returncode, 3)

    def test_command_network_is_per_launch_and_independent_of_web(self):
        for index, (net, web) in enumerate((('1', '0'), ('0', '1'), ('true', '0'), ('', '0'))):
            proc, run = self.launch(run='network' + str(index), CODEX_WORKER_NET=net, CODEX_WORKER_WEB=web)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            args = json.loads((run / 'invocation.json').read_text())['args']
            permission = next(arg for arg in args if arg.startswith('permissions.frclyo_worker='))
            self.assertIn('network={enabled=' + ('true' if net == '1' else 'false') + '}', permission)
            self.assertIn('"~/.codex"="deny"', permission)
            receipt = json.loads((run / '回执.json').read_text())
            self.assertEqual(receipt['命令网络'], '开' if net == '1' else '关')
            self.assertEqual(receipt['网页搜索'], web == '1')
        proc, run = self.launch(run='default-after-enabled')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads((run / '回执.json').read_text())['命令网络'], '关')

    def test_prepare_failure_also_records_command_network(self):
        for net in ('0', '1'):
            proc, run = self.launch(run='prepare-net-' + net, CODEX_WORKER_NET=net,
                                    WORKER_INPUT_DIR=str(self.root / 'missing'))
            self.assertEqual(proc.returncode, 2)
            receipt = json.loads((run / '回执.json').read_text())
            self.assertEqual(receipt['状态'], '准备失败')
            self.assertEqual(receipt['模型清单来源'], '缓存（版本一致）')
            self.assertEqual(receipt['命令网络'], '开' if net == '1' else '关')

    def test_private_temporary_directory_and_shell_environment(self):
        proc, run = self.launch()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        invocation = json.loads((run / 'invocation.json').read_text())
        temporary = codex_tmp(self.root / 'work space', run, self.root)
        self.assertTrue(temporary.is_dir())
        self.assertFalse(temporary.is_relative_to(self.root / 'work space'))
        self.assertEqual(temporary.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(temporary.parent.stat().st_uid, os.getuid())
        self.assertTrue(str(temporary).isascii())
        self.assertEqual(temporary.stat().st_mode & 0o777, 0o700)
        self.assertEqual({name: str(Path(value).resolve()) for name, value in invocation['temporary'].items()},
                         dict.fromkeys(('TMPDIR', 'TMP', 'TEMP'), str(temporary.resolve())))
        setting = next(arg for arg in invocation['args'] if arg.startswith('shell_environment_policy.set='))
        host = json.loads((run / 'host-environment.json').read_text())
        self.assertEqual(host['HOME'], self.env['HOME'])
        self.assertEqual(host['CODEX_HOME'], self.env.get('CODEX_HOME'))
        for name, suffix in (('TMPPREFIX', 'zsh'), ('NPM_CONFIG_CACHE', 'npm-cache'), ('PIP_CACHE_DIR', 'pip-cache')):
            self.assertEqual(host[name], str(temporary / suffix))
        for name, expected in codex_command_environment(self.root / 'work space', run, self.root).items():
            match = re.search(r'(?:\{|, )' + name + r'=("(?:[^"\\]|\\.)*")', setting)
            self.assertIsNotNone(match, name)
            value = json.loads(match.group(1))
            self.assertEqual(value, expected)
        self.assertTrue((temporary / 'home').is_dir())
        for name in ('TMPDIR', 'TMP', 'TEMP'):
            value, _ = json.JSONDecoder().raw_decode(setting.split(name + '=', 1)[1])
            self.assertEqual(Path(value).resolve(), temporary.resolve())
        for name, suffix in (('CLANG_MODULE_CACHE_PATH', 'clang-module-cache'),
                             ('SWIFT_MODULECACHE_PATH', 'swift-module-cache'), ('XDG_CACHE_HOME', 'cache')):
            self.assertEqual(invocation['cache'][name], str(temporary / suffix))
            value, _ = json.JSONDecoder().raw_decode(setting.split(name + '=', 1)[1])
            self.assertEqual(value, str(temporary / suffix))
        self.assertFalse((run / 'tmp').exists())
        proc, second = self.launch('r2')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        other = json.loads((second / 'invocation.json').read_text())['temporary']['TMPDIR']
        self.assertNotEqual(Path(other).resolve(), temporary.resolve())

    def test_preexisting_private_runtime_fails_before_worker_launch(self):
        temporary = codex_tmp(self.root / 'work space', self.root / 'work space/交付/TEST-01/r1', self.root)
        temporary.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary.mkdir(mode=0o700)
        proc, run = self.launch()
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse((run / 'invocation.json').exists())

    def test_config_proxy_fast_and_explicit_switches(self):
        self.configure(proxy="http://127.0.0.1:12345", default_fast=False)
        proc, run = self.launch(effort="ultra", CODEX_WORKER_WEB="1", CODEX_WORKER_SUBAGENTS="1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        invocation = json.loads((run / "invocation.json").read_text())
        args = invocation["args"]
        self.assertEqual(args[:2], ["--search", "exec"])
        self.assertNotIn("multi_agent", args)
        self.assertNotIn('service_tier="priority"', args)
        self.assertEqual(invocation["proxy"]["HTTPS_PROXY"], "http://127.0.0.1:12345")
        self.assertEqual(invocation["proxy"]["https_proxy"], "http://127.0.0.1:12345")
        proc, run = self.launch("r2", CODEX_WORKER_FAST="1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(json.loads((run / "回执.json").read_text())["快速档"])
        proc, run = self.launch("r3", CODEX_WORKER_FAST="0")
        self.assertFalse(json.loads((run / "回执.json").read_text())["快速档"])

    def test_invalid_effort_ultra_and_zero_timeout_fail(self):
        for effort, timeout in (("invalid", "10"), ("ultra", "10"), ("high", "0")):
            proc, run = self.launch(effort=effort, timeout=timeout)
            self.assertEqual(proc.returncode, 2)
            self.assertFalse(run.exists())

    def test_custom_codex_home_controls_model_effort_and_fast(self):
        selected = self.root / 'custom Codex 中文'
        selected.mkdir()
        default = self.root / '.codex/models_cache.json'
        catalog = json.loads(default.read_text())
        (selected / 'models_cache.json').write_text(json.dumps(catalog))
        default.write_text('{broken-default-cache')
        proc, run = self.launch(CODEX_HOME=str(selected))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(json.loads((run / '回执.json').read_text())['快速档'])
        args = json.loads((run / 'invocation.json').read_text())['args']
        self.assertIn(str(selected), next(arg for arg in args if arg.startswith('permissions.frclyo_worker=')))
        catalog['models'][0]['service_tiers'] = []
        (selected / 'models_cache.json').write_text(json.dumps(catalog))
        proc, run = self.launch('r2', CODEX_HOME=str(selected))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(json.loads((run / '回执.json').read_text())['快速档'])
        catalog['models'][0]['supported_reasoning_levels'] = [{'effort': 'medium'}]
        (selected / 'models_cache.json').write_text(json.dumps(catalog))
        proc, run = self.launch('r3', CODEX_HOME=str(selected))
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(run.exists())

    def test_missing_custom_codex_cache_never_uses_default(self):
        proc, run = self.launch(CODEX_HOME=str(self.root / 'missing-codex'))
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(run.exists())

    def test_cli_and_mismatched_cache_receipt_source(self):
        proc, run = self.launch('cli', FAKE_CATALOG='cli')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads((run / '回执.json').read_text())['模型清单来源'], '命令行')
        cache = self.root / '.codex/models_cache.json'
        cache.write_text(json.dumps({'client_version': 'app-version', 'models': []}))
        proc, run = self.launch('mismatch')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('仅提醒、不拦截', proc.stderr)
        self.assertEqual(json.loads((run / '回执.json').read_text())['模型清单来源'], '缓存（版本不同，未拦）')

    def test_timeout_receipt(self):
        self.task.write_text("timeout")
        proc, run = self.launch(timeout="1")
        self.assertEqual(proc.returncode, 124, proc.stderr)
        receipt = json.loads((run / "回执.json").read_text())
        self.assertEqual(receipt["状态"], "超时")
        self.assertEqual(receipt["退出码"], 124)
        self.assertIsNone(receipt["结果sha256"])

    def test_worker_records_success_and_unsupported_model(self):
        proc, run = self.launch()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        path = self.root / 'work space/模型可用性.json'
        row = json.loads(path.read_text())['records'][0]
        self.assertEqual((row['worker'], row['model'], row['cli_version'], row['status']),
                         ('codex', 'test-model', 'test', 'ok'))
        fake = self.root / 'commands/codex'
        error = {'type': 'error', 'message': json.dumps({'status': 400, 'error': {'message':
                 "The 'test-model' model is not supported when using Codex with a ChatGPT account."}})}
        fake.write_text(FAKE.replace("args = sys.argv[1:]", "print(" + repr(json.dumps(error)) + "); sys.exit(1)\nargs = sys.argv[1:]"))
        proc, run = self.launch('r2')
        self.assertNotEqual(proc.returncode, 0)
        row = json.loads(path.read_text())['records'][0]
        self.assertEqual(row['status'], 'unusable')
        self.assertIn('运行时/控制/codex/TEST-01/r2/events.jsonl', row['reason'])
        self.assertEqual(json.loads((run / '回执.json').read_text())['模型可用性'], 'unusable')

    def test_zero_cli_exit_with_failed_turn_is_not_success(self):
        fake = self.root / 'commands/codex'
        fake.write_text(FAKE.replace("'turn.completed'", "'turn.failed'"))
        proc, run = self.launch()
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(json.loads((run / '回执.json').read_text())['状态'], '失败')
        self.assertFalse((self.root / 'work space/模型可用性.json').exists())

    def test_worker_symlink_result_and_receipt_do_not_write_through(self):
        sentinel = self.root / 'sentinel'
        sentinel.write_bytes(b'UNCHANGED')
        fake = self.root / 'commands/codex'
        payload = "(run / '结果.txt').symlink_to(" + repr(str(sentinel)) + ")\n"
        payload += "(run / '回执.json').symlink_to(" + repr(str(sentinel)) + ")"
        fake.write_text(FAKE.replace("pathlib.Path(args[args.index('-o') + 1]).write_text('MODEL-SENTINEL\\n')", payload))
        proc, run = self.launch()
        self.assertNotEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(sentinel.read_bytes(), b'UNCHANGED')
        receipt = json.loads((run / '回执.json').read_text())
        self.assertEqual(receipt['状态'], '失败')
        self.assertTrue(receipt['错误'])
        self.assertFalse((run / '回执.json').is_symlink())
        self.assertFalse((self.root / 'work space/模型可用性.json').exists())

    def test_forged_event_file_cannot_poison_model_availability(self):
        fake = self.root / 'commands/codex'
        payload = "(run / 'events.jsonl').write_text(" + repr(json.dumps({
            'type': 'error', 'error': {'code': 'model_not_supported'}})) + ")\n"
        payload += "(run / '.超时').write_text('forged')\n"
        payload += "(run / 'stderr.log').symlink_to(" + repr(str(self.root / 'sentinel')) + ")\n"
        (self.root / 'sentinel').write_text('UNCHANGED')
        fake.write_text(FAKE.replace("pathlib.Path(args[args.index('-o') + 1])", payload + "pathlib.Path(args[args.index('-o') + 1])"))
        proc, run = self.launch()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        receipt = json.loads((run / '回执.json').read_text())
        self.assertEqual(receipt['状态'], '执行完成')
        self.assertEqual(receipt['模型可用性'], 'ok')
        row = json.loads((self.root / 'work space/模型可用性.json').read_text())['records'][0]
        self.assertEqual(row['status'], 'ok')
        self.assertNotIn('model_not_supported', (run / 'events.jsonl').read_text())
        self.assertFalse((run / 'stderr.log').is_symlink())
        self.assertEqual((self.root / 'sentinel').read_text(), 'UNCHANGED')

    def test_input_failure_prevents_worker_launch_and_leaves_no_partial_copy(self):
        source = self.root / 'input'
        source.mkdir()
        (source / 'good').write_text('data')
        (source / 'bad').symlink_to(source / 'good')
        proc, run = self.launch(WORKER_INPUT_DIR=str(source))
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertFalse((run / 'invocation.json').exists())
        self.assertFalse((run / '输入').exists())
        self.assertEqual(json.loads((run / '回执.json').read_text())['状态'], '准备失败')
        proc, run = self.launch('r2', WORKER_INPUT_DIR=str(source), WORKER_INPUT_MAX_FILES='invalid')
        self.assertEqual(proc.returncode, 2)
        self.assertFalse((run / 'invocation.json').exists())
        self.assertEqual(json.loads((run / '回执.json').read_text())['状态'], '准备失败')


if __name__ == "__main__":
    unittest.main()
