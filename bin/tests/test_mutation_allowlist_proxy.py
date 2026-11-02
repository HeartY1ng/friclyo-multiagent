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
import threading
spec = importlib.util.spec_from_file_location('mutation_proxy', Path(__file__).resolve().parents[1] / 'allowlist-proxy.py')
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)
class MutationProxyTests(unittest.TestCase):
    def test_invalid_request_updates_denial_counter_and_fixed_log(self):
        # 非法请求除了返回 403，还必须留下拒绝计数及固定日志；不建立监听或上游连接。
        server = proxy.Proxy.__new__(proxy.Proxy)
        server.lock = threading.Lock(); server.stopping = threading.Event()
        server.denials = 0; server.log = io.StringIO()
        handler = proxy.Handler.__new__(proxy.Handler); handler.server = server
        handler.receive = mock.Mock(side_effect=ValueError('untrusted-request'))
        handler.reply = mock.Mock()
        with mock.patch.object(proxy, 'open_upstream') as dial:
            handler.handle()
        handler.reply.assert_called_once_with('403 Forbidden')
        dial.assert_not_called()
        self.assertEqual(server.denials, 1)
        log = server.log.getvalue()
        self.assertEqual(json.loads(log)['原因'], 'request_rejected')
        self.assertNotIn('untrusted-request', log)
