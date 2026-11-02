"""修好后：有效安装记录足以卸载外部组件，不依赖业务配置 JSON 可解析。"""
import subprocess
import unittest
from uninstall_support import Repro


class TestB60(Repro):
    def test_uninstall_survives_broken_business_config(self):
        self.install()
        self.aw.config_path().write_text('{unfinished edit')
        result = subprocess.run(['/bin/bash', str(self.repo / 'uninstall.sh'), '--yes'],
                                text=True, capture_output=True, timeout=20)
        self.evidence(rc=result.returncode, stdout=result.stdout.strip(), stderr=result.stderr.strip(),
                      link_exists=self.link.is_symlink(), skill_exists=self.skill.exists(),
                      plist_exists=self.plist.exists())
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertFalse(self.link.is_symlink(), '配置解析失败使按有效 journal 撤销链接也无法执行')


if __name__ == '__main__':
    unittest.main(verbosity=2)
