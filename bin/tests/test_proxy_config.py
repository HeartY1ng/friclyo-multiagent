"""反向代理路由/模板和历史观察的纯内存回归，不调用钥匙串或宿主配置。"""
import ast
import contextlib
import ctypes
import errno
import importlib.util
import io
import json
from pathlib import Path
import sys
import subprocess
import unittest
import tempfile
import shutil
from unittest import mock

BIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BIN))
from frclyo import config, execution, models, worker

HOST_SCRIPT = BIN.parent / 'tests/host/proxy_credentials.py'
spec = importlib.util.spec_from_file_location('proxy_credentials', HOST_SCRIPT)
credentials = importlib.util.module_from_spec(spec)
spec.loader.exec_module(credentials)


class RouteConfigTests(unittest.TestCase):
    def prepare(self, family, host=None, endpoint=None):
        route = worker.PROXY_ROUTES[family]
        cfg = dict(work_dir='/virtual', deliver_dir='/virtual/交付', runtime_dir='/virtual/运行时/opencode',
                   control_dir='/virtual/运行时/控制/opencode', opencode_config='/virtual/template',
                   providers={family: dict(opencode_family=family, api_host=host or route[0])})
        template = dict(provider={family: dict(whitelist=['model'], options=dict(
            baseURL=endpoint or 'https://' + route[0] + route[1], apiKey='{env:OLD_KEY}',
            headers={'Authorization': 'OLD-KEY'}))})
        with mock.patch.object(config, 'ensure_layout', return_value=cfg), \
             mock.patch.object(worker, 'read_jsonc', return_value=template), \
             mock.patch.object(worker, 'snapshot', return_value=('/virtual/snapshot', {'api': 'unused'})), \
             mock.patch.object(worker, 'sensitive_paths', return_value=['/virtual/sensitive']):
            return worker.prepare(family + '/model')

    def test_each_base_url_uses_local_port_and_fixed_dummy_key(self):
        for family, route in worker.PROXY_ROUTES.items():
            with self.subTest(family=family):
                prepared = self.prepare(family)
                value = json.loads(worker.local_config(prepared['content'], family, 43210))
                self.assertEqual(value['enabled_providers'], [family])
                options = value['provider'][family]['options']
                self.assertEqual(options['baseURL'], 'http://127.0.0.1:43210/' + family + route[1])
                self.assertEqual(options['apiKey'], worker.DUMMY_API_KEY)
                self.assertNotIn('OLD-KEY', json.dumps(value))
                self.assertNotIn('headers', options)
                self.assertEqual(prepared['target'], route[0] + ':443')

    def test_config_cannot_override_host_scheme_or_prefix(self):
        for endpoint in ('http://api.deepseek.com', 'https://evil', 'https://api.deepseek.com/other',
                         'https://api.deepseek.com:443', 'https://key@api.deepseek.com',
                         'https://api.deepseek.com?key=secret'):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                self.prepare('deepseek', endpoint=endpoint)
        with self.assertRaises(ValueError):
            self.prepare('deepseek', host='evil.example.com')

    def test_zai_rejects_other_hosts_and_non_coding_endpoints(self):
        for endpoint in ('https://open.bigmodel.cn/api/coding/paas/v4',
                         'https://api.z.ai/api/paas/v4',
                         'https://api.z.ai/api/coding/paas/v40',
                         'https://api.z.ai.evil/api/coding/paas/v4',
                         'http://api.z.ai/api/coding/paas/v4'):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                self.prepare('zai-coding-plan', endpoint=endpoint)
        with self.assertRaises(ValueError):
            self.prepare('zai-coding-plan', host='open.bigmodel.cn')

    def test_zhipuai_rejects_other_hosts_and_coding_endpoints(self):
        for endpoint in ('https://api.z.ai/api/paas/v4',
                         'https://open.bigmodel.cn/api/coding/paas/v4',
                         'https://open.bigmodel.cn/api/paas/v40',
                         'https://open.bigmodel.cn.evil/api/paas/v4',
                         'http://open.bigmodel.cn/api/paas/v4'):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                self.prepare('zhipuai', endpoint=endpoint)
        with self.assertRaises(ValueError):
            self.prepare('zhipuai', host='api.z.ai')

    def test_zai_template_model_whitelist(self):
        template = worker.read_jsonc(BIN.parent / 'workers/opencode/opencode.worker.jsonc')
        self.assertEqual(set(template['enabled_providers']), set(worker.PROXY_ROUTES))
        settings = template['provider']['zai-coding-plan']
        self.assertEqual(settings['npm'], '@ai-sdk/openai-compatible')
        self.assertEqual(settings['options'], {
            'baseURL': 'https://api.z.ai/api/coding/paas/v4', 'apiKey': '{env:ZHIPU_API_KEY}'})
        self.assertEqual(set(settings['whitelist']), {
            'glm-5.3', 'glm-5.3-flash', 'glm-5.3-highspeed', 'glm-5.2',
            'glm-5.2-highspeed', 'glm-4.7', 'glm-5-turbo'})

    def test_zhipuai_template_model_whitelist(self):
        template = worker.read_jsonc(BIN.parent / 'workers/opencode/opencode.worker.jsonc')
        self.assertEqual(set(template['enabled_providers']), set(worker.PROXY_ROUTES))
        settings = template['provider']['zhipuai']
        self.assertEqual(settings['npm'], '@ai-sdk/openai-compatible')
        self.assertEqual(settings['options'], {
            'baseURL': 'https://open.bigmodel.cn/api/paas/v4', 'apiKey': '{env:ZHIPU_API_KEY}'})
        self.assertEqual(set(settings['whitelist']), {
            'glm-5.3', 'glm-5.3-flash', 'glm-5.2', 'glm-5.1', 'glm-5', 'glm-4.5-air'})

    def test_version_probe_also_receives_sanitized_environment(self):
        import os
        with mock.patch.dict(os.environ, OTHER_KEY='FAKE-VERSION-KEY', BENIGN_NAME='FAKE-VERSION-KEY'), \
             mock.patch.object(models.subprocess, 'run', return_value=mock.Mock(returncode=0, stdout='opencode 1.2')) as run:
            self.assertEqual(models.cli_version('opencode'), '1.2')
        self.assertNotIn('FAKE-VERSION-KEY', repr(run.call_args.kwargs['env']))

    def test_invalid_port_never_publishes_a_template(self):
        for port in (0, 65536, 'bad'):
            with self.assertRaises(ValueError):
                worker.local_config('{}', 'deepseek', port)

    def test_xiaomi_auth_is_confirmed_and_reaches_key_handoff(self):
        from frclyo import keys
        self.assertEqual(worker.PROXY_ROUTES['xiaomi'],
                         ('api.xiaomimimo.com', '/v1', 'Authorization', 'Bearer '))
        self.assertEqual(keys.ENV_NAMES['xiaomi'], 'XIAOMI_API_KEY')
        with mock.patch.object(keys, 'get_key', return_value='FAKE-XIAOMI-KEY') as get_key, \
             mock.patch.object(execution.os, 'getppid', return_value=123), \
             mock.patch.object(execution.os, 'dup2'), \
             mock.patch.object(execution.os, 'execve', side_effect=SystemExit) as execute:
            with self.assertRaises(SystemExit):
                execution.start_proxy('xiaomi', '/unused', '123')
        get_key.assert_called_once_with('xiaomi')
        self.assertIn('xiaomi', execute.call_args.args[1])
        self.assertEqual(execute.call_args.args[2], {})
        self.assertNotIn('FAKE-XIAOMI-KEY', repr(execute.call_args))

    def test_zai_coding_plan_auth_is_confirmed_and_reaches_key_handoff(self):
        from frclyo import keys
        self.assertEqual(worker.PROXY_ROUTES['zai-coding-plan'],
                         ('api.z.ai', '/api/coding/paas/v4', 'Authorization', 'Bearer '))
        self.assertEqual(keys.ENV_NAMES['zai-coding-plan'], 'ZHIPU_API_KEY')
        with mock.patch.object(keys, 'get_key', return_value='FAKE-XIAOMI-KEY') as get_key, \
             mock.patch.object(execution.os, 'getppid', return_value=123), \
             mock.patch.object(execution.os, 'dup2'), \
             mock.patch.object(execution.os, 'execve', side_effect=SystemExit) as execute:
            with self.assertRaises(SystemExit):
                execution.start_proxy('zai-coding-plan', '/unused', '123')
        get_key.assert_called_once_with('zai-coding-plan')
        self.assertIn('zai-coding-plan', execute.call_args.args[1])
        self.assertEqual(execute.call_args.args[2], {})
        self.assertNotIn('FAKE-XIAOMI-KEY', repr(execute.call_args))

    def test_zhipuai_auth_is_confirmed_and_reaches_key_handoff(self):
        from frclyo import keys
        self.assertEqual(worker.PROXY_ROUTES['zhipuai'],
                         ('open.bigmodel.cn', '/api/paas/v4', 'Authorization', 'Bearer '))
        self.assertEqual(keys.ENV_NAMES['zhipuai'], 'ZHIPU_API_KEY')
        with mock.patch.object(keys, 'get_key', return_value='FAKE-ZHIPUAI-KEY') as get_key, \
             mock.patch.object(execution.os, 'getppid', return_value=123), \
             mock.patch.object(execution.os, 'dup2'), \
             mock.patch.object(execution.os, 'execve', side_effect=SystemExit) as execute:
            with self.assertRaises(SystemExit):
                execution.start_proxy('zhipuai', '/unused', '123')
        get_key.assert_called_once_with('zhipuai')
        self.assertIn('zhipuai', execute.call_args.args[1])
        self.assertEqual(execute.call_args.args[2], {})
        self.assertNotIn('FAKE-ZHIPUAI-KEY', repr(execute.call_args))

    def test_none_key_stops_before_proxy_or_worker_without_fallback(self):
        from frclyo import keys
        with mock.patch.object(keys, 'get_key', return_value=None) as get_key, \
             mock.patch.dict(execution.os.environ, DEEPSEEK_API_KEY='ENV-SENTINEL'), \
             mock.patch.object(keys, '_env_value') as fallback, \
             mock.patch.object(execution.os, 'getppid', return_value=123), \
             mock.patch.object(execution.os, 'pipe') as pipe, \
             mock.patch.object(execution.os, 'execve') as execute, \
             mock.patch.object(execution.subprocess, 'Popen') as popen, \
             contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(execution.start_proxy('deepseek', '/unused', '123'), 2)
        get_key.assert_called_once_with('deepseek')
        for operation in (fallback, pipe, execute, popen):
            operation.assert_not_called()
        self.assertIn('代理和工人未启动', error.getvalue())
        self.assertNotIn('ENV-SENTINEL', error.getvalue())


