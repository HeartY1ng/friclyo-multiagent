"""第二轮日审回归：合成完成卡，分别校验失败理由和成功证据摘要。"""
import json
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from frclyo import audit
import test_audit

class Mutation2AuditTests(unittest.TestCase):
    def setUp(self):
        self.fixture=test_audit.AuditTests('runTest');self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.record();self.task='CL-20000101-01'
        self.paths=self.fixture.paths

    def test_status_and_hash_are_independent_requirements(self):
        # 只坏状态或只坏哈希，任一条件就足以拒绝完成声明。
        self.fixture.record_delivery(self.task,'r1')
        path=self.paths.deliver/self.task/'r1/回执.json'
        original=json.loads(path.read_text())
        for field,value in [('状态','失败'),('结果sha256','incorrect')]:
            path.write_text(json.dumps(dict(original,**{field:value})))
            lines,bad=audit.audit(self.paths)
            self.assertEqual(bad,1)
            self.assertIn('❌',lines[0])

    def test_missing_latest_receipt_reports_correct_round(self):
        # r9 存在但 r10 缺回执，诊断必须指向最新的 r10。
        self.fixture.record_delivery(self.task,'r9')
        (self.paths.deliver/self.task/'r10').mkdir()
        lines,bad=audit.audit(self.paths)
        self.assertEqual(bad,1)
        self.assertIn('最新一轮 r10 没有回执',lines[0])

    def test_success_report_names_verified_task_and_round(self):
        # 成功计数之外仍要留逐卡核对证据，不能静默删掉报告行。
        self.fixture.record_delivery(self.task,'r10')
        lines,bad=audit.audit(self.paths)
        self.assertEqual(bad,0)
        self.assertEqual(len(lines),2)
        self.assertIn(self.task,lines[0]);self.assertIn('最新一轮 r10',lines[0])
        self.assertTrue(lines[0].startswith('✅'))

if __name__=='__main__':unittest.main()
