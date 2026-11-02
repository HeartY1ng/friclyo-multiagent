#!/usr/bin/env python3
"""F02/F03 宿主探针：真实 sandbox-exec + 两轮替身，零网络、零真实账号。"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parents[2]

FAKE = r'''#!/usr/bin/env python3
import errno, json, os, pathlib, sys
if '--version' in sys.argv:
    print('opencode fake-host-probe')
    sys.exit(0)
run = pathlib.Path(sys.argv[sys.argv.index('--dir') + 1])
rt = pathlib.Path(os.environ['XDG_STATE_HOME']).parent
oc = rt.parents[2]
round_id = run.name
snapshot = pathlib.Path(os.environ['OPENCODE_MODELS_PATH'])
current = pathlib.Path(os.environ['OPENCODE_CONFIG'])
checks = {}
debug = '--print-logs' in sys.argv and '--log-level' in sys.argv and 'DEBUG' in sys.argv
checks['调试开关'] = debug if round_id == 'r2' else not any(
    flag in sys.argv for flag in ('--print-logs', '--log-level', 'DEBUG'))
def denied(name, action):
    try:
        action()
    except OSError as exc:
        checks[name] = exc.errno in (errno.EPERM, errno.EACCES)
    else:
        checks[name] = False
for name, path in [('模型快照', snapshot), ('本轮模板', current),
                   ('其他模板', oc / 'worker-OTHER-r9.json'),
                   ('其他状态', oc / 'rounds/OTHER/r9/state/sentinel')]:
    denied('禁写 ' + name, lambda path=path: path.write_text('BAD'))
for name, path in [('其他模板', oc / 'worker-OTHER-r9.json'),
                   ('其他状态', oc / 'rounds/OTHER/r9/state/sentinel')]:
    denied('禁读 ' + name, lambda path=path: path.read_bytes())
checks['可读快照模板'] = bool(snapshot.read_bytes() and current.read_bytes())
for variable in ('XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME', 'XDG_CACHE_HOME', 'TMPDIR', 'TMP', 'TEMP', 'HOME', 'ZDOTDIR', 'NPM_CONFIG_CACHE', 'PIP_CACHE_DIR', 'CLANG_MODULE_CACHE_PATH', 'SWIFT_MODULECACHE_PATH'):
    path = pathlib.Path(os.environ[variable]) / 'owned'
    path.write_text(round_id)
    checks['本轮可写 ' + variable] = path.read_text() == round_id
checks['ASCII临时目录'] = os.environ['TMPDIR'].isascii()
checks['私有HOME'] = pathlib.Path(os.environ['HOME']) == rt / 'home'
checks['私有TMPPREFIX'] = os.environ['TMPPREFIX'] == str(pathlib.Path(os.environ['TMPDIR']) / 'zsh')
task = sys.stdin.read()
checks['注入本轮目录'] = ('本轮目录（交付文件只在这里读写，临时操作按守则第 3 条）：' + str(run.resolve())) in task.split('\n\n---\n\n', 1)[0]
control = run.parents[2] / '运行时/控制/opencode/HOST' / round_id
for name in ('events.jsonl', 'stderr.log'):
    denied('宿主禁读 ' + name, lambda name=name: (control / name).read_bytes())
    denied('宿主禁写 ' + name, lambda name=name: (control / name).write_text('BAD'))
if round_id == 'r1':
    denied('不能预植下轮目录', lambda: (oc / 'rounds/HOST/r2').mkdir())
    denied('不能预植下轮模板', lambda: (oc / 'worker-HOST-r2.json').symlink_to(current))
else:
    old = oc / 'rounds/HOST/r1/state/owned'
    denied('不能读前轮状态', lambda: old.read_bytes())
    denied('不能改前轮状态', lambda: old.write_text('BAD'))
# 工人目录里的伪造事件不能覆盖宿主捕获；这次仍应完成。
(run / 'events.jsonl').write_text('forged worker event')
(run / 'probe-report.json').write_text(json.dumps(checks, ensure_ascii=False))
print(json.dumps({'type': 'text', 'part': {'text': 'HOST_PROBE_OK'}}), flush=True)
sys.exit(0 if all(checks.values()) else 1)
'''
PROXY = '''import signal, sys, time
# 宿主替身代理不监听、不联网；只提供执行脚本所需端口与退出统计协议。
def stop(*args):
    print('{"拒绝次数":0}', flush=True)
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
print(42345, flush=True)
while True: time.sleep(.1)
'''


def main():
    if sys.platform != 'darwin' or not shutil.which('sandbox-exec'):
        print('未运行：需要 macOS sandbox-exec；无宽松回退')
        return 1
    # 所有材料在测试临时目录中；保留便于大脑核对，不接触生产目录。
    base = Path(tempfile.mkdtemp(prefix='frclyo-runtime-rounds-')).resolve()
    repo, home, commands = base / 'repo', base / 'home', base / 'commands'
    shutil.copytree(REPO / 'bin', repo / 'bin', ignore=shutil.ignore_patterns('__pycache__', 'tests'))
    shutil.copytree(REPO / 'workers', repo / 'workers')
    shutil.copy2(REPO / '工人守则.md', repo / '工人守则.md')
    (repo / 'bin/allowlist-proxy.py').write_text(PROXY)
    home.mkdir(); commands.mkdir()
    for name, body in [('opencode', FAKE), ('security', '#!/bin/sh\nprintf FAKE_HOST_ONLY\n')]:
        path = commands / name
        path.write_text(body)
        path.chmod(0o700)
    root = home / 'AgentWork'
    cfg = base / 'config.json'
    catalog = base / 'models.json'
    catalog.write_text(json.dumps({'deepseek': {'api': 'https://api.deepseek.com',
        'npm': '@ai-sdk/openai-compatible', 'models': {'deepseek-flash': {'id': 'deepseek-flash'}}}}))
    cfg.write_text(json.dumps({'models_snapshot_source': str(catalog)}))
    runtime = root / '运行时/opencode'
    other = runtime / 'rounds/OTHER/r9/state'
    other.mkdir(parents=True)
    (other / 'sentinel').write_text('UNCHANGED')
    (runtime / 'worker-OTHER-r9.json').write_text('UNCHANGED')
    card = base / 'card.txt'; card.write_text('HOST_FAKE_ONLY')
    env = {k: v for k, v in os.environ.items() if not any(
        word in k.upper() for word in ('KEY', 'TOKEN', 'SECRET', 'PASSWORD'))}
    for name in ('WORKER_INPUT_DIR', 'FRCLYO_CONFIG', 'CODEX_HOME', 'OPENCODE_WORKER_DEBUG'):
        env.pop(name, None)
    env.update(HOME=str(home), FRCLYO_HOME=str(root), FRCLYO_CONFIG=str(cfg),
               PATH=str(commands) + os.pathsep + env['PATH'], PYTHONDONTWRITEBYTECODE='1')
    summary = {}
    for round_id in ('r1', 'r2'):
        if round_id == 'r2':
            env['OPENCODE_WORKER_DEBUG'] = '1'
        run = root / '交付/HOST' / round_id
        proc = None
        failed = False
        with (base / (round_id + '.log')).open('wb') as log:
            try:
                proc = subprocess.run(['/bin/bash', str(repo / 'bin/opencode-worker.sh'), 'HOST', round_id,
                    str(card), 'deepseek/deepseek-flash', 'nothink', '10'], env=env,
                    stdout=log, stderr=subprocess.STDOUT, timeout=25)
                receipt = json.loads((run / '回执.json').read_text())
                checks = json.loads((run / 'probe-report.json').read_text())
                summary[round_id] = {'rc': proc.returncode, 'receipt': receipt['状态'], 'checks': checks}
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                summary[round_id] = {'error': type(exc).__name__,
                                     'errno': getattr(exc, 'errno', None),
                                     'rc': proc.returncode if proc else None}
                failed = True
        # 即使报告没生成，仍交回启动器和工人 stderr，不只留下 FileNotFoundError。
        diagnostics = {}
        for label, path in (
            ('启动器', base / (round_id + '.log')),
            ('工人stderr', root / '运行时/控制/opencode/HOST' / round_id / 'stderr.log'),
        ):
            try:
                text = path.read_text(encoding='utf-8', errors='replace')
                diagnostics[label] = {'path': str(path), 'tail': text.splitlines()[-20:]}
            except OSError as exc:
                diagnostics[label] = {'path': str(path), 'errno': exc.errno, 'error': type(exc).__name__}
        summary[round_id]['diagnostics'] = diagnostics
        if failed or (proc is not None and proc.returncode):
            print(round_id + ': ' + json.dumps(summary[round_id], ensure_ascii=False), flush=True)
            break
        if round_id == 'r1':
            summary['未预植后轮'] = not os.path.lexists(runtime / 'rounds/HOST/r2') and not os.path.lexists(runtime / 'worker-HOST-r2.json')
        else:
            summary['前轮状态保留'] = (runtime / 'rounds/HOST/r1/state/owned').read_text() == 'r1'
    summary['其他轮次未改变'] = (other / 'sentinel').read_text() == 'UNCHANGED' and (runtime / 'worker-OTHER-r9.json').read_text() == 'UNCHANGED'
    ok = all(summary.get(name) is True for name in ('未预植后轮', '前轮状态保留', '其他轮次未改变'))
    for round_id in ('r1', 'r2'):
        row = summary.get(round_id, {})
        ok = ok and row.get('rc') == 0 and row.get('receipt') == '执行完成' and bool(row.get('checks')) and all(row['checks'].values())
    (base / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(('PASS' if ok else 'FAIL') + '；证据目录：' + str(base))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
