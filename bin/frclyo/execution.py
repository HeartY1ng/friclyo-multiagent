"""工人入口共用的文件边界、输入复制和宿主监督器（不信任轮次目录）。"""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import time
import uuid

RESULT_LIMIT = 16 * 1024 * 1024
LOG_LIMIT = 64 * 1024 * 1024
INPUT_BYTES = 1024 * 1024 * 1024
INPUT_FILES = 10000


def result_limit():
    """只用于结果；宿主环境设置，默认 16 MiB，非法值明确失败。"""
    raw = os.environ.get('WORKER_RESULT_MAX_BYTES', str(RESULT_LIMIT))
    if not raw.isascii() or not raw.isdigit() or not 1 <= int(raw) <= sys.maxsize - 1:
        raise ValueError('WORKER_RESULT_MAX_BYTES 必须为正整数且不超过平台读取上限')
    return int(raw)


def start_proxy(provider, control, parent):
    """宿主监督入口：取 key -> 匿名管道 -> exec 代理；argv/env/磁盘无 key。"""
    from . import keys
    from .worker import PROXY_ROUTES
    read_fd = write_fd = None
    try:
        if provider not in PROXY_ROUTES:
            raise ValueError()
        if int(parent) <= 1 or os.getppid() != int(parent):
            raise ValueError()
        value = keys.get_key(provider)
        if value is None:
            print('未取得本轮提供方凭据；代理和工人未启动。请检查钥匙串读取提示或登记本家 key。', file=sys.stderr)
            return 2
        if not isinstance(value, str) or not 1 <= len(value) <= 4095 or any(
                ord(c) <= 32 or ord(c) >= 127 for c in value):
            raise ValueError()
        read_fd, write_fd = os.pipe()
        # macOS/POSIX 最小 pipe 容量足够此有界单帧；写完即关闭，代理不会等未知发送者。
        data = (value + '\n').encode('ascii')
        if os.write(write_fd, data) != len(data):
            raise OSError()
        os.close(write_fd)
        write_fd = None
        os.dup2(read_fd, 0, inheritable=True)
        if read_fd != 0:
            os.close(read_fd)
        read_fd = None
        del value, data
        script = Path(__file__).resolve().parents[1] / 'allowlist-proxy.py'
        # exec 保持 PID/父 PID，沿用 shell 的清理和代理孤儿检查协议。
        # 空环境也排除 SSL_CERT_FILE/HTTPS_PROXY 等对认证目的地的覆盖。
        os.execve(sys.executable, [sys.executable, '-I', str(script), '--provider', provider,
                  '--log', str(Path(control) / '代理拒绝.log'), '--parent-pid', str(parent)], {})
    except Exception:
        print('代理准备失败：检查本家凭据与固定认证路由', file=sys.stderr)
        return 2
    finally:
        for fd in (read_fd, write_fd):
            if fd is not None:
                os.close(fd)


@contextlib.contextmanager
def directory(path, create=False):
    """逐段打开并钉住目录；拒绝任何链接祖先。调用者传规范绝对路径。"""
    parts = Path(os.path.abspath(path)).parts
    fd = os.open(parts[0], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[1:]:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = new
        yield fd
    finally:
        os.close(fd)


def _read_at(parent, name, limit):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('不是普通文件：' + name)
        if info.st_size > limit:
            raise ValueError('文件超过大小上限：' + name)
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError('读取时文件超过大小上限：' + name)
        return data


def safe_read(path, limit=RESULT_LIMIT):
    path = Path(path)
    with directory(path.parent) as parent:
        return _read_at(parent, path.name, limit)


def exclusive_write(path, data):
    path = Path(path)
    with directory(path.parent) as parent:
        fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=parent)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(parent)


def atomic_write(path, data):
    """临时文件与目标同目录；replace 只替换链接本身，永不打开旧目标。"""
    path = Path(path)
    with directory(path.parent) as parent:
        name = '.host-' + uuid.uuid4().hex
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path.name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:
                os.unlink(name, dir_fd=parent)
            except FileNotFoundError:
                pass


