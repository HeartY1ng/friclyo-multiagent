"""修好后：撤销本次 bootstrap，保留安装前已有且相同的 plist。"""
import os
import unittest
from unittest import mock
from uninstall_support import Repro


class TestB58(Repro):
    def reinstall_changed_plist(self):
        # B99：真实安装两次生成不同指纹的计划，系统服务只用内存替身。
        self.install()
        first = self.aw.fingerprint(self.plist)
        with mock.patch.object(self.aw, 'confirm', return_value=True):
            self.quiet(self.aw.board, ['stop'])
        self.plist.unlink()
        with mock.patch.dict(os.environ, PATH=str(self.base / 'new-tools') + os.pathsep + os.environ['PATH']):
            self.install()
        self.assertNotEqual(first, self.aw.fingerprint(self.plist))
        self.assertTrue(self.loaded)
        self.calls.clear()

    def test_b99_reinstalled_service_uses_latest_plan(self):
        self.reinstall_changed_plist()
        self.assertEqual(self.uninstall()[0], 0)
        self.assertEqual(self.calls, ['bootout'])
        self.assertFalse(self.loaded)
        self.assertFalse(self.plist.exists())
        undone = {r.get('id') for r in self.rows() if r['action'] == 'undone'}
        self.assertTrue(all(r['id'] in undone for r in self.rows() if r.get('kind') == 'launchd'))
        self.assertEqual(self.uninstall()[0], 0)

    def test_b99_latest_stop_failure_keeps_all_plans_and_plist(self):
        # 最新计划失败不能回退到旧计划授权删除；失败后仍可重试。
        self.reinstall_changed_plist()
        self.bootout_error = True
        self.assertEqual(self.uninstall()[0], 1)
        self.assertEqual(self.calls, ['bootout', 'print'])
        self.assertTrue(self.loaded)
        self.assertTrue(self.plist.exists())
        undone = {r.get('id') for r in self.rows() if r['action'] == 'undone'}
        self.assertTrue(all(r['id'] not in undone for r in self.rows() if r.get('kind') == 'launchd'))
        self.bootout_error = False
        self.assertEqual(self.uninstall()[0], 0)

    def test_b99_retry_after_latest_plan_recorded_stopped(self):
        # 最新停服已落账、旧计划尚未处理时中断：重试只收尾，不再使用旧指纹。
        self.reinstall_changed_plist()
        latest = [r for r in self.rows() if r.get('kind') == 'launchd'][-1]
        record = self.aw.record

        def interrupt(action, **fields):
            record(action, **fields)
            if action == 'undone' and fields.get('id') == latest['id']:
                raise KeyboardInterrupt

        with mock.patch.object(self.aw, 'record', side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.uninstall()
        self.assertFalse(self.loaded)
        self.assertTrue(self.plist.exists())
        self.calls.clear()
        self.assertEqual(self.uninstall()[0], 0)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.plist.exists())

    def test_preexisting_identical_plist_can_undo_bootstrap(self):
        self.install()
        original = self.plist.read_bytes()
        self.assertEqual(self.uninstall()[0], 0)
        # 构造用户事先放置了正确 plist、但服务尚未加载的机器状态。
        self.plist.parent.mkdir(parents=True, exist_ok=True)
        self.plist.write_bytes(original)
        self.install()
        self.assertTrue(self.loaded)
        self.calls.clear()
        rc, output = self.uninstall()
        self.evidence(rc=rc, service_running=self.loaded, launchctl_calls=self.calls,
                      plist_unchanged=self.plist.read_bytes() == original,
                      falsely_reports_modified='plist 已变化' in output)
        self.assertFalse(self.loaded, '本次启动的服务因 skip 文件无活动指纹记录而无法撤销')


if __name__ == '__main__':
    unittest.main(verbosity=2)
