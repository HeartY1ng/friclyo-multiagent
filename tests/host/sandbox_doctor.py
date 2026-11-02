#!/usr/bin/env python3
"""宿主零模型诊断：独立启动每项，使用两种工人的同源真实权限，不降级。"""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

import isolation_check as check
from frclyo.isolation import seatbelt, temp_environment, codex_command_environment, USER_DATA_ROOTS
from frclyo.execution import exclusive_directory
import offline_opencode as offline
import production_opencode as production

# 放进 -c 参数，不让沙箱读取框架源码。失败只输出 errno，不输出哨兵内容。
CASE = r'''
import errno, json, os, pathlib, sys
name, filename, run, worker = sys.argv[1:]
blocked = name == '根外哨兵' and worker == 'opencode'
row = {'ok': False, 'errno': None}
if name == '根外哨兵' and worker == 'codex':
    row['note'] = '按方案 A，Codex 不做根外禁读'
try:
    if name == 'getcwd':
        row['ok'] = pathlib.Path(os.getcwd()).resolve() == pathlib.Path(run).resolve()
    elif name == '/dev/null':
        with open(filename, 'wb') as f: row['ok'] = f.write(b'doctor') == 6
    else:
        with open(filename, 'rb') as f: value = f.read(1)
        row['ok'] = bool(value) and not blocked
except OSError as exc:
    row.update(errno=exc.errno, error=type(exc).__name__)
    row['ok'] = blocked and exc.errno in (errno.EPERM, errno.EACCES)
print('FRCLYO_DOCTOR_RESULT=' + json.dumps(row), flush=True)
sys.exit(0 if row['ok'] else 1)
'''


SWIFT_CASE = r'''
import json, os, pathlib, subprocess, sys
run, expected = map(pathlib.Path, sys.argv[1:3])
root_cwd = sys.argv[3:] == ['root-cwd']
private_cwd = sys.argv[3:] == ['private-cwd']
checks = {name: os.environ.get(name) == str(expected) for name in ('TMPDIR', 'TMP', 'TEMP')}
checks['TMPPREFIX'] = os.environ.get('TMPPREFIX') == str(expected / 'zsh')
checks['ascii'] = str(expected).isascii() and expected.resolve() == expected
for name, suffix in (('CLANG_MODULE_CACHE_PATH', 'clang-module-cache'),
                     ('SWIFT_MODULECACHE_PATH', 'swift-module-cache')):
    checks[name] = os.environ.get(name) == str(expected / suffix)
work = expected if private_cwd else run
source, program = work / 'doctor.swift', work / 'doctor-swift'
row = {'ok': False, 'checks': checks}
try:
    source.write_text('print("ok")\n')
    if not all(checks.values()):
        raise ValueError('invalid private temporary environment')
    options = {'cwd': str(expected)} if private_cwd else ({'cwd': '/'} if root_cwd else {})
    row['compile_cwd'] = str(expected) if private_cwd else ('/' if root_cwd else str(run))
    compiled = subprocess.run(['swiftc', str(source), '-o', str(program)],
                              capture_output=True, text=True, timeout=60, **options)
    row['stderr_tail'] = compiled.stderr.splitlines()[-8:]
    row['warning'] = bool(compiled.stderr.strip())
    row['compile_exit'] = compiled.returncode
    if compiled.returncode == 0:
        executed = subprocess.run([str(program)], capture_output=True, text=True, timeout=10)
        row.update(run_exit=executed.returncode, stdout=executed.stdout)
        row['stderr_tail'] = (row['stderr_tail'] + executed.stderr.splitlines())[-8:]
        row['warning'] = row['warning'] or bool(executed.stderr.strip())
        row['ok'] = executed.returncode == 0 and executed.stdout.strip() == 'ok'
        if private_cwd and row['ok']:
            import shutil
            for path in (source, program):
                target = run / path.name
                shutil.copy2(path, target)
                assert target.read_bytes() == path.read_bytes()
            row['copy_back'] = True
except (OSError, ValueError, AssertionError, subprocess.SubprocessError) as exc:
    row.update(error=type(exc).__name__, errno=getattr(exc, 'errno', None))
    if isinstance(exc, subprocess.TimeoutExpired):
        stderr = exc.stderr or b''
        if isinstance(stderr, bytes):
            stderr = stderr.decode('utf-8', errors='replace')
        row['stderr_tail'] = stderr.splitlines()[-8:]
print('FRCLYO_DOCTOR_RESULT=' + json.dumps(row), flush=True)
sys.exit(0 if row['ok'] else 1)
'''


