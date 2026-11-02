"""B63/B70：逐行隔离坏记录，两家缓存用量显示一致。直接 python3 -B 运行。"""
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from regression_support import board_fixture, write_json, run_js, http


class BoardPerfUsage(unittest.TestCase):
    def test_b96_deep_line_keeps_all_projects_and_detail_available(self):
        """深层表现记录只跳过坏行，三个 HTTP 入口及后续正常验收记录均保留。"""
        with board_fixture() as (board, base):
            task = 'TEST-PERF-DEEP'
            run = Path(board.DELIVER) / task / 'r1'
            run.mkdir(parents=True)
            write_json(run / '回执.json', {'状态': '执行完成', '退出码': 0})
            depth = sys.getrecursionlimit() + 20
            broken = '{"secret-sentinel":' + '[' * depth + '0' + ']' * depth + '}\n'
            good = {'任务号': task, '轮次': 'r1', '通过验收': True}
            raw = broken + json.dumps(good, ensure_ascii=False) + '\n'
            Path(board.PERF).write_text(raw, encoding='utf-8')
            stderr = io.StringIO()
            with patch.object(board.sys, 'stderr', stderr):
                for path in ('/api/state', '/api/state?project=other', '/api/task?id=' + task):
                    status, body = http(board, path)
                    self.assertEqual(status, 200, body)
                    if path.startswith('/api/task'):
                        self.assertEqual(json.loads(body)['轮次'][0]['验收'], '通过')
                self.assertEqual(board.read_perf()[(task, 'r1')], good)
            self.assertIn('%s:1（RecursionError）' % board.PERF, stderr.getvalue())
            self.assertNotIn('secret-sentinel', stderr.getvalue())
            self.assertEqual(Path(board.PERF).read_text(encoding='utf-8'), raw)

    def test_b96_unexpected_line_exception_is_isolated(self):
        """逐行边界覆盖普通 Exception，而不只列举 JSON 解码器的已知错误。"""
        with board_fixture() as (board, base):
            Path(board.PERF).write_text('broken\n{"任务号":"TEST","轮次":"r1"}\n', encoding='utf-8')
            loads = json.loads
            def decode(line):
                if line.startswith('broken'):
                    raise RuntimeError('secret-sentinel')
                return loads(line)
            with patch.object(board.json, 'loads', side_effect=decode), patch.object(board, 'log') as log:
                self.assertIn(('TEST', 'r1'), board.read_perf())
            log.assert_called_once_with('跳过表现记录坏行：%s:1（RuntimeError）' % board.PERF)

    def test_bad_lines_log_once_each_and_preserve_surrounding_records(self):
        with board_fixture() as (board, base):
            records = [{'任务号': 'TEST-PERF', '轮次': 'r%d' % i, '通过验收': True} for i in range(5)]
            good = [json.dumps(r, ensure_ascii=False).encode() for r in records]
            bad = [b'{"secret-sentinel":', good[0] + good[1], b'\xffsecret-sentinel', b'[]']
            lines = []
            for ok, broken in zip(good, bad):
                lines.extend([ok, broken])
            lines.append(good[-1])  # 最后一条完整记录没有 LF。
            raw = b'\n'.join(lines)
            Path(board.PERF).write_bytes(raw)
            stderr = io.StringIO()
            with patch.object(board.sys, 'stderr', stderr):
                result = board.read_perf()
            self.assertEqual(result, {(r['任务号'], r['轮次']): r for r in records})
            logs = stderr.getvalue().splitlines()
            self.assertEqual(len(logs), len(bad))
            for number, line in zip((2, 4, 6, 8), logs):
                self.assertIn('%s:%d' % (board.PERF, number), line)
                self.assertIn('跳过表现记录坏行', line)
            self.assertIn('UnicodeDecodeError', logs[2])
            self.assertNotIn('secret-sentinel', stderr.getvalue())
            self.assertEqual(Path(board.PERF).read_bytes(), raw)
            with patch.object(board.sys, 'stderr', io.StringIO()):
                row = Path(board.DELIVER) / 'TEST-PERF' / 'r4'
                row.mkdir(parents=True)
                write_json(row / '回执.json', {'工人': 'codex', '状态': '执行完成', '退出码': 0})
                task = board.build_task('TEST-PERF')
            self.assertEqual(task['轮次'][0]['验收'], '通过')

    def test_missing_and_empty_perf_are_not_bad_lines(self):
        with board_fixture() as (board, base), patch.object(board, 'log') as log:
            self.assertEqual(board.read_perf(), {})
            Path(board.PERF).write_bytes(b'')
            self.assertEqual(board.read_perf(), {})
            log.assert_not_called()

    def test_both_receipt_formats_have_same_cache_display(self):
        with board_fixture() as (board, base):
            usages = [{'input_tokens': 13, 'output_tokens': 9},
                      {'input': 13, 'output': 6, 'reasoning': 3}]
            for worker, usage in zip(('codex', 'opencode'), usages):
                usage.update(cached_input_tokens=300, cache_write_input_tokens=50)
                run = Path(board.DELIVER) / 'TEST-USAGE' / worker
                run.mkdir(parents=True)
                write_json(run / '回执.json', {'工人': worker, '状态': '执行完成', '退出码': 0, '用量': usage})
                row = board.build_task('TEST-USAGE', worker)['轮次']
                selected = next(r for r in row if r['轮次'] == worker)
                self.assertEqual(selected['用量'], {'输入': 13, '输出': 9, '缓存读取': 300, '缓存写入': 50})
            self.assertEqual(board.norm_usage({'cached_input_tokens': 0}), {'输入': 0, '输出': 0, '缓存读取': 0})
            result = run_js('B70', r'''
S.open={id:'TEST',round:'r1',tab:'progress'}; S.detail=detail('TEST');
for (const worker of ['codex','opencode']) {
  S.detail.轮次[0].工人=worker;
  S.detail.轮次[0].用量={输入:13,输出:9,缓存读取:300,缓存写入:50}; renderDrawer();
  assert(elements.dh.innerHTML.includes('13 入 / 9 出 / 缓存读取 300 / 缓存写入 50'));
}
''')
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
