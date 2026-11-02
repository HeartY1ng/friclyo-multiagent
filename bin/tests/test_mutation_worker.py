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
from frclyo import worker
class MutationWorkerTests(unittest.TestCase):
    def test_relocated_fallback_file_is_sensitive(self):
        # 搬到默认目录外的回退凭据路径仍必须进入禁读清单；不读取文件内容。
        with mock.patch.object(worker.config, 'config_file', return_value=Path('/fixture/settings')), \
             mock.patch.object(worker.config, 'codex_home', return_value=Path('/fixture/codex')):
            paths = worker.sensitive_paths({'key_env_file':'/fixture/relocated-fallback'})
        self.assertEqual(set(paths), {'/fixture/settings', '/fixture/codex', '/fixture/relocated-fallback'})
