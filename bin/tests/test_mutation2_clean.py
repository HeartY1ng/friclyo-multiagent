"""第二轮清理回归：仅操作合成目录，不使用真实账本或宿主退出证据。"""
import contextlib
import fcntl
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from frclyo import clean
import test_clean


class Mutation2CleanTests(unittest.TestCase):
    def setUp(self):
        # 复用现有安全夹具，不继承其测试方法，避免重复运行原有用例。
        self.fixture=test_clean.CleanTests('runTest');self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root=self.fixture.root

    def test_external_cleanup_includes_recently_closed_round(self):
        # 卸载根外临时节点没有一天保留期；刚收卡的安全节点也应清理。
        _,_,control,paths=self.fixture.fixture(age=0.5)
        external=[p for p,_ in paths if not p.is_relative_to(self.root)]
        self.assertTrue(external)
        with contextlib.redirect_stdout(io.StringIO()):clean.uninstall_external(self.root)
        self.assertTrue(all(not p.exists() for p in external))
        self.assertTrue((control/'receipt.json').exists())

    def test_cleanup_lock_is_private(self):
        # 清理锁不能因数值变异向其他用户开放执行权限。
        (self.root/'运行时').mkdir()
        previous=os.umask(0o022)
        try:
            with mock.patch.object(clean,'plan',return_value=[]),mock.patch.object(clean,'record'):
                clean.delete(self.root,0,[])
        finally:
            os.umask(previous)
        self.assertEqual((self.root/'运行时/.清理.lock').stat().st_mode & 0o777,0o600)

    def test_concurrent_cleanup_stops_before_record_or_delete(self):
        # 真实 flock 在本轮合成锁上竞争；第二位清理者必须立即拒绝。
        runtime=self.root/'运行时';runtime.mkdir()
        with (runtime/'.清理.lock').open('w') as held:
            fcntl.flock(held.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            with mock.patch.object(clean,'plan',return_value=[]),mock.patch.object(clean,'record') as record:
                with self.assertRaises(BlockingIOError):clean.delete(self.root,0,[])
            record.assert_not_called()

    def test_unicode_line_separator_in_path_is_safely_journalled(self):
        # 路径中的 U+2028 只能以转义形式进入 Markdown 台账，否则会被换行校验拒绝。
        (self.root/'运行时').mkdir()
        path=self.root/'synthetic\u2028node';path.write_text('fixture')
        row={'path':path,'identity':clean.identity(path.stat()),'kind':'file','task':'T','round':'r1'}
        with mock.patch.object(clean,'plan',return_value=[row]), \
                mock.patch.object(clean,'closed_cards',return_value={'T':0}), \
                mock.patch.object(clean,'round_paths',return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()):
            clean.delete(self.root,0,[row])
        journal=(self.root/'台账/改动台账.md').read_text()
        self.assertFalse(path.exists())
        self.assertIn('清理开始',journal)
        self.assertIn('清理结束；完整删除 1/1 个节点',journal)
        self.assertNotIn('\u2028',journal)
        self.assertIn('\\u2028',journal)

    def test_uninspectable_external_path_has_actionable_diagnostic(self):
        # 注入拒绝仅是测试结果；不得静默略过无法检查的候选。
        path=mock.Mock();path.is_relative_to.return_value=False
        path.lstat.side_effect=PermissionError('synthetic denial')
        with mock.patch.object(clean,'names',return_value=['T']), \
                mock.patch.object(clean.isolation,'codex_tmp',return_value=path), \
                mock.patch.object(clean.isolation,'round_tmp',return_value=path), \
                contextlib.redirect_stderr(io.StringIO()) as output:
            with self.assertRaises(PermissionError):clean.uninstall_external(self.root)
        self.assertIn('无法检查根外临时目录',output.getvalue())

if __name__=='__main__':unittest.main()
