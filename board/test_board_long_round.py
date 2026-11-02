"""B142：匹配、去重和日志关联用完整身份，只有展示截短。"""
import unittest

from regression_support import board_fixture
from test_board_release_dispatch import BATCH, journal, metadata, row


class LongRoundTests(unittest.TestCase):
    def test_long_result_matches_queue_and_is_not_pending(self):
        with board_fixture() as (board, base):
            task, rnd = 'CL-20261101-01', 'review-' + 'a' * 154
            metadata(base, 1, 任务=[{'任务': task, '轮': rnd}],
                     结束时间='2026-11-01T20:01:00Z', 结束状态='完成')
            path = journal(base, [row(轮=rnd)])
            (path.parent / ('%s-%d-%s-%s.log' % (BATCH, len(task), task, rnd))).touch()
            result = board.dispatch_panel(str(base), {})['批次'][0]
            self.assertEqual(result['计数']['完成'], 1)
            self.assertEqual(result['已记录总数'], 1)
            self.assertEqual(result['坏行'], 0)
            self.assertEqual(result['tokens'], 120)
            self.assertEqual(len(result['任务']), 1)
            self.assertEqual(result['任务'][0]['轮'], board.panel_text(rnd))

    def test_shared_display_prefix_keeps_two_identities_and_rejects_duplicate(self):
        with board_fixture() as (board, base):
            task, prefix = 'CL-20261101-01', 'a' * 160
            rounds = [prefix + '-one', prefix + '-two']
            metadata(base, 2, 任务=[{'任务': task, '轮': rnd} for rnd in rounds])
            journal(base, [row(轮=rounds[0]), row(轮=rounds[1]), row(轮=rounds[0])])
            result = board.dispatch_panel(str(base), {})['批次'][0]
            self.assertEqual(result['计数']['完成'], 2)
            self.assertEqual(result['已记录总数'], 2)
            self.assertEqual(result['坏行'], 1)
            self.assertEqual(result['tokens'], 240)
            self.assertEqual(len(result['任务']), 2)

    def test_legacy_log_and_result_match_full_round(self):
        with board_fixture() as (board, base):
            task, rnd = 'CL-20261101-01', 'b' * 161
            path = journal(base, [row(轮=rnd)])
            (path.parent / ('%s-%d-%s-%s.log' % (BATCH, len(task), task, rnd))).touch()
            result = board.dispatch_panel(str(base), {})['批次'][0]
            self.assertEqual(len(result['任务']), 1)
            self.assertEqual(result['计数']['完成'], 1)
            self.assertEqual(result['计数']['未知'], 0)

    def test_pending_long_round_uses_full_identity_to_find_worker(self):
        with board_fixture() as (board, base):
            task, rnd = 'CL-20261101-01', 'c' * 161
            metadata(base, 1, 任务=[{'任务': task, '轮': rnd}])
            tasks = {task: {'轮次': [{'轮次': rnd, '工人': 'codex', '模型': 'fixture'}]}}
            result = board.dispatch_panel(str(base), tasks)['批次'][0]
            self.assertEqual(result['计数']['在跑'], 1)
            self.assertEqual(result['任务'][0]['工人'], 'codex')
            self.assertEqual(result['任务'][0]['轮'], board.panel_text(rnd))


if __name__ == '__main__':
    unittest.main(verbosity=2)
