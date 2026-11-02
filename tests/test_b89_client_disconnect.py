"""期望：客户端已完整断开时，慢上游请求应及时取消并释放处理线程。"""
import socket
import threading
import time
import unittest
from unittest import mock
from proxy_regression_support import proxy_fixture, REQUEST


class ClientDisconnect(unittest.TestCase):
    def test_disconnect_cancels_wait_for_upstream_headers(self):
        self.exercise()

    def test_disconnect_cancels_wait_during_response_body(self):
        self.exercise(body_started=True)

    def test_half_close_keeps_waiting_for_final_response(self):
        self.exercise(half_close=True)

    def exercise(self, body_started=False, half_close=False):
        module, server = proxy_fixture()
        client, downstream = socket.socketpair()
        upstream_socket, peer = socket.socketpair()
        peer.settimeout(2)
        upstream = module.HTTPSConnection('fixture.invalid')
        upstream.sock = upstream_socket
        upstream.connect = lambda: None

        def serve():
            with downstream:
                module.Handler(downstream, ('local', 0), server)

        thread = threading.Thread(target=serve)
        try:
            with mock.patch.object(module, 'open_upstream', return_value=upstream), \
                 mock.patch.object(module, 'IDLE_SECONDS', 2), \
                 mock.patch.object(module, 'RESPONSE_SECONDS', 4):
                thread.start()
                client.sendall(REQUEST)
                request = b''
                while not request.endswith(b'\r\n\r\n{}'):
                    block = peer.recv(4096)
                    if not block:
                        self.fail('上游连接在接收请求之前关闭')
                    request += block
                if body_started:
                    peer.sendall(b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n')
                    client.settimeout(2)
                    data = b''
                    while b'hello' not in data:
                        block = client.recv(4096)
                        self.assertTrue(block)
                        data += block
                if half_close:
                    client.shutdown(socket.SHUT_WR)
                    thread.join(0.2)
                    self.assertTrue(thread.is_alive(), '半关闭仍需等待响应')
                    peer.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}')
                    client.settimeout(2)
                    data = b''
                    while True:
                        block = client.recv(4096)
                        if not block:
                            break
                        data += block
                    thread.join(1)
                    self.assertIn(b'200 Upstream', data)
                    self.assertTrue(data.endswith(b'0\r\n\r\n'))
                    self.assertFalse(server.connections)
                    return
                # 上游已收到整个请求，客户端取消；上游保持安静模拟模型思考。
                client.shutdown(socket.SHUT_RDWR)
                client.close()
                started = time.monotonic()
                thread.join(0.75)
                still_waiting = thread.is_alive()
                peer.settimeout(0.05)
                try:
                    cancelled = peer.recv(1) == b''
                except socket.timeout:
                    cancelled = False
                print('after %.2fs: handler_alive=%s, upstream_cancelled=%s' %
                      (time.monotonic() - started, still_waiting, cancelled))
                self.assertFalse(still_waiting, '客户端关闭后仍占用处理线程；生产空闲时限为 300 秒')
                self.assertTrue(cancelled)
                self.assertFalse(server.connections, '上游连接必须移出存活集合')
        finally:
            peer.close()
            client.close()
            thread.join(3)
            upstream.close()
            self.assertFalse(thread.is_alive(), '夹具清理失败')


if __name__ == '__main__':
    unittest.main(verbosity=2)
