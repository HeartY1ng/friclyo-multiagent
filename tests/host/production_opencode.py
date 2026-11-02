"""宿主诊断夹具：原样生产入口、真实 OpenCode、假 security 和有界假上游。

所有替换仅发生在本次新建的夹具副本，逐项记录；生产代码没有诊断开关。
英文组只翻译三个目录组件，不改入口、环境、参数、模型或权限语义。
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import sys
import time
import unicodedata
from http.server import ThreadingHTTPServer

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'bin'))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # exec uses Python -I
import offline_opencode as offline

FAKE_KEY = 'FAKE-DOCTOR-PIPE-ONLY'
LAYOUT = {'交付': 'delivery', '运行时': 'runtime', '控制': 'control'}
CWD_LINE = 'cwd=str(Path(task).absolute().parent)'
VARIANTS = ('baseline', 'working-directory', 'caller', 'home', 'tmp', 'folders', 'nfd-rules')
EXPECTED_ERROR = 'error: An unknown error occurred (Unexpected)'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def replace_once(path, old, new):
    source = path.read_text()
    if source.count(old) != 1:
        raise ValueError('夹具替换锚点不唯一：' + path.name)
    path.write_text(source.replace(old, new, 1))


def build_fixture(base, binary, layout, variant):
    if layout not in ('zh', 'ascii') or variant not in VARIANTS:
        raise ValueError('未知诊断组')
    base.mkdir(parents=True, exist_ok=False)
    repo, commands = base / 'repo', base / 'commands'
    home = base / ('中文-home' if layout == 'zh' else 'home')
    root = home / 'AgentWork'
    caller = root / 'framework-caller'
    for path in (commands, caller):
        path.mkdir(parents=True)
    shutil.copytree(REPO / 'bin', repo / 'bin', ignore=shutil.ignore_patterns('__pycache__', 'tests'))
    shutil.copytree(REPO / 'workers', repo / 'workers')
    shutil.copyfile(REPO / '工人守则.md', repo / '工人守则.md')
    support = repo / 'tests/host'
    support.mkdir(parents=True)
    for name in ('offline_opencode.py', 'production_opencode.py'):
        shutil.copyfile(Path(__file__).with_name(name), support / name)
    changes = []
    execution = repo / 'bin/frclyo/execution.py'
    # 保留生产取假 key -> 匿名管道 -> exec 的交接，只换管道接收方为不转发的假上游。
    replace_once(execution,
        "script = Path(__file__).resolve().parents[1] / 'allowlist-proxy.py'",
        "script = Path(__file__).resolve().parents[2] / 'tests/host/production_opencode.py'")
    changes.append('proxy receiver = fixture HTTP completion server; no forwarding')
    if variant != 'working-directory':
        replace_once(execution, CWD_LINE, 'cwd=None')
        changes.append('supervisor cwd=None (r4 inherited caller cwd)')
    isolation = repo / 'bin/frclyo/isolation.py'
    if layout == 'ascii':
        # 生产布局硬编码中文；只在临时测试副本翻译目录词素。不是生产配置功能。
        # capture_directory 也校验交付词素；漏翻译会在启动工人前退出 2。
        for path in (repo / 'bin/frclyo/config.py', isolation, execution):
            source = path.read_text()
            for old, new in LAYOUT.items():
                source = source.replace(old, new)
            path.write_text(source)
        changes.append('fixture directory labels only: ' + json.dumps(LAYOUT, ensure_ascii=False))
    if variant in ('home', 'tmp', 'folders', 'caller'):
        with isolation.open('a') as stream:
            stream.write('\n# Test-only single-factor content experiment, never shipped in production.\n'
                '_production_seatbelt = seatbelt\n'
                'def seatbelt(*args, **kwargs):\n'
                '    import sys\n'
                '    sys.path.insert(0, ' + repr(str(support)) + ')\n'
                '    from offline_opencode import diagnostic_profile\n'
                '    return diagnostic_profile(_production_seatbelt(*args, **kwargs), '
                + repr(variant) + ', args[0], args[1], [' + repr(str(base))
                + ', str(round_tmp(args[1], args[2]))])\n')
        changes.append('single content rule experiment + real-user-data guard: ' + variant)
    elif variant == 'nfd-rules':
        with isolation.open('a') as stream:
            stream.write('\n_production_seatbelt = seatbelt\n'
                'def seatbelt(*args, **kwargs):\n'
                '    import unicodedata\n'
                '    return unicodedata.normalize("NFD", _production_seatbelt(*args, **kwargs))\n')
        changes.append('NFD spelling of generated profile; identical for ordinary Chinese characters')
    security = commands / 'security'
    security.write_text('#!/bin/sh\n[ "$1" = find-generic-password ] || exit 2\nprintf %s '
                        + shlex.quote(FAKE_KEY) + '\n')
    security.chmod(0o700)
    (commands / 'opencode').symlink_to(Path(binary).resolve())
    model = dict(id='deepseek-flash', name='Offline fixture', attachment=False,
                 reasoning=False, tool_call=True, temperature=True,
                 cost=dict(input=0, output=0), limit=dict(context=32768, output=4096),
                 modalities=dict(input=['text'], output=['text']))
    catalog = base / 'models.json'
    catalog.write_text(json.dumps({'deepseek': dict(id='deepseek', name='Offline fixture', env=[],
        npm='@ai-sdk/openai-compatible', api='https://api.deepseek.com', models={'deepseek-flash': model})}))
    cfg = base / 'config.json'
    cfg.write_text(json.dumps({'models_snapshot_source': str(catalog)}))
    task = base / 'task.txt'
    task.write_text('Reply briefly without calling tools. This is an offline startup diagnostic.\n')
    env = {k: v for k, v in os.environ.items() if k in ('PATH', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TZ')}
    env.update(HOME=str(home), FRCLYO_HOME=str(root), FRCLYO_CONFIG=str(cfg),
               CODEX_HOME=str(home / 'fake-codex-home'), PYTHONDONTWRITEBYTECODE='1',
               OPENCODE_WORKER_DEBUG='1',
               PATH=str(commands) + os.pathsep + env.get('PATH', '/usr/bin:/bin'))
    delivery, runtime, control_name = (LAYOUT.get(n, n) if layout == 'ascii' else n
                                       for n in ('交付', '运行时', '控制'))
    run = root / delivery / 'DOCTOR/r1'
    control = root / runtime / control_name / 'opencode/DOCTOR/r1'
    argv = ['/bin/bash', str(repo / 'bin/opencode-worker.sh'), 'DOCTOR', 'r1', str(task),
            'deepseek/deepseek-flash', 'nothink', '60']
    entry = repo / 'bin/opencode-worker.sh'
    if entry.read_bytes() != (REPO / 'bin/opencode-worker.sh').read_bytes():
        raise ValueError('生产入口字节发生变化')
    manifest = dict(layout=layout, variant=variant, caller_cwd=str(caller), run=str(run),
        runtime=str(root / runtime / 'opencode'), control=str(control), executable=str(Path(binary).resolve()),
        production_entry_sha256=digest(entry), pure=False, substitutions=changes,
        run_is_ascii=str(run).isascii(), run_nfc_equals_nfd=(unicodedata.normalize('NFC', str(run)) ==
                                                           unicodedata.normalize('NFD', str(run))),
        argv=argv)
    manifest['fixture_sources'] = {name: {'original_sha256': digest(REPO / name),
                                         'fixture_sha256': digest(repo / name)}
        for name in ('bin/frclyo/config.py', 'bin/frclyo/isolation.py', 'bin/frclyo/execution.py',
                     'bin/allowlist-proxy.py', 'workers/opencode/opencode.worker.jsonc')}
    (base / 'fixture.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    return argv, env, caller, run, control, manifest


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def run_case(base, binary, layout, variant):
    argv, env, caller, run, control, manifest = build_fixture(base, binary, layout, variant)
    rc = offline.execute(argv, env, caller, base / 'launcher.log', 100)
    receipt = read_json(control / 'receipt.json')
    status = read_json(control / 'status.json')
    upstream = read_json(control / 'upstream.json')
    events = control / 'events.jsonl'
    response = offline.response_seen(events.read_text(errors='replace')) if events.exists() else False
    tails = {}
    for name, path in (('launcher', base / 'launcher.log'), ('stderr', control / 'stderr.log'),
                       ('proxy', control / '代理.stderr')):
        tails[name] = path.read_text(errors='replace').splitlines()[-20:] if path.exists() else []
    requests = upstream.get('requests', 0)
    stderr = control / 'stderr.log'
    old_failure = (status.get('rc') not in (None, 0) and requests == 0 and
                   stderr.exists() and EXPECTED_ERROR in stderr.read_text(errors='replace'))
    ok = (rc == 0 and status.get('rc') == 0 and status.get('group_reaped') is True
          and receipt.get('状态') == '执行完成' and requests > 0 and response)
    result = dict(ok=ok, launcher_exit=rc, command_exit=status.get('rc'), requests=requests,
                  response=response, receipt=receipt.get('状态'), group_reaped=status.get('group_reaped'),
                  old_failure_reproduced=old_failure,
                  tails=tails, fixture=manifest, profile=str(control / 'sandbox.sb'),
                  log=str(base / 'launcher.log'))
    (base / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def fake_upstream_main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--provider', required=True, choices=['deepseek'])
    parser.add_argument('--log', type=Path, required=True)
    parser.add_argument('--parent-pid', type=int, required=True)
    args = parser.parse_args()
    if sys.stdin.buffer.readline(4097) != (FAKE_KEY + '\n').encode():
        return 2
    running = [True]
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *args: running.__setitem__(0, False))
    server = ThreadingHTTPServer(('127.0.0.1', 0), offline.FakeCompletion)
    server.daemon_threads = True
    server.timeout = .2
    server.requests = 0
    stats = args.log.parent / 'upstream.json'
    server.record = lambda count: stats.write_text(json.dumps({'requests': count}))
    server.record(0)
    args.log.write_bytes(b'')
    print(server.server_address[1], flush=True)
    until = time.monotonic() + 120
    try:
        while running[0] and os.getppid() == args.parent_pid and time.monotonic() < until:
            server.handle_request()
    finally:
        server.server_close()
        print(json.dumps({'拒绝次数': 0}), flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(fake_upstream_main())
