"""范围内的对照检查；这些用例预期通过，不计入缺陷条数。"""
import json
import os
import subprocess
import unittest
from unittest import mock
from uninstall_support import Repro


class Controls(Repro):
    def snapshot(self):
        return {str(p.relative_to(self.home)): ('link', os.readlink(p)) if p.is_symlink()
                else ('dir',) if p.is_dir() else ('file', p.read_bytes())
                for p in self.home.rglob('*')}

    def test_repeat_install_is_idempotent(self):
        self.install()
        before = self.snapshot()
        self.install()
        after = self.snapshot()
        for value in (before, after):
            value.pop('work/配置/安装记录.jsonl')
        self.assertEqual(before, after)
        self.assertEqual(self.calls.count('bootstrap'), 1)
        self.assertEqual(self.uninstall()[0], 0)
        self.assertFalse(self.loaded)
        self.assertFalse(self.link.exists())

    def test_dry_run_is_read_only(self):
        self.install()
        before = self.snapshot()
        calls = list(self.calls)
        self.assertEqual(self.uninstall('--dry-run', '--delete-data')[0], 0)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.calls, calls)
        result = subprocess.run(['/bin/bash', str(self.repo / 'uninstall.sh'), '--dry-run'],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_default_preserves_config_and_data(self):
        self.install()
        marker = self.work / 'user-data.txt'
        marker.write_text('synthetic user data')
        config = self.aw.config_path().read_bytes()
        self.assertEqual(self.uninstall()[0], 0)
        self.assertTrue(marker.exists())
        self.assertEqual(self.aw.config_path().read_bytes(), config)
        self.assertTrue(self.aw.JOURNAL.exists())

    def test_delete_data_requires_full_path(self):
        self.install()
        with mock.patch('builtins.input', return_value='y'):
            self.assertEqual(self.uninstall('--delete-data')[0], 0)
        self.assertTrue(self.work.exists())
        with mock.patch('builtins.input', return_value=str(self.work)):
            self.assertEqual(self.uninstall('--delete-data')[0], 0)
        self.assertFalse(self.work.exists())

    def test_foreign_link_collision_preserves_owner(self):
        self.link.parent.mkdir(parents=True)
        self.link.symlink_to(self.home / 'other-tool')
        with self.assertRaises(self.aw.UserError):
            self.quiet(self.aw.install, ['--yes'])
        self.assertEqual(self.uninstall()[0], 0)
        self.assertEqual(os.readlink(self.link), str(self.home / 'other-tool'))

    def test_bootstrap_failure_can_retry(self):
        original = self.launchctl

        def failed(*args):
            return 1 if args[0] == 'bootstrap' else original(*args)

        with mock.patch.object(self.aw, 'launchctl', side_effect=failed):
            with self.assertRaises(self.aw.UserError):
                self.quiet(self.aw.install, ['--yes'])
        self.install()
        self.assertEqual(self.uninstall()[0], 0)
        self.assertFalse(self.loaded)

    def test_modified_skill_is_preserved(self):
        self.install()
        self.skill.write_text('synthetic local edits')
        self.assertEqual(self.uninstall()[0], 1)
        self.assertEqual(self.skill.read_text(), 'synthetic local edits')

    def test_existing_loaded_service_is_not_claimed(self):
        self.loaded = True
        self.install()
        self.assertNotIn('bootstrap', self.calls)
        self.assertEqual(self.uninstall()[0], 0)
        self.assertNotIn('bootout', self.calls)
        self.assertTrue(self.loaded)

    def test_relocated_upgrade_preserves_old_command(self):
        self.install()
        original = os.readlink(self.link)
        # 同一快照换仓库位置，模拟升级迁址；不改产品源码。
        with mock.patch.object(self.aw, 'ROOT', self.base / 'new-version'):
            with self.assertRaisesRegex(self.aw.UserError, '已有不同文件'):
                self.quiet(self.aw.install, ['--yes'])
        self.assertEqual(os.readlink(self.link), original)

    def test_registered_default_key_only(self):
        self.install()
        keys = self.aw.runtime('keys')
        self.aw.record('plan', id='fake-key', kind='key', provider='deepseek')
        self.aw.record('done', id='fake-key')
        cfg = self.aw.config()
        cfg['keychain_entries'] = {'deepseek': {'service': 'external-fixture', 'account': 'owner'}}
        self.aw.runtime('config').save(cfg)
        calls = []

        def security(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch.object(keys.subprocess, 'run', side_effect=security):
            self.assertEqual(self.uninstall()[0], 0)
            self.assertEqual(self.uninstall()[0], 0)
        self.assertEqual(calls, [['security', 'delete-generic-password', '-s',
                                 'frclyo-deepseek', '-a', 'frclyo']])


if __name__ == '__main__':
    unittest.main(verbosity=2)