def write_json(path, value):
    # 在 JSON 序列化后统一转义孤立代理码点，覆盖键、嵌套值及列表；保留正常中文。
    atomic_write(path, (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode(
        'utf-8', errors='backslashreplace'))


def exclusive_directory(path):
    path = Path(path)
    with directory(path.parent, create=True) as parent:
        # mkdir 原子拒绝任何已存在节点；O_DIRECTORY|O_NOFOLLOW 校验创建结果。
        os.mkdir(path.name, 0o700, dir_fd=parent)
        fd = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        os.close(fd)


def private_directory(path):
    """共享私有父目录首次独占创建；复用必须同 uid、0700 且无链接祖先。

    不 chmod 预先存在的目录，不接管其他主体创建的节点。
    轮次叶节点仍使用 exclusive_directory，永不复用。
    """
    try:
        exclusive_directory(path)
    except FileExistsError:
        pass
    with directory(path) as fd:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError('私有目录父级必须属于当前宿主且权限为 0700')


def capture_directory(worker, run):
    from . import config
    cfg = config.load()
    root = Path(cfg['work_dir'])
    run = Path(run).absolute()
    relative = run.relative_to(root / '交付')
    if len(relative.parts) != 2 or any(p in ('.', '..') for p in relative.parts):
        raise ValueError('无效轮次目录')
    return Path(cfg['control_dir']).parent / worker / relative


def prepare_runtime(runtime, task, run, content):
    runtime = Path(runtime)
    state = runtime / 'rounds' / task / run
    exclusive_directory(state)
    for name in ('config', 'data', 'state', 'cache', 'tmp'):
        exclusive_directory(state / name)
    template = runtime / ('worker-' + task + '.' + run + '.json')
    exclusive_write(template, (content + '\n').encode())
    return str(state)


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def copy_inputs(source, destination, max_bytes=INPUT_BYTES, max_files=INPUT_FILES):
    """源根只 realpath 一次；完整清单通过后复制到暂存目录，成功才发布。"""
    if max_bytes < 1 or max_files < 1:
        raise ValueError('素材上限必须为正整数')
    source = Path(os.path.realpath(source))
    destination = Path(destination)
    manifest = []
    size = 0
    with directory(source) as root_fd:
        def scan(fd, relative=Path(), depth=0):
            nonlocal size
            if depth > 128:
                raise ValueError('素材目录超过 128 层')
            with os.scandir(fd) as entries:
                for entry in entries:
                    name = entry.name
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)  # lstat 语义
                    rel = relative / name
                    if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                        raise ValueError('素材含链接或特殊文件：' + str(rel))
                    manifest.append((rel, info))
                    if len(manifest) > max_files:
                        raise ValueError('素材文件/目录数量超过上限')
                    if stat.S_ISREG(info.st_mode):
                        size += info.st_size
                        if size > max_bytes:
                            raise ValueError('素材总大小超过上限')
                    else:
                        child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                        try:
                            if _identity(os.fstat(child)) != _identity(info):
                                raise ValueError('扫描时素材目录发生变化')
                            scan(child, rel, depth + 1)
                        finally:
                            os.close(child)
        scan(root_fd)
        with directory(destination.parent) as target_fd:
            if destination.name in os.listdir(target_fd):
                raise FileExistsError('输入目录已存在')
            stage_name = '.input-' + uuid.uuid4().hex
            os.mkdir(stage_name, 0o700, dir_fd=target_fd)
            stage = destination.parent / stage_name
            try:
                # 每次从已钉住的源根重新打开祖先；链接替换在复制阶段也被拒绝。
                identities = {rel: info for rel, info in manifest}
                for rel, expected in manifest:
                    parent_fd = os.dup(root_fd)
                    try:
                        ancestor = Path()
                        for part in rel.parts[:-1]:
                            ancestor /= part
                            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
                            os.close(parent_fd)
                            parent_fd = child
                            if _identity(os.fstat(child)) != _identity(identities[ancestor]):
                                raise ValueError('复制时素材目录发生变化')
                        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                        if stat.S_ISDIR(expected.st_mode):
                            flags |= os.O_DIRECTORY
                        fd = os.open(rel.name, flags, dir_fd=parent_fd)
                        try:
                            if _identity(os.fstat(fd)) != _identity(expected):
                                raise ValueError('复制时素材发生变化')
                            if stat.S_ISDIR(expected.st_mode):
                                (stage / rel).mkdir(mode=0o700)
                            else:
                                with open(stage / rel, 'xb') as output:
                                    remaining = expected.st_size
                                    while remaining:
                                        block = os.read(fd, min(1024 * 1024, remaining))
                                        if not block:
                                            raise ValueError('复制时素材被截断')
                                        output.write(block)
                                        remaining -= len(block)
                                    if os.read(fd, 1) or _identity(os.fstat(fd)) != _identity(expected):
                                        raise ValueError('复制时素材发生变化')
                                    output.flush()
                                    os.fsync(output.fileno())
                                os.chmod(stage / rel, 0o700 if expected.st_mode & 0o111 else 0o600)
                        finally:
                            os.close(fd)
                    finally:
                        os.close(parent_fd)
                # 本轮尚未启动；目标不存在且轮次目录由宿主独占。
                os.rename(stage_name, destination.name, src_dir_fd=target_fd, dst_dir_fd=target_fd)
                os.fsync(target_fd)
            finally:
                if stage.exists():
                    shutil.rmtree(stage)


