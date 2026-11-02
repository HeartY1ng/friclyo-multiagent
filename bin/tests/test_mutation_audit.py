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
from frclyo import audit
class MutationAuditTests(unittest.TestCase):
    def test_failed_audit_has_zero_verified_cards(self):
        # 配置或证据读取失败，不能把尚未核对的卡计为已核对。
        with mock.patch.object(audit, 'Paths', side_effect=ValueError('fixture')):
            report = audit.check()
        self.assertEqual(report['核对卡数'], 0)
        self.assertTrue(report['问题'])
