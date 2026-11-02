#!/usr/bin/env python3
"""宿主统一验收；子命令静默记录，末尾输出一屏。付费模式各运行一次正式冒烟卡。"""
import argparse
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
from datetime import datetime, timezone
import time

ROOT = Path(__file__).resolve().parents[2]
RECITATIONS = {'codex 真实守则复述', 'opencode 真实守则复述'}
sys.path.insert(0, str(ROOT / 'bin'))
from frclyo import config
from frclyo.execution import Descendants, reap_group

SMOKES = (('OpenCode 短卡', 'opencode', 'deepseek/deepseek-flash', 'nothink'),
          ('Codex 真实改代码', 'codex', 'gpt-6-astra', 'low'))
CODEX_FILES = ('calc.py', 'test_calc.py', 'changes.diff', 'swift-smoke', 'tools.txt')
TOOL_STEPS = ('git_init', 'git_add', 'git_status', 'git_diff', 'unittest',
              'swift_compile', 'swift_run')

def timestamp():
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')



def plan():
    py = [sys.executable, '-B']
    host = 'tests/host/'
    return [
        ('离线测试', py + ['tests/run_all.py'], 'exit'),
        ('隔离判定回归', py + ['-m', 'unittest', 'discover', '-s', host,
                              '-p', 'test_isolation_check.py', '-v'], 'exit'),
        ('TCP 代理', py + [host + 'test_proxy_tcp.py'], 'exit'),
        ('两轮替身', py + [host + 'runtime_rounds.py'], 'exit'),
        ('假凭据探针', py + [host + 'proxy_credentials.py'], 'exit'),
        ('离线内核隔离', ['bash', host + 'isolation_check.sh', '--offline-only'], 'isolation'),
        ('内核工具诊断', py + [host + 'sandbox_doctor.py', '--kernel-only'], 'exit'),
        ('OpenCode 路径对照', py + [host + 'sandbox_doctor.py', '--opencode-only'], 'exit'),
    ]


def clean_line(value, limit=100):
    value = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', value)
    return ''.join(c if c.isprintable() else ' ' for c in value).strip()[:limit]


def tail_reason(log, fallback):
    with log.open('rb') as stream:
        stream.seek(0, 2)
        stream.seek(max(0, stream.tell() - 65536))
        lines = stream.read().decode('utf-8', errors='replace').splitlines()
    for line in reversed(lines):
        line = clean_line(line)
        if line and not any(mark in line for mark in ('证据目录：', '系统临时哨兵目录：')):
            return line
    return fallback


def evidence_paths(log):
    paths = []
    # 只处理固定宿主脚本的显式证据行；不从模型正文收集路径。
    with log.open(encoding='utf-8', errors='replace') as stream:
        for line in stream:
            if '证据目录：' in line:
                path = Path(line.split('证据目录：', 1)[1].strip())
                if path.is_absolute() and path not in paths:
                    paths.append(path)
    return paths


def isolation_pass(summary, rc):
    """离线约定必须恰为退出 1；false 仅能是明确未执行的两项复述。"""
    if rc != 1 or not isinstance(summary, dict):
        return False
    checks, probes = summary.get('checks'), summary.get('probes')
    if not isinstance(checks, dict) or not isinstance(probes, dict):
        return False
    if not RECITATIONS <= checks.keys() or any(checks[k] is not False for k in RECITATIONS):
        return False
    for worker in ('codex', 'opencode'):
        probe = probes.get(worker, {})
        if not isinstance(probe, dict) or probe.get('状态') != '已运行' or probe.get('退出码') != 0:
            return False
        if not {worker + ' 本轮读写', worker + ' 探针退出'} <= checks.keys():
            return False
    return all(value is True for name, value in checks.items() if name not in RECITATIONS)


def archive_logs(paths, directory):
    """汇集日志与汇总，保留来源索引；不复制配置、凭据或模型缓存。"""
    import shutil
    sources = []
    for index, source in enumerate(paths):
        if source.is_symlink() or not source.is_dir():
            raise ValueError('证据目录不存在或为链接')
        destination = directory / ('evidence-%02d' % index)
        destination.mkdir()
        for current, dirs, files in os.walk(source, followlinks=False):
            base = Path(current)
            dirs[:] = [name for name in dirs if not (base / name).is_symlink()]
            for name in files:
                item = base / name
                if not item.is_symlink() and (name.endswith('.log') or name == 'summary.json'):
                    target = destination / item.relative_to(source)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(item, target)
        sources.append(str(source))
    (directory / 'sources.json').write_text(json.dumps(sources, ensure_ascii=False, indent=2))


class Interruption:
    def __init__(self):
        self.signal = None

    def receive(self, signum, frame):
        # Do not raise inside Popen, cleanup or evidence writes. Repeated signals
        # retain the first exit code and cannot interrupt the cleanup itself.
        if self.signal is None:
            self.signal = signum

    @property
    def returncode(self):
        return 128 + self.signal if self.signal is not None else None


_interruption = None


