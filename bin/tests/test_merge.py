"""合并回归只用临时完整树、真实 merge-file 和故障替身，不碰宿主业务数据。"""
import contextlib
import io
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

BIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BIN))
from frclyo import merge


class MergeTests(unittest.TestCase):
    def setUp(self):
        # 先 resolve 临时父目录；macOS 的 /var 别名不能混入路径断言。
        parent = Path(tempfile.gettempdir()).resolve()
        self.temp = tempfile.TemporaryDirectory(prefix='merge-test-', dir=parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.base = self.root / '素材/TEST-MERGE/框架'
        self.worker = self.root / '交付/TEST-MERGE/r1/交付/框架'
        self.received = self.root / '交付/TEST-MERGE/r1/输入/框架'
        self.dev = self.root / 'dev'
        for path in (self.base, self.worker, self.dev, self.received):
            path.mkdir(parents=True)
        patch = mock.patch.dict(os.environ, {'FRCLYO_HOME': str(self.root)})
        patch.start()
        self.addCleanup(patch.stop)

    def put(self, root, name, data, mode=0o644):
        # 每份夹具独立创建，避免硬链接使一次修改影响另一棵树。
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data.encode() if isinstance(data, str) else data)
        path.chmod(mode)
        return path

    def three(self, name, base, worker, dev, modes=(0o644, 0o644, 0o644)):
        # None 表示这一侧不存在文件，覆盖新增和删除的组合。
        if base is not None:
            self.put(self.received, name, base, modes[0])
        for root, data, mode in zip((self.base, self.worker, self.dev), (base, worker, dev), modes):
            if data is not None:
                self.put(root, name, data, mode)

    def run_merge(self, *extra):
        # 捕获报告便于检查冲突片段和计数，同时不隐藏命令返回值。
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = merge.main(['TEST-MERGE', 'r1', '--到', str(self.dev), *extra])
        return code, output.getvalue(), errors.getvalue()

    def tree(self):
        # 比较内容和权限；读取自身会影响 atime，因此不拿 atime 当写入证据。
        return merge.snapshot(self.dev)

    def test_worker_unchanged_preserves_developer_change(self):
        self.three('keep.py', 'x=0\n', 'x=0\n', 'x=2\n')
        before = self.tree()
        self.assertEqual(self.run_merge()[0], 0)
        self.assertEqual(self.tree(), before)

    def test_adopt_and_add_keep_executable_permissions(self):
        self.three('bin/aw', 'old', 'new', 'old', (0o755, 0o600, 0o751))
        self.three('scripts/new.sh', None, '#!/bin/bash\necho ok\n', None, (0o644, 0o750, 0o644))
        code, out, err = self.run_merge()
        self.assertEqual(code, 0, err)
        self.assertEqual((self.dev / 'bin/aw').read_bytes(), b'new')
        self.assertEqual(stat.S_IMODE((self.dev / 'bin/aw').stat().st_mode), 0o751)
        self.assertEqual(stat.S_IMODE((self.dev / 'scripts/new.sh').stat().st_mode), 0o750)
        self.assertIn('采用 1，三方合并 0，冲突 0，新增 1', out)
        self.assertIn('权限变化 0', out)

    def test_mode_only_change_warned_but_preserved(self):
        self.three('bin/aw', 'same', 'same', 'same', (0o755, 0o644, 0o755))
        before = self.tree()
        code, out, _ = self.run_merge('--只许', '*.py')
        self.assertEqual(code, 0)
        self.assertIn('仅工人权限改变', out)
        self.assertIn('范围外改动提醒', out)
        self.assertEqual(self.tree(), before)

    def test_b103_normalized_input_modes_are_not_worker_edits(self):
        for name in ('bin/tool', 'note.txt'):
            self.three(name, 'same', 'same', 'same', (0o755, 0o600, 0o751))
            (self.received / name).chmod(0o600)
        before = self.tree()
        for options in (('--试跑', '--只许', '*.py'), ()):
            code, out, err = self.run_merge(*options)
            self.assertEqual(code, 0, err)
            self.assertNotIn('仅工人权限改变', out)
            self.assertNotIn('范围外改动提醒', out)
            self.assertIn('工人改了 0 个文件的权限', out)
            self.assertEqual(self.tree(), before)
        (self.worker / 'bin/tool').chmod(0o700)
        code, out, err = self.run_merge()
        self.assertEqual(code, 0, err)
        self.assertIn('仅工人权限改变，保留开发版权限：bin/tool', out)
        self.assertNotIn('仅工人权限改变，保留开发版权限：note.txt', out)
        self.assertIn('工人改了 1 个文件的权限', out)
        self.assertEqual(self.tree(), before)

    def test_b103_reports_modes_with_content_changes_and_equal_material_mode(self):
        self.three('changed.py', 'x=0\n', 'x=1\n', 'x=0\n', (0o755, 0o755, 0o751))
        self.three('same.py', 'x=0\n', 'x=0\n', 'x=0\n', (0o755, 0o755, 0o751))
        self.three('new.py', None, 'new=1\n', None, (0o644, 0o700, 0o644))
        for name in ('changed.py', 'same.py'):
            (self.received / name).chmod(0o600)
        code, out, err = self.run_merge()
        self.assertEqual(code, 0, err)
        self.assertIn('工人权限改变，保留开发版权限：changed.py', out)
        self.assertIn('仅工人权限改变，保留开发版权限：same.py', out)
        self.assertIn('工人改了 2 个文件的权限', out)
        self.assertEqual((self.dev / 'changed.py').read_text(), 'x=1\n')
        self.assertEqual(stat.S_IMODE((self.dev / 'changed.py').stat().st_mode), 0o751)
        self.assertEqual(stat.S_IMODE((self.dev / 'new.py').stat().st_mode), 0o700)

    def test_b103_missing_received_snapshot_requires_input_without_writing(self):
        self.three('a.py', 'x=0\n', 'x=1\n', 'x=0\n')
        (self.received / 'a.py').unlink()
        self.received.rmdir()
        before = self.tree()
        code, _, err = self.run_merge()
        self.assertEqual(code, 1)
        self.assertIn('需要输入', err)
        self.assertEqual(self.tree(), before)

    def test_nonoverlapping_three_way_keeps_both_edits(self):
        self.three('both.py', 'a=0\nb=0\nc=0\nd=0\ne=0\n',
                   'a=1\nb=0\nc=0\nd=0\ne=0\n', 'a=0\nb=0\nc=0\nd=0\ne=2\n')
        code, out, err = self.run_merge()
        self.assertEqual(code, 0, err)
        self.assertEqual((self.dev / 'both.py').read_text(), 'a=1\nb=0\nc=0\nd=0\ne=2\n')
        self.assertIn('三方合并 1', out)

    def test_conflict_does_not_write_file_but_applies_other_file(self):
        self.three('conflict.py', 'a=0\n', 'a=1\n', 'a=2\n')
        self.three('good.py', 'b=0\n', 'b=1\n', 'b=0\n')
        code, out, _ = self.run_merge()
        self.assertEqual(code, 1)
        self.assertEqual((self.dev / 'conflict.py').read_text(), 'a=2\n')
        self.assertEqual((self.dev / 'good.py').read_text(), 'b=1\n')
        for text in ('<<<<<<< 开发版', '||||||| 基准', '>>>>>>> 工人交付', '冲突 1'):
            self.assertIn(text, out)

    def test_union_retains_two_test_methods_and_checks_syntax(self):
        base = 'class Tests:\n    def test_old(self):\n        pass\n'
        worker = base + '\n    def test_worker(self):\n        assert 1\n'
        dev = base + '\n    def test_developer(self):\n        assert 2\n'
        self.three('tests/test_demo.py', base, worker, dev)
        self.assertEqual(self.run_merge()[0], 1)
        code, out, err = self.run_merge('--并集', 'tests/test_demo.py')
        self.assertEqual(code, 0, err)
        data = (self.dev / 'tests/test_demo.py').read_bytes()
        self.assertIn(b'def test_worker', data)
        self.assertIn(b'def test_developer', data)
        compile(data, 'test_demo.py', 'exec')
        self.assertIn('三方合并 1', out)

    def test_invalid_union_rolls_back_whole_batch(self):
        self.three('a.py', 'a=0\n', 'a=1\n', 'a=0\n')
        self.three('union.py', 'x=(\n    0\n)\n', 'x=(\n    1\n)\n', 'x=(\n    2\n)\n')
        before = self.tree()
        code, _, err = self.run_merge('--并集', 'union.py')
        self.assertEqual(code, 1)
        self.assertIn('语法错误', err)
        self.assertIn('整体回滚', err)
        self.assertEqual(self.tree(), before)

    def test_b101_extensionless_union_uses_shebang(self):
        for number, header in enumerate(('#!/usr/bin/env python3', '#!/usr/bin/python3',
                                         '#!/usr/bin/env -S python3 -B', '#!/usr/bin/python3.13',
                                         '#!/usr/bin/env bash', '#!/bin/bash', '#!/bin/sh')):
            with self.subTest(header=header):
                name = 'bin/tool%d' % number
                self.three(name, header + '\nx=0\n', header + '\nx=1\n', header + '\nx=2\n')
                before = self.tree()
                code, _, err = self.run_merge('--并集', name, '--试跑')
                self.assertEqual(code, 0, err)
                self.assertEqual(self.tree(), before)
                code, _, err = self.run_merge('--并集', name)
                self.assertEqual(code, 0, err)
                result = (self.dev / name).read_text()
                self.assertIn('x=1', result)
                self.assertIn('x=2', result)
                for root in (self.base, self.worker, self.dev):
                    (root / name).unlink()

    def test_b101_extensionless_syntax_errors_restore_whole_batch(self):
        for header, bad, label in (('#!/usr/bin/env python3', 'def broken(\n', 'Python'),
                                    ('#!/usr/bin/env bash', 'if true; then\n', 'Shell')):
            for existing in (False, True):
                with self.subTest(header=header, existing=existing):
                    old = header + '\nx=0\n' if existing else None
                    self.three('bin/broken', old, header + '\n' + bad, old)
                    self.three('a.py', 'a=0\n', 'a=1\n', 'a=0\n')
                    self.three('removed', 'keep', None, 'keep')
                    before = self.tree()
                    code, _, err = self.run_merge('--允许删除', '--试跑')
                    self.assertEqual(code, 1)
                    self.assertIn(label + ' 语法错误', err)
                    self.assertEqual(self.tree(), before)
                    code, _, err = self.run_merge('--允许删除')
                    self.assertEqual(code, 1)
                    self.assertIn('整体回滚', err)
                    self.assertEqual(self.tree(), before)
                    for root in (self.base, self.worker, self.dev):
                        (root / 'bin/broken').unlink(missing_ok=True)

    def test_b101_invalid_extensionless_union_rolls_back(self):
        header = '#!/usr/bin/env python3\n'
        self.three('bin/tool', header + 'x=(\n0\n)\n',
                   header + 'x=(\n1\n)\n', header + 'x=(\n2\n)\n')
        self.three('a.py', 'a=0\n', 'a=1\n', 'a=0\n')
        before = self.tree()
        code, _, err = self.run_merge('--并集', 'bin/tool')
        self.assertEqual(code, 1)
        self.assertIn('Python 语法错误', err)
        self.assertIn('整体回滚', err)
        self.assertEqual(self.tree(), before)

    def test_b101_shebang_checkers_never_execute_sources_or_startup_files(self):
        marker = self.root / 'MUST-NOT-EXECUTE'
        startup = self.put(self.root, 'startup', 'touch "%s"\n' % marker)
        self.three('bin/shell', None, '#!/usr/bin/env bash\ntouch "%s"\n' % marker, None)
        self.three('bin/python', None, '#!/usr/bin/env python3\nraise RuntimeError("no execution")\n', None)
        with mock.patch.dict(os.environ, {'BASH_ENV': str(startup), 'ENV': str(startup)}):
            code, _, err = self.run_merge()
        self.assertEqual(code, 0, err)
        self.assertFalse(marker.exists())
        self.assertFalse(list(self.dev.rglob('__pycache__')))

    def test_b101_actual_aw_polyglot_union_and_python_syntax(self):
        original = (BIN / 'aw').read_bytes()
        self.three('bin/aw', original, original + b'\nworker_added = 1\n',
                   original + b'\ndeveloper_added = 2\n')
        code, _, err = self.run_merge('--并集', 'bin/aw')
        self.assertEqual(code, 0, err)
        result = (self.dev / 'bin/aw').read_bytes()
        self.assertIn(b'worker_added = 1', result)
        self.assertIn(b'developer_added = 2', result)
        self.three('bin/aw', result, result + b'\ndef broken(\n', result)
        before = self.tree()
        code, _, err = self.run_merge()
        self.assertEqual(code, 1)
        self.assertIn('Python 语法错误', err)
        self.assertEqual(self.tree(), before)

    def test_b101_unknown_or_nonfirst_shebang_cannot_enable_union(self):
        for header in ('#!/usr/bin/env ruby', '# comment\n#!/usr/bin/env python3',
                       '#!/usr/bin/env', '#!/usr/bin/env "python3', '#!/usr/bin/env python3-fake'):
            with self.subTest(header=header):
                self.three('bin/tool', header + '\nx=0\n', header + '\nx=1\n', header + '\nx=2\n')
                before = self.tree()
                code, _, err = self.run_merge('--并集', 'bin/tool')
                self.assertEqual(code, 1)
                self.assertIn('并集需要可检查', err)
                self.assertEqual(self.tree(), before)

    def test_b101_aw_launcher_shell_syntax_is_checked_too(self):
        original = (BIN / 'aw').read_bytes()
        broken = original.replace(b'command -v python3', b'if then; command -v python3', 1)
        # 包装位于 Python 字符串内，单独 compile 不能识别它的 Shell 语法错误。
        compile(broken, 'bin/aw', 'exec')
        self.three('bin/aw', original, broken, original)
        before = self.tree()
        code, _, err = self.run_merge()
        self.assertEqual(code, 1)
        self.assertIn('Shell 语法错误', err)
        self.assertEqual(self.tree(), before)

    def test_b114_launcher_flags_preserve_both_syntax_checks(self):
        original = (BIN / 'aw').read_bytes()
        for command in (b'exec python3 -B "$0" "$@"',
                        b'exec python3 -I -B "$0" "$@"',
                        b'exec python3 -B "$0" "$@" # flags'):
            modified = original.replace(b'exec python3 "$0" "$@"', command, 1)
            for options in (('--试跑',), ()):
                with self.subTest(command=command, options=options):
                    self.three('bin/aw', original, modified, original)
                    code, _, err = self.run_merge(*options)
                    self.assertEqual(code, 0, err)
                    self.assertEqual((self.dev / 'bin/aw').read_bytes(),
                                     original if options else modified)
            for bad, label in ((modified + b'\ndef broken(\n', 'Python'),
                               (modified.replace(b'command -v python3', b'if then; command -v python3', 1), 'Shell')):
                self.three('bin/aw', original, bad, original)
                self.three('a.py', 'a=0\n', 'a=1\n', 'a=0\n')
                before = self.tree()
                code, _, err = self.run_merge()
                self.assertEqual(code, 1)
                self.assertIn(label + ' 语法错误', err)
                self.assertIn('整体回滚', err)
                self.assertEqual(self.tree(), before)

    def test_b114_changed_launcher_flags_also_work_with_union(self):
        original = (BIN / 'aw').read_bytes()
        modified = original.replace(b'exec python3 "$0" "$@"', b'exec python3 -B "$0" "$@"', 1)
        self.three('bin/aw', original, modified + b'\nworker_added=1\n',
                   original + b'\ndeveloper_added=2\n')
        code, _, err = self.run_merge('--并集', 'bin/aw')
        self.assertEqual(code, 0, err)
        result = (self.dev / 'bin/aw').read_bytes()
        self.assertIn(b'exec python3 -B', result)
        self.assertIn(b'worker_added=1', result)
        self.assertIn(b'developer_added=2', result)

    def test_b115_union_repeated_without_duplicate_registrations_or_writes(self):
        base = 'TESTS = []\n'
        self.three('tests.py', base, base + 'TESTS.append("worker")\n',
                   base + 'TESTS.append("developer")\n', (0o644, 0o600, 0o751))
        options = ('--并集', 'tests.py')
        code, _, err = self.run_merge(*options)
        self.assertEqual(code, 0, err)
        before = self.tree()
        mtime = (self.dev / 'tests.py').stat().st_mtime_ns
        for extra in (('--试跑',), (), ()):
            code, out, err = self.run_merge(*options, *extra)
            self.assertEqual(code, 0, err)
            self.assertIn('三方合并 0', out)
            self.assertEqual(self.tree(), before)
            self.assertEqual((self.dev / 'tests.py').stat().st_mtime_ns, mtime)
        namespace = {}
        exec((self.dev / 'tests.py').read_bytes(), namespace)
        self.assertEqual(namespace['TESTS'], ['developer', 'worker'])

    def test_b115_union_preserves_intentional_repetitions_and_multiple_hunks(self):
        # 相同语句出现在两个不同函数、同一工人块内各两次，均不能被全局去重。
        base = ('def one():\n    pass\n' + '\n# gap\n' * 10 + 'def two():\n    pass\n')
        worker = base.replace('    pass\n', '    calls.append("worker")\n' * 2)
        current = base.replace('    pass\n', '    calls.append("developer")\n')
        self.three('tests.py', base, worker, current)
        options = ('--并集', 'tests.py')
        self.assertEqual(self.run_merge(*options)[0], 0)
        first = (self.dev / 'tests.py').read_bytes()
        self.assertEqual(first.count(b'calls.append("worker")'), 4)
        for _ in range(2):
            code, _, err = self.run_merge(*options)
            self.assertEqual(code, 0, err)
            self.assertEqual((self.dev / 'tests.py').read_bytes(), first)

    def test_b115_union_does_not_deduplicate_matching_code_in_other_context(self):
        base = 'def existing():\n    register("worker")\n' + '\n# gap\n' * 10 + 'def target():\n    pass\n'
        worker = base.replace('    pass\n', '    register("worker")\n')
        current = base.replace('    pass\n', '    register("developer")\n')
        self.three('tests.py', base, worker, current)
        options = ('--并集', 'tests.py')
        self.assertEqual(self.run_merge(*options)[0], 0)
        first = (self.dev / 'tests.py').read_bytes()
        self.assertEqual(first.count(b'register("worker")'), 2)
        self.assertEqual(self.run_merge(*options)[0], 0)
        self.assertEqual((self.dev / 'tests.py').read_bytes(), first)

    def test_b115_union_marker_like_source_and_developer_followup(self):
        base = 'TEXT = """\n<<<<<<< 开发版\n||||||| 基准\n=======\n>>>>>>> 工人交付\n"""\nTESTS=[]\n'
        worker = base + 'TESTS.append("worker")\n'
        current = base + 'TESTS.append("developer")\n'
        self.three('tests.py', base, worker, current)
        options = ('--并集', 'tests.py')
        self.assertEqual(self.run_merge(*options)[0], 0)
        first = (self.dev / 'tests.py').read_bytes() + b'followup=True\n'
        (self.dev / 'tests.py').write_bytes(first)
        code, _, err = self.run_merge(*options)
        self.assertEqual(code, 0, err)
        self.assertEqual((self.dev / 'tests.py').read_bytes(), first)

    def test_b115_union_boundaries_and_partial_worker_blocks(self):
        for number, (base, worker, current) in enumerate((
                ('', 'worker=1\n', 'developer=1\n'),
                ('value=0', 'worker=1', 'developer=1'),
                ('value=0\r\n', 'worker=1\r\n', 'developer=1\r\n'),
                ('value=0\n', 'worker=1\nworker=2\n', 'worker=1\ndeveloper=1\n'))):
            with self.subTest(number=number):
                name = 'boundary%d.py' % number
                self.three(name, base, worker, current)
                options = ('--并集', name)
                code, _, err = self.run_merge(*options)
                self.assertEqual(code, 0, err)
                first = (self.dev / name).read_bytes()
                self.assertIn(b'developer=1', first)
                self.assertIn(b'worker=1', first)
                if number == 3:
                    self.assertIn(b'worker=2', first)
                code, _, err = self.run_merge(*options)
                self.assertEqual(code, 0, err)
                self.assertEqual((self.dev / name).read_bytes(), first)
                for tree in (self.base, self.worker, self.dev, self.received):
                    (tree / name).unlink()

    def test_b115_unstable_union_of_adjacent_deletions_is_a_conflict(self):
        self.three('tests.py', 'a=0\nb=0\nc=0\n', 'a=0\nc=0\n', 'a=0\nb=0\n')
        before = self.tree()
        for extra in (('--试跑',), (), ()):
            code, out, err = self.run_merge('--并集', 'tests.py', *extra)
            self.assertEqual(code, 1, err)
            self.assertIn('并集重跑结果不稳定', out)
            self.assertEqual(self.tree(), before)

    def test_repeated_union_options_and_union_dry_run(self):
        # 多文件分别显式授权；试跑计算同样的并集但不写目标。
        for name in ('one.py', 'two.py'):
            self.three(name, 'base=0\n', 'base=1\n', 'base=2\n')
        before = self.tree()
        args = ('--并集', 'one.py', '--union', 'two.py')
        code, out, err = self.run_merge(*args, '--试跑')
        self.assertEqual(code, 0, err)
        self.assertIn('三方合并 2', out)
        self.assertEqual(self.tree(), before)
        self.assertEqual(self.run_merge(*args)[0], 0)
        for name in ('one.py', 'two.py'):
            data = (self.dev / name).read_text()
            self.assertIn('base=1', data)
            self.assertIn('base=2', data)

    def test_union_without_known_syntax_checker_stops_before_all_writes(self):
        self.three('a.py', 'a=0\n', 'a=1\n', 'a=0\n')
        self.three('note.txt', 'base\n', 'worker\n', 'developer\n')
        before = self.tree()
        code, _, err = self.run_merge('--并集', 'note.txt')
        self.assertEqual(code, 1)
        self.assertIn('并集需要可检查的 .py 或 .sh', err)
        self.assertEqual(self.tree(), before)

    def test_shell_startup_files_and_python_sources_are_not_executed(self):
        # 检查器必须只读语法，不能执行交付内容或环境指定的 shell 启动文件。
        marker = self.root / 'MUST-NOT-EXECUTE'
        startup = self.put(self.root, 'startup.sh', 'touch "%s"\n' % marker)
        self.three('script.sh', None, 'touch "%s"\n' % marker, None)
        self.three('script.py', None, 'raise RuntimeError("must not execute")\n', None)
        with mock.patch.dict(os.environ, {'BASH_ENV': str(startup), 'ENV': str(startup)}):
            code, _, err = self.run_merge()
        self.assertEqual(code, 0, err)
        self.assertFalse(marker.exists())
        self.assertFalse(list(self.dev.rglob('__pycache__')))

    def test_python_syntax_failure_restores_deletes_modes_and_new_directories(self):
        self.three('a.py', 'a=0\n', 'a=1\n', 'a=0\n', (0o755, 0o600, 0o751))
        self.three('remove', 'keep', None, 'keep')
        self.three('nested/new/bad.py', None, 'def broken(\n', None)
        before = self.tree()
        original_mtime = (self.dev / 'a.py').stat().st_mtime_ns
        code, _, err = self.run_merge('--允许删除')
        self.assertEqual(code, 1)
        self.assertIn('整体回滚', err)
        self.assertEqual(self.tree(), before)
        self.assertEqual((self.dev / 'a.py').stat().st_mtime_ns, original_mtime)
        self.assertFalse((self.dev / 'nested').exists())

    def test_shell_syntax_failure_rolls_back_and_never_executes_script(self):
        marker = self.root / 'MUST-NOT-EXIST'
        self.three('good.sh', '', 'touch "%s"\n' % marker, '')
        self.three('bad.sh', None, 'if true; then\n', None)
        before = self.tree()
        code, _, err = self.run_merge()
        self.assertEqual(code, 1)
        self.assertIn('Shell 语法错误', err)
        self.assertFalse(marker.exists())
        self.assertEqual(self.tree(), before)

    def test_dry_run_computes_merge_and_leaves_tree_untouched(self):
        self.three('a.py', 'a=0\n', 'a=1\n', 'a=0\n')
        self.three('new.py', None, 'b=1\n', None)
        self.three('removed', 'old', None, 'old')
        before = self.tree()
        code, out, err = self.run_merge('--试跑', '--允许删除')
        self.assertEqual(code, 0, err)
        self.assertIn('开发版未写入', out)
        self.assertEqual(self.tree(), before)

    def test_dry_run_reports_bad_syntax_without_creating_target(self):
        self.three('nested/bad.py', None, 'def (', None)
        before = self.tree()
        self.assertEqual(self.run_merge('--试跑')[0], 1)
        self.assertEqual(self.tree(), before)

    def test_deletion_is_opt_in_and_modified_developer_file_conflicts(self):
        self.three('unchanged', 'old', None, 'old')
        self.three('modified', 'old', None, 'new')
        before = self.tree()
        code, out, _ = self.run_merge()
        self.assertEqual(code, 0)
        self.assertIn('工人删除，默认保留', out)
        self.assertEqual(self.tree(), before)
        code, out, _ = self.run_merge('--允许删除')
        self.assertEqual(code, 1)
        self.assertFalse((self.dev / 'unchanged').exists())
        self.assertEqual((self.dev / 'modified').read_text(), 'new')
        self.assertIn('工人删除，但开发版修改', out)

    def test_developer_deletion_and_add_add_collisions(self):
        self.three('deleted.py', 'a=0\n', 'a=1\n', None)
        self.three('new.py', None, 'b=1\n', 'b=2\n')
        before = self.tree()
        code, out, _ = self.run_merge()
        self.assertEqual(code, 1)
        self.assertIn('冲突 2', out)
        self.assertEqual(self.tree(), before)

    def test_identical_changes_and_both_deleted_are_noops(self):
        self.three('same.py', 'a=0\n', 'a=1\n', 'a=1\n')
        self.three('gone.py', 'b=0\n', None, None)
        before = self.tree()
        self.assertEqual(self.run_merge('--允许删除')[0], 0)
        self.assertEqual(self.tree(), before)

    def test_binary_one_sided_copy_and_two_sided_conflict(self):
        self.three('image.bin', b'\0old', b'\0new', b'\0old')
        self.three('both.bin', b'\0old', b'\0new', b'\0dev')
        code, out, _ = self.run_merge()
        self.assertEqual(code, 1)
        self.assertEqual((self.dev / 'image.bin').read_bytes(), b'\0new')
        self.assertEqual((self.dev / 'both.bin').read_bytes(), b'\0dev')
        self.assertIn('二进制文件', out)

    def test_repeated_allow_patterns_warn_without_filtering(self):
        for name in ('bin/a.py', 'tests/b.py', 'docs/note.txt'):
            self.three(name, None, 'x=1\n', None)
        code, out, _ = self.run_merge('--只许', 'bin/*', '--只许', 'tests/*')
        self.assertEqual(code, 0)
        warnings = [line for line in out.splitlines() if line.startswith('范围外')]
        self.assertEqual(warnings, ['范围外改动提醒（--只许）：docs/note.txt'])
        self.assertTrue((self.dev / 'docs/note.txt').exists())

    def test_custom_base_and_english_and_chinese_entry_points(self):
        other = self.root / 'custom-base'
        self.base.rename(other)
        self.put(other, 'a.py', 'x=0\n')
        self.put(self.worker, 'a.py', 'x=1\n')
        self.put(self.dev, 'a.py', 'x=0\n')
        for command in ('merge', '合并'):
            result = subprocess.run([sys.executable, '-B', str(BIN / 'aw'), command,
                                     'TEST-MERGE', 'r1', '--to', str(self.dev), '--base', str(other),
                                     '--dry-run', '--only', '*.py'], cwd=self.root,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('采用 1', result.stdout)
        self.assertEqual((self.dev / 'a.py').read_text(), 'x=0\n')

    def test_argument_errors_and_help_do_not_read_configuration(self):
        # 非法路径在访问数据根前拒绝；help 也不能触发配置读取。
        with mock.patch.object(merge.config, 'root_dir', side_effect=AssertionError('must not read')):
            for args, expected in ((['--help'], 0), (['../task', 'r1', '--到', 'x'], 2),
                                   (['T', '../r1', '--到', 'x'], 2),
                                   (['T', 'r1', '--到', 'x', '--并集', '../x.py'], 2)):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        merge.main(args)
                self.assertEqual(raised.exception.code, expected)

    def test_git_metadata_excluded_and_unmodified_bad_python_not_checked(self):
        self.three('unchanged.py', 'invalid (', 'invalid (', 'invalid (')
        for root in (self.base, self.worker, self.dev):
            self.put(root, '.git/config', str(root))
        before = (self.dev / '.git/config').read_bytes()
        self.assertEqual(self.run_merge()[0], 0)
        self.assertEqual((self.dev / '.git/config').read_bytes(), before)

    def test_symlink_and_special_files_rejected_without_writes(self):
        # 链接仅指向本测试夹具；绝不把测试探针指向真实敏感目录。
        self.put(self.root, 'outside.txt', 'keep')
        for kind in ('symlink', 'fifo'):
            node = self.worker / 'unsafe'
            if kind == 'symlink':
                node.symlink_to(self.root / 'outside.txt')
            else:
                os.mkfifo(node)
            code, _, err = self.run_merge()
            self.assertEqual(code, 1)
            self.assertIn('不支持链接或特殊文件', err)
            self.assertEqual(self.tree(), ({}, set()))
            node.unlink()

    def test_destination_symlink_and_overlapping_roots_rejected(self):
        alias = self.root / 'alias'
        alias.symlink_to(self.dev, target_is_directory=True)
        code, _, err = self.run_merge('--到', str(alias))
        self.assertEqual(code, 1)
        self.assertIn('符号链接', err)
        code, _, err = self.run_merge('--到', str(self.base))
        self.assertEqual(code, 1)
        self.assertIn('互相包含', err)

    def test_file_directory_collisions_are_conflicts(self):
        self.put(self.worker, 'folder', 'file')
        self.put(self.dev, 'folder/keep', 'keep')
        self.put(self.worker, 'parent/new.py', 'a=1\n')
        self.put(self.dev, 'parent', 'keep')
        before = self.tree()
        code, out, _ = self.run_merge()
        self.assertEqual(code, 1)
        self.assertIn('冲突 2', out)
        self.assertEqual(self.tree(), before)

    def test_write_error_and_interrupt_restore_previous_files(self):
        for failure in (OSError('simulated disk failure'), KeyboardInterrupt()):
            self.three('a.py', 'a=0\n', 'a=1\n', 'a=0\n')
            self.three('b.py', 'b=0\n', 'b=1\n', 'b=0\n')
            before, original, calls = self.tree(), merge.atomic_write, []
            def fail_once(path, item):
                calls.append(path.name)
                if len(calls) == 2:
                    raise failure
                return original(path, item)
            with mock.patch.object(merge, 'atomic_write', side_effect=fail_once):
                if isinstance(failure, KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        self.run_merge()
                else:
                    self.assertEqual(self.run_merge()[0], 1)
            self.assertEqual(self.tree(), before)

    def test_file_directory_conversion_does_not_apply_half_the_change(self):
        # 允许删除也不能在新增路径冲突时只删掉原文件。
        self.three('file_to_dir', 'keep', None, 'keep')
        self.put(self.worker, 'file_to_dir/new.py', 'x=1\n')
        self.three('dir_to_file/old.py', 'x=0\n', None, 'x=0\n')
        self.put(self.worker, 'dir_to_file', 'replacement')
        before = self.tree()
        code, out, _ = self.run_merge('--允许删除')
        self.assertEqual(code, 1)
        self.assertIn('冲突 4', out)
        self.assertEqual(self.tree(), before)

    def test_rollback_failure_is_not_reported_as_success(self):
        self.three('bad.py', 'x=0\n', 'def (', 'x=0\n')
        original, calls = merge.atomic_write, []
        def fail_restore(path, item):
            calls.append(path.name)
            if len(calls) > 1:
                raise OSError('simulated restore failure')
            return original(path, item)
        with mock.patch.object(merge, 'atomic_write', side_effect=fail_restore):
            code, _, err = self.run_merge()
        self.assertEqual(code, 1)
        self.assertIn('回滚未完成，需要人工处理：bad.py', err)
        self.assertNotIn('已整体回滚', err)

    def test_git_failure_does_not_write_any_planned_file(self):
        self.three('a.py', 'a=0\n', 'a=1\n', 'a=0\n')
        self.three('both.py', 'b=0\n', 'b=1\n', 'b=2\n')
        before = self.tree()
        with mock.patch.object(merge, 'merge_bytes', side_effect=OSError('git unavailable')):
            code, _, _ = self.run_merge()
        self.assertEqual(code, 1)
        self.assertEqual(self.tree(), before)

    def test_changed_destination_after_plan_is_not_overwritten(self):
        self.three('a.py', 'a=0\n', 'a=1\n', 'a=0\n')
        base, _ = merge.snapshot(self.base)
        worker, _ = merge.snapshot(self.worker)
        current, directories = self.tree()
        plan = merge.make_plan(base, worker, current, directories, set(), [], False)
        self.put(self.dev, 'a.py', 'a=9\n')
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(merge.MergeError, '计划后发生变化'):
                merge.apply_plan(self.dev, current, plan)
        self.assertEqual((self.dev / 'a.py').read_text(), 'a=9\n')

    def test_atomic_replace_publishes_complete_bytes_with_original_mode(self):
        self.three('readonly.py', 'a=0\n', 'a=1\n', 'a=0\n', (0o555, 0o600, 0o555))
        real_replace, seen = os.replace, []
        def inspect(source, destination):
            source, destination = Path(source), Path(destination)
            self.assertEqual(source.parent, destination.parent)
            self.assertEqual(source.read_bytes(), b'a=1\n')
            self.assertEqual(stat.S_IMODE(source.stat().st_mode), 0o555)
            seen.append(destination)
            return real_replace(source, destination)
        with mock.patch.object(merge.os, 'replace', side_effect=inspect):
            self.assertEqual(self.run_merge()[0], 0)
        self.assertEqual(seen, [self.dev / 'readonly.py'])
        self.assertFalse(list(self.dev.glob('.aw-merge-*')))


if __name__ == '__main__':
    unittest.main(verbosity=2)