class CredentialProbeTests(unittest.TestCase):
    def test_fake_cli_reads_probe_only_from_copied_round_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            commands, inputs = base / 'commands', base / 'inputs'
            commands.mkdir()
            body = 'import pathlib; print(pathlib.Path(__file__).resolve())\n'
            with mock.patch.object(credentials, 'PROBE', body):
                credentials.prepare_fake_opencode(commands, inputs)
            fake = str(commands / 'opencode')
            version = subprocess.run([fake, '--version'], capture_output=True, text=True, timeout=5)
            self.assertEqual(version.returncode, 0)
            run = base / '交付 with spaces/T/r1'
            run.mkdir(parents=True)
            shutil.copytree(inputs, run / '输入')
            # 宿主源夹具删掉后仍应从本轮执行，不能靠源文件意外可读来通过。
            shutil.rmtree(inputs)
            result = subprocess.run([fake, 'run', '--dir', str(run)], capture_output=True,
                                    text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), str(run / '输入/probe.py'))

    def setUp(self):
        # 测试实际内嵌探针函数，不运行沙箱入口、ps 或真实 sysctl。
        tree = ast.parse(credentials.PROBE)
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
        namespace = dict(ctypes=ctypes, errno=errno, subprocess=subprocess)
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(HOST_SCRIPT), 'exec'), namespace)
        self.probe = namespace['process_credentials']

    def query(self, ps=None, ps_error=None, sysctl_error=None, sysctl_rc=0, sysctl_errno=0,
              data=b'allowlist-proxy.py'):
        def sysctl(mib, length, buffer, size, unused, zero):
            self.assertEqual(list(mib), [1, 49, 123])
            if sysctl_error is not None:
                raise sysctl_error
            buffer.value = data
            return sysctl_rc
        if ps is None:
            ps = subprocess.CompletedProcess([], 0, b'allowlist-proxy.py', b'')
        with mock.patch.object(subprocess, 'run', return_value=ps, side_effect=ps_error), \
             mock.patch.object(ctypes, 'CDLL', return_value=mock.Mock(sysctl=sysctl)), \
             mock.patch.object(ctypes, 'get_errno', return_value=sysctl_errno):
            return self.probe(123, credentials.FAKE_KEY)

    def test_ps_and_sysctl_permission_exceptions_are_explicit_passes(self):
        for code in (errno.EPERM, errno.EACCES):
            with self.subTest(code=code):
                result = self.query(ps_error=PermissionError(code, 'denied'),
                                    sysctl_error=PermissionError(code, 'denied'))
                for name in ('ps', 'sysctl'):
                    self.assertIs(result[name + '_no_key'], True)
                    self.assertIs(result[name + '_readable'], False)
                    self.assertEqual(result[name + '_errno'], code)
                    self.assertEqual(result[name + '_status'], '无法查看（内核拒绝）')

    def test_sysctl_returned_errno_and_ps_error_output(self):
        for code, message in ((errno.EPERM, b'Operation not permitted'), (errno.EACCES, b'Permission denied')):
            with self.subTest(code=code):
                result = self.query(ps=subprocess.CompletedProcess([], 1, b'', message),
                                    sysctl_rc=-1, sysctl_errno=code)
                self.assertTrue(result['ps_no_key'])
                self.assertTrue(result['sysctl_no_key'])
                self.assertEqual(result['ps_status'], '无法查看（内核拒绝）')

    def test_other_exceptions_still_fail(self):
        for exc in (FileNotFoundError(errno.ENOENT, 'missing'), OSError(errno.EIO, 'io'),
                    subprocess.TimeoutExpired('ps', 5)):
            for parameter in ('ps_error', 'sysctl_error'):
                with self.subTest(error=type(exc).__name__, operation=parameter), self.assertRaises(type(exc)):
                    self.query(**{parameter: exc})
        self.assertFalse(self.query(sysctl_rc=-1, sysctl_errno=errno.ESRCH)['sysctl_no_key'])
        self.assertFalse(self.query(ps=subprocess.CompletedProcess([], 1, b'', b'unknown'))['ps_no_key'])

    def test_readable_output_requires_proxy_and_must_not_contain_key(self):
        result = self.query()
        self.assertTrue(result['ps_no_key'] and result['sysctl_no_key'])
        for data in (b'', b'unrelated-process', b'allowlist-proxy.py ' + credentials.FAKE_KEY.encode()):
            with self.subTest(data_kind=len(data)):
                result = self.query(ps=subprocess.CompletedProcess([], 0, data, b''), data=data)
                self.assertFalse(result['ps_no_key'])
                self.assertFalse(result['sysctl_no_key'])
        result = self.query(ps=subprocess.CompletedProcess([], 1, credentials.FAKE_KEY.encode(), b'Permission denied'))
        self.assertFalse(result['ps_no_key'])

    def diagnostics(self, data, receipt=None):
        with mock.patch.object(Path, 'read_text', return_value=json.dumps(receipt or {'状态': '失败', '退出码': 1})), \
             mock.patch.object(Path, 'read_bytes', return_value=data), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            credentials.failure_details(Path('/evidence/work/交付/PROXY-HOST/credentials'), 1)
        return output.getvalue()

    def test_failure_diagnostics_show_receipt_code_path_and_only_tail(self):
        output = self.diagnostics(b'first-line-hidden\n' + b'\n'.join(('tail-%d' % n).encode() for n in range(8)))
        self.assertIn('状态=失败，退出码=1；入口退出码=1', output)
        self.assertIn('/evidence/work/运行时/控制/opencode/PROXY-HOST/credentials/stderr.log', output)
        self.assertNotIn('first-line-hidden', output)
        self.assertIn('tail-0\n', output)
        self.assertIn('tail-7', output)

    def test_diagnostics_scan_entire_log_before_printing_tail(self):
        output = self.diagnostics(credentials.FAKE_KEY.encode() + b'\n' + b'innocent-tail\n' * 20)
        self.assertNotIn(credentials.FAKE_KEY, output)
        self.assertNotIn('innocent-tail', output)
        self.assertIn('已隐藏全部内容', output)
        output = self.diagnostics(b'', {'状态': credentials.FAKE_KEY, '退出码': 1})
        self.assertNotIn(credentials.FAKE_KEY, output)

    def test_missing_receipt_and_log_are_diagnostic_not_another_crash(self):
        with mock.patch.object(Path, 'read_text', side_effect=FileNotFoundError), \
             mock.patch.object(Path, 'read_bytes', side_effect=FileNotFoundError), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            credentials.failure_details(Path('/evidence/work/交付/PROXY-HOST/credentials'), 1)
        self.assertIn('内层回执：不可用（FileNotFoundError）；入口退出码=1', output.getvalue())
        self.assertIn('stderr.log 不可读：FileNotFoundError', output.getvalue())