TOOLS_CASE = r'''
import json, os, pathlib, shlex, shutil, subprocess, sys, tempfile
name, directory, temporary = sys.argv[1:4]
worker = sys.argv[4] if len(sys.argv) > 4 else 'opencode'
run, expected = pathlib.Path(directory), pathlib.Path(temporary)
work = expected if worker == 'codex' else run
repo = work / 'doctor-git'
row = {'ok': False, 'errno': None, 'stderr_tail': [], 'warning': False, 'commands': []}
def execute(command, **kwargs):
    result = subprocess.run(command, capture_output=True, text=True, timeout=65, **kwargs)
    row['commands'].append({'exit': result.returncode, 'stderr_tail': result.stderr.splitlines()[-8:]})
    row['stderr_tail'] = (row['stderr_tail'] + result.stderr.splitlines())[-8:]
    row['warning'] = row['warning'] or bool(result.stderr.strip())
    if result.returncode:
        raise RuntimeError('tool exit=' + str(result.returncode))
    return result.stdout
def git(*args):
    # 显式仓库路径；前序失败时绝不向上发现宿主仓库。
    return execute(['git', '--git-dir=' + str(repo / '.git'), '--work-tree=' + str(repo), *args], cwd=repo)
try:
    if name in ('zsh-heredoc', 'bash-heredoc'):
        script = shlex.quote(sys.executable) + " -I - <<'PY'\nprint('HEREDOC_OK')\nPY\n"
        output = execute(['/bin/' + name.split('-')[0], '-lc', script])
        assert output.strip() == 'HEREDOC_OK', output
    elif name in ('mktemp', 'mktemp-explicit'):
        command = ['mktemp'] if name == 'mktemp' else ['mktemp', '-p', str(expected)]
        row['invocation'] = command
        path = pathlib.Path(execute(command).strip())
        assert path.parent.resolve() == expected.resolve(), str(path)
        assert path.is_file()
        path.unlink()
    elif name == 'sort-large':
        source, target = run / 'doctor-sort-input', run / 'doctor-sort-output'
        count, padding = 250000, 'x' * 64
        with source.open('w') as output:
            for number in range(count - 1, -1, -1):
                output.write('%08d %s\n' % (number, padding))
        # 约 18 MiB 输入，1 MiB 内存上限，迫使常用 sort 使用临时文件；不传 -T 掩盖环境问题。
        execute(['sort', '-S', '1M', '-o', str(target), str(source)], env=dict(os.environ, LC_ALL='C'))
        with target.open() as result:
            for number in range(count):
                assert result.readline() == '%08d %s\n' % (number, padding)
            assert result.read() == ''
    elif name == 'git-init':
        execute(['git', 'init', str(repo)], cwd=work)
        git('config', '--local', 'user.name', 'Sandbox Doctor')
        git('config', '--local', 'user.email', 'doctor@example.invalid')
        git('config', '--local', 'commit.gpgsign', 'false')
        (repo / 'sample.txt').write_text('before\n')
    elif name == 'git-add':
        git('add', 'sample.txt')
        assert git('diff', '--cached', '--name-only').strip() == 'sample.txt'
    elif name == 'git-commit':
        git('commit', '-m', 'sandbox doctor local fixture')
        assert git('log', '-1', '--format=%s').strip() == 'sandbox doctor local fixture'
    elif name == 'git-status':
        assert git('status', '--porcelain') == ''
    elif name == 'git-diff':
        (repo / 'sample.txt').write_text('after\n')
        output = git('diff', '--', 'sample.txt')
        assert '-before' in output and '+after' in output, output
        (repo / 'change.patch').write_text(output)
    elif name == 'copy-back':
        target = run / 'doctor-copy-back'
        target.mkdir()
        for filename in ('sample.txt', 'change.patch'):
            source = repo / filename
            shutil.copy2(source, target / filename)
            assert (target / filename).read_bytes() == source.read_bytes()
        assert (target / 'sample.txt').read_text() == 'after\n'
        patch = (target / 'change.patch').read_text()
        assert '-before' in patch and '+after' in patch
        row['destination'] = str(target)
    elif name == 'python-tempfile':
        code = 'import os,tempfile; fd,path=tempfile.mkstemp(); os.close(fd); print(path)'
        path = pathlib.Path(execute(['python3', '-c', code]).strip())
        assert path.parent.resolve() == expected.resolve(), str(path)
        assert path.is_file()
        path.unlink()
    elif name == 'node-tmpdir':
        path = execute(['node', '-e', 'console.log(require("os").tmpdir())']).strip()
        assert pathlib.Path(path).resolve() == expected.resolve(), path
    elif name == 'private-home':
        home = pathlib.Path(os.environ['HOME'])
        assert home.is_dir() and os.environ['ZDOTDIR'] == str(home)
        assert os.environ['TMPPREFIX'] == str(expected / 'zsh')
        for key, suffix in (('NPM_CONFIG_CACHE', 'npm-cache'), ('PIP_CACHE_DIR', 'pip-cache')):
            assert os.environ[key] == str(expected / suffix)
        sentinel = home / 'doctor-home.txt'
        sentinel.write_text('PRIVATE_HOME_OK')
        assert sentinel.read_text() == 'PRIVATE_HOME_OK'
        # 不指定 --global 路径：验证 git 自然选择私有 HOME。
        execute(['git', 'config', '--global', 'doctor.sentinel', 'private'], cwd=work)
        assert execute(['git', 'config', '--global', '--get', 'doctor.sentinel'], cwd=work).strip() == 'private'
        assert (home / '.gitconfig').is_file()
    else:
        raise ValueError('unknown tool case')
    row['ok'] = True
except (OSError, ValueError, RuntimeError, AssertionError, subprocess.SubprocessError) as exc:
    row.update(error=type(exc).__name__, errno=getattr(exc, 'errno', None), message=str(exc))
    if isinstance(exc, subprocess.TimeoutExpired):
        stderr = exc.stderr or b''
        if isinstance(stderr, bytes):
            stderr = stderr.decode('utf-8', errors='replace')
        row['stderr_tail'] = stderr.splitlines()[-8:]
print('FRCLYO_DOCTOR_RESULT=' + json.dumps(row), flush=True)
sys.exit(0 if row['ok'] else 1)
'''


