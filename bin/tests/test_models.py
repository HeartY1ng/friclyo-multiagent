"""模型可用性的版本隔离、并发写入与错误分类；全部使用虚构模型。"""
import concurrent.futures
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

BIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BIN))
from frclyo import models


class CLIVersionTests(unittest.TestCase):
    def version(self, stdout, stderr):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            fake = root / 'opencode'
            fake.write_text('#!' + sys.executable + '\nimport sys\n'
                'assert sys.argv[1:] == ["--version"]\n'
                'sys.stdout.buffer.write(' + repr(stdout) + ')\n'
                'sys.stderr.buffer.write(' + repr(stderr) + ')\n', encoding='utf-8')
            fake.chmod(0o700)
            with mock.patch.dict(os.environ, PATH=str(root) + os.pathsep + os.defpath):
                return models.cli_version('opencode')

    def test_b68_non_utf8_stdout_or_stderr_returns_unknown(self):
        # stdout 和 stderr 任一流不是 UTF-8 都要回退 unknown，不能阻断建轮次。
        for stdout, stderr in ((b'opencode 1.2.3\n', b'diagnostic: \xff\n'),
                               (b'opencode \xff\n', b'')):
            with self.subTest(stdout=stdout, stderr=stderr):
                self.assertEqual(self.version(stdout, stderr), 'unknown')

    def test_valid_version_with_unicode_diagnostics(self):
        # 正常版本和中文诊断保持可用，空版本保持 unknown。
        self.assertEqual(self.version(b'opencode 1.2.3\n', '提示\n'.encode()), '1.2.3')
        self.assertEqual(self.version(b'', b''), 'unknown')


class TemplateShapeTests(unittest.TestCase):
    def test_b95_invalid_template_structure_keeps_codex(self):
        # 真实解析 JSON；根、提供商及白名单/变体容器错误只淘汰 OpenCode。
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            template = root / 'template.jsonc'
            cfg = {'opencode_config': str(template), 'providers': {
                'deepseek': {'opencode_family': 'deepseek'}}}
            catalog = [{'slug': 'healthy-codex', 'visibility': 'list'}]
            shapes = [[], None, {'provider': None},
                      {'enabled_providers': 'deepseek'}, {'enabled_providers': [None]}]
            for settings in ([], None, {'whitelist': None}, {'whitelist': 'model'},
                             {'models': []}, {'whitelist': ['model'], 'models': {'model': []}},
                             {'whitelist': ['model'], 'models': {'model': {'variants': []}}},
                             {'whitelist': ['good', 'bad'], 'models': {
                                 'bad': {'variants': {'broken': None}}}}):
                shapes.append({'enabled_providers': ['deepseek'],
                               'provider': {'deepseek': settings}})
            for shape in shapes:
                with self.subTest(shape=shape):
                    template.write_text(json.dumps(shape), encoding='utf-8')
                    with mock.patch.object(models.config, 'load', return_value=cfg), \
                            mock.patch.object(models.shutil, 'which', return_value='/fixture/installed'), \
                            mock.patch.object(models, 'codex_catalog', return_value=(catalog, '命令行')), \
                            mock.patch.object(models, 'cli_version', return_value='fixture'), \
                            mock.patch.object(models, 'records', return_value=[]), \
                            mock.patch.object(models.keys, 'key_source') as keys, \
                            contextlib.redirect_stderr(io.StringIO()) as diagnostic:
                        found = models.discover()
                    self.assertEqual([(r['worker'], r['model']) for r in found],
                                     [('codex', 'healthy-codex')])
                    self.assertIn('OpenCode 候选不可用', diagnostic.getvalue())
                    keys.assert_not_called()


