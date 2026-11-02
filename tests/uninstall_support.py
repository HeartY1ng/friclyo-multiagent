"""仅在入口 TMPDIR 中运行；隔离 HOME、配置及所有系统命令。"""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock


class Repro(unittest.TestCase):
    def setUp(self):
        private = Path(tempfile.gettempdir()).resolve()
        source = Path(__file__).resolve().parents[1]
        self.temp = tempfile.TemporaryDirectory(prefix='undo-case-', dir=private)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.home = self.base / 'home'
        self.home.mkdir()
        self.tmp = self.base / 'tmp'
        self.tmp.mkdir()
        self.repo = self.base / 'repo'
        shutil.copytree(source, self.repo, ignore=shutil.ignore_patterns('.offline-tests-*', '.aw-test-*', '__pycache__'))
        self.work = self.home / 'work'
        self.fakebin = self.base / 'fakebin'
        self.fakebin.mkdir()
        (self.fakebin / 'python3').symlink_to(sys.executable)
        (self.fakebin / 'bash').symlink_to('/bin/bash')
        (self.fakebin / 'dirname').symlink_to('/usr/bin/dirname')
        # 所有可能调用宿主的命令均失败关闭；launchctl 在模块边界用内存替身。
        for name in ('security', 'launchctl', 'codex', 'opencode', 'claude', 'open'):
            path = self.fakebin / name
            path.write_text('#!/bin/sh\nexit 99\n')
            path.chmod(0o755)
        (self.fakebin / 'launchctl').write_text('#!/bin/sh\ncase "$1" in\nprint) exit 113;;\nbootout) exit 0;;\n*) exit 99;;\nesac\n')
        env = {'HOME': str(self.home), 'FRCLYO_HOME': str(self.work),
               'TMPDIR': str(self.tmp), 'TMP': str(self.tmp), 'TEMP': str(self.tmp),
               'PATH': str(self.fakebin),
               'PYTHONDONTWRITEBYTECODE': '1'}
        patch = mock.patch.dict(os.environ, env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        old_path = list(sys.path)
        self.addCleanup(lambda: sys.path.__setitem__(slice(None), old_path))
        for name in list(sys.modules):
            if name == 'frclyo' or name.startswith('frclyo.'):
                del sys.modules[name]
        loader = importlib.machinery.SourceFileLoader('aw_repro', str(self.repo / 'bin/aw'))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        self.aw = importlib.util.module_from_spec(spec)
        loader.exec_module(self.aw)
        self.loaded = False
        self.calls = []
        self.bootout_error = False
        self.print_error = False
        for patch in (mock.patch.object(self.aw, 'launchctl', side_effect=self.launchctl),
                      mock.patch.object(self.aw, 'tools_report', return_value={}),
                      mock.patch.object(self.aw.platform, 'system', return_value='Darwin')):
            patch.start()
            self.addCleanup(patch.stop)
        self.plist = self.home / 'Library/LaunchAgents/com.frclyo.board.plist'
        self.skill = self.home / '.claude/skills/frclyo-brain/SKILL.md'
        self.link = self.home / '.local/bin/aw'

    def launchctl(self, *args):
        self.calls.append(args[0])
        if args[0] == 'print':
            return 1 if self.print_error else 0 if self.loaded else 113
        if args[0] == 'bootstrap':
            self.loaded = True
            return 0
        if args[0] == 'bootout':
            if self.bootout_error:
                return 1
            self.loaded = False
            return 0
        raise AssertionError(args)

    def quiet(self, function, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = function(*args)
        return result, output.getvalue()

    def install(self):
        result, _ = self.quiet(self.aw.install, ['--yes'])
        self.assertEqual(result, 0)

    def uninstall(self, *extra):
        return self.quiet(self.aw.uninstall, ['--yes', *extra])

    def rows(self):
        return self.aw.read_journal()

    def evidence(self, **fields):
        print(json.dumps(fields, ensure_ascii=False, sort_keys=True), flush=True)
