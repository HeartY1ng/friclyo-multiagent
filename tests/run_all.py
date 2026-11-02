#!/usr/bin/env python3
"""统一执行三组必过离线测试；缺组、零测试或跳过都按失败处理。"""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from host.verify_all import interrupt_scope, run_check, write_summary


def main():
    """各组单独进程隔离模块缓存；主机 TCP/真实模型验收有独立入口。"""
    root = Path(__file__).resolve().parents[1]
    if len(sys.argv) == 3 and sys.argv[1] == '--suite':
        # Internal worker: the supervising invocation owns its process tree and
        # evidence. Keep unittest's ordinary signal behavior in this worker.
        suite = unittest.defaultTestLoader.discover(sys.argv[2], pattern='test_*.py')
        count = suite.countTestCases()
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        return 0 if count and result.wasSuccessful() and not result.skipped else 1
    with interrupt_scope() as state:
        return run_groups(root, state)


def run_groups(root, state):
    # Evidence must outlive the isolated HOME/TMPDIR removed below. The host
    # verifier archives this directory through the existing evidence protocol.
    output = Path(tempfile.mkdtemp(prefix='.offline-evidence-',
                                  dir=str(Path(tempfile.gettempdir()).resolve())))
    print('证据目录：' + str(output), flush=True)
    rows = []
    with tempfile.TemporaryDirectory(prefix='.offline-tests-', dir=str(Path(tempfile.gettempdir()).resolve())) as temporary:
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', FRCLYO_LAUNCHD_DRY_RUN='1',
                   TMPDIR=temporary, TMP=temporary, TEMP=temporary,
                   HOME=str(Path(temporary) / 'home'), FRCLYO_HOME=str(Path(temporary) / 'data'))
        Path(env['HOME']).mkdir()
        env.pop('FRCLYO_CONFIG', None)
        # 各测试自行建立假 Codex 目录，不能继承运行者的真实账号路径。
        env.pop('CODEX_HOME', None)
        jobs = [(group, [sys.executable, '-B', str(Path(__file__).resolve()),
                         '--suite', str(root / group)]) for group in ('bin/tests', 'board', 'tests')]
        if os.environ.get('FRCLYO_E2E') == '1':
            jobs.append(('端到端场景', [sys.executable, '-B', str(root / 'tests/e2e/场景.py')]))
        else:
            print('可选端到端场景未运行；FRCLYO_E2E=1 时启用', flush=True)
        for index, (group, command) in enumerate(jobs):
            if state.signal is not None:
                rows.append(dict(name=group, status='跳过', seconds=0, reason='前项中断',
                                 interrupted=True))
            else:
                print('必过离线测试：' + group, flush=True)
                rows.append(run_check(group, command, 'exit', output / ('%02d' % index),
                                      None, root=root, env=env, live=True, shutdown_grace=0,
                                      archive_evidence=False))
            # Persist completed groups before starting the next one.
            (output / 'summary.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2))
    report = write_summary(rows, output, state)
    print(report, flush=True)
    return state.returncode or int(any(row['status'] == '失败' for row in rows))


if __name__ == '__main__':
    sys.exit(main())