def signal_group(child, sig):
    for attempt in range(2):
        try:
            os.killpg(child.pid, sig)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            child.poll()
    return None


def group_running(child):
    child.poll()
    exists = signal_group(child, 0)
    if exists is False:
        return False
    # macOS 的僵尸组可报 EPERM；Linux 的僵尸组也可能 killpg(0) 成功。
    # 只把已确认全为僵尸的组当作退出；真正活着/查不清的 EPERM 不冒充退出。
    try:
        snapshot = subprocess.run(['/bin/ps', '-axo', 'pgid=,stat='],
                                  capture_output=True, text=True, timeout=0.5, check=True)
        for line in snapshot.stdout.splitlines():
            pgid, state = line.split()
            if int(pgid) == child.pid and not state.startswith('Z'):
                return True
        return child.poll() is None
    except (OSError, ValueError, subprocess.SubprocessError):
        return True


class Descendants:
    """采样已知后代的 PID + 启动时间；不读取命令行或进程环境。

    ps 的秒级启动时间与信号之间仍有竞态；两次采样间脱离的后代可能漏记。
    这不是内核级进程容器。查询失败时不能确认回收；后续仍采样核对已知身份。
    """
    def __init__(self, child):
        self.child = child
        self.known = {}
        self.complete = True
        self.snapshot_warned = False

    def sample(self):
        try:
            result = subprocess.run(['/bin/ps', '-axo', 'pid=,ppid=,pgid=,stat=,lstart='],
                                    capture_output=True, text=True, timeout=0.5, check=True,
                                    env=dict(os.environ, LC_ALL='C'))
            rows = {}
            for line in result.stdout.splitlines():
                pid, ppid, pgid, state, started = line.split(None, 4)
                rows[int(pid)] = (int(ppid), int(pgid), state, started)
            if not rows:
                raise ValueError('empty process snapshot')
            roots = {pid for pid, started in self.known.items()
                     if pid in rows and rows[pid][3] == started}
            if self.child.poll() is None:
                roots.add(self.child.pid)
            # 原组中尚未采到的后代也纳入；包括直接工人刚刚退出的情况。
            roots.update(pid for pid, row in rows.items() if row[1] == self.child.pid)
            while True:
                added = {pid for pid, row in rows.items() if row[0] in roots} - roots
                if not added:
                    break
                roots.update(added)
            for pid in roots:
                if pid in rows:
                    self.known[pid] = rows[pid][3]
            self.complete = True
            return rows
        except (OSError, ValueError, subprocess.SubprocessError):
            self.complete = False
            if not self.snapshot_warned:
                print('后代进程快照失败；将继续采样，未确认前不报告全部回收', file=sys.stderr)
                self.snapshot_warned = True
            return None

    def running(self, sig=None):
        rows = self.sample()
        if rows is None:
            return True
        alive = False
        for pid, started in tuple(self.known.items()):
            row = rows.get(pid)
            if (row is None or row[3] != started or row[2].startswith('Z')
                    or row[1] == self.child.pid):
                continue
            alive = True
            if sig is not None:
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    pass
                except OSError:
                    self.complete = False
        return alive