class ObservationTests(unittest.TestCase):
    def test_invalid_query_path_is_unknown_and_invalid_write_still_fails(self):
        with mock.patch.object(execution, 'capture_directory', side_effect=ValueError), \
             mock.patch.object(execution, 'safe_read') as read:
            self.assertEqual(models.observe('codex', 'model', 'v1', '/invalid'), 'unknown')
            with self.assertRaises(ValueError):
                models.observe('codex', 'model', 'v1', '/invalid', captured=b'', succeeded=True)
        read.assert_not_called()

    def test_legacy_query_never_reads_worker_events_or_updates_global_record(self):
        with mock.patch.object(execution, 'capture_directory', return_value=Path('/private/round')), \
             mock.patch.object(execution, 'safe_read', side_effect=FileNotFoundError) as read, \
             mock.patch.object(models, 'record') as record:
            self.assertEqual(models.observe('codex', 'model', 'v1', '/round', succeeded=True), 'unknown')
        read.assert_called_once_with(Path('/private/round/observation.json'))
        record.assert_not_called()

    def test_trusted_historical_observation_remains_queryable(self):
        row = dict(worker='codex', model='model', cli_version='v1', status='ok')
        with mock.patch.object(execution, 'capture_directory', return_value=Path('/private/round')), \
             mock.patch.object(execution, 'safe_read', return_value=json.dumps(row).encode()):
            self.assertEqual(models.observe('codex', 'model', 'v1', '/round'), 'ok')
            self.assertEqual(models.observe('codex', 'model', 'v2', '/round'), 'unknown')

    def test_malformed_trusted_observation_is_unknown(self):
        for data in (b'[]', b'null', b'{bad'):
            with mock.patch.object(execution, 'capture_directory', return_value=Path('/private/round')), \
                 mock.patch.object(execution, 'safe_read', return_value=data):
                self.assertEqual(models.observe('codex', 'model', 'v1', '/round'), 'unknown')


if __name__ == '__main__':
    unittest.main()
