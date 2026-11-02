"""复制交付复现脚本与所选框架；默认在 macOS 沙箱中逐个执行。"""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time

from . import config, execution


# 旧交付使用过这些名字；全部指向本次所选框架副本，避免误用宿主源码。
ROOT_VARIABLES = ('FRCLYO_REPRO_ROOT', 'REVIEW_SOURCE', 'REVIEW_REPO',
                  'AW_REVIEW_REPO', 'REVIEW_FRAMEWORK', 'FRCLYO_REVIEW_ROOT',
                  'REPRO_FRAMEWORK')
TIMEOUT = 300
SANDBOX = '/usr/bin/sandbox-exec'


class ReproError(Exception):
    """可向用户显示的准备错误，不包含配置或凭据内容。"""


def component(value):
    """沿用执行入口的任务/轮次字符范围，不让参数变成任意路径。"""
    if not re.fullmatch(r'[A-Za-z0-9_-]+', value):
        raise argparse.ArgumentTypeError('任务和轮次只能含英文字母、数字、下划线和连字符')
    return value


def test_name(value):
    """允许完整文件名或省略 .py；只做精确选择，不解释通配符。"""
    name = value if value.endswith('.py') else value + '.py'
    if not re.fullmatch(r'test_[^/\\\x00-\x1f\x7f]+\.py', name):
        raise argparse.ArgumentTypeError('测试名须为 test_名字.py，不含目录或通配符')
    if any(char in name for char in '*?[]'):
        raise argparse.ArgumentTypeError('测试名不能含通配符')
    return name


def ordinary_path(root, relative):
    """逐层拒绝链接，避免轮次或日志路径被链接导向其他目录。"""
    path = root
    for part in Path(relative).parts:
        path = path / part
        if path.is_symlink():
            raise ReproError('轮次和日志路径不能包含符号链接')
    return path


def copy_tree(source, destination):
    """只复制普通文件和目录；不跟随链接，不打开可能阻塞的管道。"""
    if not stat.S_ISDIR(source.lstat().st_mode):
        raise ReproError('输入和交付必须是普通目录')
    destination.mkdir()
    for source_file in source.iterdir():
        if source_file.name in ('.git', '__pycache__'):
            continue
        target = destination / source_file.name
        mode = source_file.lstat().st_mode
        if stat.S_ISDIR(mode):
            copy_tree(source_file, target)
        elif stat.S_ISREG(mode):
            shutil.copy2(source_file, target, follow_symlinks=False)
        else:
            raise ReproError('输入或交付包含符号链接或特殊文件，未执行测试')


def sandbox_profile(work):
    """禁止联网；所有写入仅限副本与 /dev/null，原轮次和家目录不开放。"""
    quoted = json.dumps(str(work.resolve()), ensure_ascii=False)
    home = json.dumps(str(Path.home().resolve()), ensure_ascii=False)
    return '\n'.join([
        '(version 1)', '(allow default)', '(deny network*)',
        '(deny file-write* (require-all (require-not (subpath %s)) '
        '(require-not (literal "/dev/null"))))' % quoted,
        '(deny file-write* (subpath %s))' % home,
    ]) + '\n'


def environment(work, framework=None):
    """构造干净的子进程环境，不继承宿主密钥、配置位置或 Python 注入变量。"""
    home = work / '.home'
    home.mkdir()
    env = {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'HOME': str(home),
           'TMPDIR': str(work), 'TMP': str(work), 'TEMP': str(work),
           'LANG': 'en_US.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1',
           'FRCLYO_HOME': str(work / '.data'),
           'FRCLYO_CONFIG': str(work / '.data/配置/config.json')}
    env.update({key: str(framework if framework is not None else work / '输入/框架')
                for key in ROOT_VARIABLES})
    return env


def stop_group(process):
    """复用监督器的身份复核和多轮收尾，覆盖已采样的独立会话后代。"""
    return execution.reap_group(process)


