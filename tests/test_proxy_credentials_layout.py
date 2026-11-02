"""B18：凭据探针从正式输入口读取；仅执行固定文本替身，不运行凭据探针本体。"""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bin'))
from frclyo.execution import copy_inputs
spec = importlib.util.spec_from_file_location('proxy_layout_fixture', ROOT / 'tests/host/proxy_credentials.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class ProbeLayoutTests(unittest.TestCase):
    def test_b18_fake_worker_uses_copied_round_probe(self):
        with tempfile.TemporaryDirectory(dir=Path(os.environ['TMPDIR']).resolve()) as tmp:
            base = Path(tmp).resolve()
            commands, inputs, run = base / 'commands', base / 'source', base / '中文轮次'
            commands.mkdir()
            run.mkdir()
            probe.prepare_fake_opencode(commands, inputs)
            # 不运行读环境/凭据的 PROBE；固定响应只证明入口选择了正确文件。
            (inputs / 'probe.py').write_text('print("ROUND_COPY_OK")\n', encoding='utf-8')
            copy_inputs(inputs, run / '输入')
            shutil.rmtree(inputs)
            result = subprocess.run([str(commands / 'opencode'), '--dir', str(run)],
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, 'ROUND_COPY_OK\n')


if __name__ == '__main__':
    unittest.main()
