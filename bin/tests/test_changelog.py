"""只操作临时台账；覆盖两个独立进程竞争及中英文命令。"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

AW = Path(__file__).resolve().parents[1] / 'aw'


class ChangelogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.env = dict(os.environ, FRCLYO_HOME=str(self.root))
        self.path = self.root / '台账/改动台账.md'

    def invoke(self, *args):
        return subprocess.run([sys.executable, '-B', str(AW), *args], env=self.env,
                              capture_output=True, text=True)

    def test_b20_append_waits_for_existing_allocator_lock(self):
        # 两个进程偶然错开也会让“去掉锁”的版本通过；先持锁再启动追加确保竞争发生。
        import fcntl
        import time
        self.path.parent.mkdir()
        ready = self.root / 'ready'
        code = ('import sys; from pathlib import Path; '
                'sys.path.insert(0, sys.argv[1]); from frclyo import changelog; '
                'Path(sys.argv[2]).write_text("ready"); '
                'print(changelog.append("新增", "撤销"))')
        with (self.path.parent / '.改动台账.lock').open('a+b') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            process = subprocess.Popen([sys.executable, '-B', '-c', code, str(AW.parent), str(ready)],
                                       env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 5
                while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists(), '追加进程必须先启动，不能把启动失败当锁生效')
                time.sleep(0.3)
                blocked = process.poll() is None
                no_write = not self.path.exists()
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
                try:
                    stdout, stderr = process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                    raise
        self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(stdout, '1\n')
        self.assertTrue(blocked, '其他追加持锁时不能提前分配并发布编号')
        self.assertTrue(no_write, '拿到锁之前不能读取旧编号并追加台账')

    def test_concurrent_append_uses_maximum_and_preserves_history(self):
        self.path.parent.mkdir()
        original = '| 编号 | 改动 | 撤销 |\n|---|---|---|\n| #9 | old | undo |\n| 2 | earlier | undo |'
        self.path.write_text(original)
        processes = [subprocess.Popen([sys.executable, '-B', str(AW), *args], env=self.env,
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for args in (
                     ['台账', '追加', '--改动', '甲', '--撤销', '撤甲'],
                     ['changelog', 'append', '--change', '乙', '--undo', '撤乙'])]
        outputs = []
        for process in processes:
            out, err = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, err)
            outputs.append(out)
        self.assertEqual(set(outputs), {'10\n', '11\n'})
        content = self.path.read_text()
        self.assertTrue(content.startswith(original + '\n'))
        self.assertEqual(len(content.splitlines()), 6)

    def test_new_file_and_invalid_cells(self):
        result = self.invoke('台账', '追加', '--改动', '新增', '--撤销', '删除')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '1\n')
        original = self.path.read_bytes()
        for option in ('--change', '--undo'):
            for bad in ('a|b', 'a\nb', 'a\rb', 'a\u2028b'):
                args = ['changelog', 'append', '--change', 'ok', '--undo', 'ok']
                args[args.index(option) + 1] = bad
                result = self.invoke(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, '')
                self.assertEqual(self.path.read_bytes(), original)

    def test_legacy_four_column_tables_are_complete(self):
        """真实台账开头是旧版 4 栏表格（# | 改了什么 | 为什么 | 怎么撤销）：不能当成残缺而拒绝追加。"""
        self.path.parent.mkdir()
        legacy = ('| # | 改了什么 | 为什么 | 怎么撤销 |\n|---|---|---|---|\n'
                  '| 1 | 旧改动 | 旧原因 | 旧撤销 |\n| 2 | 键 | 新值 | 撤销 |\n\n'
                  '| 编号 | 改动 | 撤销 |\n|---|---|---|\n| 7 | 新格式改动 | 新格式撤销 |\n')
        self.path.write_text(legacy, encoding='utf-8')
        result = self.invoke('台账', '追加', '--改动', '下一项', '--撤销', '撤销下一项')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '8\n')
        self.assertTrue(self.path.read_text(encoding='utf-8').startswith(legacy))

    def test_partial_numbered_rows_rejected_without_changing_history(self):
        """B64：编号后或单元格内截断均须明确报错，不静默封口或复用编号。"""
        self.path.parent.mkdir()
        header = '| 编号 | 改动 | 撤销 |\n|---|---|---|\n| 11 | 完整改动 | 完整撤销 |\n'
        for tail in ('| 12', '| #12 ', '| 12 | 写到一半的改动',
                     '| 12 | 改动 | 撤销缺封口', '| 12 | 改动 |'):
            for suffix in ('', '\n', '\n| 3 | 较小编号 | 撤销 |\n'):
                with self.subTest(tail=tail, suffix=suffix):
                    before = (header + tail + suffix).encode('utf-8')
                    self.path.write_bytes(before)
                    result = self.invoke('changelog', 'append', '--change', '下一项', '--undo', '撤销下一项')
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(result.stdout, '')
                    self.assertIn('第 4 行（编号 12）记录残缺', result.stderr)
                    self.assertEqual(self.path.read_bytes(), before)
        # 人工补全原编号后才可继续，下一号必须为 13。
        self.path.write_text(header + '| 12 | 已补全 | 撤销 |')
        result = self.invoke('changelog', 'append', '--change', '下一项', '--undo', '撤销下一项')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '13\n')


if __name__ == '__main__':
    unittest.main()
