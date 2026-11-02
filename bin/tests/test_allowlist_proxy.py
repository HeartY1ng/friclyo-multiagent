#!/usr/bin/env python3
"""生产 HTTP 处理器的 socketpair 测试；不监听端口、不发 DNS/HTTPS。"""
import importlib.util
import errno
import http.client
import io
import json
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "allowlist-proxy.py"
spec = importlib.util.spec_from_file_location("allowlist_proxy", SCRIPT)
proxy_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy_module)
FAKE_KEY = "FAKE-PROXY-KEY-NOT-A-CREDENTIAL"


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "denials.log"
        with mock.patch.object(proxy_module.Proxy, "server_bind"), mock.patch.object(proxy_module.Proxy, "server_activate"):
            self.proxy = proxy_module.Proxy("deepseek", FAKE_KEY, self.log)
        self.proxy.server_address = ('127.0.0.1', 43210)
        self.threads = []
        self.response = mock.Mock(status=200)
        self.response.getheader.return_value = None
        self.response.getheaders.return_value = [('Content-Type', 'text/event-stream')]
        self.response.read1.side_effect = [b'data: first\n\n', b'data: done\n\n', b'']
        self.upstream = mock.Mock()
        self.upstream.getresponse.return_value = self.response
        patch = mock.patch.object(proxy_module, 'open_upstream', return_value=self.upstream)
        self.dial = patch.start()
        self.addCleanup(patch.stop)

    def tearDown(self):
        self.proxy.close_all()
        for thread in self.threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.tmp.cleanup()

    def connection(self):
        client, server = socket.socketpair()
        client.settimeout(3)
        def serve():
            with server:
                self.proxy.finish_request(server, ('local', 0))
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        self.threads.append(thread)
        return client

    def raw(self, path='/deepseek/chat/completions', method='POST', headers=b'', body=b'{}'):
        return (method + ' ' + path + ' HTTP/1.1\r\nHost: 127.0.0.1:43210\r\nContent-Length: ' + str(len(body)) + '\r\n').encode() + headers + b'\r\n' + body

    def request(self, raw):
        with self.connection() as client:
            client.sendall(raw)
            try:
                client.shutdown(socket.SHUT_WR)
            except OSError as exc:
                # 非法请求可能已被服务端拒绝并关闭；仍读取并断言实际 HTTP 响应。
                if exc.errno != errno.ENOTCONN:
                    raise
            result = b''
            deadline = time.monotonic() + 5
            while True:
                self.assertLess(time.monotonic(), deadline, '响应未在测试时限内结束')
                block = client.recv(65536)
                if not block:
                    return result
                result += block

    def test_host_auth_replaces_all_client_authentication(self):
        reply = self.request(self.raw(headers=b'Authorization: Bearer attacker\r\nx-api-key: attacker\r\napi-key: attacker\r\nCookie: attacker\r\nProxy-Authorization: attacker\r\nX-Custom-Token: attacker\r\nConnection: Authorization\r\n'))
        self.assertIn(b'200 Upstream', reply)
        args, kwargs = self.upstream.request.call_args
        self.assertEqual(args, ('POST', '/chat/completions'))
        headers = kwargs['headers']
        self.assertEqual(headers['Authorization'], 'Bearer ' + FAKE_KEY)
        self.assertEqual(headers['Host'], 'api.deepseek.com')
        self.assertNotIn('attacker', repr(headers))
        self.dial.assert_called_once_with('api.deepseek.com')

    def test_other_hosts_paths_and_tunnels_never_dial(self):
        for path in ('http://example.com/chat/completions', 'https://api.deepseek.com/chat/completions',
                     '//evil/path', '/moonshotai-cn/v1/chat/completions', '/chat/completions',
                     '/deepseekish/chat/completions', '/deepseek/../evil', '/deepseek/%2e%2e/evil',
                     '/deepseek/a?url=https://evil', '/deepseek/a\\b'):
            with self.subTest(path=path):
                self.assertIn(b'403 Forbidden', self.request(self.raw(path)))
        self.assertIn(b'403 Forbidden', self.request(self.raw('api.deepseek.com:443', method='CONNECT')))
        self.assertIn(b'403 Forbidden', self.request(self.raw().replace(b'127.0.0.1:43210', b'api.deepseek.com')))
        self.dial.assert_not_called()

    def test_ambiguous_or_oversize_requests_never_dial(self):
        for headers in (b'Content-Length: 2\r\n', b'Transfer-Encoding: chunked\r\n',
                        b'Expect: 100-continue\r\n', b'Host: evil\r\n', b' Broken: value\r\n',
                        b'X-Test: bad\x00value\r\n', b'Broken\r\n'):
            self.assertIn(b'403 Forbidden', self.request(self.raw(headers=headers)))
        with mock.patch.object(proxy_module, 'MAX_REQUEST', 1):
            self.assertIn(b'403 Forbidden', self.request(self.raw()))
        self.assertIn(b'403 Forbidden', self.request(b'X' * proxy_module.MAX_HEADER))
        self.dial.assert_not_called()

    def test_cross_host_and_same_host_redirects_are_rejected(self):
        for status, target in ((302, 'https://evil/'), (307, 'https://api.deepseek.com/other'), (308, '/deepseek/other')):
            self.response.status = status
            self.response.getheaders.return_value = [('Location', target)]
            reply = self.request(self.raw())
            self.assertIn(b'403 Forbidden', reply)
            self.assertNotIn(target.encode(), reply)
        self.assertEqual(self.dial.call_count, 3)
        self.response.read1.assert_not_called()

    def test_sse_first_chunk_arrives_before_second_is_available(self):
        # 使用真正 HTTPResponse.read1 解 chunked；上游第二块等客户端确认收到第一块。
        upstream, writer = socket.socketpair()
        self.upstream.sock = upstream
        response = http.client.HTTPResponse(upstream)
        gate = threading.Event()
        def produce():
            with writer:
                writer.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n')
                writer.sendall(b'd\r\ndata: first\n\n\r\n')
                if gate.wait(3):
                    writer.sendall(b'c\r\ndata: done\n\n\r\n0\r\n\r\n')
        thread = threading.Thread(target=produce, daemon=True)
        self.threads.append(thread)
        thread.start()
        def getresponse():
            response.begin()
            return response
        self.upstream.getresponse.side_effect = getresponse
        try:
            with self.connection() as client:
                client.sendall(self.raw())
                data = b''
                while b'data: first\n\n' not in data:
                    data += client.recv(4096)
                self.assertNotIn(b'data: done', data)
                gate.set()
                while True:
                    block = client.recv(4096)
                    if not block:
                        break
                    data += block
                self.assertIn(b'data: done', data)
        finally:
            gate.set()
            response.close()
            upstream.close()

    def test_errors_denials_and_reflected_key_do_not_expose_key(self):
        self.request(self.raw('/' + FAKE_KEY))
        self.dial.side_effect = OSError(FAKE_KEY)
        with mock.patch.object(sys, 'stderr', new_callable=io.StringIO) as err:
            reply = self.request(self.raw())
        self.assertIn(b'502 Bad Gateway', reply)
        self.assertNotIn(FAKE_KEY, err.getvalue() + reply.decode() + self.log.read_text())
        self.dial.side_effect = None
        raw = FAKE_KEY.encode()
        self.response.read1.side_effect = [b'data: ' + raw[:7], raw[7:] + b'\n\n', b'']
        self.response.getheaders.return_value = [('x-request-id', FAKE_KEY)]
        reply = self.request(self.raw())
        self.assertNotIn(raw, reply)
        self.assertIn(b'[REDACTED]', reply)

    def test_response_size_and_total_time_are_bounded(self):
        self.response.getheader.side_effect = lambda name: '999999999' if name == 'Content-Length' else None
        self.assertIn(b'502 Bad Gateway', self.request(self.raw()))
        self.response.getheader.side_effect = None
        self.response.read1.side_effect = [b'a' * 6, b'b' * 6, b'']
        with mock.patch.object(proxy_module, 'MAX_RESPONSE', 10):
            reply = self.request(self.raw())
        self.assertIn(b'a' * 6, reply)
        self.assertNotIn(b'b' * 6, reply)
        self.assertFalse(reply.endswith(b'0\r\n\r\n'))
        # 慢 SSE 总时限通过关闭客户端体现，不能把截断当完整成功响应。
        expired = threading.Event()
        original_timer = threading.Timer
        def observed_timer(interval, callback):
            def fire():
                try:
                    callback()
                finally:
                    expired.set()
            return original_timer(interval, fire)
        def slow_read(size):
            expired.wait(2)
            return b'data: late\n\n'
        self.response.read1.side_effect = slow_read
        with mock.patch.object(proxy_module, 'RESPONSE_SECONDS', .05), \
                mock.patch.object(proxy_module.threading, 'Timer', side_effect=observed_timer):
            reply = self.request(self.raw())
        # 在处理器外断言，避免其异常处理吞掉夹具断言。
        self.assertTrue(expired.is_set(), 'response deadline callback did not run')
        self.assertNotIn(b'data: late', reply)

    def test_moonshot_prefix_is_preserved(self):
        self.proxy.mount = '/moonshotai-cn'
        self.proxy.prefix = '/v1'
        self.proxy.host = 'api.moonshot.cn'
        self.assertIn(b'403', self.request(self.raw('/moonshotai-cn/other')))
        self.assertIn(b'200', self.request(self.raw('/moonshotai-cn/v1/chat/completions')))
        self.assertEqual(self.upstream.request.call_args.args[1], '/v1/chat/completions')

    def test_xiaomi_fixed_host_prefix_and_bearer_replace_client_auth(self):
        self.proxy.close_all()
        with mock.patch.object(proxy_module.Proxy, 'server_bind'), mock.patch.object(proxy_module.Proxy, 'server_activate'):
            self.proxy = proxy_module.Proxy('xiaomi', FAKE_KEY, self.log)
        self.proxy.server_address = ('127.0.0.1', 43210)
        for path in ('/xiaomi/chat/completions', '/deepseek/chat/completions'):
            self.assertIn(b'403', self.request(self.raw(path)))
        self.dial.assert_not_called()
        reply = self.request(self.raw('/xiaomi/v1/chat/completions',
                                     headers=b'Authorization: Bearer attacker\r\nx-api-key: attacker\r\napi-key: attacker\r\n'))
        self.assertIn(b'200', reply)
        self.dial.assert_called_once_with('api.xiaomimimo.com')
        args, kwargs = self.upstream.request.call_args
        self.assertEqual(args, ('POST', '/v1/chat/completions'))
        self.assertEqual(kwargs['headers']['Host'], 'api.xiaomimimo.com')
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer ' + FAKE_KEY)
        self.assertNotIn('attacker', repr(kwargs['headers']))

    def test_zai_coding_plan_fixed_host_prefix_and_bearer_replace_client_auth(self):
        self.proxy.close_all()
        with mock.patch.object(proxy_module.Proxy, 'server_bind'), mock.patch.object(proxy_module.Proxy, 'server_activate'):
            self.proxy = proxy_module.Proxy('zai-coding-plan', FAKE_KEY, self.log)
        self.proxy.server_address = ('127.0.0.1', 43210)
        for path in ('/zai-coding-plan/chat/completions', '/deepseek/chat/completions',
                     '/zai-coding-plan/api/paas/v4/chat/completions',
                     '/zai-coding-plan/api/coding/paas/v40/chat/completions',
                     '/zai-coding-plan/api/coding/paas/v4/../other',
                     'https://api.z.ai/api/coding/paas/v4/chat/completions',
                     'https://open.bigmodel.cn/api/coding/paas/v4/chat/completions'):
            self.assertIn(b'403', self.request(self.raw(path)))
        self.dial.assert_not_called()
        reply = self.request(self.raw('/zai-coding-plan/api/coding/paas/v4/chat/completions',
                                     headers=b'Authorization: Bearer attacker\r\nx-api-key: attacker\r\napi-key: attacker\r\n'))
        self.assertIn(b'200', reply)
        self.dial.assert_called_once_with('api.z.ai')
        args, kwargs = self.upstream.request.call_args
        self.assertEqual(args, ('POST', '/api/coding/paas/v4/chat/completions'))
        self.assertEqual(kwargs['headers']['Host'], 'api.z.ai')
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer ' + FAKE_KEY)
        self.assertNotIn('attacker', repr(kwargs['headers']))

    def test_zhipuai_fixed_host_prefix_and_bearer_replace_client_auth(self):
        self.proxy.close_all()
        with mock.patch.object(proxy_module.Proxy, 'server_bind'), mock.patch.object(proxy_module.Proxy, 'server_activate'):
            self.proxy = proxy_module.Proxy('zhipuai', FAKE_KEY, self.log)
        self.proxy.server_address = ('127.0.0.1', 43210)
        for path in ('/zhipuai/chat/completions', '/deepseek/chat/completions',
                     '/zhipuai/api/coding/paas/v4/chat/completions',
                     '/zhipuai/api/paas/v40/chat/completions',
                     '/zhipuai/api/paas/v4/../other',
                     'https://open.bigmodel.cn/api/paas/v4/chat/completions',
                     'https://api.z.ai/api/paas/v4/chat/completions'):
            self.assertIn(b'403', self.request(self.raw(path)))
        self.dial.assert_not_called()
        reply = self.request(self.raw('/zhipuai/api/paas/v4/chat/completions',
                                     headers=b'Authorization: Bearer attacker\r\nx-api-key: attacker\r\napi-key: attacker\r\n'))
        self.assertIn(b'200', reply)
        self.dial.assert_called_once_with('open.bigmodel.cn')
        args, kwargs = self.upstream.request.call_args
        self.assertEqual(args, ('POST', '/api/paas/v4/chat/completions'))
        self.assertEqual(kwargs['headers']['Host'], 'open.bigmodel.cn')
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer ' + FAKE_KEY)
        self.assertNotIn('attacker', repr(kwargs['headers']))


