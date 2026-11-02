"""派活离线回归：只用 resolve 后的临时根、假账本、假 CLI 与短命假工人。"""
import contextlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

BIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BIN))
from frclyo import config, dispatch, ledger


# 假工人只读测试生成的卡，按真实回执结构写结果；不调用实际 worker 或模型。
WORKER = r'''
import fcntl, json, os, sys, time
from pathlib import Path
task, rnd, card, model, level, timeout = sys.argv[1:]
options = json.loads(Path(card).read_text())
root = Path(os.environ['FRCLYO_HOME'])
worker = Path(sys.argv[0]).name.split('-')[0]
run = root / '交付' / task / rnd
run.mkdir(parents=True)
def event(kind):
    with open(root / 'events.jsonl', 'a') as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        stream.write(json.dumps({'kind': kind, 'task': task, 'round': rnd, 'time': time.monotonic(),
            'worker': worker, 'model': model, 'pid': os.getpid(),
            'switches': {key: os.environ.get(key) for key in
                ['CODEX_WORKER_FAST', 'CODEX_WORKER_WEB', 'CODEX_WORKER_NET', 'CODEX_WORKER_SUBAGENTS']}}) + '\n')
        stream.flush()
event('start')
time.sleep(options.get('delay', 0.15))
receipt = {'任务编号': task, '轮次编号': rnd, '工人': worker, '模型': model,
    '退出码': options.get('rc', 0), '状态': options.get('status', '执行完成'),
    '用量': options.get('usage', {'input_tokens': 100, 'cached_input_tokens': 80,
        'output_tokens': 10, 'reasoning_output_tokens': 5} if worker == 'codex' else
        {'input': 10, 'output': 20, 'reasoning': 5, 'cached_input_tokens': 30, 'cache_write_input_tokens': 2})}
mode = options.get('receipt', 'normal')
if mode == 'wrong': receipt['轮次编号'] = 'old'
if mode != 'missing':
    (run / '回执.json').write_text('[]' if mode == 'array' else json.dumps(receipt))
if mode == 'trusted':
    control = root / '运行时/控制' / worker / task / rnd
    control.mkdir(parents=True)
    receipt['状态'] = '失败'
    (control / 'receipt.json').write_text(json.dumps(receipt))
event('end')
sys.exit(options.get('rc', 0))
'''

# 假额度服务严格检查握手；只走本地管道，还能模拟半行和 RPC 错误。
QUOTA = r'''
import json, os, sys, time
if sys.argv[1:] == ['--version']:
    print('codex-cli ' + os.environ.get('FAKE_CODEX_VERSION', '0.154.0')); sys.exit(0)
assert sys.argv[1:] == ['app-server']
assert json.loads(sys.stdin.readline())['method'] == 'initialize'
print(json.dumps({'id': 1, 'result': {}}), flush=True)
assert json.loads(sys.stdin.readline())['method'] == 'initialized'
request = json.loads(sys.stdin.readline())
if request['method'] == 'model/list':
    print(json.dumps({'id': request['id'], 'result': {'data': [
        {'model': name, 'hidden': False, 'supportedReasoningEfforts': [{'reasoningEffort': level} for level in levels]}
        for name, levels in {'fixture': ['high'], 'gpt-6-astra': ['high', 'ultra'],
                            'gpt-5.6-luna': ['low', 'high', 'ultra'], 'gpt-5.6-sol': ['high'],
                            'gpt-5.6-terra': ['high']}.items()]}}), flush=True)
    sys.exit(0)
assert request['method'] == 'account/rateLimits/read'
mode = os.environ.get('FAKE_QUOTA', 'normal')
if mode == 'stall':
    sys.stdout.write('{'); sys.stdout.flush(); time.sleep(3)
elif mode == 'error':
    print(json.dumps({'id': 2, 'error': {'message': 'do not echo this'}}), flush=True)
else:
    print(json.dumps({'method': 'notification'}), flush=True)
    print(json.dumps({'id': 2, 'result': {'rateLimits': {
        'primary': {'usedPercent': 99, 'windowDurationMins': 300},
        'secondary': {'usedPercent': 12, 'windowDurationMins': 10080, 'resetsAt': 1900000000}}}}), flush=True)
'''


