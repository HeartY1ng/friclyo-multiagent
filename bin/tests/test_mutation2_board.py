"""第二轮看板回归：仅在合成文件上验证缓存入口的安全条件。"""
import os
import unittest
from unittest import mock
from pathlib import Path
import test_mutation_board

class Mutation2BoardTests(unittest.TestCase):
    def setUp(self):
        self.fixture=test_mutation_board.MutationBoardTests('runTest');self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.board=self.fixture.board;self.root=self.fixture.root

    def test_signature_requires_both_regular_file_and_size_bound(self):
        # 缓存签名必须独立拒绝超限普通文件和限额内的 FIFO，不能只在两者同时坏时拒绝。
        path=self.root/'regular';path.write_bytes(b'12345678')
        signature=self.board.file_signature(str(path),8)
        self.assertEqual(signature[2],8)
        with self.assertRaises(ValueError):self.board.file_signature(str(path),7)
        fifo=self.root/'fifo';os.mkfifo(fifo)
        with self.assertRaises(ValueError):self.board.file_signature(str(fifo),8)

    def test_round_scan_excludes_hidden_and_linked_task_directories(self):
        # 隐藏目录或符号链接任一个条件成立，都不得进入轮次读取。
        deliver=Path(self.board.DELIVER);deliver.mkdir()
        (deliver/'visible/r1').mkdir(parents=True)
        (deliver/'.hidden/r1').mkdir(parents=True)
        (deliver/'linked').symlink_to(deliver/'visible',target_is_directory=True)
        def summary(task,rnd,directory,**kwargs):return {'任务':task,'轮次':rnd}
        with mock.patch.object(self.board,'probe_deliver',return_value=None), \
                mock.patch.object(self.board,'load_round',side_effect=summary) as load:
            rows,error=self.board.scan_rounds()
        self.assertIsNone(error)
        self.assertEqual(rows,[{'任务':'visible','轮次':'r1'}])
        self.assertEqual(load.call_count,1)

if __name__=='__main__':unittest.main()
