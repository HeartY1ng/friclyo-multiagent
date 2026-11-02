"""任务卡离线测试：只用 TMPDIR 中的假数据，直接 python3 -B 本文件即可运行。"""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'bin'))
from frclyo import cards

TASK = 'CL-20261101-05'


class CardTests(unittest.TestCase):
    def setUp(self):
        # 先解析临时根目录，避免 macOS 的 /var、/tmp 别名影响路径核对。
        self.tmp = tempfile.TemporaryDirectory(dir=Path(os.environ['TMPDIR']).resolve())
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.data = self.root / 'data'
        self.data.mkdir()
        self.cfg = self.root / 'fake-config.json'
        self.cfg.write_text('{}', encoding='utf-8')
        self.env = dict(os.environ, FRCLYO_HOME=str(self.data), FRCLYO_CONFIG=str(self.cfg))
        self.patch = mock.patch.dict(os.environ, self.env)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.ledger = self.data / '账本.jsonl'
        self.row = {'时间': '2026-11-01T00:00:00Z', '动作': '开卡', '任务': TASK,
                    '标题': '可靠性核对', '项目': '示例项目', '来源': 'CL', '派活人': '测试', '父任务': ''}
        self.write_ledger(self.row)

    def write_ledger(self, *rows):
        # 使用最小合法事件替身，不调用真实开卡，也不读取任何宿主数据。
        self.ledger.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows),
                               encoding='utf-8')

    def invoke(self, *args):
        return subprocess.run([sys.executable, '-B', str(ROOT / 'bin/aw'), *args],
                              cwd=self.root, env=self.env, capture_output=True, text=True, timeout=15)

    def target(self, round_name='r1', task=TASK):
        return self.data / '任务卡' / (task + '-' + round_name + '.txt')

    def test_chinese_defaults_and_readonly_ledger(self):
        before = self.ledger.read_bytes()
        result = self.invoke('卡', '新建', '找bug卡.md', TASK)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, str(self.target()) + '\n')
        content = self.target().read_text(encoding='utf-8')
        for expected in ('项目：示例项目', TASK, '（r1）：可靠性核对', '<Cx>'):
            self.assertIn(expected, content)
        for token in ('<任务号>', '<轮次>', '<项目>', '<标题>'):
            self.assertNotIn(token, content)
        self.assertEqual(self.ledger.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.data.iterdir()), ['任务卡', '账本.jsonl'])

    def test_four_templates_and_english_options(self):
        for index, name in enumerate(('bughunt', 'fix', 'review', 'experiment')):
            round_name = 'r%d_A-z' % index
            result = self.invoke('card', 'new', name, TASK, '--title', '自定标题', '--round', round_name)
            self.assertEqual(result.returncode, 0, result.stderr)
            content = self.target(round_name).read_text(encoding='utf-8')
            self.assertIn('：自定标题', content)
            self.assertIn('项目：示例项目', content)
            self.assertIn('python3 -B 交付/复现测试/test_x.py', content)
            self.assertIn('FRCLYO_REPRO_ROOT', content)
            self.assertIn('1 = 复现', content)
            self.assertIn('resolve()', content)
            self.assertIn('中性版', content)
            self.assertIn('权限错误属于运行结果', content)

    def test_chinese_options_and_child_task(self):
        child = TASK + '-A2'
        self.write_ledger(self.row, dict(self.row, 任务=child, 父任务=TASK))
        result = self.invoke('卡', '新建', '修复', child, '--标题', '修复标题', '--轮', 'r2')
        self.assertEqual(result.returncode, 0, result.stderr)
        text = self.target('r2', child).read_text(encoding='utf-8')
        self.assertIn('：修复标题', text)
        self.assertIn('报告临时编号 → 缺陷清单正式编号', text)

    def test_unknown_task_missing_or_invalid_ledger_has_no_card(self):
        for content in ('', self.ledger.read_text(), '{invalid}\n', json.dumps(self.row)):
            self.ledger.write_text(content, encoding='utf-8')
            result = self.invoke('card', 'new', '找bug', 'CL-20261101-99')
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertEqual(result.stdout, '')
            self.assertFalse((self.data / '任务卡').exists())
            self.assertEqual(self.ledger.read_text(), content)
        self.ledger.unlink()
        result = self.invoke('卡', '新建', '实验', TASK)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(self.ledger.exists())

    def test_invalid_arguments_return_usage_error_without_writes(self):
        cases = [('new', 'unknown', TASK), ('new', '../修复卡.md', TASK),
                 ('new', '找bug', '../task'), ('new', '修复', TASK, '--extra'),
                 ('new', '修复', TASK, '--round', ''), ('new', '修复', TASK, '--round=-r1'), ('new', '修复', TASK, '--round', '../r2'),
                 ('new', '修复', TASK, '--round', '审查'), ('new', '修复', TASK, '--round', 'r 1'),
                 ('new', '修复', TASK, '--title', ''), ('new', '修复', TASK, '--title', 'a\nb'),
                 ('new', '修复', TASK, '--title', 'a\u2028b'), ('new', '修复'), ()]
        for args in cases:
            with self.subTest(args=args):
                result = self.invoke('card', *args)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertFalse((self.data / '任务卡').exists())

    def test_existing_file_directory_and_broken_link_are_preserved(self):
        self.target().parent.mkdir()
        self.target().write_text('已有卡片', encoding='utf-8')
        self.target('r2').mkdir()
        self.target('r3').symlink_to(self.root / 'missing')
        for round_name in ('r1', 'r2', 'r3'):
            result = self.invoke('card', 'new', '修复', TASK, '--round', round_name)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn('拒绝覆盖', result.stderr)
        self.assertEqual(self.target().read_text(encoding='utf-8'), '已有卡片')
        self.assertTrue(self.target('r2').is_dir())
        self.assertTrue(self.target('r3').is_symlink())
        self.assertFalse(list(self.target().parent.glob('.card-*')))

    def test_two_processes_publish_only_one_complete_card(self):
        # 两个独立 CLI 竞争同一目标：一方成功、一方拒绝，内容必须是完整模板。
        processes = [subprocess.Popen([sys.executable, '-B', str(ROOT / 'bin/aw'), 'card',
                     'new', '修复', TASK, '--title', title], cwd=self.root, env=self.env,
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for title in ('甲', '乙')]
        try:
            outputs = [process.communicate(timeout=15) for process in processes]
            self.assertEqual(sorted(process.returncode for process in processes), [0, 1], outputs)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
        content = self.target().read_text(encoding='utf-8')
        self.assertTrue('：甲\n' in content or '：乙\n' in content)
        self.assertTrue(content.endswith('失败（原因）\n'))
        self.assertFalse(list(self.target().parent.glob('.card-*')))

    def test_publication_failure_does_not_leave_partial_card(self):
        # 用替身模拟文件系统失败，无需改变沙箱权限；失败后不能留半张目标卡。
        error = io.StringIO()
        with mock.patch.object(cards.os, 'link', side_effect=PermissionError('synthetic')), \
                contextlib.redirect_stderr(error):
            code = cards.main(['new', '修复', TASK])
        self.assertEqual(code, 1)
        self.assertIn('目录访问权限', error.getvalue())
        self.assertFalse(self.target().exists())
        self.assertEqual(list(self.target().parent.iterdir()), [])

    def test_missing_or_incomplete_template_fails_before_output(self):
        template_dir = self.root / 'templates'
        template_dir.mkdir()
        with mock.patch.object(cards, 'TEMPLATE_DIR', template_dir):
            with self.assertRaisesRegex(cards.CardError, '不可读'):
                cards.create('修复', TASK)
            (template_dir / '修复卡.md').write_text('<任务号>\n', encoding='utf-8')
            with self.assertRaisesRegex(cards.CardError, '缺少'):
                cards.create('修复', TASK)
        self.assertFalse((self.data / '任务卡').exists())

    def test_help_does_not_load_configuration_or_ledger(self):
        # 损坏的配置也不能让帮助失败，避免用户无从查找用法。
        self.cfg.write_text('{broken}', encoding='utf-8')
        for args in (('help',), ('card', '--help'), ('卡', '新建', '--help')):
            result = self.invoke(*args)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('card', result.stdout)
        self.assertFalse((self.data / '任务卡').exists())

    def test_titles_are_not_recursively_interpreted_and_closed_cards_are_readable(self):
        # 已收卡仍满足“已开卡”前提；生成草稿不恢复状态、不登记执行轮次。
        closed = {'时间': '2026-11-01T01:00:00Z', '动作': '完成', '任务': TASK,
                  '结论': '通过', '摘要': '测试收卡'}
        self.write_ledger(self.row, closed)
        before = self.ledger.read_bytes()
        result = self.invoke('card', 'new', '改动审查', TASK, '--title', '标题保留<项目>')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('标题保留<项目>', self.target().read_text(encoding='utf-8'))
        self.assertEqual(self.ledger.read_bytes(), before)

    def test_invalid_ledger_fields_do_not_make_a_card(self):
        for fields in ({'项目': ''}, {'项目': 'a\nb'}, {'标题': 'a\nb'}):
            self.write_ledger(dict(self.row, **fields))
            result = self.invoke('card', 'new', '实验', TASK)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertFalse((self.data / '任务卡').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
