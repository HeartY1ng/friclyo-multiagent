"""B133/B137：完整快照、真实链接/状态/台账，只替换看板重启边界。"""
import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

BIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BIN))
from frclyo import release

A, B = 'a' * 40, 'b' * 40


class PendingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(os.environ['TMPDIR']).resolve())
        self.root = Path(self.temp.name).resolve()
        self.addCleanup(self.dispose)
        cfg = self.root / 'config.json'
        cfg.write_text('{}')
        env = patch.dict(os.environ, FRCLYO_HOME=str(self.root), FRCLYO_CONFIG=str(cfg))
        env.start()
        self.addCleanup(env.stop)
        release.layout(self.root, True)
        for version in (A, B):
            tree = self.root / '框架-版本' / version
            tree.mkdir()
            (tree / 'version.txt').write_text(version)
            release.permissions(tree, True)
            release.write_json(self.root / '运行时/发布' / (version + '.json'),
                               dict(commit=version, digest=release.digest(tree), accepted=True, leases=True))
        release.atomic_link(self.root, A)
        release.write_json(release.state_path(self.root), dict(current=A, previous=None))
        self.restarts = []
        self.bad_versions = set()
        def restart(root):
            version = release.current(root)
            self.restarts.append(version)
            if version in self.bad_versions:
                raise release.ReleaseError('fixture: 看板重启失败')
        mock = patch.object(release, 'restart_board', side_effect=restart)
        mock.start()
        self.addCleanup(mock.stop)

    def dispose(self):
        release.permissions(self.root, False)
        self.temp.cleanup()

    def state(self):
        return release.read_json(release.state_path(self.root))

    def journal(self):
        return (self.root / '台账/改动台账.md').read_text()

    def pending_publication(self):
        self.bad_versions.add(B)
        with self.assertRaises(release.ReleaseError):
            release.switch(self.root, B, '发布')
        self.assertEqual(release.current(self.root), B)
        self.assertEqual(self.state()['pending']['new'], B)

    def test_explicit_rollback_does_not_retry_failed_publication_restart(self):
        self.pending_publication()
        self.assertEqual(release.main(['rollback', A]), 0)
        self.assertEqual(self.restarts, [B, A])
        self.assertEqual(release.current(self.root), A)
        self.assertNotIn('pending', self.state())
        release.metadata(self.root, A)

    def test_failed_rollback_restart_keeps_target_and_owns_pending(self):
        self.pending_publication()
        self.bad_versions.add(A)
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            self.assertEqual(release.main(['rollback', A]), 1)
        self.assertIn('看板重启失败', error.getvalue())
        self.assertEqual(release.current(self.root), A)
        release.metadata(self.root, A)
        self.assertEqual(self.state()['pending']['kind'], '回滚')
        self.assertEqual(self.state()['pending']['new'], A)
        self.bad_versions.remove(A)
        self.assertEqual(release.main(['rollback', A]), 0)
        self.assertEqual(self.state()['previous'], B)
        self.assertNotIn('pending', self.state())
        self.assertEqual(self.journal().count('| 回滚完成：'), 1)

    def test_invalid_target_keeps_existing_entry_and_pending(self):
        self.pending_publication()
        before = release.state_path(self.root).read_bytes()
        self.assertEqual(release.main(['rollback', 'c' * 40]), 1)
        self.assertEqual(release.current(self.root), B)
        self.assertEqual(release.state_path(self.root).read_bytes(), before)
        self.assertEqual(self.restarts, [B])

    def test_rollback_cancels_pending_switch_that_never_happened(self):
        release.begin(self.root, '发布', A, B, 'aw 回滚 ' + A)
        self.assertEqual(release.main(['rollback', A]), 0)
        self.assertEqual(release.current(self.root), A)
        self.assertNotIn('pending', self.state())
        self.assertIsNone(self.state()['previous'])

    def test_atomic_switch_failure_retains_complete_entry(self):
        self.pending_publication()
        with patch.object(release, 'atomic_link', side_effect=OSError('fixture: rename failed')):
            self.assertEqual(release.main(['rollback', A]), 1)
        self.assertEqual(release.current(self.root), B)
        release.metadata(self.root, B)
        self.assertEqual(release.main(['rollback', A]), 0)
        self.assertEqual(release.current(self.root), A)

    def test_default_rollback_does_not_retry_failed_publication_restart(self):
        self.pending_publication()
        self.assertEqual(release.main(['rollback']), 0)
        self.assertEqual(self.restarts, [B, A])
        self.assertEqual(release.current(self.root), A)
        self.assertEqual(self.state()['previous'], B)
        self.assertNotIn('pending', self.state())
        release.metadata(self.root, A)

    def test_default_failed_rollback_retry_finishes_same_target(self):
        self.pending_publication()
        self.bad_versions.add(A)
        for _ in range(2):
            error = io.StringIO()
            with contextlib.redirect_stderr(error):
                self.assertEqual(release.main(['rollback']), 1)
            self.assertIn('看板重启失败', error.getvalue())
            self.assertEqual(release.current(self.root), A)
            release.metadata(self.root, A)
            self.assertEqual(self.state()['pending']['kind'], '回滚')
            self.assertEqual(self.state()['pending']['new'], A)
        self.bad_versions.remove(A)
        self.assertEqual(release.main(['rollback']), 0)
        self.assertEqual(self.restarts, [B, A, A, A])
        self.assertEqual(release.current(self.root), A)
        self.assertEqual(self.state()['previous'], B)
        self.assertNotIn('pending', self.state())
        self.assertEqual(self.journal().count('| 回滚完成：'), 1)

    def test_default_invalid_previous_keeps_entry_and_pending(self):
        self.pending_publication()
        tree = self.root / '框架-版本' / A
        release.permissions(tree, False)
        (tree / 'version.txt').write_text('incomplete snapshot')
        release.permissions(tree, True)
        before = release.state_path(self.root).read_bytes()
        self.assertEqual(release.main(['rollback']), 1)
        self.assertEqual(release.current(self.root), B)
        release.metadata(self.root, B)
        self.assertEqual(release.state_path(self.root).read_bytes(), before)
        self.assertEqual(self.restarts, [B])

    def test_default_atomic_switch_failure_can_retry_same_target(self):
        self.pending_publication()
        with patch.object(release, 'atomic_link', side_effect=OSError('fixture: rename failed')):
            self.assertEqual(release.main(['rollback']), 1)
        self.assertEqual(release.current(self.root), B)
        release.metadata(self.root, B)
        self.assertEqual(release.main(['rollback']), 0)
        self.assertEqual(release.current(self.root), A)
        self.assertNotIn('pending', self.state())

    def early_pending(self, previous=None):
        state = self.state()
        state['previous'] = previous
        release.write_json(release.state_path(self.root), state)
        write = release.write_json
        injected = []
        def fail(path, value):
            if path == release.state_path(self.root) and value.get('current') == B and not injected:
                injected.append(True)
                raise OSError('fixture: first switched state write failed')
            return write(path, value)
        with patch.object(release, 'write_json', side_effect=fail):
            with self.assertRaises(release.ReleaseError):
                release.switch(self.root, B, '发布')
        self.assertTrue(injected)
        self.assertEqual(release.current(self.root), B)
        self.assertEqual(self.state()['current'], A)
        self.assertEqual(self.state()['previous'], previous)
        self.assertEqual(self.state()['pending']['old'], A)

    def test_b145_first_publication_early_state_failure(self):
        self.early_pending()
        self.assertEqual(release.main(['rollback']), 0)
        self.assertEqual(release.current(self.root), A)
        release.metadata(self.root, A)
        self.assertNotIn('pending', self.state())

    def test_b145_stale_previous_does_not_skip_pending_old(self):
        self.early_pending(previous='c' * 40)
        self.assertEqual(release.main(['rollback']), 0)
        self.assertEqual(release.current(self.root), A)
        release.metadata(self.root, A)

    def test_b145_unswitched_pending_keeps_old_version(self):
        release.begin(self.root, '发布', A, B, 'aw 回滚 ' + A)
        self.assertEqual(release.main(['rollback']), 0)
        self.assertEqual(release.current(self.root), A)
        self.assertNotIn('pending', self.state())
        release.metadata(self.root, A)

    def test_b145_no_pending_still_uses_previous(self):
        release.switch(self.root, B, '发布')
        self.assertEqual(release.main(['rollback']), 0)
        self.assertEqual(release.current(self.root), A)

    def test_b145_explicit_target_overrides_early_pending(self):
        self.early_pending()
        self.assertEqual(release.main(['rollback', B]), 0)
        self.assertEqual(release.current(self.root), B)
        self.assertNotIn('pending', self.state())

    def test_b145_early_pending_bad_old_preserves_stable_entry(self):
        self.early_pending()
        tree = self.root / '框架-版本' / A
        release.permissions(tree, False)
        (tree / 'version.txt').write_text('damaged')
        release.permissions(tree, True)
        before = release.state_path(self.root).read_bytes()
        self.assertEqual(release.main(['rollback']), 1)
        self.assertEqual(release.current(self.root), B)
        release.metadata(self.root, B)
        self.assertEqual(release.state_path(self.root).read_bytes(), before)

    def completion_write_failure(self, expected=1):
        write = release.write_json
        def fail(path, value):
            if path == release.state_path(self.root) and value.get('pending', {}).get('recorded'):
                raise OSError('fixture: completion persisted but state write failed')
            return write(path, value)
        with patch.object(release, 'write_json', side_effect=fail):
            with self.assertRaises(release.ReleaseError):
                release.switch(self.root, B, '发布')
        self.assertFalse(self.state()['pending'].get('recorded'))
        self.assertEqual(self.journal().count('| 发布完成：'), expected)

    def test_completion_state_failure_recovers_in_fresh_process_without_duplicate(self):
        self.completion_write_failure()
        script = ('import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); '
                  'from frclyo import release; release.restart_board = lambda root: None; '
                  'release.recover(Path(sys.argv[2]))')
        result = subprocess.run([sys.executable, '-B', '-c', script, str(BIN), str(self.root)],
                                cwd=self.root, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.journal().count('| 发布完成：'), 1)
        self.assertNotIn('pending', self.state())

    def test_append_succeeded_but_record_raised_is_idempotent(self):
        record = release.record
        def fail(change, undo):
            record(change, undo)
            if change.startswith('发布完成：'):
                raise OSError('fixture: failure after durable append')
        with patch.object(release, 'record', side_effect=fail):
            with self.assertRaises(release.ReleaseError):
                release.switch(self.root, B, '发布')
        release.recover(self.root)
        self.assertEqual(self.journal().count('| 发布完成：'), 1)

    def test_recovery_completion_annotation_does_not_break_deduplication(self):
        release.begin(self.root, '发布', A, B, 'aw 回滚 ' + A)
        release.atomic_link(self.root, B)
        write = release.write_json
        def fail(path, value):
            if path == release.state_path(self.root) and value.get('pending', {}).get('recorded'):
                raise OSError('fixture: recovery state write failed')
            return write(path, value)
        with patch.object(release, 'write_json', side_effect=fail):
            with self.assertRaises(release.ReleaseError):
                release.recover(self.root)
        self.assertIn('恢复中断的发布', self.journal())
        release.recover(self.root)
        self.assertEqual(self.journal().count('| 发布完成：'), 1)

    def test_later_real_publication_of_same_pair_gets_its_own_record(self):
        release.switch(self.root, B, '发布')
        release.switch(self.root, A, '回滚')
        self.completion_write_failure(expected=2)
        release.recover(self.root)
        self.assertEqual(self.journal().count('| 发布完成：'), 2)
        rows = [line for line in self.journal().splitlines() if '| 发布完成：' in line]
        self.assertNotEqual(rows[0].split('操作 ID：')[1], rows[1].split('操作 ID：')[1])

    def test_legacy_pending_reuses_completion_since_latest_start(self):
        state = release.begin(self.root, '发布', A, B, 'aw 回滚 ' + A)
        state['pending'].pop('operation_id', None)
        release.atomic_link(self.root, B)
        release.record('发布完成：%s → %s' % (A, B), 'aw 回滚 ' + A)
        release.write_json(release.state_path(self.root), state)
        release.recover(self.root)
        self.assertEqual(self.journal().count('| 发布完成：'), 1)
        self.assertNotIn('pending', self.state())

    def test_legacy_pending_does_not_reuse_older_publication(self):
        release.switch(self.root, B, '发布')
        release.switch(self.root, A, '回滚')
        state = release.begin(self.root, '发布', A, B, 'aw 回滚 ' + A)
        state['pending'].pop('operation_id', None)
        release.write_json(release.state_path(self.root), state)
        release.atomic_link(self.root, B)
        release.recover(self.root)
        self.assertEqual(self.journal().count('| 发布完成：'), 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
