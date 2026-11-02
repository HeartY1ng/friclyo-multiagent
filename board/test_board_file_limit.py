"""修复后：现有 300 项展示上限必须对整个文件遍历生效，并停止后续扫描。"""
import unittest
from pathlib import Path
from unittest.mock import patch
from regression_support import board_fixture, write_json, run_js


class BoardFileLimit(unittest.TestCase):
    def test_file_display_cap_applies_across_directories(self):
        with board_fixture() as (board, base):
            task = 'TEST-REVIEW'
            run = Path(board.DELIVER) / task / 'r1'
            run.mkdir(parents=True)
            write_json(run / '回执.json', {'工人': 'codex', '状态': '执行完成', '退出码': 0})
            for index in range(350):
                directory = run / ('src%03d' % index)
                directory.mkdir()
                (directory / 'module.py').write_text('# fixture\n')
            walked = []
            walk = board.os.walk
            def tracked_walk(*args, **kwargs):
                for row in walk(*args, **kwargs):
                    walked.append(row[0])
                    yield row
            with patch.object(board.os, 'walk', side_effect=tracked_walk):
                detail = board.build_task(task)
            count = len(detail['轮次'][0]['文件'])
            print('含 350 个子目录的正常交付，文件列表返回=%d 项（现有上限=300）' % count)
            self.assertLessEqual(count, 300, 'break 只退出当前目录的文件循环，继续遍历并追加其他目录')
            self.assertEqual(count, 300)
            self.assertEqual(len(walked), 300, '到上限后不应再向 os.walk 请求下一个目录')
            self.assertTrue(detail['轮次'][0]['文件列表已截断'])

    def test_exact_limit_stops_before_requesting_next_directory(self):
        with board_fixture() as (board, base):
            run = Path(board.DELIVER) / 'TEST-CAP' / 'r1'
            run.mkdir(parents=True)
            write_json(run / '回执.json', {'工人': 'codex', '状态': '执行完成', '退出码': 0})
            def walk(*args):
                yield str(run), ['unvisited'], ['回执.json'] * 300
                self.fail('扫描已达到上限，仍请求下一目录')
            with patch.object(board.os, 'walk', side_effect=walk):
                row = board.build_task('TEST-CAP')['轮次'][0]
            self.assertEqual(len(row['文件']), 300)
            self.assertTrue(row['文件列表已截断'])
            row = board.build_task('TEST-CAP')['轮次'][0]
            self.assertEqual(len(row['文件']), 1)
            self.assertFalse(row['文件列表已截断'])

    def test_file_limit_notice_is_last_and_not_a_file_link(self):
        result = run_js('B84', r'''
S.open={id:'TEST',round:'r1',tab:'files'}; S.detail=detail('TEST');
S.detail.轮次[0].文件=[{路径:'a.txt',大小:1}];
S.detail.轮次[0].文件列表已截断=true; renderDrawer();
assert(elements.db.innerHTML.includes('<li class="muted">还有更多文件未列出</li></ul>'));
assert.equal((elements.db.innerHTML.match(/data-file=/g)||[]).length,1);
S.detail.轮次[0].文件列表已截断=false; renderDrawer();
assert(!elements.db.innerHTML.includes('还有更多文件未列出'));
''')
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