def common_tool_cases(run, manifest, worker='codex'):
    names = ['private-home', 'zsh-heredoc', 'bash-heredoc', 'mktemp', 'sort-large',
             'git-init', 'git-add', 'git-commit', 'git-status', 'git-diff', 'copy-back', 'python-tempfile']
    if shutil.which('node'):
        names.append('node-tmpdir')
    # macOS OpenCode 下无参数 mktemp 的系统临时目录选择已被证据确认受限。
    # 直接检查首次调用推荐写法，不在失败后重试，也不开放公共临时写口。
    return {name: [sys.executable, '-I', '-c', TOOLS_CASE,
                  'mktemp-explicit' if name == 'mktemp' and worker == 'opencode' else name,
                  str(run), manifest['temporary'], worker]
            for name in names}


ANCESTOR_CASE = r'''
import errno, json, os, pathlib, sys
run = pathlib.Path(sys.argv[1])
expectations = json.loads(sys.argv[2])
rows = []
for path in (run, *run.parents):
    expected = expectations[str(path)]
    item = {'path': str(path), 'expected': expected}
    entered = False
    for name in ('stat', 'chdir', 'getcwd'):
        if name == 'getcwd' and not entered:
            item[name] = {'ok': None, 'errno': None, 'reason': 'chdir 未成功，未运行 getcwd'}
            continue
        try:
            if name == 'stat':
                os.stat(path)
            elif name == 'chdir':
                os.chdir(path)
                entered = True
            else:
                os.getcwd()
            item[name] = {'ok': True, 'errno': None}
        except OSError as exc:
            item[name] = {'ok': False, 'errno': exc.errno}
    # 每级从本轮独立开始，不能把上一项失败后的 cwd 当作本项的结果。
    os.chdir(run)
    def matches(name):
        actual, policy = item[name], expected[name]
        if actual['ok'] is None:
            return name == 'getcwd' and item['chdir']['ok'] is False and matches('chdir')
        if policy == 'allow':
            return actual['ok'] is True
        denied = actual['ok'] is False and actual['errno'] in (errno.EACCES, errno.EPERM)
        return denied if policy == 'deny' else (actual['ok'] is True or denied)
    item['matches_policy'] = all(matches(name) for name in ('stat', 'chdir', 'getcwd'))
    rows.append(item)
ok = all(row['matches_policy'] for row in rows)
limitations = [row['path'] for row in rows if any(row[name]['ok'] is not True for name in ('stat', 'chdir', 'getcwd'))]
print('FRCLYO_DOCTOR_RESULT=' + json.dumps({'ok': ok, 'ancestors': rows, 'warning': bool(limitations),
    'limited_paths': limitations,
    'note': '逐项核对权限预期；受保护祖先的拒绝不代表工具失败，实际工具另测。'}), flush=True)
sys.exit(0 if ok else 1)
'''