def reap_group(child, grace=2):
    descendants = getattr(child, '_frclyo_descendants', None)
    if descendants is None:
        descendants = child._frclyo_descendants = Descendants(child)
    descendants.running(signal.SIGCONT)
    descendants.running(signal.SIGTERM)
    def running():
        # 两者都检查，不让短路运算跳过后代采样。
        detached = descendants.running()
        return group_running(child) or detached
    signal_group(child, signal.SIGCONT)
    signal_group(child, signal.SIGTERM)
    until = time.monotonic() + grace
    while time.monotonic() < until and running():
        time.sleep(0.05)
    if running():
        descendants.running(signal.SIGKILL)
        signal_group(child, signal.SIGKILL)
        until = time.monotonic() + 1
        while time.monotonic() < until and running():
            time.sleep(0.05)
    clean = not running() and descendants.complete
    try:
        child.wait(timeout=0.1)
    except subprocess.TimeoutExpired:
        clean = False
    return clean


def monitor_process(child, seconds, parent, proxy, timeout_code, reason, drain, status):
    """由两种工人的同一监督器调用；drain 负责有界捕获并推进等待。"""
    deadline = time.monotonic() + seconds
    descendants = child._frclyo_descendants = Descendants(child)
    descendants.sample()
    # 拍快照要跑一次 ps：开头每 0.05 秒拍一次，之后间隔逐次翻倍，最长 1 秒，长任务不白占 CPU
    interval = 0.05
    next_sample = time.monotonic() + interval
    while child.poll() is None and not reason[0]:
        drain(0.05)
        if time.monotonic() >= next_sample:
            descendants.sample()
            interval = min(1.0, interval * 2)
            next_sample = time.monotonic() + interval
        if os.getppid() != parent:
            reason[0] = reason[0] or 143
        elif time.monotonic() >= deadline:
            if not reason[0]:
                status['timed_out'] = True
                reason[0] = timeout_code
        elif proxy:
            try:
                os.kill(proxy, 0)
            except (ProcessLookupError, PermissionError):
                reason[0] = reason[0] or 125
    status['group_reaped'] = reap_group(child)


