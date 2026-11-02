"""第二轮模型查询回归：替身管道/选择器/时钟，不启动真实 CLI。"""
import contextlib
import sys
import unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from frclyo import models

class Mutation2ModelsTests(unittest.TestCase):
    @contextlib.contextmanager
    def rpc(self):
        process=mock.Mock(pid=123)
        selector=mock.MagicMock()
        selector.__enter__.return_value=selector
        selector.select.return_value=[(None,None)]
        response=b'{"id":1,"result":{}}\n{"id":2,"result":{"data":[],"nextCursor":null}}\n'
        with mock.patch.object(models.subprocess,'Popen',return_value=process), \
                mock.patch.object(models.os,'set_blocking'), \
                mock.patch.object(models.os,'killpg'), \
                mock.patch.object(models.os,'write',side_effect=lambda fd,data:len(data)) as write, \
                mock.patch.object(models.os,'read',return_value=response), \
                mock.patch.object(models.selectors,'DefaultSelector',return_value=selector), \
                mock.patch.object(models.time,'monotonic',return_value=0):
            yield process,selector,write

    def test_unwritable_pipe_times_out_before_any_write(self):
        # 截止前但不可写也应超时，不能把两个拒绝条件错误地要求同时成立。
        with self.rpc() as (_,selector,write):
            selector.select.return_value=[]
            write.side_effect=AssertionError('不可写管道不应调用 os.write')
            with self.assertRaises(TimeoutError):models.codex_cli_models(timeout=0.5)
            write.assert_not_called()

    def test_subsecond_budget_is_usable_until_deadline(self):
        # 剩余半秒仍属于有效预算；不能提前一秒报超时。
        with self.rpc():self.assertEqual(models.codex_cli_models(timeout=0.5),[])

    def test_successful_protocol_still_surfaces_cleanup_failure(self):
        # 协议成功不能掩盖 wait 的失败；确认回收失败必须让调用者知晓。
        with self.rpc() as (process,_,__):
            process.wait.side_effect=OSError('synthetic wait failure')
            with self.assertRaisesRegex(OSError,'synthetic wait failure'):
                models.codex_cli_models()
        process.stdin.close.assert_called_once();process.stdout.close.assert_called_once()

    def test_version_query_does_not_accept_reply_after_ten_seconds(self):
        # 用虚拟耗时模拟迟到响应，避免测试真的等待十秒或调用 CLI。
        def late_reply(argv, **kwargs):
            if kwargs['timeout'] < 10.5:
                raise models.subprocess.TimeoutExpired(argv,kwargs['timeout'])
            return models.subprocess.CompletedProcess(argv,0,'codex 9.9\n','')
        with mock.patch.object(models.subprocess,'run',side_effect=late_reply):
            self.assertEqual(models.cli_version('codex'),'unknown')

if __name__=='__main__':unittest.main()
