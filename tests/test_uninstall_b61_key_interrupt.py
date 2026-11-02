"""修好后：登记中断有可恢复的意图记录，不能遗留无记录的新增条目。

security 只用内存替身；不传入、生成、读取任何密码。
"""
import subprocess
import unittest
from unittest import mock
from uninstall_support import Repro


class TestB61(Repro):
    def check_cancel_before_confirmation(self, cancellation):
        # B98：确认前 EOF/Ctrl-C 不能冒领后来由其他工具登记的同名条目。
        keys = self.aw.runtime('keys')
        entries, calls = set(), []
        entry = ('frclyo-deepseek', 'frclyo')

        def security(argv, **kwargs):
            self.assertEqual(argv[0], 'security')
            self.assertNotIn('-w', argv, '取消前不应新增或读取密码')
            action = argv[1]
            calls.append(action)
            target = (argv[argv.index('-s') + 1], argv[argv.index('-a') + 1])
            existed = target in entries
            if action == 'delete-generic-password':
                entries.discard(target)
            else:
                self.assertEqual(action, 'find-generic-password')
            return subprocess.CompletedProcess(argv, 0 if existed else 44)

        with mock.patch.object(keys.subprocess, 'run', side_effect=security):
            with mock.patch('builtins.input', side_effect=cancellation):
                with self.assertRaises(cancellation):
                    self.quiet(self.aw.key_command, ['add', 'deepseek'])
            self.assertEqual(calls, ['find-generic-password'])
            self.assertFalse(self.aw.JOURNAL.exists(), '确认前不能创建安装记录')
            entries.add(entry)
            self.assertEqual(self.uninstall()[0], 0)
        self.assertIn(entry, entries)
        self.assertEqual(calls, ['find-generic-password'])

    def test_b98_eof_before_confirmation(self):
        self.check_cancel_before_confirmation(EOFError)

    def test_b98_ctrl_c_before_confirmation(self):
        self.check_cancel_before_confirmation(KeyboardInterrupt)

    def test_b98_decline_does_not_create_journal(self):
        # 普通拒绝也不建立归属记录，且只询问一次。
        keys = self.aw.runtime('keys')
        with mock.patch.object(keys.subprocess, 'run', return_value=subprocess.CompletedProcess([], 44)) as run, \
                mock.patch('builtins.input', return_value='n') as prompt:
            self.assertEqual(self.quiet(self.aw.key_command, ['add', 'deepseek'])[0], 0)
        prompt.assert_called_once()
        self.assertEqual(run.call_count, 1)
        self.assertNotIn('-w', run.call_args.args[0])
        self.assertFalse(self.aw.JOURNAL.exists())

    def test_interrupt_after_security_add_before_journal(self):
        self.install()
        keys = self.aw.runtime('keys')
        entries = set()

        def security(argv, **kwargs):
            self.assertEqual(argv[0], 'security')
            action = argv[1]
            entry = (argv[argv.index('-s') + 1], argv[argv.index('-a') + 1])
            if action == 'find-generic-password':
                code = 0 if entry in entries else 44
            elif action == 'add-generic-password':
                entries.add(entry)
                code = 0
            elif action == 'delete-generic-password':
                entries.discard(entry)
                code = 0
            else:
                raise AssertionError(action)
            return subprocess.CompletedProcess(argv, code)

        with mock.patch.object(keys.subprocess, 'run', side_effect=security):
            with mock.patch('builtins.input', return_value='y'), mock.patch.object(
                    self.aw, 'journal_begin', side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    self.quiet(self.aw.key_command, ['add', 'deepseek'])
            key_plans = [r for r in self.rows() if r.get('kind') == 'key']
            rc, _ = self.uninstall()
        self.evidence(rc=rc, key_plan_count=len(key_plans), fake_entries_remaining=sorted(entries))
        self.assertFalse(entries, '新增条目先于 plan 写入，中断后卸载完全看不到它')

    def test_interrupt_after_add_keeps_pending_intent(self):
        self.install()
        keys = self.aw.runtime('keys')
        entries = set()

        def security(argv, **kwargs):
            self.assertEqual(argv[0], 'security')
            action = argv[1]
            entry = (argv[argv.index('-s') + 1], argv[argv.index('-a') + 1])
            if action == 'find-generic-password':
                code = 0 if entry in entries else 44
            elif action == 'add-generic-password':
                plans = [r for r in self.rows() if r.get('kind') == 'key']
                self.assertEqual(len(plans), 1)
                self.assertEqual(plans[0]['state'], 'in-progress')
                self.assertTrue(plans[0]['absent_before'])
                entries.add(entry)
                raise KeyboardInterrupt
            elif action == 'delete-generic-password':
                entries.discard(entry)
                code = 0
            else:
                raise AssertionError(action)
            return subprocess.CompletedProcess(argv, code)

        with mock.patch.object(keys.subprocess, 'run', side_effect=security):
            with mock.patch('builtins.input', return_value='y'):
                with self.assertRaises(KeyboardInterrupt):
                    self.quiet(self.aw.key_command, ['add', 'deepseek'])
            key_plans = [r for r in self.rows() if r.get('kind') == 'key']
            self.assertEqual(len(key_plans), 1)
            self.assertTrue(entries)
            self.assertFalse(any(r['action'] == 'done' and r.get('id') == key_plans[0]['id'] for r in self.rows()))
            rc, _ = self.uninstall()
            self.assertEqual(rc, 0)
        self.evidence(rc=rc, key_plan_count=len(key_plans), fake_entries_remaining=sorted(entries))
        self.assertFalse(entries, '新增条目先于 plan 写入，中断后卸载完全看不到它')


if __name__ == '__main__':
    unittest.main(verbosity=2)
