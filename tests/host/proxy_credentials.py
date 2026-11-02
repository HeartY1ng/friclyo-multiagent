#!/usr/bin/env python3
"""宿主专用：真实入口 + sandbox-exec + 假 OpenCode；默认离线，无真实 key/模型费用。"""
import argparse
import ctypes
import errno
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from isolation_check import recitation_config

BIN = Path(__file__).resolve().parents[2] / 'bin'
FAKE_KEY = 'FAKE-HOST-PROXY-CREDENTIAL-20261014'

# 本段在真实沙箱中作为替身 OpenCode 运行；不替换 seatbelt、不替换生产代理。
PROBE = r'''
import ctypes, errno, http.client, json, os, pathlib, socket, subprocess, sys, time
def process_credentials(pid, expected):
    checks = {}
    denied = (errno.EPERM, errno.EACCES)
    try:
        ps = subprocess.run(['/bin/ps', '-E', '-ww', '-p', str(pid), '-o', 'command='],
                            capture_output=True, timeout=5)
    except OSError as exc:
        if exc.errno not in denied:
            raise
        checks.update(ps_no_key=True, ps_readable=False, ps_errno=exc.errno,
                      ps_status='无法查看（内核拒绝）')
    else:
        blocked = ps.returncode != 0 and (b'Operation not permitted' in ps.stderr or
                                         b'Permission denied' in ps.stderr)
        checks.update(ps_no_key=expected.encode() not in ps.stdout + ps.stderr and (
            (ps.returncode == 0 and b'allowlist-proxy.py' in ps.stdout) or blocked),
            ps_readable=ps.returncode == 0, ps_returncode=ps.returncode,
            ps_status='无法查看（内核拒绝）' if blocked else ('可查看' if ps.returncode == 0 else '查询失败'))
    # macOS KERN_PROCARGS2；异常和 libc 返回的 errno 都只接受 EPERM/EACCES。
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        mib = (ctypes.c_int * 3)(1, 49, pid)
        size = ctypes.c_size_t(1024 * 1024)
        buffer = ctypes.create_string_buffer(size.value)
        rc = libc.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0)
        err = ctypes.get_errno() if rc else 0
    except OSError as exc:
        if exc.errno not in denied:
            raise
        rc, err = -1, exc.errno
    blocked = rc == -1 and err in denied
    checks.update(sysctl_no_key=((rc == 0 and expected.encode() not in buffer.raw[:size.value] and
                                 b'allowlist-proxy.py' in buffer.raw[:size.value]) or blocked),
                  sysctl_errno=err, sysctl_readable=rc == 0,
                  sysctl_status='无法查看（内核拒绝）' if blocked else ('可查看' if rc == 0 else '查询失败'))
    return checks

if '--version' in sys.argv:
    print('opencode fake-proxy-probe-v1')
    sys.exit(0)
run = pathlib.Path(sys.argv[sys.argv.index('--dir') + 1])
task = sys.stdin.read().split('---')[-1].strip()
info = json.loads((run / '网络信息.json').read_text())
expected = 'FAKE-HOST-PROXY-CREDENTIAL-20261014'
checks = {}
if task.startswith('hold'):
    child = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(300)'])
    (run / 'probe-ready.json').write_text(json.dumps({'pid': os.getpid(), 'child': child.pid}))
    time.sleep(300)
    sys.exit(1)
blocked_port = int(task.split()[-1])
checks['environment'] = expected not in json.dumps(dict(os.environ)) and 'FRCLYO_PROVIDER_API_KEY' not in os.environ
cfg = json.loads(pathlib.Path(os.environ['OPENCODE_CONFIG']).read_text())
options = cfg['provider']['deepseek']['options']
checks['template'] = (options['baseURL'] == 'http://127.0.0.1:%s/deepseek' % info['端口'] and
                      options['apiKey'] == 'frclyo-local-proxy-placeholder' and
                      expected not in json.dumps(cfg) and cfg == json.loads(os.environ['OPENCODE_CONFIG_CONTENT']))
# 只检查本轮代理 PID；不输出 ps/sysctl 内容，失败证据也只写布尔值和 errno。
checks.update(process_credentials(info['代理进程号'], expected))
# 对本轮代理发非法路由，必须拿到 HTTP 403；连接失败绝不当作通过。
conn = http.client.HTTPConnection('127.0.0.1', info['端口'], timeout=3)
conn.request('POST', '/other-provider/chat/completions', body=b'{}', headers={'Authorization': expected})
resp = conn.getresponse()
checks['own_proxy'] = resp.status == 403
resp.read()
conn.close()
for label, family, address in (
        ('other_port', socket.AF_INET, ('127.0.0.1', blocked_port)),
        ('public_tcp', socket.AF_INET, ('1.1.1.1', 443)),
        ('ipv6', socket.AF_INET6, ('::1', info['端口']))):
    try:
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.settimeout(2)
            sock.connect(address)
        checks[label] = False
    except OSError as exc:
        checks[label] = exc.errno in (errno.EPERM, errno.EACCES)
try:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.sendto(b'probe', ('127.0.0.1', blocked_port))
    checks['udp'] = False
except OSError as exc:
    checks['udp'] = exc.errno in (errno.EPERM, errno.EACCES)
(run / 'probe-result.json').write_text(json.dumps(checks))
print(json.dumps({'type': 'text', 'part': {'text': 'PROXY_PROBE_COMPLETE'}}), flush=True)
'''


