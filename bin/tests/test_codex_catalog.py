"""B41：真实子进程模拟 app-server JSON-RPC，不接触宿主 Codex 或网络。"""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

BIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BIN))
from frclyo import models

FAKE = '''#!/usr/bin/env python3
import json, os, pathlib, sys, time
if '--version' in sys.argv:
    print('codex-cli cli-test')
    sys.exit(0)
assert sys.argv[1:] == ['app-server']
pathlib.Path(os.environ['PROBE_PID']).write_text(str(os.getpid()))
mode = os.environ.get('PROBE_MODE', 'normal')
initialized = False
for line in sys.stdin:
    request = json.loads(line)
    with open(os.environ['PROBE_REQUESTS'], 'a') as log:
        log.write(line)
    if request['method'] == 'initialized':
        initialized = True
        continue
    if request['method'] == 'initialize':
        result = {'userAgent': 'fake'}
    else:
        assert initialized and request['method'] == 'model/list'
        assert request['params']['includeHidden'] is False
        if mode == 'timeout':
            print('{', end='', flush=True)
            time.sleep(60)
        if mode == 'rpc_error':
            print(json.dumps({'id': request['id'], 'error': {'code': -32601}}), flush=True)
            continue
        if mode == 'malformed':
            print('not-json', flush=True)
            continue
        cursor = request['params']['cursor']
        row = {'id': 'display-id', 'model': 'cli-model' if cursor is None else 'second',
               'hidden': False, 'supportedReasoningEfforts': [{'reasoningEffort': 'medium'}]}
        result = {'data': [row, dict(row, model='hidden-model', hidden=True)],
                  'nextCursor': 'next' if cursor is None else None}
        if mode == 'empty':
            result = {'data': [], 'nextCursor': None}
        if mode == 'bad_row':
            result['data'][0]['supportedReasoningEfforts'] = None
        if mode == 'loop':
            result['nextCursor'] = 'next'
    print(json.dumps({'method': 'unrelated/notification'}), flush=True)
    print(json.dumps({'id': request['id'], 'result': result}), flush=True)
'''


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        executable = self.root / 'codex'
        executable.write_text(FAKE)
        executable.chmod(0o700)
        self.home = self.root / 'codex-home'
        self.home.mkdir()
        env = mock.patch.dict(os.environ, PATH=str(self.root) + os.pathsep + os.environ['PATH'],
                              CODEX_HOME=str(self.home), PROBE_PID=str(self.root / 'pid'),
                              PROBE_REQUESTS=str(self.root / 'requests'), PROBE_MODE='normal')
        env.start()
        self.addCleanup(env.stop)
        self.processes = []
        original = subprocess.Popen
        def spawn(*args, **kwargs):
            process = original(*args, **kwargs)
            self.processes.append(process)
            return process
        patch = mock.patch.object(models.subprocess, 'Popen', side_effect=spawn)
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self.assert_reaped)
        self.cache('cli-test')

    def assert_reaped(self):
        for process in self.processes:
            self.assertIsNotNone(process.returncode, 'child must be waited')
            with self.assertRaises(ProcessLookupError):
                os.kill(process.pid, 0)
            with self.assertRaises(ChildProcessError):
                os.waitpid(process.pid, os.WNOHANG)

    def cache(self, version):
        (self.home / 'models_cache.json').write_text(json.dumps({
            'client_version': version, 'models': [{'slug': 'cache-model', 'visibility': 'list',
                'supported_reasoning_levels': [{'effort': 'high'}]}]}))

    def test_cli_wins_and_paginates_without_reading_cache(self):
        (self.home / 'models_cache.json').write_text('{broken')
        self.assertEqual(models.check_codex_model('second', 'medium', '0', 'cli-test'), '命令行')
        requests = [json.loads(line) for line in (self.root / 'requests').read_text().splitlines()]
        self.assertEqual([r['method'] for r in requests], ['initialize', 'initialized', 'model/list', 'model/list'])
        self.assertEqual(requests[-1]['params']['cursor'], 'next')

    def test_timeout_partial_line_falls_back_and_reaps(self):
        os.environ['PROBE_MODE'] = 'timeout'
        start = time.monotonic()
        rows, source = models.codex_catalog('cli-test', timeout=0.2)
        self.assertLess(time.monotonic() - start, 3)
        self.assertEqual(source, '缓存（版本一致）')
        self.assertEqual(rows[0]['slug'], 'cache-model')

    def test_different_or_unknown_cache_version_only_warns(self):
        os.environ['PROBE_MODE'] = 'rpc_error'
        for cache_version, cli_version in [('app-test', 'cli-test'), ('unknown', 'unknown'), (None, 'cli-test')]:
            with self.subTest(cache_version=cache_version):
                self.cache(cache_version)
                warning = io.StringIO()
                with contextlib.redirect_stderr(warning):
                    self.assertEqual(models.check_codex_model('absent', 'other', '0', cli_version), '缓存（版本不同，未拦）')
                self.assertIn('仅提醒、不拦截', warning.getvalue())
        with self.assertRaisesRegex(ValueError, 'SUBAGENTS=1'):
            models.check_codex_model('absent', 'ultra', '0', 'cli-test')

    def test_model_and_effort_rejection_on_cli_and_matching_cache(self):
        for mode, model, level in [('normal', 'cli-model', 'medium'), ('rpc_error', 'cache-model', 'high')]:
            os.environ['PROBE_MODE'] = mode
            with self.assertRaisesRegex(ValueError, '不认识的模型.*可用：' + model):
                models.check_codex_model('absent', level, '0', 'cli-test')
            with self.assertRaisesRegex(ValueError, '不支持思考档位.*可用：' + level):
                models.check_codex_model(model, 'other', '0', 'cli-test')
        os.environ['PROBE_MODE'] = 'normal'
        with self.assertRaisesRegex(ValueError, '不认识的模型'):
            models.check_codex_model('hidden-model', 'medium', '0', 'cli-test')

    def test_protocol_errors_fall_back_and_empty_list_is_authoritative(self):
        for mode in ('malformed', 'bad_row', 'loop'):
            os.environ['PROBE_MODE'] = mode
            self.assertEqual(models.codex_catalog('cli-test')[1], '缓存（版本一致）')
        os.environ['PROBE_MODE'] = 'empty'
        with self.assertRaisesRegex(ValueError, '不认识的模型'):
            models.check_codex_model('cache-model', 'high', '0', 'cli-test')

    def test_discovery_uses_cli_model_names_and_public_levels(self):
        with mock.patch.object(models.config, 'load', return_value={}), \
                mock.patch.object(models.shutil, 'which', side_effect=lambda w: '/fake' if w == 'codex' else None), \
                mock.patch.object(models, 'records', return_value=[]):
            rows = models.discover()
        self.assertEqual([row['model'] for row in rows], ['cli-model', 'second'])
        self.assertTrue(all(row['levels'] == ['medium'] and row['source'] == 'app_server' for row in rows))