@contextmanager
def interrupt_scope():
    global _interruption
    if _interruption is not None:
        yield _interruption
        return
    state = _interruption = Interruption()
    handlers = {}
    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            handlers[signum] = signal.signal(signum, state.receive)
        yield state
    finally:
        for signum, handler in handlers.items():
            signal.signal(signum, handler)
        _interruption = None


def stop_group(proc):
    # The shared reaper also tracks descendants that start a new session.
    return reap_group(proc, grace=0.3)


def wait_check(proc, timeout, state, drain):
    descendants = proc._frclyo_descendants = Descendants(proc)
    deadline = None if timeout is None else time.monotonic() + timeout
    interval = 0.05
    next_sample = 0
    while True:
        now = time.monotonic()
        if now >= next_sample:
            descendants.sample()
            next_sample = time.monotonic() + interval
            interval = min(0.5, interval * 2)
        drain()
        if state.signal is not None or proc.poll() is not None:
            return proc.poll()
        if deadline is not None and now >= deadline:
            raise subprocess.TimeoutExpired(proc.args, timeout)
        try:
            return proc.wait(timeout=0.05)
        except subprocess.TimeoutExpired:
            pass


def run_check(name, argv, kind, directory, timeout, root=ROOT, *, env=None,
              live=False, shutdown_grace=5, archive_evidence=True):
    with interrupt_scope() as state:
        return _run_check(name, argv, kind, directory, timeout, root, env,
                          live, shutdown_grace, archive_evidence, state)


def _run_check(name, argv, kind, directory, timeout, root, env, live,
               shutdown_grace, archive_evidence, state):
    directory.mkdir()
    log = directory / 'output.log'
    start = time.monotonic()
    row = dict(name=name, status='失败', seconds=0.0, reason='', returncode=None,
               command=argv, log=str(log))
    proc = None
    try:
        with log.open('wb') as output, log.open('rb') as reader:
            def drain():
                if live:
                    chunk = reader.read(65536)
                    if chunk:
                        sys.stdout.buffer.write(chunk)
                        sys.stdout.buffer.flush()
                    return bool(chunk)
                return False

            try:
                if state.signal is None:
                    proc = subprocess.Popen(argv, cwd=root, env=env, stdin=subprocess.DEVNULL,
                                            stdout=output, stderr=subprocess.STDOUT,
                                            start_new_session=True)
                    row['returncode'] = wait_check(proc, timeout, state, drain)
            finally:
                if proc is not None:
                    # A nested runner needs time to save its own completed rows.
                    # Keep sampling while it exits, before the final tree sweep.
                    if state.signal is not None and proc.poll() is None:
                        proc._frclyo_descendants.sample()
                        try:
                            proc.send_signal(state.signal)
                        except ProcessLookupError:
                            pass
                        until = time.monotonic() + shutdown_grace
                        while proc.poll() is None and time.monotonic() < until:
                            proc._frclyo_descendants.sample()
                            drain()
                            time.sleep(0.05)
                    row['group_reaped'] = stop_group(proc)
                    if row['returncode'] is None:
                        row['returncode'] = proc.poll()
                while drain():
                    pass
        paths = evidence_paths(log) if archive_evidence else []
        archive_logs(paths, directory)
        ok = row['returncode'] == 0 and row.get('group_reaped', False)
        if kind == 'isolation':
            ok = row.get('group_reaped', False) and len(paths) == 1 and isolation_pass(
                json.loads((directory / 'evidence-00/summary.json').read_text()), row['returncode'])
        if ok:
            row['status'] = '通过'
        elif not row.get('group_reaped', True):
            row['reason'] = '后代进程未确认全部回收'
        else:
            row['reason'] = tail_reason(log, '退出码 ' + str(row['returncode']))
    except subprocess.TimeoutExpired:
        row['reason'] = '超时（%.1fs）；%s' % (timeout, tail_reason(log, '无输出'))
    except (OSError, ValueError, TypeError) as exc:
        row['reason'] = '启动/证据失败：' + type(exc).__name__
    except KeyboardInterrupt:
        state.receive(signal.SIGINT, None)
    if state.signal is not None:
        row.update(status='被中断', interrupted=True, signal=state.signal,
                   reason='被中断；' + ('后代进程已回收' if row.get('group_reaped') else
                                     '后代进程未确认全部回收'))
    row['seconds'] = round(time.monotonic() - start, 3)
    (directory / 'result.json').write_text(json.dumps(row, ensure_ascii=False, indent=2))
    return row


def smoke_evidence(worker, run):
    """只核对本次新轮次的固定产物，不把模型回复当回执。"""
    sentinel = run / 'model-sentinel.txt'
    if sentinel.is_symlink() or sentinel.read_bytes() != b'MODEL_API_OK\n':
        raise ValueError('哨兵内容不符')
    receipt = run / '回执.json'
    if receipt.is_symlink() or json.loads(receipt.read_text())['状态'] != '执行完成':
        raise ValueError('回执不是执行完成')
    if worker == 'codex':
        for name in CODEX_FILES:
            item = run / name
            if item.is_symlink() or not item.is_file() or not item.stat().st_size:
                raise ValueError('缺少拷回文件：' + name)
        if not os.access(run / 'swift-smoke', os.X_OK):
            raise ValueError('Swift 产物不可执行')
        lines = (run / 'tools.txt').read_text().splitlines()
        for step in TOOL_STEPS:
            codes = [line for line in lines if line.startswith('EXIT ' + step + '=')]
            if codes != ['EXIT ' + step + '=0']:
                raise ValueError('工具退出码缺失、重复或非零：' + step)