@contextlib.contextmanager
def interruption_handlers():
    """让 CLI 的退出信号经过 finally；临时处理器不泄漏到调用者。"""
    previous = {}
    deferred = [False, None]
    def interrupt(signum, frame):
        if deferred[0]:
            deferred[1] = deferred[1] or signum
            return
        if signum == signal.SIGINT:
            raise KeyboardInterrupt
        raise SystemExit(128 + signum)
    def defer():
        deferred[0] = True
    try:
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                previous[sig] = signal.signal(sig, interrupt)
        yield defer
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        # 收尾期间收到的第一次中断延后抛出；已有中断则保留原退出原因。
        if deferred[1] and sys.exc_info()[0] is None:
            deferred[0] = False
            interrupt(deferred[1], None)


def attempt(test, cwd, env, profile, log, seconds):
    """输出直接落盘；仅显式无沙箱模式传入 profile=None，不自动降级。"""
    command = [sys.executable, '-B', str(test)]
    if profile is not None:
        command = [SANDBOX, '-p', profile, *command]
    result = {'cwd': str(cwd), 'log': log.name, 'exit_code': None,
              'timed_out': False, 'launch_error': '', 'cleanup_error': ''}
    with log.open('xb') as stream, interruption_handlers() as defer_interrupts:
        try:
            process = subprocess.Popen(command, cwd=cwd, env=env,
                                       stdin=subprocess.DEVNULL, stdout=stream,
                                       stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as exc:
            result['launch_error'] = ('无法启动 macOS 沙箱：' if profile is not None
                                      else '无法启动测试：') + type(exc).__name__
            stream.write((result['launch_error'] + '\n').encode('utf-8'))
            return result
        try:
            descendants = process._frclyo_descendants = execution.Descendants(process)
            deadline = time.monotonic() + max(0, seconds)
            # 在父进程仍活着时持续记录关系；只在收尾时查询会漏掉已重挂的后代。
            interval = 0.05
            while True:
                descendants.sample()
                remaining = max(0, deadline - time.monotonic())
                try:
                    process.wait(timeout=min(interval, remaining))
                    break
                except subprocess.TimeoutExpired:
                    if time.monotonic() >= deadline:
                        result['timed_out'] = True
                        break
                interval = min(1.0, interval * 2)
        finally:
            # 收尾有界；重复中断不能在清理到一半时再次跳出。
            defer_interrupts()
            if not stop_group(process):
                result['cleanup_error'] = '未能确认测试进程及后代全部回收'
                stream.write((result['cleanup_error'] + '\n').encode('utf-8'))
                print(result['cleanup_error'], file=sys.stderr)
        result['exit_code'] = process.returncode
    return result


def inspect_output(log):
    """流式提取诊断和重试线索；仅精确沙箱嵌套失败诊断作为环境例外。"""
    first = error = ''
    missing = False
    nested_sandbox = False
    traceback_seen = assertion = False
    with log.open(encoding='utf-8', errors='replace') as stream:
        for raw in stream:
            line = raw.strip()
            # unittest 常把捕获的 stderr 接在 AssertionError: … : 后面。
            nested_sandbox = nested_sandbox or bool(re.search(
                r'(?:^|:\s+)(?:sandbox-exec:\s*)?sandbox_apply:\s*Operation not permitted$', line))
            missing = missing or bool(re.search(r'No module named [\'\"]frclyo(?:\.[^\'\"]+)?[\'\"]', line))
            traceback_seen = traceback_seen or line.startswith('Traceback (most recent call last):')
            assertion = assertion or line.startswith('AssertionError')
            # 仅保留第一条诊断供核查；即使出现 ERROR 也不覆盖退出码。
            is_error = (line.startswith(('ERROR:', 'sandbox-exec:')) or
                        bool(re.search(r'\berrors=[1-9]\d*', line)) or
                        bool(re.match(r'(?:[\w.]+(?:Error|Exception)|KeyboardInterrupt|SystemExit)(?::|$)', line))
                        and not line.startswith('AssertionError'))
            is_failure = line.startswith(('FAIL:', 'AssertionError'))
            if not first and (is_error or is_failure):
                first = line
            if is_error and not error:
                error = line
    if traceback_seen and not assertion and not error:
        error = 'Python 异常回溯；请查看完整输出'
    return {'first': first, 'error': error, 'missing_frclyo': missing,
            'nested_sandbox': nested_sandbox}


def verdict(result, output):
    """已完成的脚本只按退出码判定；执行器错误不伪装成脚本结果。"""
    if result['timed_out']:
        return '测试本身出错', '超时（单个测试总限时 300 秒，含兼容重试）'
    if result['launch_error']:
        return '测试本身出错', result['launch_error']
    if result.get('cleanup_error'):
        return '测试本身出错', result['cleanup_error']
    code = result['exit_code']
    if code is not None and code != 0 and output.get('nested_sandbox'):
        return ('环境受限：沙箱不能嵌套',
                '可显式加 --不套沙箱（--no-sandbox）重跑；测试代码将直接在本机运行')
    if code not in (0, 1):
        return '测试本身出错', output['first'] or '异常退出；请查看完整输出'
    return ('复现' if code == 1 else '没复现'), output['first']


def log_name(name, index):
    """短名沿用旧格式；长名按 UTF-8 字节截短并加摘要，后缀计入上限。"""
    suffix = '.attempt-%d.log' % index
    encoded = name.encode('utf-8')
    if len(encoded) + len(suffix) <= 240:
        return name + suffix
    digest = hashlib.sha256(encoded).hexdigest()
    budget = 240 - len(suffix) - len(digest) - 1
    prefix = encoded[:budget].decode('utf-8', errors='ignore')
    return prefix + '-' + digest + suffix


def run_test(test, work, env, profile, logs):
    """合法退出 0/1 不重跑；非协议退出且缺模块时兼容重试，共用 300 秒。"""
    deadline = time.monotonic() + TIMEOUT
    attempts = []
    for index, cwd in enumerate((work, work / '交付'), 1):
        result = attempt(test, cwd, env, profile,
                         logs / log_name(test.name, index),
                         deadline - time.monotonic())
        output = inspect_output(logs / result['log'])
        attempts.append(result)
        if (not output['missing_frclyo'] or result['exit_code'] in (0, 1) or
                result['timed_out'] or result['launch_error'] or result.get('cleanup_error') or
                output.get('nested_sandbox') or
                time.monotonic() >= deadline):
            break
    decision, first = verdict(result, output)
    return {'test': test.name, 'exit_code': result['exit_code'], 'verdict': decision,
            'first_failure': first, 'attempts': attempts}


def reproduce(root, task, round_id, selected=None, framework='输入', no_sandbox=False):
    """只读原轮次，复制到新的临时目录；每次运行的日志独立保留。"""
    run = ordinary_path(root, Path('交付') / task / round_id)
    if not run.is_dir():
        raise ReproError('找不到指定轮次目录')
    if not no_sandbox and (sys.platform != 'darwin' or not Path(SANDBOX).is_file()):
        raise ReproError('需要 macOS /usr/bin/sandbox-exec；未执行任何测试')
    if framework in ('输入', '交付'):
        source = ordinary_path(run, Path(framework) / '框架')
    else:
        source = Path(framework).expanduser().absolute()
    if source.is_symlink() or not source.is_dir():
        raise ReproError('所选框架必须是普通目录：' + str(source))
    fixed = framework != '输入'
    # 显式使用已解析的临时父目录；不在权限失败后换目录重试。
    temporary_parent = os.environ['TMPDIR'] if 'TMPDIR' in os.environ else tempfile.gettempdir()
    if not temporary_parent:
        raise ReproError('TMPDIR 不能为空')
    parent = Path(temporary_parent).resolve()
    if parent.is_relative_to(run):
        raise ReproError('TMPDIR 不能位于原轮次内')
    if parent.is_relative_to(Path.home().resolve()):
        raise ReproError('TMPDIR 位于用户家目录内；需要家目录外的宿主临时目录，未运行测试')
    with tempfile.TemporaryDirectory(prefix='frclyo-repro-', dir=parent) as temporary:
        work = Path(temporary).resolve()
        if work.is_relative_to(source.resolve()):
            raise ReproError('临时副本不能位于所选框架内')
        for part in ('输入', '交付'):
            copy_tree(run / part, work / part)
        if framework in ('输入', '交付'):
            copied_framework = work / framework / '框架'
        else:
            copied_framework = work / '指定框架'
            copy_tree(source, copied_framework)
        tests = sorted((work / '交付/复现测试').glob('test_*.py'))
        tests = [test for test in tests if test.is_file()]
        if selected:
            tests = [test for test in tests if test.name == selected]
        if not tests:
            raise ReproError('没有找到匹配的 交付/复现测试/test_*.py')
        # 日志不放进可写沙箱，重复运行也不会覆盖以前的证据。
        log_parent = ordinary_path(root, Path('运行时/复现') / (task + '-' + round_id))
        log_parent.mkdir(parents=True, exist_ok=True)
        logs = Path(tempfile.mkdtemp(prefix='run-', dir=log_parent)).resolve()
        profile = None if no_sandbox else sandbox_profile(work)
        if profile is not None:
            (logs / 'sandbox.sb').write_text(profile, encoding='utf-8')
        (logs / 'run.json').write_text(json.dumps({
            'framework': framework, 'framework_source': str(source),
            'framework_copy': str(copied_framework), 'sandbox': not no_sandbox,
            'expectation': '修复版应全部没复现' if fixed else '原版复现核对',
        }, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        env = environment(work, copied_framework)
        rows = []
        print('框架：%s（%s）' % (source, '修复版' if fixed else '原版'), flush=True)
        if no_sandbox:
            print('提醒：测试代码将直接在本机运行（--不套沙箱 / --no-sandbox）', flush=True)
        print('日志：' + str(logs), flush=True)
        for test in tests:
            row = run_test(test, work, env, profile, logs)
            if fixed and row['verdict'] == '复现':
                row['verdict'] = '仍复现'
            rows.append(row)
            # 每完成一项就保存摘要；后续中断时，已有结果仍然可检查。
            (logs / 'results.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            first = ' '.join(row['first_failure'].split())[:200] or '—'
            name = json.dumps(test.name, ensure_ascii=False)
            print('%s\texit=%s\t%s\t%s' % (name, row['exit_code'], row['verdict'], first), flush=True)
        reproduced = '仍复现' if fixed else '复现'
        counts = {name: sum(row['verdict'] == name for row in rows)
                  for name in (reproduced, '没复现', '测试本身出错', '环境受限：沙箱不能嵌套')}
        print('汇总（%s）：%s %d；没复现 %d；测试本身出错 %d；环境受限 %d' % (
            '修复版应全部没复现' if fixed else '原版复现核对', reproduced,
            counts[reproduced], counts['没复现'], counts['测试本身出错'],
            counts['环境受限：沙箱不能嵌套']), flush=True)
        # 命令退出码描述收集是否成功；具体是否复现看每个脚本的结果。
        return int(any(row['verdict'] in ('测试本身出错', '环境受限：沙箱不能嵌套') for row in rows))


def main(args=None):
    """帮助和用法错误无需访问配置；运行仅通过公共接口定位数据根。"""
    parser = argparse.ArgumentParser(prog='aw repro', description='在宿主 macOS 沙箱中复现指定轮次的测试（中文：复现）')
    parser.add_argument('task', type=component, metavar='任务')
    parser.add_argument('round', type=component, metavar='轮')
    parser.add_argument('--测试', '--test', dest='selected', type=test_name, metavar='名字')
    parser.add_argument('--框架', '--framework', default='输入', dest='framework',
                        metavar='输入|交付|路径', help='所测框架树，默认输入；交付或路径按修复版汇总')
    parser.add_argument('--不套沙箱', '--no-sandbox', action='store_true', dest='no_sandbox',
                        help='显式直接在本机运行测试；仍复制、限时、收尾并保存日志')
    options = parser.parse_args(args)
    try:
        return reproduce(config.root_dir(), options.task, options.round, options.selected,
                         options.framework, options.no_sandbox)
    except ReproError as exc:
        print('复现未完成：' + str(exc), file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print('复现未完成：文件或数据根不可用（' + type(exc).__name__ + '）', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
