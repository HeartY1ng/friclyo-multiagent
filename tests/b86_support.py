"""B86 离线夹具；固定覆盖符号链接 TMPDIR 与中文物理路径。"""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SCENE = Path(__file__).resolve().parent / 'e2e/场景.py'
spec = importlib.util.spec_from_file_location('offline_day', SCENE)
scene = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scene)


class Repro(unittest.TestCase):
    def setUp(self):
        framework = os.environ.get('REVIEW_FRAMEWORK', Path(__file__).resolve().parents[1])
        temporary = tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve())
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name).resolve()
        real = base / '宿主中文临时目录'
        real.mkdir()
        link = base / 'tmp-link'
        link.symlink_to(real, target_is_directory=True)
        environment = mock.patch.dict(os.environ, TMPDIR=str(link))
        environment.start()
        self.addCleanup(environment.stop)
        cache = mock.patch.object(tempfile, 'tempdir', None)
        cache.start()
        self.addCleanup(cache.stop)
        self.lab = scene.Lab(framework)
        self.addCleanup(self.lab.close)
        self.assertEqual(self.lab.private, real)
        self.assertEqual(self.lab.base, self.lab.base.resolve())
        self.lab.install()

    def complete_round(self, task, **plan):
        job = self.lab.launch(task, **plan)
        self.assertTrue(self.lab.started(job), 'CLI 替身必须已启动')
        self.lab.finish(job)
        return job
