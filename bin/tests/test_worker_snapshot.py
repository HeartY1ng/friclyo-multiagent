"""共享快照首次发布的多进程竞争与胜者校验。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

BIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BIN))
from frclyo import worker


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="snapshot-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.runtime = self.root / "runtime"
        self.source = self.root / "source.json"
        self.entry = {"npm": "test", "api": "https://api.deepseek.com", "models": {"model": {}}}
        self.source.write_text(json.dumps({"deepseek": self.entry}))
        self.cfg = {"runtime_dir": str(self.runtime), "models_snapshot_source": str(self.source)}

    def test_concurrent_first_creation_reuses_winner(self):
        # 四个独立解释器读完各自源后才放行；发布仍使用真实 os.link。
        code = r'''import json, sys, time
from pathlib import Path
from unittest.mock import patch
from frclyo import execution, worker
root, index = Path(sys.argv[1]), sys.argv[2]
source = root / ("source-" + index + ".json")
original = execution.safe_read
def synchronized_read(path, limit):
    data = original(path, limit)
    if Path(path) == source:
        (root / ("ready-" + index)).touch()
        deadline = time.monotonic() + 15
        while len(list(root.glob("ready-*"))) != 4:
            if time.monotonic() > deadline:
                raise RuntimeError("barrier timeout")
            time.sleep(0.01)
    return data
with patch.object(execution, "safe_read", side_effect=synchronized_read):
    _, entry = worker.snapshot({"runtime_dir": str(root / "runtime"),
                               "models_snapshot_source": str(source)}, "deepseek", "model")
print(json.dumps(entry))
'''
        processes = []
        try:
            for index in range(4):
                entry = dict(self.entry, npm="source-" + str(index))
                (self.root / ("source-%s.json" % index)).write_text(json.dumps({"deepseek": entry}))
                processes.append(subprocess.Popen(
                    [sys.executable, "-B", "-c", code, str(self.root), str(index)],
                    cwd=BIN, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
            outputs = [process.communicate(timeout=25) for process in processes]
            self.assertEqual([process.returncode for process in processes], [0] * 4, outputs)
            winner = json.loads((self.runtime / "models.snapshot.json").read_text())["deepseek"]
            self.assertEqual([json.loads(out) for out, _ in outputs], [winner] * 4)
            self.assertEqual(list(self.runtime.glob(".models-*")), [])
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.communicate()

    def test_competing_snapshot_is_revalidated(self):
        destination = self.runtime / "models.snapshot.json"
        for content in ("broken", json.dumps({"deepseek": dict(self.entry, models={})})):
            destination.unlink(missing_ok=True)
            with self.subTest(content=content):
                def competing_link(*args):
                    destination.write_text(content)
                    raise FileExistsError()
                with patch.object(worker.os, "link", side_effect=competing_link):
                    with self.assertRaisesRegex(ValueError, "模型快照"):
                        worker.snapshot(self.cfg, "deepseek", "model")
                self.assertEqual(destination.read_text(), content)
                self.assertEqual(list(self.runtime.glob(".models-*")), [])
                destination.unlink()


if __name__ == "__main__":
    unittest.main()
