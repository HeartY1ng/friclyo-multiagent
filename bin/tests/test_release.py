"""发布回归：所有数据在 resolve 后的私有临时目录，git/launchctl/ps/lsof 都是替身。"""
import contextlib
import importlib.util
import fcntl
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

BIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BIN))
from frclyo import release

A, B, C = 'a' * 40, 'b' * 40, 'c' * 40

# 替身只实现发布实际需要的只读 git 命令；不运行 git commit/push，也不访问远端网络。
FAKE = r'''#!PYTHON
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
base = pathlib.Path(os.environ['RELEASE_FIXTURE'])
state = json.loads((base / 'fake.json').read_text())
args = sys.argv[1:]
with (base / 'commands.jsonl').open('a') as log:
    log.write(json.dumps([name, *args]) + '\n')
if name == 'launchctl':
    sys.exit(state.get('launchctl_rc', 0))
if name == 'ps':
    print(state.get('ps', ''))
    sys.exit(state.get('ps_rc', 0))
if name == 'lsof':
    print(state.get('lsof', ''))
    if state.get('lsof_warning'):
        print('fixture: information may be incomplete', file=sys.stderr)
    sys.exit(state.get('lsof_rc', 0))
assert name == 'git' and args[0] == '-C', args
repo, args = pathlib.Path(args[1]), args[2:]
head = state['stable'] if repo.name in ('框架', '框架.迁移前') else state['dev']
if args == ['rev-parse', '--show-toplevel']:
    print(repo)
elif args[0] == 'status':
    print(state.get('dirty', ''))
elif args == ['rev-parse', '--verify', 'HEAD^{commit}']:
    print(head)
elif args[0] == 'ls-tree':
    print(state.get('tree', ''))
elif args[0] == 'symbolic-ref':
    sys.exit(1) if state.get('detached') else print('main')
elif args[:2] == ['config', '--get']:
    print('origin' if args[2].endswith('.remote') else 'refs/heads/main')
elif args[:2] == ['remote', 'get-url']:
    print(state.get('remote', str(base / 'remote.git')))
elif args[:2] == ['rev-parse', '--verify']:
    print(state['dev'])
elif args[0] == 'merge-base':
    sys.exit(0 if state.get('pushed', True) else 1)
elif args[0] == 'archive':
    sys.stdout.buffer.write((base / (args[-1] + '.tar')).read_bytes())
else:
    raise AssertionError(args)
'''