def supervise(command, task, control, seconds, parent, timeout_code, proxy=0, provider=None):
    """宿主独占日志句柄；子进程只拿 pipe，不能经 stdout fd 改写捕获文件。"""
    control = Path(control)
    reason = [0]
    handlers = {}
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        handlers[sig] = signal.signal(sig, lambda sig, frame: reason[0] or reason.__setitem__(0, 128 + sig))
    # True 仅表示实际启动过的工人组已确认回收；未启动或无法确认均为 False。
    status = dict(rc=125, timed_out=False, group_reaped=False, errors=[])
    child = None
    try:
        # OpenCode 子进程只获得显式允许的运行环境；监督器不再为它取 key。
        from .worker import worker_environment
        child_env = worker_environment() if provider else None
        if os.getppid() != parent:
            raise ValueError('启动前父进程已退出')
        with contextlib.ExitStack() as stack:
            outputs = []
            for name in ('events.jsonl', 'stderr.log'):
                with directory(control) as d:
                    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=d)
                outputs.append(stack.enter_context(os.fdopen(fd, 'wb')))
            source = stack.enter_context(open(task, 'rb'))  # 启动前宿主写入，尚无工人
            # --dir/-C 要等 CLI 初始化才生效。先在宿主侧进入本轮，避免启动时
            # getcwd/配置发现访问调用者的框架目录；不增加任何读取例外。
            child = subprocess.Popen(command, stdin=source, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     start_new_session=True, close_fds=True, env=child_env,
                                     cwd=str(Path(task).absolute().parent))
            selector = stack.enter_context(selectors.DefaultSelector())
            for pipe, output in zip((child.stdout, child.stderr), outputs):
                stack.enter_context(pipe)
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, output)
            total = [0]
            def drain(wait):
                for key, _ in selector.select(wait):
                    block = os.read(key.fd, 65536)
                    if not block:
                        selector.unregister(key.fileobj)
                        continue
                    room = max(0, LOG_LIMIT - total[0])
                    key.data.write(block[:room])
                    total[0] += len(block)
                    if total[0] > LOG_LIMIT:
                        if not reason[0]:
                            reason[0] = 125
                            status['errors'].append('stdout/stderr 合计超过 64 MiB')
            monitor_process(child, seconds, parent, proxy, timeout_code, reason, drain, status)
            # 整组回收后管道只含有限的尾部数据；脱组后仍持有管道也不能无限等待。
            until = time.monotonic() + 1
            while selector.get_map() and time.monotonic() < until:
                drain(0.02)
            if selector.get_map():
                status['errors'].append('进程组退出后输出管道仍未关闭')
                reason[0] = reason[0] or 125
            if not status['group_reaped']:
                status['errors'].append('监督器无法确认进程组已退出：pgid=%s' % child.pid)
            rc = child.returncode
            status['rc'] = reason[0] or (125 if not status['group_reaped'] else (128 - rc if rc < 0 else rc))
            for output in outputs:
                output.flush()
                os.fsync(output.fileno())
    except Exception as exc:
        status['rc'] = reason[0] or 125
        status['errors'].append('监督器失败：' + type(exc).__name__)
        if child is not None:
            status['group_reaped'] = reap_group(child)
        else:
            status['errors'].append('工人未启动；没有可回收的进程组')
    try:
        status['rc'] = reason[0] or status['rc']
        write_json(control / 'status.json', status)
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
    return status['rc']


def events_from(data, errors='replace'):
    for line in data.decode('utf-8', errors=errors).splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            yield value


def task_project(run):
    """启动前从注入文本中的任务卡第一行取项目，仅用于回执标签。"""
    try:
        text = safe_read(Path(run) / '任务.txt', LOG_LIMIT).decode('utf-8')
        card = text.split('\n\n---\n\n', 1)[1]
        first = card.splitlines()[0]
        return first[len('项目：'):].strip() if first.startswith('项目：') else ''
    except (OSError, ValueError, IndexError):
        return ''


def receipt_summary(control, run, receipt=None):
    """只打印可信回执摘要，不读取或打印结果正文。"""
    if receipt is None:
        receipt = json.loads(safe_read(Path(control) / 'receipt.json'))
    # 项目与状态归一为单行，避免控制字符制造额外 stdout 行。
    project = ''.join(c for c in receipt.get('项目', '') if c.isprintable())
    print('【项目 %s】%s %s %s（回执：%s）' % (
        project, receipt['任务编号'], receipt['轮次编号'], receipt['状态'],
        Path(run) / '回执.json'))