def ancestor_expectations(run, manifest, worker):
    root = Path(manifest.get('root', str(run.parent)))
    home = Path(manifest.get('home', str(root.parent)))
    protected = [Path(os.path.realpath(p)) for p in USER_DATA_ROOTS] + [root, home]
    result = {}
    for path in (run, *run.parents):
        inside_root = path != run and path.is_relative_to(root)
        content_denied = path != run and any(path.is_relative_to(p) for p in protected)
        if worker == 'codex':
            traversal = 'allow-or-permission-denied' if inside_root else 'allow'
            result[str(path)] = dict(stat='deny' if inside_root else 'allow',
                                     chdir=traversal, getcwd=traversal)
        else:
            # 元数据必须可用；禁读目录里的 chdir/getcwd 并非 F01 的工具兼容承诺。
            # 允许明确权限拒绝，其他 errno 必须失败；目录内容禁读由完整探针单独核对。
            traversal = 'allow-or-permission-denied' if content_denied else 'allow'
            result[str(path)] = dict(stat='allow', chdir=traversal, getcwd=traversal)
    return result


def cases(run, manifest, worker='codex'):
    # 先跑不依赖 Python 的项；每项单独进沙箱，不因上一项失败跳过。
    result = {
        'bash': ['bash', '--noprofile', '--norc', '-c', 'printf BASH_OK'],
        'pwd': ['/bin/pwd', '-P'],
        'dev-null-shell': ['/bin/bash', '--noprofile', '--norc', '-c', 'printf doctor > /dev/null'],
        'dev-zero-native': ['/bin/dd', 'if=/dev/zero', 'of=/dev/null', 'bs=1', 'count=1'],
        'dev-urandom-native': ['/bin/dd', 'if=/dev/urandom', 'of=/dev/null', 'bs=1', 'count=1'],
        'python3': ['python3', '-I', '-c', 'print("PYTHON_PATH_OK")'],
        'git': ['git', '--version'],
    }
    for name, filename in (
        ('getcwd', str(run)), ('/dev/null', '/dev/null'), ('/dev/zero', '/dev/zero'),
        ('/dev/urandom', '/dev/urandom'), ('本轮文件', str(run / 'doctor-local.txt')),
        ('根外哨兵', manifest['targets']['根外用户文档']),
    ):
        result[name] = [sys.executable, '-I', '-c', CASE, name, filename, str(run), worker]
    if manifest.get('temporary'):
        result.update(common_tool_cases(run, manifest, worker))
        result['Swift编译运行和ASCII临时目录'] = [sys.executable, '-I', '-c', SWIFT_CASE,
                                            str(run), manifest['temporary']] + (
                                                ['private-cwd'] if worker == 'codex' else [])
        if worker == 'codex':
            temporary = Path(manifest['temporary'])
            result['私有目录上层可遍历'] = [sys.executable, '-I', '-c', ANCESTOR_CASE, str(temporary),
                json.dumps({str(path): dict(stat='allow', chdir='allow', getcwd='allow')
                            for path in (temporary, *temporary.parents)})]
    result['逐级上层目录stat和chdir'] = [sys.executable, '-I', '-c', ANCESTOR_CASE, str(run),
                                     json.dumps(ancestor_expectations(run, manifest, worker))]
    # 最后保留全套原拒绝项，不能用诊断子集代替隔离回归。
    result['完整隔离探针'] = [sys.executable, '-I', str(run / 'probe.py'), str(run / 'manifest.json')]
    return result


