"""离线安全边界：只用临时哨兵、替身进程，无真实模型/网络/账号。"""
import contextlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from frclyo import execution as ex


class FileBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def test_network_field_on_unreaped_failure_and_timeout(self):
        run, control = self.root / 'run', self.root / 'control'
        run.mkdir()
        control.mkdir()
        for worker in ('codex', 'opencode'):
            for net in ('0', '1'):
                for timed_out in (False, True):
                    ex.write_json(control / 'status.json', dict(rc=124 if timed_out else 125,
                                  timed_out=timed_out, group_reaped=False, errors=[]))
                    with mock.patch.dict(os.environ, CODEX_WORKER_NET=net):
                        result = ex.finalize(worker, run, control, 'T', 'r1', 'fake', 'high',
                                             'start', 'end', 'sha')
                    self.assertNotEqual(result, 0)
                    receipt = json.loads((run / '回执.json').read_text())
                    trusted = json.loads((control / 'receipt.json').read_text())
                    self.assertEqual(receipt, trusted)
                    self.assertEqual(receipt['状态'], '超时' if timed_out else '失败')
                    if worker == 'codex':
                        self.assertEqual(receipt['命令网络'], '开' if net == '1' else '关')
                    else:
                        self.assertNotIn('命令网络', receipt)

    def test_project_first_card_line_and_summary(self):
        from frclyo.isolation import inject_task
        card = self.root / 'card.txt'
        run = self.root / 'run'
        run.mkdir()
        for text, expected in [('项目：zh2en-语料\n正文', 'zh2en-语料'),
                               ('标题\n项目：忽略', ''), ('', '')]:
            card.write_text(text)
            inject_task(card, run / '任务.txt')
            self.assertEqual(ex.task_project(run), expected)
        control = self.root / 'control'
        control.mkdir()
        for worker in ('codex', 'opencode'):
            ex.write_json(control / 'receipt.json', {'项目': 'zh2en-语料', '工人': worker,
                '任务编号': 'T', '轮次编号': 'r1', '状态': '执行完成'})
            with contextlib.redirect_stdout(io.StringIO()) as output:
                ex.receipt_summary(control, run)
            self.assertEqual(output.getvalue(),
                '【项目 zh2en-语料】T r1 执行完成（回执：%s）\n' % (run / '回执.json'))

    def test_safe_read_rejects_symlink_fifo_directory_and_large_file(self):
        regular = self.root / 'regular'
        regular.write_bytes(b'sentinel')
        link = self.root / 'link'
        link.symlink_to(regular)
        fifo = self.root / 'fifo'
        os.mkfifo(fifo)
        for path, limit in ((link, 20), (fifo, 20), (self.root, 20), (regular, 2)):
            with self.subTest(path=path):
                with self.assertRaises((OSError, ValueError)):
                    ex.safe_read(path, limit)
        self.assertEqual(ex.safe_read(regular), b'sentinel')

    def test_read_rejects_link_ancestor(self):
        real = self.root / 'real'
        real.mkdir()
        (real / 'data').write_bytes(b'sentinel')
        link = self.root / 'link'
        link.symlink_to(real, target_is_directory=True)
        with self.assertRaises(OSError):
            ex.safe_read(link / 'data')

    def test_atomic_write_replaces_link_and_preserves_sentinel(self):
        sentinel = self.root / 'sentinel'
        sentinel.write_bytes(b'UNCHANGED')
        output = self.root / '回执.json'
        output.symlink_to(sentinel)
        ex.atomic_write(output, b'NEW')
        self.assertEqual(sentinel.read_bytes(), b'UNCHANGED')
        self.assertFalse(output.is_symlink())
        self.assertEqual(output.read_bytes(), b'NEW')
        self.assertEqual(list(self.root.glob('.host-*')), [])

    def test_atomic_write_failure_preserves_target_and_removes_temporary(self):
        target = self.root / 'target'
        target.write_bytes(b'original')
        with mock.patch.object(ex.os, 'replace', side_effect=OSError('injected')):
            with self.assertRaises(OSError):
                ex.atomic_write(target, b'new')
        self.assertEqual(target.read_bytes(), b'original')
        self.assertEqual(list(self.root.glob('.host-*')), [])

    def test_exclusive_creation_never_overwrites_existing_file_or_link(self):
        runtime = self.root / 'runtime'
        runtime.mkdir()
        sentinel = self.root / 'sentinel'
        sentinel.write_text('original')
        for kind in ('file', 'link'):
            template = runtime / ('worker-T.' + kind + '.json')
            if kind == 'file':
                template.write_text('old')
            else:
                template.symlink_to(sentinel)
            with self.assertRaises(FileExistsError):
                ex.prepare_runtime(runtime, 'T', kind, '{}')
            self.assertEqual(template.read_text(), 'old' if kind == 'file' else 'original')
        state = runtime / 'rounds/T/occupied'
        state.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(FileExistsError):
            ex.prepare_runtime(runtime, 'T', 'occupied', '{}')
        self.assertFalse((runtime / 'worker-T.occupied.json').exists())

    def test_colliding_legacy_names_create_distinct_templates(self):
        runtime = self.root / 'runtime'
        runtime.mkdir()
        legacy = runtime / 'worker-T-A-r1.json'
        legacy.write_text('legacy unchanged')
        pairs = [('T', 'A-r1'), ('T-A', 'r1')]
        self.assertEqual(len({task + '-' + run for task, run in pairs}), 1)
        for task, run in pairs:
            content = json.dumps({'task': task, 'run': run})
            ex.prepare_runtime(runtime, task, run, content)
            template = runtime / ('worker-' + task + '.' + run + '.json')
            self.assertEqual(json.loads(template.read_text()), {'task': task, 'run': run})
        self.assertEqual(legacy.read_text(), 'legacy unchanged')

    def test_launcher_template_references_resolve_generated_files(self):
        # 执行生产脚本的两处路径表达式，覆盖只读授权和 OPENCODE_CONFIG。
        import re
        source = (Path(__file__).resolve().parents[1] / 'opencode-worker.sh').read_text()
        references = re.findall(r'"(\$OC/worker-[^"]+\.json)"', source)
        self.assertEqual(len(references), 2)
        runtime = self.root / 'runtime with spaces'
        for task, run in [('T', 'A-r1'), ('T-A', 'r1')]:
            ex.prepare_runtime(runtime, task, run, json.dumps([task, run]))
            for reference in references:
                result = subprocess.run(['/bin/bash', '-c',
                    'OC=$1; TASK_ID=$2; RUN_ID=$3; cat "' + reference + '"',
                    'test', str(runtime), task, run], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), [task, run])

    def test_round_runtime_is_separate_and_preserved(self):
        runtime = self.root / 'runtime'
        first = Path(ex.prepare_runtime(runtime, 'T', 'r1', '{}'))
        second = Path(ex.prepare_runtime(runtime, 'T', 'r2', '{}'))
        self.assertNotEqual(first, second)
        self.assertEqual({p.name for p in first.iterdir()}, {'config', 'data', 'state', 'cache', 'tmp'})
        with self.assertRaises(FileExistsError):
            ex.prepare_runtime(runtime, 'T', 'r1', '{}')

    def test_round_tmp_rejects_existing_nodes_and_link_ancestors(self):
        from frclyo import isolation
        sentinel = self.root / 'sentinel'
        sentinel.mkdir()
        for kind in ('directory', 'file', 'link', 'ancestor'):
            target = self.root / kind
            if kind == 'directory':
                target.mkdir()
            elif kind == 'file':
                target.write_text('UNCHANGED')
            elif kind == 'link':
                target.symlink_to(sentinel)
            else:
                target.symlink_to(sentinel)
                target = target / 'new-round'
            with mock.patch.object(isolation, 'round_tmp', return_value=target), \
                 self.assertRaises(OSError):
                isolation.prepare_round_tmp('/unused', '/unused')
            self.assertEqual(list(sentinel.iterdir()), [])
        self.assertEqual((self.root / 'file').read_text(), 'UNCHANGED')
        target = self.root / 'fresh-round'
        with mock.patch.object(isolation, 'round_tmp', return_value=target):
            self.assertEqual(isolation.prepare_round_tmp('/unused', '/unused'), str(target))
        for directory in [target] + list(target.iterdir()):
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)

    def test_codex_private_parent_is_owned_0700_and_round_is_exclusive(self):
        from frclyo import isolation
        parent = self.root / 'private-work'
        target = parent / 'r1'
        with mock.patch.object(isolation, 'codex_tmp', return_value=target):
            self.assertEqual(isolation.prepare_codex_tmp('/unused', '/unused'), str(target))
            with self.assertRaises(FileExistsError):
                isolation.prepare_codex_tmp('/unused', '/unused')
        self.assertEqual(parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(parent.stat().st_uid, os.getuid())
        with mock.patch.object(isolation, 'codex_tmp', return_value=parent / 'r2'):
            isolation.prepare_codex_tmp('/unused', '/unused')
        parent.chmod(0o755)
        with mock.patch.object(isolation, 'codex_tmp', return_value=parent / 'r3'), \
             self.assertRaises(ValueError):
            isolation.prepare_codex_tmp('/unused', '/unused')
        self.assertFalse((parent / 'r3').exists())
        self.assertEqual(parent.stat().st_mode & 0o777, 0o755)  # 不接管/修复已有父目录
        linked = self.root / 'linked-work'
        linked.symlink_to(parent)
        with mock.patch.object(isolation, 'codex_tmp', return_value=linked / 'r4'), \
             self.assertRaises(OSError):
            isolation.prepare_codex_tmp('/unused', '/unused')
        self.assertFalse((parent / 'r4').exists())

    def test_input_rejects_links_fifo_byte_and_entry_limits_without_partial_copy(self):
        for case in ('symlink', 'fifo', 'bytes', 'files'):
            with self.subTest(case=case):
                source = self.root / case
                source.mkdir()
                (source / 'a-good').write_bytes(b'good')
                if case == 'symlink':
                    (source / 'z-bad').symlink_to(source / 'a-good')
                elif case == 'fifo':
                    os.mkfifo(source / 'z-bad')
                else:
                    (source / 'z-file').write_bytes(b'x')
                target = self.root / (case + '-out')
                with self.assertRaises((OSError, ValueError)):
                    ex.copy_inputs(source, target, max_bytes=2 if case == 'bytes' else 100,
                                   max_files=1 if case == 'files' else 10)
                self.assertFalse(target.exists())
                self.assertEqual(list(self.root.glob('.input-*')), [])

    def test_copy_preserves_bytes_and_exec_bit_with_root_realpath(self):
        source = self.root / 'source'
        source.mkdir()
        (source / 'nested').mkdir()
        (source / 'nested/program').write_bytes(b'#!/bin/sh\n')
        (source / 'nested/program').chmod(0o755)
        alias = self.root / 'alias'
        alias.symlink_to(source, target_is_directory=True)
        output = self.root / 'input'
        ex.copy_inputs(alias, output)
        self.assertEqual((output / 'nested/program').read_bytes(), b'#!/bin/sh\n')
        self.assertTrue((output / 'nested/program').stat().st_mode & 0o100)

    def test_copy_failure_midway_removes_staging(self):
        source = self.root / 'source'
        source.mkdir()
        (source / 'a').write_bytes(b'first')
        (source / 'b').write_bytes(b'second')
        original = ex.os.read
        count = [0]
        def read(fd, limit):
            count[0] += 1
            if count[0] == 3:
                raise OSError('injected midway')
            return original(fd, limit)
        with mock.patch.object(ex.os, 'read', side_effect=read):
            with self.assertRaises(OSError):
                ex.copy_inputs(source, self.root / 'out')
        self.assertFalse((self.root / 'out').exists())
        self.assertEqual(list(self.root.glob('.input-*')), [])

    def test_copy_rejects_scan_to_copy_symlink_swap(self):
        source = self.root / 'source'
        source.mkdir()
        (source / 'data').write_bytes(b'old')
        sentinel = self.root / 'sentinel'
        sentinel.write_bytes(b'do not copy')
        original = ex.os.mkdir
        def mkdir(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if str(path).startswith('.input-'):
                (source / 'data').unlink()
                (source / 'data').symlink_to(sentinel)
            return result
        with mock.patch.object(ex.os, 'mkdir', side_effect=mkdir):
            with self.assertRaises(OSError):
                ex.copy_inputs(source, self.root / 'out')
        self.assertFalse((self.root / 'out').exists())
        self.assertEqual(list(self.root.glob('.input-*')), [])


class ProcessGroupTests(unittest.TestCase):
    def test_supervisor_enters_task_directory_before_cli_initialization(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            run = root / '交付/TASK/r1'
            run.mkdir(parents=True)
            task = run / '任务.txt'
            task.write_text('fixture')
            control = root / 'control'
            control.mkdir()
            caller = Path.cwd()
            self.assertNotEqual(caller, run)
            rc = ex.supervise([sys.executable, '-I', '-c', 'import os; print(os.getcwd())'],
                              task, control, 5, os.getppid(), 124)
            self.assertEqual(rc, 0)
            self.assertEqual(Path((control / 'events.jsonl').read_text().strip()), run)
            self.assertEqual(Path.cwd(), caller)

    def test_timeout_reaps_parent_and_term_resistant_descendant(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            task = root / 'task'
            task.write_text('fake')
            control = root / 'control'
            control.mkdir()
            code = '''import os, signal, time
pid = os.fork()
if pid == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    print('child=' + str(os.getpid()), flush=True)
else:
    print('parent=' + str(os.getpid()), flush=True)
while True: time.sleep(.1)
'''
            rc = ex.supervise([sys.executable, '-I', '-c', code], task, control,
                              0.3, os.getppid(), 124)
            self.assertEqual(rc, 124)
            state = json.loads(ex.safe_read(control / 'status.json'))
            self.assertTrue(state['timed_out'])
            self.assertTrue(state['group_reaped'])
            lines = ex.safe_read(control / 'events.jsonl').decode().splitlines()
            pgid = int(next(line.split('=')[1] for line in lines if line.startswith('parent=')))
            # ps 只报告状态；僵尸已经不可执行，不能误判为仍存活。
            snapshot = subprocess.run(['/bin/ps', '-axo', 'pgid=,stat='], capture_output=True,
                                      text=True, check=True)
            self.assertFalse(any(int(row.split()[0]) == pgid and not row.split()[1].startswith('Z')
                                 for row in snapshot.stdout.splitlines()))

    def test_capture_is_bounded_and_status_is_not_worker_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / 'task').write_text('fake')
            control = root / 'control'; control.mkdir()
            with mock.patch.object(ex, 'LOG_LIMIT', 1024):
                rc = ex.supervise([sys.executable, '-I', '-c',
                                   'import sys; sys.stdout.write("x"*4096); sys.stdout.flush()'],
                                  root / 'task', control, 3, os.getppid(), 124)
            self.assertEqual(rc, 125)
            self.assertLessEqual((control / 'events.jsonl').stat().st_size, 1024)
            self.assertFalse(json.loads(ex.safe_read(control / 'status.json'))['timed_out'])


class ProxyHandoffTests(unittest.TestCase):
    def test_key_only_crosses_pipe_not_argv_environment_or_files(self):
        from frclyo import keys
        key = 'FAKE-HANDOFF-KEY'
        handoff = []
        def dup2(fd, target, inheritable):
            self.assertEqual(target, 0)
            handoff.append(os.read(fd, 4096))
        # exec 用 BaseException 中断，保留成功交接语义；真实匿名 pipe 仍被测试。
        with mock.patch.object(keys, 'get_key', return_value=key), \
             mock.patch.object(ex.os, 'getppid', return_value=123), \
             mock.patch.object(ex.os, 'dup2', side_effect=dup2), \
             mock.patch.object(ex.os, 'execve', side_effect=SystemExit) as execute:
            with self.assertRaises(SystemExit):
                ex.start_proxy('deepseek', '/private/control', '123')
        self.assertEqual(handoff, [(key + '\n').encode()])
        args = execute.call_args.args
        self.assertNotIn(key, repr(args))
        self.assertEqual(args[2], {})
        self.assertIn('--provider', args[1])
        self.assertNotIn('--key', args[1])

    def test_missing_key_fails_without_exec_or_secret_error(self):
        import io
        from frclyo import keys
        with mock.patch.object(keys, 'get_key', side_effect=ValueError('FAKE-KEY')), \
             mock.patch.object(ex.os, 'getppid', return_value=123), \
             mock.patch.object(ex.os, 'execve') as execute, \
             mock.patch.object(sys, 'stderr', new_callable=io.StringIO) as error:
            self.assertEqual(ex.start_proxy('deepseek', '/private/control', '123'), 2)
        execute.assert_not_called()
        self.assertNotIn('FAKE-KEY', error.getvalue())

    def test_worker_not_started_never_claims_group_reaped(self):
        with mock.patch.object(ex.os, 'getppid', return_value=999), \
             mock.patch.object(ex, 'write_json') as write, \
             mock.patch.object(ex.subprocess, 'Popen') as popen:
            self.assertEqual(ex.supervise(['fake'], '/unused', '/unused', 1, 123, 142, provider='deepseek'), 125)
        popen.assert_not_called()
        self.assertIs(write.call_args.args[1]['group_reaped'], False)

    def test_result_limit_default_override_and_invalid_values(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(ex.result_limit(), 16 * 1024 * 1024)
        for value in ('32', '33554432'):
            with mock.patch.dict(os.environ, WORKER_RESULT_MAX_BYTES=value):
                self.assertEqual(ex.result_limit(), int(value))
        for value in ('0', '-1', 'oops', '1.5', ' 12', '9' * 80):
            with mock.patch.dict(os.environ, WORKER_RESULT_MAX_BYTES=value), self.assertRaises(ValueError):
                ex.result_limit()


class PostprocessingTests(unittest.TestCase):
    """内存替身只测后处理决策；真实文件/链接安全由 FileBoundaryTests 单独覆盖。"""
    def finalize(self, worker='codex', result=b'OK', reaped=True, timed_out=False, events=None, fallback=0):
        from frclyo import models
        run, control = Path('/virtual/run'), Path('/virtual/control')
        events = events if events is not None else b'{"type":"turn.completed"}\n'
        memory = {control / 'status.json': json.dumps(dict(rc=124 if timed_out else 0,
            timed_out=timed_out, group_reaped=reaped, errors=[])).encode(),
            control / 'project.json': json.dumps('项目甲').encode(),
            control / 'events.jsonl': events, control / 'stderr.log': b'',
            control / '代理.stdout': '12345\n{"拒绝次数":0}\n'.encode(),
            run / 'events.jsonl': b'{"type":"error","error":{"code":"model_not_supported"}}'}
        if result is not None:
            memory[run / '结果.txt'] = result
        reads, writes, receipts = [], {}, {}
        def read(path, limit=ex.RESULT_LIMIT):
            path = Path(path)
            reads.append(path)
            if path.name == '代理.stdout':
                self.assertEqual(limit, ex.LOG_LIMIT)
            if path.name == '结果.txt':
                self.assertEqual(limit, ex.result_limit())
            value = memory.get(path)
            if value is None:
                raise FileNotFoundError(path)
            if isinstance(value, Exception):
                raise value
            return value
        def observe(*args, **kwargs):
            self.assertEqual(kwargs['captured'], events)
            return 'ok' if kwargs['succeeded'] else 'unknown'
        with mock.patch.object(ex, 'safe_read', side_effect=read), \
             mock.patch.object(ex, 'atomic_write', side_effect=lambda p, d: writes.__setitem__(Path(p), d)), \
             mock.patch.object(ex, 'write_json', side_effect=lambda p, d: receipts.__setitem__(Path(p), dict(d))), \
             mock.patch.object(models, 'observe', side_effect=observe) as observation:
            rc = ex.finalize(worker, run, control, 'T', 'r1', 'fake', 'high', 'start', 'end', 'hash',
                             target='api.example.com:443', fallback=fallback)
        return rc, receipts[run / '回执.json'], reads, writes, observation.call_count

    def test_b14_final_receipt_keeps_project_for_both_workers(self):
        # 从可信 project.json 到最终回执；只测摘要函数会漏掉回执字段被删除。
        for worker, events in (('codex', b'{"type":"turn.completed"}\n'),
                               ('opencode', b'{"type":"text","part":{"text":"OK"}}\n')):
            with self.subTest(worker=worker):
                rc, receipt, _, _, _ = self.finalize(worker, events=events)
                self.assertEqual(rc, 0)
                self.assertEqual(receipt['项目'], '项目甲')
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    ex.receipt_summary('/virtual/control', '/virtual/run', receipt)
                self.assertIn('【项目 项目甲】T r1 执行完成', output.getvalue())

    def test_project_parser_and_summary_without_filesystem(self):
        for card, expected in [('项目：甲\n内容', '甲'), ('标题\n项目：乙', ''), ('', '')]:
            with mock.patch.object(ex, 'safe_read', return_value=(
                    '守则\n\n---\n\n' + card).encode()):
                self.assertEqual(ex.task_project('/virtual/run'), expected)
        for worker in ('codex', 'opencode'):
            for status in ('执行完成', '失败', '超时'):
                receipt = {'工人': worker, '项目': '甲', '任务编号': 'T',
                           '轮次编号': 'r1', '状态': status}
                with mock.patch.object(ex, 'safe_read', return_value=json.dumps(receipt).encode()), \
                     contextlib.redirect_stdout(io.StringIO()) as output:
                    ex.receipt_summary('/virtual/control', '/virtual/run')
                self.assertEqual(output.getvalue(),
                    '【项目 甲】T r1 %s（回执：/virtual/run/回执.json）\n' % status)

    def test_unreaped_group_never_reads_worker_result_or_updates_models(self):
        rc, receipt, reads, writes, observed = self.finalize(reaped=False)
        self.assertEqual(rc, 1)
        self.assertEqual(receipt['状态'], '失败')
        self.assertEqual(reads, [Path('/virtual/control/status.json'), Path('/virtual/control/project.json')])
        self.assertEqual(receipt['项目'], '项目甲')
        self.assertEqual((writes, observed), ({}, 0))

    def test_rejected_result_fails_without_hash(self):
        rc, receipt, reads, writes, observed = self.finalize(result=OSError('symlink rejected'))
        self.assertEqual(rc, 1)
        self.assertEqual(receipt['状态'], '失败')
        self.assertIsNone(receipt['结果sha256'])
        self.assertTrue(receipt['错误'])
        self.assertNotIn(Path('/virtual/run/结果.txt'), writes)

    def test_worker_event_file_is_never_read(self):
        rc, receipt, reads, writes, observed = self.finalize()
        self.assertEqual(rc, 0)
        self.assertEqual(receipt['模型可用性'], 'ok')
        self.assertNotIn(Path('/virtual/run/events.jsonl'), reads)
        self.assertEqual(writes[Path('/virtual/run/events.jsonl')], b'{"type":"turn.completed"}\n')

    def test_timeout_comes_from_private_supervisor_state(self):
        rc, receipt, reads, writes, observed = self.finalize(timed_out=True)
        self.assertEqual(rc, 124)
        self.assertEqual(receipt['状态'], '超时')
        self.assertFalse(any(path.name == '.超时' for path in reads))

    def test_shell_interruption_after_worker_exit_is_not_reported_as_success(self):
        rc, receipt, reads, writes, observed = self.finalize(fallback=143)
        self.assertEqual(rc, 143)
        self.assertEqual(receipt['状态'], '失败')
        rc, receipt, reads, writes, observed = self.finalize(timed_out=True, fallback=143)
        self.assertEqual(rc, 124)
        self.assertEqual(receipt['状态'], '超时')

    def test_opencode_result_comes_from_host_capture(self):
        event = b'{"type":"text","part":{"text":"HOST"}}\n'
        rc, receipt, reads, writes, observed = self.finalize('opencode', result=None, events=event)
        self.assertEqual(rc, 0)
        self.assertEqual(writes[Path('/virtual/run/结果.txt')], b'HOST\n')
        self.assertEqual(receipt['网络'], {'放行': ['api.example.com:443'], '拒绝次数': 0})

    def test_configured_limit_applies_to_generated_result(self):
        event = b'{"type":"text","part":{"text":"HOST"}}\n'
        with mock.patch.dict(os.environ, WORKER_RESULT_MAX_BYTES='4'):
            rc, receipt, reads, writes, observed = self.finalize('opencode', result=None, events=event)
        self.assertEqual(rc, 1)
        self.assertNotIn(Path('/virtual/run/结果.txt'), writes)
        with mock.patch.dict(os.environ, WORKER_RESULT_MAX_BYTES='5'):
            self.assertEqual(self.finalize('opencode', result=None, events=event)[0], 0)

    def test_opencode_rejected_result_is_not_overwritten_with_text(self):
        event = b'{"type":"text","part":{"text":"HOST"}}\n'
        rc, receipt, reads, writes, observed = self.finalize('opencode', result=OSError('link'), events=event)
        self.assertEqual(rc, 1)
        self.assertEqual(receipt['状态'], '失败')
        self.assertNotIn(Path('/virtual/run/结果.txt'), writes)


class OpenCodeReceiptTests(unittest.TestCase):
    """B67/B69/B70：合成已回收状态，真实解析和落盘；不调用模型或真实代理。"""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.run, self.control = self.root / 'run', self.root / 'control'
        self.run.mkdir()
        self.control.mkdir()

    def finish(self, raw, child_rc=0):
        from frclyo import models
        # 短命替身输出原始字节；只替换模型观察器，回执序列化使用真实实现。
        process = subprocess.run([sys.executable, '-I', '-c',
            'import sys; sys.stdout.buffer.write(' + repr(raw) + '); sys.exit(' + str(child_rc) + ')'],
            cwd=self.run, capture_output=True, timeout=10)
        ex.write_json(self.control / 'status.json', dict(rc=process.returncode,
                      timed_out=False, group_reaped=True, errors=[]))
        (self.control / 'events.jsonl').write_bytes(process.stdout)
        (self.control / 'stderr.log').write_bytes(process.stderr)
        (self.control / '代理.stdout').write_text('42345\n{"拒绝次数":0}\n', encoding='utf-8')
        with mock.patch.object(models, 'observe', return_value='unknown') as observe:
            rc = ex.finalize('opencode', self.run, self.control, 'TEST', 'r1',
                             'fake/model', '', 'start', 'end', 'fixture-sha')
        receipt = json.loads((self.control / 'receipt.json').read_text(encoding='utf-8'))
        self.assertEqual(json.loads((self.run / '回执.json').read_text(encoding='utf-8')), receipt)
        self.assertEqual(receipt['退出码'], rc)
        self.assertEqual((self.control / 'events.jsonl').read_bytes(), raw)
        self.assertEqual((self.run / 'events.jsonl').read_bytes(), raw)
        self.assertEqual(observe.call_args.kwargs['captured'], raw)
        return rc, receipt, observe.call_args.kwargs['succeeded']

    def test_b67_surrogate_error_still_writes_both_failure_receipts(self):
        # JSON 转义的孤立高/低代理码点不能使两份失败回执丢失，退出 0 也要失败。
        for surrogate in ('\ud800', '\udfff'):
            for child_rc in (0, 1):
                with self.subTest(surrogate=repr(surrogate), child_rc=child_rc):
                    raw = (json.dumps({'type': 'error', 'error': {
                        'name': 'APIError', 'message': 'upstream: ' + surrogate}}) + '\n').encode()
                    rc, receipt, succeeded = self.finish(raw, child_rc)
                    self.assertNotEqual(rc, 0)
                    self.assertEqual(receipt['状态'], '失败')
                    self.assertIn(surrogate.encode('unicode_escape').decode(), receipt['错误'][0])
                    self.assertFalse(succeeded)

    def test_b94_invalid_usage_key_preserves_failure_receipts(self):
        # 坏 UTF-8 和 JSON 代理码点用量键均可落盘；同名文字转义键不能碰撞丢数。
        for key in (b'output\xff', b'output\\ud800'):
            with self.subTest(key=key):
                raw = (b'{"type":"text","part":{"text":"OK"}}\n'
                       b'{"type":"step_finish","part":{"tokens":{"input":1,"' + key +
                       b'":2,"output\\\\udcff":3}}}\n')
                rc, receipt, succeeded = self.finish(raw, child_rc=0 if b'\xff' in key else 1)
                self.assertNotEqual(rc, 0)
                self.assertEqual(receipt['状态'], '失败')
                self.assertFalse(succeeded)
                decoded_key = json.loads((b'{"' + key + b'":0}').decode('utf-8', 'surrogateescape'))
                self.assertEqual(receipt['用量'], {'input': 1, next(iter(decoded_key)): 2,
                                                  r'output\udcff': 3})
                if b'\xff' in key:
                    self.assertTrue(any('编码错误' in error for error in receipt['错误']))

    def test_b94_codex_nested_event_strings_are_json_encodable(self):
        # Codex 的 threadId、任意嵌套用量键和值也经过统一 JSON 编码边界。
        from frclyo import models
        usage = {'键\ud800': [{'值': '中文🙂\udfff'}], r'键\ud800': 7}
        raw = ('\n'.join(json.dumps(event) for event in (
            {'type': 'thread.started', 'thread_id': 'thread\ud800'},
            {'type': 'turn.completed', 'usage': usage})) + '\n').encode()
        ex.write_json(self.control / 'status.json', dict(rc=1, timed_out=False,
                      group_reaped=True, errors=[]))
        (self.control / 'events.jsonl').write_bytes(raw)
        (self.control / 'stderr.log').write_bytes(b'')
        (self.run / '结果.txt').write_text('OK\n', encoding='utf-8')
        with mock.patch.object(models, 'observe', return_value='unknown'):
            self.assertNotEqual(ex.finalize('codex', self.run, self.control, 'TEST', 'r1',
                                           'fake', '', 'start', 'end', 'sha'), 0)
        trusted = json.loads((self.control / 'receipt.json').read_text(encoding='utf-8'))
        self.assertEqual(json.loads((self.run / '回执.json').read_text(encoding='utf-8')), trusted)
        self.assertEqual(trusted['threadId'], 'thread\ud800')
        self.assertEqual(trusted['用量'], usage)
        self.assertEqual((self.run / 'events.jsonl').read_bytes(), raw)

    def test_b69_invalid_utf8_fails_and_preserves_result_bytes(self):
        # 单字节、截断和非法代理编码均须失败；最后正文保持原始字节，不替换成 U+FFFD。
        for invalid in (b'\xff', b'\xe4\xb8', b'\xed\xa0\x80'):
            with self.subTest(invalid=invalid):
                raw = (b'{"type":"text","part":{"text":"earlier valid text"}}\n'
                       b'{"type":"text","part":{"text":"amount=' + invalid + b'"}}\n'
                       b'{"type":"step_finish","part":{"reason":"stop","tokens":{"output":1}}}\n')
                rc, receipt, succeeded = self.finish(raw)
                self.assertNotEqual(rc, 0)
                self.assertEqual(receipt['状态'], '失败')
                self.assertTrue(any('编码错误' in error and 'UTF-8' in error for error in receipt['错误']))
                self.assertFalse(succeeded)
                self.assertEqual((self.run / '结果.txt').read_bytes(), b'amount=' + invalid + b'\n')

    def test_valid_unicode_including_replacement_character_remains_successful(self):
        # 合法中文、emoji 和显式 U+FFFD 都是正常正文，不能误判成编码错误。
        raw = (json.dumps({'type': 'text', 'part': {'text': '中文🙂�'}}, ensure_ascii=False) + '\n').encode()
        rc, receipt, succeeded = self.finish(raw)
        self.assertEqual(rc, 0)
        self.assertEqual(receipt['状态'], '执行完成')
        self.assertEqual(receipt['错误'], [])
        self.assertTrue(succeeded)
        self.assertEqual((self.run / '结果.txt').read_bytes(), '中文🙂�\n'.encode())

    def test_b70_nested_cache_usage_accumulates_across_steps(self):
        # 原复现约定：两步嵌套缓存读写分别累计到 300/50，其余用量保持原值。
        events = [
            {'type': 'step_finish', 'part': {'reason': 'tool-calls', 'tokens': {
                'input': 10, 'output': 2, 'reasoning': 1, 'cache': {'read': 100, 'write': 20}}}},
            {'type': 'text', 'part': {'text': 'OK'}},
            {'type': 'step_finish', 'part': {'reason': 'stop', 'tokens': {
                'input': 3, 'output': 4, 'reasoning': 2, 'cache': {'read': 200, 'write': 30}}}}]
        raw = b''.join((json.dumps(event) + '\n').encode() for event in events)
        rc, receipt, succeeded = self.finish(raw)
        self.assertEqual(rc, 0)
        self.assertTrue(succeeded)
        self.assertEqual(receipt['用量'], {'input': 13, 'output': 6, 'reasoning': 3,
                                          'cached_input_tokens': 300, 'cache_write_input_tokens': 50})

    def test_b70_missing_cache_and_zero_counts(self):
        # 没有缓存和零计数均合法；只读、只写的步各自累计，不互相覆盖。
        events = [{'type': 'text', 'part': {'text': 'OK'}}]
        for tokens in ({'input': 1}, {'cache': {'read': 0}}, {'cache': {'write': 0}},
                       {'cache': {'read': 5}}, {'cache': {'write': 2}}):
            events.append({'type': 'step_finish', 'part': {'tokens': tokens}})
        raw = b''.join((json.dumps(event) + '\n').encode() for event in events)
        rc, receipt, succeeded = self.finish(raw)
        self.assertEqual(rc, 0)
        self.assertTrue(succeeded)
        self.assertEqual(receipt['用量'], {'input': 1, 'cached_input_tokens': 5, 'cache_write_input_tokens': 2})

    def test_invalid_model_bytes_do_not_prevent_failure_receipt(self):
        # 编码诊断后模型标签也必须可序列化，不能在回执最后落盘时再次失败。
        raw = b'{"type":"text","part":{"text":"OK","modelID":"bad\xff"}}\n'
        rc, receipt, succeeded = self.finish(raw)
        self.assertNotEqual(rc, 0)
        self.assertEqual(receipt['状态'], '失败')
        self.assertFalse(succeeded)
        self.assertEqual(receipt['事件中出现的模型'], [r'bad\udcff'])


class DetachedDescendantTests(unittest.TestCase):
    def test_detached_descendant_is_reaped(self):
        self.detached_descendant(0.5, 8, True)

    def test_normal_exit_also_reaps_detached_descendant(self):
        self.detached_descendant(2, 0.3, False)

    def detached_descendant(self, seconds, lifetime, timed_out):
        probe = ex.Descendants(mock.Mock(pid=-1, poll=lambda: 0))
        if probe.sample() is None:
            self.skipTest('沙箱无法取得进程快照；需宿主验证 B44')
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as tmp:
            root = Path(tmp)
            control = root / 'control'
            control.mkdir()
            task = root / 'task.txt'
            task.write_text('offline fixture')
            pidfile = root / 'descendant.pid'
            code = '''import subprocess, sys, time
from pathlib import Path
p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(8)'],
    start_new_session=True, stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
Path(sys.argv[1]).write_text(str(p.pid))
time.sleep(float(sys.argv[2]))
'''
            pid = None
            try:
                rc = ex.supervise([sys.executable, '-c', code, str(pidfile), str(lifetime)],
                    task, control, seconds, os.getppid(), 124)
                pid = int(pidfile.read_text())
                status = json.loads((control / 'status.json').read_text())
                try:
                    os.kill(pid, 0)
                    alive = True
                except ProcessLookupError:
                    alive = False
                print(f'rc={rc}; timed_out={status["timed_out"]}; '
                      f'group_reaped={status["group_reaped"]}; descendant_alive={alive}', flush=True)
                self.assertFalse(alive, '超时返回后，另建 session 的后代仍存活')
                self.assertTrue(status['group_reaped'])
                self.assertEqual(status['timed_out'], timed_out)
                self.assertEqual(rc, 124 if timed_out else 0)
            finally:
                if pid is None and pidfile.exists():
                    pid = int(pidfile.read_text())
                if pid is not None:
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                print('fixture cleanup: detached descendant kill requested', flush=True)


class DescendantIdentityTests(unittest.TestCase):
    def test_detached_child_stays_tracked_after_reparenting(self):
        child = mock.Mock(pid=10, poll=lambda: 0)
        tracker = ex.Descendants(child)
        first = mock.Mock(stdout='10 1 10 S parent\n20 10 20 S child\n30 20 30 S grandchild\n')
        orphaned = mock.Mock(stdout='20 1 20 S child\n30 1 30 S grandchild\n')
        gone = mock.Mock(stdout='1 0 1 S init\n')
        with mock.patch.object(ex.subprocess, 'run', side_effect=[first, orphaned, gone]), \
                mock.patch.object(ex.os, 'kill') as kill:
            tracker.sample()
            self.assertTrue(tracker.running(signal.SIGKILL))
            self.assertFalse(tracker.running())
        self.assertEqual(kill.call_args_list, [mock.call(20, signal.SIGKILL), mock.call(30, signal.SIGKILL)])
        self.assertTrue(tracker.complete)

    def test_reused_pid_is_not_signalled(self):
        child = mock.Mock(pid=10, poll=lambda: 0)
        tracker = ex.Descendants(child)
        tracker.known[20] = 'old start'
        snapshot = mock.Mock(stdout='20 1 20 S new start\n')
        with mock.patch.object(ex.subprocess, 'run', return_value=snapshot), mock.patch.object(ex.os, 'kill') as kill:
            self.assertFalse(tracker.running(signal.SIGTERM))
        kill.assert_not_called()

    def test_b93_snapshot_failure_recovers_and_reaps_known_descendant(self):
        # 成功记录身份后短暂失败，下一次恢复仍回收脱组后代；诊断仅记一次。
        for failure in (subprocess.TimeoutExpired('synthetic ps', 0.5),
                        subprocess.CalledProcessError(1, 'synthetic ps'), OSError('synthetic ps')):
            with self.subTest(failure=type(failure).__name__):
                child = mock.Mock(spec=['pid', 'poll', 'wait'], pid=10)
                child.poll.return_value = 0
                tracker = child._frclyo_descendants = ex.Descendants(child)
                state = {'alive': True, 'snapshots': 0}

                def snapshot(*args, **kwargs):
                    state['snapshots'] += 1
                    if state['snapshots'] in (2, 3):
                        raise failure
                    rows = '1 0 1 S init-start\n10 1 10 Z worker-start\n'
                    if state['alive']:
                        rows += '20 10 20 S descendant-start\n'
                    return subprocess.CompletedProcess(args[0], 0, rows)

                def kill(pid, sig):
                    self.assertEqual(pid, 20)
                    if sig in (signal.SIGTERM, signal.SIGKILL):
                        state['alive'] = False

                with mock.patch.object(ex.subprocess, 'run', side_effect=snapshot), \
                        mock.patch.object(ex.os, 'kill', side_effect=kill) as signals, \
                        mock.patch.object(ex, 'signal_group', return_value=False), \
                        mock.patch.object(ex, 'group_running', return_value=False), \
                        mock.patch.object(ex.time, 'monotonic', side_effect=range(20)), \
                        mock.patch.object(ex.time, 'sleep'), \
                        contextlib.redirect_stderr(io.StringIO()) as diagnostic:
                    self.assertIsNotNone(tracker.sample())
                    self.assertEqual(tracker.known[20], 'descendant-start')
                    self.assertIsNone(tracker.sample())
                    self.assertIsNone(tracker.sample())
                    self.assertTrue(ex.reap_group(child, grace=0))
                self.assertFalse(state['alive'])
                self.assertGreater(state['snapshots'], 3)
                self.assertIn(mock.call(20, signal.SIGTERM), signals.call_args_list)
                self.assertEqual(diagnostic.getvalue().count('快照失败'), 1)

    def test_b93_recovered_snapshot_does_not_signal_reused_pid(self):
        # 失败恢复后也必须核对启动时间，不能向复用同一 PID 的无关进程发信号。
        child = mock.Mock(pid=10, poll=lambda: 0)
        tracker = child._frclyo_descendants = ex.Descendants(child)
        tracker.known[20] = 'old start'
        with mock.patch.object(ex.subprocess, 'run', side_effect=[
                subprocess.TimeoutExpired('synthetic ps', 0.5),
                mock.Mock(stdout='20 1 20 S new start\n')]), \
                mock.patch.object(ex.os, 'kill') as kill:
            self.assertIsNone(tracker.sample())
            self.assertFalse(tracker.running(signal.SIGKILL))
        kill.assert_not_called()
        self.assertTrue(tracker.complete)

    def test_failed_snapshot_never_confirms_reaping(self):
        child = mock.Mock(pid=10, poll=lambda: 0)
        with mock.patch.object(ex.subprocess, 'run', side_effect=PermissionError), \
                mock.patch.object(ex, 'signal_group', return_value=False), \
                mock.patch.object(ex, 'group_running', return_value=False), \
                mock.patch.object(ex.time, 'sleep'):
            tracker = child._frclyo_descendants = ex.Descendants(child)
            self.assertIsNone(tracker.sample())
            with mock.patch.object(ex.time, 'monotonic', side_effect=range(20)):
                self.assertFalse(ex.reap_group(child, grace=0))



if __name__ == '__main__':
    unittest.main()
