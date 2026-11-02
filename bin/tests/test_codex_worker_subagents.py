"""B85：真实 bash 启动检查；通过分支在不存在的任务文件处停止，不启动模型。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

BIN = Path(__file__).resolve().parents[1]


class SubagentWorkerTests(unittest.TestCase):
    def test_guard_version_model_force_matrix(self):
        with tempfile.TemporaryDirectory(dir=Path(os.environ['TMPDIR']).resolve()) as temporary:
            base = Path(temporary).resolve()
            commands = base / 'commands'
            commands.mkdir()
            fake = commands / 'codex'
            fake.write_text('#!' + sys.executable + '\n' + '''
import json, os, sys
if sys.argv[1:] == ['--version']:
    print('codex-cli ' + os.environ['FAKE_VERSION']); sys.exit(0)
assert sys.argv[1:] == ['app-server']
for line in sys.stdin:
    request = json.loads(line)
    if request['method'] == 'initialized': continue
    result = {} if request['method'] == 'initialize' else {'data': [
        {'model': name, 'hidden': False, 'supportedReasoningEfforts': [{'reasoningEffort': 'high'}]}
        for name in ['gpt-6-astra', 'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna']]}
    print(json.dumps({'id': request['id'], 'result': result}), flush=True)
''')
            fake.chmod(0o700)
            cfg = base / 'config.json'
            cfg.write_text('{}')
            env = dict(os.environ, PATH=str(commands) + os.pathsep + os.environ['PATH'],
                       FRCLYO_HOME=str(base / 'data'), FRCLYO_CONFIG=str(cfg),
                       CODEX_HOME=str(base / 'fake-codex'))
            for version in ('0.153.0', '0.153.12', '0.153.2-alpha.1', '0.154.0', 'unknown'):
                for model in ('gpt-6-astra', 'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna'):
                    for enabled, force in (('1', '0'), ('0', '0'), ('1', '1')):
                        with self.subTest(version=version, model=model, enabled=enabled, force=force):
                            result = subprocess.run(['/bin/bash', str(BIN / 'codex-worker.sh'), 'TEST-01', 'r1',
                                str(base / 'absent-card'), model, 'high', '10'], env=dict(env,
                                FAKE_VERSION=version, CODEX_WORKER_SUBAGENTS=enabled,
                                CODEX_WORKER_SUBAGENTS_FORCE=force), capture_output=True, text=True, timeout=10)
                            self.assertEqual(result.returncode, 2, result.stderr)
                            blocked = version.startswith('0.153.') and model != 'gpt-5.6-luna' and enabled == '1' and force != '1'
                            if blocked:
                                self.assertIn('no thread with id', result.stderr)
                                self.assertIn('CODEX_WORKER_SUBAGENTS_FORCE=1', result.stderr)
                                self.assertNotIn('找不到任务说明文件', result.stderr)
                            else:
                                self.assertIn('找不到任务说明文件', result.stderr)
                                self.assertNotIn('no thread with id', result.stderr)
            self.assertFalse((base / 'data/交付').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
