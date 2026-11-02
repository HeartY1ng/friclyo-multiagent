"""B118/B119/B120/B124/B85/B126：离线目标回归，不依赖真实模型或账本。"""
import contextlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_dispatch as fixtures
from frclyo import dispatch, models


class ReliabilityTests(unittest.TestCase):
    setUp = fixtures.DispatchTests.setUp
    job = fixtures.DispatchTests.job
    run_jobs = fixtures.DispatchTests.run_jobs
    rows = fixtures.DispatchTests.rows
    events = fixtures.DispatchTests.events
    child = fixtures.DispatchTests.child

    def catalog(self):
        return [{'slug': name, 'visibility': 'list', 'supported_reasoning_levels': [
            {'effort': level} for level in levels]} for name, levels in {
                'fixture': ['high'], 'gpt-6-astra': ['high', 'ultra'],
                'gpt-5.6-luna': ['low', 'high', 'ultra'],
                'gpt-5.6-sol': ['high'], 'gpt-5.6-terra': ['high']}.items()]

    def metadata(self):
        paths = list((self.root / '运行时/派活').glob('*.批次.json'))
        self.assertEqual(len(paths), 1)
        return json.loads(paths[0].read_text())

    def test_model_invalid_codex_model_and_level_report_item(self):
        jobs = [dict(self.job(), 模型='unknown'), dict(self.job(), 档位='nonsense')]
        with mock.patch.object(models, 'codex_cli_models', return_value=self.catalog()) as query:
            self.assertEqual(self.run_jobs(jobs, '--试跑'), 2)
        self.assertEqual(query.call_count, 1)
        for text in ('第 1 项', 'unknown', '第 2 项', 'nonsense'):
            self.assertIn(text, self.stderr.getvalue())
        self.assertFalse(self.events())
        self.assertFalse((self.root / '运行时').exists())

    def test_model_opencode_own_whitelist_and_variants(self):
        template = self.base / 'catalog.json'
        template.write_text(json.dumps({'enabled_providers': ['deepseek'], 'provider': {
            'deepseek': {'whitelist': ['model'], 'models': {'model': {
                'variants': {'nothink': {}, 'high': {'disabled': True}}}}}}}))
        cfg = dict(self.cfg, opencode_config=str(template))
        job = self.job('opencode')
        for model, level, expected in (('deepseek/unknown', '', 2), ('deepseek/model', 'high', 2),
                                       ('deepseek/model', 'nonsense', 2), ('deepseek/model', '', 0),
                                       ('deepseek/model', 'nothink', 0)):
            with self.subTest(model=model, level=level), mock.patch.object(dispatch.config, 'load', return_value=cfg):
                self.assertEqual(self.run_jobs([dict(job, 模型=model, 档位=level)], '--试跑'), expected)
                if expected == 2:
                    self.assertIn('第 1 项', self.stderr.getvalue())
        self.assertFalse((self.root / '运行时').exists())

    def test_model_unavailable_warns_without_blocking_or_cache(self):
        job = self.job()
        with mock.patch.object(models, 'codex_cli_models', side_effect=OSError('fixture')):
            self.assertEqual(self.run_jobs([job], '--试跑'), 0)
        self.assertIn('警告', self.stderr.getvalue())
        self.assertIn('codex', self.stderr.getvalue())
        oc = self.job('opencode')
        with mock.patch.object(dispatch.config, 'load', return_value=dict(self.cfg, opencode_config=str(self.base / 'missing'))):
            self.assertEqual(self.run_jobs([oc], '--试跑'), 0)
        self.assertIn('警告', self.stderr.getvalue())
        self.assertIn('opencode', self.stderr.getvalue())

    def test_model_valid_and_ultra_requires_subagents(self):
        job = self.job(model='gpt-6-astra')
        with mock.patch.object(models, 'codex_cli_models', return_value=self.catalog()), \
                mock.patch.object(models, 'cli_version', return_value='0.154.0'):
            self.assertEqual(self.run_jobs([job], '--试跑'), 0)
            self.assertEqual(self.run_jobs([dict(job, 档位='ultra')], '--试跑'), 2)
            self.assertIn('ultra', self.stderr.getvalue())
            self.assertEqual(self.run_jobs([dict(job, 档位='ultra', 子agent=1)], '--试跑'), 0)

    def test_subagent_version_model_force_matrix(self):
        job = self.job()
        cases = [(version, model, enabled, force, expected)
                 for version in ('0.153.0', '0.153.9', '0.153.1-alpha.1', '0.154.0', '0.152.9', 'unknown')
                 for model in ('gpt-6-astra', 'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna')
                 for enabled, force in ((1, '0'), (0, '0'), (1, '1'))
                 for expected in (2 if version.startswith('0.153.') and model != 'gpt-5.6-luna'
                                  and enabled == 1 and force == '0' else 0,)]
        with mock.patch.object(models, 'codex_cli_models', return_value=self.catalog()):
            for version, model, enabled, force, expected in cases:
                with self.subTest(version=version, model=model, enabled=enabled, force=force), \
                        mock.patch.object(models, 'cli_version', return_value=version), \
                        mock.patch.dict(os.environ, CODEX_WORKER_SUBAGENTS_FORCE=force):
                    self.assertEqual(self.run_jobs([dict(job, 模型=model, 子agent=enabled)], '--试跑'), expected)
                    if expected == 2:
                        for text in ('第 1 项', 'no thread with id', 'CODEX_WORKER_SUBAGENTS_FORCE=1'):
                            self.assertIn(text, self.stderr.getvalue())
        self.assertFalse(self.events())

    def test_subagent_guard_still_applies_when_catalog_unavailable(self):
        job = dict(self.job(model='gpt-6-astra'), 子agent=1)
        with mock.patch.object(models, 'codex_cli_models', side_effect=OSError('fixture')), \
                mock.patch.object(models, 'cli_version', return_value='0.153.2'), \
                mock.patch.dict(os.environ, CODEX_WORKER_SUBAGENTS_FORCE='0'):
            self.assertEqual(self.run_jobs([job], '--试跑'), 2)
            self.assertIn('no thread with id', self.stderr.getvalue())

    def test_quota_after_latches_across_reset_and_opencode_continues(self):
        jobs = [self.job(), self.job(), self.job('opencode')]
        readings = []
        def quota(cfg):
            used = (94, 95, 2)[min(len(readings), 2)]
            readings.append(used)
            return {'已用': used}
        self.assertEqual(self.run_jobs(jobs, '--并行', '1', quota=quota), 1)
        self.assertEqual(readings, [94, 95])
        self.assertEqual([row['状态'] for row in self.rows()], ['完成', '额度跳过', '完成'])
        self.assertEqual([row['task'] for row in self.events() if row['kind'] == 'start'],
                         [jobs[0]['任务'], jobs[2]['任务']])

    def test_queue_json_syntax_is_validation_not_runtime_error(self):
        self.queue.write_text('[{"任务":"bad",}]')
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            self.assertEqual(dispatch.main([str(self.queue), '--试跑']), 2)
        self.assertIn('JSON', out.getvalue())
        self.assertFalse((self.root / '运行时').exists())
        self.queue.unlink()
        with contextlib.redirect_stderr(out):
            self.assertEqual(dispatch.main([str(self.queue), '--试跑']), 2)
        self.assertIn('队列文件不存在：%s' % self.queue, out.getvalue())

    def test_batch_start_finish_and_unchanged_jsonl_columns(self):
        job = self.job('opencode', delay=0.4)
        child = self.child([job])
        try:
            deadline = time.monotonic() + 5
            while not self.events() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(self.events(), 'fixture worker did not start')
            before = self.metadata()
            self.assertEqual(before['总件数'], 1)
            self.assertEqual(before['并行数'], 1)
            self.assertEqual(before['额度上限'], 95)
            self.assertEqual(before['任务'], [{'任务': job['任务'], '轮': 'r1'}])
            self.assertIn('开始时间', before)
            self.assertNotIn('结束时间', before)
            self.assertNotIn('结束状态', before)
            stdout, stderr = child.communicate(timeout=5)
            self.assertEqual(child.returncode, 0, stderr)
            after = self.metadata()
            self.assertEqual(after['结束状态'], '完成')
            self.assertGreaterEqual(after['结束时间'], before['开始时间'])
            self.assertEqual({key: after[key] for key in before}, before)
            row = self.rows()[0]
            self.assertEqual(set(row), {'任务', '轮', '工人', '模型', '档位', '状态', '退出码', '用时秒',
                '回执状态', '用量', '额度前', '额度后', '批次', '批次被中断'})
            self.assertEqual(row['批次'], before['批次号'])
            self.assertFalse(list((self.root / '运行时/派活').glob('.*.批次.json-*')))
        finally:
            try:
                child.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate()
            deadline = time.monotonic() + 5
            while self.events() and self.events()[-1]['kind'] != 'end' and time.monotonic() < deadline:
                time.sleep(0.01)

    def test_batch_sigterm_waits_records_all_jobs_and_finishes_metadata(self):
        jobs = [self.job('opencode', delay=0.4), self.job('opencode')]
        child = self.child(jobs)
        try:
            deadline = time.monotonic() + 5
            while not self.events() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(self.events(), 'fixture worker did not start')
            child.send_signal(signal.SIGTERM)
            stdout, stderr = child.communicate(timeout=5)
            self.assertEqual(child.returncode, 130, stderr)
            self.assertEqual([e['kind'] for e in self.events()], ['start', 'end'])
            self.assertEqual({row['状态'] for row in self.rows()}, {'完成', '被中断'})
            self.assertTrue(all(row['批次被中断'] for row in self.rows()))
            self.assertIn('（被中断）', stdout)
            self.assertEqual(self.metadata()['结束状态'], '被中断')
            self.assertIn('结束时间', self.metadata())
        finally:
            try:
                child.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate()
            deadline = time.monotonic() + 5
            while self.events() and self.events()[-1]['kind'] != 'end' and time.monotonic() < deadline:
                time.sleep(0.01)

    def test_batch_validation_failure_has_metadata_but_no_worker_or_journal(self):
        job = dict(self.job(), 超时=0)
        self.assertEqual(self.run_jobs([job]), 2)
        info = self.metadata()
        self.assertEqual(info['总件数'], 1)
        self.assertEqual(info['任务'], [{'任务': job['任务'], '轮': 'r1'}])
        self.assertEqual(info['结束状态'], '校验失败')
        self.assertIn('结束时间', info)
        self.assertFalse(self.rows())
        self.assertFalse(self.events())

    def test_metadata_atomic_failure_preserves_previous_complete_file(self):
        self.assertEqual(self.run_jobs([]), 0)
        info = self.metadata()
        parent_path = self.root / '运行时/派活'
        with dispatch.directory(parent_path) as parent, \
                mock.patch.object(dispatch.os, 'replace', side_effect=PermissionError('synthetic')):
            with self.assertRaises(PermissionError):
                dispatch.save_batch(parent, dict(info, 结束状态='被中断'))
        self.assertEqual(self.metadata(), info)
        self.assertFalse(list(parent_path.glob('.*.批次.json-*')))

    def test_metadata_invalid_identity_is_null_and_validation_exit_stays_two(self):
        job = dict(self.job(), 任务=float('nan'), 轮=[])
        self.assertEqual(self.run_jobs([job]), 2)
        self.assertEqual(self.metadata()['任务'], [{'任务': None, '轮': None}])
        self.assertFalse(self.events())

    def test_batch_empty_queue_and_signal_handlers_restored(self):
        before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        self.assertEqual(self.run_jobs([]), 0)
        self.assertEqual(self.metadata()['总件数'], 0)
        self.assertEqual(self.metadata()['结束状态'], '完成')
        self.assertEqual({sig: signal.getsignal(sig) for sig in before}, before)


if __name__ == '__main__':
    unittest.main(verbosity=2)
