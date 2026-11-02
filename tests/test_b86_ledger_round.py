"""B86：新轮原子占用、终态恢复与只读编号查询。"""
import unittest
from unittest import mock

from proxy_regression_support import aw_fixture


class RoundReopening(unittest.TestCase):
    def test_completed_and_abandoned_cards_reopen_only_on_new_directory(self):
        for ending in ('完成', '放弃'):
            with self.subTest(ending=ending), aw_fixture() as (aw, root, cfg, stack):
                from frclyo import ledger
                task = aw.new_task('fixture', source='DM')
                first = root / '交付' / task / 'r1'
                ledger.claim_round(first)
                if ending == '完成':
                    aw.ledger(ending, task, '--结论', '通过', '--摘要', 'old round')
                else:
                    aw.ledger(ending, task, 'old round')
                book = root / '账本.jsonl'
                before = book.read_bytes()
                self.assertEqual(aw.ledger('下一轮', task), 'r2')
                self.assertEqual(book.read_bytes(), before, '查询不应提前恢复')
                with self.assertRaises(FileExistsError):
                    ledger.claim_round(first)
                self.assertEqual(book.read_bytes(), before, '重复派轮不得修改终态')
                second = first.with_name('r2')
                ledger.claim_round(second)
                self.assertTrue(second.is_dir())
                self.assertTrue(book.read_bytes().startswith(before), '保留原有历史字节')
                card = ledger.fold(ledger.read_events(book))[task]
                self.assertEqual(card['状态'], '开卡')
                self.assertEqual(card['结论'], '')
                self.assertEqual(card['事件'][-1]['动作'], '恢复')
                self.assertEqual(card['事件'][-1]['轮次'], 'r2')
                aw.ledger('完成', task, '--结论', '未通过', '--摘要', 'new round')
                self.assertEqual(ledger.fold(ledger.read_events(book))[task]['结论'], '未通过')

    def test_failed_ledger_write_never_creates_a_new_round(self):
        with aw_fixture() as (aw, root, cfg, stack):
            from frclyo import ledger
            task = aw.new_task('fixture', source='DM')
            aw.ledger('放弃', task, 'old')
            run = root / '交付' / task / 'r1'
            with mock.patch.object(ledger.LedgerLock, 'append', side_effect=OSError('fixture failure')):
                with self.assertRaises(OSError):
                    ledger.claim_round(run)
            self.assertFalse(run.exists())
            self.assertEqual(ledger.fold(ledger.read_events(root / '账本.jsonl'))[task]['状态'], '放弃')


if __name__ == '__main__':
    unittest.main(verbosity=2)
