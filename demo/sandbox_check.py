#!/usr/bin/env python3
"""宿主补跑：只用合成哨兵验证真实 Seatbelt，不需要账号或模型 API。
运行 python3 -B demo/sandbox_check.py；不支持/不能嵌套沙箱时失败，不跳过。
"""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('host_demo_check', ROOT / 'demo/check.py')
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


class KernelSandboxTests(unittest.TestCase):
    def test_network_secret_reads_and_external_writes_denied(self):
        with tempfile.TemporaryDirectory(prefix='.checker-host-', dir=ROOT) as directory:
            base = Path(directory)
            home, work = base / 'synthetic-home', base / 'work'
            home.mkdir(); work.mkdir()
            for name in ('.hermes', '.codex', '.ssh'):
                (home / name).mkdir()
                (home / name / 'sentinel').write_text('synthetic')
            script = '''import errno,json,pathlib,socket
results = {}
def denied(name, action):
    try:
        action()
    except OSError as exc:
        results[name] = exc.errno in (errno.EPERM, errno.EACCES)
    else:
        results[name] = False
home = pathlib.Path(HOME_FIXTURE)
for name in ('.hermes', '.codex', '.ssh'):
    denied(name, lambda: (home / name / 'sentinel').read_bytes())
denied('home_write', lambda: (home / 'written').write_text('bad'))
denied('sibling_write', lambda: pathlib.Path(OUTSIDE_FIXTURE).write_text('bad'))
with socket.socket() as sock:
    sock.settimeout(1)
    denied('network', lambda: sock.connect(('127.0.0.1', 9)))
pathlib.Path('local-output').write_text('allowed')
results['local_write'] = pathlib.Path('local-output').read_text() == 'allowed'
print(json.dumps(results))
'''.replace('HOME_FIXTURE', repr(str(home))).replace('OUTSIDE_FIXTURE', repr(str(base / 'outside')))
            (work / 'probe.py').write_text(script)
            with mock.patch.object(check.Path, 'home', return_value=home):
                rc, out, err = check.execute([sys.executable, work / 'probe.py'], work)
            self.assertEqual(rc, 0, err.decode(errors='replace'))
            results = json.loads(out)
            self.assertEqual(set(results), {'.hermes', '.codex', '.ssh', 'home_write', 'sibling_write', 'network', 'local_write'})
            self.assertTrue(all(results.values()), results)
            self.assertFalse((work / 'local-output').exists())
            self.assertFalse((base / 'outside').exists())


if __name__ == '__main__':
    unittest.main()