def finalize(worker, run, control, task, round_id, model, level, start, end, rule_sha,
             version='unknown', target='', fallback=0):
    run, control = Path(run), Path(control)
    errors = []
    try:
        status = json.loads(safe_read(control / 'status.json'))
    except (OSError, ValueError):
        status = dict(rc=fallback or 125, timed_out=False, group_reaped=False,
                      errors=['缺少可信监督状态；未做工人文件后处理'])
    errors.extend(status.get('errors', []))
    rc = status['rc'] or fallback
    try:
        project = json.loads(safe_read(control / 'project.json'))
    except (OSError, ValueError):
        project = ''
    receipt = {'项目': project, '工人': worker, '模型': model, '任务编号': task, '轮次编号': round_id,
               '退出码': rc, '开始': start, '结束': end, '守则sha256': rule_sha,
               '结果sha256': None, '进程组已回收': status['group_reaped']}
    sha, completed, usage, tid, commands = None, False, None, None, 0
    texts, seen, tokens = [], set(), {}
    captured = b''
    # 无法确认回收时只写失败回执，绝不读工人产物或更新模型记录。
    if status['group_reaped']:
        try:
            captured = safe_read(control / 'events.jsonl', LOG_LIMIT)
            for name, data in (('events.jsonl', captured),
                               ('stderr.log', safe_read(control / 'stderr.log', LOG_LIMIT))):
                atomic_write(run / name, data)
            encoding_error = False
            if worker == 'opencode':
                try:
                    captured.decode('utf-8')
                except UnicodeDecodeError as exc:
                    encoding_error = True
                    errors.append('OpenCode 事件编码错误：非法 UTF-8，字节偏移 %s' % exc.start)
            # OpenCode 的坏字节往返保留到结果；上面的诊断确保不能记为成功。
            for e in events_from(captured, errors='surrogateescape' if worker == 'opencode' else 'replace'):
                kind = e.get('type')
                if worker == 'codex':
                    if kind == 'thread.started':
                        tid = e.get('thread_id')
                    elif kind == 'turn.completed':
                        completed, usage = True, e.get('usage')
                    elif kind == 'turn.failed':
                        completed = False
                    elif kind == 'item.completed' and isinstance(e.get('item'), dict) and e['item'].get('type') == 'command_execution':
                        commands += 1
                else:
                    part = e.get('part') or {}
                    if not isinstance(part, dict):
                        continue
                    if kind == 'text' and isinstance(part.get('text'), str) and part['text']:
                        texts.append(part['text'])
                    elif kind == 'tool_use':
                        commands += 1
                    elif kind == 'error':
                        errors.append(json.dumps(e, ensure_ascii=True)[:300])
                    elif kind == 'step_finish' and isinstance(part.get('tokens'), dict):
                        for k, v in part['tokens'].items():
                            if k != 'cache' and isinstance(v, (int, float)):
                                tokens[k] = tokens.get(k, 0) + v
                        # OpenCode 缓存是嵌套计数，不能被顶层数值筛选丢弃。
                        cache = part['tokens'].get('cache')
                        if isinstance(cache, dict):
                            for key, field in (('read', 'cached_input_tokens'), ('write', 'cache_write_input_tokens')):
                                value = cache.get(key)
                                if isinstance(value, (int, float)):
                                    tokens[field] = tokens.get(field, 0) + value
                    for key in ('modelID', 'model'):
                        value = part.get(key) or e.get(key)
                        if isinstance(value, str):
                            # 事件字段也可能含代理码点，回执必须能编码为 UTF-8。
                            seen.add(value.encode('utf-8', errors='backslashreplace').decode('utf-8'))
            # 即使 OpenCode 会从可信文本生成结果，也先拒绝工人放置的恶意节点。
            limit = result_limit()
            result = run / '结果.txt'
            try:
                data = safe_read(result, limit)
            except FileNotFoundError:
                data = None
            if worker == 'opencode' and texts:
                data = (texts[-1].strip() + '\n').encode(
                    'utf-8', errors='surrogateescape' if encoding_error else 'strict')
                if len(data) > limit:
                    raise ValueError('结果超过 WORKER_RESULT_MAX_BYTES 上限')
                atomic_write(result, data)
            if data is not None:
                sha = hashlib.sha256(data).hexdigest()
        except (OSError, ValueError) as exc:
            errors.append('后处理拒绝：' + str(exc))
    if worker == 'opencode':
        denied = None
        try:
            denied = int(json.loads(safe_read(control / '代理.stdout', LOG_LIMIT).splitlines()[-1])['拒绝次数'])
        except (OSError, ValueError, TypeError, KeyError, IndexError):
            errors.append('代理未留下可信退出计数')
        if status['group_reaped']:
            for name in ('代理.stderr', '代理拒绝.log'):
                try:
                    atomic_write(run / name, safe_read(control / name, LOG_LIMIT))
                except FileNotFoundError:
                    pass
                except (OSError, ValueError) as exc:
                    errors.append('代理日志发布失败：' + str(exc))
        receipt.update({'变体': level or '默认', '事件中出现的模型': sorted(seen),
                        '工具调用数': commands, '用量': tokens or None,
                        '网络': {'放行': [target], '拒绝次数': denied}})
    else:
        receipt.update({'思考档位': level, 'CLI版本': version, 'threadId': tid,
                        '模型清单来源': os.environ.get('CODEX_MODEL_SOURCE'),
                        '执行命令数': commands, '用量': usage,
                        '命令网络': '开' if os.environ.get('CODEX_WORKER_NET') == '1' else '关',
                        '网页搜索': os.environ.get('CODEX_WORKER_WEB', '0') == '1',
                        '快速档': os.environ.get('CODEX_WORKER_FAST_ON', '0') == '1'})
    ok = rc == 0 and sha is not None and not errors and status['group_reaped'] and (completed or worker == 'opencode')
    if status['group_reaped']:
        from .models import observe
        try:
            available = observe(worker, model, version, run, succeeded=ok, captured=captured)
            receipt['模型可用性'] = available
            if available == 'unusable':
                ok = False
        except (OSError, ValueError) as exc:
            errors.append('模型记录失败：' + type(exc).__name__)
            ok = False
    else:
        receipt['模型可用性'] = 'unknown'
    if not ok and not rc:
        rc = 1
    receipt.update({'退出码': rc, '结果sha256': sha, '错误': errors,
                    '状态': '超时' if status['timed_out'] else ('执行完成' if ok else '失败')})
    write_json(control / 'receipt.json', receipt)
    try:
        write_json(run / '回执.json', receipt)
    except OSError as exc:
        # 例如工人把回执名建成目录：不删除其内容，也不丢失宿主失败诊断。
        rc = rc or 1
        receipt['退出码'] = rc
        receipt['状态'] = '超时' if status['timed_out'] else '失败'
        receipt['错误'].append('回执发布失败：' + str(exc))
        write_json(control / 'receipt.json', receipt)
        print('回执发布失败；原因保存在宿主控制目录 receipt.json', file=sys.stderr)
    return rc