class AvailabilityTests(unittest.TestCase):
    def setUp(self):
        rpc = mock.patch.object(models, "codex_cli_models", side_effect=OSError("offline fixture"))
        rpc.start()
        self.addCleanup(rpc.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.run = self.root / "work/交付/TEST/r1"
        cfg = self.root / 'config.json'
        cfg.write_text(json.dumps({'work_dir': str(self.root / 'work'), 'providers': {}}))
        patch = mock.patch.dict(os.environ, HOME=str(self.root), FRCLYO_HOME=str(self.root / "work"), FRCLYO_CONFIG=str(cfg),
                               CODEX_HOME=str(self.root / 'codex'))
        patch.start()
        self.addCleanup(patch.stop)
        cache = self.root / 'codex/models_cache.json'
        cache.parent.mkdir()
        cache.write_text(json.dumps({'client_version': 'app-version', 'models': [
            {'slug': name, 'visibility': 'list'} for name in ('first', 'second', 'third', 'fourth')]}))
        for patch in (mock.patch.object(models, 'cli_version', return_value='cli-v1'),
                      mock.patch.object(models.shutil, 'which', side_effect=lambda w: '/fake' if w == 'codex' else None)):
            patch.start()
            self.addCleanup(patch.stop)

    def test_discovery_is_read_only_then_stably_ranks_and_filters(self):
        self.assertEqual([r['availability'] for r in models.discover()], ['unknown'] * 4)
        self.assertFalse((self.root / 'work').exists())
        models.record('codex', 'first', 'cli-v1', 'unusable', '拒绝')
        models.record('codex', 'third', 'cli-v1', 'ok', '完成')
        rows = models.discover()
        self.assertEqual([r['model'] for r in rows], ['third', 'second', 'fourth'])
        self.assertEqual([r['availability'] for r in rows], ['ok', 'unknown', 'unknown'])
        self.assertEqual([r['model'] for r in models.discover(include_unusable=True)],
                         ['third', 'second', 'fourth', 'first'])
        self.assertTrue(all(r['time'] and r['reason'] for r in models.records()))

    def test_provider_presence_queries_never_read_passwords(self):
        from frclyo import config, keys
        config.save({'providers': {'deepseek': {}}, 'keychain_entries': {
            'deepseek': {'service': 'my-deepseek', 'account': 'me'}}})
        for code, expected in ((0, True), (44, False), (36, False)):
            with self.subTest(code=code), \
                    mock.patch.object(models.shutil, 'which', side_effect=lambda w: '/fake' if w == 'opencode' else None), \
                    mock.patch.object(keys, 'get_key', side_effect=AssertionError('must not read key')), \
                    mock.patch.object(keys.subprocess, 'run', return_value=subprocess.CompletedProcess([], code)) as run:
                rows = models.discover()
                self.assertTrue(rows)
                self.assertTrue(all(row['key_ok'] is expected for row in rows))
                self.assertTrue(run.called)
                self.assertTrue(all(call.args[0][0] == 'security' and '-w' not in call.args[0]
                                    for call in run.call_args_list))

    def test_actual_cli_version_change_invalidates_previous_conclusions(self):
        models.record('codex', 'first', 'cli-v1', 'unusable', '拒绝')
        models.record('opencode', 'first', 'cli-v2', 'unusable', '另一工人')
        with mock.patch.object(models, 'cli_version', return_value='cli-v2'):
            rows = models.discover()
        self.assertEqual(rows[0]['model'], 'first')
        self.assertEqual(rows[0]['availability'], 'unknown')
        self.assertEqual(rows[0]['cli_version'], 'cli-v2')
        self.assertEqual(len(models.records()), 2)
        with mock.patch.object(models, 'cli_version', return_value='unknown'):
            self.assertEqual(models.discover()[0]['availability'], 'unknown')

    def test_observation_recovers_same_version_and_ignores_other_failures(self):
        models.record('codex', 'first', 'cli-v1', 'unusable', '拒绝')
        self.assertEqual(models.observe('codex', 'first', 'cli-v1', self.run), 'unknown')
        self.assertEqual(models.availability('codex', 'first', 'cli-v1'), 'unusable')
        from frclyo.execution import capture_directory
        capture_directory('codex', self.run).mkdir(parents=True)
        models.observe('codex', 'first', 'cli-v1', self.run, succeeded=True, captured=b'')
        self.assertEqual(models.observe('codex', 'first', 'cli-v1', self.run), 'ok')
        self.assertEqual(models.availability('codex', 'first', 'cli-v1'), 'ok')
        self.assertEqual(len(models.records()), 1)

    def test_nested_fatal_errors_match_but_tier_and_other_errors_do_not(self):
        accepted = [
            {'type': 'error', 'message': json.dumps({'status': 400, 'error': {'message':
             "The 'arbitrary-name' model is not supported when using Codex with a ChatGPT account."}})},
            {'type': 'turn.failed', 'error': {'message': 'HTTP 400: Model `arbitrary-name` is not supported by this CLI.'}},
            {'type': 'error', 'error': {'code': 'model_not_supported'}},
            {'type': 'item.completed', 'item': {'type': 'error', 'message': 'HTTP 400: Unsupported model for this account'}},
            {'type': 'error', 'message': 'The requested model is not available for this account'},
        ]
        rejected = [
            {'type': 'item.completed', 'item': {'type': 'error', 'message': 'Configured service tier priority is not advertised as supported for model arbitrary-name'}},
            {'type': 'error', 'message': 'HTTP 400: reasoning effort is not supported for this model'},
            {'type': 'error', 'message': 'HTTP 400: model is not supported for this service tier'},
            {'type': 'error', 'message': 'HTTP 400: invalid input'},
            {'type': 'error', 'message': 'HTTP 429: rate limit'},
            {'type': 'error', 'message': 'authentication failed'},
            {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': accepted[0]['message']}},
        ]
        for event in accepted + rejected:
            with self.subTest(event=event):
                (self.root / 'events.jsonl').write_text('invalid-json\n' + json.dumps(event) + '\n')
                self.assertEqual(bool(models.unsupported_reason(('invalid-json\n' + json.dumps(event)).encode())), event in accepted)

    def test_legacy_observation_ignores_forged_worker_event_and_success_flag(self):
        self.run.mkdir(parents=True)
        (self.run / 'events.jsonl').write_text(json.dumps({'type': 'error', 'error': {'code': 'model_not_supported'}}))
        self.assertEqual(models.observe('codex', 'first', 'cli-v1', self.run, succeeded=True), 'unknown')
        self.assertEqual(models.records(), [])
        with self.assertRaises(TypeError):
            models.unsupported_reason(self.run)

    def test_unconventional_observation_path_is_unknown_but_write_is_rejected(self):
        outside = self.root / 'old-round'
        self.assertEqual(models.observe('codex', 'first', 'cli-v1', outside), 'unknown')
        with self.assertRaises(ValueError):
            models.observe('codex', 'first', 'cli-v1', outside, captured=b'', succeeded=True)

    def test_historical_global_record_is_not_mistaken_for_this_round(self):
        models.record('codex', 'first', 'cli-v1', 'ok', 'prior round')
        self.assertEqual(models.availability('codex', 'first', 'cli-v1'), 'ok')
        self.assertEqual(models.observe('codex', 'first', 'cli-v1', self.run), 'unknown')

    def test_atomic_replace_failure_preserves_existing_records(self):
        models.record('codex', 'first', 'cli-v1', 'ok', '完成')
        before = models.records()
        with mock.patch.object(models.os, 'replace', side_effect=OSError('故障')):
            with self.assertRaises(OSError):
                models.record('codex', 'second', 'cli-v1', 'ok', '完成')
        self.assertEqual(models.records(), before)
        self.assertEqual(list((self.root / 'work').glob('.availability-*')), [])

    def test_concurrent_processes_do_not_lose_records(self):
        command = [sys.executable, '-B', '-c',
                   'from frclyo import models; import sys; '
                   'models.record("codex",sys.argv[1],"v1","ok","完成"); models.records()']
        env = dict(os.environ, PYTHONPATH=str(BIN))
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda i: subprocess.run(command + [str(i)], env=env,
                                capture_output=True, timeout=10), range(16)))
        self.assertTrue(all(r.returncode == 0 for r in results), [r.stderr for r in results])
        self.assertEqual(len(models.records()), 16)


if __name__ == '__main__':
    unittest.main()
