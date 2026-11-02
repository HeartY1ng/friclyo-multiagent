#!/usr/bin/env python3
"""Claude 本机入口：两种沙箱各执行确定性 Python，再各启动一次真实工人复述守则。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import uuid

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'bin'))
from frclyo.isolation import (codex_permission, seatbelt, codex_tmp, codex_temp_environment,
                              codex_command_environment, prepare_codex_tmp,
                              prepare_round_tmp, temp_environment, SECRET_NAMES)
from frclyo.execution import exclusive_write
from frclyo import config


def recitation_config(models_source=None):
    """宿主读取生产配置的非凭据字段；不取 key、不读 key_env_file。

    新的假根仅保存 DeepSeek 路由、钥匙串条目名称和模型缓存路径。
    真正的 key 仍由生产 execution.start_proxy 经匿名管道交给代理。
    """
    cfg = config.load()
    provider = cfg['providers'].get('deepseek')
    if provider is None:
        raise ValueError('生产配置未启用 deepseek')
    result = {'providers': {'deepseek': {name: provider[name] for name in
              ('opencode_family', 'api_host', 'balance_api') if name in provider}}}
    entry = cfg.get('keychain_entries', {}).get('deepseek')
    if entry is not None:
        result['keychain_entries'] = {'deepseek': {name: entry[name] for name in ('service', 'account')}}
    source = models_source or cfg.get('models_snapshot_source') or (
        Path(os.environ.get('XDG_CACHE_HOME', str(Path.home() / '.cache'))) / 'opencode/models.json')
    result['models_snapshot_source'] = str(Path(source).expanduser().resolve())
    return result


def codex_probe_argv(binary, root, run, sensitive, runtime, command):
    """0.153.4 的 sandbox 直接接命令；权限定义复用真实工人函数。

    exec 的 ignore-user-config/ignore-rules 不属于 sandbox 的参数。
    调用方提供空 CODEX_HOME，显式选档并保留 managed requirements；
    不传 -s/--full-auto，避免覆盖命名权限。
    """
    # 权限生成器在宿主 Python 内执行，Path.home() 不会跟随随后传给 CLI 的假 HOME。
    # 根外可读后必须显式保留假 HOME 的密钥拒绝，不能依赖 F01 整体禁读来遮盖。
    fixture_secrets = [str(Path(root).parent / name) for name in SECRET_NAMES]
    permission = codex_permission(str(root), str(run), list(sensitive) + fixture_secrets,
                                  str(runtime), temporary_home=Path(root).parent)
    return [binary, 'sandbox', '-C', str(run), '-c', permission,
            '-c', 'default_permissions="frclyo_worker"',
            '-c', 'approvals_reviewer="user"', '-c', 'approval_policy="never"',
            '-c', 'shell_environment_policy.inherit="core"',
            '-c', 'shell_environment_policy.ignore_default_excludes=false',
            '-c', codex_temp_environment(root, run, Path(root).parent),
            '-P', 'frclyo_worker', '--include-managed-config', '--'] + command


def normalize_body(text):
    """只忽略编号、空白与引号/标点表现形式，不改正文字符。"""
    text = unicodedata.normalize('NFKC', text).strip()
    text = re.sub(r'^\s*(?:\d+[.、)]|[（(]\d+[）)])\s*', '', text)
    return ''.join(c for c in text if not c.isspace() and
                   not unicodedata.category(c).startswith('P'))


def recitation_matches(first, output):
    # 结果含五段交付格式；在整段回复中找规范化后的第一条正文。
    body = normalize_body(first)
    return bool(body) and body in normalize_body(output)


PLAN_A_NOTE = '按方案 A，Codex 不做根外禁读'


def readable_names(manifest):
    """仅由宿主夹具指定允许读取的项，不能由探针输出改变预期。"""
    return manifest.get('readable_names', [])


def kernel_checks(log, names, rc, readable=()):
    """只有完整的探针结果才能裁定隔离；CLI 退出码本身不是证据。"""
    try:
        lines = log.read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        lines = []
    data = None
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if (isinstance(row, dict) and
                all(isinstance(row.get(name), dict) and
                    type(row[name].get('blocked')) is bool for name in names) and
                isinstance(row.get('本轮读写'), dict) and
                type(row['本轮读写'].get('ok')) is bool):
            data = row
    labels = [('可读 ' if name in readable else '禁读 ') + name for name in names] + ['本轮读写', '探针退出']
    if data is None:
        state = ('探针未完成' if 'FRCLYO_ISOLATION_PROBE_STARTED' in lines
                 else '探针未运行')
        return state, dict.fromkeys(labels, None)
    # ENOENT 等同样 blocked=False；必须有明确的无异常记录才算读取成功。
    values = [(data[name]['blocked'] is False and 'errno' in data[name] and data[name]['errno'] is None)
              if name in readable else data[name]['blocked'] for name in names]
    values += [data['本轮读写']['ok'], rc == 0]
    return '已运行', dict(zip(labels, values))


def probe_names(manifest):
    return (list(manifest['targets']) +
            ['写入 ' + name for name in manifest.get('write_targets', {})] +
            ['列目录 ' + name for name in manifest.get('list_targets', {})])


def build_area(base, name, system_temp=None):
    """哨兵全部预先在沙箱外验证可读，包括符号链接；不接触生产数据。"""
    area = base / name
    home = area / 'home'
    root = home / 'AgentWork'
    run = root / '交付/ISOLATION/r1'
    runtime_root = root / ('运行时/' + name)
    runtime = runtime_root / 'rounds/ISOLATION/r1'
    control = root / ('运行时/控制/' + name + '/.network-probe')
    for directory in (run, control, home / '.hermes', home / '.ssh', root / '配置',
                      root / '交付/OTHER/r9', root / '素材', home / 'Documents',
                      area / 'outside-home', area / 'kernel-codex-home'):
        directory.mkdir(parents=True, exist_ok=True)
    targets = {'账本': root / '账本.jsonl', '配置': root / '配置/config.json',
               '偏好': root / '配置/偏好.md', '别的轮次': root / '交付/OTHER/r9/secret.txt',
               '素材': root / '素材/secret.txt', '.hermes': home / '.hermes/sentinel',
               '.ssh': home / '.ssh/sentinel',
               '根外用户文档': home / 'Documents/sentinel',
               '公共临时文件': area / 'outside-home/sentinel',
               'CODEX_HOME/auth.json': area / 'kernel-codex-home/auth.json'}
    for path in targets.values():
        path.write_text('{}' if path.name == 'auth.json' else 'ISOLATION_FAKE_SENTINEL', encoding='utf-8')
    link = run / 'other-round-link'
    link.symlink_to(targets['别的轮次'])
    targets['符号链接'] = link
    assert all(path.read_text() == ('{}' if path.name == 'auth.json' else 'ISOLATION_FAKE_SENTINEL')
               for path in targets.values())
    auth_link = run / 'codex-auth-link'
    auth_link.symlink_to(targets['CODEX_HOME/auth.json'])
    assert auth_link.read_text() == '{}'
    targets['登录目录符号链接'] = auth_link
    manifest = {'run': str(run), 'root': str(root), 'home': str(home),
                'targets': {name: str(path) for name, path in targets.items()},
                'list_targets': {'根外文档': str(home / 'Documents'),
                                 '别的轮次': str(root / '交付/OTHER/r9'),
                                 '数据根目录': str(root),
                                 '交付父目录': str(run.parent.parent),
                                 '任务父目录': str(run.parent)},
                'write_targets': {'HOME外哨兵': str(area / 'outside-home/sentinel'),
                                  'HOME外新文件': str(area / 'outside-home/new-file'),
                                  'HOME其他位置哨兵': str(home / 'Documents/sentinel'),
                                  'HOME其他位置新文件': str(home / 'Documents/new-file')},
                'tools': {'python3': ['python3', '-I', '-c', 'print(1)'],
                          'git': ['git', '--version'], 'bash': ['bash', '--noprofile', '--norc', '-c', 'printf 1']}}
    prepare = (lambda target: prepare_codex_tmp(root, target, home)) if name == 'codex' else (
        lambda target: prepare_round_tmp(root, target))
    temporary = Path(prepare(run))
    manifest['temporary'] = str(temporary)
    manifest['ascii_temporary'] = True
    other_tmp = Path(prepare(root / '交付/OTHER/r9'))
    (other_tmp / 'sentinel').write_text('ISOLATION_FAKE_SENTINEL')
    manifest['targets']['其他轮次ASCII临时目录'] = str(other_tmp / 'sentinel')
    manifest['write_targets']['其他轮次ASCII临时目录'] = str(other_tmp / 'sentinel')
    parent_sentinel = temporary.parent / ('frclyo-doctor-parent-' + uuid.uuid4().hex)
    exclusive_write(parent_sentinel, b'ISOLATION_FAKE_SENTINEL')
    manifest['write_targets']['临时目录父级'] = str(parent_sentinel)
    manifest['write_targets']['临时目录父级新文件'] = str(temporary.parent / ('new-' + uuid.uuid4().hex))
    write_link = run / 'outside-write-link'
    write_link.symlink_to(area / 'outside-home')
    manifest['write_targets']['符号链接根外哨兵'] = str(write_link / 'sentinel')
    manifest['write_targets']['符号链接根外新文件'] = str(write_link / 'link-new-file')
    if name == 'codex':
        runtime.mkdir(parents=True)
        other = runtime_root / 'rounds/OTHER/r9/tmp'
        other.mkdir(parents=True)
        (other / 'sentinel').write_text('ISOLATION_FAKE_SENTINEL')
        (control / 'events.jsonl').write_text('ISOLATION_FAKE_SENTINEL')
        manifest['targets'].update({'其他轮次运行时': str(other / 'sentinel'),
                                    '宿主捕获': str(control / 'events.jsonl')})
        manifest['write_targets'].update({'其他轮次运行时': str(other / 'sentinel'),
                                          '运行时父目录': str(runtime / 'outside-tmp')})
        manifest['allowed_write'] = [str(temporary / sub / 'state.txt') for sub in (
            '', 'home', 'cache', 'clang-module-cache', 'swift-module-cache')]
        manifest['temporary'] = str(temporary)
    if name == 'opencode':
        runtime.mkdir(parents=True)
        other = runtime_root / 'rounds/OTHER/r9'
        other.mkdir(parents=True)
        snapshot = runtime_root / 'models.snapshot.json'
        template = runtime_root / 'worker-ISOLATION-r1.json'
        other_template = runtime_root / 'worker-OTHER-r9.json'
        other_state = other / 'state.txt'
        private = control / 'events.jsonl'
        for path in (snapshot, template, other_template, other_state, private):
            path.write_text('ISOLATION_FAKE_SENTINEL')
        manifest['targets'].update({'其他轮次模板': str(other_template), '其他轮次运行时': str(other_state),
                                    '宿主捕获': str(private)})
        manifest['write_targets'].update({key: str(value) for key, value in
            {'模型快照': snapshot, '本轮模板': template, '其他轮次模板': other_template,
             '其他轮次运行时': other_state, '宿主捕获': private}.items()})
        manifest['allowed_read'] = [str(snapshot), str(template)]
        manifest['allowed_write'] = [str(runtime / 'state.txt'), str(temporary / 'state.txt')]
    if system_temp is not None:
        system_file = system_temp / (name + '-sentinel')
        system_file.write_text('ISOLATION_FAKE_SENTINEL')
        manifest['targets']['系统临时文件'] = str(system_file)
        manifest['write_targets']['系统临时文件'] = str(system_file)
    if name == 'codex':
        manifest['read_policy_note'] = PLAN_A_NOTE
        manifest['readable_names'] = ['根外用户文档', '公共临时文件', '列目录 根外文档',
                                     '其他轮次ASCII临时目录']
        if system_temp is not None:
            manifest['readable_names'].append('系统临时文件')
    shutil.copy2(Path(__file__).with_name('isolation_probe.py'), run / 'probe.py')
    (run / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
    return home, root, run, runtime, control


def execute(argv, env, cwd, log, timeout):
    """保存原始 stdout/stderr，但不保存调用环境或凭据；超时明确失败。"""
    with log.open('wb') as output:
        try:
            result = subprocess.run(argv, env=env, cwd=cwd, stdout=output,
                                    stderr=subprocess.STDOUT, timeout=timeout)
            return result.returncode
        except (OSError, subprocess.TimeoutExpired) as exc:
            # 只记异常类型、errno 和超时，不打印环境或 argv。
            output.write(('\nlauncher: ' + type(exc).__name__ +
                          ' errno=' + str(getattr(exc, 'errno', None)) +
                          ' timeout=' + str(timeout if isinstance(exc, subprocess.TimeoutExpired) else None) + '\n').encode())
            return 125


# 子进程主动重定向到本轮文件，避免 sandbox CLI 不转发子进程 stderr 时丢证据。
# 宿主再收集到本轮外的 kernel.log；这些是假探针的证据，不是可信工人回执。
KERNEL_SHELL = r'''out=$1; err=$2; shift 2
exec >"$out" 2>"$err" || exit 125
printf '%s\n' FRCLYO_KERNEL_SHELL_STARTED >&2
"$@"
rc=$?
printf 'FRCLYO_KERNEL_COMMAND_EXIT=%s\n' "$rc" >&2
exit "$rc"
'''


def kernel_command(command, run):
    return ['/bin/bash', '--noprofile', '--norc', '-c', KERNEL_SHELL,
            'frclyo-kernel', str(run / 'kernel-child.stdout'),
            str(run / 'kernel-child.stderr')] + command


def execute_kernel(argv, env, run, log, timeout, executor=None):
    # 文件先由宿主清空，避免上次项的输出被误作这次证据；不重试命令。
    for suffix in ('stdout', 'stderr'):
        (run / ('kernel-child.' + suffix)).write_bytes(b'')
    if argv:
        rc = (executor or execute)(argv, env, run, log, timeout)
    else:
        log.write_text('launcher: required CLI missing; exit=125\n')
        rc = 125
    with log.open('ab') as output:
        output.write(('\nFRCLYO_LAUNCHER_EXIT=' + str(rc) + '\n').encode())
        for suffix in ('stdout', 'stderr'):
            output.write(('\n--- child ' + suffix + ' ---\n').encode())
            try:
                output.write((run / ('kernel-child.' + suffix)).read_bytes())
            except OSError as exc:
                output.write(('capture: ' + type(exc).__name__ + ' errno=' + str(exc.errno) + '\n').encode())
            output.write(b'\n')
    return rc


def kernel_diagnosis(log, state, rc):
    """连空日志、启动器退出 0 却没有结果都必须给出可核对的原因。"""
    try:
        text = log.read_text(encoding='utf-8', errors='replace')
        error = None
    except OSError as exc:
        text = ''
        error = type(exc).__name__ + ' errno=' + str(exc.errno)
    tail = text.split('--- child stderr ---', 1)[-1].strip().splitlines()[-12:]
    # 子 stderr 为空时保留启动器诊断；绝不伪造内核 errno。
    if not tail:
        tail = text.strip().splitlines()[-12:]
    launcher_tail = text.split('--- child stdout ---', 1)[0].strip().splitlines()[-12:]
    shell = 'FRCLYO_KERNEL_SHELL_STARTED' in text
    python = 'FRCLYO_ISOLATION_PROBE_STARTED' in text
    exits = [line.partition('=')[2] for line in text.splitlines()
             if line.startswith('FRCLYO_KERNEL_COMMAND_EXIT=')]
    command_rc = int(exits[-1]) if exits and exits[-1].lstrip('-').isdigit() else None
    reason = ('完整结果已收集' if state == '已运行' else
              '未收到完整探针 JSON；shell 启动标记=' + str(shell) +
              '；Python 启动标记=' + str(python))
    reason += '；启动器退出码=' + str(rc)
    reason += '；沙箱内命令退出码=' + str(command_rc)
    launch_errors = [line for line in launcher_tail if line.startswith('launcher:')]
    if launch_errors:
        reason += '；' + launch_errors[-1]
    if error:
        reason += '；日志读取失败：' + error
    elif not text.strip():
        reason += '；stdout/stderr 均为空'
    return {'原因': reason, '命令退出码': command_rc, 'stderr末尾': tail,
            '启动器末尾': launcher_tail, '日志读取错误': error}


def main():
    """缺 CLI、登录、缓存、钥匙串凭据或权限支持都失败，绝不自动降级。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codex-model', default='gpt-6-astra')
    parser.add_argument('--codex-effort', default='high')
    parser.add_argument('--models-source', type=Path, help='本机 OpenCode models.json（不自动下载）')
    parser.add_argument('--offline-only', action='store_true', help='仅内核探针；未做真实复述，整体返回非零')
    args = parser.parse_args()
    if sys.platform != 'darwin' or not shutil.which('sandbox-exec'):
        print('⏸ 探针未运行：需要 macOS sandbox-exec；未验证任何隔离项')
        return 1
    codex, opencode = shutil.which('codex'), shutil.which('opencode')
    original_home = Path.home()
    codex_home = Path(os.environ.get('CODEX_HOME', str(original_home / '.codex'))).expanduser().resolve()
    live_config = None
    if not args.offline_only:
        print('完整模式：各启动一次 Codex 和 DeepSeek 真实复述，可能产生模型费用；'
              '探针不读取 key，OpenCode 由生产宿主入口从钥匙串经匿名管道注入代理。', flush=True)
        try:
            # 必须在改成夹具 HOME/FRCLYO_HOME 之前读取真实配置。
            live_config = recitation_config(args.models_source)
        except (OSError, ValueError):
            print('OpenCode 复述准备失败：请检查宿主生产配置；不回显配置内容。', flush=True)
    base = Path(tempfile.mkdtemp(prefix='frclyo-isolation-', dir='/private/tmp')).resolve()
    system_temp = Path(tempfile.mkdtemp(prefix='frclyo-system-temp-')).resolve()
    # 确定性探针与生产启动器均不继承环境中的 key/token。
    env_base = {k: v for k, v in os.environ.items() if not any(
        word in k.upper() for word in ('KEY', 'TOKEN', 'SECRET', 'PASSWORD'))}
    for name in ('FRCLYO_CONFIG', 'WORKER_INPUT_DIR', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME',
                 'XDG_DATA_HOME', 'XDG_STATE_HOME', 'OPENCODE_CONFIG', 'OPENCODE_CONFIG_CONTENT'):
        env_base.pop(name, None)
    results = {}
    probes = {}
    def report(name, ok):
        results[name] = ok
        print(('⏸ ' if ok is None else '✅ ' if ok else '❌ ') + name, flush=True)
    for worker in ('codex', 'opencode'):
        home, root, run, runtime, control = build_area(base, worker, system_temp)
        env = dict(env_base, HOME=str(home), FRCLYO_HOME=str(root), CODEX_HOME=str(codex_home))
        binary = codex if worker == 'codex' else opencode
        if binary:
            execute([binary, '--version'], env, run, base / worker / 'version.log', 15)
        probe_home = base / worker / 'kernel-codex-home'
        sensitive = [str(codex_home), str(probe_home), str(root / '配置/config.json')]
        command = kernel_command([sys.executable, '-I', str(run / 'probe.py'), str(run / 'manifest.json')], run)
        # Codex 使用自身 sandbox 子命令；配置与生产工人同源，不能换成外套模拟沙箱。
        if worker == 'codex' and codex:
            command = ['/usr/bin/env'] + [key + '=' + value for key, value in
                codex_command_environment(root, run, home).items()] + command
            argv = codex_probe_argv(codex, root, run, sensitive, runtime, command)
            (control / 'permission.txt').write_text(argv[5], encoding='utf-8')
            # 仅内核启动器使用空配置，不读取真实登录/配置/规则。
            # 禁读清单仍指向真实 CODEX_HOME；真实复述继续使用原 env。
            # 只有假 auth.json；确定性探针不使用真实登录。
        elif worker == 'opencode':
            profile = control / 'sandbox.sb'
            profile.write_text(seatbelt(str(home), str(root), str(run), str(runtime), str(control),
                                       9, sensitive, [str(root), str(root / '运行时/opencode')],
                                       [str(root / '运行时/opencode/models.snapshot.json'),
                                        str(root / '运行时/opencode/worker-ISOLATION-r1.json')]), encoding='utf-8')
            argv = ['sandbox-exec', '-f', str(profile)] + command
        else:
            argv = []
        log = base / worker / 'kernel.log'
        temporary_env = (codex_command_environment(root, run, home) if worker == 'codex'
                         else temp_environment(root, run))
        kernel_env = dict(env, **temporary_env)
        # CLI 解析 ~ 时仍使用夹具宿主 HOME；命令私有 HOME 由 env 包装设置。
        kernel_env.update(HOME=str(home), CODEX_HOME=str(probe_home))
        manifest = json.loads((run / 'manifest.json').read_text())
        # 启动前钉住假文件的预期；探针若声称拒绝却实际改写，宿主独立发现。
        before = {filename: Path(filename).read_bytes() if Path(filename).exists() else None
                  for filename in manifest.get('write_targets', {}).values()}
        rc = execute_kernel(argv, kernel_env, run, log, 45)
        names = probe_names(manifest)
        state, checks = kernel_checks(log, names, rc, readable_names(manifest))
        if state == '已运行':
            for name, filename in manifest.get('write_targets', {}).items():
                try:
                    after = Path(filename).read_bytes() if Path(filename).exists() else None
                    if after != before[filename]:
                        checks['禁读 写入 ' + name] = False
                except OSError:
                    checks['禁读 写入 ' + name] = False
        probes[worker] = dict({'状态': state, '退出码': rc, '日志': str(log)},
                              **kernel_diagnosis(log, state, rc))
        print(worker + ' 内核：' + state + '；' + probes[worker]['原因'], flush=True)
        if manifest.get('read_policy_note'):
            print(manifest['read_policy_note'], flush=True)
        if state != '已运行':
            print('\n'.join(probes[worker]['stderr末尾']) or '(无 stderr；见启动器退出码及启动标记)', flush=True)
        for name, ok in checks.items():
            report(worker + ' ' + name, ok)
        # 复述任务不含第一条答案；模型必须从执行脚本注入的守则中取得。
        if args.offline_only or not (codex if worker == 'codex' else opencode and live_config):
            report(worker + ' 真实守则复述', False)
            continue
        # 真实工人使用同一假根的新轮次；配置是无凭据的有效 JSON，所有哨兵仍存在。
        if worker == 'opencode':
            # 内核检查及写入比对已完成；哨兵不是模型缓存。
            # 移除本探针创建的假快照，让生产入口从 models_snapshot_source 建快照。
            (root / '运行时/opencode/models.snapshot.json').unlink()
        cfg = live_config if worker == 'opencode' else {}
        (root / '配置/config.json').write_text(json.dumps(cfg), encoding='utf-8')
        task = base / worker / 'task.txt'
        task.write_text('请遵守五段交付格式，在结论中逐字复述本次收到的《工人守则》第一条（保留编号）。不运行命令，不读取任何其他文件。', encoding='utf-8')
        env.update(CODEX_WORKER_WEB='0', CODEX_WORKER_SUBAGENTS='0', CODEX_WORKER_FAST='0')
        model, effort = (args.codex_model, args.codex_effort) if worker == 'codex' else ('deepseek/deepseek-flash', 'nothink')
        argv = ['/bin/bash', str(REPO / 'bin' / (worker + '-worker.sh')), 'ISOLATION', 'model', str(task), model, effort, '180']
        # 只有生产宿主入口恢复真实 HOME，供 security 定位登录钥匙串。
        # opencode-worker.sh 在启动工人前仍设置独立的 RT/home；内核探针 env 不变。
        launch_env = dict(env, HOME=str(original_home)) if worker == 'opencode' else env
        rc = execute(argv, launch_env, REPO, base / worker / 'model-launch.log', 210)
        output = root / '交付/ISOLATION/model'
        try:
            receipt = json.loads((output / '回执.json').read_text())
            first = next(line for line in (REPO / '工人守则.md').read_text().splitlines() if line.startswith('1. '))
            ok = (rc == 0 and receipt['状态'] == '执行完成' and
                  recitation_matches(first, (output / '结果.txt').read_text()) and receipt['守则sha256'] ==
                  hashlib.sha256((REPO / '工人守则.md').read_bytes()).hexdigest())
        except (OSError, ValueError, KeyError):
            ok = False
        report(worker + ' 真实守则复述', ok)
    (base / 'summary.json').write_text(json.dumps({'checks': results, 'probes': probes},
                                                ensure_ascii=False, indent=2), encoding='utf-8')
    print('证据目录：' + str(base))
    print('系统临时哨兵目录：' + str(system_temp))
    print('汇总：%d 通过，%d 未通过，%d 未判定；复述与隔离独立计分' % (
        sum(v is True for v in results.values()), sum(v is False for v in results.values()),
        sum(v is None for v in results.values())))
    return 0 if all(results.values()) else 1


if __name__ == '__main__':
    sys.exit(main())
