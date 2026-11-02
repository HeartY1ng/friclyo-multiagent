#!/usr/bin/env python3
"""只读取临时哨兵；按宿主预期分别核对禁读和方案 A 的根外可读项。"""
import errno
import json
import os
import subprocess
from pathlib import Path
import sys


def probe(manifest):
    """每个目标恰好尝试一次，拒绝后不重试或换路径；只输出状态，不输出内容。"""
    result = {}
    for name, filename in manifest.get('list_targets', {}).items():
        try:
            os.listdir(filename)
        except OSError as exc:
            result['列目录 ' + name] = {'blocked': exc.errno in (errno.EACCES, errno.EPERM), 'errno': exc.errno}
        else:
            result['列目录 ' + name] = {'blocked': False, 'errno': None}
    for name, filename in manifest['targets'].items():
        try:
            with open(filename, 'rb') as stream:
                stream.read(1)
        except OSError as exc:
            result[name] = {'blocked': exc.errno in (errno.EACCES, errno.EPERM), 'errno': exc.errno}
        else:
            result[name] = {'blocked': False, 'errno': None}
    for name, filename in manifest.get('write_targets', {}).items():
        try:
            # 只写预建的假哨兵；若越权成功，此项与宿主字节对照都会失败。
            with open(filename, 'wb') as stream:
                stream.write(b'UNEXPECTED_WRITE')
        except OSError as exc:
            result['写入 ' + name] = {'blocked': exc.errno in (errno.EACCES, errno.EPERM), 'errno': exc.errno}
        else:
            result['写入 ' + name] = {'blocked': False, 'errno': None}
    # 正向对照：证明解释器确实执行且本轮读写可用，不能把启动失败当作全拒绝。
    run = Path(manifest['run'])
    output = run / 'positive.txt'
    try:
        output.write_text('LOCAL_WRITE_OK', encoding='utf-8')
        positive = output.read_text(encoding='utf-8') == 'LOCAL_WRITE_OK'
        for filename in manifest.get('allowed_read', []):
            positive = Path(filename).read_text() == 'ISOLATION_FAKE_SENTINEL' and positive
        for filename in manifest.get('allowed_write', []):
            Path(filename).write_text('ROUND_STATE_OK')
            positive = Path(filename).read_text() == 'ROUND_STATE_OK' and positive
    except OSError as exc:
        positive = False
        print('positive: errno=' + str(exc.errno) + ' ' + str(exc), file=sys.stderr, flush=True)
    # 正向工具启动是隔离证据的一部分；CLI/解释器启动失败不能算禁读通过。
    tools = {}
    diagnostics = {}
    for name, argv in manifest.get('tools', {}).items():
        try:
            process = subprocess.run(argv, capture_output=True, text=True, timeout=10)
            tools[name] = process.returncode == 0 and bool(process.stdout.strip())
            diagnostics[name] = {'exit': process.returncode, 'errno': None,
                                 'stderr': process.stderr.splitlines()[-12:]}
            if process.stderr:
                print(name + ' stderr:\n' + process.stderr, file=sys.stderr, flush=True)
        except (OSError, subprocess.SubprocessError) as exc:
            tools[name] = False
            stderr = getattr(exc, 'stderr', None) or ''
            if isinstance(stderr, bytes):
                stderr = stderr.decode('utf-8', errors='replace')
            diagnostics[name] = {'exit': None, 'errno': getattr(exc, 'errno', None),
                                 'error': type(exc).__name__, 'stderr': stderr.splitlines()[-12:]}
            print(name + ': ' + json.dumps(diagnostics[name]), file=sys.stderr, flush=True)
    for name in ('getcwd', '/dev/null', '/dev/zero', '/dev/urandom'):
        try:
            if name == 'getcwd':
                ok = Path(os.getcwd()).resolve() == run.resolve()
            elif name == '/dev/null':
                with open(name, 'wb') as stream:
                    ok = stream.write(b'probe') == 5
            else:
                with open(name, 'rb') as stream:
                    ok = len(stream.read(1)) == 1
            tools[name] = ok
            diagnostics[name] = {'ok': ok, 'errno': None}
        except OSError as exc:
            tools[name] = False
            diagnostics[name] = {'ok': False, 'errno': exc.errno, 'error': type(exc).__name__}
            print(name + ': ' + json.dumps(diagnostics[name]), file=sys.stderr, flush=True)
    if manifest.get('temporary'):
        if manifest.get('ascii_temporary'):
            tools['ASCII临时目录'] = manifest['temporary'].isascii() and os.environ.get('TMPDIR', '').isascii()
        tools['私有临时环境'] = all(os.path.realpath(os.environ.get(name, '')) == os.path.realpath(manifest['temporary'])
                                     for name in ('TMPDIR', 'TMP', 'TEMP'))
    positive = positive and all(tools.values())
    result['本轮读写'] = {'ok': positive, 'tools': tools, 'diagnostics': diagnostics}
    if manifest.get('read_policy_note'):
        print(manifest['read_policy_note'], file=sys.stderr, flush=True)
    print(json.dumps(result, ensure_ascii=False))
    readable = manifest.get('readable_names', [])
    return 0 if positive and all(
        (not row['blocked'] and row['errno'] is None) if name in readable else row['blocked']
        for name, row in result.items() if name != '本轮读写') else 1


if __name__ == '__main__':
    print('FRCLYO_ISOLATION_PROBE_STARTED', file=sys.stderr, flush=True)
    sys.exit(probe(json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))))