def failure_details(run, launcher_rc):
    """内层失败时保留可定位证据；任何包含假 key 的文本都不回显。"""
    control = run.parents[2] / '运行时/控制/opencode/PROXY-HOST' / run.name
    stderr = control / 'stderr.log'
    def safe_print(text):
        print(text if FAKE_KEY not in text else '诊断包含假 key，已隐藏。', flush=True)
    try:
        receipt = json.loads((run / '回执.json').read_text())
        safe_print('内层回执：状态=%s，退出码=%s；入口退出码=%s' % (
            receipt.get('状态', '缺失'), receipt.get('退出码', '缺失'), launcher_rc))
    except (OSError, ValueError, AttributeError) as exc:
        safe_print('内层回执：不可用（%s）；入口退出码=%s' % (type(exc).__name__, launcher_rc))
    safe_print('内层 stderr.log：' + str(stderr))
    try:
        data = stderr.read_bytes()
    except OSError as exc:
        safe_print('stderr.log 不可读：' + type(exc).__name__)
        return
    # 先检查完整文件；即使 key 只出现在较早行，也不打印日志尾部。
    if FAKE_KEY.encode() in data:
        print('stderr.log 含假 key，已隐藏全部内容。', flush=True)
        return
    tail = b'\n'.join(data.splitlines()[-8:]).decode('utf-8', errors='replace')
    safe_print('stderr.log 末尾（最多 8 行 / 4000 字符）：\n' + tail[-4000:])


