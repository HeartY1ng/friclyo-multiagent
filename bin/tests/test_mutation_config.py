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
from frclyo import config
class MutationConfigTests(unittest.TestCase):
    def test_path_rejects_each_invalid_condition(self):
        # 空字符串和控制字符各自足以拒绝，不能把所有错误条件连成 and。
        for value in ('', 'bad\npath', 42):
            with self.subTest(value=value), self.assertRaises(ValueError): config._path(value, 'fixture')
    def test_legacy_fields_warn_without_revealing_values(self):
        # 旧字段默认给迁移提醒，但警告不回显字段中的内容。
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as tmp, mock.patch.object(config, 'root_dir', return_value=Path(tmp)):
            with self.assertWarnsRegex(UserWarning, 'work_dir') as warning:
                result = config._normalize({'work_dir':'private-fixture'})
            self.assertNotIn('private-fixture', str(warning.warning))
            self.assertEqual(result['work_dir'], tmp)
