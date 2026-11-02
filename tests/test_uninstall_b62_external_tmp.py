"""修好后：完整卸载在删根前处理已登记的根外临时目录或报告未完成。

只创建本用例目录内的真实临时布局，不运行工人、不产生进程或凭据。
"""
from pathlib import Path
import json
import time
import unittest
from unittest import mock
from uninstall_support import Repro


class TestB62(Repro):
    def setUp(self):
        super().setUp()
        isolation = self.aw.runtime('isolation')

        def private_tmp(worker, root, run):
            isolation.validate_run(root, run)
            root, run = Path(root).resolve(), Path(run).resolve()
            self.assertEqual(root, self.work.resolve())
            relative = run.relative_to(root / '交付')
            path = (self.base / 'external-tmp' / worker / relative).resolve()
            self.assertTrue(path.is_relative_to(self.base.resolve()))
            self.assertFalse(path.is_relative_to(root))
            return path

        # 两个算法均不读取 TMPDIR；中文 HOME/数据根会回退到 /private/tmp。
        # 本组验证卸载顺序和重试，替换路径入口，准备、扫描、删除共用同一夹具。
        # 连未创建的 OpenCode 候选也必须替换，避免卸载扫描宿主回退目录。
        for name, replacement in (
                ('codex_tmp', lambda root, run, home=None: private_tmp('codex', root, run)),
                ('round_tmp', lambda root, run: private_tmp('opencode', root, run))):
            patch = mock.patch.object(isolation, name, side_effect=replacement)
            patch.start()
            self.addCleanup(patch.stop)

    def check_external_cleanup(self, fail=False, unsafe=False, legacy=False):
        self.install()
        run = self.work / '交付/CL-20261031-01/r1'
        run.mkdir(parents=True)
        isolation = self.aw.runtime('isolation')
        computed = isolation.codex_tmp(self.work, run).resolve()
        self.assertTrue(computed.is_relative_to(self.base.resolve()), '不允许回退到真实系统临时目录')
        other = isolation.round_tmp(self.work, run).resolve()
        self.assertTrue(other.is_relative_to(self.base.resolve()), '不允许扫描真实系统临时目录')
        private = Path(isolation.prepare_codex_tmp(self.work, run)).resolve()
        self.assertEqual(private, computed)
        (private / 'synthetic-artifact.txt').write_text('test data only\n')
        # 构造合成已收卡轮次和可信退出证据，确保卸载前 clean 可识别此节点。
        clean = self.aw.runtime('clean')
        ledger = self.aw.runtime('ledger')
        task = run.parent.name
        stamp = int(time.time()) - 86400
        events = [ledger.event('开卡', task, stamp=stamp - 100, 标题='fixture',
                               项目='fixture', 来源='CL', 派活人='test', 父任务=''),
                  ledger.event('完成', task, stamp=stamp, 结论='通过', 摘要='fixture')]
        if legacy:
            events[0]['卡片文件'] = 'runs/' + task + '-r1.txt'
        (self.work / '账本.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in events))
        (run / '运行信息.json').write_text(json.dumps({'工人': 'codex', '进程号': 2147483647}))
        control = self.work / '运行时/控制/codex' / task / 'r1'
        control.mkdir(parents=True)
        (control / 'status.json').write_text(json.dumps({'group_reaped': True}))
        (control / 'receipt.json').write_text(json.dumps({'工人': 'codex', '任务编号': task,
            '轮次编号': 'r1', '进程组已回收': True, '结束': ledger.utc(stamp - 10)}))
        # 不探测宿主 PID：仅在进程存活查询边界返回合成的“已退出”。
        with mock.patch.object(clean, 'dead_pid', return_value=True):
            before = clean.plan(self.work, 0)
        eligible = private in [row['path'].resolve() for row in before]
        self.assertTrue(eligible, '卸载前夹具必须可清理')
        if fail or unsafe:
            patch = (mock.patch.object(clean, 'remove_at', side_effect=PermissionError('injected'))
                     if fail else mock.patch.object(clean, 'dead_pid', return_value=False))
            with patch, mock.patch('builtins.input', return_value=str(self.work)):
                rc, _ = self.uninstall('--delete-data')
            self.assertEqual(rc, 1)
            self.assertTrue(private.exists())
            self.assertTrue((self.work / '账本.jsonl').exists())
            self.assertTrue((control / 'status.json').exists())
            self.assertTrue(self.aw.JOURNAL.exists())
        # 坏业务配置不影响根外索引清理与删除数据。
        self.aw.config_path().write_text('{broken')
        with mock.patch.object(clean, 'dead_pid', return_value=True), mock.patch('builtins.input', return_value=str(self.work)):
            rc, _ = self.uninstall('--delete-data')
        self.assertEqual(rc, 0)
        self.assertFalse(self.work.exists())
        self.evidence(rc=rc, work_exists=self.work.exists(), private_tmp_exists=private.exists(),
                      remaining_fake_artifact=(private / 'synthetic-artifact.txt').exists(),
                      eligible_before_uninstall=eligible)
        self.assertFalse(private.exists(), '根外私有目录遗留，根内账本与清理证据却已被删除')

    def test_delete_root_cleans_external_first(self):
        self.check_external_cleanup()

    def test_b100_legacy_card_with_bad_config_cleans_external_first(self):
        # B100：旧卡片在正常配置下可清理；损坏配置后仍先清根外目录再删根。
        self.check_external_cleanup(legacy=True)

    def test_external_delete_failure_retains_index_and_retries(self):
        self.check_external_cleanup(fail=True)

    def test_unconfirmed_exit_retains_index_and_retries(self):
        self.check_external_cleanup(unsafe=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
