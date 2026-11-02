"""B136：真实脚本输出异常证据，合法结果不能被第二次执行覆盖。"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from frclyo import repro


class RetryExitTests(unittest.TestCase):
    def test_zero_and_one_preserved_even_when_output_names_missing_frclyo(self):
        for code in (0, 1):
            with self.subTest(code=code), tempfile.TemporaryDirectory(
                    dir=Path(os.environ['TMPDIR']).resolve()) as temporary:
                work = Path(temporary).resolve()
                (work / '交付').mkdir()
                logs = work / 'logs'
                logs.mkdir()
                test = work / 'test_evidence.py'
                test.write_text('from pathlib import Path\nimport sys\n'
                    'flag = Path(__file__).with_name("once")\n'
                    'if flag.exists():\n    sys.exit(%d)\n' % (1 - code) +
                    'flag.write_text("ran")\n'
                    'print("Captured: ModuleNotFoundError: No module named \'frclyo\'")\n'
                    'sys.exit(%d)\n' % code)
                def attempt(test, cwd, env, profile, log, seconds):
                    with log.open('xb') as output:
                        child = subprocess.run([sys.executable, '-B', str(test)], cwd=cwd,
                            env=env, stdout=output, stderr=subprocess.STDOUT, timeout=10)
                    return dict(cwd=str(cwd), log=log.name, exit_code=child.returncode,
                                timed_out=False, launch_error='', cleanup_error='')
                with patch.object(repro, 'attempt', side_effect=attempt):
                    row = repro.run_test(test, work, dict(os.environ), 'fixture', logs)
                self.assertEqual(row['exit_code'], code)
                self.assertEqual(row['verdict'], '复现' if code else '没复现')
                self.assertEqual(len(row['attempts']), 1)
                self.assertIn('ModuleNotFoundError', (logs / row['attempts'][0]['log']).read_text())


if __name__ == '__main__':
    unittest.main(verbosity=2)