class DialTests(unittest.TestCase):
    def test_b30_upstream_first_byte_timeout_is_five_minutes(self):
        # 直接检查生产拨号路径传给 HTTPSConnection 的时限，无真实网络或等待。
        with mock.patch.object(proxy_module, 'HTTPSConnection') as connection:
            proxy_module.open_upstream('api.deepseek.com')
        self.assertEqual(connection.call_args.kwargs['timeout'], 300)
        self.assertGreaterEqual(proxy_module.RESPONSE_SECONDS, 300)

    def test_private_dns_results_never_dial(self):
        for ip in ('127.0.0.1', '10.0.0.1', '169.254.1.1', '::1', '::ffff:127.0.0.1'):
            with self.subTest(ip=ip), mock.patch.object(proxy_module.socket, 'getaddrinfo', return_value=[
                    (socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 443))]), mock.patch.object(proxy_module.socket, 'socket') as dial:
                with self.assertRaises(OSError):
                    proxy_module.direct_connect('api.deepseek.com', 443)
                dial.assert_not_called()

    def test_tls_verifies_fixed_host_and_ignores_proxy_environment(self):
        with mock.patch.dict(os.environ, {'HTTPS_PROXY': 'http://evil'}), \
                mock.patch.object(proxy_module, 'direct_connect') as dial:
            conn = proxy_module.open_upstream('api.deepseek.com')
            self.assertTrue(conn._context.check_hostname)
            self.assertEqual(conn._context.verify_mode, proxy_module.ssl.CERT_REQUIRED)
            with mock.patch.object(conn._context, 'wrap_socket') as wrap:
                conn.connect()
                dial.assert_called_once_with('api.deepseek.com', 443)
                wrap.assert_called_once_with(dial.return_value, server_hostname='api.deepseek.com')

    def test_production_cli_has_no_upstream_or_key_override(self):
        for option in ('--upstream', '--base-url', '--allow', '--key', '--insecure', '--auth-header'):
            result = subprocess.run([sys.executable, '-I', str(SCRIPT), '--provider', 'deepseek',
                                     '--log', 'unused', option, 'http://evil'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn('unrecognized arguments', result.stderr)
        self.assertEqual(set(proxy_module.PROXY_ROUTES), {'deepseek', 'moonshotai-cn', 'xiaomi', 'zai-coding-plan', 'zhipuai'})

    def test_key_pipe_is_bounded_and_rejects_header_injection(self):
        self.assertEqual(proxy_module.read_key(io.BytesIO((FAKE_KEY + '\n').encode())), FAKE_KEY)
        for value in (b'', b'key', b'key\r\n', b'bad key\n', b'x' * 4096 + b'\n'):
            with self.assertRaises(ValueError):
                proxy_module.read_key(io.BytesIO(value))


# 直接提取交付 shell 中实际执行的 Python，避免测试副本正确、生产监督器仍旧出错。
WORKER_SOURCE = (Path(__file__).resolve().parents[1] / "opencode-worker.sh").read_text(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from frclyo import worker, execution



class SupervisorTests(unittest.TestCase):
    def supervise(self, mode="timeout", denied=(), persistent=False, survivor=False,
                  proxy_denied=False, ps_fails=False, late_term=False, normal_rc=0):
        # 虚拟时钟和进程只替代系统边界；执行完整生产监督器（含信号、升级、wait、退出码）。
        now = [0.0]
        parent, proxy_pid, pgid = 101, 102, 103
        state = {"rc": normal_rc if mode == "normal" else None, "reaped": False,
                 "alive": mode != "normal" or survivor, "kills": [], "polls": 0, "ps": 0}
        handlers = {}
        child = mock.Mock(pid=pgid)

        def poll():
            state["polls"] += 1
            if state["rc"] is not None:
                state["reaped"] = True
            return state["rc"]

        def wait(timeout):
            rc = poll()
            if rc is None:
                raise subprocess.TimeoutExpired("fake-worker", timeout)
            return rc

        child.poll.side_effect = poll
        child.wait.side_effect = wait
        rejected = set()

        def killpg(pid, sig):
            self.assertEqual(pid, pgid)
            state["kills"].append((sig, state["polls"]))
            if sig in denied and (persistent or sig not in rejected):
                rejected.add(sig)
                # 单次 EPERM 也可能是暂时权限拒绝，工人仍活着，必须继续发信号。
                raise PermissionError(errno.EPERM, "mock killpg")
            if not state["alive"]:
                if not state["reaped"]:
                    raise PermissionError(errno.EPERM, "zombie")
                raise ProcessLookupError(errno.ESRCH, "gone")
            if sig == signal.SIGKILL or (sig == signal.SIGTERM and not survivor):
                state["alive"] = False
                if state["rc"] is None:
                    state["rc"] = -sig
            if late_term and sig == signal.SIGTERM:
                handlers[signal.SIGTERM](signal.SIGTERM, None)

        def kill(pid, sig):
            self.assertEqual((pid, sig), (proxy_pid, 0))
            if proxy_denied:
                raise PermissionError(errno.EPERM, "mock proxy probe")

        def snapshot(*args, **kwargs):
            state["ps"] += 1
            if ps_fails:
                raise PermissionError(errno.EPERM, "mock ps")
            if args[0][-1] == 'pid=,ppid=,pgid=,stat=,lstart=':
                return types.SimpleNamespace(stdout='%s %s %s %s fixture start\n' % (
                    pgid, parent, pgid, 'S' if state['alive'] else 'Z'))
            return types.SimpleNamespace(stdout="%s %s\n999 S\n" % (pgid, "S" if state["alive"] else "Z"))

        def sleep(seconds):
            now[0] += seconds
            if mode == "term" and not state["kills"]:
                handlers[signal.SIGTERM](signal.SIGTERM, None)
            self.assertLess(now[0], 6, "清理不能无限等待")

        modules = {
            "os": types.SimpleNamespace(environ={}, getppid=lambda: 1 if mode == "orphan" and now[0] else parent,
                                        kill=kill, killpg=killpg),
            "signal": types.SimpleNamespace(SIGTERM=signal.SIGTERM, SIGINT=signal.SIGINT,
                                             SIGHUP=signal.SIGHUP, SIGKILL=signal.SIGKILL, SIGCONT=signal.SIGCONT,
                                             signal=lambda sig, fn: handlers.__setitem__(sig, fn)),
            "subprocess": types.SimpleNamespace(Popen=mock.Mock(return_value=child), run=snapshot,
                                                 SubprocessError=subprocess.SubprocessError,
                                                 TimeoutExpired=subprocess.TimeoutExpired),
            "time": types.SimpleNamespace(monotonic=lambda: now[0], sleep=sleep),
        }
        reason = [0]
        status = {'timed_out': False}
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            handlers[sig] = lambda sig, frame: reason[0] or reason.__setitem__(0, 128 + sig)
        with mock.patch.multiple(execution, **modules):
            execution.monitor_process(child, 1, parent, proxy_pid, 142, reason, sleep, status)
        child.wait.assert_called_once()
        rc = reason[0] or (125 if not status['group_reaped'] else
                          (128 - state['rc'] if state['rc'] < 0 else state['rc']))
        errors = '' if status['group_reaped'] else '无法确认进程组已退出'
        return rc, state, errors

    def test_timeout_eperm_each_group_operation_reaps_and_retries(self):
        for sig in (0, signal.SIGTERM, signal.SIGKILL):
            with self.subTest(sig=sig):
                rc, state, errors = self.supervise(denied=(sig,), survivor=True)
                self.assertEqual(rc, 142)
                self.assertFalse(state["alive"])
                self.assertTrue(state["reaped"])
                calls = [polls for signal_sent, polls in state["kills"] if signal_sent == sig]
                self.assertGreaterEqual(len(calls), 2)
                self.assertGreater(calls[1], calls[0], "EPERM 后必须先 poll 再重试")
                self.assertEqual(errors, "")

    def test_eperm_with_reaped_leader_still_kills_live_descendant(self):
        rc, state, errors = self.supervise(mode="normal", denied=(0,), persistent=True, survivor=True)
        self.assertEqual(rc, 0)
        self.assertGreater(state["ps"], 0)
        self.assertIn(signal.SIGKILL, [sig for sig, _ in state["kills"]])
        self.assertFalse(state["alive"])
        self.assertEqual(errors, "")

    def test_persistent_eperm_zombie_group_is_verified(self):
        rc, state, errors = self.supervise(mode="normal", denied=(0, signal.SIGTERM), persistent=True)
        self.assertEqual(rc, 0)
        self.assertGreater(state["ps"], 0)
        self.assertTrue(state["reaped"])
        self.assertEqual(errors, "")

    def test_persistent_eperm_live_group_reports_failure_and_preserves_timeout(self):
        for mode, expected in (("timeout", 142), ("normal", 125)):
            with self.subTest(mode=mode):
                rc, state, errors = self.supervise(mode=mode, denied=(0, signal.SIGTERM, signal.SIGKILL),
                                                   persistent=True, survivor=True)
                self.assertEqual(rc, expected)
                self.assertTrue(state["alive"])
                self.assertIn(signal.SIGKILL, [sig for sig, _ in state["kills"]])
                self.assertIn("无法确认进程组已退出", errors)

    def test_failed_state_query_never_counts_as_group_exit(self):
        rc, state, errors = self.supervise(mode="normal", denied=(0,), persistent=True, ps_fails=True)
        self.assertEqual(rc, 125)
        self.assertIn(signal.SIGKILL, [sig for sig, _ in state["kills"]])
        self.assertIn("无法确认进程组已退出", errors)

    def test_proxy_kill_eperm_stops_worker(self):
        rc, state, errors = self.supervise(proxy_denied=True)
        self.assertEqual(rc, 125)
        self.assertTrue(state["reaped"])
        self.assertFalse(state["alive"])
        self.assertEqual(errors, "")

    def test_normal_term_and_killed_shell_cleanup(self):
        for mode, expected in (("normal", 0), ("term", 143), ("orphan", 143)):
            with self.subTest(mode=mode):
                rc, state, errors = self.supervise(mode=mode, survivor=True)
                self.assertEqual(rc, expected)
                self.assertFalse(state["alive"])
                self.assertTrue(state["reaped"])
                self.assertEqual(errors, "")

    def test_term_during_timeout_cleanup_cannot_replace_142(self):
        rc, state, errors = self.supervise(late_term=True)
        self.assertEqual(rc, 142)
        self.assertFalse(state["alive"])
        self.assertEqual(errors, "")

    def test_worker_exit_code_and_signal_are_preserved(self):
        for worker_rc, expected in ((7, 7), (-signal.SIGKILL, 137)):
            with self.subTest(worker_rc=worker_rc):
                rc, state, errors = self.supervise(mode="normal", normal_rc=worker_rc)
                self.assertEqual(rc, expected)
                self.assertTrue(state["reaped"])
                self.assertEqual(errors, "")

    def test_timeout_eperm_receipt_keeps_status_and_network(self):
        rc, _, _ = self.supervise(denied=(0, signal.SIGTERM, signal.SIGKILL), survivor=True)
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp).resolve() / 'run'
            control = Path(tmp).resolve() / 'control'
            run.mkdir(); control.mkdir()
            (control / '代理.stdout').write_text('12345\n{"拒绝次数":0}\n')
            (control / 'events.jsonl').write_bytes(b'')
            (control / 'stderr.log').write_bytes(b'')
            execution.write_json(control / 'status.json', dict(rc=rc, timed_out=True, group_reaped=True, errors=[]))
            self.assertEqual(execution.finalize('opencode', run, control, 'TEST', 'timeout',
                             'deepseek/deepseek-flash', 'nothink', 'start', 'end', 'a' * 64,
                             target='api.deepseek.com:443'), 142)
            receipt = json.loads((run / '回执.json').read_text())
            self.assertEqual(receipt['退出码'], 142)
            self.assertEqual(receipt['状态'], '超时')
            self.assertEqual(receipt['网络'], {'放行': ['api.deepseek.com:443'], '拒绝次数': 0})



class TruncatedResponseTests(unittest.TestCase):
    def response(self, body, length):
        raw = (b'HTTP/1.1 200 OK\r\nContent-Length: ' + str(length).encode() +
               b'\r\nContent-Type: text/event-stream\r\n\r\n' + body)
        sock = mock.Mock()
        sock.makefile.return_value = io.BytesIO(raw)
        response = http.client.HTTPResponse(sock)
        response.begin()
        upstream = mock.Mock()
        upstream.getresponse.return_value = response
        client = mock.Mock()
        # 处理器还监测客户端断开；正文断言仍记录 sendall，但提供真实可轮询 fd。
        peer, descriptor = socket.socketpair()
        self.addCleanup(peer.close)
        self.addCleanup(descriptor.close)
        client.fileno.return_value = descriptor.fileno()
        server = types.SimpleNamespace(host='offline.invalid', key=FAKE_KEY,
            lock=threading.Lock(), connections=set(), stopping=threading.Event())
        handler = object.__new__(proxy_module.Handler)
        handler.server, handler.request = server, client
        with mock.patch.object(handler, 'receive', return_value=('POST', '/chat/completions', b'{}', {})), \
                mock.patch.object(proxy_module, 'open_upstream', return_value=upstream):
            handler.handle()
        return b''.join(call.args[0] for call in client.sendall.call_args_list), client

    def test_truncated_content_length_is_not_success(self):
        output, client = self.response(b'data: partial\n\n', 100)
        self.assertIn(b'200 Upstream', output)
        self.assertFalse(output.endswith(b'0\r\n\r\n'))
        client.shutdown.assert_called_once()

    def test_eof_before_headers_returns_502(self):
        output, client = self.response(b'', 100)
        self.assertIn(b'502 Bad Gateway', output)
        self.assertNotIn(b'200 Upstream', output)

    def test_exact_length_and_empty_response_finish(self):
        for body in (b'', b'data: done\n\n', FAKE_KEY.encode()):
            with self.subTest(size=len(body)):
                output, client = self.response(body, len(body))
                self.assertIn(b'200 Upstream', output)
                self.assertTrue(output.endswith(b'0\r\n\r\n'))
                self.assertNotIn(FAKE_KEY.encode(), output)
                client.shutdown.assert_not_called()


if __name__ == "__main__":
    unittest.main()
