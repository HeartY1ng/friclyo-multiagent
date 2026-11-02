"""第二轮变异回归：进程快照和时钟均用替身，不调用宿主 ps 或真实信号。"""
import json
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from frclyo import execution as ex, models


class Mutation2ExecutionTests(unittest.TestCase):
    def test_snapshot_tracks_valid_roots_but_not_reused_pid(self):
        # 已脱离的后代仍可扩展谱系；复用 PID 的新进程不能成为根。
        child = mock.Mock(pid=10, poll=lambda: 0)
        tracker = ex.Descendants(child)
        tracker.known = {20: 'Mon Oct 26 12:00:00 2026', 40: 'old start'}
        snapshot = ('20 1 20 S Mon Oct 26 12:00:00 2026\n'
                    '30 20 30 S Mon Oct 26 12:01:00 2026\n'
                    '40 1 40 S new start\n41 40 41 S child of reused pid\n')
        with mock.patch.object(ex.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, snapshot)):
            rows = tracker.sample()
        self.assertIsInstance(rows, dict)
        self.assertEqual(rows[20][3], 'Mon Oct 26 12:00:00 2026')
        self.assertIn(30, tracker.known)
        self.assertNotIn(41, tracker.known)
        self.assertTrue(tracker.complete)

    def test_unknown_snapshot_is_conservatively_running(self):
        # 不能取证时不能把“未知”当作已回收。
        tracker = ex.Descendants(mock.Mock(pid=10))
        with mock.patch.object(tracker, 'sample', return_value=None):
            self.assertIs(tracker.running(), True)

    def test_running_signals_only_live_detached_identity(self):
        # 同组、僵尸、身份变化和缺失节点均不重复发送独立 PID 信号。
        tracker = ex.Descendants(mock.Mock(pid=10))
        tracker.known = {20: 'a', 21: 'a', 22: 'a', 23: 'a', 24: 'a'}
        rows = {20: (1, 20, 'S', 'a'), 21: (1, 10, 'S', 'a'),
                22: (1, 22, 'Z', 'a'), 23: (1, 23, 'S', 'b')}
        with mock.patch.object(tracker, 'sample', return_value=rows), mock.patch.object(ex.os, 'kill') as kill:
            self.assertIs(tracker.running(signal.SIGTERM), True)
        kill.assert_called_once_with(20, signal.SIGTERM)

    def test_reap_initializes_tracker_and_returns_confirmed_boolean(self):
        # 没有附带跟踪器的 child 也必须完成回收流程并返回明确布尔值。
        child = mock.Mock(spec=['pid', 'wait'], pid=10)
        tracker = mock.Mock(complete=True)
        tracker.running.return_value = False
        with mock.patch.object(ex, 'Descendants', return_value=tracker) as create, \
                mock.patch.object(ex, 'group_running', return_value=False), \
                mock.patch.object(ex, 'signal_group'), mock.patch.object(ex.time, 'sleep') as sleep:
            self.assertIs(ex.reap_group(child), True)
        create.assert_called_once_with(child)
        child.wait.assert_called_once_with(timeout=0.1)
        sleep.assert_not_called()

    def test_reap_default_grace_does_not_sleep_after_two_seconds(self):
        # 默认宽限到期后直接升级信号，不能悄悄延长为三秒。
        child = mock.Mock(pid=10)
        tracker = child._frclyo_descendants
        tracker.complete = True
        tracker.running.return_value = False
        with mock.patch.object(ex, 'group_running', side_effect=[True, False, False, False]), \
                mock.patch.object(ex, 'signal_group'), \
                mock.patch.object(ex.time, 'monotonic', side_effect=[0, 2.5, 3, 5]), \
                mock.patch.object(ex.time, 'sleep') as sleep:
            self.assertIs(ex.reap_group(child), True)
        sleep.assert_not_called()

    def test_monitor_samples_at_start_and_when_interval_expires(self):
        # 第一拍与到期采样都需要保留；drain 使替身进程退出，循环必须有界。
        child = mock.Mock()
        child.poll.return_value = None
        clock = [0.0]
        drains = []
        def drain(seconds):
            drains.append(seconds)
            self.assertEqual(len(drains), 1, '进程退出后不应继续 drain')
            clock[0] = 0.1
            child.poll.return_value = 0
        tracker = mock.Mock()
        status = {}
        with mock.patch.object(ex, 'Descendants', return_value=tracker), \
                mock.patch.object(ex.time, 'monotonic', side_effect=lambda: clock[0]), \
                mock.patch.object(ex.os, 'getppid', return_value=99), \
                mock.patch.object(ex, 'reap_group', return_value=True):
            ex.monitor_process(child, 5, 99, None, 124, [0], drain, status)
        self.assertEqual(tracker.sample.call_count, 2)
        self.assertIs(status['group_reaped'], True)

    def test_monitor_stops_immediately_for_existing_reason(self):
        # 已有中断原因时不得继续读输出；避免 and 退化为 or 后继续执行。
        child = mock.Mock(poll=lambda: None)
        drain = mock.Mock(side_effect=AssertionError('已有中断原因仍调用 drain'))
        with mock.patch.object(ex, 'Descendants'), mock.patch.object(ex, 'reap_group', return_value=False):
            status = {}
            ex.monitor_process(child, 5, 99, None, 124, [143], drain, status)
        drain.assert_not_called()
        self.assertIs(status['group_reaped'], False)

    def test_opencode_finalization_preserves_text_and_nested_usage(self):
        # 合成事件：文本不能当成 step_finish，不能跳过 OpenCode 分支或丢弃嵌套缓存计数。
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve(); run = root/'run'; control = root/'control'
            run.mkdir(); control.mkdir()
            ex.write_json(control/'status.json', dict(rc=0, timed_out=False, group_reaped=True, errors=[]))
            events = [{'type':'text','part':{'text':'synthetic result'}},
                      {'type':'step_finish','part':{'tokens':None}},
                      {'type':'step_finish','part':{'tokens':{'input':3,'cache':{'read':7,'write':2}}}}]
            (control/'events.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in events))
            (control/'stderr.log').write_bytes(b'')
            (control/'代理.stdout').write_text('{"拒绝次数":0}\n')
            with mock.patch.object(models,'observe',return_value='ok'):
                rc = ex.finalize('opencode',run,control,'T','r1','fake','high','start','end','hash')
            self.assertEqual(rc,0)
            receipt=json.loads((run/'回执.json').read_text())
            self.assertEqual(receipt['状态'],'执行完成')
            self.assertEqual(receipt['用量'],{'input':3,'cached_input_tokens':7,'cache_write_input_tokens':2})
            self.assertEqual((run/'结果.txt').read_text(),'synthetic result\n')

    def test_invalid_utf8_cannot_be_reported_as_success(self):
        # 非法 UTF-8 必须使回执失败，同时保留可信捕获字节。
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();run=root/'run';control=root/'control';run.mkdir();control.mkdir()
            ex.write_json(control/'status.json',dict(rc=0,timed_out=False,group_reaped=True,errors=[]))
            data=b'{"type":"text","part":{"text":"bad\xff"}}\n'
            (control/'events.jsonl').write_bytes(data);(control/'stderr.log').write_bytes(b'')
            (control/'代理.stdout').write_text('{"拒绝次数":0}\n')
            with mock.patch.object(models,'observe',return_value='unknown'):
                self.assertEqual(ex.finalize('opencode',run,control,'T','r1','fake','high','s','e','sha'),1)
            receipt=json.loads((run/'回执.json').read_text())
            self.assertEqual(receipt['状态'],'失败')
            self.assertTrue(any('非法 UTF-8' in e for e in receipt['错误']))
            self.assertEqual((run/'events.jsonl').read_bytes(),data)

if __name__ == '__main__':
    unittest.main()
