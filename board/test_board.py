#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
看板的自动测试：用临时造的假数据，检查每种情况看板都判断对了，并且安全限制有效。
运行：python3 test_board.py
"""
import importlib
import importlib.util
import io
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


class BoardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # ---------- 造一套假的交付目录、任务卡和账本 ----------
        cls.tmp = tempfile.mkdtemp(prefix="board-test-", dir=HERE)
        cls.deliver = os.path.join(cls.tmp, "交付")
        cls.runs = os.path.join(cls.tmp, "任务卡")
        os.makedirs(cls.runs)
        t = time.time()

        def round_dir(task, rnd, files):
            d = os.path.join(cls.deliver, task, rnd)
            os.makedirs(d, exist_ok=True)
            for name, content in files.items():
                mode = "wb" if isinstance(content, bytes) else "w"
                with open(os.path.join(d, name), mode, **({} if mode == "wb" else {"encoding": "utf-8"})) as f:
                    f.write(content)
            return d

        # 假工人进程：名字里带 worker.sh，看板才认它是工人
        cls.fake = os.path.join(cls.tmp, "fake-worker.sh")
        with open(cls.fake, "w") as f:
            f.write("#!/bin/bash\nsleep 120\n")
        os.chmod(cls.fake, 0o755)
        cls.proc = subprocess.Popen([cls.fake], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

        # 1) 正在跑
        round_dir("CL-20991231-01", "r1", {
            "任务.txt": "项目：测试项目\n# 正在跑的活\n",
            "运行信息.json": json.dumps({"工人": "opencode", "模型": "deepseek/deepseek-v4-pro", "档位": "high",
                                     "开始": iso(t - 60), "超时秒": 600, "进程号": cls.proc.pid}),
            "events.jsonl": json.dumps({"type": "tool_use", "timestamp": int(t * 1000), "part": {"tool": "bash", "state": {
                "status": "completed", "input": {"command": "pytest -q"}, "output": "3 passed"}}}) + "\n",
        })
        # 2) 中断：进程号不存在
        round_dir("CL-20991231-02", "r1", {
            "任务.txt": "项目：测试项目\n# 中断的活\n",
            "运行信息.json": json.dumps({"工人": "opencode", "模型": "xiaomi/mimo-v2.6-pro", "开始": iso(t - 600), "超时秒": 600, "进程号": 999999}),
        })
        # 3) 需要输入，结果里有决策披露
        round_dir("CL-20991231-03", "r1", {
            "任务.txt": "项目：测试项目\n# 等输入的活\n",
            "回执.json": json.dumps({"工人": "opencode", "模型": "deepseek/deepseek-flash", "开始": iso(t - 300), "结束": iso(t - 200),
                                  "退出码": 0, "状态": "执行完成", "用量": {"input": 100, "output": 20, "reasoning": 5}}),
            "结果.txt": "## 1. 结论\n没做完\n## 4. 决策披露\n- 空行跳过\n## 5. 状态\n状态：需要输入 —— 标点怎么切没写\n",
        })
        # 4) 做完并验收通过（表现记录里写了通过），里面放一个二进制文件和一个带密钥的文件
        round_dir("CL-20991231-04", "r1", {
            "任务.txt": "项目：别的项目\n# 通过的活\n",
            "回执.json": json.dumps({"工人": "codex", "模型": "gpt-6.1-sol", "思考档位": "high", "开始": iso(t - 900), "结束": iso(t - 800),
                                  "退出码": 0, "状态": "执行完成", "用量": {"input_tokens": 1000, "output_tokens": 50}}),
            "结果.txt": "全部完成\n",
            "图.png": b"\x89PNG\x00\x00\x00binary",
            "泄露.txt": "key=sk-abcdefghijklmnopqrstuvwxyz123456\nsha=" + "a" * 64 + "\n",
        })
        # 5) 测试目录（不是正式任务号）
        round_dir("GATE-20991231", "x", {"回执.json": json.dumps({"状态": "超时", "退出码": 124, "开始": iso(t - 50), "结束": iso(t - 40)})})
        with open(os.path.join(cls.tmp, "表现记录.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"任务号": "CL-20991231-04", "轮次": "r1", "通过验收": True}, ensure_ascii=False) + "\n")

        # 全部路径与端口走临时配置；保留原四块规则的用例，登记数据改用账本。
        cls.ledger_path = os.path.join(cls.tmp, "账本.jsonl")
        cls.config_path = os.path.join(cls.tmp, "config.json")
        cls.cfg = {"version": 1, "work_dir": cls.tmp, "board_port": 18799,
                   "cards_dir": cls.runs, "perf_file": os.path.join(cls.tmp, "表现记录.jsonl"),
                   "codex": {"proxy": "", "default_fast": True}}
        with open(cls.config_path, "w", encoding="utf-8") as fh:
            json.dump(cls.cfg, fh)
        cls.env_patch = patch.dict(os.environ, {"HOME": cls.tmp, "FRCLYO_HOME": cls.tmp, "FRCLYO_CONFIG": cls.config_path,
                                               "BOARD_NO_BACKGROUND": "1", "BOARD_BALANCE": "0"})
        cls.env_patch.start()
        # 直接加载仓库公共库，测试不再寻找上游交付或旧账本。
        cls.old_sys_path = sys.path[:]
        sys.path.insert(0, os.path.join(os.path.dirname(HERE), "bin"))
        sys.path.insert(0, HERE)
        import board
        cls.b = importlib.reload(board)

    @classmethod
    def tearDownClass(cls):
        # 连同 sleep 子进程一起结束，不然它会占着输出管道
        os.killpg(cls.proc.pid, 9)
        cls.proc.wait()
        shutil.rmtree(cls.tmp)
        cls.env_patch.stop()
        sys.path[:] = cls.old_sys_path

    def setUp(self):
        # 每个测试重建登记基线；不改变原有状态规则和断言。
        if os.path.exists(self.ledger_path):
            os.unlink(self.ledger_path)
        self.projects_path = os.path.join(self.tmp, "配置", "项目表.json")
        os.makedirs(os.path.dirname(self.projects_path), exist_ok=True)
        if os.path.exists(self.projects_path):
            os.unlink(self.projects_path)
        self.b._state_cache.clear()
        self.b.HEALTH.update(codex=None, 余额=[], 日审=None)
        self.ledger_events("CL-20991231-05", ("卡住", {"原因": "契约冲突，需要拍板"}), title="卡住的活", project="测试项目")
        self.ledger_events("CL-20991231-03", title="看板标题", project="测试项目")
        self.ledger_events("CL-20991231-06", title="刚开卡", project="测试项目")

    def ledger_events(self, key, *actions, title="账本标题", project="账本项目"):
        # 通过真实账本格式造事件，不伪造看板内部状态，检验整条读取链路。
        events = [self.b.ledger.event("开卡", key, 标题=title, 项目=project, 来源=key[:2], 派活人="Codex 甲", 父任务="")]
        for action, fields in actions:
            events.append(self.b.ledger.event(action, key, **fields))
        with open(self.ledger_path, "a", encoding="utf-8") as fh:
            for e in events:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")
        return events

    # ---------- 1. 四块内容判断对不对 ----------
    def test_long_task_and_result_parse_before_display_truncation(self):
        with tempfile.TemporaryDirectory(dir=HERE) as directory:
            card = '项目：中文 项目\n# 路径验证\n' + '材料' * 60_000
            result = ('内容\n' * 70_000) + '状态：需要输入（补充材料）\n'
            for name, content in (('任务.txt', card), ('结果.txt', result)):
                with open(os.path.join(directory, name), 'w', encoding='utf-8') as stream:
                    stream.write(content)
            row = self.b.load_round('CL-20991231-99', 'r1', directory)
            self.assertEqual(row['项目'], '中文 项目')
            self.assertEqual(row['标题'], '路径验证')
            self.assertTrue(row['有结果'])
            self.assertIn('需要输入', row['需输入'])
            self.assertEqual(row['_result'], result)
            self.assertIn('显示已截断', self.b.display_text(card, 100_000))
            self.assertIn('显示已截断', self.b.display_text(result, 200_000))
            with patch.dict(os.environ, {'WORKER_RESULT_MAX_BYTES': '100'}):
                limited = self.b.load_round('CL-20991231-99', 'r1', directory)
            self.assertFalse(limited['有结果'])
            os.unlink(os.path.join(directory, '结果.txt'))
            os.symlink(os.path.join(directory, '任务.txt'), os.path.join(directory, '结果.txt'))
            self.assertIsNone(self.b.load_round('CL-20991231-99', 'r1', directory)['_result'])

    def test_sections(self):
        s = self.b.build_state()
        running = {x["任务"] for x in s["正在干活"]}
        self.assertIn("CL-20991231-01", running)
        self.assertIn("CL-20991231-06", running)        # 刚认领：显示"准备中"
        self.assertNotIn("CL-20991231-04", running)     # 跑完没收卡：不算在干活
        attn = {(x.get("任务"), x["事情"]) for x in s["要你处理"]}
        self.assertNotIn("CL-20991231-02", {task for task, _ in attn})
        self.assertIn(("CL-20991231-03", "工人停下来等输入"), attn)
        self.assertIn(("CL-20991231-05", "卡住了"), attn)
        blocked = next(x for x in s["要你处理"] if x.get("任务") == "CL-20991231-05")
        self.assertIn("契约冲突", blocked["细节"])
        # 红色排在前面
        levels = [x["级别"] for x in s["要你处理"]]
        self.assertEqual(levels, sorted(levels, key=lambda v: {"red": 0, "amber": 1, "gray": 2}[v]))
        recent = {x["任务"]: x for x in s["最近完成"]}
        self.assertEqual(recent["CL-20991231-04"]["结论"], "未登记")
        self.assertEqual(recent["CL-20991231-03"]["结论"], "等输入")
        self.assertEqual(recent["CL-20991231-03"]["标题"], "看板标题")   # 看板上的标题优先
        self.assertEqual(recent["CL-20991231-03"]["tokens"], 125)
        # "全部"不含测试目录
        self.assertNotIn("GATE-20991231", recent)

    def test_unregistered_rounds_gray_and_no_attention(self):
        import copy
        original, _ = self.b.build_tasks()
        for key in ("TEST-20991231-01", "CL-20991231-99"):
            for status in ("failed", "timeout", "interrupted", "done", "running"):
                for needs_input in (None, "需要输入"):
                    with self.subTest(key=key, status=status, needs_input=needs_input):
                        task = copy.deepcopy(original["CL-20991231-03"])
                        task.update(任务=key, 看板=None)
                        task["轮次"][-1].update(状态=status, 需输入=needs_input, 验收=None)
                        label = "测试" if key.startswith("TEST-") else "未登记"
                        suffix = self.b.VERDICT_TEXT.get(status)
                        expected = ("进行中", "run") if status == "running" else (
                            label + ("（%s）" % suffix if suffix else ""), "gray")
                        self.assertEqual(self.b.task_status(task), expected)
                        with patch.object(self.b, "build_tasks", return_value=({key: task}, [])):
                            state = self.b.build_state()
                        self.assertEqual(state["要你处理"], [])
                        if status == "running":
                            self.assertEqual(len(state["正在干活"]), 1)
                        else:
                            self.assertEqual(state["最近完成"][0]["结论级别"], "gray")
        self.assertEqual(self.b.build_task("CL-20991231-04")["结论级别"], "gray")

    def test_project_registry_and_conversation(self):
        self.assertEqual(self.b.read_projects(), {})
        self.assertEqual(self.b.build_task("CL-20991231-03")["负责对话"], "未登记")
        with open(self.projects_path, "w", encoding="utf-8") as f:
            json.dump({"项目": {"测试项目": {"负责对话": "语料对话", "说明": "测试"}}}, f)
        self.assertEqual(self.b.read_projects(), {"测试项目": "语料对话"})
        state = self.b.build_state()
        item = next(x for x in state["要你处理"] if x.get("任务") == "CL-20991231-03")
        self.assertIn("发到「语料对话」对话", item["怎么办"])
        for section in ("要你处理", "正在干活", "最近完成"):
            for row in state[section]:
                if row.get("项目") == "测试项目":
                    self.assertEqual(row["负责对话"], "语料对话")
        self.assertEqual(self.b.build_task("CL-20991231-03")["负责对话"], "语料对话")
        for bad in ('{', '[]', '{"项目": []}', '{"项目": {"甲": null}}',
                    '{"项目": {"甲": {"负责对话": 12}}}'):
            with self.subTest(bad=bad):
                with open(self.projects_path, "w", encoding="utf-8") as f:
                    f.write(bad)
                self.assertEqual(self.b.read_projects(), {})
                self.assertEqual(self.b.build_task("CL-20991231-03")["负责对话"], "未登记")

    def test_running_project_groups_in_page(self):
        task_dir = os.path.join(self.deliver, "CX-20991231-98")
        rnd = os.path.join(task_dir, "r1")
        os.makedirs(rnd)
        self.addCleanup(shutil.rmtree, task_dir)
        with open(os.path.join(rnd, "运行信息.json"), "w", encoding="utf-8") as f:
            json.dump({"工人": "opencode", "模型": "test", "开始": iso(time.time()),
                       "进程号": self.proc.pid, "超时秒": 600}, f)
        self.ledger_events("CX-20991231-98", project="另一项目")
        with open(self.projects_path, "w", encoding="utf-8") as f:
            json.dump({"项目": {"测试项目": {"负责对话": "语料对话"},
                                "另一项目": {"负责对话": "框架对话"}}}, f)
        rows = self.b.build_state()["正在干活"]
        self.assertEqual({r["项目"] for r in rows if r["种类"] == "run"}, {"测试项目", "另一项目"})
        html = self.b.read_text(os.path.join(HERE, "index.html"))
        self.assertIn("groups.size < 2", html)
        self.assertIn("负责对话：", html)
        self.assertIn("prefers-color-scheme: dark", html)
        # 框架仍只要求 Python；本机有 Node 时额外执行真实页面渲染函数。
        node = shutil.which("node")
        if not node:
            self.skipTest("缺少 Node，跳过实际前端渲染回归")
        functions = html[html.index("const PROJECT_COLORS"):html.index("// 每秒刷新计时器")]
        script = r"""
