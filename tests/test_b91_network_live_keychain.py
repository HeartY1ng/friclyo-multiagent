"""B91：从 TMPDIR 中的框架副本作为 cwd 运行；只用名称，不生成/读取任何密码。

完整运行 proxy_credentials.main 的控制流，socket、进程及进程存活查询均为替身。
断言网络 live 轮配置保留宿主显式登记的钥匙串条目名称；仅验证配置传递。
"""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('b07_probe', ROOT / 'tests/host/proxy_credentials.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class B91LiveConfigRegression(unittest.TestCase):
    def test_network_live_preserves_external_keychain_entry(self):
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as tmp:
            base = Path(tmp).resolve()
            evidence = base / 'evidence'
            evidence.mkdir()
            models = base / 'models.json'
            models.write_text(json.dumps({'deepseek': {'npm': 'fixture', 'api': 'https://api.deepseek.com',
                                                      'models': {'deepseek-flash': {}}}}))
            executable = base / 'opencode'
            executable.write_text('#!/bin/sh\nexit 99\n')
            executable.chmod(0o700)
            entry = {'service': 'external-fixture-service', 'account': 'fixture-account'}
            host_config = base / 'host-config.json'
            host_config.write_text(json.dumps({'keychain_entries': {'deepseek': entry}}))
            captured = {}

            def start(argv, **kwargs):
                # 生成进程需要交回的合成证据，不执行实际脚本。
                name, env = argv[3], kwargs['env']
                work = Path(env['FRCLYO_HOME'])
                run = work / '交付/PROXY-HOST' / name
                run.mkdir(parents=True)
                control = work / '运行时/控制/opencode/PROXY-HOST' / name
                control.mkdir(parents=True)
                rc = {'timeout': 142, 'term': 143, 'kill': -9}.get(name, 0)
                receipt = {'状态': '执行完成', '退出码': rc, '进程组已回收': True,
                           '工具调用数': 1, '网络': {'放行': ['api.deepseek.com:443'], '拒绝次数': 1}}
                (run / '回执.json').write_text(json.dumps(receipt))
                (run / 'model-sentinel.txt').write_text('MODEL_API_OK\n')
                (run / 'probe-result.json').write_text(json.dumps(dict.fromkeys(
                    ('environment', 'template', 'ps_no_key', 'sysctl_no_key', 'own_proxy',
                     'other_port', 'public_tcp', 'ipv6', 'udp'), True)))
                if name == 'live':
                    captured.update(json.loads(Path(env['FRCLYO_CONFIG']).read_text()))
                process = mock.Mock()
                process.wait.return_value = rc
                process.poll.return_value = rc
                return process

            def wait(path, process):
                return ({'pid': 987654321, 'child': 987654322} if path.name == 'probe-ready.json'
                        else {'代理进程号': 987654320, '端口': 12345})

            listener = mock.Mock()
            listener.getsockname.return_value = ('127.0.0.1', 12345)
            argv = ['probe', '--live', '--models', str(models), '--opencode', str(executable)]
            with mock.patch.object(probe.sys, 'argv', argv), \
                 mock.patch.object(probe.sys, 'platform', 'darwin'), \
                 mock.patch.object(probe.shutil, 'which', return_value=str(executable)), \
                 mock.patch.object(probe.tempfile, 'mkdtemp', return_value=str(evidence)), \
                 mock.patch.object(probe.socket, 'socket', return_value=listener), \
                 mock.patch.object(probe.socket, 'create_connection', return_value=mock.MagicMock()), \
                 mock.patch.object(probe.subprocess, 'Popen', side_effect=start), \
                 mock.patch.object(probe.subprocess, 'run', return_value=subprocess.CompletedProcess(
                     [], 0, b'allowlist-proxy.py', b'')), \
                 mock.patch.object(probe, 'wait_json', side_effect=wait), \
                 mock.patch.object(probe, 'gone', return_value=True), \
                 mock.patch.object(probe, 'alive', return_value=False), \
                 mock.patch.dict(os.environ, FRCLYO_CONFIG=str(host_config), FRCLYO_HOME=str(base / 'host-root')), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(probe.main(), 0, '替身控制流须先走到 live 轮')
            self.assertIn('models_snapshot_source', captured)
            self.assertEqual(captured.get('keychain_entries', {}).get('deepseek'), entry,
                             'network_check --live 丢失宿主外部钥匙串条目，只能退回默认条目')


if __name__ == '__main__':
    unittest.main(verbosity=2)