# 睡眠替身沿用真实入口的物理路径固定和锁协议，切换后再读版本文件检测是否串版。
SLEEPER = '''#!/bin/bash
set -eu
BIN_DIR=$(cd "$(dirname "$0")" && pwd -P)
if [ "${FRCLYO_RELEASE_PIN:-}" != "$$:$BIN_DIR/WORKER-worker.sh" ]; then
  exec python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1]); from frclyo.release import pin_worker; pin_worker(sys.argv[1], "WORKER", sys.argv[2:])' "$BIN_DIR" "$@"
fi
cd "$BIN_DIR/.."
pwd -P > "$1"
while [ ! -f "$2" ]; do sleep 0.05; done
pwd -P > "$3"
cat "$BIN_DIR/../version.txt" >> "$3"
'''


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        # 环境覆盖仅作用于替身测试，不查宿主配置、台账、钥匙串或真实进程。
        self.temp = tempfile.TemporaryDirectory(prefix='release-test-', dir=Path(tempfile.gettempdir()).resolve())
        self.base = Path(self.temp.name).resolve()
        self.addCleanup(self.dispose)
        self.root = self.base / 'data'
        self.root.mkdir()
        self.stable, self.dev = self.root / '框架', self.root / '框架-开发'
        self.stable.mkdir()
        self.dev.mkdir()
        (self.stable / '.git').mkdir()
        (self.stable / 'KEEP').write_text('original repository')
        (self.dev / '.git').mkdir()
        (self.base / 'remote.git').mkdir()
        self.fakebin = self.base / 'fakebin'
        self.fakebin.mkdir()
        for name in ('git', 'ps', 'lsof', 'launchctl'):
            path = self.fakebin / name
            path.write_text(FAKE.replace('PYTHON', sys.executable, 1))
            path.chmod(0o755)
        self.data = dict(stable=A, dev=B)
        self.save()
        self.env = dict(FRCLYO_HOME=str(self.root), HOME=str(self.base / 'home'),
                        FRCLYO_CONFIG=str(self.base / 'absent.json'),
                        RELEASE_FIXTURE=str(self.base), PYTHONDONTWRITEBYTECODE='1',
                        PATH=str(self.fakebin) + os.pathsep + os.environ['PATH'])
        patch = mock.patch.dict(os.environ, self.env)
        patch.start()
        self.addCleanup(patch.stop)
        self.make_archive(A)
        self.make_archive(B)
        self.make_archive(C)

    def dispose(self):
        # 只解除此测试创建的只读目录；临时目录清理不会触及真实版本。
        release.permissions(self.base, False)
        self.temp.cleanup()

    def save(self, **changes):
        self.data.update(changes)
        (self.base / 'fake.json').write_text(json.dumps(self.data))

    def make_archive(self, version, malicious=None):
        import tarfile
        tree = self.base / ('source-' + version)
        (tree / 'bin').mkdir(parents=True)
        (tree / 'board').mkdir()
        shutil.copytree(BIN / 'frclyo', tree / 'bin/frclyo', ignore=shutil.ignore_patterns('__pycache__'))
        # 验收替身可被指定失败；把 argv/cwd 保存到版本树外，检查 --paid 的真实转发。
        (tree / 'bin/aw').write_text('''import json, os, pathlib, sys
base = pathlib.Path(os.environ['RELEASE_FIXTURE'])
with (base / 'verify.jsonl').open('a') as stream:
    stream.write(json.dumps(dict(argv=sys.argv[1:], cwd=os.getcwd())) + '\\n')
settings = json.loads((base / 'fake.json').read_text())
if settings.get('verify_cache'):
    cache = pathlib.Path.cwd() / 'bin/frclyo/__pycache__'
    cache.mkdir(exist_ok=True)
    (cache / 'generated.cpython-test.pyc').write_bytes(b'generated cache')
if settings.get('verify_mutation'):
    (pathlib.Path.cwd() / 'version.txt').write_text('modified during verification')
sys.exit(settings.get('verify_rc', 0))
''')
        for worker in ('codex', 'opencode'):
            path = tree / 'bin' / (worker + '-worker.sh')
            path.write_text(SLEEPER.replace('WORKER', worker))
            path.chmod(0o755)
        (tree / 'board/board.py').write_text('# fake board\n')
        (tree / 'version.txt').write_text(version + '\n')
        with tarfile.open(self.base / (version + '.tar'), 'w') as archive:
            for path in sorted(tree.iterdir()):
                archive.add(path, arcname=path.name)
            if malicious:
                item = tarfile.TarInfo(malicious)
                item.size = 3
                archive.addfile(item, io.BytesIO(b'bad'))

    def run_aw(self, *args, input=None):
        # 子进程从临时目录运行，避免 git/路径解析走受保护的交付父目录。
        return subprocess.run([sys.executable, '-B', str(BIN / 'aw'), *args],
                              cwd=self.base, capture_output=True, text=True, input=input, timeout=30)

    def invoke(self, *args):
        result = self.run_aw(*args)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def migrate(self):
        self.invoke('发布', '--迁移')
        self.assertEqual(release.current(self.root), A)

    def journal(self):
        return (self.root / '台账/改动台账.md').read_text()

    def test_migration_archive_readonly_backup_and_undo(self):
        self.migrate()
        version = self.root / '框架-版本' / A
        self.assertFalse((version / '.git').exists())
        self.assertEqual((self.root / '框架.迁移前/KEEP').read_text(), 'original repository')
        self.assertTrue((self.root / '框架.迁移前/.git').is_dir())
        for path in [version, *version.rglob('*')]:
            self.assertEqual(path.stat().st_mode & 0o222, 0)
        self.invoke('release', '--undo-migration')
        self.assertFalse(self.stable.is_symlink())
        self.assertTrue((self.stable / '.git').is_dir())
        self.assertFalse((self.root / '框架.迁移前').exists())
        self.assertIn('迁移完成', self.journal())
        self.assertIn('撤销迁移完成', self.journal())
        self.assertTrue(version.exists())

    def test_release_paid_rollback_default_and_explicit(self):
        self.migrate()
        self.invoke('发布', '--付费验收')
        self.assertEqual(release.current(self.root), B)
        evidence = json.loads((self.base / 'verify.jsonl').read_text().splitlines()[-1])
        self.assertEqual(evidence['argv'], ['验收', '--paid'])
        self.assertEqual(Path(evidence['cwd']).parent, self.root / '框架-版本')
        self.assertEqual(release.read_json(release.state_path(self.root))['previous'], A)
        self.invoke('回滚')
        self.assertEqual(release.current(self.root), A)
        self.invoke('rollback', B)
        self.assertEqual(release.current(self.root), B)
        self.assertIn('aw 回滚 ' + A, self.journal())
        calls = [json.loads(line) for line in (self.base / 'commands.jsonl').read_text().splitlines()]
        restarts = [line for line in calls if line[0] == 'launchctl']
        self.assertEqual(len(restarts), 4)
        self.assertEqual(restarts[0][1:], ['kickstart', '-k', 'gui/%d/com.frclyo.board' % os.getuid()])

    def test_verification_failure_never_switches_or_restarts(self):
        self.migrate()
        before = self.journal()
        self.save(verify_rc=9)
        result = self.run_aw('release')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('验收失败', result.stderr)
        self.assertEqual(release.current(self.root), A)
        self.assertEqual(self.journal(), before)
        self.assertFalse((self.root / '框架-版本' / B).exists())
        self.assertFalse(list((self.root / '框架-版本').glob('.candidate-*')))

    def test_dirty_unpushed_network_remote_and_submodules_fail_before_verify(self):
        self.migrate()
        for bad in (dict(dirty='?? extra.py'), dict(pushed=False),
                    dict(remote='https://example.invalid/repo.git'),
                    dict(tree='160000 commit ' + B + '\tchild'), dict(detached=True)):
            with self.subTest(bad=bad):
                self.save(dirty='', pushed=True, remote=str(self.base / 'remote.git'), tree='', detached=False)
                self.save(**bad)
                self.assertNotEqual(self.run_aw('release').returncode, 0)
                self.assertEqual(release.current(self.root), A)
                self.assertFalse((self.base / 'verify.jsonl').exists())

    def test_migration_requires_idle_and_does_not_overwrite_backup(self):
        self.save(ps='51 1 bash /somewhere/codex-worker.sh task r1')
        self.assertNotEqual(self.run_aw('release', '--migrate').returncode, 0)
        self.assertTrue((self.stable / '.git').exists())
        self.save(ps='')
        (self.root / '框架.迁移前').mkdir()
        self.assertNotEqual(self.run_aw('release', '--migrate').returncode, 0)
        self.assertTrue((self.stable / '.git').exists())

    def test_restart_failure_reports_already_switched(self):
        self.migrate()
        self.save(launchctl_rc=3)
        result = self.run_aw('release')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(release.current(self.root), B)
        self.assertIn('版本已切换', result.stderr)
        self.assertIn('看板重启失败', self.journal())
        self.assertEqual(release.read_json(release.state_path(self.root))['previous'], A)

    def test_versions_counts_deduplicate_and_protect_cwd_users(self):
        self.migrate()
        self.invoke('release')
        old = self.root / '框架-版本' / A
        self.save(ps='10 1 bash %s/bin/codex-worker.sh task r1\n11 10 bash %s/bin/codex-worker.sh task r1\n12 1 python tool.py' % (old, old),
                  lsof='p12\nn' + str(old))
        result = self.invoke('版本')
        self.assertIn(A + '\t上一个\t工人：1', result.stdout)
        self.assertIn(B + '\t当前\t工人：0', result.stdout)
        self.save(ps_rc=1)
        result = self.run_aw('versions')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('未知', result.stdout)
        self.assertIn('禁止清理', result.stderr)
        self.save(ps_rc=0, lsof_warning=True)
        result = self.run_aw('versions')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('未知', result.stdout)

    def test_running_worker_keeps_physical_version_and_lease_after_switch(self):
        self.migrate()
        started, go, ended = [self.base / name for name in ('started', 'go', 'ended')]
        proc = subprocess.Popen(['bash', str(self.stable / 'bin/codex-worker.sh'),
                                 str(started), str(go), str(ended)], cwd=self.base,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 10
            while not started.exists() and proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(started.exists())
            old = self.root / '框架-版本' / A
            self.assertEqual(started.read_text().strip(), str(old))
            with self.assertRaises(release.ReleaseError):
                with release.unused_version(self.root, A):
                    self.fail('运行中的工人必须持有共享锁')
            self.save(ps='%d 1 bash %s/bin/codex-worker.sh task r1' % (proc.pid, old),
                      lsof='p%d\nn%s' % (proc.pid, old))
            self.invoke('release')
            self.assertEqual(release.current(self.root), B)
            self.assertIn(A + '\t上一个\t工人：1', self.invoke('versions').stdout)
            go.touch()
            out, err = proc.communicate(timeout=10)
            self.assertEqual(proc.returncode, 0, out + err)
            self.assertEqual(ended.read_text().splitlines(), [str(old), A])
        finally:
            if proc.poll() is None:
                go.touch()
                proc.terminate()
            proc.communicate(timeout=10)
        with release.unused_version(self.root, A):
            pass

    def test_clean_preview_and_confirmed_delete_protect_current_previous_and_busy(self):
        self.migrate()
        self.invoke('release')
        self.save(dev=C)
        self.invoke('release')
        old = self.root / '框架-版本' / A
        preview = self.invoke('clean')
        self.assertIn('可清理旧版本：' + A, preview.stdout)
        self.assertTrue(old.exists())
        self.save(ps='10 1 bash %s/bin/opencode-worker.sh task r1' % old)
        self.assertNotIn('可清理旧版本：' + A, self.invoke('clean').stdout)
        self.save(ps='')
        self.assertNotEqual(self.run_aw('clean', '--yes', input='wrong\n').returncode, 0)
        self.assertTrue(old.exists())
        self.invoke('clean')
        result = self.run_aw('clean', '--yes', input=str(self.root) + '\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(old.exists())
        self.assertTrue((self.root / '框架-版本' / B).exists())
        self.assertTrue((self.root / '框架-版本' / C).exists())
        self.assertTrue((self.root / '框架.迁移前/.git').exists())

    def test_cleanup_cannot_delete_between_process_scan_and_worker_start(self):
        self.migrate()
        self.invoke('release')
        self.save(dev=C)
        self.invoke('release')
        lease = self.root / '运行时/发布' / (A + '.lease')
        with lease.open('a+b') as stream:
            fcntl.flock(stream, fcntl.LOCK_SH)
            with self.assertRaises(release.ReleaseError):
                release.cleanup_delete(self.root, [A])
        self.assertTrue((self.root / '框架-版本' / A).exists())

    def test_pending_switch_recovers_previous_version_and_journal(self):
        self.migrate()
        release.prepare(self.root, self.dev, B, verify=True)
        release.begin(self.root, '发布', A, B, 'aw 回滚 ' + A)
        release.atomic_link(self.root, B)
        with release.locked(self.root):
            state = release.read_json(release.state_path(self.root))
            self.assertNotIn('pending', state)
            self.assertEqual(state['previous'], A)
        self.assertIn('恢复中断的发布', self.journal())
        self.invoke('rollback')
        self.assertEqual(release.current(self.root), A)

    def test_interrupted_migration_restores_original_directory(self):
        release.layout(self.root, True)
        release.prepare(self.root, self.stable, A)
        release.begin(self.root, '迁移', None, A, '撤销迁移')
        self.stable.rename(self.root / '框架.迁移前')
        with release.locked(self.root):
            self.assertTrue((self.stable / '.git').exists())
            self.assertNotIn('pending', release.read_json(release.state_path(self.root)))

    def test_state_lock_journal_and_target_errors_leave_old_link(self):
        self.migrate()
        release.prepare(self.root, self.dev, B, verify=True)
        with release.locked(self.root):
            self.assertNotEqual(self.run_aw('rollback', B).returncode, 0)
        with mock.patch.object(release.changelog, 'append', side_effect=ValueError('broken')):
            with self.assertRaises(ValueError):
                release.switch(self.root, B, '发布')
        self.assertEqual(release.current(self.root), A)
        for version in ('../outside', 'b', C):
            self.assertNotEqual(self.run_aw('rollback', version).returncode, 0)
            self.assertEqual(release.current(self.root), A)

    def test_tampered_version_and_unsafe_archive_are_rejected(self):
        self.migrate()
        self.invoke('release')
        old = self.root / '框架-版本' / A / 'version.txt'
        old.chmod(0o644)
        self.assertNotEqual(self.run_aw('rollback', A).returncode, 0)
        old.write_text('tampered')
        old.chmod(0o444)
        self.assertNotEqual(self.run_aw('rollback', A).returncode, 0)
        self.assertEqual(release.current(self.root), B)
        # 重新制作一个带穿越项的归档，只在临时目录操作。
        shutil.rmtree(self.base / ('source-' + C))
        self.make_archive(C, '../ESCAPE')
        self.save(dev=C)
        self.assertNotEqual(self.run_aw('release').returncode, 0)
        self.assertFalse((self.root / '框架-版本/ESCAPE').exists())
        self.assertEqual(release.current(self.root), B)

    def test_generated_bytecode_is_removed_but_changed_source_blocks_publish(self):
        self.migrate()
        self.save(verify_cache=True)
        self.invoke('release')
        self.assertFalse(list((self.root / '框架-版本' / B).rglob('*.pyc')))
        self.save(dev=C, verify_mutation=True)
        result = self.run_aw('release')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('验收改动了候选源码', result.stderr)
        self.assertEqual(release.current(self.root), B)

    def test_atomic_switch_never_exposes_missing_framework(self):
        import threading
        self.migrate()
        release.prepare(self.root, self.dev, B, verify=True)
        stop, observed, errors = threading.Event(), set(), []
        def reader():
            while not stop.is_set():
                try:
                    observed.add((self.stable / 'version.txt').read_text().strip())
                except OSError as exc:
                    errors.append(type(exc).__name__)
        thread = threading.Thread(target=reader)
        thread.start()
        try:
            for index in range(80):
                release.atomic_link(self.root, A if index % 2 else B)
        finally:
            stop.set()
            thread.join()
        self.assertFalse(errors)
        self.assertTrue(observed)
        self.assertLessEqual(observed, {A, B})

    def test_final_journal_failure_reports_the_actual_switched_state(self):
        self.migrate()
        release.prepare(self.root, self.dev, B, verify=True)
        original = release.record
        def fail_final(change, undo):
            if change.startswith('发布完成'):
                raise OSError('fixture disk failure')
            return original(change, undo)
        with mock.patch.object(release, 'record', side_effect=fail_final):
            with self.assertRaisesRegex(release.ReleaseError, '版本已切换'):
                release.switch(self.root, B, '发布')
        self.assertEqual(release.current(self.root), B)
        self.assertEqual(release.read_json(release.state_path(self.root))['previous'], A)
        self.assertIn('发布开始', self.journal())

    def test_symlink_management_directory_is_rejected(self):
        external = self.base / 'external'
        external.mkdir()
        (self.root / '框架-版本').symlink_to(external, target_is_directory=True)
        self.assertNotEqual(self.run_aw('release', '--migrate').returncode, 0)
        self.assertEqual(list(external.iterdir()), [])
        self.assertTrue((self.stable / '.git').exists())

    def test_entry_pinning_and_launchd_path_stays_logical(self):
        self.migrate()
        physical = self.stable.resolve()
        self.assertEqual(release.installed_entry(physical), self.stable)
        self.assertEqual(release.installed_entry(self.dev), self.dev)
        for worker in ('codex', 'opencode'):
            text = (BIN / (worker + '-worker.sh')).read_text()
            self.assertIn('pwd -P', text)
            self.assertIn('frclyo.release import pin_worker', text)
        board = (BIN.parent / 'board/board.py').read_text()
        self.assertIn('os.path.realpath(__file__)', board)
        aw = (BIN / 'aw').read_text()
        self.assertIn("str(runtime('release').installed_entry(ROOT) / 'board/board.py')", aw)
        self.assertIn('Path(__file__).resolve()', aw)


if __name__ == '__main__':
    unittest.main(verbosity=2)
