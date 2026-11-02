#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""宿主认证反向代理；固定 HTTPS 上游，stdin 收 key，不提供 CONNECT 或上游覆盖开关。"""
import argparse
import datetime
import http.client
import ipaddress
import json
import os
import re
import select
import signal
import socket
import socketserver
import ssl
import sys
import threading
import time
from pathlib import Path

# 路由集中定义在 worker，生产 CLI 只能选择已定义的一家，不能传 URL/认证格式。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from frclyo.worker import PROXY_ROUTES

MAX_HEADER = 16384
MAX_REQUEST = 16 * 1024 * 1024
MAX_RESPONSE = 64 * 1024 * 1024
HEADER_SECONDS = 10
REQUEST_SECONDS = 60
RESPONSE_SECONDS = 600
IDLE_SECONDS = 300  # 等上游第一个字节的上限：长上下文、先思考的模型（如 GLM）可能超过 30 秒才开口
MAX_KEY = 4095
TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
# 只发送模型调用所需头部；未知认证、Cookie、转发头和 hop-by-hop 均不透传。
REQUEST_HEADERS = frozenset(('content-type', 'accept', 'user-agent'))
RESPONSE_HEADERS = frozenset(('content-type', 'retry-after', 'x-request-id'))


def direct_connect(host, port):
    """DNS 只解析一次；所有结果须为公网 IP；不读取系统代理。"""
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise OSError('非公网解析')
    for family, kind, proto, _, endpoint in addresses:
        conn = socket.socket(family, kind, proto)
        try:
            conn.settimeout(10)
            conn.connect(endpoint)
            return conn
        except OSError:
            conn.close()
    raise OSError('上游连接失败')


class FinalResponse(http.client.HTTPResponse):
    def _read_status(self):
        # 在同一缓冲流里丢弃中间响应；不要重新 makefile 丢失预读的最终响应。
        # 101 协议切换不是可忽略的中间响应，本代理不支持 Upgrade。
        for _ in range(33):
            version, status, reason = super()._read_status()
            if status == 101:
                raise ValueError('upstream protocol switch unsupported')
            if not 100 <= status < 200:
                return version, status, reason
            http.client._read_headers(self.fp)
        raise ValueError('too many informational responses')


class HTTPSConnection(http.client.HTTPSConnection):
    response_class = FinalResponse

    def connect(self):
        # 默认信任库 + hostname 校验；只对检查过的 IP 拨号，SNI 用固定官方主机。
        raw = direct_connect(self.host, 443)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def open_upstream(host):
    return HTTPSConnection(host, port=443, timeout=IDLE_SECONDS,
                           context=ssl.create_default_context())


def read_key(stream):
    """有界管道协议：单行 ASCII key，必须以换行终止；不回显错误值。"""
    raw = stream.readline(MAX_KEY + 2)
    if not raw.endswith(b'\n') or not 1 <= len(raw) - 1 <= MAX_KEY:
        raise ValueError('凭据管道无效')
    value = raw[:-1].decode('ascii')
    if any(ord(c) <= 32 or ord(c) >= 127 for c in value):
        raise ValueError('凭据格式无效')
    return value


