"""版本与派活回归；tests/run_all.py 自动发现 board/test_*.py。"""
import json
import os
import importlib.util
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from regression_support import board_fixture, run_js, http

A, B, C = 'a' * 40, 'b' * 40, 'c' * 40
BATCH = '20261101-012735-3b6156d7aa48'


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


def journal(base, rows, batch=BATCH):
    path = base / '运行时/派活' / (batch + '.jsonl')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows), encoding='utf-8')
    return path


def row(task='CL-20261101-01', status='完成', **extra):
    return dict({'任务': task, '轮': 'r1', '工人': 'codex', '状态': status,
                 '用时秒': 12, '用量': {'input_tokens': 100, 'output_tokens': 20,
                                     'cached_input_tokens': 80, 'reasoning_output_tokens': 10}}, **extra)


def metadata(base, total=4, **extra):
    value = dict({'批次号': BATCH, '开始时间': '2026-11-01T20:00:00Z', '总件数': total,
                  '并行数': 3, '额度上限': 95,
                  '任务': [{'任务': 'CL-20261101-%02d' % (i + 1), '轮': 'r1'} for i in range(total)]}, **extra)
    write(base / '运行时/派活' / (BATCH + '.批次.json'), value)


def snapshots(versions=(), usage=(), issue=''):
    return lambda root, operation: {'rows': versions if operation == 'versions' else usage, 'issue': issue}


