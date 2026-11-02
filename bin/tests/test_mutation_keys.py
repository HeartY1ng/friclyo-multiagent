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
from frclyo import keys
class MutationKeysTests(unittest.TestCase):
    def test_key_read_has_ten_second_timeout(self):
        # 凭据命令必须有十秒等待上限；只模拟命令，绝不调用真实钥匙串。
        cfg = {'providers': {'fixture': {}}, 'keychain_entries': {}}
        with mock.patch.object(keys.config, 'load', return_value=cfg), mock.patch.object(keys.subprocess, 'run', return_value=mock.Mock(returncode=0, stdout='fake-value\n')) as run:
            self.assertEqual(keys.get_key('fixture'), 'fake-value')
        self.assertEqual(run.call_args.kwargs['timeout'], 10)
    def test_presence_check_has_ten_second_timeout_and_no_password_flag(self):
        # 来源探测同样受超时约束，且不能要求读出密码。
        with mock.patch.object(keys.subprocess, 'run', return_value=mock.Mock(returncode=0)) as run:
            self.assertTrue(keys._keychain_exists({'service':'fixture', 'account':'fixture'}))
        self.assertEqual(run.call_args.kwargs['timeout'], 10)
        self.assertNotIn('-w', run.call_args.args[0])