def alive(pid):
    result = subprocess.run(['/bin/ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True, timeout=3)
    return result.returncode == 0 and bool(result.stdout.strip()) and not result.stdout.strip().startswith('Z')


def wait_json(path, proc, seconds=20):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        try:
            return json.loads(path.read_text())
        except (FileNotFoundError, ValueError):
            if proc.poll() is not None:
                raise RuntimeError('入口提前退出，检查 launcher.log')
            time.sleep(.05)
    raise RuntimeError('等待入口就绪超时')


def gone(info, worker=None, child=None):
    until = time.monotonic() + 10
    while time.monotonic() < until:
        if all(not alive(pid) for pid in (info['代理进程号'], worker, child) if pid):
            try:
                with socket.create_connection(('127.0.0.1', info['端口']), timeout=.3):
                    pass
            except OSError as exc:
                if exc.errno == errno.ECONNREFUSED:
                    return True
        time.sleep(.1)
    return False


def prepare_fake_opencode(commands, inputs):
    inputs.mkdir()
    probe = inputs / 'probe.py'
    probe.write_text(PROBE)
    oc = commands / 'opencode'
    # --version 在本轮尚未创建时由宿主查询；实际探针只从生产输入复制口读取。
    oc.write_text('#!/bin/bash\n'
        'if [ "$1" = --version ]; then echo opencode-fake-proxy-probe-v2; exit 0; fi\n'
        'run=""; next=0\n'
        'for arg in "$@"; do\n'
        '  if [ "$next" = 1 ]; then run="$arg"; break; fi\n'
        '  [ "$arg" = --dir ] && next=1\n'
        'done\n'
        '[ -n "$run" ] || exit 2\n'
        'exec ' + shlex.quote(sys.executable) + ' -I "$run/输入/probe.py" "$@"\n')
    oc.chmod(0o700)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='真实 OpenCode + DeepSeek，小任务会产生模型费用')
    parser.add_argument('--models', help='live 必填，本机 models.dev JSON 快照')
    parser.add_argument('--opencode', help='live 使用的 OpenCode 可执行文件')
    args = parser.parse_args()
    if sys.platform != 'darwin' or not shutil.which('sandbox-exec'):
        parser.error('需要 macOS 宿主的真实 sandbox-exec；受限工人中不运行')
    if args.live and (not args.models or not Path(args.models).is_file()):
        parser.error('--live 需要 --models 本机快照；本探针不下载')
    real_oc = args.opencode or shutil.which('opencode')
    if args.live and (not real_oc or not os.access(real_oc, os.X_OK)):
        parser.error('--live 需要真实 OpenCode')
    # 在切换假 HOME/配置之前只读取路由与条目名称，与完整隔离检查共用实现。
    live_config = recitation_config(args.models) if args.live else None
    root = Path(tempfile.mkdtemp(prefix='frclyo-proxy-host-')).resolve()
    os.chmod(root, 0o700)
    print('证据目录：' + str(root), flush=True)
    checks, children, infos = {}, [], []
    current_run = None
    current_proc = None
    # 初始环境不继承调用者 key；live 由代理按配置的条目名称查询钥匙串。
    base_env = {name: value for name, value in os.environ.items()
                if name in ('PATH', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TZ')}
    base_env['PYTHONDONTWRITEBYTECODE'] = '1'

    def launch(name, task_text, seconds, live=False):
        nonlocal current_run, current_proc
        area = root / name
        home = area / 'home'
        work = home / 'work'
        commands = area / 'commands'
        private = work / '配置'
        for path in (private, commands):
            path.mkdir(parents=True, exist_ok=True)
        cfg = private / 'config.json'
        models = area / 'models.json'
        if live:
            shutil.copyfile(args.models, models)
            (commands / 'opencode').symlink_to(Path(real_oc).resolve())
        else:
            models.write_text(json.dumps({'deepseek': {'id': 'deepseek', 'api': 'https://api.deepseek.com',
                'npm': '@ai-sdk/openai-compatible', 'models': {'deepseek-flash': {'id': 'deepseek-flash'}}}}))
            inputs = area / 'inputs'
            prepare_fake_opencode(commands, inputs)
            # 假 key 只存于被根目录规则保护的假配置区域，不进任何启动环境。
            security = private / 'fake-security'
            security.write_text('#!/bin/bash\n[ "$1" = find-generic-password ] || exit 2\nprintf %s ' + shlex.quote(FAKE_KEY) + '\n')
            security.chmod(0o700)
            (commands / 'security').symlink_to(security)
        settings = dict(live_config) if live else {}
        settings.update(opencode_config=str(BIN.parent / 'workers/opencode/opencode.worker.jsonc'),
                        models_snapshot_source=str(models))
        cfg.write_text(json.dumps(settings))
        task = area / 'task.txt'
        task.write_text(task_text)
        env = dict(base_env, HOME=str(home), FRCLYO_HOME=str(work), FRCLYO_CONFIG=str(cfg),
                   PATH=str(commands) + os.pathsep + base_env.get('PATH', '/usr/bin:/bin'))
        if not live:
            env['WORKER_INPUT_DIR'] = str(inputs)
        run = work / '交付/PROXY-HOST' / name
        current_run = run
        current_proc = None
        with (area / 'launcher.log').open('wb') as output:
            proc = subprocess.Popen(['/bin/bash', str(BIN / 'opencode-worker.sh'), 'PROXY-HOST', name,
                str(task), 'deepseek/deepseek-flash', 'nothink', str(seconds)],
                env=env, stdout=output, stderr=subprocess.STDOUT)
        children.append(proc)
        current_proc = proc
        info = wait_json(run / '网络信息.json', proc)
        infos.append(info)
        return proc, run, info

    listener = None
    try:
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(8)
        blocked_port = listener.getsockname()[1]
        with socket.create_connection(('127.0.0.1', blocked_port), timeout=1):
            pass  # 正向对照：宿主已证实此端口可连，内核 EPERM 才能算隔离。
        proc, run, info = launch('credentials', 'probe %d' % blocked_port, 40)
        rc = proc.wait(timeout=55)
        if rc != 0:
            raise RuntimeError('内层凭据探针失败')
        result = json.loads((run / 'probe-result.json').read_text())
        for name in ('environment', 'template', 'ps_no_key', 'sysctl_no_key', 'own_proxy',
                     'other_port', 'public_tcp', 'ipv6', 'udp'):
            checks[name] = result[name] is True
        receipt = json.loads((run / '回执.json').read_text())
        checks['receipt'] = rc == 0 and receipt['状态'] == '执行完成' and receipt['网络'] == {
            '放行': ['api.deepseek.com:443'], '拒绝次数': 1}
        control = run.parents[2] / '运行时/控制/opencode/PROXY-HOST/credentials'
        checks['no_key_in_logs'] = all(FAKE_KEY.encode() not in path.read_bytes()
            for path in list(control.iterdir()) + list(run.iterdir()) if path.is_file())
        checks['normal_cleanup'] = gone(info)
        # 宿主对代理 argv/env 再做正向可读检查，避免 worker ps 被阻止掩盖环境泄漏。
        for name, sig, timeout in (('timeout', None, 4), ('term', signal.SIGTERM, 60), ('kill', signal.SIGKILL, 60)):
            proc, run, info = launch(name, 'hold', timeout)
            ready = wait_json(run / 'probe-ready.json', proc)
            ps = subprocess.run(['/bin/ps', '-E', '-ww', '-p', str(info['代理进程号']), '-o', 'command='], capture_output=True, timeout=5)
            checks[name + '_host_ps'] = ps.returncode == 0 and b'allowlist-proxy.py' in ps.stdout and FAKE_KEY.encode() not in ps.stdout + ps.stderr
            if sig:
                proc.send_signal(sig)
            rc = proc.wait(timeout=20)
            expected = 142 if sig is None else (143 if sig == signal.SIGTERM else -signal.SIGKILL)
            checks[name + '_cleanup'] = rc == expected and gone(info, ready['pid'], ready['child'])
            if sig != signal.SIGKILL:
                receipt = json.loads((run / '回执.json').read_text())
                checks[name + '_receipt'] = receipt['退出码'] == expected and receipt['进程组已回收'] is True
        if args.live:
            prompt = '只在本轮目录创建 model-sentinel.txt，内容为 MODEL_API_OK 换行，然后回复 MODEL_API_OK。不要做其他任务。'
            proc, run, info = launch('live', prompt, 180, live=True)
            rc = proc.wait(timeout=200)
            receipt = json.loads((run / '回执.json').read_text())
            checks['live'] = (rc == 0 and receipt['状态'] == '执行完成' and receipt['工具调用数'] >= 1 and
                              (run / 'model-sentinel.txt').read_text() == 'MODEL_API_OK\n' and gone(info))
    except BaseException as exc:
        checks['completed'] = False
        # 不回显异常内容、argv 或凭据，只留下类型。
        print('探针异常：' + type(exc).__name__, flush=True)
        if current_run is not None:
            failure_details(current_run, current_proc.poll() if current_proc else None)
    finally:
        for proc in children:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)
        for info in infos:
            if alive(info['代理进程号']):
                try:
                    os.kill(info['代理进程号'], signal.SIGTERM)
                except ProcessLookupError:
                    pass
        if listener:
            listener.close()
    (root / 'summary.json').write_text(json.dumps(checks, indent=2))
    for name, ok in checks.items():
        print(('PASS ' if ok else 'FAIL ') + name)
    return 0 if checks and all(checks.values()) else 1


if __name__ == '__main__':
    sys.exit(main())
