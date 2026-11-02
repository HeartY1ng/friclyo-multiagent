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
from frclyo import models, execution
class MutationModelsTests(unittest.TestCase):
    def test_wrong_snapshot_version_with_valid_records_is_rejected(self):
        # 记录数组合法也不能接受未知版本，防止错误解读未来格式。
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'availability.json'
            path.write_text(json.dumps({'version': 2, 'records': []}))
            with self.assertRaises(ValueError): models._read(path)
    def test_capture_without_success_never_marks_model_available(self):
        # 没有显式成功证据时，空的宿主捕获不能把模型标为可用。
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(execution, 'capture_directory', return_value=Path(tmp)), \
             mock.patch.object(execution, 'write_json') as write, \
             mock.patch.object(models, 'record') as record:
            self.assertEqual(models.observe('codex', 'fixture', 'v1', tmp, captured=b''), 'unknown')
            record.assert_not_called()
            self.assertEqual(write.call_args.args[1]['status'], 'unknown')
