"""B87～B90 的离线夹具；默认使用本文件所在仓库，临时路径先 resolve。"""
import contextlib
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import types
from unittest import mock


REPO = Path(os.environ.get('REVIEW_REPO', Path(__file__).resolve().parents[1])).resolve()
PRIVATE_TMP = Path(tempfile.gettempdir()).resolve()
sys.path.insert(0, str(REPO / 'bin'))


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@contextlib.contextmanager
def isolated_runtime():
    # 其他集成测试会把 frclyo 缓存在已删除的仓库副本中；本夹具独立加载，
    # 结束后恢复模块表，既能单独运行，也能加入 tests/run_all.py。
    previous_path = list(sys.path)
    previous = {name: module for name, module in sys.modules.items()
                if name == 'frclyo' or name.startswith('frclyo.')}
    for name in previous:
        del sys.modules[name]
    sys.path.insert(0, str(REPO / 'bin'))
    try:
        yield
    finally:
        for name in list(sys.modules):
            if name == 'frclyo' or name.startswith('frclyo.'):
                del sys.modules[name]
        sys.modules.update(previous)
        sys.path[:] = previous_path


@contextlib.contextmanager
def aw_fixture():
    with isolated_runtime(), _aw_fixture() as fixture:
        yield fixture


@contextlib.contextmanager
def _aw_fixture():
    from frclyo import config
    with tempfile.TemporaryDirectory(prefix='aw-repro-', dir=PRIVATE_TMP) as temporary:
        base = Path(temporary).resolve()
        root = base / 'data'
        root.mkdir()
        cfg = base / 'config.json'
        cfg.write_text('{"providers": {}}')
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(config, 'root_dir', return_value=root))
            stack.enter_context(mock.patch.object(config, 'config_file', return_value=cfg))
            stack.enter_context(mock.patch.object(config, 'codex_home', return_value=base / 'synthetic-codex'))
            aw = load('review_aw', REPO / 'bin/aw')
            from frclyo import ledger
            lock = threading.RLock()

            def local_ledger(*args):
                # 运行真实账本实现，配置路径仅指向本夹具；不启动宿主 CLI。
                with lock, contextlib.redirect_stdout(io.StringIO()) as output:
                    rc = ledger.main(list(args))
                if rc:
                    raise aw.UserError('合成账本调用失败')
                return output.getvalue().strip()

            stack.enter_context(mock.patch.object(aw, 'ledger', side_effect=local_ledger))
            stack.enter_context(mock.patch.object(aw, 'port_open', return_value=False))
            stack.enter_context(mock.patch.object(aw, 'tools_report', return_value={'codex': 'fixture'}))
            yield aw, root, cfg, stack


CHOICE = dict(worker='codex', model='fixture-model', level='medium', cli_version='fixture')


def completed_worker(job, *args):
    directory = job['directory']
    directory.mkdir(parents=True)
    result = b'fixture worker completed\n'
    (directory / '结果.txt').write_bytes(result)
    receipt = dict(任务编号=job['task'], 轮次编号=job['round'], 工人=job['choice']['worker'],
                   模型=job['choice']['model'], 状态='执行完成', 退出码=0,
                   结果sha256=hashlib.sha256(result).hexdigest())
    (directory / '回执.json').write_text(json.dumps(receipt, ensure_ascii=False))
    job.update(rc=0, seconds=0.001)
    return job


def proxy_fixture():
    with isolated_runtime():
        module = load('review_proxy', REPO / 'bin/allowlist-proxy.py')
    server = types.SimpleNamespace(host='fixture.invalid', mount='/deepseek', prefix='',
        auth_header='Authorization', auth_prefix='Bearer ',
        key='SYNTHETIC-NOT-A-REAL-KEY', server_address=('127.0.0.1', 43210),
        lock=threading.Lock(), connections=set(), stopping=threading.Event(), reject=mock.Mock())
    return module, server


REQUEST = (b'POST /deepseek/chat/completions HTTP/1.1\r\n'
           b'Host: 127.0.0.1:43210\r\nContent-Length: 2\r\n\r\n{}')


def receive_all(sock):
    output = bytearray()
    while True:
        block = sock.recv(65536)
        if not block:
            return bytes(output)
        output.extend(block)


def proxy_exchange(raw_response, request=REQUEST, allow_early_reject=False):
    """真实 HTTPConnection/HTTPResponse + 本机 socketpair；不进行 DNS 或公网拨号。"""
    module, server = proxy_fixture()
    stream = io.BytesIO(raw_response)
    fake_socket = mock.Mock()
    fake_socket.makefile.side_effect = lambda *args: stream
    upstream = module.HTTPSConnection('fixture.invalid')
    upstream.sock = fake_socket
    upstream.connect = lambda: None
    client, downstream = socket.socketpair()
    client.settimeout(2)
    failures = []

    def serve():
        try:
            with downstream:
                module.Handler(downstream, ('local', 0), server)
        except BaseException as error:
            failures.append(error)

    thread = threading.Thread(target=serve)
    try:
        with mock.patch.object(module, 'open_upstream', return_value=upstream):
            thread.start()
            try:
                client.sendall(request)
            except BrokenPipeError:
                if not allow_early_reject:
                    raise
            try:
                client.shutdown(socket.SHUT_WR)
            except OSError:
                pass  # 服务端可能已拒绝请求并关闭；仍读取实际响应。
            output = receive_all(client)
            thread.join(2)
        if thread.is_alive() or failures:
            raise AssertionError('HTTP 夹具线程未正常结束：' + repr(failures))
        return output
    finally:
        client.close()
        upstream.close()
        stream.close()
        thread.join(2)