class DispatchTests(unittest.TestCase):
    def setUp(self):
        # 每例隔离环境；CLI 查额度的 PATH 第一项永远是本例替身。
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / 'data'
        self.fakebin = self.base / 'bin'
        self.fakebin.mkdir()
        for name, source in (('codex-worker.sh', WORKER), ('opencode-worker.sh', WORKER), ('codex', QUOTA)):
            path = self.fakebin / name
            path.write_text('#!' + sys.executable + '\n' + source)
            path.chmod(0o700)
        self.root.mkdir()
        (self.root / '任务卡').mkdir()
        (self.root / '素材').mkdir()
        cfg_path = self.base / 'config.json'
        template = self.base / 'opencode.json'
        template.write_text(json.dumps({'enabled_providers': ['deepseek', 'xiaomi'],
            'provider': {family: {'whitelist': names, 'models': {name: {'variants': {
                'high': {}, 'low': {'disabled': True}, 'nothink': {}}} for name in names}}
                for family, names in [('deepseek', ['model', 'one', 'two']), ('xiaomi', ['three'])]}}))
        cfg_path.write_text(json.dumps({'opencode_config': str(template)}))
        patch = mock.patch.dict(os.environ, HOME=str(self.base), FRCLYO_HOME=str(self.root),
            FRCLYO_CONFIG=str(cfg_path), CODEX_HOME=str(self.base / 'fake-codex'),
            PATH=str(self.fakebin) + os.pathsep + os.defpath, FAKE_QUOTA='normal')
        patch.start()
        self.addCleanup(patch.stop)
        self.cfg = config.load()
        self.queue = self.base / 'queue.json'
        self.next_id = 0

    def job(self, worker='codex', model=None, **options):
        # 直接写合法开卡事件，不调用外部账本；不依赖本机日期和已有任务。
        self.next_id += 1
        task = 'CL-20261101-%02d' % self.next_id
        with open(self.root / '账本.jsonl', 'a') as stream:
            stream.write(json.dumps(ledger.event('开卡', task, 标题='fixture', 项目='fixture',
                来源='CL', 派活人='fixture', 父任务='')) + '\n')
        (self.root / '任务卡' / (task + '-r1.txt')).write_text(json.dumps(options))
        (self.root / '素材' / task).mkdir()
        return {'任务': task, '工人': worker, '模型': model or ('fixture' if worker == 'codex' else 'deepseek/model'),
                '档位': 'high', '超时': 10}

    def run_jobs(self, jobs, *args, quota=None):
        self.queue.write_text(json.dumps(jobs))
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(dispatch, 'BIN', self.fakebin), \
                mock.patch.object(dispatch, 'read_quota', side_effect=quota or (lambda cfg: {'已用': 10})), \
                contextlib.redirect_stdout(self.stdout), contextlib.redirect_stderr(self.stderr):
            code = dispatch.main([str(self.queue), *args])
        return code

    def rows(self):
        paths = list((self.root / '运行时/派活').glob('*.jsonl'))
        return [json.loads(line) for path in paths for line in path.read_text().splitlines()]

    def test_missing_queue_reports_path_and_returns_validation_error(self):
        missing = self.base / 'missing-queue.json'
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(dispatch, 'read_quota', side_effect=lambda cfg: self.fail('quota queried')), \
                contextlib.redirect_stdout(self.stdout), contextlib.redirect_stderr(self.stderr):
            code = dispatch.main([str(missing)])
        self.assertEqual(code, 2)
        self.assertIn('队列文件不存在：%s' % missing, self.stderr.getvalue())

    def events(self):
        path = self.root / 'events.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_dry_run_defaults_and_no_side_effects(self):
        job = self.job()
        original = (self.root / '账本.jsonl').read_bytes()
        self.assertEqual(self.run_jobs([job], '--试跑', quota=lambda cfg: self.fail('dry run queried quota')), 0)
        plan = json.loads(self.stdout.getvalue().splitlines()[1])
        self.assertEqual(plan['轮'], 'r1')
        self.assertEqual(plan['快速档'], 0)
        self.assertEqual(plan['卡'], str(self.root / '任务卡' / (job['任务'] + '-r1.txt')))
        self.assertEqual((self.root / '账本.jsonl').read_bytes(), original)
        self.assertFalse((self.root / '运行时').exists())
        self.assertFalse((self.root / '交付').exists())

    def test_whole_batch_rejects_all_errors(self):
        good = self.job()
        missing = self.job()
        (self.root / '任务卡' / (missing['任务'] + '-r1.txt')).unlink()
        occupied = self.job()
        target = self.root / '交付' / occupied['任务'] / 'r1'
        target.parent.mkdir(parents=True)
        target.symlink_to(self.base / 'absent')
        before = (self.root / '账本.jsonl').read_bytes()
        jobs = [good, good, missing, occupied, dict(good, 任务='CL-20261101-99'), dict(good, 轮='../r2'), []]
        self.assertEqual(self.run_jobs(jobs), 2)
        for reason in ('重复', '卡文件不存在', '轮次路径', '未在账本开卡', '任务/轮', '必须是对象'):
            self.assertIn(reason, self.stderr.getvalue())
        self.assertFalse(self.events())
        self.assertEqual((self.root / '账本.jsonl').read_bytes(), before)
        self.assertFalse(self.rows())
        info = json.loads(next((self.root / '运行时/派活').glob('*.批次.json')).read_text())
        self.assertEqual(info['结束状态'], '校验失败')

    def test_bad_types_and_unsupported_switches(self):
        job = self.job()
        for field, value in (('工人', []), ('模型', {}), ('超时', True), ('轮', 2), ('档位', None),
                             ('快速档', '1'), ('网页', 2), ('卡', ''), ('输入', '/does-not-exist')):
            with self.subTest(field=field):
                self.assertEqual(self.run_jobs([dict(job, **{field: value})], '--试跑'), 2)
        oc = self.job('opencode')
        self.assertEqual(self.run_jobs([dict(oc, 命令网络=1)]), 2)
        self.assertIn('不支持', self.stderr.getvalue())
        self.assertEqual(self.run_jobs([dict(job, 变体='low')]), 2)

    def test_closed_task_rejected_without_reopening(self):
        job = self.job()
        with open(self.root / '账本.jsonl', 'a') as stream:
            stream.write(json.dumps(ledger.event('完成', job['任务'], 结论='通过', 摘要='fixture')) + '\n')
        self.assertEqual(self.run_jobs([job]), 2)
        self.assertIn('已收卡', self.stderr.getvalue())

    def test_parallelism_provider_serialization_and_usage(self):
        jobs = [self.job('opencode', 'deepseek/one', delay=0.35),
                self.job('opencode', 'deepseek/two', delay=0.1),
                self.job('opencode', 'xiaomi/three', delay=0.35), self.job(delay=0.35)]
        self.assertEqual(self.run_jobs(jobs), 0)
        active, families, peak = set(), set(), 0
        starts = []
        # macOS 旧版 Python 的 monotonic 起点可能因进程而异；按持锁追加顺序验证重叠。
        for event in self.events():
            family = event['model'].split('/')[0] if event['worker'] == 'opencode' else None
            if event['kind'] == 'start':
                starts.append(event['task'])
                active.add(event['task'])
                if family:
                    self.assertNotIn(family, families, self.events())
                    families.add(family)
                peak = max(peak, len(active))
            else:
                active.remove(event['task'])
                if family:
                    families.remove(family)
        self.assertEqual(peak, 3)
        self.assertLess(starts.index(jobs[2]['任务']), starts.index(jobs[1]['任务']))
        rows = self.rows()
        self.assertEqual(len(rows), 4)
        self.assertEqual(sum(dispatch.token_count(row) for row in rows), 311)
        self.assertIn('完成 4 | 失败 0', self.stdout.getvalue())
        cards = ledger.fold(ledger.read_events(self.root / '账本.jsonl'))
        for card in cards.values():
            self.assertEqual(card['状态'], '开卡')
            self.assertEqual(card['事件'][-1]['内容'], '批次 %s 派出 r1' % rows[0]['批次'])
        self.assertTrue(all(row['额度前'] is None for row in rows if row['工人'] == 'opencode'))

    def test_quota_stop_is_latched_but_opencode_continues(self):
        jobs = [self.job(delay=0.25), self.job(), self.job('opencode'), self.job()]
        values = iter([{'已用': 94}, {'已用': 95}, {'已用': 2}])
        self.assertEqual(self.run_jobs(jobs, quota=lambda cfg: next(values)), 1)
        rows = {row['任务']: row for row in self.rows()}
        self.assertEqual(rows[jobs[0]['任务']]['状态'], '完成')
        self.assertEqual(rows[jobs[0]['任务']]['额度后']['已用'], 2)
        self.assertEqual(rows[jobs[1]['任务']]['状态'], '额度跳过')
        self.assertEqual(rows[jobs[3]['任务']]['状态'], '额度跳过')
        self.assertEqual(rows[jobs[2]['任务']]['状态'], '完成')
        self.assertEqual(len(self.events()), 4)
        cards = ledger.fold(ledger.read_events(self.root / '账本.jsonl'))
        self.assertEqual(len(cards[jobs[1]['任务']]['事件']), 1)

    def test_unknown_quota_is_failure_and_does_not_block_opencode(self):
        jobs = [self.job(), self.job('opencode')]
        self.assertEqual(self.run_jobs(jobs, quota=lambda cfg: {'错误': 'fixture'}), 1)
        self.assertEqual([row['状态'] for row in self.rows()], ['失败', '完成'])
        self.assertEqual(len(self.events()), 2)

    def test_worker_failure_missing_bad_and_trusted_receipts(self):
        jobs = [self.job('opencode', rc=7), self.job('opencode', receipt='missing'),
                self.job('opencode', receipt='array'), self.job('opencode', receipt='wrong'),
                self.job('opencode', receipt='trusted'), self.job('opencode')]
        self.assertEqual(self.run_jobs(jobs), 1)
        self.assertEqual([row['状态'] for row in self.rows()], ['失败'] * 5 + ['完成'])
        self.assertEqual(self.rows()[0]['退出码'], 7)
        self.assertEqual(self.rows()[4]['回执状态'], '失败')

    def test_switches_explicitly_override_inherited_defaults(self):
        job = dict(self.job(), 网页=1, 子agent=1, 命令网络=1)
        with mock.patch.dict(os.environ, CODEX_WORKER_FAST='1'):
            self.assertEqual(self.run_jobs([job]), 0)
        self.assertEqual(self.events()[0]['switches'], {'CODEX_WORKER_FAST': '0', 'CODEX_WORKER_WEB': '1',
            'CODEX_WORKER_NET': '1', 'CODEX_WORKER_SUBAGENTS': '1'})

    def test_launch_failure_is_recorded_and_other_jobs_continue(self):
        jobs = [self.job(), self.job('opencode')]
        (self.fakebin / 'codex-worker.sh').unlink()
        self.assertEqual(self.run_jobs(jobs), 1)
        self.assertEqual([row['状态'] for row in self.rows()], ['失败', '完成'])

    def test_changed_round_after_validation_does_not_launch(self):
        job = self.job()
        def occupy(cfg):
            (self.root / '交付' / job['任务'] / 'r1').mkdir(parents=True)
            return {'已用': 10}
        self.assertEqual(self.run_jobs([job], quota=occupy), 1)
        self.assertFalse(self.events())
        self.assertEqual(self.rows()[0]['状态'], '失败')

    def test_interruption_during_quota_does_not_launch_or_note(self):
        # 模拟额度查询期间按 Ctrl-C；返回额度后必须再次检查停派标记。
        job = self.job()
        def interrupt(cfg):
            os.kill(os.getpid(), signal.SIGINT)
            return {'已用': 10}
        self.assertEqual(self.run_jobs([job], quota=interrupt), 130)
        self.assertEqual(self.rows()[0]['状态'], '被中断')
        self.assertFalse(self.events())
        self.assertEqual(len(ledger.read_events(self.root / '账本.jsonl')), 1)

    def test_bad_provider_lock_does_not_abandon_started_workers(self):
        # 注入锁故障；已启动的其他工人仍应完成、留回执并进入汇总。
        jobs = [self.job(delay=0.2), self.job('opencode')]
        with mock.patch.object(dispatch, 'provider_lock', side_effect=ValueError('fixture')):
            self.assertEqual(self.run_jobs(jobs), 1)
        self.assertEqual([row['状态'] for row in self.rows()], ['失败', '完成'])
        self.assertEqual([event['kind'] for event in self.events()], ['start', 'end'])

    def test_nonfinite_receipt_is_failed_without_losing_other_rows(self):
        # 非标准 JSON 的 NaN 不能破坏后续 JSONL 追加。
        jobs = [self.job('opencode', usage={'total': float('nan')}), self.job('opencode')]
        self.assertEqual(self.run_jobs(jobs), 1)
        self.assertEqual([row['状态'] for row in self.rows()], ['失败', '完成'])

    def test_task_and_round_log_names_do_not_collide(self):
        # 合法父子任务可产生相同的“任务-轮”拼接串，日志仍须各自独占。
        parent = self.job('opencode')
        child = dict(parent, 任务=parent['任务'] + '-A')
        parent['轮'] = 'A-r1'
        with open(self.root / '账本.jsonl', 'a') as stream:
            stream.write(json.dumps(ledger.event('开卡', child['任务'], 标题='fixture', 项目='fixture',
                来源='CL', 派活人='fixture', 父任务=parent['任务'])) + '\n')
        card = self.root / '任务卡' / (parent['任务'] + '-A-r1.txt')
        card.write_text('{}')
        (self.root / '素材' / child['任务']).mkdir()
        self.assertEqual(self.run_jobs([parent, child]), 0)
        self.assertEqual(len(list((self.root / '运行时/派活').glob('*.log'))), 2)
        self.assertEqual(len(self.rows()), 2)

    def child(self, jobs, parallel=1):
        # 用真正的短命 Python 进程测信号；导入后把脚本路径替换成临时替身。
        self.queue.write_text(json.dumps(jobs))
        code = ('import sys; from pathlib import Path; sys.path.insert(0, sys.argv.pop(1)); '
                'from frclyo import dispatch; dispatch.BIN=Path(sys.argv.pop(1)); '
                'sys.exit(dispatch.main(sys.argv[1:]))')
        return subprocess.Popen([sys.executable, '-B', '-c', code, str(BIN), str(self.fakebin),
            str(self.queue), '--并行', str(parallel)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True)

    def test_ctrl_c_stops_queue_but_started_worker_finishes(self):
        jobs = [self.job('opencode', delay=0.4), self.job('opencode')]
        child = self.child(jobs)
        try:
            deadline = time.monotonic() + 5
            while not self.events() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(self.events())
            os.killpg(child.pid, signal.SIGINT)
            stdout, stderr = child.communicate(timeout=5)
            self.assertEqual(child.returncode, 130, stderr)
            self.assertIn('批次', stdout)
            self.assertIn('（被中断）', stdout)
            self.assertEqual([event['kind'] for event in self.events()], ['start', 'end'])
            self.assertEqual({row['状态'] for row in self.rows()}, {'完成', '被中断'})
            self.assertTrue(all(row['批次被中断'] for row in self.rows()))
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate()

    def test_provider_lock_shared_by_two_dispatch_processes(self):
        first, second = self.job('opencode', delay=0.4), self.job('opencode', delay=0.2)
        a = self.child([first])
        try:
            deadline = time.monotonic() + 5
            while not self.events() and time.monotonic() < deadline:
                time.sleep(0.01)
            b = self.child([second])
            try:
                for child in (a, b):
                    out, err = child.communicate(timeout=5)
                    self.assertEqual(child.returncode, 0, out + err)
            finally:
                if b.poll() is None: b.kill()
                b.communicate()
        finally:
            if a.poll() is None: a.kill()
            a.communicate()
        self.assertEqual([event['kind'] for event in self.events()], ['start', 'end', 'start', 'end'])

    def test_cli_aliases_help_and_invalid_options(self):
        job = self.job()
        self.queue.write_text(json.dumps([job]))
        for command in ('派活', 'dispatch'):
            result = subprocess.run([sys.executable, '-B', str(BIN / 'aw'), command,
                str(self.queue), '--试跑'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('试跑：1 件', result.stdout)
        for value in ('nan', 'inf', '-1', '101'):
            result = subprocess.run([sys.executable, '-B', str(BIN / 'aw'), 'dispatch',
                str(self.queue), '--额度上限', value], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
        with mock.patch.dict(os.environ, FRCLYO_HOME=str(self.base / 'invalid')):
            result = subprocess.run([sys.executable, '-B', str(BIN / 'aw'), '派活', '--help'],
                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertFalse((self.base / 'invalid').exists())

    def test_quota_protocol_timeout_and_error_cleanup(self):
        quota = dispatch.read_quota(self.cfg)
        self.assertEqual(quota['已用'], 12)
        self.assertEqual(quota['档位'], 'secondary')
        for mode in ('stall', 'error'):
            with self.subTest(mode=mode), mock.patch.dict(os.environ, FAKE_QUOTA=mode):
                start = time.monotonic()
                quota = dispatch.read_quota(self.cfg, timeout=0.1)
                self.assertIn('错误', quota)
                self.assertNotIn('do not echo this', str(quota))
                self.assertLess(time.monotonic() - start, 2)

    def test_week_window_selection_and_invalid_values(self):
        for value in (True, None, -1, 101, float('nan')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                dispatch.weekly_quota({'rateLimits': {'primary': {'usedPercent': value, 'windowDurationMins': 10080}}})
        for minutes in (None, 300, 1440):
            with self.subTest(minutes=minutes), self.assertRaises(ValueError):
                dispatch.weekly_quota({'rateLimits': {'primary': {'usedPercent': 99, 'windowDurationMins': minutes}}})
        self.assertEqual(dispatch.weekly_quota({'rateLimits': {'primary': {
            'usedPercent': 50, 'windowDurationMins': 10080}}})['已用'], 50)

    def test_variant_empty_and_token_totals(self):
        job = self.job('opencode')
        job.pop('档位')
        job['变体'] = ''
        self.assertEqual(self.run_jobs([job]), 0)
        self.assertEqual(self.rows()[0]['档位'], '')
        self.assertEqual(dispatch.token_count({'工人': 'opencode', '用量': {'total': 123, 'input': 80}}), 123)
        self.assertEqual(dispatch.token_count({'工人': 'codex', '用量': None}), 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
