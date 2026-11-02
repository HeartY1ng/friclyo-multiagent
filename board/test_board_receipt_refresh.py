"""B138/B139: receipt negative caching and automatic release panel refresh."""
import unittest
from pathlib import Path
from unittest.mock import patch
from regression_support import board_fixture, write_json, run_js


class ReceiptRefreshTests(unittest.TestCase):
    def test_b138_unreadable_receipt_cached_and_rechecked(self):
        with board_fixture() as (b, base):
            receipt = Path(b.DELIVER) / 'CL-20991231-01/r1/回执.json'
            receipt.parent.mkdir(parents=True)
            write_json(receipt, {'状态': '执行完成'})
            with patch.object(b, 'log') as log, patch.object(b, 'read_json', wraps=b.read_json) as read:
                monitor = b.HealthMonitor()
                for _ in range(9):
                    monitor.poll_receipts()
                reads = sum(c.args[0] == str(receipt) for c in read.call_args_list)
                self.assertEqual((log.call_count, reads), (1, 1), 'B138: unchanged failed receipt must log/read once')
                self.assertIn('deliver_dir/CL-20991231-01/r1/回执.json', log.call_args.args[0])
                self.assertFalse(any(event.is_set() for event in monitor.events.values()))
                write_json(receipt, {'状态': '执行完成', '修改': True})
                monitor.poll_receipts()
                self.assertEqual(log.call_count, 2)
                write_json(receipt.parent / '运行信息.json', {'工人': 'codex'})
                monitor.poll_receipts()
                self.assertTrue(monitor.events['codex'].is_set())
                monitor.events['codex'].clear()
                monitor.poll_receipts()
                self.assertFalse(monitor.events['codex'].is_set())
                self.assertEqual(log.call_count, 2)

    def test_b138_normal_receipt_and_dependency_changes(self):
        with board_fixture() as (b, base):
            receipt = Path(b.DELIVER) / 'CL-20991231-02/r1/回执.json'
            receipt.parent.mkdir(parents=True)
            monitor = b.HealthMonitor()
            write_json(receipt, {'工人': 'opencode'})
            monitor.poll_receipts()
            self.assertTrue(monitor.events['balances'].is_set())
            monitor.events['balances'].clear()
            monitor.poll_receipts()
            self.assertFalse(monitor.events['balances'].is_set())
            write_json(receipt, {'状态': '完成'})
            info = receipt.parent / '运行信息.json'
            write_json(info, {'工人': 'codex'})
            monitor.poll_receipts()
            self.assertTrue(monitor.events['codex'].is_set())
            monitor.events['codex'].clear()
            info.unlink()
            with patch.object(b, 'log') as log:
                for _ in range(10):
                    monitor.poll_receipts()
                self.assertEqual(log.call_count, 1)
            self.assertFalse(any(event.is_set() for event in monitor.events.values()))
            write_json(info, {'工人': 'opencode'})
            monitor.poll_receipts()
            self.assertTrue(monitor.events['balances'].is_set())
            receipt.unlink()
            monitor.poll_receipts()
            self.assertEqual(monitor.seen, {})

    def test_b138_partial_json_and_independent_files(self):
        with board_fixture() as (b, base):
            paths = [Path(b.DELIVER) / ('CL-20991231-0%d/r1/回执.json' % i) for i in (3, 4)]
            for path in paths:
                path.parent.mkdir(parents=True)
                path.write_text('{"工人":', encoding='utf-8')
            with patch.object(b, 'log') as log, patch.object(b, 'read_json', wraps=b.read_json) as read:
                monitor = b.HealthMonitor()
                for _ in range(9):
                    monitor.poll_receipts()
                self.assertEqual(read.call_count, 2)
                metadata_logs = [c.args[0] for c in log.call_args_list if '元信息读取失败' in c.args[0]]
                self.assertEqual(len(metadata_logs), 2)
                self.assertFalse(monitor.seen)
                write_json(paths[0], {'工人': 'codex'})
                monitor.poll_receipts()
                self.assertTrue(monitor.events['codex'].is_set())
                self.assertEqual(read.call_count, 3)
                self.assertEqual(len(b._receipt_failures), 1)
                paths[1].unlink()
                monitor.poll_receipts()
                self.assertEqual(b._receipt_failures, {})

    def test_b139_poll_refreshes_release_results(self):
        result = run_js('B139-auto-refresh', r'''
const initial = {发布:{旧版本工人:null,无法确认工人:null,
 使用记录提示:'正在读取使用记录',版本检查提示:'正在读取版本检查'},派活:{批次:[]}};
panelExpanded.add('versions');
let pending = poll();
reply(0, {...state('全部'),版本与派活:initial}); await pending;
assert(!S.failing, 'initial render failed');
assert(elements['release-dispatch'].innerHTML.includes('正在读取版本检查'));
assert(elements['release-dispatch'].innerHTML.includes('正在读取使用记录'));
const timer = [...timers.entries()].find(([id,t]) => t.ms === 3000);
assert(timer, 'automatic refresh must be scheduled');
timers.delete(timer[0]);
const ready = {发布:{旧版本工人:[{工人:'codex',任务:'CL-20991231-01',轮:'r1',PID:123,提交:'abcdef0'}],
 无法确认工人:[],使用记录提示:'使用记录已读取',版本检查提示:'版本检查已完成',
 版本列表:[{提交:'abcdef0',完整:true,可回滚:true}]},派活:{批次:[]}};
pending = timer[1].fn();
reply(1, {...state('全部'),版本与派活:ready}); await pending;
assert(!S.failing, 'second render failed');
const html = elements['release-dispatch'].innerHTML;
assert(html.includes('PID 123'), 'B139: old-version worker must update');
assert(html.includes('版本检查已完成'), 'B139: version check must update');
assert(!html.includes('正在读取'), 'B139: loading message must disappear');
assert(html.includes('data-panel-detail="versions" open'), 'expansion must persist');
console.log('B139: scheduled poll updated workers and version checks');
''')
        if result.returncode:
            self.assertIn('AssertionError:', result.stderr, 'frontend preparation/runtime error')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
