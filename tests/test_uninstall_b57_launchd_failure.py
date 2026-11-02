"""修好后：未确认服务停止，不删其 plist；查询错误不能当成服务不存在。"""
import unittest
from unittest import mock
from uninstall_support import Repro


class TestB57(Repro):
    def test_failed_stop_keeps_plist(self):
        self.install()
        self.bootout_error = True
        rc, _ = self.uninstall()
        self.evidence(rc=rc, service_running=self.loaded, plist_exists=self.plist.exists())
        self.assertTrue(self.plist.exists(), '停止失败却删除了仍在运行服务的 plist')
        self.assertEqual(rc, 1)
        plans = [r for r in self.rows() if r.get('kind') == 'launchd']
        undone = {r.get('id') for r in self.rows() if r['action'] == 'undone'}
        self.assertTrue(all(r['id'] not in undone for r in plans))
        self.bootout_error = False
        self.assertEqual(self.uninstall()[0], 0)
        self.assertFalse(self.plist.exists())
        self.assertFalse(self.loaded)

    def test_failed_status_does_not_authorize_data_deletion(self):
        self.install()
        self.bootout_error = self.print_error = True
        with mock.patch('builtins.input', return_value=str(self.work)):
            rc, _ = self.uninstall('--delete-data')
        self.evidence(rc=rc, service_running=self.loaded, work_exists=self.work.exists())
        self.assertTrue(self.work.exists(), '服务未停止、查询也失败，仍报告成功并删除数据根')
        self.assertEqual(rc, 1)
        self.assertTrue(self.plist.exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
