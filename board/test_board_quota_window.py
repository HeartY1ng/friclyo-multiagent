"""修复后：额度窗口为 300 分钟时，提醒不能把它说成一周。不调用真实 CLI。"""
import io
import json
import os
import types
import unittest
from unittest import mock
from regression_support import board_fixture, run_js


class BoardQuotaWindow(unittest.TestCase):
    def query(self, board, primary, secondary=None):
        read_fd, write_fd = os.pipe()
        rate = {'primary': primary, 'secondary': secondary, 'planType': 'fixture'}
        os.write(write_fd, (json.dumps({'id': 1, 'result': {}}) + '\n' +
                          json.dumps({'id': 2, 'result': {'rateLimits': rate}}) + '\n').encode())
        os.close(write_fd)
        process = types.SimpleNamespace(stdin=io.BytesIO(), stdout=os.fdopen(read_fd, 'rb'),
                                        pid=2147483647, wait=lambda: 0)
        with mock.patch.object(board.subprocess, 'Popen', return_value=process), \
             mock.patch.object(board.os, 'killpg'):
            return board.check_codex_quota()

    def test_five_hour_window_is_not_reported_as_weekly(self):
        with board_fixture() as (board, base):
            read_fd, write_fd = os.pipe()
            responses = [
                {'id': 1, 'result': {}},
                {'id': 2, 'result': {'rateLimits': {
                    'primary': {'usedPercent': 95, 'windowDurationMins': 300, 'resetsAt': 1900000000},
                    'planType': 'fixture'}}},
            ]
            os.write(write_fd, ''.join(json.dumps(row) + '\n' for row in responses).encode())
            os.close(write_fd)
            process = types.SimpleNamespace(stdin=io.BytesIO(), stdout=os.fdopen(read_fd, 'rb'),
                                            pid=2147483647, wait=lambda: 0)
            with mock.patch.object(board.subprocess, 'Popen', return_value=process), \
                 mock.patch.object(board.os, 'killpg'):
                quota = board.check_codex_quota()
            self.assertEqual(quota['窗口分钟'], 300)
            board.HEALTH = {'codex': quota, '余额': [], '日审': None}
            with mock.patch.object(board, 'build_tasks', return_value=({}, [])):
                alerts = board.build_state()['要你处理']
            self.assertEqual(len(alerts), 1)
            print('原始窗口=300 分钟；实际提醒=' + json.dumps(alerts[0], ensure_ascii=False))
            self.assertNotIn('周', alerts[0]['事情'] + alerts[0]['怎么办'],
                             '提醒应依据实际窗口，不应对五小时窗口给出整周停用建议')
            self.assertIn('5 小时额度', alerts[0]['事情'])
            self.assertIn('重置前少派 Codex', alerts[0]['怎么办'])

    def test_both_windows_query_alerts_and_actual_html(self):
        with board_fixture() as (board, base):
            quota = self.query(board,
                {'usedPercent': 95, 'windowDurationMins': 300, 'resetsAt': 1900000000},
                {'usedPercent': 96, 'windowDurationMins': 10080, 'resetsAt': 1900000000})
            self.assertEqual([w['名字'] for w in quota['窗口']], ['5 小时额度', '本周额度'])
            board.HEALTH = {'codex': quota, '余额': [], '日审': None}
            with mock.patch.object(board, 'build_tasks', return_value=({}, [])):
                alerts = board.build_state()['要你处理']
            self.assertEqual(len(alerts), 2)
            self.assertEqual(len({a['键'] for a in alerts}), 2)
            self.assertIn('这周少派', alerts[1]['怎么办'])
            retained = board._merge_health(quota, {'错误': '查询超时'})
            result = run_js('B83', 'const q=' + json.dumps(retained) + r''';
renderHealth({codex:q});
assert(elements.health.innerHTML.includes('5 小时额度'));
assert(elements.health.innerHTML.includes('本周额度'));
assert(elements.health.innerHTML.includes('已用 95%'));
assert(elements.health.innerHTML.includes('已用 96%'));
assert.equal((elements.health.innerHTML.match(/查询失败/g)||[]).length,2);
''')
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_window_labels_single_secondary_null_and_unknown(self):
        with board_fixture() as (board, base):
            for minutes, name in ((10080, '本周额度'), (120, '2 小时额度'), (90, '90 分钟额度'),
                                  (None, '额度（窗口未知）'), (0, '额度（窗口未知）'),
                                  ('300', '额度（窗口未知）'), (True, '额度（窗口未知）')):
                with self.subTest(minutes=minutes):
                    quota = self.query(board, {'usedPercent': 95, 'windowDurationMins': minutes})
                    self.assertEqual(len(quota['窗口']), 1)
                    self.assertEqual(quota['窗口'][0]['名字'], name)
            quota = self.query(board, {'usedPercent': 5, 'windowDurationMins': 300},
                               {'usedPercent': 95, 'windowDurationMins': 10080})
            board.HEALTH = {'codex': quota}
            with mock.patch.object(board, 'build_tasks', return_value=({}, [])):
                alerts = board.build_state()['要你处理']
            self.assertEqual(len(alerts), 1)
            self.assertIn('本周额度', alerts[0]['事情'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