def case_result(log, rc, structured=False):
    text = log.read_text(encoding='utf-8', errors='replace')
    row = None
    for line in text.splitlines():
        if line.startswith('FRCLYO_DOCTOR_RESULT='):
            try:
                row = json.loads(line.partition('=')[2])
            except ValueError:
                pass
    exits = [line.partition('=')[2] for line in text.splitlines()
             if line.startswith('FRCLYO_KERNEL_COMMAND_EXIT=')]
    command_rc = int(exits[-1]) if exits and exits[-1].lstrip('-').isdigit() else None
    ok = (rc == 0 and command_rc == 0 and
          (isinstance(row, dict) and row.get('ok') is True if structured else True))
    diagnosis = check.kernel_diagnosis(log, '已运行' if ok else '探针未完成', rc)
    # 原诊断在 stderr 为空时回填日志尾；回填内容不能当成工具警告。
    child_stderr = text.partition('--- child stderr ---')[2].strip().splitlines()[-8:]
    stderr_tail = ((row.get('stderr_tail') or []) if isinstance(row, dict) else []) + child_stderr
    warning = bool(stderr_tail) or (isinstance(row, dict) and row.get('warning') is True)
    return dict(ok=ok, errno=row.get('errno') if isinstance(row, dict) else None,
                status=('通过（有警告）' if warning else '通过') if ok else '失败/未运行',
                launcher_exit=rc, command_exit=command_rc, detail=row,
                stderr_tail=stderr_tail[-8:] if ok else (stderr_tail or diagnosis['stderr末尾']),
                launcher_tail=diagnosis['启动器末尾'],
                reason=diagnosis['原因'], log=str(log))


