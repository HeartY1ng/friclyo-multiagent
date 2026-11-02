"""期望：子卡收尾失败时，父卡不能已完成，也不能重复写相反的验收记录。"""
import contextlib
import io
import json
import unittest
from unittest import mock
from proxy_regression_support import aw_fixture, CHOICE, completed_worker


class DemoCommitOrder(unittest.TestCase):
    def test_child_completion_failure_does_not_leave_parent_completed(self):
        self.check_failure('-A')

    def test_second_child_failure_keeps_first_closed_and_parent_blocked(self):
        self.check_failure('-B')

    def check_failure(self, suffix):
        with aw_fixture() as (aw, root, cfg, stack):
            stack.enter_context(mock.patch.object(aw, 'model_inventory', return_value=[CHOICE]))
            stack.enter_context(mock.patch.object(aw.runtime('models'), 'cli_version', return_value='fixture'))
            stack.enter_context(mock.patch.object(aw, 'demo_board', side_effect=contextlib.nullcontext))
            stack.enter_context(mock.patch.object(aw, 'invoke_with_fallback', side_effect=completed_worker))
            # 独立聚焦记账阶段：三关视为已通过，工人回执仍由真实核验器检查。
            stack.enter_context(mock.patch.object(aw.checker(), 'demo_gates', return_value=None))
            finish = aw.finish_task
            injected = []

            def fail_first_child(task, passed, summary):
                if passed and task.endswith(suffix) and not injected:
                    injected.append(task)
                    raise aw.UserError('合成故障：子卡完成时账本短暂不可用')
                return finish(task, passed, summary)

            stack.enter_context(mock.patch.object(aw, 'finish_task', side_effect=fail_first_child))
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(aw.UserError):
                aw.demo(['--yes'])
            from frclyo import ledger
            cards = ledger.fold(ledger.read_events(root / '账本.jsonl'))
            rows = [json.loads(line) for line in (root / '表现记录.jsonl').read_text().splitlines()]
            print('ledger states =', {task: card['状态'] for task, card in cards.items()})
            print('performance =', [(row['任务号'], row['通过验收']) for row in rows])
            self.assertEqual(len(injected), 1, '故障注入必须确实触发')
            parents = [card for card in cards.values() if not card['父任务']]
            self.assertEqual(parents[0]['状态'], '卡住')
            self.assertIn('子卡完成时账本短暂不可用', parents[0]['原因'])
            self.assertIn('三关已通过，收尾记账失败', parents[0]['原因'])
            self.assertFalse((root / '交付' / parents[0]['任务']).exists(), '父卡不应有成功批次回执')
            if suffix == '-B':
                self.assertEqual(next(card for task, card in cards.items() if task.endswith('-A'))['状态'], '完成')
            self.assertEqual(len(rows), 2, '每个工人轮次只能保留一条一致的验收记录')
            self.assertTrue(all(row['通过验收'] is True for row in rows))


if __name__ == '__main__':
    unittest.main(verbosity=2)
