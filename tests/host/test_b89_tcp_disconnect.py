"""B89 宿主 TCP RST 回归；无 TCP 监听权限时明确跳过。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import socket
import struct
import threading
import time
import unittest
from unittest import mock
from proxy_regression_support import proxy_fixture, REQUEST


class ClientDisconnect(unittest.TestCase):
    def test_disconnect_cancels_wait_for_upstream_headers(self):
        module, server = proxy_fixture()
        # 真正的 127.0.0.1 TCP，下游使用 RST 明确表示取消，避免半关闭的歧义。
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client = None
        try:
            listener.bind(('127.0.0.1', 0))
            listener.listen(1)
            listener.settimeout(2)
            client = socket.create_connection(listener.getsockname(), timeout=2)
            downstream, _ = listener.accept()
        except PermissionError as error:
            if client is not None:
                client.close()
            self.skipTest('本环境拒绝 TCP 回环：' + str(error))
        finally:
            listener.close()
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
                # 上游已收到整个请求，客户端取消；上游保持安静模拟模型思考。
                client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
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
        finally:
            peer.close()
            client.close()
            thread.join(3)
            upstream.close()
            self.assertFalse(thread.is_alive(), '夹具清理失败')


if __name__ == '__main__':
    unittest.main(verbosity=2)
