"""修好后：未删掉的自建目录不写 undone，解决文件冲突后可重试清理。"""
import unittest
from uninstall_support import Repro


class TestB59(Repro):
    def test_retry_cleans_previously_blocked_directories(self):
        self.install()
        original = self.skill.read_bytes()
        self.skill.write_text('local user edit\n')
        first_rc, _ = self.uninstall()
        self.assertEqual(first_rc, 1)
        pending = [r for r in self.rows() if r.get('path') == str(self.skill.parent) and r.get('kind') == 'directory']
        undone_before = {r.get('id') for r in self.rows() if r['action'] == 'undone'}
        self.assertTrue(pending)
        self.assertTrue(all(r['id'] not in undone_before for r in pending))
        self.skill.write_bytes(original)
        second_rc, _ = self.uninstall()
        parents = [self.home / '.claude', self.home / '.claude/skills', self.skill.parent]
        leftover = [str(p.relative_to(self.home)) for p in parents if p.exists()]
        plans = [row for row in self.rows() if row.get('kind') == 'directory'
                 and row.get('path') in {str(p) for p in parents}]
        undone = {row.get('id') for row in self.rows() if row['action'] == 'undone'}
        self.evidence(first_rc=first_rc, retry_rc=second_rc, skill_exists=self.skill.exists(),
                      leftover=leftover, all_directories_marked_undone=all(p['id'] in undone for p in plans))
        self.assertEqual(leftover, [], '重试成功但安装新增的空目录永久漏清理')


if __name__ == '__main__':
    unittest.main(verbosity=2)
