"""B105–B112/B126：故障补偿、持久化中断点、只读在用记录。"""
from datetime import datetime, timezone
import errno
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest import mock

import test_release as fixtures

release = fixtures.release
A, B, C = fixtures.A, fixtures.B, fixtures.C


class ReleaseRecoveryTests(unittest.TestCase):
    # 复用离线夹具，不重复收集旧测试；git/launchctl/lsof/ps 都是替身。
    setUp = fixtures.ReleaseTests.setUp
    dispose = fixtures.ReleaseTests.dispose
    save = fixtures.ReleaseTests.save
    make_archive = fixtures.ReleaseTests.make_archive
    run_aw = fixtures.ReleaseTests.run_aw
    migrate = fixtures.ReleaseTests.migrate
    journal = fixtures.ReleaseTests.journal

    def invoke(self, *args, **kwargs):
        result = self.run_aw(*args, **kwargs)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def state(self):
        return release.read_json(release.state_path(self.root))

    def restarts(self):
        return sum(json.loads(line)[0] == 'launchctl'
                   for line in (self.base / 'commands.jsonl').read_text().splitlines())

    def old_version(self):
        self.migrate()
        self.invoke('release')
        self.save(dev=C)
        self.invoke('release')
        return self.root / '框架-版本' / A

    def test_b105_dangling_current_can_rollback_and_is_listed(self):
        self.migrate()
        self.invoke('release')
        broken = self.root / '框架-版本' / B
        release.permissions(broken, False)
        shutil.rmtree(broken)
        result = self.invoke('versions')
        self.assertIn(B + '\t当前', result.stdout)
        self.assertIn('不可回滚', result.stdout)
        self.invoke('rollback', A)
        self.assertEqual(release.current(self.root), A)
        release.metadata(self.root, A)
        # B 仍作为不可用的上一版展示，不能重新切入。
        self.assertNotEqual(self.run_aw('rollback').returncode, 0)
        self.assertEqual(release.current(self.root), A)

    def test_b106_restore_failure_compensates_and_same_command_retries(self):
        self.migrate()
        original = release.os.rename
        def fail(src, dst):
            if src == self.root / '框架.迁移前':
                raise OSError(errno.EIO, 'restore failed')
            return original(src, dst)
        with mock.patch.object(release.os, 'rename', side_effect=fail):
            self.assertEqual(release.main(['release', '--undo-migration']), 1)
        self.assertEqual(release.current(self.root), A)
        self.assertTrue(self.state()['pending'])
        self.assertTrue((self.root / '框架.迁移前/KEEP').is_file())
        self.invoke('release', '--undo-migration')
        self.assertTrue((self.stable / 'KEEP').is_file())
        self.assertNotIn('pending', self.state())

    def test_b106_killed_between_unlink_and_restore_can_retry(self):
        self.migrate()
        release.begin(self.root, '撤销迁移', A, None, 'aw 发布 --迁移')
        self.stable.unlink()  # 持久化的 kill 点：没有 finally/except 被执行。
        self.invoke('release', '--undo-migration')
        self.assertTrue((self.stable / 'KEEP').is_file())
        self.assertNotIn('pending', self.state())

    def test_b107_metadata_failure_after_rename_retries_same_commit(self):
        self.migrate()
        original = release.write_json
        injected = []
        def fail(path, value):
            if path.name == B + '.json':
                injected.append(True)
                raise OSError(errno.ENOSPC, 'metadata full')
            return original(path, value)
        with mock.patch.object(release, 'write_json', side_effect=fail):
            self.assertEqual(release.main(['release']), 1)
        self.assertTrue(injected)
        self.assertEqual(release.current(self.root), A)
        self.assertTrue((self.root / '框架-版本' / B).is_dir())
        self.invoke('release')
        self.assertEqual(release.current(self.root), B)
        release.metadata(self.root, B)

    def test_b107_orphan_different_from_fresh_archive_is_preserved(self):
        self.migrate()
        release.prepare(self.root, self.dev, B, verify=True)
        target = self.root / '框架-版本' / B
        (self.root / '运行时/发布' / (B + '.json')).unlink()
        release.permissions(target, False)
        (target / 'version.txt').write_text('different')
        release.permissions(target, True)
        result = self.run_aw('release')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('相同提交号已有不同内容', result.stderr)
        self.assertEqual(release.current(self.root), A)
        self.assertEqual((target / 'version.txt').read_text(), 'different')

    def test_b108_recovery_restart_failure_keeps_pending_then_restarts(self):
        self.migrate()
        release.prepare(self.root, self.dev, B, verify=True)
        release.begin(self.root, '发布', A, B, 'aw 回滚 ' + A)
        release.atomic_link(self.root, B)
        self.save(launchctl_rc=3)
        for _ in range(2):
            self.assertNotEqual(self.run_aw('release').returncode, 0)
            self.assertTrue(self.state()['pending'])
            self.assertEqual(release.current(self.root), B)
        before = self.restarts()
        self.save(launchctl_rc=0)
        # 收尾补做不依赖开发版是否依旧干净或指向原来的提交。
        self.save(dirty='M something', dev=C)
        self.invoke('release')
        self.assertEqual(self.restarts(), before + 1)
        self.assertEqual(self.state()['previous'], A)
        self.assertNotIn('pending', self.state())
        self.assertEqual(release.current(self.root), B)

    def test_b109_actual_process_death_leaves_candidate_retry_removes_it(self):
        self.migrate()
        code = '''import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from frclyo import release
original = release.unpack
def killed(*args):
    original(*args)
    os._exit(97)
release.unpack = killed
with release.locked(Path(sys.argv[2])):
    release.publish(Path(sys.argv[2]))
'''
        result = subprocess.run([sys.executable, '-B', '-c', code,
                                 str(fixtures.BIN), str(self.root)], cwd=self.base,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 97, result.stdout + result.stderr)
        self.assertTrue(list((self.root / '框架-版本').glob('.candidate-*')))
        self.invoke('release')
        self.assertFalse(list((self.root / '框架-版本').glob('.candidate-*')))
        self.assertEqual(release.current(self.root), B)

    def test_b110_partial_delete_is_visible_and_retry_removes_remainder(self):
        target = self.old_version()
        original = release.shutil.rmtree
        def fail(path, *args, **kwargs):
            if path == target:
                (path / 'version.txt').unlink()
                raise OSError(errno.EIO, 'partial delete')
            return original(path, *args, **kwargs)
        with mock.patch.object(release.shutil, 'rmtree', side_effect=fail):
            with self.assertRaises(OSError):
                release.cleanup_delete(self.root, [A])
        self.assertTrue(release.deletion(self.root, A))
        self.assertIn('不可回滚', self.invoke('versions').stdout)
        self.assertIn('继续中断的清理', self.invoke('clean').stdout)
        self.assertNotEqual(self.run_aw('rollback', A).returncode, 0)
        self.invoke('clean', '--yes', input=str(self.root) + '\n')
        self.assertFalse(target.exists())
        self.assertFalse(release.deletion(self.root, A))
        self.assertIn('清理旧版本完成', self.journal())

    def test_b110_deleted_tree_but_failed_final_journal_is_retryable(self):
        target = self.old_version()
        original = release.record
        def fail(change, undo):
            if change.startswith('清理旧版本完成'):
                raise OSError(errno.ENOSPC, 'journal full')
            return original(change, undo)
        with mock.patch.object(release, 'record', side_effect=fail):
            with self.assertRaises(OSError):
                release.cleanup_delete(self.root, [A])
        self.assertFalse(target.exists())
        self.assertTrue(release.deletion(self.root, A))
        self.assertIn(A, release.cleanup_preview(self.root))
        release.cleanup_delete(self.root, [A])
        self.assertFalse(release.deletion(self.root, A))

    def test_b110_deletion_intent_cannot_override_current_previous_or_lease(self):
        self.migrate()
        release.write_json(self.root / '运行时/发布' / (A + '.delete.json'),
                           dict(version=A, deleting=True))
        self.assertNotIn(A, release.cleanup_preview(self.root))
        with self.assertRaises(release.ReleaseError):
            release.cleanup_delete(self.root, [A])
        self.assertTrue((self.stable / 'version.txt').is_file())
        with mock.patch.object(release.os, 'execv') as execute:
            with self.assertRaises(release.ReleaseError):
                release.pin_worker(self.stable / 'bin', 'codex', ['TASK', 'r1'])
            execute.assert_not_called()

    def test_b111_final_journal_failure_retries_only_unfinished_steps(self):
        self.migrate()
        original = release.record
        def fail(change, undo):
            if change.startswith('发布完成'):
                raise OSError(errno.ENOSPC, 'journal full')
            return original(change, undo)
        with mock.patch.object(release, 'record', side_effect=fail):
            self.assertEqual(release.main(['release']), 1)
        self.assertEqual(release.current(self.root), B)
        self.assertTrue(self.state()['pending'])
        self.assertEqual(self.restarts(), 1)
        self.invoke('release')
        self.assertEqual(self.restarts(), 2)
        self.assertNotIn('pending', self.state())
        self.assertIn('发布完成', self.journal())
        self.assertEqual(self.state()['previous'], A)

    def test_b111_last_state_write_failure_does_not_repeat_completed_restart(self):
        self.migrate()
        original = release.write_json
        def fail(path, value):
            if path == release.state_path(self.root) and value.get('current') == B and not value.get('pending'):
                raise OSError(errno.ENOSPC, 'state full')
            return original(path, value)
        with mock.patch.object(release, 'write_json', side_effect=fail):
            self.assertEqual(release.main(['release']), 1)
        self.assertEqual(release.current(self.root), B)
        self.assertTrue(self.state()['pending']['restarted'])
        before = self.restarts()
        self.invoke('release')
        self.assertEqual(self.restarts(), before)
        self.assertNotIn('pending', self.state())

    def test_b111_migration_and_undo_post_switch_retry_are_idempotent(self):
        self.save(launchctl_rc=3)
        self.assertNotEqual(self.run_aw('release', '--migrate').returncode, 0)
        self.assertEqual(release.current(self.root), A)
        self.save(launchctl_rc=0)
        self.invoke('release', '--migrate')
        self.save(launchctl_rc=3)
        self.assertNotEqual(self.run_aw('release', '--undo-migration').returncode, 0)
        self.assertTrue((self.stable / 'KEEP').is_file())
        self.save(launchctl_rc=0)
        self.invoke('release', '--undo-migration')
        self.assertNotIn('pending', self.state())

    def test_b112_listing_validates_snapshot_and_registration(self):
        self.migrate()
        self.invoke('release')
        target = self.root / '框架-版本' / A
        release.permissions(target, False)
        (target / 'version.txt').unlink()
        release.permissions(target, True)
        rows, issue = release.version_rows(self.root)
        bad = next(row for row in rows if row['version'] == A)
        good = next(row for row in rows if row['version'] == B)
        self.assertFalse(bad['rollbackable'])
        self.assertFalse(bad['valid'])
        self.assertTrue(good['rollbackable'])
        self.assertTrue(bad['previous'])
        self.assertIn('不可回滚', self.invoke('versions').stdout)
        self.assertNotEqual(self.run_aw('rollback', A).returncode, 0)

    def test_b126_timestamp_is_utc_and_preserved_by_prepare_and_rollback(self):
        self.migrate()
        self.invoke('release')
        path = self.root / '运行时/发布' / (B + '.json')
        first = release.read_json(path)['published_at']
        self.assertEqual(datetime.fromisoformat(first.replace('Z', '+00:00')).tzinfo, timezone.utc)
        self.invoke('release')
        self.invoke('rollback', A)
        self.invoke('rollback', B)
        self.assertEqual(release.read_json(path)['published_at'], first)
        self.assertIn(first, self.invoke('versions').stdout)

    def test_b126_rollback_does_not_invent_legacy_publication_time(self):
        self.migrate()
        self.invoke('release')
        path = self.root / '运行时/发布' / (A + '.json')
        item = release.read_json(path)
        item.pop('published_at')
        release.write_json(path, item)
        self.invoke('rollback', A)
        self.assertNotIn('published_at', release.read_json(path))
        self.assertIn('发布时间：未知', self.invoke('versions').stdout)

    def test_b126_pin_registers_identity_and_readonly_query_checks_liveness(self):
        self.migrate()
        token = 'Sun Nov 1 12:34:56 2026'
        fds = []
        def inherit(fd, inheritable):
            self.assertTrue(inheritable)
            fds.append(fd)
        try:
            with mock.patch.object(release, 'process_identity', return_value=token), \
                    mock.patch.object(release.os, 'set_inheritable', side_effect=inherit), \
                    mock.patch.object(release.os, 'execv') as execute:
                release.pin_worker(self.stable / 'bin', 'codex', ['TASK-126', 'r1', '/private/card', 'model'])
                execute.assert_called_once()
        finally:
            for fd in fds:
                os.close(fd)
        directory = self.root / '运行时/发布'
        before = {p.relative_to(directory): p.read_bytes() for p in directory.rglob('*') if p.is_file()}
        for identity, active in ((token, True), ('Sun Nov 1 12:34:57 2026', False), (None, False)):
            with mock.patch.object(release, 'process_identity', return_value=identity):
                rows = release.worker_usage(self.root)
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertIs(row['active'], active)
            self.assertEqual(row['pid'], os.getpid())
            self.assertEqual(row['task'], 'TASK-126')
            self.assertEqual(row['round'], 'r1')
            self.assertEqual(row['version'], A)
            self.assertNotIn('/private/card', json.dumps(row))
        with mock.patch.object(release, 'process_identity', side_effect=release.ReleaseError('query unavailable')):
            self.assertIsNone(release.worker_usage(self.root)[0]['active'])
        self.assertEqual(before, {p.relative_to(directory): p.read_bytes() for p in directory.rglob('*') if p.is_file()})

    def test_b126_query_process_gone_zombie_reused_and_unavailable(self):
        for rc, out, err, expected in ((0, 'Sun Nov 1 12:34:56 2026 S\n', '', 'Sun Nov 1 12:34:56 2026'),
                                       (0, 'Sun Nov 1 12:34:56 2026 Z\n', '', None),
                                       (1, '', '', None)):
            with mock.patch.object(release.subprocess, 'run', return_value=subprocess.CompletedProcess([], rc, out, err)):
                self.assertEqual(release.process_identity(123), expected)
        with mock.patch.object(release.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'denied')):
            with self.assertRaises(release.ReleaseError):
                release.process_identity(123)

    def test_b126_live_or_unknown_records_protect_old_version_and_query_is_readonly(self):
        target = self.old_version()
        with mock.patch.object(release, 'process_identity', return_value='identity'):
            release.register_worker(self.root, A, 'opencode', ['TASK-126', 'r2'])
            rows, _ = release.version_rows(self.root)
            row = next(row for row in rows if row['version'] == A)
            self.assertEqual(row['users'][0]['worker'], 'opencode')
            self.assertEqual(row['workers'], 1)
            self.assertFalse(row['eligible'])
            self.assertTrue(row['in_use'])
        with mock.patch.object(release, 'process_identity', side_effect=release.ReleaseError('unavailable')):
            self.assertNotIn(A, release.cleanup_preview(self.root))
        with mock.patch.object(release, 'process_identity', return_value=None):
            self.assertIn(A, release.cleanup_preview(self.root))
        self.assertTrue(target.exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
