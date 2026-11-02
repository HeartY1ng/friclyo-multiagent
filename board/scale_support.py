#!/usr/bin/env python3
"""B71～B73 回归夹具：200 卡、349 轮、约 1 MiB 日志/轮；无真实凭据。"""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import resource
import sys
import time
from unittest.mock import patch


def write_json(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False) + '\n', encoding='utf-8')


def iso(t):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(t))


def generate(root, cards=200, rounds=150, log_kib=1024, worker='mixed'):
    root = Path(root).resolve()
    root.mkdir()  # 不覆盖已有数据
    for name in ('交付', '任务卡', '配置', '运行时'):
        (root / name).mkdir()
    now = int(time.time())
    result = ('## 1. 结论\n完成离线检查。\n' + '检查记录：输入样例与输出一致。\n' * 170 +
              '## 4. 决策披露\n使用本地测试替身。\n## 5. 状态\n状态：完成\n').encode()
    card = ('项目：性能实验\n# 任务卡：离线可靠性测试\n' + '核对输入格式、样例输出和回归结果。\n' * 65).encode()
    output = '\n'.join('case %03d passed; input and output checked locally.' % i for i in range(20))
    logs = {}
    for w in ('opencode', 'codex'):
        lines = []
        for i in range(10000):
            if w == 'opencode':
                event = {'type': 'tool_use', 'timestamp': (now - 1000 + i) * 1000,
                         'part': {'tool': 'bash', 'state': {'status': 'completed',
                                  'input': {'command': 'python3 -m unittest test_case_%d' % i}, 'output': output}}}
                cost = {'type': 'step_finish', 'part': {'cost': 0.0001}}
                line = json.dumps(event) + '\n' + json.dumps(cost) + '\n'
            else:
                event = {'type': 'item.completed', 'item': {'type': 'command_execution',
                         'command': 'python3 -m unittest test_case_%d' % i,
                         'exit_code': 0, 'aggregated_output': output}}
                line = json.dumps(event) + '\n'
            lines.append(line)
            if sum(map(len, lines)) >= log_kib * 1024:
                break
        logs[w] = ''.join(lines).encode()
    ledger, perf = [], []
    total_rounds = 0
    counts = {'opencode': 0, 'codex': 0}
    for i in range(cards):
        task = 'CL-20991231-%02d' % (i + 1)
        n = rounds if i == 0 else 1
        start = now - 86400 - i * 60
        ledger.append({'时间': iso(start), '动作': '开卡', '任务': task,
                       '标题': '性能测试卡 %d' % (i + 1), '项目': '性能实验', '来源': 'CL',
                       '派活人': '离线替身', '父任务': ''})
        for j in range(1, n + 1):
            w = worker if worker != 'mixed' else ('opencode' if i % 2 == 0 else 'codex')
            counts[w] += 1
            rnd = 'r%d' % j
            d = root / '交付' / task / rnd
            d.mkdir(parents=True)
            info = {'工人': w, '模型': 'local-fixture/model', '档位': 'high',
                    '开始': iso(start + j * 60), '超时秒': 7200, '进程号': 0}
            rec = dict(info, 状态='执行完成', 结束=iso(start + j * 60 + 30),
                       退出码=0, 用量={'input_tokens': 12000, 'output_tokens': 3000},
                       结果sha256=hashlib.sha256(result).hexdigest())
            write_json(d / '运行信息.json', info)
            write_json(d / '回执.json', rec)
            (d / '任务.txt').write_bytes(card)
            (root / '任务卡' / ('%s-%s.txt' % (task, rnd))).write_bytes(card)
            (d / '结果.txt').write_bytes(result)
            (d / 'events.jsonl').write_bytes(logs[w])
            (d / 'stderr.log').write_bytes(b'fixture diagnostic: completed\n' * 32)
            (d / 'artifact.txt').write_bytes(b'local fixture artifact\n' * 200)
            perf.append({'任务号': task, '轮次': rnd, '通过验收': True})
            total_rounds += 1
        ledger.append({'时间': iso(start + n * 60 + 31), '动作': '完成', '任务': task,
                       '结论': '通过', '摘要': '离线合成数据，哈希匹配'})
    for filename, rows in (('账本.jsonl', ledger), ('表现记录.jsonl', perf)):
        (root / filename).write_text(''.join(json.dumps(x, ensure_ascii=False) + '\n' for x in rows), encoding='utf-8')
    write_json(root / '配置' / '项目表.json', {'项目': {'性能实验': {'负责对话': '本地实验'}}})
    manifest = {'cards': cards, 'rounds': total_rounds, 'large_card_rounds': rounds,
                'workers': counts, 'log_bytes_per_round': {k: len(v) for k, v in logs.items()},
                'result_bytes': len(result), 'task_bytes': len(card), 'generated_at': now,
                'note': '按产品事件格式合成；输入未提供真实日志大小分布，1 MiB 为声明的基准假设'}
    write_json(root / 'manifest.json', manifest)
    return manifest



