"""变异回归：仅使用临时文件和离线替身，不接触宿主账号。"""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import importlib.util
from frclyo import config
class MutationBoardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        path = Path(__file__).resolve().parents[2] / 'board/board.py'
        spec = importlib.util.spec_from_file_location('mutation_board', path)
        self.board = importlib.util.module_from_spec(spec)
        # 模块导入只获得合成路径，禁用后台功能，也不读取宿主配置。
        with mock.patch.object(config, 'load', return_value={'board_port':18799}), \
             mock.patch.object(config, 'path_of', side_effect=lambda name:str(self.root/name)), \
             mock.patch.dict(os.environ, BOARD_NO_BACKGROUND='1', BOARD_BALANCE='0'):
            spec.loader.exec_module(self.board)
        self.board.DELIVER = str(self.root / 'delivery')
    def test_each_invalid_identifier_stops_before_file_read(self):
        # 任务号或轮次任意一个非法就拒绝，不能等两者都非法才拦截。
        with mock.patch.object(self.board, 'safe_read', return_value=b'fixture') as read:
            for task, rnd in (('bad/task','r1'), ('task','bad/round')):
                with self.subTest(task=task, rnd=rnd):
                    self.assertEqual(self.board.read_round_file(task, rnd, 'result.txt'), (None, '编号不合法'))
            read.assert_not_called()
    def test_round_file_size_exact_limit_and_one_byte_over(self):
        # 抽屉读取在 300000 字节边界接受，超一字节拒绝。
        run = Path(self.board.DELIVER) / 'task/r1'; run.mkdir(parents=True)
        path = run / 'result.txt'; path.write_bytes(b'.' * 300000)
        value, error = self.board.read_round_file('task', 'r1', 'result.txt')
        self.assertIsNone(error); self.assertEqual(len(value), 300000)
        path.write_bytes(b'.' * 300001)
        value, error = self.board.read_round_file('task', 'r1', 'result.txt')
        self.assertIsNone(value); self.assertIsNotNone(error)
    def test_text_default_size_exact_limit_and_one_byte_over(self):
        # 通用文本读取默认上限也检查相邻边界。
        path = self.root / 'text'; path.write_bytes(b'.' * 300000)
        self.assertEqual(len(self.board.read_text(path)), 300000)
        path.write_bytes(b'.' * 300001)
        self.assertIsNone(self.board.read_text(path))