def prepare_input_cli(args):
    run, worker, task, round_id, model, level, rule_sha = args
    try:
        source = os.environ.get('WORKER_INPUT_DIR')
        if source:
            copy_inputs(source, Path(run) / '输入',
                        int(os.environ.get('WORKER_INPUT_MAX_BYTES', INPUT_BYTES)),
                        int(os.environ.get('WORKER_INPUT_MAX_FILES', INPUT_FILES)))
        return 0
    except (OSError, ValueError) as exc:
        receipt = {'项目': task_project(run), '工人': worker, '模型': model, '任务编号': task, '轮次编号': round_id,
                   '退出码': 2, '状态': '准备失败', '错误': ['素材复制失败：' + str(exc)],
                   '守则sha256': rule_sha, '结果sha256': None}
        receipt['思考档位' if worker == 'codex' else '变体'] = level
        if worker == 'codex':
            receipt['模型清单来源'] = os.environ.get('CODEX_MODEL_SOURCE')
            receipt['命令网络'] = '开' if os.environ.get('CODEX_WORKER_NET') == '1' else '关'
        write_json(Path(run) / '回执.json', receipt)
        print('素材准备失败；未启动工人', file=sys.stderr)
        receipt_summary(None, run, receipt)
        return 2


def main():
    mode, *args = sys.argv[1:]
    if mode == 'proxy':
        return start_proxy(*args)
    if mode == 'local-config':
        from .worker import local_config
        print(local_config(*args))
        return 0
    if mode == 'inputs':
        return prepare_input_cli(args)
    if mode == 'control':
        path = capture_directory(args[0], args[1])
        exclusive_directory(path)
        write_json(path / 'project.json', task_project(args[1]))
        print(path)
        return 0
    if mode == 'runtime':
        print(prepare_runtime(*args))
        return 0
    if mode == 'supervise':
        worker, control, seconds, parent, proxy, provider, task, *command = args
        return supervise(command, task, control, int(seconds), int(parent),
                         124 if worker == 'codex' else 142, int(proxy), provider or None)
    if mode == 'summary':
        receipt_summary(*args)
        return 0
    if mode == 'finalize':
        worker, run, control, task, round_id, model, level, start, end, rule_sha, version, target, fallback = args
        return finalize(worker, run, control, task, round_id, model, level, start, end, rule_sha,
                        version, target, int(fallback))
    raise ValueError('未知执行桥接命令')
