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
from frclyo import isolation
class MutationIsolationTests(unittest.TestCase):
    def test_rejects_wrong_round_depth_without_symlink(self):
        # 真实目录仍需符合任务/轮次两层布局，不能只在多个异常同时出现时拒绝。
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            good = root / '交付/task/r1'; good.mkdir(parents=True)
            isolation.validate_run(root, good)
            for path in (good.parent, good / 'extra'):
                path.mkdir(exist_ok=True)
                with self.subTest(path=path), self.assertRaises(ValueError): isolation.validate_run(root, path)