def private_environment(base, home, root, run, runtime):
    for name in ('config', 'data', 'state', 'cache', 'tmp', 'home'):
        exclusive_directory(runtime / name)
    env = {key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL') if key in os.environ}
    env.update(HOME=str(runtime / 'home'), ZDOTDIR=str(runtime / 'home'),
               CODEX_HOME=str(base / 'kernel-codex-home'),
               XDG_CONFIG_HOME=str(runtime / 'config'), XDG_DATA_HOME=str(runtime / 'data'),
               XDG_STATE_HOME=str(runtime / 'state'), **temp_environment(root, run))
    return env


def opencode_matrix(base, extended=False):
    if not str(base.resolve()).isascii():
        return {'available': False, 'reason': '输出物理路径含非 ASCII；不能建立全英文对照',
                'rows': {}, 'ok': False}
    binary = shutil.which('opencode')
    if not binary:
        return {'available': False, 'reason': 'opencode executable missing', 'rows': {}, 'ok': False}
    labels = production.VARIANTS if extended else ('baseline', 'working-directory')
    result = {'available': True, 'rows': {}, 'pairs': {}, 'ok': False}
    for label in labels:
        pair = {}
        for layout in ('zh', 'ascii'):
            area = base / 'opencode-matrix' / label / layout
            print('生产入口诊断：%s / %s' % (label, layout), flush=True)
            try:
                row = production.run_case(area, binary, layout, label)
            except (OSError, ValueError, RuntimeError) as exc:
                row = dict(ok=False, launcher_exit=None, command_exit=None, requests=0,
                           response=False, old_failure_reproduced=False,
                           error=type(exc).__name__, errno=getattr(exc, 'errno', None),
                           tails={'stderr': ['诊断夹具/启动器异常；不能认作 OpenCode 失败复现']})
            pair[layout] = row
            result['rows'][label + '/' + layout] = row
            print('%s/%s: launcher=%s worker=%s requests=%s response=%s ok=%s same-error=%s' % (
                label, layout, row['launcher_exit'], row['command_exit'], row['requests'],
                row['response'], row['ok'], row['old_failure_reproduced']), flush=True)
            for line in row['tails']['stderr']:
                print(line, flush=True)
            (base / 'opencode-matrix.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
        zh, ascii_ok = pair['zh']['ok'], pair['ascii']['ok']
        result['pairs'][label] = ('both-success' if zh and ascii_ok else 'both-failure'
            if not zh and not ascii_ok else 'chinese-only-failure' if not zh else 'ascii-only-failure')
    result['ok'] = result['pairs']['working-directory'] == 'both-success'
    result['reproduced'] = any(result['rows']['baseline/' + layout]['old_failure_reproduced']
                               for layout in ('zh', 'ascii'))
    result['cwd_evidence'] = {layout: (result['rows']['baseline/' + layout]['old_failure_reproduced'] and
                                      result['rows']['working-directory/' + layout]['ok'])
                              for layout in ('zh', 'ascii')}
    result['interpretation'] = ('旧入口条件出现同一 Unexpected 错误且无上游请求；请按同语言组比较单因素实验。'
        if result['reproduced'] else '未复现同一 Unexpected 错误；其他失败或两组成功均不能证明原故障已修复。')
    header = '| 单项实验/路径 | 启动器退出 | 工人退出 | 假上游请求 | 固定响应 | 成功 | 同一 Unexpected |\n|---|---:|---:|---:|---|---|---|\n'
    table = header + ''.join('| %s | %s | %s | %s | %s | %s | %s |\n' %
        (name, row['launcher_exit'], row['command_exit'], row['requests'], row['response'], row['ok'], row['old_failure_reproduced'])
        for name, row in result['rows'].items())
    (base / 'opencode-matrix.md').write_text(table + '\n' + result['interpretation'] + '\n')
    (base / 'opencode-matrix.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(table + result['interpretation'], flush=True)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='新的证据目录，默认在 /private/tmp；已存在则拒绝')
    parser.add_argument('--opencode-matrix', action='store_true',
                        help='追加独立逐项实验；只放开合成夹具，不开放真实用户数据')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--opencode-only', action='store_true', help='仅生产入口对照，先于内核工具诊断运行')
    mode.add_argument('--kernel-only', action='store_true', help='仅确定性内核工具与隔离检查，不重复 OpenCode 对照')
    args = parser.parse_args(argv)
    if sys.platform != 'darwin' or not shutil.which('sandbox-exec'):
        print('未运行：需要 macOS sandbox-exec；未验证隔离')
        return 1
    if args.output:
        base = args.output.expanduser().absolute()
        if not args.kernel_only and not str(base).isascii():
            parser.error('--output 必须为全 ASCII 物理路径，才能建立全英文路径对照')
        base.mkdir(parents=True, exist_ok=False)
        base = base.resolve()
    else:
        base = Path(tempfile.mkdtemp(prefix='frclyo-sandbox-doctor-', dir='/private/tmp')).resolve()
    print('证据目录：' + str(base), flush=True)
    summary = {'workers': {}, 'mode': 'kernel-only' if args.kernel_only else
               'opencode-only' if args.opencode_only else 'production-and-kernel'}
    if not args.kernel_only:
        summary['opencode_matrix'] = opencode_matrix(base, args.opencode_matrix)
    (base / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.opencode_only:
        print('FIXED_PAIR_%s；同一历史错误复现=%s；证据目录：%s' % (
            'OK' if summary['opencode_matrix']['ok'] else 'FAIL',
            summary['opencode_matrix'].get('reproduced', False), base), flush=True)
        return 0 if summary['opencode_matrix']['ok'] else 1
    # 公共临时哨兵必须在 /private/tmp，即使证据目录选在其他挂载点。
    public = Path(tempfile.mkdtemp(prefix='frclyo-doctor-public-', dir='/private/tmp')).resolve()
    system_temp = Path(tempfile.mkdtemp(prefix='frclyo-doctor-system-')).resolve()
    print('证据目录：' + str(base), flush=True)
    codex = shutil.which('codex')
    summary.update(public_sentinels=str(public), system_sentinels=str(system_temp))
    for worker in ('codex', 'opencode'):
        home, root, run, runtime, control = check.build_area(base, worker, system_temp)
        manifest_path = run / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        sentinel = public / (worker + '-sentinel')
        sentinel.write_text('ISOLATION_FAKE_SENTINEL')
        manifest['targets']['固定公共临时文件'] = str(sentinel)
        if worker == 'codex':
            manifest['readable_names'].append('固定公共临时文件')
            print(check.PLAN_A_NOTE, flush=True)
        manifest['write_targets']['固定公共临时文件'] = str(sentinel)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False))
        (run / 'doctor-local.txt').write_text('LOCAL_DOCTOR_SENTINEL')
        env = private_environment(base / worker, home, root, run, runtime)
        # CLI 自身仍使用宿主 HOME；与生产 exec 一样，仅沙箱内命令换 HOME。
        # 避免 CLI 把权限中的 ~/.config 等解析到命令的私有 HOME。
        if worker == 'codex':
            env.update(codex_command_environment(root, run, home))
            env['HOME'] = str(home)
        sensitive = [env['CODEX_HOME'], str(root / '配置/config.json')]
        if worker == 'opencode':
            profile = control / 'sandbox.sb'
            profile.write_text(seatbelt(str(home), str(root), str(run), str(runtime), str(control),
                                       9, sensitive, [str(root), str(root / '运行时/opencode')],
                                       manifest['allowed_read']))
        before = {filename: Path(filename).read_bytes() if Path(filename).exists() else None
                  for filename in manifest['write_targets'].values()}
        results = {}
        summary['workers'][worker] = results
        summary.setdefault('temporary_directories', {})[worker] = manifest['temporary']
        if not shutil.which('node'):
            print(worker + ' node-tmpdir: 未运行（未安装 node；可选项）', flush=True)
        for index, (name, command) in enumerate(cases(run, manifest, worker).items()):
            log = base / worker / ('%02d-kernel.log' % index)
            wrapped = check.kernel_command(command, run)
            if worker == 'codex':
                # sandbox 直接运行命令，不保证应用 exec 的 shell_environment_policy。
                wrapped = ['/usr/bin/env'] + [key + '=' + value for key, value in
                    codex_command_environment(root, run, home).items()] + wrapped
                argv = check.codex_probe_argv(codex, root, run, sensitive, runtime, wrapped) if codex else []
                if argv:
                    (control / 'permission.txt').write_text(argv[5])
            else:
                argv = ['sandbox-exec', '-f', str(profile)] + wrapped
            rc = check.execute_kernel(argv, env, run, log, 90, executor=offline.execute)
            result = case_result(log, rc, structured=command[0] == sys.executable and '-c' in command)
            if name == '完整隔离探针':
                names = check.probe_names(manifest)
                state, checks = check.kernel_checks(log, names, rc, check.readable_names(manifest))
                # 同原探针，宿主确认所有写目标未改变。
                for filename in manifest['write_targets'].values():
                    path = Path(filename)
                    after = path.read_bytes() if path.exists() else None
                    if after != before[filename]:
                        result['ok'] = False
                result['ok'] = result['ok'] and state == '已运行' and all(checks.values())
                result.update(state=state, checks=checks)
                if not result['ok']:
                    result['status'] = '失败/未运行'
            results[name] = result
            print(worker + ' ' + name + ': ' + result['status'] +
                  ' errno=' + str(result['errno']) + ' launcher_exit=' + str(rc) +
                  ' command_exit=' + str(result['command_exit']), flush=True)
            if result['stderr_tail']:
                print('\n'.join(result['stderr_tail']), flush=True)
            if isinstance(result['detail'], dict) and result['detail'].get('limited_paths'):
                print('祖先目录访问受限（详见逐项 stat/chdir/getcwd）：' +
                      ', '.join(result['detail']['limited_paths']), flush=True)
            (base / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    ok = (all(row['ok'] for rows in summary['workers'].values() for row in rows.values())
          and (args.kernel_only or summary['opencode_matrix']['ok']))
    print(('PASS' if ok else 'FAIL') + '；证据目录：' + str(base))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
