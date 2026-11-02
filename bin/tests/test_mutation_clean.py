"""变异回归：仅使用临时文件和离线替身，不接触宿主账号。"""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from frclyo import clean
class MutationCleanTests(unittest.TestCase):
    def test_remove_rechecks_identity_even_on_same_device(self):
        # 同盘 inode 已变化也必须保留文件，不能要求身份和设备同时变化。
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as tmp:
            path = Path(tmp) / 'keep'; path.write_text('keep')
            info = path.stat(); expected = (info.st_dev, info.st_ino + 1, info.st_mode)
            with clean.directory(path.parent) as fd:
                with self.assertRaises(clean.Unsafe):
                    clean.remove_at(fd, path.name, expected, info.st_dev)
            self.assertEqual(path.read_text(), 'keep')
    def test_file_usage_uses_allocated_512_byte_blocks(self):
        # 文件占用使用 POSIX 分配块单位，不能只检查结果非负。
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as tmp:
            path = Path(tmp) / 'template'; path.write_bytes(b'x' * 8192)
            info = path.stat()
            self.assertGreater(info.st_blocks, 0)
            self.assertEqual(clean.inspect(path, 'file'), (clean.identity(info), info.st_blocks * 512))
    def test_inspect_closes_directory_descriptor(self):
        # 扫描成功和失败都要释放打开的目录描述符，避免多次预览耗尽句柄。
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as tmp:
            path = Path(tmp) / 'candidate'; path.mkdir()
            actual_open, actual_close = os.open, os.close
            for failure in (False, True):
                descriptors = []
                def tracked_open(name, *args, **kwargs):
                    fd = actual_open(name, *args, **kwargs)
                    if name == path.name: descriptors.append(fd)
                    return fd
                with mock.patch.object(clean.os, 'open', side_effect=tracked_open):
                    try:
                        if failure:
                            with mock.patch.object(clean, 'measure', side_effect=clean.Unsafe('fixture')):
                                with self.assertRaises(clean.Unsafe): clean.inspect(path, 'dir')
                        else: clean.inspect(path, 'dir')
                        self.assertEqual(len(descriptors), 1)
                        with self.assertRaises(OSError): os.fstat(descriptors[0])
                    finally:
                        for fd in descriptors:
                            try: actual_close(fd)
                            except OSError: pass
    def test_template_symlink_is_rejected(self):
        # 模板叶节点本身必须是普通文件，链接目标是普通文件也不能接受。
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as tmp:
            target = Path(tmp) / 'target'; target.write_text('keep')
            link = Path(tmp) / 'template'; link.symlink_to(target)
            with self.assertRaises(clean.Unsafe): clean.inspect(link, 'file')
            self.assertEqual(target.read_text(), 'keep')