def run_smoke(spec, directory, timeout):
    name, worker, model, effort = spec
    task_id = 'TEST-' + timestamp() + '-' + worker
    run = config.root_dir() / '交付' / task_id / 'r1'
    # 不调用 ledger；生产入口自行原子占用新轮次，拒绝覆盖。
    command = ['/bin/bash', str(ROOT / 'bin' / (worker + '-worker.sh')),
               task_id, 'r1', str(ROOT / 'tests/host/冒烟卡' / (worker + '.txt')),
               model, effort, str(max(1, math.ceil(timeout)))]
    row = run_check(name, command, 'exit', directory, timeout)
    row.update(paid=True, task_id=task_id, run=str(run))
    try:
        if row['status'] == '通过':
            smoke_evidence(worker, run)
            row['evidence_verified'] = True
    except (OSError, ValueError, KeyError, TypeError) as exc:
        row.update(status='失败', reason='冒烟产物核验失败：' + type(exc).__name__)
    finally:
        try:
            (run / 'model-sentinel.txt').unlink(missing_ok=True)
        except OSError as exc:
            row.update(status='失败', reason='哨兵清理失败：' + type(exc).__name__)
    (directory / 'result.json').write_text(json.dumps(row, ensure_ascii=False, indent=2))
    return row


def render(rows, output):
    lines = ['检查 | 结果 | 耗时 | 原因']
    for row in rows:
        lines.append('%s | %s | %.1fs | %s' % (
            row['name'], row['status'], row['seconds'], clean_line(row['reason'])))
    failed = sum(row['status'] == '失败' for row in rows)
    interrupted = any(row.get('interrupted') for row in rows)
    lines += ['结论：' + ('被中断' if interrupted else '失败' if failed else ('通过' if any(row.get('paid') for row in rows) else '通过（默认不含真实模型检查）')),
              '证据目录：' + str(output)]
    return '\n'.join(lines)


def write_summary(rows, output, state):
    # A signal may arrive between checks or during the final evidence write.
    # The first signal is sticky, so at most one rewrite is required.
    while True:
        recorded_signal = state.signal
        if recorded_signal is not None and not any(row.get('interrupted') for row in rows):
            rows.append(dict(name='验收收尾', status='被中断', seconds=0,
                             reason='被中断', interrupted=True, signal=recorded_signal))
        (output / 'summary.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2))
        report = render(rows, output)
        (output / 'summary.txt').write_text(report + '\n')
        if state.signal == recorded_signal:
            return report


def positive_seconds(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError('超时必须为有限正数')
    return number


def main(argv=None):
    with interrupt_scope() as state:
        return _main(argv, state)


def _main(argv, state):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--paid', action='store_true', help='会花钱；OpenCode 和 Codex 正式冒烟卡各一次')
    parser.add_argument('--output', type=Path, help='新建证据目录；不能已存在')
    parser.add_argument('--timeout', type=positive_seconds, default=600, help='每项超时秒数，默认 600')
    args = parser.parse_args(argv)
    if args.paid:
        print('会花钱：两张真实模型冒烟卡消耗 API 费用或订阅额度。', flush=True)
    try:
        if args.output:
            output = args.output.absolute()
            output.mkdir(mode=0o700)
        else:
            parent = config.root_dir() / '运行时/验收'
            parent.mkdir(parents=True, exist_ok=True)
            output = parent / timestamp()
            output.mkdir(mode=0o700)
    except (OSError, ValueError) as exc:
        print('失败：无法新建证据目录（' + type(exc).__name__ + '）')
        return 1
    rows = []
    blocked = False
    for index, (name, command, kind) in enumerate(plan()):
        if blocked or state.signal is not None:
            rows.append(dict(name=name, status='跳过', seconds=0, reason='前项中断', interrupted=True))
            continue
        row = run_check(name, command, kind, output / ('%02d' % index), args.timeout)
        rows.append(row)
        blocked = row.get('interrupted', False)
    for offset, spec in enumerate(SMOKES):
        if args.paid and not blocked and state.signal is None:
            row = run_smoke(spec, output / ('paid-%02d' % offset), args.timeout)
            blocked = row.get('interrupted', False)
            rows.append(row)
        else:
            rows.append(dict(name=spec[0], status='跳过', seconds=0,
                             reason='前项中断' if blocked or state.signal is not None else '未指定 --paid',
                             interrupted=blocked or state.signal is not None))
    report = write_summary(rows, output, state)
    print(report)
    return state.returncode or int(any(row['status'] == '失败' for row in rows))


if __name__ == '__main__':
    sys.exit(main())