@contextlib.contextmanager
def board(source, data):
    source, data = Path(source).resolve(), Path(data).resolve()
    sys.path.insert(0, str(source / 'bin'))
    from frclyo import config, keys
    paths = {'deliver_dir': data / '交付', 'cards_dir': data / '任务卡',
             'ledger_file': data / '账本.jsonl', 'perf_file': data / '表现记录.jsonl',
             'board_log_file': data / '运行时/看板.log', 'config_dir': data / '配置'}
    cfg = {'board_port': 18799, 'codex': {}, 'providers': {}}
    def forbidden(*args, **kwargs):
        raise AssertionError('基准不允许查询凭据/外部服务')
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(config, 'load', return_value=cfg))
        stack.enter_context(patch.object(config, 'path_of', side_effect=lambda k: str(paths[k])))
        stack.enter_context(patch.object(keys, 'get_key', side_effect=forbidden))
        stack.enter_context(patch.object(keys, 'key_source', side_effect=forbidden))
        spec = importlib.util.spec_from_file_location('bench_board', source / 'board/board.py')
        b = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(b)
        stack.enter_context(patch.object(b, 'check_codex_quota', return_value={'已用': 0, '更新时间': time.time()}))
        stack.enter_context(patch.object(b, 'check_balances', return_value=[]))
        yield b


class Reads:
    def __init__(self, b):
        self.b = b
        self.counts = {}
        self.paths = set()

    def __enter__(self):
        original = self.b.safe_read
        def read(path, *args, **kwargs):
            start = time.perf_counter()
            data = original(path, *args, **kwargs)
            name = '任务卡/*.txt' if Path(path).parent == Path(self.b.RUNS) else Path(path).name
            row = self.counts.setdefault(name, {'calls': 0, 'bytes': 0, 'seconds': 0})
            row['calls'] += 1
            row['bytes'] += len(data)
            row['seconds'] += time.perf_counter() - start
            self.paths.add(str(path))
            return data
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(patch.object(self.b, 'safe_read', new=read))
        self.stack.enter_context(patch.object(self.b.audit, 'safe_read', new=read))
        return self

    def __exit__(self, *args):
        self.stack.close()


def memory():
    # macOS ru_maxrss 为字节，Linux 为 KiB；峰值不会随 GC 降低。
    v = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return {'peak_rss_mib': round(v / (1024 ** 2 if sys.platform == 'darwin' else 1024), 3)}


def deep_size(obj):
    seen = set()
    def walk(x):
        if id(x) in seen:
            return 0
        seen.add(id(x))
        n = sys.getsizeof(x)
        if isinstance(x, dict):
            n += sum(walk(k) + walk(v) for k, v in x.items())
        elif isinstance(x, (list, tuple, set)):
            n += sum(walk(v) for v in x)
        return n
    return walk(obj)


def measure(b, name, fn):
    before = memory()
    with Reads(b) as reads:
        start = time.perf_counter()
        cpu = time.process_time()
        value = fn()
        elapsed = time.perf_counter() - start
        cpu = time.process_time() - cpu
    row = {'name': name, 'seconds': round(elapsed, 6), 'cpu_seconds': round(cpu, 6),
           'memory_before': before, 'memory_after': memory(), 'reads': reads.counts}
    return value, row


def emit(obj):
    print(json.dumps(obj, ensure_ascii=False, sort_keys=True), flush=True)


def handler_request(b, path):
    """真实 Handler + JSON 序列化，传输用内存替身；不是本地 HTTP 实测。"""
    class Connection:
        def __init__(self):
            self.body_bytes = 0
            self.status = None
            self.header = True
        def makefile(self, *args):
            return io.BytesIO(('GET %s HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nConnection: close\r\n\r\n' %
                              (path, b.PORT)).encode())
        def sendall(self, data):
            if self.header:
                self.status = int(data.split(b' ', 2)[1])
                self.header = False
            else:
                self.body_bytes += len(data)
    conn = Connection()
    b.Handler(conn, ('127.0.0.1', 12345), object())
    return {'status': conn.status, 'response_bytes': conn.body_bytes}

