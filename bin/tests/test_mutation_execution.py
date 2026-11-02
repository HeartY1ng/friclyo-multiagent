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
from frclyo import execution
class MutationExecutionTests(unittest.TestCase):
    def test_copy_rejects_each_zero_limit_before_writing(self):
        # 空素材也不能绕过任意一项零上限；校验必须发生在发布目录之前。
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as tmp:
            source = Path(tmp) / 'source'; source.mkdir()
            for count, (size, files) in enumerate(((0, 1), (1, 0))):
                destination = Path(tmp) / ('output' + str(count))
                with self.subTest(size=size, files=files), self.assertRaises(ValueError):
                    execution.copy_inputs(source, destination, max_bytes=size, max_files=files)
                self.assertFalse(destination.exists())
    def test_read_only_directory_does_not_create_missing_path(self):
        # 只读打开目录不能隐式创建路径，缺失应原样报错且不落盘。
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as tmp:
            missing = Path(tmp) / 'missing' / 'child'
            with self.assertRaises(FileNotFoundError):
                with execution.directory(missing): pass
            self.assertFalse(missing.parent.exists())