class RpcWriteBoundaryTests(unittest.TestCase):
    def test_partial_writes_and_would_block_preserve_json_requests(self):
        process = mock.Mock(pid=123)
        received = bytearray()
        calls = [0]
        def write(fd, data):
            calls[0] += 1
            if calls[0] == 1:
                raise BlockingIOError()
            count = min(3, len(data))
            received.extend(data[:count])
            return count
        response = b'{"id":1,"result":{}}\n{"id":2,"result":{"data":[],"nextCursor":null}}\n'
        with mock.patch.object(models.subprocess, 'Popen', return_value=process), \
                mock.patch.object(models.os, 'set_blocking'), \
                mock.patch.object(models.os, 'killpg'), \
                mock.patch.object(models.os, 'write', side_effect=write), \
                mock.patch.object(models.os, 'read', return_value=response), \
                mock.patch.object(models.selectors, 'DefaultSelector'):
            self.assertEqual(models.codex_cli_models(), [])
        requests = [json.loads(line) for line in received.splitlines()]
        self.assertEqual([r['method'] for r in requests], ['initialize', 'initialized', 'model/list'])
        process.wait.assert_called_once_with(timeout=0.2)

    def test_cleanup_errors_do_not_replace_protocol_error(self):
        process = mock.Mock(pid=123)
        process.wait.side_effect = subprocess.TimeoutExpired('fixture', 0.2)
        process.stdin.close.side_effect = OSError('fixture close')
        with mock.patch.object(models.subprocess, 'Popen', return_value=process), \
                mock.patch.object(models.os, 'set_blocking'), \
                mock.patch.object(models.os, 'killpg', side_effect=PermissionError), \
                mock.patch.object(models.os, 'write', side_effect=lambda fd, data: len(data)), \
                mock.patch.object(models.os, 'read', return_value=b'bad json\n'), \
                mock.patch.object(models.selectors, 'DefaultSelector'):
            with self.assertRaises(json.JSONDecodeError):
                models.codex_cli_models()
        process.stdout.close.assert_called_once()


class RpcWriteDeadlineTests(unittest.TestCase):
    def test_rpc_write_obeys_deadline(self):
        code = '''import json, sys, time
sys.stdin.readline()
print(json.dumps({'id': 1, 'result': {}}), flush=True)
sys.stdin.readline()
sys.stdin.readline()
print(json.dumps({'id': 2, 'result': {'data': [], 'nextCursor': 'x' * 300000}}), flush=True)
# 不再消费 stdin，模拟暂停读取的新一页请求；两秒后自退，保证复现有界。
time.sleep(2)
'''
        real_popen = subprocess.Popen
        def fake_popen(argv, **kwargs):
            return real_popen([sys.executable, '-c', code], **kwargs)
        started = time.monotonic()
        error = None
        with mock.patch.object(models.subprocess, 'Popen', side_effect=fake_popen):
            try:
                models.codex_cli_models(timeout=0.2)
            except Exception as exc:
                error = type(exc).__name__
        elapsed = time.monotonic() - started
        print(f'deadline=0.2s; elapsed={elapsed:.3f}s; exception={error}', flush=True)
        self.assertLess(elapsed, 1.5, '阻塞写管道绕过总查询时限')
        self.assertEqual(error, 'TimeoutError')




if __name__ == '__main__':
    unittest.main()