class ReleaseDispatchTests(unittest.TestCase):
    def test_iso_timestamps_accept_utc_offset_and_fractional_seconds(self):
        with board_fixture() as (b, base):
            metadata(base, 0, 开始时间='2026-11-01T20:00:00.125+00:00',
                     结束时间='2026-11-01T20:01:00+00:00', 结束状态='完成')
            data = b.dispatch_panel(str(base), {})['批次'][0]
            self.assertEqual(data['开始'], b.parse_iso('2026-11-01T20:00:00Z') + 0.125)
            self.assertEqual(data['结束'], b.parse_iso('2026-11-01T20:01:00Z'))
            self.assertEqual(data['计数']['在跑'], 0)
            for bad in (None, 123, 'bad', '2026-11-01T20:00:00'):
                self.assertIsNone(b.panel_timestamp(bad))

    def test_new_batch_counts_queue_without_claiming_process_liveness(self):
        with board_fixture() as (b, base):
            metadata(base)
            journal(base, [row(), row('CL-20261101-02', '失败'), row('CL-20261101-03', '额度跳过')])
            data = b.dispatch_panel(str(base), {})['批次'][0]
            self.assertEqual(data['总数'], 4)
            self.assertEqual({k: data['计数'][k] for k in ('完成', '失败', '跳过', '在跑')},
                             {'完成': 1, '失败': 1, '跳过': 1, '在跑': 1})
            self.assertTrue(data['在跑完整'])
            self.assertFalse(data['有结束记录'])
            self.assertEqual(data['开始'], b.parse_iso('2026-11-01T20:00:00Z'))
            self.assertIn('不能证明进程', data['提示'])

    def test_metadata_only_start_and_validation_failure(self):
        with board_fixture() as (b, base):
            metadata(base, 2)
            data = b.dispatch_panel(str(base), {})['批次'][0]
            self.assertEqual(data['计数']['在跑'], 2)
            self.assertEqual(data['已记录总数'], 0)
            self.assertEqual(len(data['任务']), 2)
            metadata(base, 2, 任务=[{'任务': None, '轮': None}] * 2,
                     结束时间='2026-11-01T20:01:00Z', 结束状态='校验失败')
            data = b.dispatch_panel(str(base), {})['批次'][0]
            self.assertEqual(data['总数'], 2)
            self.assertEqual(data['计数']['在跑'], 0)
            self.assertEqual(data['结束状态'], '校验失败')

    def test_finished_and_interrupted_batch_override_stale_running_round(self):
        for status in ('完成', '被中断'):
            with self.subTest(status=status), board_fixture() as (b, base):
                metadata(base, 2, 结束时间='2026-11-01T20:01:00Z', 结束状态=status)
                journal(base, [row()])
                tasks = {'CL-20261101-02': {'轮次': [{'轮次': 'r1', '状态': 'running'}]}}
                data = b.dispatch_panel(str(base), tasks)['批次'][0]
                self.assertEqual(data['计数']['在跑'], 0)
                self.assertTrue(data['在跑完整'])
                self.assertEqual(data['结束状态'], status)
                self.assertEqual(data['结束'], b.parse_iso('2026-11-01T20:01:00Z'))
                self.assertEqual(data['任务'][1]['状态'], '无结果（已结束）')

    def test_partial_end_record_never_claims_running(self):
        with board_fixture() as (b, base):
            metadata(base, 1, 结束时间=None)
            self.assertEqual(b.dispatch_panel(str(base), {})['批次'][0]['计数']['在跑'], 0)

    def test_bad_metadata_and_partial_results_are_unknown(self):
        with board_fixture() as (b, base):
            metadata(base, 2, 总件数=True)
            path = journal(base, [row()])
            data = b.dispatch_panel(str(base), {})['批次'][0]
            self.assertIsNone(data['总数'])
            self.assertFalse(data['在跑完整'])
            metadata(base, 2)
            with path.open('a') as stream:
                stream.write('{partial')
            data = b.dispatch_panel(str(base), {})['批次'][0]
            self.assertEqual(data['总数'], 2)
            self.assertIsNone(data['计数']['在跑'])
            self.assertFalse(data['在跑完整'])

    def test_recent_batch_union_reads_older_partner_file(self):
        with board_fixture() as (b, base):
            metadata(base, 1)
            path = journal(base, [row()])
            os.utime(path, (1, 1))
            for i in range(6):
                other = journal(base, [], '20261101-012735-%012x' % i)
                os.utime(other, (100 + i, 100 + i))
            batches = b.dispatch_panel(str(base), {})['批次']
            self.assertEqual(len(batches), 5)
            self.assertEqual(batches[0]['批次'], BATCH)
            self.assertEqual(batches[0]['计数']['在跑'], 0)
            self.assertEqual(batches[0]['计数']['完成'], 1)

    def test_version_health_and_publication_from_readonly_api(self):
        with board_fixture() as (b, base):
            (base / '框架').symlink_to('框架-版本/' + A)
            write(base / '运行时/发布' / (A + '.json'), {'published_at': '2026-11-01T20:00:00Z'})
            rows = [{'version': A, 'valid': True, 'rollbackable': True},
                    {'version': B, 'valid': False, 'rollbackable': False, 'deleting': True, 'problem': '<缺失>'}]
            with patch.object(b, 'release_snapshot', side_effect=snapshots(rows)):
                data = b.release_panel(str(base))
            self.assertEqual(data['当前版本']['时间'], b.parse_iso('2026-11-01T20:00:00Z'))
            self.assertTrue(data['当前版本']['可回滚'])
            broken = next(v for v in data['版本列表'] if v['提交'] == B[:7])
            self.assertFalse(broken['完整'])
            self.assertFalse(broken['可回滚'])
            self.assertEqual(broken['原因'], '<缺失>')
            self.assertTrue(broken['清理中'])
            self.assertIsNone(broken['时间'])

    def test_worker_usage_true_false_none_and_unknown_current(self):
        with board_fixture() as (b, base):
            (base / '框架').symlink_to('框架-版本/' + A)
            usage = [{'version': v, 'active': active, 'pid': i, 'task': 'task-' + str(i)}
                     for i, (v, active) in enumerate(((A, True), (B, True), (B, False), (C, None)), 1)]
            with patch.object(b, 'release_snapshot', side_effect=snapshots(usage=usage)):
                data = b.release_panel(str(base))
                self.assertEqual([w['PID'] for w in data['旧版本工人']], [2])
                self.assertEqual([w['PID'] for w in data['无法确认工人']], [4])
                (base / '框架').unlink()
                data = b.release_panel(str(base))
                self.assertIsNone(data['旧版本工人'])
                self.assertEqual([w['PID'] for w in data['无法确认工人']], [1, 2, 4])

    def test_worker_read_failure_is_unknown_not_empty(self):
        with board_fixture() as (b, base):
            with patch.object(b, 'release_snapshot', side_effect=snapshots(usage=None, issue='读取失败，未知')):
                data = b.release_panel(str(base))
            self.assertIsNone(data['旧版本工人'])
            self.assertIsNone(data['无法确认工人'])
            self.assertIn('未知', data['使用记录提示'])

    def test_async_probe_single_flight_and_cache_expiry(self):
        with board_fixture() as (b, base):
            entered, finish = threading.Event(), threading.Event()
            def slow(root, operation):
                entered.set()
                finish.wait(2)
                return {'rows': [], 'issue': ''}
            with patch.object(b, 'release_probe', side_effect=slow) as probe:
                try:
                    started = time.monotonic()
                    self.assertIsNone(b.release_snapshot(str(base), 'usage')['rows'])
                    self.assertLess(time.monotonic() - started, 0.5)
                    self.assertTrue(entered.wait(1))
                    for _ in range(20):
                        self.assertIsNone(b.release_snapshot(str(base), 'usage')['rows'])
                    self.assertEqual(probe.call_count, 1)
                finally:
                    finish.set()
                deadline = time.monotonic() + 2
                while b.release_snapshot(str(base), 'usage')['rows'] is None and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertEqual(b.release_snapshot(str(base), 'usage')['rows'], [])
                self.assertEqual(probe.call_count, 1)
                with b._release_lock:
                    b._release_cache[(str(base), 'usage')]['at'] -= 31
                entered.clear()
                b.release_snapshot(str(base), 'usage')
                self.assertTrue(entered.wait(1))
                self.assertEqual(probe.call_count, 2)

    def test_probe_exception_cached_and_does_not_break_state(self):
        with board_fixture() as (b, base):
            with patch.object(b, 'release_probe', side_effect=RuntimeError('unavailable')) as probe:
                b.release_snapshot(str(base), 'usage')
                deadline = time.monotonic() + 2
                while '正在' in b.release_snapshot(str(base), 'usage')['issue'] and time.monotonic() < deadline:
                    time.sleep(0.005)
                value = b.release_snapshot(str(base), 'usage')
                self.assertIsNone(value['rows'])
                self.assertEqual(value['issue'], '读取失败，未知')
                self.assertEqual(probe.call_count, 1)
                with patch.dict(b.CONFIG, {'work_dir': str(base)}):
                    code, body = http(b, '/api/state')
                self.assertEqual(code, 200)
                self.assertIsNone(json.loads(body)['版本与派活']['发布']['无法确认工人'])

    def test_probe_timeout_reaps_child_and_is_unknown(self):
        with board_fixture() as (b, base):
            real_probe = b.release_probe._mock_wraps
            popen = subprocess.Popen
            children = []
            def sleeping_child(*args, **kwargs):
                child = popen([sys.executable, '-c', 'import time; time.sleep(5)'], **kwargs)
                children.append(child)
                return child
            with patch.object(b.subprocess, 'Popen', side_effect=sleeping_child), patch.object(b, 'RELEASE_PROBE_TIMEOUT', 0.05):
                started = time.monotonic()
                value = real_probe(str(base), 'versions')
            self.assertLess(time.monotonic() - started, 1)
            self.assertIsNone(value['rows'])
            self.assertIn('超时', value['issue'])
            self.assertIsNotNone(children[0].poll())

    def test_readonly_helper_contract_and_exception_boundary(self):
        spec = importlib.util.spec_from_file_location('test_release_probe', Path(__file__).with_name('release_probe.py'))
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        with board_fixture() as (b, base):
            usage = [{'version': B, 'active': value} for value in (True, False, None)]
            with patch.object(helper.release, 'worker_usage', return_value=usage) as read:
                self.assertEqual([v['active'] for v in helper.read(str(base), 'usage')['rows']], [True, False, None])
                read.assert_called_once_with(base.resolve())
            with patch.object(helper.release, 'version_rows', return_value=([{'version': A, 'path': base}], '提示')):
                value = helper.read(str(base), 'versions')
                self.assertEqual(value['issue'], '提示')
                json.dumps(value)
            # 坏管理记录实际经辅助进程执行，异常不能被当成零使用记录。
            write(base / '运行时/发布/users/bad.json', {})
            value = b.release_probe._mock_wraps(str(base), 'usage')
            self.assertIsNone(value['rows'])
            self.assertEqual(value['issue'], '读取失败，未知')

    def test_release_link_is_authority_and_mtime_is_not_publication(self):
        with board_fixture() as (b, base):
            folder = base / '运行时/发布'
            write(folder / 'state.json', {'current': B, 'previous': B})
            write(folder / (A + '.json'), {'commit': A, 'accepted': True, 'acceptance': 'paid'})
            write(folder / (B + '.json'), {'commit': B, 'accepted': False, 'acceptance': 'verify'})
            os.utime(folder / (A + '.json'), (1700000000, 1700000000))
            (base / '框架').symlink_to('框架-版本/' + A)
            data = b.release_panel(str(base))
            self.assertEqual(data['当前版本']['提交'], A[:7])
            self.assertEqual(data['当前版本']['验收方式'], '付费验收')
            self.assertTrue(data['当前版本']['验收通过'])
            self.assertFalse(data['当前版本']['约'])
            self.assertIsNone(data['当前版本']['时间'])
            self.assertEqual(data['上一个版本'], B[:7])
            self.assertIn('不一致', data['提示'])
            self.assertEqual(sum(v['当前'] for v in data['版本列表']), 1)

    def test_missing_files_and_no_lease_do_not_create_files(self):
        with board_fixture() as (b, base):
            before = sorted(str(p) for p in base.rglob('*'))
            data = b.release_panel(str(base))
            self.assertIsNone(data['当前版本'])
            self.assertEqual(data['版本列表'], [])
            self.assertEqual(data['旧版本锁记录'], [])
            self.assertEqual(data['旧版本占用'], '未知')
            self.assertEqual(b.dispatch_panel(str(base), {})['批次'], [])
            self.assertEqual(before, sorted(str(p) for p in base.rglob('*')))

    def test_lease_presence_is_not_claimed_as_live_worker(self):
        with board_fixture() as (b, base):
            folder = base / '运行时/发布'
            folder.mkdir(parents=True)
            (base / '框架').symlink_to('框架-版本/' + A)
            (folder / (A + '.lease')).touch()
            (folder / (B + '.lease')).touch()
            data = b.release_panel(str(base))
            self.assertEqual(data['旧版本锁记录'], [B[:7]])
            self.assertEqual(data['旧版本占用'], '未知')

    def test_bad_metadata_does_not_hide_good_version(self):
        with board_fixture() as (b, base):
            folder = base / '运行时/发布'
            write(folder / (A + '.json'), {'accepted': True, 'acceptance': 'migration',
                                          'published_at': '2026-11-01T01:00:00Z'})
            (folder / (B + '.json')).write_text('{broken')
            write(folder / 'state.json', ['bad'])
            write(folder / (C + '.json'), {'accepted': 'yes', 'acceptance': ['bad']})
            data = b.release_panel(str(base))
            self.assertEqual(len(data['版本列表']), 3)
            good = next(v for v in data['版本列表'] if v['提交'] == A[:7])
            self.assertFalse(good['约'])
            self.assertEqual(good['验收方式'], '迁移保留')
            self.assertIsNone(next(v for v in data['版本列表'] if v['提交'] == C[:7])['验收通过'])

    def test_recent_limits_and_no_read_of_old_files(self):
        with board_fixture() as (b, base):
            folder = base / '运行时/发布'
            for index in range(15):
                path = folder / ('%040x.json' % index)
                write(path, {'accepted': True})
                os.utime(path, (1000 + index, 1000 + index))
            for index in range(8):
                path = journal(base, [row()], '20261101-012735-%012x' % index)
                os.utime(path, (1000 + index, 1000 + index))
            with patch.object(b, 'panel_json', wraps=b.panel_json) as read:
                versions = b.release_panel(str(base))['版本列表']
                self.assertEqual(len(versions), 10)
                self.assertEqual(read.call_count, 11)  # state + 最近 10 份
                self.assertNotIn(str(folder / ('%040x.json' % 0)), [c.args[0] for c in read.call_args_list])
            with patch.object(b, 'dispatch_rows', wraps=b.dispatch_rows) as read:
                batches = b.dispatch_panel(str(base), {})['批次']
                self.assertEqual(len(batches), 5)
                self.assertEqual(read.call_count, 5)
                self.assertEqual(batches[0]['批次'], '20261101-012735-000000000007')

    def test_normal_counts_token_semantics_and_chronological_quota(self):
        with board_fixture() as (b, base):
            rows = [row(额度前={'已用': 20, '时间': 30}, 额度后={'已用': 30, '时间': 50}),
                    row('CL-20261101-02', '失败', 额度前={'已用': 10, '时间': 10},
                        额度后={'已用': 25, '时间': 40}),
                    row('CL-20261101-03', '额度跳过', 用量=None),
                    row('CL-20261101-04', '被中断', 用量=None),
                    row('CL-20261101-05', 工人='opencode', 用量={'input': 10, 'output': 5,
                        'reasoning': 2, 'cached_input_tokens': 8, 'cache_write_input_tokens': 3}),
                    row('CL-20261101-06', 工人='opencode', 用量={'total': 40, 'input': 100})]
            journal(base, rows)
            data = b.dispatch_panel(str(base), {})['批次'][0]
            self.assertEqual(data['已记录总数'], 6)
            self.assertIsNone(data['总数'])
            self.assertEqual(data['计数'], {'完成': 3, '失败': 1, '跳过': 2, '在跑': 0, '未知': 0})
            self.assertEqual(data['tokens'], 308)
            self.assertEqual(data['用量缺失'], 2)
            self.assertEqual(data['额度前']['已用'], 10)
            self.assertEqual(data['额度后']['已用'], 30)

    def test_bad_lines_missing_fields_and_partial_append(self):
        with board_fixture() as (b, base):
            path = journal(base, [row(), {}, ['bad'], row('CL-20261101-02', 用量={'input_tokens': 'bad', 'output_tokens': 5})])
            with path.open('ab') as stream:
                stream.write(b'not json\n\xff\n{"unfinished":')
            data = b.dispatch_panel(str(base), {})['批次'][0]
            self.assertEqual(data['坏行'], 3)
            self.assertEqual(data['已记录总数'], 3)
            self.assertEqual(data['tokens'], 125)
            self.assertIn('末行', data['提示'])
            self.assertEqual(data['计数']['未知'], 1)

    def test_running_batch_joins_existing_round_and_refreshes(self):
        with board_fixture() as (b, base):
            task, rnd = 'CL-20261101-01-A', 'r-1'
            path = journal(base, [])
            log = path.parent / ('%s-%d-%s-%s.log' % (BATCH, len(task), task, rnd))
            log.touch()
            tasks = {task: {'轮次': [{'轮次': rnd, '状态': 'running', '工人': 'codex'}]}}
            data = b.dispatch_panel(str(base), tasks)['批次'][0]
            self.assertEqual(data['计数']['在跑'], 1)
            self.assertEqual(data['已记录总数'], 1)
            self.assertEqual(data['任务'][0]['轮'], rnd)
            journal(base, [row(task, 轮=rnd)])
            data = b.dispatch_panel(str(base), tasks)['批次'][0]
            self.assertEqual(data['计数']['在跑'], 0)
            self.assertEqual(data['计数']['完成'], 1)
            self.assertEqual(data['已记录总数'], 1)

    def test_empty_batch_and_unmatched_start_stay_unknown(self):
        with board_fixture() as (b, base):
            path = journal(base, [])
            data = b.dispatch_panel(str(base), {})['批次'][0]
            self.assertIsNone(data['tokens'])
            self.assertFalse(data['在跑完整'])
            self.assertIsNone(data['总数'])
            (path.parent / (BATCH + '-4-TEST-r1.log')).touch()
            data = b.dispatch_panel(str(base), {})['批次'][0]
            self.assertEqual(data['任务'][0]['状态'], '等待结果')
            self.assertEqual(data['计数']['在跑'], 0)

    def test_large_file_preserves_prefix_and_warns(self):
        with board_fixture() as (b, base):
            path = journal(base, [row(), row('CL-20261101-02')])
            first_size = len(path.read_bytes().splitlines(keepends=True)[0])
            with patch.object(b, 'PANEL_BATCH_BYTES', first_size + 10):
                data = b.dispatch_panel(str(base), {})['批次'][0]
            self.assertTrue(data['截断'])
            self.assertEqual(data['已记录总数'], 1)
            self.assertEqual(data['tokens'], 120)
            with patch.object(b, 'PANEL_ROW_LIMIT', 1):
                data = b.dispatch_panel(str(base), {})['批次'][0]
            self.assertTrue(data['截断'])

    def test_symlinks_special_files_and_external_entry_are_not_followed(self):
        with board_fixture() as (b, base):
            folder = base / '运行时/发布'
            write(base / 'outside.json', {'accepted': True})
            folder.mkdir(parents=True)
            (folder / (A + '.json')).symlink_to(base / 'outside.json')
            os.mkfifo(folder / (B + '.json'))
            (base / '框架').symlink_to(base / 'outside' / C)
            data = b.release_panel(str(base))
            self.assertEqual(data['版本列表'], [])
            self.assertIsNone(data['当前版本'])
            dispatch = base / '运行时/派活'
            dispatch.mkdir()
            (dispatch / (BATCH + '.jsonl')).symlink_to(base / 'outside.json')
            self.assertEqual(b.dispatch_panel(str(base), {})['批次'], [])

    def test_numeric_garbage_and_duplicate_rows(self):
        with board_fixture() as (b, base):
            bad = row(用量={'input_tokens': float('nan'), 'output_tokens': -1},
                      用时秒=True, 额度前={'已用': 200}, 额度后={'已用': '5'})
            journal(base, [bad, bad, row('CL-20261101-02', 批次='wrong')])
            data = b.dispatch_panel(str(base), {})['批次'][0]
            self.assertEqual(data['坏行'], 2)
            self.assertIsNone(data['tokens'])
            self.assertIsNone(data['额度前'])
            self.assertIsNone(data['任务'][0]['用时秒'])
            self.assertIsNone(b.panel_number(10 ** 1000))
            json.dumps(data, allow_nan=False)

    def test_overflow_usage_does_not_poison_json_response(self):
        with board_fixture() as (b, base):
            journal(base, [row(用量={'input_tokens': 1e308, 'output_tokens': 1e308}),
                           row('CL-20261101-02', 用量={'input_tokens': 1e308}),
                           row('CL-20261101-03', 用量={'input_tokens': 1e308})])
            data = b.dispatch_panel(str(base), {})['批次'][0]
            self.assertIsNone(data['任务'][0]['tokens'])
            self.assertIsNone(data['tokens'])
            json.dumps(data, allow_nan=False)
            self.assertIsNone(b.dispatch_quota({'已用': 10, '窗口分钟': 300}))

    def test_panel_in_state_and_http_keeps_short_commit(self):
        with board_fixture() as (b, base):
            with patch.dict(b.CONFIG, {'work_dir': str(base)}):
                (base / '框架').symlink_to('框架-版本/' + A)
                write(base / '运行时/发布' / (A + '.json'), {'accepted': True})
                code, body = http(b, '/api/state')
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(body)['版本与派活']['发布']['当前版本']['提交'], A[:7])

    def test_frontend_render_escape_expansion_and_refresh(self):
        result = run_js('release-dispatch', r'''
const data = {发布:{当前版本:{提交:'abcdef0',验收通过:true,验收方式:'付费验收',时间:1,约:true},
 版本列表:[{提交:'abcdef0',当前:true,验收通过:true,验收方式:'付费验收',时间:1,约:true}]},
 派活:{批次:[{批次:'batch-1',开始:1,已记录总数:1,总数:null,计数:{完成:0,失败:0,跳过:0,在跑:1},tokens:0,
 任务:[{任务:'<img src=x onerror=alert(1)>',轮:'r1',状态:'在跑',tokens:0}]}]}};
panelExpanded.add('batch:batch-1');
renderReleaseDispatch(data);
let html = elements['release-dispatch'].innerHTML;
assert(html.includes('abcdef0'));
assert(!html.includes('约 '));
assert(html.includes('release-version current'));
assert(html.includes('总数：未知（已记录 1）'));
assert(html.includes('至少 1'));
assert(html.includes('token 合计 0'));
assert(html.includes('data-panel-detail="batch:batch-1" open'));
assert(html.includes('&lt;img'));
assert(!html.includes('<img'));
data.派活.批次[0].计数 = {完成:1,在跑:0};
renderReleaseDispatch(data);
html = elements['release-dispatch'].innerHTML;
assert(html.includes('在跑 未知'));
assert(html.includes('data-panel-detail="batch:batch-1" open'));
const p = poll(); reply(0, {...state('全部'),版本与派活:data}); await p;
assert([...timers.values()].some(t => t.ms === 3000));
renderReleaseDispatch({});
assert(elements['release-dispatch'].innerHTML.includes('暂无派活批次'));
''')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_frontend_new_fields_unknowns_and_end_states(self):
        result = run_js('release-dispatch-metadata', r'''
const data = {发布:{当前版本:{提交:'aaa',时间:null,完整:false,可回滚:false,原因:'<broken>'},
 版本列表:[{提交:'bbb',时间:1793563200,完整:true,可回滚:true}],
 旧版本工人:[{提交:'bbb',工人:'codex',任务:'<task>',轮:'r1',PID:123}],
 无法确认工人:[{提交:'ccc',工人:'opencode',任务:'uncertain',轮:'r2',PID:456}]},
 派活:{批次:[{批次:'new-batch',总数:2,有批次信息:true,有结束记录:false,在跑完整:true,
 计数:{完成:1,失败:0,跳过:0,在跑:1},任务:[]}]}};
renderReleaseDispatch(data);
let html = elements['release-dispatch'].innerHTML;
for (const text of ['未知（旧记录）','不能回滚','残缺','可回滚','&lt;broken&gt;',
 '还在用旧版本的工人','&lt;task&gt;','PID 123','无法确认','uncertain','总数：2','在跑 1','没有结束记录']) assert(html.includes(text), text);
assert(!html.includes('修改时间'));
assert(!html.includes('至少 1'));
assert(!html.includes('<task>'));
for (const status of ['完成','被中断','校验失败']) {
 Object.assign(data.派活.批次[0], {有结束记录:true,结束:1793563260,结束状态:status,计数:{在跑:0}});
 renderReleaseDispatch(data);
 html = elements['release-dispatch'].innerHTML;
 assert(html.includes(status)); assert(html.includes('在跑 0'));
 assert(!html.includes('没有结束记录'));
}
data.发布.旧版本工人 = null; data.发布.无法确认工人 = null;
data.发布.使用记录提示 = '读取失败，未知';
renderReleaseDispatch(data);
html = elements['release-dispatch'].innerHTML;
assert(html.includes('读取失败，未知'));
assert(!html.includes('暂无匹配的登记记录'));
''')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
