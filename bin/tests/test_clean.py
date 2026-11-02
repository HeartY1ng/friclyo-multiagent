"""合成账本/进程证据；不读取宿主数据，不调用真实模型。"""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

BIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BIN))
from frclyo import clean, isolation, ledger


class CleanTests(unittest.TestCase):
    def setUp(self):
        # 夹具必须有独立 ASCII HOME；不自动逃到宿主共享临时目录。
        temporary_parent = Path(tempfile.gettempdir()).resolve()
        if not str(temporary_parent).isascii():
            raise RuntimeError('此测试需要 ASCII 物理临时目录；请显式设置 TMPDIR，不回退到共享 /tmp')
        self.temp = tempfile.TemporaryDirectory(prefix='clean-test-', dir=temporary_parent)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'data'
        self.root.mkdir()
        self.home = self.base / 'home'
        self.home.mkdir()
        self.assertTrue(str(self.home).isascii())
        self.env = dict(HOME=str(self.home), FRCLYO_HOME=str(self.root),
                        FRCLYO_CONFIG=str(self.base / 'absent.json'))
        patch = mock.patch.dict(os.environ, self.env)
        patch.start()
        self.addCleanup(patch.stop)
        self.now = int(time.time())
        self.events = []
        self.count = 0

    def fixture(self, worker='codex', age=8, action='完成', round_id='r1'):
        self.count += 1
        task = 'CL-20261001-%02d' % self.count
        stamp = self.now - age * 86400
        self.events.append(ledger.event('开卡', task, stamp=stamp-100,
                           标题='fixture', 项目='fixture', 来源='CL', 派活人='test', 父任务=''))
        if action == '完成':
            self.events.append(ledger.event(action, task, stamp=stamp, 结论='通过', 摘要='fixture'))
        elif action == '放弃':
            self.events.append(ledger.event(action, task, stamp=stamp, 原因='fixture'))
        (self.root / '账本.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in self.events))
        run = self.root / '交付' / task / round_id
        run.mkdir(parents=True)
        self.write(run / '运行信息.json', {'工人': worker, '进程号': 2147483647})
        control = self.root / '运行时/控制' / worker / task / round_id
        control.mkdir(parents=True)
        self.write(control / 'status.json', {'group_reaped': True})
        self.write(control / 'receipt.json', {'工人': worker, '任务编号': task,
                   '轮次编号': round_id, '进程组已回收': True, '结束': ledger.utc(stamp-10)})
        paths = clean.round_paths(self.root, task, round_id, stamp)
        for path, kind in paths:
            self.assertTrue(path.is_relative_to(self.base), path)
            path.parent.mkdir(parents=True, exist_ok=True)
            if kind == 'dir':
                path.mkdir(exist_ok=True)
                (path / 'sentinel').write_text('private data')
            else:
                path.write_text('template')
        return task, run, control, paths

    def write(self, path, value):
        path.write_text(json.dumps(value))

    def plan(self, days=7):
        with contextlib.redirect_stderr(io.StringIO()):
            return clean.plan(self.root, days, self.now)

    def test_b100_legacy_card_uses_supplied_root_without_config(self):
        # 使用传入根转换旧路径；账本原字节不变，禁止间接读取业务配置。
        task, _, _, _ = self.fixture()
        self.events[0]['卡片文件'] = 'runs/' + task + '-r1.txt'
        path = self.root / '账本.jsonl'
        path.write_text(''.join(json.dumps(e) + '\n' for e in self.events))
        original = path.read_bytes()
        with mock.patch.object(clean.config, 'load', side_effect=AssertionError('不应解析业务配置')), \
                mock.patch.object(clean.ledger, 'fold', wraps=ledger.fold) as fold:
            self.assertIn(task, clean.closed_cards(self.root, 0, self.now))
        self.assertEqual(fold.call_args.args[0][0]['卡片文件'], str(self.root / '任务卡' / (task + '-r1.txt')))
        self.assertEqual(path.read_bytes(), original)

    def test_b100_legacy_card_still_rejects_escaping_paths(self):
        # 坏配置兼容不能放宽旧卡片路径的越界校验。
        self.fixture()
        for card in ('runs/../outside', 'runs//absolute'):
            with self.subTest(card=card):
                self.events[0]['卡片文件'] = card
                (self.root / '账本.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in self.events))
                with self.assertRaisesRegex(ledger.LedgerError, '越界'):
                    clean.closed_cards(self.root, 0, self.now)

    def test_interrupted_control_removal_is_retryable_at_every_boundary(self):
        # 覆盖普通文件、两份退出证据和最后 rmdir 后的中断窗口。
        for boundary in ('sentinel', 'status.json', 'receipt.json', 'rmdir'):
            task, run, control, paths = self.fixture()
            original_unlink, original_rmdir = os.unlink, os.rmdir
            fired = []
            def unlink(name, *args, **kwargs):
                original_unlink(name, *args, **kwargs)
                if name == boundary and not fired:
                    fired.append(name)
                    raise KeyboardInterrupt
            def rmdir(name, *args, **kwargs):
                original_rmdir(name, *args, **kwargs)
                if boundary == 'rmdir' and name == control.name and not fired:
                    fired.append(name)
                    raise KeyboardInterrupt
            with contextlib.redirect_stdout(io.StringIO()), \
                 contextlib.redirect_stderr(io.StringIO()):
                first = [row for row in self.plan() if row['task'] == task]
                with mock.patch.object(clean.os, 'unlink', side_effect=unlink), \
                     mock.patch.object(clean.os, 'rmdir', side_effect=rmdir):
                    with self.assertRaises(KeyboardInterrupt):
                        clean.delete(self.root, 7, first)
                self.assertTrue(fired, boundary)
                retry = [row for row in self.plan() if row['task'] == task]
                self.assertTrue(retry, boundary)
                clean.delete(self.root, 7, retry)
            self.assertFalse(control.exists(), boundary)
            self.assertFalse(clean.retry_path(control).exists(), boundary)
            self.assertTrue(run.exists())

    def test_retry_proof_cannot_authorize_replacement_directory(self):
        task, _, control, _ = self.fixture()
        clean.prepare_retry(control, clean.identity(control.stat()))
        moved = control.with_name('retained-original')
        control.rename(moved)
        control.mkdir()
        (control / 'KEEP').write_text('unrelated replacement')
        self.assertEqual(self.plan(), [])
        self.assertEqual((control / 'KEEP').read_text(), 'unrelated replacement')

    def test_cutoff_terminal_and_unknown_tasks(self):
        old, _, _, oldpaths = self.fixture()
        abandoned, _, _, abandonedpaths = self.fixture(worker='opencode', action='放弃')
        self.fixture(age=6)
        self.fixture(age=20, action='开卡')
        self.fixture(age=7)
        rows = self.plan()
        self.assertEqual({r['task'] for r in rows}, {old, abandoned})
        self.assertEqual(len(rows), len(oldpaths) + len(abandonedpaths))
        self.assertTrue(all(r['bytes'] >= 0 for r in rows))
        self.assertEqual(self.plan(90), [])

    def test_dry_run_has_no_writes_and_cli_aliases(self):
        _, _, _, paths = self.fixture()
        before = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        for command in ('clean', '清理'):
            proc = subprocess.run([sys.executable, '-B', str(BIN / 'aw'), command],
                                  env=dict(os.environ), capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn('字节', proc.stdout)
            self.assertIn(str(paths[0][0]), proc.stdout)
        after = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        self.assertFalse((self.root / '台账').exists())

    def test_yes_still_needs_confirmation(self):
        self.fixture()
        for answer in ('no', EOFError()):
            kwargs = {'side_effect': answer} if isinstance(answer, Exception) else {'return_value': answer}
            with mock.patch('builtins.input', **kwargs), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(clean.main(['--yes']), 1)
            self.assertEqual(len(self.plan()), 2)
        self.assertFalse((self.root / '台账').exists())

    def test_empty_plan_yes_still_needs_confirmation(self):
        # 原失败的直接分支：候选被安全检查排除时，也不能绕过 --yes 确认。
        with mock.patch.object(clean, 'plan', return_value=[]), \
             mock.patch.object(clean, 'delete') as delete, \
             contextlib.redirect_stdout(io.StringIO()):
            for answer in ('no', EOFError()):
                kwargs = {'side_effect': answer} if isinstance(answer, Exception) else {'return_value': answer}
                with mock.patch('builtins.input', **kwargs) as prompt:
                    self.assertEqual(clean.main(['--yes']), 1)
                    prompt.assert_called_once()
            with mock.patch('builtins.input', return_value=str(self.root)):
                self.assertEqual(clean.main(['--yes']), 0)
            with mock.patch('builtins.input') as prompt:
                self.assertEqual(clean.main([]), 0)
                prompt.assert_not_called()
            delete.assert_not_called()
        self.assertFalse((self.root / '台账').exists())

    def test_delete_preserves_protected_trees_and_logs(self):
        _, run, _, paths = self.fixture(worker='opencode')
        _, _, _, codex = self.fixture()
        for name in ('任务卡', '素材', '配置', '台账', '交付'):
            (self.root / name).mkdir(exist_ok=True)
            (self.root / name / 'KEEP').write_text('unchanged')
        before = (self.root / '账本.jsonl').read_bytes()
        outside = self.base / 'outside'
        outside.mkdir()
        (outside / 'KEEP').write_text('unchanged')
        (paths[0][0] / 'link').symlink_to(outside, target_is_directory=True)
        with mock.patch('builtins.input', return_value=str(self.root)), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(clean.main(['--yes']), 0)
        for path, _ in paths + codex:
            self.assertFalse(path.exists())
        self.assertTrue(run.is_dir())
        self.assertEqual(before, (self.root / '账本.jsonl').read_bytes())
        for name in ('任务卡', '素材', '配置', '台账', '交付'):
            self.assertEqual((self.root / name / 'KEEP').read_text(), 'unchanged')
        self.assertEqual((outside / 'KEEP').read_text(), 'unchanged')
        log = (self.root / '台账/改动台账.md').read_text()
        self.assertIn('清理结束；完整删除 7/7', log)
        self.assertIn('不可自动撤销', log)
        self.assertEqual(self.plan(), [])

    def test_alive_missing_unreaped_and_wrong_evidence_are_kept(self):
        _, run, control, _ = self.fixture()
        self.write(run / '运行信息.json', {'工人': 'codex', '进程号': os.getpid()})
        self.assertEqual(self.plan(), [])
        self.write(run / '运行信息.json', {'工人': 'codex', '进程号': 2147483647})
        self.write(control / 'status.json', {'group_reaped': False})
        self.assertEqual(self.plan(), [])
        self.write(control / 'status.json', {'group_reaped': True})
        receipt = json.loads((control / 'receipt.json').read_text())
        receipt['任务编号'] = 'wrong'
        self.write(control / 'receipt.json', receipt)
        self.assertEqual(self.plan(), [])
        (control / 'status.json').unlink()
        self.assertEqual(self.plan(), [])

    def test_top_link_and_link_ancestor_are_kept(self):
        _, _, _, paths = self.fixture()
        path = paths[0][0]
        moved = path.with_name('outside')
        path.rename(moved)
        path.symlink_to(moved, target_is_directory=True)
        self.assertEqual(self.plan(), [])
        path.unlink()
        moved.rename(path)
        parent = path.parent
        movedparent = parent.with_name('other-parent')
        parent.rename(movedparent)
        parent.symlink_to(movedparent, target_is_directory=True)
        self.assertEqual(self.plan(), [])
        self.assertTrue((movedparent / path.name / 'sentinel').exists())

    def test_recheck_after_confirmation_aborts_before_delete(self):
        _, run, _, paths = self.fixture()
        rows = self.plan()
        self.write(run / '运行信息.json', {'工人': 'codex', '进程号': os.getpid()})
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(clean.Unsafe):
            clean.delete(self.root, 7, rows)
        self.assertTrue(all(path.exists() for path, _ in paths))
        self.assertFalse((self.root / '台账').exists())

    def test_log_failure_prevents_delete(self):
        self.fixture()
        rows = self.plan()
        with mock.patch.object(clean, 'record', side_effect=OSError('fixture')), self.assertRaises(OSError):
            clean.delete(self.root, 7, rows)
        self.assertTrue(all(row['path'].exists() for row in rows))

    def test_template_referenced_by_another_round_is_preserved(self):
        task, _, _, paths = self.fixture(worker='opencode', round_id='A-r1')
        other = self.root / '交付' / (task + '-A') / 'r1'
        other.mkdir(parents=True)
        self.assertEqual(self.plan(), [])
        self.assertTrue(all(path.exists() for path, _ in paths))

    def test_new_and_legacy_templates_are_both_recognized(self):
        task, _, _, paths = self.fixture(worker='opencode')
        templates = {p for p, kind in paths if kind == 'file'}
        self.assertEqual({p.name for p in templates},
                         {'worker-' + task + '.r1.json', 'worker-' + task + '-r1.json'})
        self.assertTrue(templates <= {row['path'] for row in self.plan()})
        for separator in ('.', '-'):
            path = next(p for p in templates if p.name == 'worker-' + task + separator + 'r1.json')
            path.unlink()
            self.assertEqual({row['path'] for row in self.plan()},
                             {p for p, _ in paths if p != path})
            path.write_text('template')

    def test_missing_ambiguous_legacy_name_does_not_block_new_template(self):
        task, _, _, paths = self.fixture(worker='opencode', round_id='A-r1')
        (self.root / '交付' / (task + '-A') / 'r1').mkdir(parents=True)
        legacy = self.root / '运行时/opencode' / ('worker-' + task + '-A-r1.json')
        legacy.unlink()
        self.assertEqual({row['path'] for row in self.plan()},
                         {p for p, _ in paths if p != legacy})

    def test_ascii_fallback_derivation_without_reading_host(self):
        root = self.base / '中文'
        run = root / '交付/CL-20261001-99/r1'
        self.assertEqual(isolation.round_tmp(root, run).parent, Path('/private/tmp'))
        self.assertEqual(isolation.codex_tmp(root, run, self.base / '中文家').parent,
                         Path('/private/tmp') / ('frclyo-work-%s' % os.getuid()))


if __name__ == '__main__':
    unittest.main()
