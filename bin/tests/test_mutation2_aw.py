"""第二轮入口回归：只加载函数，模型、账本、系统命令均由离线替身提供。"""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from frclyo import config


class Mutation2AwTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.home=self.root/'home';self.home.mkdir()
        path=Path(__file__).resolve().parents[1]/'aw'
        loader=importlib.machinery.SourceFileLoader('mutation2_aw',str(path))
        spec=importlib.util.spec_from_loader(loader.name,loader)
        self.aw=importlib.util.module_from_spec(spec)
        with mock.patch.object(config,'root_dir',return_value=self.root/'data'):
            loader.exec_module(self.aw)
        self.aw.HOME=self.home
        self.aw.JOURNAL=self.root/'journal.jsonl'
        journal=mock.patch.object(self.aw,'journal_path',return_value=self.aw.JOURNAL)
        journal.start();self.addCleanup(journal.stop)

    @contextlib.contextmanager
    def doctor_environment(self):
        cfg=mock.Mock();cfg.migration_needed.return_value=False
        cfg.config_file.return_value=self.root/'absent.json'
        pool=[{'worker':'codex','model':'synthetic','level':'medium'}]
        with mock.patch.object(self.aw,'runtime',return_value=cfg), \
                mock.patch.object(self.aw,'config',return_value={'providers':[],'board_port':12345}), \
                mock.patch.object(self.aw,'tools_report',return_value={'codex'}), \
                mock.patch.object(self.aw,'port_open',return_value=False), \
                mock.patch.object(self.aw,'model_inventory',return_value=pool), \
                mock.patch.object(self.aw.platform,'system',return_value='Darwin'), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            yield output

    def test_doctor_healthy_offline_report(self):
        # 正常配置和已登录候选可用时返回成功；守护失败计数/返回值及系统和迁移提示。
        with self.doctor_environment() as output:
            self.assertEqual(self.aw.doctor([]),0)
        self.assertIn('✅ 系统：Darwin',output.getvalue())
        self.assertNotIn('正在读取旧位置',output.getvalue())

    def test_doctor_live_propagates_calibration_result(self):
        # live 仅验证转发；把 live=True 改为 False 或丢弃校准返回值必须失败。
        with self.doctor_environment(),mock.patch.object(self.aw,'calibrate',return_value=19) as calibrate:
            self.assertEqual(self.aw.doctor(['--live']),19)
        self.assertEqual(calibrate.call_args.kwargs['live'],True)

    def test_main_rejects_old_python_and_accepts_help(self):
        # Python 下限和帮助退出码分别断言，捕获比较/条件/返回值变异。
        with mock.patch.object(self.aw.sys,'version_info',(3,8)):
            with self.assertRaisesRegex(self.aw.UserError,'Python'):self.aw.main(['help'])
        with mock.patch.object(self.aw.sys,'version_info',(3,9)),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.aw.main(['help']),0)

    def test_run_rejects_each_invalid_worker_argument(self):
        # 没传工人和传错工人都不能到达 exec 边界。
        with mock.patch.object(self.aw.os,'execv') as execute:
            for args in (['run'],['run','unknown']):
                with self.subTest(args=args),self.assertRaises(self.aw.UserError):self.aw.main(args)
        execute.assert_not_called()

    def test_finished_batch_publishes_matching_success_receipt(self):
        # 父卡成功证据须建立缺失父目录，并记录退出码零。
        delivery=self.root/'delivery'
        with mock.patch.object(self.aw,'ledger',return_value='r1'), \
                mock.patch.object(self.aw,'path_of',return_value=delivery), \
                mock.patch.object(self.aw,'finish_task') as finish:
            self.aw.finish_batch('T',True,'summary')
        receipt=json.loads((delivery/'T/r1/回执.json').read_text())
        self.assertEqual(receipt['退出码'],0)
        self.assertEqual(receipt['任务编号'],'T')
        self.assertEqual((delivery/'T/r1/结果.txt').read_bytes(),b'summary\n')
        finish.assert_called_once_with('T',True,'summary')

    def test_failed_batch_does_not_publish_success_artifacts(self):
        # 未通过只关闭卡片，不生成成功回执。
        with mock.patch.object(self.aw,'ledger') as ledger, \
                mock.patch.object(self.aw,'path_of') as path, \
                mock.patch.object(self.aw,'finish_task') as finish:
            self.aw.finish_batch('T',False,'failed')
        ledger.assert_not_called();path.assert_not_called()
        finish.assert_called_once_with('T',False,'failed')

    def test_calibration_uses_supplied_pool_and_displays_level(self):
        # 确认前只展示；显式候选不得被重新发现覆盖，档位不能改成“默认”。
        pool=[{'worker':'codex','model':'fixture','level':'high'}]
        with mock.patch.object(self.aw,'model_inventory',side_effect=AssertionError('不应重新发现')), \
                mock.patch.object(self.aw,'confirm',return_value=False), \
                mock.patch.object(self.aw,'prepare_worker',side_effect=AssertionError('未确认却启动')), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(self.aw.calibrate([],pool=pool),0)
        self.assertIn('codex × fixture × high',output.getvalue())

    def test_journal_accepts_unicode_separator_and_rejects_invalid_rows(self):
        # 只按 LF 分行，且对象必须有 action；安装记录仅用合成文件。
        row={'action':'plan','id':'fixture','path':'name\u2028tail'}
        self.aw.JOURNAL.write_text(json.dumps(row,ensure_ascii=False)+'\n')
        self.assertEqual(self.aw.read_journal(),[row])
        for value in ({'id':'missing action'},[],None):
            self.aw.JOURNAL.write_text(json.dumps(value)+'\n')
            with self.subTest(value=value),self.assertRaises(self.aw.UserError):self.aw.read_journal()

    def test_missing_journal_returns_empty_list(self):
        # 没有安装记录时只返回空集合，不读取不存在的文件。
        self.assertEqual(self.aw.read_journal(),[])

    def test_install_target_boundary_and_atomic_publication(self):
        # 临时 HOME 内安装成功，HOME 外普通安装拒绝；两个地址都在测试目录。
        inside=self.home/'tool';outside=self.root/'outside'
        with mock.patch.object(self.aw,'record'),mock.patch.object(self.aw,'ensure_dirs'):
            self.assertIs(self.aw.install_file(inside,b'fixture'),True)
            self.assertEqual(inside.read_bytes(),b'fixture')
            with self.assertRaises(self.aw.UserError):self.aw.install_file(outside,b'fixture')
        self.assertFalse(outside.exists())

if __name__=='__main__':unittest.main()
