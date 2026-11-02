#!/usr/bin/env python3
"""宿主真实 Codex 命令冒烟（一次付费模型调用）；不读取或打印登录内容。"""
import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'bin'))
from frclyo.isolation import codex_tmp


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', default='gpt-6-astra')
    parser.add_argument('--effort', default='high')
    parser.add_argument('--swift-cwd', choices=('tmp', 'run', 'root'), default='tmp',
                        help='默认 tmp 从本轮私有目录构建并拷回；run/root 仅诊断旧行为，不自动重试')
    args = parser.parse_args()
    base = Path(tempfile.mkdtemp(prefix='frclyo-codex-tmp-smoke-')).resolve()
    root = base / 'AgentWork'
    run = root / '交付/TMP-SMOKE/r1'
    temporary = codex_tmp(root, run)
    code = '\n'.join([
        'import json, os, pathlib, shutil, subprocess',
        'run = pathlib.Path(' + repr(str(run)) + ')',
        'expected = ' + repr(str(temporary)),
        "checks = {name: os.environ.get(name) == expected for name in ('TMPDIR', 'TMP', 'TEMP')}",
        "path = pathlib.Path(os.environ['TMPDIR']) / 'smoke-owned'",
        "path.write_text('ROUND_TMP_OK')",
        "checks['tmp_rw'] = path.read_text() == 'ROUND_TMP_OK'",
        "checks['git'] = subprocess.run(['git', '--version'], capture_output=True).returncode == 0",
        "checks['bash'] = subprocess.run(['/bin/bash', '--noprofile', '--norc', '-c', 'exit 0']).returncode == 0",
        "checks['getcwd'] = pathlib.Path(os.getcwd()).resolve() == run.resolve()",
        "work = pathlib.Path(expected)" if args.swift_cwd == 'tmp' else "work = run",
        "source, program = work / 'tmp-smoke.swift', work / 'tmp-smoke-swift'",
        "source.write_text('print(\"ok\")\\n')",
        "compiled = subprocess.run(['swiftc', str(source), '-o', str(program)], cwd="
            + ("'/'" if args.swift_cwd == 'root' else 'str(work)') + ", timeout=60)",
        "checks['swift_compile'] = compiled.returncode == 0",
        "checks['swift_run'] = False",
        "if checks['swift_compile']:",
        "    executed = subprocess.run([str(program)], capture_output=True, text=True, timeout=10)",
        "    checks['swift_run'] = executed.returncode == 0 and executed.stdout.strip() == 'ok'",
        "checks['copy_back'] = False",
        "if checks['swift_run']:",
        "    if work != run:",
        "        for item in (source, program): shutil.copy2(item, run / item.name)",
        "    checks['copy_back'] = all((run / item.name).read_bytes() == item.read_bytes() for item in (source, program))",
        "(run / 'tmp-smoke.json').write_text(json.dumps(checks))",
        'assert all(checks.values()), checks',
        "print('TMP_SMOKE_OK')",
    ])
    task = base / 'task.txt'
    task.write_text('只做本轮临时目录冒烟，执行下面一条命令一次；失败就停止并报告，不重试、不换路径。'
                    '保留 tmp-smoke.json，并按五段格式交回。\n\npython3 -I -c ' + shlex.quote(code) + '\n')
    env = dict(os.environ, FRCLYO_HOME=str(root), CODEX_WORKER_WEB='0',
               CODEX_WORKER_SUBAGENTS='0', CODEX_WORKER_FAST='0')
    for name in ('FRCLYO_CONFIG', 'WORKER_INPUT_DIR'):
        env.pop(name, None)
    # CODEX_HOME 只交给宿主入口；模型命令依靠真实命名权限禁止读取它。
    with (base / 'launch.log').open('wb') as log:
        try:
            result = subprocess.run(['/bin/bash', str(REPO / 'bin/codex-worker.sh'),
                'TMP-SMOKE', 'r1', str(task), args.model, args.effort, '180'],
                env=env, cwd=REPO, stdout=log, stderr=subprocess.STDOUT, timeout=210)
            receipt = json.loads((run / '回执.json').read_text())
            checks = json.loads((run / 'tmp-smoke.json').read_text())
            ok = (result.returncode == 0 and receipt.get('状态') == '执行完成'
                  and receipt.get('执行命令数', 0) >= 1
                  and set(checks) == {'TMPDIR', 'TMP', 'TEMP', 'tmp_rw', 'git', 'bash', 'getcwd',
                                     'swift_compile', 'swift_run', 'copy_back'}
                  and all(value is True for value in checks.values())
                  and (temporary / 'smoke-owned').read_text() == 'ROUND_TMP_OK')
        except (OSError, ValueError, subprocess.SubprocessError):
            ok = False
    print(('PASS' if ok else 'FAIL') + '；Swift cwd 模式=' + args.swift_cwd + '；证据目录：' + str(base))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