const assert = require('assert');
const output = {}, counters = {};
const $ = id => counters[id] || (counters[id] = {});
const put = (id, value) => output[id] = value;
const esc = value => String(value == null ? '' : value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;');
const who = () => '工人', clock = () => '', nowS = () => 0, dur = () => '', ktok = () => '', ago = () => '';
const dismissed = {}, S = {};
""" + functions + "\nconst rows = " + json.dumps(rows, ensure_ascii=False) + r""";
renderRunning(rows);
assert.equal((output.running.match(/class="project-group"/g) || []).length, 2);
assert(output.running.includes('负责对话：语料对话'));
assert(output.running.includes('负责对话：框架对话'));
renderRunning(rows.filter(x => x.项目 === '测试项目'));
assert(!output.running.includes('class="project-group"'));
const tag = projectTag('测试项目');
assert(output.running.includes(tag));
renderAttention([{项目:'测试项目', 级别:'amber'}]);
renderRecent([{项目:'测试项目'}]);
assert(output.attn.includes(tag));
assert(output.recent.includes(tag));
assert(!projectTag('<img src=x>').includes('<img'));
renderRunning([{项目:'<项目>', 负责对话:'<script>', 任务:'一'}, {项目:'另一项目', 任务:'二'}]);
assert(!output.running.includes('<script>'));
assert(output.running.includes('负责对话：未登记'));
renderRunning([]);
assert(output.running.includes('现在没有工人在干活'));
"""
        result = subprocess.run([node, "-e", script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_project_filter(self):
        s = self.b.build_state("别的项目")
        self.assertEqual([x["任务"] for x in s["最近完成"]], ["CL-20991231-04"])
        self.assertEqual(s["正在干活"], [])      # 在跑的那件属于"测试项目"
        self.assertTrue(all(x.get("项目") in (None, "别的项目") for x in s["要你处理"]))
        s2 = self.b.build_state("测试与验收")
        self.assertIn("GATE-20991231", [x["任务"] for x in s2["最近完成"]])

    # ---------- 2. 详情 ----------
    def test_task_detail(self):
        d = self.b.build_task("CL-20991231-03")
        r = d["轮次"][0]
        self.assertEqual(r["决策披露"], "- 空行跳过")
        self.assertIn("需要输入", r["需输入"])
        self.assertTrue(any(f["路径"] == "结果.txt" for f in r["文件"]))
        self.assertIsNone(self.b.build_task("不存在"))

    def test_proc_alive_not_fooled(self):
        # 正反例都由夹具提供；真实 proc_alive 实现仍负责识别命令行。
        for command, expected in (('/System/Library/mdworker.shared', False),
                                  ('python ordinary.py', False),
                                  ('/bin/bash /fixture/codex-worker.sh', True),
                                  ('/bin/bash /fixture/opencode-worker.sh', True)):
            with self.subTest(command=command), patch.object(self.b.os, 'kill'), \
                 patch.object(self.b.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, stdout=command)):
                self.assertEqual(self.b.proc_alive(12345), expected)
        with patch.object(self.b.os, 'kill', side_effect=ProcessLookupError), \
             patch.object(self.b.subprocess, 'run') as inspect:
            self.assertFalse(self.b.proc_alive(12345))
            inspect.assert_not_called()

    def test_needs_input_forms(self):
        f = self.b.needs_input
        self.assertTrue(f("1. 结论：需要输入（契约没写标点）"))
        self.assertTrue(f("**状态**：需要输入"))
        self.assertIsNone(f("1. 结论：完成\n4. 决策披露：没有需要输入的地方"))

    # ---------- 注入守则剥离：标题/项目取原任务卡，未注入照旧 ----------
    def test_card_title_skips_injected_rules(self):
        injected = ("# 工人守则\n\n1. 只在本轮目录内……\n6. 任务卡……\n"
                    "本轮目录（只能在这里读写，产物都放这里）：/交付/中文 round/r1\n"
                    "\n\n---\n\n项目：真实项目\n# 真实标题\n")
        self.assertEqual(self.b.strip_injected_rules(injected), "项目：真实项目\n# 真实标题\n")
        self.assertEqual(self.b.card_title(injected), "真实标题")
        self.assertEqual(self.b.card_project(injected), "真实项目")
        plain = "项目：普通项目\n# 普通标题\n"
        self.assertEqual(self.b.card_title(plain), "普通标题")
        self.assertEqual(self.b.card_project(plain), "普通项目")

    def test_unregistered_test_round_shows_test(self):
        # TEST- 开头、账本里查不到 → 状态「测试」，灰色，不进「要你处理」
        t = time.time()
        d = os.path.join(self.deliver, "TEST-20991231-01", "r1")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "任务.txt"), "w", encoding="utf-8") as f:
            f.write("# 工人守则\n\n1. 只在本轮目录内……\n\n\n---\n\n项目：测试与验收\n# 冒烟测试\n")
        with open(os.path.join(d, "回执.json"), "w", encoding="utf-8") as f:
            json.dump({"工人": "opencode", "模型": "deepseek/deepseek-v4-pro", "开始": iso(t - 100),
                       "结束": iso(t - 50), "退出码": 0, "状态": "执行完成"}, f)
        s = self.b.build_state("测试与验收")
        row = next(x for x in s["最近完成"] if x["任务"] == "TEST-20991231-01")
        self.assertEqual(row["结论"], "测试")
        self.assertEqual(row["结论级别"], "gray")
        self.assertEqual(row["标题"], "冒烟测试")
        self.assertNotIn("TEST-20991231-01", {x.get("任务") for x in s["要你处理"]})

    def test_ledger_ready_with_finished_round_is_pending(self):
        # 账本里有、只是还没收卡 → 仍是「待验收」
        key = "CL-20991231-07"
        self.ledger_events(key, title="未收卡", project="测试项目")
        t = time.time()
        d = os.path.join(self.deliver, key, "r1")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "任务.txt"), "w", encoding="utf-8") as f:
            f.write("项目：测试项目\n# 未收卡的活\n")
        with open(os.path.join(d, "回执.json"), "w", encoding="utf-8") as f:
            json.dump({"工人": "opencode", "模型": "deepseek/deepseek-v4-pro", "开始": iso(t - 100),
                       "结束": iso(t - 50), "退出码": 0, "状态": "执行完成"}, f)
        row = next(x for x in self.b.build_state()["最近完成"] if x["任务"] == key)
        self.assertEqual(row["结论"], "待验收")

    # ---------- 3. 安全：文件只能看本轮目录里的，二进制不显示，密钥打码 ----------
    def test_file_guard(self):
        f = self.b.read_round_file
        self.assertIsNone(f("CL-20991231-04", "r1", "../../CL-20991231-03/r1/结果.txt")[0])
        self.assertIsNone(f("CL-20991231-04", "..", "r1/结果.txt")[0])
        self.assertIsNone(f("../x", "r1", "结果.txt")[0])
        self.assertIsNone(f("CL-20991231-04", "r1", "/etc/passwd")[0])
        self.assertIsNone(f("CL-20991231-04", "r1", "图.png")[0])
        text, err = f("CL-20991231-04", "r1", "泄露.txt")
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz123456", text)
        self.assertIn("a" * 64, text)       # 校验码不打码

    def test_file_guard_rejects_directory_and_file_symlinks(self):
        # 文件、轮次或任务目录的链接均不能绕过本轮边界。
        base = os.path.join(self.deliver, "CL-20991231-04", "r1")
        target = os.path.join(self.deliver, "CL-20991231-03", "r1")
        links = [(os.path.join(base, "outside.txt"), os.path.join(target, "结果.txt")),
                 (os.path.join(self.deliver, "CL-20991231-04", "linked"), target),
                 (os.path.join(self.deliver, "LINK-TEST"), self.tmp)]
        try:
            for path, dest in links:
                os.symlink(dest, path)
            for task, rnd, rel in [("CL-20991231-04", "r1", "outside.txt"),
                                   ("CL-20991231-04", "linked", "结果.txt"),
                                   ("LINK-TEST", "runs", "表现记录.jsonl"),
                                   ("CL-20991231-04", ".", "r1/结果.txt"),
                                   ("CL-20991231-04", "..", "CL-20991231-03/r1/结果.txt")]:
                self.assertIsNone(self.b.read_round_file(task, rnd, rel)[0])
        finally:
            for path, _ in links:
                os.unlink(path)

    def test_round_reads_reject_leaf_symlinks(self):
        task = "CL-20991231-91"
        with tempfile.TemporaryDirectory(dir=self.deliver) as targets:
            rdir = os.path.join(self.deliver, task, "r1")
            os.makedirs(rdir)
            self.addCleanup(shutil.rmtree, os.path.dirname(rdir))
            marker = "SYNTHETIC_PRIVATE_SENTINEL"
            contents = {
                "任务.txt": "# " + marker,
                "结果.txt": marker,
                "运行信息.json": json.dumps({"工人": marker}),
                "回执.json": json.dumps({"工人": marker, "状态": "执行完成"}),
                "events.jsonl": json.dumps({"type": "item.completed", "item": {
                    "type": "agent_message", "text": marker}}),
            }
            for name, content in contents.items():
                target = os.path.join(targets, name)
                with open(target, "w") as f:
                    f.write(content)
                os.symlink(target, os.path.join(rdir, name))
            # 同轮内的链接也不应被 realpath 洗成普通文件。
            with open(os.path.join(rdir, "ordinary.txt"), "w") as f:
                f.write("ordinary")
            os.symlink("ordinary.txt", os.path.join(rdir, "alias.txt"))
            for name in contents:
                with self.subTest(name=name):
                    path = os.path.join(rdir, name)
                    self.assertIsNone(self.b.read_text(path))
                    self.assertIsNone(self.b.read_json(path))
                    self.assertEqual(self.b.parse_events(path)["条目"], [])
                    self.assertIsNone(self.b.read_round_file(task, "r1", name)[0])
            self.assertIsNone(self.b.read_round_file(task, "r1", "alias.txt")[0])
            for value in (self.b.load_round(task, "r1", rdir), self.b.build_state(),
                          self.b.build_task(task)):
                self.assertNotIn(marker, json.dumps(value))
            self.assertEqual(self.b.read_round_file(task, "r1", "ordinary.txt"), ("ordinary", None))

    def test_round_fifo_reads_do_not_block(self):
        task = "CL-20991231-92"
        rdir = os.path.join(self.deliver, task, "r1")
        os.makedirs(rdir)
        self.addCleanup(shutil.rmtree, os.path.dirname(rdir))
        ordinary = os.path.join(rdir, "ordinary.txt")
        with open(ordinary, "w") as f:
            f.write("ordinary")
        for name in ("运行信息.json", "回执.json", "任务.txt", "结果.txt", "events.jsonl"):
            with self.subTest(name=name):
                path = os.path.join(rdir, name)
                os.mkfifo(path)
                try:
                    script = """
import board, json, sys
path, task = sys.argv[1:]
# 正常文件必须读得到，避免权限拒绝让 FIFO 检查假通过。
assert board.read_round_file(task, 'r1', 'ordinary.txt') == ('ordinary', None)
assert board.read_json(path) is None
assert board.read_text(path) is None
assert board.parse_events(path)['条目'] == []
assert board.read_round_file(task, 'r1', path.rsplit('/', 1)[-1])[0] is None
assert board.build_task(task) is not None
board.build_state()
board.receipt_snapshot()
print('FIFO rejected; state and detail returned')
"""
                    result = subprocess.run([sys.executable, "-B", "-c", script, path, task],
                                            cwd=HERE, capture_output=True, text=True, timeout=3)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                finally:
                    os.unlink(path)

    def test_round_reads_reject_oversize_and_nonregular(self):
        with tempfile.TemporaryDirectory(dir=self.tmp) as d:
            path = os.path.join(d, "large")
            with open(path, "wb") as f:
                f.write(b'{}')
                f.truncate(64 * 1024 * 1024 + 1)
            self.assertIsNone(self.b.read_text(path))
            self.assertIsNone(self.b.read_json(path))
            self.assertEqual(self.b.parse_events(path)["条目"], [])
            self.assertIsNone(self.b.read_text(d))
            self.assertIsNone(self.b.read_json(d))
            with open(path, "wb") as f:
                f.write(b"abcd")
            self.assertEqual(self.b.read_text(path, 4), "abcd")
            self.assertIsNone(self.b.read_text(path, 3))

    def test_events_cache_cannot_bypass_safe_read(self):
        with tempfile.TemporaryDirectory(dir=self.tmp) as d:
            path, target = os.path.join(d, "events"), os.path.join(d, "target")
            with open(path, "w") as f:
                f.write(json.dumps({"type": "thread.started"}))
            self.assertTrue(self.b.parse_events(path)["条目"])
            os.rename(path, target)
            os.symlink(target, path)
            self.assertEqual(self.b.parse_events(path)["条目"], [])

    def test_timeout_requires_finite_positive_number(self):
        with tempfile.TemporaryDirectory(dir=self.tmp) as d:
            for value in ('"><img src=x onerror="void(0)">', "600", True, 0, -1, float("inf"), float("nan")):
                with self.subTest(value=value):
                    with open(os.path.join(d, "运行信息.json"), "w") as f:
                        json.dump({"超时秒": value}, f)
                    self.assertIsNone(self.b.load_round("CL-20991231-93", "r1", d)["超时秒"])
            with open(os.path.join(d, "运行信息.json"), "w") as f:
                json.dump({"超时秒": 600}, f)
            self.assertEqual(self.b.load_round("CL-20991231-93", "r1", d)["超时秒"], 600)

    def test_round_html_escapes_untrusted_values(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("缺少 Node，跳过实际前端渲染回归")
        with open(os.path.join(HERE, "index.html")) as f:
            html = f.read()
        js = html.split("<script>", 1)[1].split("</script>", 1)[0]
        js = js[:js.rindex("readHash();")]
        script = r"""
const assert = require('assert');
const elements = {};
const document = {getElementById: id => elements[id] || (elements[id] = {
  innerHTML: '', textContent: '', addEventListener() {}, classList: {add(){},remove(){}},
  scrollHeight: 0, scrollTop: 0, clientHeight: 0}),
  addEventListener(){}, querySelectorAll: () => []};
const window = {addEventListener(){}};
const localStorage = {getItem: () => null};
const setInterval = () => 0;
""" + js + r"""
const payload = '"><img src=x onerror="void(0)">';
const base = {任务:'test', 项目:'p', 轮次:'r1', 状态:'running', 开始:1, 超时秒:600,
  时间线:[], 文件:[]};
function clean() {
  for (const el of Object.values(elements)) {
    assert(!el.innerHTML.includes('<img'), el.innerHTML);
    assert(!el.innerHTML.includes('NaN'), el.innerHTML);
  }
}
for (const field of ['超时秒', '开始', '步数', '标题', '任务', '轮次', '工人', '模型', '档位', '最近一步']) {
  renderRunning([{...base, [field]:payload}]); clean();
}
for (const field of ['结束', '轮数', 'tokens', '费用', '用时秒']) {
  renderRecent([{...base, [field]:payload}]); clean();
}
for (const field of ['开始', '超时秒', '步数', '费用', '退出码', '任务卡', '交付说明']) {
  S.open = {round:'r1', tab:'progress'};
  S.detail = {任务:'test', 轮次:[{...base, [field]:payload}]};
  renderDrawer(); clean();
}
S.open = {round:'r1', tab:'progress'};
S.detail = {任务:'test', 轮次:[{...base, 用量:{输入:payload, 输出:payload}, 时间线总数:payload}]};
renderDrawer(); clean();
S.open.tab = 'files'; S.detail.轮次[0].文件 = [{路径:payload, 大小:payload}];
renderDrawer(); clean();
for (const invalid of ['600', true, null, Infinity, NaN, payload]) {
  renderRunning([{...base, 超时秒:invalid}]);
  assert(!elements.running.innerHTML.includes('data-timeout=')); clean();
}
renderRunning([base]);
assert(elements.running.innerHTML.includes('data-timeout="600"'));
assert(elements.running.innerHTML.includes('data-start="1"'));
assert.equal(ktok(1200), '1.2k'); assert.equal(fsize(512), '512 B');
console.log('round HTML regression passed');
"""
        result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    # ---------- 4. 事件流翻译 ----------
    def test_events(self):
        p = os.path.join(self.tmp, "ev.jsonl")
        with open(p, "w", encoding="utf-8") as fh:
            for e in [
                {"type": "thread.started"},
                {"type": "item.started", "item": {"type": "command_execution", "command": "/bin/zsh -lc \"ls -la\""}},
                {"type": "item.completed", "item": {"type": "command_execution", "command": "x", "exit_code": 1, "aggregated_output": "boom"}},
                {"type": "item.completed", "item": {"type": "file_change", "changes": [{"path": "/w/a.py", "kind": "add"}]}},
                {"type": "step_finish", "part": {"cost": 0.002}},
                {"type": "step_finish", "part": {"cost": 0.001}},
                {"type": "error", "error": {"name": "X", "data": {"message": "server down"}}},
            ]:
                fh.write(json.dumps(e) + "\n")
            fh.write("这一行不是 JSON\n")
        ev = self.b.parse_events(p, "/w")
        kinds = [x["k"] for x in ev["条目"]]
        self.assertEqual(kinds, ["start", "tool", "warn", "edit", "error"])
        self.assertEqual(ev["条目"][1]["s"], "运行命令：ls -la")
        self.assertIn("a.py（新建）", ev["条目"][3]["s"])
        self.assertAlmostEqual(ev["费用"], 0.003)
        self.assertEqual(ev["步数"], 2)

    # ---------- 5. 读不到交付目录时不崩，给出说明 ----------
    def test_missing_deliver(self):
        old = self.b.DELIVER
        try:
            self.b.DELIVER = os.path.join(self.tmp, "没有这个目录")
            self.b._probe["时间"] = 0
            s = self.b.build_state()
            self.assertTrue(any(x["事情"] == "看板有数据读不到" for x in s["要你处理"]))
        finally:
            self.b.DELIVER = old
            self.b._probe["时间"] = 0

    # ---------- 6. 网页服务：只认本机地址，只接受 GET ----------
    def memory_http(self, request):
        """把原始 HTTP 字节送进同一个 Handler，并用标准库解析真实响应字节。"""
        req = request if isinstance(request, urllib.request.Request) else urllib.request.Request(request)
        url = urllib.parse.urlsplit(req.full_url)
        headers = dict(req.header_items())
        headers.setdefault("Host", url.netloc)
        headers["Connection"] = "close"
        data = req.data or b""
        if data:
            headers["Content-Length"] = str(len(data))
        raw = (req.get_method() + " " + (url.path or "/") + ("?" + url.query if url.query else "") +
               " HTTP/1.1\r\n" + "".join(k + ": " + v + "\r\n" for k, v in headers.items()) + "\r\n").encode("utf-8") + data

        class MemorySocket:
            # Handler 的读写接口与 socket 相同，但无网络权限需求。
            def __init__(self, value):
                self.incoming = value
                self.outgoing = bytearray()

            def makefile(self, *args):
                return io.BytesIO(self.incoming)

            def sendall(self, value):
                self.outgoing.extend(value)

        sock = MemorySocket(raw)
        self.b.Handler(sock, ("127.0.0.1", 12345), object())
        response = http.client.HTTPResponse(MemorySocket(bytes(sock.outgoing)))
        response.begin()
        if response.status >= 400:
            error = urllib.error.HTTPError(req.full_url, response.status, response.reason, response.headers, response)
            response.close()
            raise error
        return response

    def test_http(self):
        # 正常环境仍走真实 TCP；沙箱禁止 bind 时仅替换传输，不跳过原安全断言。
        srv = None
        open_http = urllib.request.build_opener(urllib.request.ProxyHandler({})).open
        try:
            srv = self.b.ThreadingHTTPServer(("127.0.0.1", 0), self.b.Handler)
        except PermissionError:
            print("HTTP 自检：沙箱禁止监听端口，改用内存传输运行真实 Handler", file=sys.stderr)
            open_http = self.memory_http
        else:
            port_patch = patch.object(self.b, "PORT", srv.server_address[1])
            port_patch.start()
            self.addCleanup(port_patch.stop)
            th = threading.Thread(target=srv.serve_forever, daemon=True)
            th.start()
        try:
            base = "http://127.0.0.1:%d" % self.b.PORT
            with open_http(base + "/") as r:
                self.assertIn("工作看板", r.read().decode("utf-8"))
            with open_http(base + "/api/state") as r:
                self.assertIn("要你处理", json.loads(r.read().decode("utf-8")))
            with open_http(base + "/api/task?id=CL-20991231-04") as r:
                self.assertEqual(json.loads(r.read().decode("utf-8"))["结论"], "未登记")
            # 伪造的 Host（DNS 重绑定攻击的样子）要被拒绝
            req = urllib.request.Request(base + "/api/state", headers={"Host": "evil.example:%d" % self.b.PORT})
            with self.assertRaises(urllib.error.HTTPError) as cm:
                open_http(req)
            self.assertEqual(cm.exception.code, 403)
            # 不接受 POST
            with self.assertRaises(urllib.error.HTTPError) as cm:
                open_http(urllib.request.Request(base + "/api/state", data=b"x", method="POST"))
            self.assertEqual(cm.exception.code, 501)
            # 按任务卡只接受 GET，HEAD 也不能触发查询或日志写入。
            with self.assertRaises(urllib.error.HTTPError) as cm:
                open_http(urllib.request.Request(base + "/api/log?m=x", method="HEAD"))
            self.assertEqual(cm.exception.code, 501)
            # 错误日志接口：别的网站发来的拒绝；自己页面发来的接受，但每分钟最多 20 条
            with self.assertRaises(urllib.error.HTTPError) as cm:
                open_http(urllib.request.Request(base + "/api/log?m=x", headers={"Sec-Fetch-Site": "cross-site"}))
            self.assertEqual(cm.exception.code, 403)
            before = len(self.b._log_times)
            for _ in range(30):
                with open_http(urllib.request.Request(base + "/api/log?m=test", headers={"Sec-Fetch-Site": "same-origin"})) as response:
                    response.read()
            self.assertLessEqual(len(self.b._log_times), 20)
            # 文件接口拒绝跳出本轮目录
            with self.assertRaises(urllib.error.HTTPError) as cm:
                open_http(base + "/api/file?id=CL-20991231-04&round=r1&path=..%2F..%2FCL-20991231-03%2Fr1%2F结果.txt".replace("结果", "%E7%BB%93%E6%9E%9C"))
            self.assertEqual(cm.exception.code, 400)
        finally:
            if srv is not None:
                srv.shutdown()
                srv.server_close()
                th.join(timeout=5)
                self.assertFalse(th.is_alive())


    def test_ledger_ready_and_custom_source(self):
        """CX 和未来的两字母来源都属于正式任务；开卡无轮次显示准备中。"""
        for key in ("CX-20991231-01", "ZZ-20991231-01"):
            self.ledger_events(key)
            state = self.b.build_state("账本项目")
            self.assertIn(key, {x["任务"] for x in state["正在干活"]})
            self.assertEqual(self.b.build_task(key)["结论"], "准备中")
            self.assertNotIn(key, {x["任务"] for x in state["最近完成"]})

    def test_ledger_block_reason_and_unblock(self):
        """卡住进入要你处理并显示原因；恢复后重回准备中。"""
        key = "CX-20991231-02"
        self.ledger_events(key, ("卡住", {"原因": "需要确认接口"}))
        row = next(x for x in self.b.build_state()["要你处理"] if x.get("任务") == key)
        self.assertIn("需要确认接口", row["细节"])
        self.assertIn("发到负责这个项目的对话", row["怎么办"])
        with open(self.ledger_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(self.b.ledger.event("恢复", key), ensure_ascii=False) + "\n")
        self.assertEqual(self.b.build_task(key)["结论"], "准备中")

    def test_ledger_done_verdict_overrides_perf(self):
        """旧表现记录是通过，账本为未通过时，以账本结论为准。"""
        key = "CL-20991231-04"
        self.ledger_events(key, ("完成", {"结论": "未通过", "摘要": "验收发现问题"}))
        row = next(x for x in self.b.build_state()["最近完成"] if x["任务"] == key)
        self.assertEqual(row["结论"], "未通过")
        detail = self.b.build_task(key)
        self.assertEqual(detail["轮次"][-1]["验收"], "未通过")
        self.assertEqual(detail["标题"], "账本标题")

    def test_ledger_done_without_round(self):
        """没有轮次的通过卡也显示账本结论，证据缺失由日审负责报告。"""
        key = "CX-20991231-03"
        self.ledger_events(key, ("完成", {"结论": "通过", "摘要": "人工验收"}))
        row = next(x for x in self.b.build_state()["最近完成"] if x["任务"] == key)
        self.assertEqual((row["结论"], row["结论级别"]), ("通过", "ok"))

    def test_ledger_drop_gray_suppresses_old_interrupt(self):
        """放弃显示灰色，旧轮次的中断提醒不会继续要求处理。"""
        for key in ("CL-20991231-02", "CX-20991231-04"):
            self.ledger_events(key, ("放弃", {"原因": "方案变更"}))
            state = self.b.build_state()
            row = next(x for x in state["最近完成"] if x["任务"] == key)
            self.assertEqual((row["结论"], row["结论级别"]), ("已放弃", "gray"))
            self.assertNotIn(key, {x.get("任务") for x in state["要你处理"]})

    def test_ledger_detail_all_events_and_dispatcher(self):
        """抽屉包含完整中文事件和派活人，前端仍使用原刷新接口。"""
        key = "CX-20991231-05"
        events = self.ledger_events(key, ("备注", {"内容": "补充契约"}), ("卡住", {"原因": "缺输入"}),
                                    ("恢复", {}), ("完成", {"结论": "通过", "摘要": "核对完成"}))
        detail = self.b.build_task(key)
        self.assertEqual(detail["看板"]["派活人"], "Codex 甲")
        self.assertEqual([x["内容"] for x in detail["看板"]["事件"]], events)
        html = self.b.read_text(os.path.join(HERE, "index.html"))
        self.assertIn("账本记录", html)
        self.assertNotIn("看板记录", html)
        self.assertIn(".badge.gray", html)

    def test_ledger_events_sorted_by_time_stable(self):
        """同一张卡的事件按时间稳定排序，与账本追加顺序无关。"""
        key = "CX-20991231-10"
        events = [
            {"时间": "2026-01-01T10:00:00Z", "动作": "开卡", "任务": key,
             "标题": "t", "项目": "p", "来源": "CX", "派活人": "x", "父任务": ""},
            {"时间": "2026-01-01T11:00:00Z", "动作": "备注", "任务": key, "内容": "晚"},
            {"时间": "2026-01-01T09:00:00Z", "动作": "备注", "任务": key, "内容": "早"},
        ]
        with open(self.ledger_path, "a", encoding="utf-8") as fh:
            for e in events:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")
        card = self.b.read_ledger()[key]
        times = [e["时间"] for e in card["事件"]]
        self.assertEqual(times, sorted(times))
        notes = [e["内容"]["内容"] for e in card["事件"] if e["内容"].get("内容")]
        self.assertEqual(notes, ["早", "晚"])

    def test_ledger_corrupt_is_visible(self):
        """坏账本显式出现在数据错误提醒，不假装账本为空。"""
        with open(self.ledger_path, "w", encoding="utf-8") as fh:
            fh.write("坏行\n")
        self.assertTrue(any("读账本失败" in x["细节"] for x in self.b.build_state()["要你处理"]))

    # ---------- 7. 配置与公共库：临时配置、默认值和优先导入 ----------
    def test_legacy_card_path_maps_without_rewriting_book(self):
        # 看板走公共账本读取，映射后的路径可打开，旧账本字节保持原样。
        key = 'CL-20991231-88'
        row = self.b.ledger.event('开卡', key, 标题='旧卡', 项目='迁移', 来源='CL',
                                  派活人='测试', 父任务='', 卡片文件='runs/old-card.txt')
        with open(self.ledger_path, 'a', encoding='utf-8') as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')
        target = os.path.join(self.runs, 'old-card.txt')
        with open(target, 'w', encoding='utf-8') as stream:
            stream.write('旧任务卡')
        with open(self.ledger_path, 'rb') as stream:
            before = stream.read()
        card = self.b.read_ledger()[key]
        self.assertEqual(card['卡片文件'], target)
        with open(card['卡片文件'], encoding='utf-8') as stream:
            self.assertEqual(stream.read(), '旧任务卡')
        with open(self.ledger_path, 'rb') as stream:
            self.assertEqual(stream.read(), before)

    def test_config_paths_and_port(self):
        self.assertEqual(self.b.PORT, self.cfg["board_port"])
        self.assertEqual(self.b.DELIVER, self.deliver)
        self.assertEqual(self.b.RUNS, self.runs)
        self.assertEqual(self.b.PERF, os.path.join(self.tmp, "表现记录.jsonl"))
        self.assertEqual(self.b.LEDGER, self.ledger_path)
        self.assertEqual(self.b.LOG_FILE, os.path.join(self.tmp, "运行时/看板.log"))
        # 原环境变量不能盖过契约规定的唯一配置来源。
        with patch.dict(os.environ, {"BOARD_PORT": "1", "BOARD_DELIVER": "/wrong"}):
            self.assertEqual(self.b.config.load()["board_port"], self.cfg["board_port"])
            self.assertEqual(self.b.config.path_of("deliver_dir"), self.deliver)

    def test_config_defaults_and_tilde(self):
        path = os.path.join(self.tmp, "partial.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"work_dir": "~/isolated-work", "cards_dir": "~/cards", "perf_file": "~/perf.jsonl",
                       "codex": {"proxy": "http://127.0.0.1:1234"},
                       "providers": {"deepseek": {"api_host": "balance.example"}}}, fh)
        with patch.dict(os.environ, {"FRCLYO_CONFIG": path}):
            cfg = self.b.config.load()
            self.assertEqual(cfg["board_port"], 8789)
            self.assertTrue(cfg["codex"]["default_fast"])
            self.assertEqual(cfg["providers"]["deepseek"]["balance_api"], "deepseek")
            self.assertNotIn("moonshotai-cn", cfg["providers"])
            self.assertEqual(self.b.config.path_of("cards_dir"), os.path.join(self.tmp, "任务卡"))
            self.assertEqual(self.b.config.path_of("perf_file"), os.path.join(self.tmp, "表现记录.jsonl"))
            self.assertEqual(self.b.config.path_of("deliver_dir"), os.path.join(self.tmp, "交付"))
        with patch.dict(os.environ, {"FRCLYO_CONFIG": os.path.join(self.tmp, "absent.json")}):
            self.assertEqual(self.b.config.path_of("cards_dir"), os.path.join(self.tmp, "任务卡"))
            self.assertEqual(self.b.config.load()["codex"]["proxy"], "")

    def test_runtime_modules_are_real(self):
        # 确认四个公共库就是生产包，避免回归到独立配置解析。
        from frclyo import config, keys, ledger, audit
        for name, module in (("config", config), ("keys", keys), ("ledger", ledger), ("audit", audit)):
            self.assertIs(getattr(self.b, name), module)

    def test_audit_populates_health_and_attention(self):
        # 通过卡缺回执由真实日审报告，并同步到健康条和提醒。
        self.ledger_events("CL-20991231-09", ("完成", {"结论": "通过", "摘要": "测试"}), title="缺回执")
        self.b.refresh_health("audit")
        self.assertEqual(self.b.HEALTH["日审"]["核对卡数"], 1)
        self.assertEqual(len(self.b.HEALTH["日审"]["问题"]), 1)
        self.assertEqual(self.b.AUDIT_INTERVAL, 3600)

    def test_audit_receipt_exception_is_gray_and_already_explained(self):
        self.ledger_events("CL-20991231-09", ("备注", {"内容": "回执例外：宿主已验收"}),
                           ("完成", {"结论": "通过", "摘要": "测试"}), title="缺回执")
        self.b.refresh_health("audit")
        self.assertEqual(self.b.HEALTH["日审"]["问题"], [])
        self.assertEqual(len(self.b.HEALTH["日审"]["提醒"]), 1)
        rows = [x for x in self.b.build_state()["要你处理"] if x["键"].startswith("audit-")]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["级别"], "gray")
        self.assertEqual(rows[0]["事情"], "回执例外已说明")
        self.assertIn("宿主已验收", rows[0]["细节"])
        self.assertNotIn("待你决定", rows[0]["事情"])

    # ---------- 8. 健康条：启动真实的假 stdio 进程验证协议、超时和回收 ----------
    def fake_app_server(self, mode="ok"):
        # 不碰真实账户；替换命令后仍由操作系统建立管道、进程组，执行完整握手。
        from contextlib import contextmanager
        script = os.path.join(self.tmp, "fake_app_server.py")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write('''import json, sys, time
mode = sys.argv[1]
def receive():
    return json.loads(sys.stdin.readline())
def send(value):
    print(json.dumps(value), flush=True)
first = receive()
assert first["id"] == 1 and first["method"] == "initialize"
assert first["params"]["clientInfo"]["name"] == "board-probe"
if mode == "init_timeout":
    time.sleep(30)
send({"method": "notice"})
send({"id": 1, "result": {"userAgent": "fake"}})
assert receive() == {"method": "initialized"}
assert receive() == {"id": 2, "method": "account/rateLimits/read"}
if mode == "timeout":
    time.sleep(30)
elif mode == "partial":
    sys.stdout.write('{"id": 2,')
    sys.stdout.flush()
    time.sleep(30)
elif mode == "error":
    send({"id": 2, "error": {"code": -32000, "message": "private-error-must-not-escape"}})
elif mode == "invalid":
    send({"id": 2, "result": {"rateLimits": {"primary": {}}}})
elif mode == "exit":
    sys.exit(0)
else:
    send({"id": 999, "result": {}})
    send({"id": 2, "result": {"rateLimits": {"primary": {
        "usedPercent": 16.5, "windowDurationMins": 10080, "resetsAt": 1792487478}, "planType": "plus"}}})
time.sleep(30)
''')
        real_popen = subprocess.Popen
        processes, environments = [], []

        def start(command, **kwargs):
            self.assertEqual(command, ["codex", "app-server"])
            environments.append(kwargs["env"])
            process = real_popen([sys.executable, script, mode], **kwargs)
            processes.append(process)
            return process

        @contextmanager
        def fixture():
            with patch.object(self.b.subprocess, "Popen", side_effect=start):
                try:
                    yield environments
                finally:
                    for process in processes:
                        self.assertIsNotNone(process.poll(), "查询后必须回收进程")
                        self.assertTrue(process.stdin.closed)
                        self.assertTrue(process.stdout.closed)
        return fixture()

    def test_quota_protocol_proxy_and_cleanup(self):
        with patch.dict(self.b.CONFIG, {"codex": {"proxy": "http://127.0.0.1:1234"}}), self.fake_app_server() as envs, patch.object(self.b, "now", return_value=1234):
            quota = self.b.check_codex_quota()
        self.assertEqual(quota["已用"], 16.5)
        self.assertEqual(quota["窗口分钟"], 10080)
        self.assertEqual(quota["重置"], 1792487478)
        self.assertEqual(quota["更新时间"], 1234)
        self.assertEqual(envs[0]["HTTPS_PROXY"], "http://127.0.0.1:1234")
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://unwanted.invalid"}), self.fake_app_server() as envs:
            self.b.check_codex_quota()
        self.assertNotIn("HTTPS_PROXY", envs[0])

    def test_quota_timeouts_including_partial_line(self):
        for mode in ("init_timeout", "timeout", "partial"):
            with self.subTest(mode=mode), patch.object(self.b, "QUERY_TIMEOUT", 0.25), self.fake_app_server(mode):
                started = time.monotonic()
                self.b.refresh_health("codex")
                self.assertLess(time.monotonic() - started, 2)
                self.assertEqual(self.b.HEALTH["codex"]["错误"], "查询超时")
                self.assertNotIn("更新时间", self.b.HEALTH["codex"])

    def test_quota_failure_keeps_value_and_timestamp_then_recovers(self):
        with self.fake_app_server(), patch.object(self.b, "now", return_value=2000 if self.b.HEALTH["codex"] else 1000):
            self.b.refresh_health("codex")
        previous = dict(self.b.HEALTH["codex"])
        for mode in ("error", "invalid", "exit", "timeout"):
            with self.subTest(mode=mode), patch.object(self.b, "QUERY_TIMEOUT", 0.25), self.fake_app_server(mode):
                self.b.refresh_health("codex")
            current = self.b.HEALTH["codex"]
            self.assertEqual(current["已用"], previous["已用"])
            self.assertEqual(current["更新时间"], previous["更新时间"])
            self.assertIn("错误", current)
            self.assertNotIn("private-error", json.dumps(current))
        with self.fake_app_server(), patch.object(self.b, "now", return_value=2000 if self.b.HEALTH["codex"] else 1000):
            self.b.refresh_health("codex")
        self.assertNotIn("错误", self.b.HEALTH["codex"])
        self.assertGreater(self.b.HEALTH["codex"]["更新时间"], previous["更新时间"])

    def test_balances_success_uses_keys_hosts_and_timeout(self):
        # 模拟 HTTP opener，覆盖真实请求构造和超时参数，同时不进行网络访问。
        from unittest.mock import Mock
        def opened(request, timeout):
            self.assertEqual(timeout, 10)
            self.assertEqual(request.get_header("Authorization"), "Bearer unit-test-key")
            if request.full_url == "https://balance.example/user/balance":
                data = {"balance_infos": [{"total_balance": "12.34", "currency": "CNY"}], "is_available": True}
            else:
                self.assertEqual(request.full_url, "https://api.moonshot.cn/v1/users/me/balance")
                data = {"data": {"available_balance": 56.78}}
            return io.BytesIO(json.dumps(data).encode())
        opener = Mock()
        opener.open.side_effect = opened
        cfg = json.loads(json.dumps(self.b.CONFIG))
        cfg["providers"]["deepseek"]["api_host"] = "balance.example"
        cfg["providers"] = {k: v for k, v in cfg["providers"].items() if k in ("deepseek", "moonshotai-cn", "xiaomi")}
        with patch.object(self.b, "CONFIG", cfg), patch.object(self.b.keys, "get_key", return_value="unit-test-key") as get_key, \
                patch.object(self.b.keys, "key_source", return_value={"kind": "keychain"}) as source, \
                patch.object(self.b.urllib.request, "build_opener", return_value=opener):
            self.b.refresh_health("balances")
        rows = {row["名字"]: row for row in self.b.HEALTH["余额"]}
        self.assertEqual(rows["DeepSeek"]["余额"], 12.34)
        self.assertEqual(rows["Kimi"]["余额"], 56.78)
        self.assertIn("更新时间", rows["Kimi"])
        self.assertEqual(rows["MiMo"]["说明"], "没有余额接口")
        self.assertEqual({call.args[0] for call in get_key.call_args_list}, {"deepseek", "moonshotai-cn"})
        self.assertEqual({call.args[0] for call in source.call_args_list}, {"xiaomi"})

    def test_provider_without_balance_api_only_checks_presence(self):
        cfg = dict(self.b.CONFIG, providers={'xiaomi': {}}, keychain_entries={}, key_env_file=None)
        for code, expected in ((0, True), (44, False), (36, False)):
            with self.subTest(code=code), patch.object(self.b, 'CONFIG', cfg), \
                    patch.object(self.b.keys.config, 'load', return_value=cfg), \
                    patch.object(self.b.keys, 'get_key', side_effect=AssertionError('must not read key')) as read, \
                    patch.object(self.b.keys.subprocess, 'run', return_value=subprocess.CompletedProcess([], code)) as run, \
                    patch.object(self.b, '_get_json') as http:
                rows = self.b.check_balances()
                self.assertEqual(bool(rows), expected)
                self.assertTrue(run.called)
                self.assertTrue(all('-w' not in call.args[0] for call in run.call_args_list))
                read.assert_not_called()
                http.assert_not_called()

    def test_balances_failures_preserve_each_provider_and_recover(self):
        providers = {k: v for k, v in self.b.CONFIG["providers"].items() if k in ("deepseek", "moonshotai-cn")}
        config_patch = patch.dict(self.b.CONFIG, providers=providers)
        config_patch.start()
        self.addCleanup(config_patch.stop)
        def response(url, key):
            return {"balance_infos": [{"total_balance": "0", "currency": "CNY"}]} if "/user/" in url else {"data": {"available_balance": 8}}
        with patch.object(self.b.keys, "get_key", return_value="unit-test-key"), \
                patch.object(self.b.keys, "key_source", return_value={"kind": "none"}):
            with patch.object(self.b, "_get_json", side_effect=response):
                self.b.refresh_health("balances")
            old = {row["名字"]: dict(row) for row in self.b.HEALTH["余额"]}
            for failure in (TimeoutError(), urllib.error.HTTPError("https://fake", 503, "private-key", {}, None),
                            ValueError("private-key")):
                with patch.object(self.b, "_get_json", side_effect=failure):
                    self.b.refresh_health("balances")
                for row in self.b.HEALTH["余额"]:
                    self.assertEqual(row["余额"], old[row["名字"]]["余额"])
                    self.assertEqual(row["更新时间"], old[row["名字"]]["更新时间"])
                    self.assertIn("错误", row)
                    self.assertNotIn("private-key", json.dumps(row))
            # 一个 provider 失败不阻止另一家拿到新值；缺字段不能被误报成零余额。
            with patch.object(self.b, "_get_json", side_effect=[{}, {"data": {"available_balance": 9}}]):
                self.b.refresh_health("balances")
            ds, kimi = self.b.HEALTH["余额"]
            self.assertEqual(ds["余额"], 0)
            self.assertIn("错误", ds)
            self.assertEqual(kimi["余额"], 9)
            self.assertNotIn("错误", kimi)
            self.assertGreater(kimi["更新时间"], old["Kimi"]["更新时间"])

    def test_missing_keys_are_visible_without_erasing_balance(self):
        self.b.HEALTH["余额"] = [{"provider": "deepseek", "名字": "DeepSeek", "余额": 20, "更新时间": 123}]
        with patch.object(self.b.keys, "get_key", return_value=None), \
                patch.object(self.b.keys, "key_source", return_value={"kind": "none"}), \
                patch.object(self.b, "_get_json") as http:
            self.b.refresh_health("balances")
        http.assert_not_called()
        ds = self.b.HEALTH["余额"][0]
        self.assertEqual(ds["余额"], 20)
        self.assertEqual(ds["更新时间"], 123)
        self.assertEqual(ds["错误"], "未配置 API key")

    def test_bad_info_does_not_kill_watcher_or_startup(self):
        with tempfile.TemporaryDirectory(dir=os.path.realpath(tempfile.gettempdir())) as delivery, \
                patch.object(self.b, 'DELIVER', delivery), \
                patch.object(self.b, 'probe_deliver', return_value=None), \
                patch.object(self.b, 'log') as log:
            monitor = self.b.HealthMonitor()
            bad = os.path.join(delivery, 'BAD', 'r1')
            good = os.path.join(delivery, 'GOOD', 'r1')
            os.makedirs(bad)
            os.makedirs(good)
            with open(os.path.join(bad, '回执.json'), 'w') as stream:
                json.dump({'状态': '准备失败'}, stream)
            thread = threading.Thread(target=monitor.watch_loop)
            try:
                for value in ([1], [], 'bad', 1, True, None):
                    with open(os.path.join(bad, '运行信息.json'), 'w') as stream:
                        json.dump(value, stream)
                    self.b.HealthMonitor()  # 启动基线也必须能跳过坏轮次。
                thread.start()
                with open(os.path.join(good, '回执.json'), 'w') as stream:
                    json.dump({'工人': 'codex'}, stream)
                self.assertTrue(monitor.events['codex'].wait(3))
                self.assertTrue(thread.is_alive())
                self.assertTrue(log.called)
            finally:
                monitor.stop_event.set()
                if thread.ident is not None:
                    thread.join(2)

    def test_receipt_scan_recovers_from_single_round_read_error(self):
        with tempfile.TemporaryDirectory(dir=os.path.realpath(tempfile.gettempdir())) as delivery, \
                patch.object(self.b, 'DELIVER', delivery), \
                patch.object(self.b, 'probe_deliver', return_value=None), \
                patch.object(self.b, 'log') as log:
            for task in ('bad', 'good'):
                directory = os.path.join(delivery, task, 'r1')
                os.makedirs(directory)
                with open(os.path.join(directory, '回执.json'), 'w') as stream:
                    json.dump({'工人': 'codex'}, stream)
            original = self.b.read_json
            def read(path):
                if os.sep + 'bad' + os.sep in path:
                    raise ValueError('fixture corrupt round')
                return original(path)
            with patch.object(self.b, 'read_json', side_effect=read):
                monitor = self.b.HealthMonitor()
                self.assertEqual(len(monitor.seen), 1)
                self.assertIn(os.path.join(delivery, 'good', 'r1', '回执.json'), monitor.seen)
            self.assertTrue(log.called)

    def test_health_interval_is_sixty_seconds_for_both_channels(self):
        # 虚拟单调时钟检查两通道真实调度循环，测试无需等待一分钟。
        for kind in ("codex", "balances", "audit"):
            monitor = self.b.HealthMonitor()
            clock, calls, waits = [0.0], [], []
            class Timer:
                def wait(self, seconds):
                    waits.append(seconds)
                    clock[0] += seconds
                def clear(self):
                    pass
            monitor.events[kind] = Timer()
            def refresh(channel):
                calls.append((channel, clock[0]))
                if len(calls) == 3:
                    monitor.stop_event.set()
            with patch.object(self.b.time, "monotonic", side_effect=lambda: clock[0]), \
                    patch.object(self.b, "refresh_health", side_effect=refresh):
                monitor.query_loop(kind)
            interval = 3600 if kind == "audit" else 60
            self.assertEqual(calls, [(kind, 0), (kind, interval), (kind, interval * 2)])
            self.assertEqual(waits, [0, interval, interval])

    def test_new_receipts_wake_correct_channel_immediately(self):
        # 使用真实目录和真实等待线程；新回执出现后无需等 60 秒，也无需请求网页。
        monitor = self.b.HealthMonitor()
        counts = {"codex": 0, "balances": 0, "audit": 0}
        notified = {kind: threading.Event() for kind in counts}
        def refresh(kind):
            counts[kind] += 1
            notified[kind].set()
        with patch.object(self.b, "CHECK_BALANCE", True), patch.object(self.b, "refresh_health", side_effect=refresh):
            monitor.start()
            try:
                for event in notified.values():
                    self.assertTrue(event.wait(2))
                    event.clear()
                for worker, channel in (("codex", "codex"), ("opencode", "balances")):
                    directory = os.path.join(self.deliver, "CX-20991231-99", worker)
                    os.makedirs(directory, exist_ok=True)
                    path = os.path.join(directory, "回执.json")
                    with open(path, "w", encoding="utf-8") as fh:
                        fh.write('{"工人":')
                    monitor.poll_receipts()
                    self.assertFalse(notified[channel].is_set())
                    with open(path, "w", encoding="utf-8") as fh:
                        json.dump({"工人": worker, "状态": "执行完成", "退出码": 0}, fh)
                    monitor.poll_receipts()
                    self.assertTrue(notified[channel].wait(2))
                    notified[channel].clear()
                    monitor.poll_receipts()
                    self.assertEqual(counts[channel], 2)
                self.assertEqual(counts, {"codex": 2, "balances": 2, "audit": 1})
            finally:
                monitor.stop()
                shutil.rmtree(os.path.join(self.deliver, "CX-20991231-99"), ignore_errors=True)

    def test_receipt_during_query_is_not_lost(self):
        monitor = self.b.HealthMonitor()
        calls = []
        def refresh(kind):
            calls.append(kind)
            if len(calls) == 1:
                monitor.events[kind].set()
            else:
                monitor.stop_event.set()
        with patch.object(self.b, "refresh_health", side_effect=refresh):
            thread = threading.Thread(target=monitor.query_loop, args=("codex",), daemon=True)
            thread.start()
            thread.join(timeout=2)
            try:
                self.assertFalse(thread.is_alive())
                self.assertEqual(calls, ["codex", "codex"])
            finally:
                monitor.stop_event.set()
                monitor.events["codex"].set()
                thread.join(timeout=2)

    def test_health_html_has_live_age_and_error_for_retained_values(self):
        html = self.b.read_text(os.path.join(HERE, "index.html"))
        self.assertIn('data-health-updated', html)
        self.assertIn('秒前更新', html)
        self.assertIn('查询失败（', html)
        self.assertIn('healthAge(q)', html)
        self.assertIn('healthError(q)', html)
        self.assertIn('healthAge(b)', html)
        self.assertIn('healthError(b)', html)



class GLMBalanceTest(unittest.TestCase):
    """独立离线替身：不改 HOME、不读配置/凭据、不启动服务器或常驻进程。"""
    @classmethod
    def setUpClass(cls):
        from unittest.mock import Mock
        cls.old_path = sys.path[:]
        sys.path.insert(0, os.path.join(os.path.dirname(HERE), "bin"))
        from frclyo import config
        spec = importlib.util.spec_from_file_location("board_glm_test", os.path.join(HERE, "board.py"))
        cls.b = importlib.util.module_from_spec(spec)
        with patch.object(config, "load", return_value=json.loads(json.dumps(config.DEFAULTS))), \
                patch.object(config, "path_of", side_effect=lambda name: os.path.join(HERE, "unused-" + name)):
            spec.loader.exec_module(cls.b)

    @classmethod
    def tearDownClass(cls):
        sys.path[:] = cls.old_path

    def setUp(self):
        self.b.CONFIG = json.loads(json.dumps(self.b.config.DEFAULTS))
        self.b.CONFIG["providers"] = {k: v for k, v in self.b.CONFIG["providers"].items() if k in ("zai-coding-plan", "zhipuai")}
        self.b.HEALTH["余额"] = []
        self.payload = {"code": 200, "success": True, "data": {"limits": [
            {"type": "TOKENS_LIMIT", "percentage": 25, "nextResetTime": 1800000000000},
            {"type": "TIME_LIMIT", "percentage": 10, "currentValue": 10, "usage": 100}]}}

    def query(self, payload=None, failure=None):
        from unittest.mock import Mock
        opener = Mock()
        opener.open.side_effect = failure or (lambda *a, **kw: io.BytesIO(json.dumps(payload).encode()))
        with patch.object(self.b.keys, "get_key", return_value="test-placeholder") as key, \
                patch.object(self.b.keys, "key_source", side_effect=AssertionError("unexpected credential lookup")), \
                patch.object(self.b.urllib.request, "build_opener", return_value=opener):
            self.b.refresh_health("balances")
        key.assert_called_once_with("zai-coding-plan")
        req = opener.open.call_args.args[0]
        self.assertEqual(req.full_url, "https://api.z.ai/api/monitor/usage/quota/limit")
        self.assertEqual(req.get_header("Authorization"), "test-placeholder")
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 10)
        self.assertNotIn("test-placeholder", json.dumps(self.b.HEALTH))
        return self.b.HEALTH["余额"]

    def test_normal_and_legacy_empty_defaults(self):
        for empty in (False, True):
            if empty:
                for settings in self.b.CONFIG["providers"].values():
                    settings["balance_api"] = ""
            rows = self.query(self.payload)
            self.assertEqual(rows[0]["额度"][0], {"名字": "编程额度", "已用": 25, "限额": 100, "单位": "%", "重置": 1800000000})
            self.assertEqual(rows[0]["额度"][1]["单位"], "次")
            self.assertIn("未查到公开余额查询接口", rows[1]["说明"])

    def test_bare_payload_zero_credit_and_missing_reset(self):
        rows = self.query({"limits": [{"type": "CREDIT_LIMIT", "percentage": 0}]})
        self.assertEqual(rows[0]["额度"][0]["已用"], 0)
        self.assertNotIn("重置", rows[0]["额度"][0])

    def test_quota_failure_messages(self):
        cases = [
            ({"success": False, "code": 500, "msg": "当前用户不存在 coding\tplan"}, "编程套餐已到期或未开通"),
            ({"success": True, "code": 500, "msg": "服务暂时不可用"}, "额度接口返回失败：服务暂时不可用"),
            ({"success": False, "code": 500}, "额度接口返回失败"),
            ({"success": False, "code": 500, "msg": "坏\n消息" + "x" * 100}, "额度接口返回失败：坏消息" + "x" * 57),
        ]
        for payload, expected in cases:
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(self.b.HealthQueryError, "^" + expected + "$"):
                    self.b._zai_quota(payload)

    def test_invalid_payloads_never_become_zero(self):
        invalid = [None, [], {}, {"code": 401, "data": self.payload["data"]},
                   {"success": False, "data": self.payload["data"]},
                   {"limits": []}, {"limits": [None]}, {"limits": [{"type": "unknown"}]}]
        for value in (None, True, "20", -1, 101, float("nan"), float("inf")):
            invalid.append({"limits": [{"type": "TOKENS_LIMIT", "percentage": value}]})
        for field, value in (("nextResetTime", "bad"), ("nextResetTime", -1), ("nextResetTime", 1e30)):
            invalid.append({"limits": [{"type": "TOKENS_LIMIT", "percentage": 10, field: value}]})
        invalid.append({"limits": [{"type": "TIME_LIMIT", "percentage": 10, "currentValue": 10}]})
        for payload in invalid:
            with self.subTest(payload=repr(payload)):
                self.b.HEALTH["余额"] = []
                rows = self.query(payload)
                self.assertIn("错误", rows[0])
                self.assertNotIn("额度", rows[0])
                self.assertIn("说明", rows[1])

    def test_failure_retains_timestamp_and_recovers(self):
        old = self.query(self.payload)[0]
        for failure in (TimeoutError("test-placeholder"), ValueError("test-placeholder"),
                        urllib.error.HTTPError("https://fake", 403, "test-placeholder", {}, None)):
            row = self.query(failure=failure)[0]
            self.assertEqual(row["更新时间"], old["更新时间"])
            self.assertEqual(row["额度"], old["额度"])
            self.assertIn("错误", row)
        self.assertNotIn("错误", self.query(self.payload)[0])

    def test_missing_key_is_visible(self):
        with patch.object(self.b.keys, "get_key", return_value=None), patch.object(self.b, "_get_json") as http:
            rows = self.b.check_balances()
        http.assert_not_called()
        self.assertEqual(rows[0]["错误"], "未配置 API key")
        self.assertIn("bigmodel.cn", rows[1]["说明"])

    def test_existing_balance_regressions(self):
        for name in ("test_balances_success_uses_keys_hosts_and_timeout",
                     "test_provider_without_balance_api_only_checks_presence",
                     "test_balances_failures_preserve_each_provider_and_recover",
                     "test_missing_keys_are_visible_without_erasing_balance",
                     "test_health_html_has_live_age_and_error_for_retained_values"):
            with self.subTest(name=name):
                self.b.CONFIG = json.loads(json.dumps(self.b.config.DEFAULTS))
                self.b.HEALTH["余额"] = []
                getattr(BoardTest, name)(self)
                self.doCleanups()

    def test_render_actual_health_javascript(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("缺少 Node，跳过实际前端渲染验证")
        with open(os.path.join(HERE, "index.html"), encoding="utf-8") as stream:
            html = stream.read()
        start = html.index("function healthAge(")
        end = html.index("\n}", html.index("function renderHealth(", start)) + 2
        js = html[start:end]
        rows = self.query(self.payload)
        states = [{"余额": rows}, {"余额": self.query(failure=TimeoutError())},
                  {"余额": [{"名字": "GLM 编程套餐", "错误": "无效响应"}]}]
        script = """
const assert = require('assert');
let rendered = '';
const nowS = () => 1800000001;
const esc = x => String(x).replace(/</g, '&lt;');
const put = (id, value) => { rendered = value; };
""" + js + "\nconst states = " + json.dumps(states, ensure_ascii=False) + ";" + """
renderHealth(states[0]);
assert(rendered.includes('已用 25% / 100%'));
assert(rendered.includes('已用 10次 / 100次'));
assert(rendered.includes('重置时间未提供'));
assert(rendered.includes('bigmodel.cn'));
assert(!rendered.includes('Invalid Date'));
renderHealth(states[1]);
assert(rendered.includes('查询失败'));
assert(rendered.includes('已用 25% / 100%'));
renderHealth(states[2]);
assert(rendered.includes('查询失败'));
assert(!rendered.includes('已用 0'));
console.log('health render assertions OK');
"""
        result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("health render assertions OK", result.stdout)


"""修好后：项目乙响应已展示，项目甲的迟到响应必须被丢弃。"""
import unittest
from regression_support import run_js
class B52Regression(unittest.TestCase):
    def test_project_selection_survives_stale_poll(self):
        r=run_js('B52',r'''
S.project='甲'; const oldPoll=poll();
S.project='乙'; S.data=null; const newPoll=poll0();
reply(1,state('乙')); await newPoll;
assert.equal(S.data.项目,'乙');
reply(0,state('甲')); await oldPoll;
console.log(JSON.stringify({selected:S.project,displayed:S.data.项目}));
assert.equal(S.data.项目,S.project,'迟到的旧项目响应覆盖了当前项目数据');
''')
        self.assertEqual(r.returncode,0,'前端回归失败，见 Node 输出')


"""修好后：切换轮次/任务后，旧文件请求不得写入当前抽屉。"""
import unittest
from regression_support import run_js
class B53Regression(unittest.TestCase):
    def test_old_round_file_cannot_enter_new_round(self):
        r=run_js('B53',r'''
S.open={id:'TASK',round:'r1',tab:'files'}; S.detail=detail('TASK','r1');
const old=openFile('结果.txt');
// 等价于点击 r2：切轮次并清空文件视图。
S.open.round='r2'; S.fileView=null; S.detail=detail('TASK','r2'); renderDrawer();
reply(0,{内容:'r1 的旧结果',错误:null}); await old;
console.log(JSON.stringify({selectedRound:S.open.round,fileView:S.fileView,body:elements.db.innerHTML}));
assert.equal(S.fileView,null,'r1 文件响应被展示在 r2 文件页');
''')
        self.assertEqual(r.returncode,0,'前端回归失败，见 Node 输出')


"""修好后：/api/task 的临时 HTTP 500 应保留重试计划，恢复后自动更新。"""
import unittest
from regression_support import run_js
class B54Regression(unittest.TestCase):
    def test_http_error_schedules_retry(self):
        r=run_js('B54',r'''
S.open={id:'TASK',round:'r1',tab:'progress'};
const first=loadDetail(); reply(0,{错误:'临时读取失败'},500); await first;
const retries=[...timers.values()].filter(t=>t.fn===loadDetail);
console.log(JSON.stringify({header:elements.dh.innerHTML,retryTimers:retries.length}));
assert.equal(retries.length,1,'HTTP 错误提前 return，抽屉自动刷新永久停止');
const recovered=retries[0].fn(); reply(1,detail('TASK')); await recovered;
assert.equal(S.detail.任务,'TASK');
''')
        self.assertEqual(r.returncode,0,'前端回归失败，见 Node 输出')


"""修好后：准备失败有稳定结束时间，超过提醒阈值后进入待处理。"""
import contextlib
import io
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch
from regression_support import board_fixture, card, http as fixture_http, run_js
from frclyo import execution
class B55Regression(unittest.TestCase):
    def test_prepare_failure_is_not_timeless(self):
        with board_fixture() as (b, base):
            task=card(b)
            run=Path(b.DELIVER)/task/'r1'; run.mkdir(parents=True)
            (run/'任务.txt').write_text('# 工人守则\n\n---\n\n项目：测试项目\n# 输入复制失败\n')
            output=io.StringIO()
            # 使用真正的回执写入路径；用素材条目超限替身避免操作任何真实素材。
            with patch.dict(execution.os.environ,WORKER_INPUT_DIR=str(base/'fake-input')), patch.object(execution,'copy_inputs',side_effect=ValueError('素材文件/目录数量超过上限')), contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                rc=execution.prepare_input_cli([str(run),'codex',task,'r1','fake-model','medium','0'*64])
            self.assertEqual(rc,2)
            receipt=json.loads((run/'回执.json').read_text())
            self.assertEqual(receipt['状态'],'准备失败')
            future=time.time()+1200
            with patch.object(b,'now',return_value=future):
                status, body=fixture_http(b,'/api/state'); state=json.loads(body)
                status2, body=fixture_http(b,'/api/task?id='+task); detail=json.loads(body)
            self.assertEqual((status,status2),(200,200))
            row=detail['轮次'][0]
            self.assertIsNotNone(row['结束'])
            self.assertAlmostEqual(row['结束'], (run/'回执.json').stat().st_mtime)
            with patch.object(b, 'now', return_value=future + 3600):
                later = b.load_round(task, 'r1', str(run))
            self.assertEqual(later['结束'], row['结束'])
            print(json.dumps({'回执状态':receipt['状态'],'详情结束':row['结束'],'列表用时':state['最近完成'][0]['用时秒'],'20分钟后待处理':state['要你处理']},ensure_ascii=False))
            self.assertTrue(any(x.get('任务')==task for x in state['要你处理']), '准备失败 20 分钟后仍无失败提醒；结束为 None 使延迟永远是 0')


"""修好后：表现记录的 null 验收保持未知，不等同于 false。"""
import json
from pathlib import Path
import time
import unittest
from regression_support import board_fixture, card, http as fixture_http, write_json
from frclyo import perf
class B56Regression(unittest.TestCase):
    def test_unknown_perf_does_not_mean_rejected(self):
        with board_fixture() as (b, base):
            task=card(b)
            run=Path(b.DELIVER)/task/'r1'; run.mkdir(parents=True)
            iso=b.ledger.utc
            write_json(run/'回执.json',{'工人':'codex','状态':'执行完成','退出码':0,'开始':iso(time.time()-7300),'结束':iso(time.time()-7200)})
            (run/'结果.txt').write_text('状态：完成\n')
            # 真正的 v1.2 写入器：缺少通过验收字段时合法地补 null。
            perf.append({'任务号':task,'轮次':'r1','工人':'codex'})
            saved=json.loads(Path(b.PERF).read_text()); self.assertIsNone(saved['通过验收'])
            status,body=fixture_http(b,'/api/state'); self.assertEqual(status,200); state=json.loads(body)
            status,body=fixture_http(b,'/api/task?id='+task); self.assertEqual(status,200); detail=json.loads(body)
            print(json.dumps({'表现记录验收':saved['通过验收'],'列表结论':state['最近完成'][0]['结论'],'详情验收':detail['轮次'][0]['验收'],'待处理':state['要你处理']},ensure_ascii=False))
            self.assertEqual(state['最近完成'][0]['结论'],'待验收','null 被误判为未通过，且抑制未验收提醒')
            self.assertIsNone(detail['轮次'][0]['验收'])
            self.assertTrue(any(x.get('任务') == task and '还没验收' in x.get('事情', '') for x in state['要你处理']))


class RequestOrderingRegression(unittest.TestCase):
    def test_stale_errors_files_and_retry_recovery(self):
        result = run_js('请求顺序与失败恢复', r'''
S.project='甲'; const first=poll0();
S.project='乙'; const second=poll0();
reply(1,state('乙')); await second;
requests[0].reject(new Error('旧请求断线')); await first;
assert.equal(S.failing,false); assert.equal(S.data.项目,'乙');
// 同一项目的两次请求也必须以最新发起的为准。
const older=poll0(), newer=poll0();
reply(3,{...state('乙'),标记:'new'}); await newer;
reply(2,{...state('乙'),标记:'old'}); await older;
assert.equal(S.data.标记,'new');
S.open={id:'TASK',round:'r1',tab:'files'}; S.detail=detail('TASK');
const file1=openFile('a'), file2=openFile('b');
reply(5,{内容:'new'}); await file2;
requests[4].reject(new Error('旧文件失败')); await file1;
assert.equal(S.fileView.path,'b'); assert.equal(S.fileView.text,'new');
const file3=openFile('c'); S.open.round='r2'; S.fileView=null;
requests[6].reject(new Error('旧轮次失败')); await file3;
assert.equal(S.fileView,null);
S.open={id:'TASK',round:'r1',tab:'result'}; S.detail=detail('TASK');
S.detail.轮次[0].交付说明='保留内容'; renderDrawer();
assert(elements.db.innerHTML.includes('未验收'));
const failed=loadDetail(); reply(7,{错误:'临时错误'},503); await failed;
assert(elements.db.innerHTML.includes('保留内容'));
assert(elements.db.innerHTML.includes('刷新失败，稍后重试'));
assert.equal([...timers.values()].filter(t=>t.fn===loadDetail && t.ms===5000).length,1);
const recovery=loadDetail(); reply(8,detail('TASK')); await recovery;
assert(!elements.db.innerHTML.includes('刷新失败，稍后重试'));
assert.equal([...timers.values()].filter(t=>t.fn===loadDetail && t.ms===15000).length,1);
''')
        self.assertEqual(result.returncode, 0)

    def test_prepare_failure_prefers_explicit_end(self):
        with board_fixture() as (b, base):
            run = base/'round'; run.mkdir()
            write_json(run/'回执.json', {'状态':'准备失败', '退出码':2,
                                       '开始':iso(1000), '结束':iso(1020)})
            row = b.load_round('TASK', 'r1', str(run))
            self.assertEqual(row['结束'], 1020)
            self.assertEqual(row['结束'] - row['开始'], 20)


import statistics
from scale_support import board, generate, emit, measure, deep_size, Reads, handler_request


class RoundAndCacheRegression(unittest.TestCase):
    def test_b96_deep_event_keeps_http_and_following_events(self):
        """深层事件后面的费用、时间线都要保留；首页和详情返回 200 并记坏行位置。"""
        with board_fixture() as (b, base):
            task = 'TEST-EVENTS'
            run = Path(b.DELIVER) / task / 'r1'
            run.mkdir(parents=True)
            write_json(run / '回执.json', {'工人': 'opencode', '状态': '执行完成',
                                         '退出码': 0, '结束': b.ledger.utc()})
            (run / '任务.txt').write_text('项目：离线审查\n# 测试\n', encoding='utf-8')
            depth = sys.getrecursionlimit() + 20
            broken = '{"secret-sentinel":' + '[' * depth + '0' + ']' * depth + '}\n'
            events = run / 'events.jsonl'
            events.write_text(broken + '{"type":"step_finish","part":{"cost":0.5}}\n'
                              + '{"type":"text","part":{"text":"正常后续事件"}}\n', encoding='utf-8')
            stderr = io.StringIO()
            with patch.object(b.sys, 'stderr', stderr):
                status, body = fixture_http(b, '/api/state')
                self.assertEqual(status, 200, body)
                self.assertEqual(json.loads(body)['最近完成'][0]['费用'], 0.5)
                status, body = fixture_http(b, '/api/task?id=' + task)
                self.assertEqual(status, 200, body)
                row = json.loads(body)['轮次'][0]
                self.assertTrue(any(x['s'] == '正常后续事件' for x in row['时间线']))
            self.assertIn('%s:1（RecursionError）' % events, stderr.getvalue())
            self.assertNotIn('secret-sentinel', stderr.getvalue())
            # 两个缓存均已建立，重复读取不应再次解析、重复记坏行。
            with patch.object(b, 'log') as log:
                self.assertEqual(b.event_cost(str(events)), 0.5)
                self.assertTrue(b.parse_events(str(events), str(run))['条目'])
            log.assert_not_called()

    def test_b96_unexpected_event_exception_is_isolated(self):
        """单行解码出现非 ValueError 异常时，费用和时间线读取也继续处理下一行。"""
        with board_fixture() as (b, base):
            events = base / 'events.jsonl'
            events.write_text('broken\n{"type":"step_finish","part":{"cost":0.5}}\n', encoding='utf-8')
            loads = json.loads
            def decode(line):
                if line == 'broken':
                    raise RuntimeError('secret-sentinel')
                return loads(line)
            with patch.object(b.json, 'loads', side_effect=decode), patch.object(b, 'log') as log:
                self.assertEqual(b.event_cost(str(events)), 0.5)
                self.assertEqual(b.parse_events(str(events))['费用'], 0.5)
            self.assertEqual(log.call_count, 2)
            for call in log.call_args_list:
                self.assertEqual(call.args, ('跳过事件坏行：%s:1（RuntimeError）' % events,))

    def test_b96_deep_receipt_does_not_hide_other_rounds(self):
        """单文件回执也隔离深层 JSON 并记类别；其他轮次和 HTTP 接口仍可读取。"""
        with board_fixture() as (b, base):
            task = 'TEST-RECEIPT'
            for rnd in ('r1', 'r2'):
                run = Path(b.DELIVER) / task / rnd
                run.mkdir(parents=True)
                write_json(run / '回执.json', {'状态': '执行完成', '退出码': 0})
            broken = Path(b.DELIVER) / task / 'r1/回执.json'
            depth = sys.getrecursionlimit() + 20
            broken.write_text('[' * depth + '0' + ']' * depth, encoding='utf-8')
            with patch.object(b, 'log') as log:
                for path in ('/api/state', '/api/task?id=' + task):
                    status, body = fixture_http(b, path)
                    self.assertEqual(status, 200, body)
                rows = json.loads(body)['轮次']
                self.assertEqual(rows[-1]['状态'], 'done')
            self.assertIn(unittest.mock.call('跳过 JSON 文件：%s（RecursionError）' % broken), log.call_args_list)

    def test_b97_project_does_not_depend_on_selected_round_body(self):
        """只有首轮有项目且没有账本/外部卡时，首页与任意详情保持同一项目和负责对话。"""
        with board_fixture() as (b, base):
            task = 'TEST-PROJECT'
            for rnd in ('r1', 'r2', 'r3'):
                run = Path(b.DELIVER) / task / rnd
                run.mkdir(parents=True)
                write_json(run / '回执.json', {'状态': '执行完成', '退出码': 0, '结束': b.ledger.utc()})
                (run / '任务.txt').write_text('项目：项目甲\n# 首轮任务\n' if rnd == 'r1' else '# 继续复核\n', encoding='utf-8')
                (run / '结果.txt').write_text('状态：完成\n', encoding='utf-8')
            write_json(Path(b.config.path_of('config_dir')) / '项目表.json',
                       {'项目': {'项目甲': {'负责对话': '甲对话'}}})
            state = b.build_state()
            self.assertIn('项目甲', [p['名字'] for p in state['项目列表']])
            recent = next(row for row in state['最近完成'] if row['任务'] == task)
            self.assertEqual((recent['项目'], recent['负责对话']), ('项目甲', '甲对话'))
            for selected in (None, 'r1', 'r2', 'r3', 'missing'):
                with self.subTest(selected=selected), patch.object(b, 'read_text', wraps=b.read_text) as read:
                    detail = b.build_task(task, selected)
                    self.assertEqual((detail['项目'], detail['负责对话']), ('项目甲', '甲对话'))
                    expected = selected if selected in ('r1', 'r2', 'r3') else 'r3'
                    self.assertEqual([r['轮次'] for r in detail['轮次'] if '任务卡' in r], [expected])
                    result_rounds = {Path(c.args[0]).parent.name for c in read.call_args_list
                                     if Path(c.args[0]).name == '结果.txt'}
                    self.assertLessEqual(result_rounds, {expected, 'r3'})

    def test_b97_project_source_priority_and_numeric_rounds(self):
        """账本优先；否则按数字轮序选任务卡，同轮独立卡优先，且修改后立即重读。"""
        with board_fixture() as (b, base):
            task = 'CL-20991231-99'
            for rnd in ('r9', 'r10'):
                run = Path(b.DELIVER) / task / rnd
                run.mkdir(parents=True)
                write_json(run / '回执.json', {'状态': '执行完成', '退出码': 0})
                (run / '任务.txt').write_text('项目：交付' + rnd, encoding='utf-8')
                (Path(b.RUNS) / (task + '-' + rnd + '.txt')).write_text('项目：任务卡' + rnd, encoding='utf-8')
            def check(expected):
                self.assertEqual(b.build_tasks()[0][task]['项目'], expected)
                for selected in (None, 'r9', 'r10'):
                    self.assertEqual(b.build_task(task, selected)['项目'], expected)
            check('任务卡r10')
            latest = Path(b.RUNS) / (task + '-r10.txt')
            latest.write_text('项目：修改后', encoding='utf-8')
            check('修改后')
            latest.unlink()
            check('交付r10')
            card(b, task)
            check('测试项目')
            Path(b.LEDGER).unlink()
            # 交付轮次不可用时，独立任务卡回退仍按 r10 > r9 排序。
            latest.write_text('项目：任务卡r10', encoding='utf-8')
            with patch.object(b, 'scan_rounds', return_value=([], None)):
                self.assertEqual(b.runs_card_project(task), '任务卡r10')

    def test_b65_numeric_latest_matches_audit(self):
        """同秒和时钟回拨都以 r10 为最新；账本结论不能覆盖失败的 r9。"""
        import hashlib
        for start in ('2099-12-31T00:00:00Z', '2099-12-30T00:00:00Z'):
            with self.subTest(start=start), board_fixture() as (b, base):
                task = card(b)
                with Path(b.LEDGER).open('a') as stream:
                    stream.write(json.dumps(b.ledger.event('完成', task, 结论='通过', 摘要='已核对')) + '\n')
                for rnd in ('r9', 'r10'):
                    run = Path(b.DELIVER) / task / rnd
                    run.mkdir(parents=True)
                    result = b'completed\n'
                    (run / '结果.txt').write_bytes(result)
                    write_json(run / '回执.json', {
                        '任务编号': task, '轮次编号': rnd, '工人': 'codex',
                        '状态': '失败' if rnd == 'r9' else '执行完成',
                        '退出码': 1 if rnd == 'r9' else 0,
                        '开始': '2099-12-31T00:00:00Z' if rnd == 'r9' else start,
                        '结束': '2099-12-31T00:00:00Z',
                        '结果sha256': hashlib.sha256(result).hexdigest()})
                write_json(b.PERF, {'任务号': task, '轮次': 'r9', '通过验收': False})
                tasks, errors = b.build_tasks()
                rows = tasks[task]['轮次']
                lines, bad = b.audit.audit(b.ledger.Paths())
                self.assertFalse(errors)
                self.assertEqual(bad, 0, lines)
                self.assertEqual([r['轮次'] for r in rows], ['r9', 'r10'])
                self.assertEqual({r['轮次']: r['验收'] for r in rows}, {'r9': '未通过', 'r10': '通过'})
                self.assertEqual(b.build_task(task)['当前轮次'], 'r10')
                self.assertEqual(b.build_state()['最近完成'][0]['轮次'], 'r10')

    def test_b71_cache_signature_and_safety(self):
        """缓存识别追加、截短和同尺寸替换；命中后仍拒绝叶子/祖先链接和特殊文件。"""
        with board_fixture() as (b, base):
            run = base / 'round'; run.mkdir()
            path = run / 'events.jsonl'
            def content(cost):
                return json.dumps({'type': 'step_finish', 'part': {'cost': cost}}).encode() + b'\n'
            path.write_bytes(content(1))
            self.assertEqual(b.event_cost(str(path)), 1)
            with patch.object(b, 'safe_read', wraps=b.safe_read) as read:
                self.assertEqual(b.event_cost(str(path)), 1)
                self.assertFalse(read.called)
            with path.open('ab') as stream:
                stream.write(content(2))
            self.assertEqual(b.event_cost(str(path)), 3)
            path.write_bytes(content(4))
            self.assertEqual(b.event_cost(str(path)), 4)
            old = path.stat()
            replacement = run / 'replacement'
            replacement.write_bytes(content(5))
            os.utime(replacement, ns=(old.st_atime_ns, old.st_mtime_ns))
            replacement.replace(path)
            self.assertEqual(b.event_cost(str(path)), 5)
            # 时间线缓存也复用签名，并有独立的数量上限。
            for i in range(b.EVENT_CACHE_LIMIT + 2):
                timeline = run / ('timeline-%d' % i)
                timeline.write_text('{"type":"thread.started"}\n')
                self.assertTrue(b.parse_events(str(timeline))['条目'])
            self.assertEqual(len(b._events_cache), b.EVENT_CACHE_LIMIT)
            path.rename(replacement)
            path.symlink_to(replacement)
            self.assertIsNone(b.event_cost(str(path)))
            self.assertFalse(b.parse_events(str(path))['条目'])
            path.unlink()
            replacement.rename(path)
            self.assertEqual(b.event_cost(str(path)), 5)
            moved = base / 'moved'
            run.rename(moved); run.symlink_to(moved, target_is_directory=True)
            self.assertIsNone(b.event_cost(str(path)))
            run.unlink(); moved.rename(run)
            path.unlink(); os.mkfifo(path)
            self.assertIsNone(b.event_cost(str(path)))
            path.unlink(); path.write_bytes(content(6))
            with patch.object(b, 'LOG_LIMIT', 1):
                self.assertIsNone(b.event_cost(str(path)))
            self.assertEqual(b.event_cost(str(path)), 6)
            path.unlink()
            self.assertIsNone(b.event_cost(str(path)))

    def test_b73_fallback_partial_and_replaced_receipts(self):
        """旧回执依赖运行信息；半写文件恢复、新 inode 及链接替换不能漏检或沿用旧工人。"""
        with board_fixture() as (b, base):
            run = Path(b.DELIVER) / 'TASK' / 'r1'; run.mkdir(parents=True)
            rec, info = run / '回执.json', run / '运行信息.json'
            write_json(rec, {'状态': '执行完成'})
            write_json(info, {'工人': 'codex'})
            monitor = b.HealthMonitor()
            with patch.object(b, 'read_json', wraps=b.read_json) as read:
                monitor.poll_receipts()
                self.assertFalse(read.called)
            write_json(info, {'工人': 'opencode'})
            monitor.poll_receipts()
            self.assertTrue(monitor.events['balances'].is_set())
            monitor.events['balances'].clear()
            old = rec.stat()
            replacement = run / 'new'
            replacement.write_bytes(rec.read_bytes())
            os.utime(replacement, ns=(old.st_atime_ns, old.st_mtime_ns))
            replacement.replace(rec)
            monitor.poll_receipts()
            self.assertTrue(monitor.events['balances'].is_set())
            monitor.events['balances'].clear()
            rec.write_text('{"工人":')
            monitor.poll_receipts()
            self.assertFalse(monitor.seen)
            self.assertFalse(monitor.events['codex'].is_set())
            write_json(rec, {'工人': 'codex'})
            monitor.poll_receipts()
            self.assertTrue(monitor.events['codex'].is_set())
            rec.rename(replacement); rec.symlink_to(replacement)
            monitor.poll_receipts()
            self.assertFalse(monitor.seen)
            rec.unlink(); replacement.rename(rec)
            monitor.poll_receipts()
            self.assertTrue(monitor.seen)
            rec.unlink()
            monitor.poll_receipts()
            self.assertFalse(monitor.seen)

    def test_b72_frontend_loads_selected_round_and_ignores_stale_responses(self):
        """实际前端脚本：摘要先显示加载态，乱序成功/失败、关闭重开都不能串轮。"""
        result = run_js('B72 按需切轮', r'''
const summary = {轮次:'r1',状态:'done',开始:1,结束:2,用时秒:1};
S.open={id:'TASK',round:'r2',tab:'progress'};
S.detail={...detail('TASK','r2'),轮次:[summary,...detail('TASK','r2').轮次]};
S.open.round='r1'; renderDrawer();
assert(elements.db.innerHTML.includes('正在加载这一轮'));
const older=loadDetail();
assert(requests[0].url.includes('&round=r1'));
S.open.round='r2'; const newer=loadDetail();
assert(requests[1].url.includes('&round=r2'));
reply(1,detail('TASK','r2')); await newer;
reply(0,detail('TASK','r1')); await older;
assert.equal(S.open.round,'r2'); assert.equal(S.detail.当前轮次,'r2');
const failedOld=loadDetail(); S.open.round='r1'; const latest=loadDetail();
reply(3,detail('TASK','r1')); await latest;
requests[2].reject(new Error('旧轮次失败')); await failedOld;
assert.equal(S.open.round,'r1'); assert(!elements.db.innerHTML.includes('刷新失败'));
const closing=loadDetail(); closeTask(); openTask('TASK','r1','progress');
reply(5,{...detail('TASK','r1'),标题:'重新打开'}); await Promise.resolve(); await Promise.resolve();
reply(4,{...detail('TASK','r1'),标题:'已经关闭'}); await closing;
assert.equal(S.detail.标题,'重新打开');
assert.equal([...timers.values()].filter(t=>t.fn===loadDetail).length,1);
''')
        self.assertEqual(result.returncode, 0)

    def test_b72_historical_selection_keeps_current_task_verdict(self):
        """查看旧轮也不能把最新轮的“等输入”误显示成“待验收”。"""
        with board_fixture() as (b, base):
            task = card(b)
            for rnd in ('r9', 'r10'):
                run = Path(b.DELIVER) / task / rnd; run.mkdir(parents=True)
                write_json(run / '回执.json', {'工人': 'codex', '状态': '执行完成', '退出码': 0})
                (run / '结果.txt').write_text('状态：需要输入（缺材料）' if rnd == 'r10' else '旧轮结果')
            current = b.build_task(task)
            historical = b.build_task(task, 'r9')
            self.assertEqual(current['结论'], '等输入')
            self.assertEqual(historical['结论'], current['结论'])
            self.assertNotIn('交付说明', historical['轮次'][-1])


class BoardScaleRegression(unittest.TestCase):
    """B71～B73：保留原复现的数据规模和 8 MiB / 2 MiB / 10 次阈值。"""
    @classmethod
    def setUpClass(cls):
        cls.scale_tmp = tempfile.TemporaryDirectory(prefix='board-scale-')
        cls.scale_data = Path(cls.scale_tmp.name).resolve() / 'data'
        generate(cls.scale_data)

    @classmethod
    def tearDownClass(cls):
        cls.scale_tmp.cleanup()

    def test_warm_poll_history_budget(self):
        """B71：静态暖轮询最多 8 MiB 日志，冷首页只缓存费用，追加后金额刷新。"""
        with board(Path(HERE).parent, self.scale_data) as b:
            state, cold = measure(b, 'state_cold', lambda: b.cached_state(None))
            cold['timeline_cache_bytes'] = deep_size(b._events_cache)
            cold['cost_cache_bytes'] = deep_size(b._cost_cache)
            emit(cold)
            self.assertEqual(len(state['最近完成']), 40)
            self.assertFalse(b._events_cache, '冷首页不应构造未显示的历史时间线')
            self.assertLess(deep_size(b._cost_cache), 1024 ** 2)
            baseline = [x['费用'] for x in state['最近完成']]
            samples = []
            for i in range(3):
                # 只让二秒状态缓存过期，保留日志解析缓存；模拟三秒轮询。
                b._state_cache.clear()
                state, row = measure(b, 'state_warm_%d' % i, lambda: b.cached_state(None))
                samples.append(row)
                self.assertEqual([x['费用'] for x in state['最近完成']], baseline)
                emit(row)
            log_bytes = max(x['reads'].get('events.jsonl', {}).get('bytes', 0) for x in samples)
            # 正确性对照：费用来源真的变化时，摘要仍必须更新。
            path = Path(b.DELIVER) / 'CL-20991231-01/r150/events.jsonl'
            original = path.read_bytes()
            previous_cost = next(x['费用'] for x in state['最近完成'] if x['任务'] == 'CL-20991231-01')
            try:
                with path.open('ab') as f:
                    f.write(b'{"type":"step_finish","part":{"cost":0.5}}\n')
                b._state_cache.clear()
                changed = b.cached_state(None)
                changed_cost = next(x['费用'] for x in changed['最近完成'] if x['任务'] == 'CL-20991231-01')
                self.assertAlmostEqual(changed_cost, previous_cost + 0.5)
            finally:
                path.write_bytes(original)
            emit({'median_seconds': statistics.median(x['seconds'] for x in samples),
                  'history_log_bytes': log_bytes, 'budget_bytes': 8 * 1024 ** 2,
                  'changed_log_cost_refreshed': True})
            self.assertLessEqual(log_bytes, 8 * 1024 ** 2,
                                 '静态首页暖轮询仍重读全部历史日志；应复用安全验证后的费用摘要')



    def test_first_detail_payload_budget(self):
        """B72：150 轮首屏最多 2 MiB，旧轮按请求返回，未选轮仅有摘要。"""
        with board(Path(HERE).parent, self.scale_data) as b:
            for label, task in [('one_round_control', 'CL-20991231-02'),
                                ('150_round_detail', 'CL-20991231-01')]:
                response, row = measure(b, label, lambda: handler_request(b, '/api/task?id=' + task))
                row.update(response)
                emit(row)
                self.assertEqual(response['status'], 200)
            # 真实 Handler 的 JSON 必须保留 150 个入口，但仅一轮有正文。
            for selected in ('', 'r9', 'r10', 'r150', 'missing'):
                with Reads(b) as reads:
                    status, body = fixture_http(b, '/api/task?id=CL-20991231-01&round=' + selected)
                self.assertEqual(status, 200)
                detail = json.loads(body)
                expected = selected if selected in ('r9', 'r10', 'r150') else 'r150'
                self.assertEqual(detail['当前轮次'], expected)
                self.assertEqual(len(detail['轮次']), 150)
                full = [r for r in detail['轮次'] if '时间线' in r]
                self.assertEqual([r['轮次'] for r in full], [expected])
                self.assertTrue(full[0]['任务卡'])
                self.assertTrue(full[0]['交付说明'])
                self.assertTrue(full[0]['文件'])
                self.assertTrue(full[0]['时间线'])
                # 默认只读最新正文；旧轮详情另需最新结果判断任务总状态，最多两轮。
                self.assertEqual(reads.counts['结果.txt']['calls'], 1 if expected == 'r150' else 2)
                for r in detail['轮次']:
                    if r['轮次'] != expected:
                        self.assertEqual(set(r), {'轮次', '状态', '验收', '开始', '结束', '用时秒'})
            emit({'response_bytes': response['response_bytes'], 'budget_bytes': 2 * 1024 ** 2})
            self.assertLessEqual(response['response_bytes'], 2 * 1024 ** 2,
                                 '一次详情包含全部历史轮次正文；应按当前轮懒加载，保留历史可访问性')



    def test_unchanged_receipt_read_budget(self):
        """B73：349 份静态回执每次扫描最多重读 10 份，变更仍唤醒正确通道。"""
        with board(Path(HERE).parent, self.scale_data) as b:
            monitor = b.HealthMonitor()
            self.assertEqual(len(monitor.seen), 349)
            samples = []
            for i in range(3):
                _, row = measure(b, 'unchanged_poll_%d' % i, monitor.poll_receipts)
                samples.append(row)
                emit(row)
            self.assertFalse(monitor.events['codex'].is_set())
            self.assertFalse(monitor.events['balances'].is_set())
            # 变更对照：临时数据中的一份有效回执变长，确保通知未被优化掉。
            path = Path(b.DELIVER) / 'CL-20991231-02/r1/回执.json'
            original = path.read_bytes()
            try:
                changed = json.loads(original)
                changed['测试备注'] = 'changed signature'
                path.write_text(json.dumps(changed, ensure_ascii=False), encoding='utf-8')
                monitor.poll_receipts()
                self.assertTrue(monitor.events['codex'].is_set())
            finally:
                path.write_bytes(original)
            count = max(x['reads'].get('回执.json', {}).get('calls', 0) for x in samples)
            emit({'median_seconds': statistics.median(x['seconds'] for x in samples),
                  'unchanged_receipt_reads': count, 'budget_reads': 10,
                  'changed_receipt_triggered': True})
            self.assertLessEqual(count, 10, '每秒重解析未变化的所有回执；应复用已验证签名对应的工人信息')




if __name__ == "__main__":
    unittest.main(verbosity=2)
