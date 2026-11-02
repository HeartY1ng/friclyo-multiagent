"""B86：已通过任务再次派轮，运行/失败被旧收卡结论遮住，且不能正常再收卡。

合法启动的新轮不能继承旧轮验收；任务必须能重新完成生命周期，
首页应显示运行中，新轮失败不能显示通过，清理/卸载最终可完成。
覆盖真实入口自动恢复与最新一轮收卡前后的清理资格。
"""
import json
import unittest
from b86_support import Repro, scene


class ClosedCardRerun(Repro):
    def test_new_round_must_not_inherit_old_acceptance(self):
        lab = self.lab
        task = lab.new('已通过任务再次运行')
        first = self.complete_round(task)
        self.assertEqual(first['receipt']['状态'], '执行完成')
        closed = lab.run('ledger','完成',task,'--结论','通过','--摘要','r1 样例一致')
        self.assertEqual(closed.returncode,0)
        second = lab.launch(task,fail=True)
        self.assertTrue(lab.started(second))
        state = lab.state('r2_running')
        running = lab.detail('r2_running',task)
        self.assertEqual(running['轮次'][-1]['状态'],'running')
        lab.finish(second)
        self.assertEqual(second['receipt']['状态'],'失败')
        self.assertEqual(second['receipt']['退出码'],7)
        before_close = lab.run('clean','--days','0')
        self.assertEqual(before_close.returncode, 0)
        self.assertNotIn(task + '/r1\t', before_close.stdout)
        self.assertNotIn(task + '/r2\t', before_close.stdout)
        code, finished = scene.api(lab.board,'/api/task?id='+task)
        self.assertEqual(code,200)
        drop = lab.run('ledger','放弃',task,'第二轮失败后正常收尾')
        preview = lab.run('clean','--days','0')
        facts = {
            'r1_回执':first['receipt']['状态'], 'r1_收卡退出码':closed.returncode,
            'r2_实际状态':running['轮次'][-1]['状态'],
            'r2_首页正在运行':any(row['任务']==task for row in state['正在干活']),
            'r2_验收':running['轮次'][-1]['验收'],
            'r2_失败退出码':second['receipt']['退出码'],
            'r2_结束后任务结论':finished['结论'],
            'r2_收尾退出码':drop.returncode,
            'r2_收尾stderr':drop.stderr.strip(),
            'clean_明确跳过r2':task+'/r2' in preview.stderr,
        }
        print('B86 '+json.dumps(facts,ensure_ascii=False,sort_keys=True),flush=True)
        self.assertIsNone(running['轮次'][-1]['验收'],
                          '尚在运行的新轮被贴上了旧轮的“通过”验收')
        self.assertTrue(facts['r2_首页正在运行'])
        self.assertNotEqual(finished['结论'],'通过')
        self.assertEqual(drop.returncode,0)
        self.assertEqual(preview.returncode, 0)
        self.assertIn(task + '/r2\t', preview.stdout)
        self.assertFalse(facts['clean_明确跳过r2'])


if __name__=='__main__': unittest.main(verbosity=2)