class Proxy(socketserver.ThreadingTCPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = False

    def __init__(self, provider, key, log_path):
        self.host, self.prefix, self.auth_header, self.auth_prefix = PROXY_ROUTES[provider]
        self.mount = "/" + provider
        self.key = key
        self.lock = threading.Lock()
        self.connections = set()
        self.slots = threading.BoundedSemaphore(64)
        self.denials = 0
        self.stopping = threading.Event()
        # 日志不含任何客户端/上游字符串，只记录固定事件编号和时间。
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        self.log = os.fdopen(fd, 'w', encoding='utf-8', buffering=1)
        try:
            super().__init__(('127.0.0.1', 0), Handler)
        except BaseException:
            self.log.close()
            raise

    def reject(self):
        with self.lock:
            if not self.stopping.is_set():
                # 磁盘有界；内存计数仍覆盖全部拒绝。
                if self.denials < 10000:
                    self.log.write(json.dumps({'时间': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                                               '原因': 'request_rejected'}, ensure_ascii=True) + '\n')
                self.denials += 1

    def handle_error(self, request, client_address):
        # socketserver 默认 traceback 可能泄露带凭据的异常；不打印异常对象。
        pass

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                self.reject()
                request.settimeout(1)
                request.sendall(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
            finally:
                self.shutdown_request(request)
            return
        with self.lock:
            self.connections.add(request)
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.release(request)
            raise

    def release(self, request):
        with self.lock:
            self.connections.discard(request)
        self.slots.release()

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.release(request)

    def close_all(self):
        self.stopping.set()
        self.server_close()
        with self.lock:
            for conn in self.connections:
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                conn.close()
            self.log.close()


class Redactor:
    """上游意外反射认证值时也不交给工人；仅暂留可能跨块的 key 前缀。"""
    def __init__(self, key):
        self.key, self.pending = key, b''

    def feed(self, block, final=False):
        data = (self.pending + block).replace(self.key, b'[REDACTED]')
        self.pending = b''
        if not final:
            for count in range(min(len(self.key) - 1, len(data)), 0, -1):
                if data.endswith(self.key[:count]):
                    self.pending = data[-count:]
                    return data[:-count]
        return data


class Handler(socketserver.BaseRequestHandler):
    def reply(self, status):
        self.request.sendall(('HTTP/1.1 ' + status +
            '\r\nContent-Length: 0\r\nConnection: close\r\n\r\n').encode('ascii'))

    def receive(self):
        data = b''
        deadline = time.monotonic() + HEADER_SECONDS
        while b'\r\n\r\n' not in data:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or len(data) >= MAX_HEADER:
                raise ValueError()
            self.request.settimeout(remaining)
            block = self.request.recv(min(4096, MAX_HEADER - len(data)))
            if not block:
                raise ValueError()
            data += block
        header, body = data.split(b'\r\n\r\n', 1)
        lines = header.decode('ascii').split('\r\n')
        parts = lines[0].split(' ')
        if len(parts) != 3 or parts[0] not in ('POST', 'GET') or parts[2] != 'HTTP/1.1':
            raise ValueError()
        method, path, _ = parts
        # 仅 origin-form；不接受编码路径、查询串、点段、重复斜线或反斜线。
        # 模型 SDK 的 chat/completions、models 不需要 URL 参数。
        if not re.fullmatch(r'/[A-Za-z0-9_./-]*', path) or '//' in path or any(
                part in ('.', '..') for part in path.split('/')):
            raise ValueError()
        prefix = self.server.mount + self.server.prefix
        if not path.startswith(prefix + '/'):
            raise ValueError()
        headers = {}
        for line in lines[1:]:
            if ':' not in line:
                raise ValueError()
            name, value = line.split(':', 1)
            name = name.lower()
            if not TOKEN.fullmatch(name) or name in headers or any(ord(c) < 32 or ord(c) == 127 for c in value):
                raise ValueError()
            headers[name] = value.strip()
        local = '127.0.0.1:' + str(self.server.server_address[1])
        if headers.get('host') != local or any(name in headers for name in ('transfer-encoding', 'expect', 'upgrade')):
            raise ValueError()
        length = headers.get('content-length', '0')
        if not re.fullmatch(r'[0-9]{1,10}', length) or int(length) > MAX_REQUEST:
            raise ValueError()
        length = int(length)
        if method == 'GET' and length:
            raise ValueError()
        deadline = time.monotonic() + REQUEST_SECONDS
        while len(body) < length:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError()
            self.request.settimeout(min(IDLE_SECONDS, remaining))
            block = self.request.recv(min(65536, length - len(body)))
            if not block:
                raise ValueError()
            body += block
        if len(body) != length:
            raise ValueError()
        # 每连接只处理一个请求；客户端认证头一律丢弃，再由宿主注入。
        forwarded = {name: value for name, value in headers.items() if name in REQUEST_HEADERS}
        forwarded.update({'Host': self.server.host, 'Content-Length': str(length),
                          'Connection': 'close', 'Accept-Encoding': 'identity',
                          self.server.auth_header: self.server.auth_prefix + self.server.key})
        return method, path[len(self.server.mount):], body, forwarded

    def handle(self):
        started = False
        upstream = None
        upstream_socket = None
        response = None
        timer = None
        watcher = None
        watch_done = threading.Event()
        try:
            try:
                method, path, body, headers = self.receive()
            except (ValueError, OSError):
                self.server.reject()
                self.reply('403 Forbidden')
                return
            # 总时限通过关闭两端 socket 实施，也涵盖慢头部、慢 SSE 和慢客户端。
            expired = threading.Event()
            def expire():
                expired.set()
                for conn in (self.request, upstream_socket):
                    if conn is not None:
                        try:
                            conn.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
            timer = threading.Timer(RESPONSE_SECONDS, expire)
            timer.daemon = True
            timer.start()
            upstream = open_upstream(self.server.host)
            upstream.connect()
            upstream_socket = upstream.sock
            with self.server.lock:
                self.server.connections.add(upstream_socket)
            upstream_socket.settimeout(IDLE_SECONDS)

            poller = select.poll()
            poller.register(self.request, select.POLLHUP | select.POLLERR | select.POLLNVAL)

            def watch_client():
                # macOS 连 SHUT_WR 也会报告 HUP；用不发送任何协议字节的
                # 写探测确认取消，不能误杀仍在接收响应的半关闭客户端。
                while not watch_done.is_set():
                    try:
                        events = poller.poll(50)
                    except OSError:
                        if not watch_done.is_set():
                            expire()
                        return
                    if not events:
                        continue
                    disconnected = any(mask & (select.POLLERR | select.POLLNVAL) for _, mask in events)
                    if not disconnected:
                        try:
                            self.request.send(b'', socket.MSG_DONTWAIT)
                        except (BlockingIOError, InterruptedError):
                            pass
                        except OSError:
                            disconnected = True
                    if disconnected:
                        if not watch_done.is_set():
                            expire()
                        return
                    watch_done.wait(0.05)

            watcher = threading.Thread(target=watch_client, daemon=True)
            watcher.start()
            upstream.request(method, path, body=body, headers=headers)
            response = upstream.getresponse()
            # 不返回 Location，也不跟随同主机重定向；避免 SDK 自行跟随重定向。
            if 300 <= response.status < 400:
                self.server.reject()
                self.reply('403 Forbidden')
                return
            if not 200 <= response.status <= 599:
                raise ValueError()
            if response.getheader('Content-Encoding') not in (None, 'identity'):
                raise ValueError()
            length = response.getheader('Content-Length')
            if length is not None and (not length.isdigit() or int(length) > MAX_RESPONSE):
                raise ValueError()
            output = ['HTTP/1.1 %d Upstream' % response.status, 'Connection: close', 'Transfer-Encoding: chunked']
            for name, value in response.getheaders():
                if name.lower() in RESPONSE_HEADERS and self.server.key not in value and not any(
                        ord(c) < 32 or ord(c) == 127 for c in value):
                    output.append(name + ': ' + value)
            self.request.settimeout(IDLE_SECONDS)
            total = 0
            redactor = Redactor(self.server.key.encode('ascii'))
            while not self.server.stopping.is_set():
                # read1 不等待填满 64 KiB；每个已到达的 SSE 数据块立即转发。
                block = response.read1(65536)
                if expired.is_set():
                    raise TimeoutError()
                if not block and length is not None and total < int(length):
                    raise ValueError('upstream response truncated')
                if not started:
                    # 先读首块；首字节前断开可明确返回 502。
                    started = True
                    self.request.sendall(('\r\n'.join(output) + '\r\n\r\n').encode('latin-1'))
                if not block:
                    tail = redactor.feed(b'', final=True)
                    if tail:
                        self.request.sendall(('%x\r\n' % len(tail)).encode('ascii') + tail + b'\r\n')
                    self.request.sendall(b'0\r\n\r\n')
                    return
                total += len(block)
                if total > MAX_RESPONSE:
                    raise ValueError()
                block = redactor.feed(block)
                if block:
                    self.request.sendall(('%x\r\n' % len(block)).encode('ascii') + block + b'\r\n')
        except Exception:
            # 不打印来自 HTTP/TLS/SDK 的异常字符串或请求、响应内容。
            if started:
                try:
                    self.request.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            else:
                try:
                    self.reply('502 Bad Gateway')
                except OSError:
                    pass
        finally:
            watch_done.set()
            if watcher is not None:
                watcher.join()
            if timer is not None:
                timer.cancel()
            if response is not None:
                response.close()
            if upstream is not None:
                upstream.close()
            if upstream_socket is not None:
                with self.server.lock:
                    self.server.connections.discard(upstream_socket)


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--provider', choices=tuple(PROXY_ROUTES), required=True)
    parser.add_argument('--log', required=True)
    parser.add_argument('--parent-pid', type=int, default=os.getppid())
    args = parser.parse_args()
    try:
        if args.parent_pid <= 1 or os.getppid() != args.parent_pid:
            raise ValueError()
        # 管道只能等十秒；启动失败不输出端口，不启动工人。
        import io
        raw = b''
        deadline = time.monotonic() + HEADER_SECONDS
        while not raw.endswith(b'\n'):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or len(raw) > MAX_KEY or not select.select([0], [], [], remaining)[0]:
                raise ValueError()
            block = os.read(0, MAX_KEY + 2 - len(raw))
            if not block:
                raise ValueError()
            raw += block
        key = read_key(io.BytesIO(raw))
        del raw
        sys.stdin.close()
        proxy = Proxy(args.provider, key, args.log)
        del key
    except Exception:
        print('代理启动失败：检查凭据管道、提供方和私有日志目录', file=sys.stderr)
        return 2
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *_: proxy.stopping.set())
    proxy.timeout = 0.2
    print(proxy.server_address[1], flush=True)
    try:
        while not proxy.stopping.is_set() and os.getppid() == args.parent_pid:
            proxy.handle_request()
    finally:
        proxy.close_all()
        print(json.dumps({'拒绝次数': proxy.denials}, ensure_ascii=False), flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
