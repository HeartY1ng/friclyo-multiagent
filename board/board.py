#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Agent 看板服务（只读）

做什么：把散在各处的协作记录汇总成一个网页，打开就能看到
  ① 要他处理的事  ② 正在干的活  ③ 最近做完的活  ④ 额度、余额、安全校验
数据从哪来（全部只读，不改任何东西）：
  - 本地账本（主数据源）
  - 交付目录里每一轮的 运行信息.json / 回执.json / events.jsonl / 结果.txt
  - 配置指定的任务卡和表现记录
  - Codex app-server 的实时额度、DeepSeek / Kimi 的余额接口
安全：只监听 127.0.0.1；只接受 GET；只允许看交付目录和 runs/ 里的文件；显示前把像密钥的字符串打码。
兼容：macOS 自带的 Python 3.9，只用标准库。
"""
import json
from collections import OrderedDict
import stat
import os
import re
import math
import selectors
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------- 1. 直接使用仓库公共库，配置与凭据只经过统一接口 ----------
# 固定本进程的真实版本目录；发布切换链接不会混用新的模块和静态文件。
HERE = os.path.dirname(os.path.realpath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "bin"))
import frclyo.config as config
import frclyo.keys as keys
import frclyo.ledger as ledger
import frclyo.audit as audit
from frclyo.execution import directory, safe_read, RESULT_LIMIT, LOG_LIMIT, result_limit

CONFIG = config.load()
PORT = int(CONFIG["board_port"])
DELIVER = config.path_of("deliver_dir")
RUNS = config.path_of("cards_dir")
LEDGER = config.path_of("ledger_file")
PERF = config.path_of("perf_file")

BACKGROUND = os.environ.get("BOARD_NO_BACKGROUND") != "1"
CHECK_BALANCE = os.environ.get("BOARD_BALANCE", "1") == "1"
LOG_FILE = config.path_of("board_log_file")

RECENT_DAYS = 14          # "最近完成"和"要你处理"只看最近 14 天
LOW_BALANCE = 10.0        # 余额低于 10 元就提醒
STD_ID = ledger.TASK_ID   # 正式任务号（子任务带后缀，如 CL-20261013-02-A）；其他（GATE-、TEST-……）算测试

# ---------- 2. 小工具：时间、读文件、打码 ----------
def now():
    return time.time()


def parse_iso(s):
    """把 2026-10-13T09:18:10Z 这种时间转成秒数；转不了返回 None。"""
    if not s:
        return None
    try:
        return datetime.strptime(str(s), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except Exception:
        return None


def read_json(path):
    """读 JSON 文件；不存在或坏了返回 None（看板不因为一个坏文件整体挂掉）。"""
    try:
        return json.loads(safe_read(path, RESULT_LIMIT).decode("utf-8"))
    except FileNotFoundError:
        return None
    except Exception as exc:
        log("跳过 JSON 文件：%s（%s）" % (path, type(exc).__name__))
        return None


def read_projects():
    """项目表是可选信息；损坏或缺失不影响任务与账本展示。"""
    data = read_json(os.path.join(config.path_of("config_dir"), "项目表.json"))
    if not isinstance(data, dict) or not isinstance(data.get("项目"), dict):
        return {}
    result = {}
    for name, entry in data["项目"].items():
        if not isinstance(entry, dict):
            return {}
        owner = entry.get("负责对话")
        if not isinstance(owner, str) or not owner.strip():
            return {}
        result[name] = owner.strip()
    return result


def conversation_target(owner):
    return "发到「%s」对话" % owner if owner != "未登记" else "发到负责这个项目的对话"


def read_text(path, limit=300_000):
    """安全读取有界普通文本；链接、特殊文件、超限或读取失败均视为缺失。"""
    try:
        return safe_read(path, limit).decode("utf-8", errors="replace")
    except Exception:
        return None


def display_text(text, limit):
    """仅展示截断；状态和标签始终从限额内的完整文本解析。"""
    if text is None or len(text) <= limit:
        return text
    return text[:limit] + "\n…（显示已截断）"


# 像密钥的字符串：sk- 开头、GitHub token、JWT、40 位以上的长串。64 位小写十六进制是校验码，不打码。
SECRET = re.compile(r"(sk-[A-Za-z0-9_\-]{8,}|tp-[A-Za-z0-9_\-]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|eyJ[A-Za-z0-9_\-.]{20,}|[A-Za-z0-9]{40,})")


def redact(s):
    """显示前把像密钥的部分换成 ****。"""
    if s is None:
        return None
    s = s if isinstance(s, str) else str(s)
    return SECRET.sub(lambda m: m.group(0) if re.fullmatch(r"[0-9a-f]{64}", m.group(0)) else "****", s)


def redact_obj(o):
    """把一个 JSON 结构里所有字符串都打码。"""
    if isinstance(o, str):
        return redact(o)
    if isinstance(o, list):
        return [redact_obj(x) for x in o]
    if isinstance(o, dict):
        return {k: redact_obj(v) for k, v in o.items()}
    return o


def clip(s, n):
    """截断太长的文字。"""
    s = s or ""
    return s if len(s) <= n else s[:n] + "…"


def tail_lines(s, n=6, maxlen=800):
    """取输出的最后几行，用来在时间线上展示命令输出。"""
    lines = [x for x in (s or "").rstrip().splitlines()]
    out = "\n".join(lines[-n:])
    return out[-maxlen:]


# ---------- 3. 读取账本 ----------
# 账本适配成原看板的数据结构，前端布局和刷新接口都不用改变。
def read_ledger():
    """全部历史事件保留在抽屉中，账本结论优先于表现记录。"""
    cards = {}
    for key, c in ledger.fold(ledger.read_events(LEDGER)).items():
        state = {"开卡": "ready", "卡住": "blocked", "完成": "done", "放弃": "dropped"}[c["状态"]]
        cards[key] = {
            "数据源": "账本", "卡号": key, "标题": c["标题"], "项目": c["项目"],
            "派活人": c["派活人"], "派给": "",
            "来源": c["来源"], "父任务": c["父任务"], "卡片文件": c.get("卡片文件"),
            "状态": state, "创建": parse_iso(c["时间"]), "开始": None,
            "完成": parse_iso(c["更新"]) if c["状态"] in ledger.TERMINAL else None,
            "更新": max(parse_iso(e["时间"]) for e in c["事件"]),
            "卡住原因": c["原因"], "结论": c["结论"], "放弃原因": c["原因"] if state == "dropped" else "",
            "事件": [{"类型": e["动作"], "时间": parse_iso(e["时间"]), "内容": e}
                     for e in sorted(c["事件"], key=lambda e: e["时间"])],
            "留言": [], "执行记录": ([{"结果": "completed", "摘要": c["摘要"],
                "附加": {"验收": c["结论"]}, "结束": parse_iso(c["更新"])}] if state == "done" else []),
        }
    return cards


# ---------- 4. 先安全核对文件签名，再复用费用摘要或按需解析时间线 ----------
def file_signature(path, limit):
    """逐段拒绝链接，打开普通文件核对限额；命中缓存也不能绕过安全检查。"""
    path = os.path.abspath(path)
    with directory(os.path.dirname(path)) as parent:
        fd = os.open(os.path.basename(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=parent)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise ValueError("不是限额内的普通文件")
            # ctime 还可识别同大小改写后恢复 mtime 的文件。
            return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        finally:
            os.close(fd)


_events_cache = OrderedDict()
_cost_cache = OrderedDict()
_events_lock = threading.Lock()
EVENT_CACHE_LIMIT = 8


def _cached_events(path, rel_base, costs_only):
    cache = _cost_cache if costs_only else _events_cache
    empty = {"条目": [], "步数": 0, "费用": None}
    try:
        signature = file_signature(path, LOG_LIMIT)
    except Exception:
        with _events_lock:
            cache.pop(path, None)
        return empty
    key = (rel_base, signature)
    with _events_lock:
        hit = cache.get(path)
        if hit and hit[0] == key:
            cache.move_to_end(path)
            return hit[1]
    try:
        data = safe_read(path, LOG_LIMIT)
    except Exception:
        return empty
    items, steps, cost = [], 0, None
    for number, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
        try:
            e = json.loads(line)
            if not isinstance(e, dict):
                continue
            if not costs_only:
                for it in _event_items(e, rel_base):
                    items.append(it)
                    if it["k"] in ("tool", "edit"):
                        steps += 1
            if e.get("type") == "step_finish":
                c = (e.get("part") or {}).get("cost")
                if isinstance(c, (int, float)):
                    cost = (cost or 0) + c
        except Exception as exc:
            # 异常只属于这一行；日志不包含事件正文或异常原文。
            log("跳过事件坏行：%s:%d（%s）" % (path, number, type(exc).__name__))
            continue
    res = {"条目": items, "步数": steps, "费用": cost}
    try:
        if file_signature(path, LOG_LIMIT) != signature:
            return empty  # 写入中的快照不入缓存，下次重读。
    except Exception:
        return empty
    with _events_lock:
        cache[path] = (key, res)
        cache.move_to_end(path)
        if not costs_only:
            while len(cache) > EVENT_CACHE_LIMIT:
                cache.popitem(last=False)
    return res


def parse_events(path, rel_base=""):
    """把 Codex 或 OpenCode 的 events.jsonl 翻译成人能看懂的时间线。
    返回 {条目: [...], 步数: n, 费用: 美元或 None}。每个条目：{t 时间, k 种类, s 一句话, d 细节}"""
    return _cached_events(path, rel_base, False)


def event_cost(path):
    """首页只保留费用，不构造或长期保存未显示的历史时间线。"""
    return _cached_events(path, "", True)["费用"]


def _short_path(p, base):
    """把工人目录里的绝对路径缩短成相对路径，好读。"""
    p = str(p or "")
    if base and p.startswith(base):
        p = p[len(base):].lstrip("/") or "."
    return p


def _short_cmd(cmd):
    """Codex 的命令都包了一层 /bin/zsh -lc "…"，去掉外壳只留真正的命令。"""
    m = re.match(r'^/bin/(?:ba|z)?sh -l?c (["\'])(.*)\1$', cmd or "", re.S)
    return m.group(2) if m else (cmd or "")


def _event_items(e, base):
    """一条原始事件 → 0 到多条时间线条目。认不出来的事件直接跳过。"""
    t = e.get("type")
    ts = e.get("timestamp")
    ts = ts / 1000.0 if isinstance(ts, (int, float)) else None
    out = []

    def add(k, s, d=None):
        out.append({"t": ts, "k": k, "s": clip(redact(s), 300), "d": clip(redact(d), 4000) if d else None})

    # --- Codex 的格式 ---
    if t == "thread.started":
        add("start", "开始干活")
    elif t in ("item.started", "item.completed"):
        it = e.get("item") or {}
        typ = it.get("type")
        if typ == "command_execution":
            cmd = _short_cmd(it.get("command"))
            if t == "item.started":
                add("tool", "运行命令：" + clip(cmd.replace("\n", " "), 160), cmd if len(cmd) > 160 else None)
            else:
                code = it.get("exit_code")
                add("out" if code == 0 else "warn", "命令结束（退出码 %s）" % code, tail_lines(it.get("aggregated_output")))
        elif typ == "agent_message" and t == "item.completed":
            add("say", it.get("text") or "", it.get("text") if len(it.get("text") or "") > 300 else None)
        elif typ == "reasoning" and t == "item.completed":
            add("think", "思考：" + clip(it.get("text") or "", 200))
        elif typ == "file_change" and t == "item.completed":
            files = ["%s（%s）" % (_short_path(c.get("path"), base), {"add": "新建", "update": "修改", "delete": "删除"}.get(c.get("kind"), c.get("kind")))
                     for c in it.get("changes") or []]
            add("edit", "改文件：" + "、".join(files))
        elif typ == "error":
            add("error", "出错：" + (it.get("message") or ""))
        elif t == "item.completed" and typ:
            add("tool", "工具：" + typ)
    elif t == "turn.completed":
        u = e.get("usage") or {}
        add("end", "本回合结束 · 输入 %s · 输出 %s tokens" % (u.get("input_tokens", "?"), u.get("output_tokens", "?")))
    elif t in ("turn.failed", "error") and "error" not in (e.get("part") or {}):
        err = e.get("error")
        msg = err.get("message") if isinstance(err, dict) else (err or e.get("message"))
        if isinstance(err, dict) and isinstance(err.get("data"), dict):
            msg = err["data"].get("message") or msg
        add("error", "出错：" + str(msg or "未知错误"))
    # --- OpenCode 的格式 ---
    elif t == "text":
        txt = (e.get("part") or {}).get("text") or ""
        add("say", txt, txt if len(txt) > 300 else None)
    elif t == "reasoning":
        add("think", "思考：" + clip((e.get("part") or {}).get("text") or "", 200))
    elif t == "tool_use":
        p = e.get("part") or {}
        st = p.get("state") or {}
        inp = st.get("input") or {}
        tool = p.get("tool") or "工具"
        what = inp.get("command") or inp.get("filePath") or inp.get("pattern") or inp.get("path") or ""
        what = _short_path(what, base) if what.startswith("/") else what
        names = {"bash": "运行命令", "write": "写文件", "edit": "改文件", "read": "读文件",
                 "glob": "找文件", "grep": "搜内容", "list": "列目录", "todowrite": "记待办"}
        label = names.get(tool, "工具 " + tool)
        status = st.get("status")
        k = "edit" if tool in ("write", "edit") else "tool"
        if status == "error":
            k = "warn"
        add(k, "%s：%s" % (label, clip(str(what).replace("\n", " "), 160)), tail_lines(st.get("output") or st.get("error") or ""))
    return out


# ---------- 5. 扫交付目录：每个 任务号/轮次 文件夹就是一轮 ----------
def proc_alive(pid):
    """工人脚本还在不在跑：进程存在，而且命令行里有 xxx-worker.sh（防止进程号被别的程序复用）。"""
    try:
        pid = int(pid)
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    except Exception:
        return False
    try:
        cmd = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return True
    # 必须是"xxx-worker.sh"：光查 worker.sh 会被系统进程 mdworker.shared 冒充（它的名字里正好有这几个字）
    return re.search(r"[A-Za-z]+-worker\.sh", cmd) is not None


def strip_injected_rules(text):
    """执行脚本会把「工人守则」插到任务卡开头，中间用空行 + 分隔线隔开。
    如果是这种格式，只保留第一条分隔线之后的原任务卡；不是则原样返回。"""
    if text and text.startswith("# 工人守则") and "\n\n---\n\n" in text:
        return text.split("\n\n---\n\n", 1)[1]
    return text


def card_project(text):
    """任务卡里写的"项目：xxx"。"""
    text = strip_injected_rules(text)
    m = re.search(r"^\s*[#*\s]*项目\s*[:：]\s*\**\s*([^\n*|]+)", text or "", re.M)
    return m.group(1).strip() if m else ""


def card_title(text):
    """任务卡的标题：第一行 markdown 标题，没有就用第一行非空文字。"""
    text = strip_injected_rules(text)
    for line in (text or "").splitlines():
        s = line.strip()
        if not s:
            continue
        s = re.sub(r"^#+\s*", "", s)
        if re.match(r"^\**\s*项目\s*[:：]", s):   # "项目：xxx" 是标签，不是标题
            continue
        s = re.sub(r"^任务卡\s*[:：]\s*", "", s)
        return clip(s, 60)
    return ""


def needs_input(result_text):
    """提取任务卡协议中表示缺少信息的状态或结论。"""
    m = re.search(r"(?:状态|结论)\s*\**\s*[:：]\s*\**\s*需[要]输入[^\n]*", result_text or "")
    return m.group(0) if m else None


def disclosure(result_text):
    """从交付说明里取出"决策披露"那一节（到下一个标题为止）。"""
    t = result_text or ""
    i = t.find("决策披露")
    if i < 0:
        return None
    start = t.rfind("\n", 0, i) + 1
    rest = t[start:]
    lines = rest.splitlines()
    out = [lines[0]]
    for line in lines[1:]:
        if re.match(r"^\s*(#{1,6}\s|\**\s*(5[.、]|状态\s*[:：]|\*\*状态))", line):
            break
        out.append(line)
    # 第一行只是"## 4. 决策披露"这种标题的话去掉，抽屉里已经有标题了
    if re.fullmatch(r"[#*\s\d.、]*决策披露[*\s:：（(）)A-Za-z]*", out[0].strip()):
        out = out[1:]
    return clip("\n".join(out).strip(), 4000)


def norm_usage(u):
    """两家的用量字段不一样，统一输入、输出和缓存读写字段。"""
    if not isinstance(u, dict):
        return None
    if "input_tokens" in u:   # Codex
        out = {"输入": u.get("input_tokens") or 0, "输出": (u.get("output_tokens") or 0)}
    elif "input" in u:        # OpenCode（思考也算输出）
        out = {"输入": u.get("input") or 0, "输出": (u.get("output") or 0) + (u.get("reasoning") or 0)}
    elif "cached_input_tokens" in u or "cache_write_input_tokens" in u:
        out = {"输入": 0, "输出": 0}
    else:
        return None
    for field, label in (("cached_input_tokens", "缓存读取"), ("cache_write_input_tokens", "缓存写入")):
        if field in u:
            out[label] = u[field]
    return out


def load_round(task, rnd, rdir, include_body=True):
    """读一轮状态；只需机器摘要时不读取任务正文和结果。"""
    info = read_json(os.path.join(rdir, "运行信息.json")) or {}
    rec = read_json(os.path.join(rdir, "回执.json"))
    ev_path = os.path.join(rdir, "events.jsonl")
    card = (read_text(os.path.join(rdir, "任务.txt"), LOG_LIMIT) or "") if include_body else ""
    run_card = (read_text(os.path.join(RUNS, "%s-%s.txt" % (task, rnd)), LOG_LIMIT) or "") if include_body else ""
    try:
        ev_stat = os.stat(ev_path, follow_symlinks=False)
        ev_mtime = ev_stat.st_mtime if stat.S_ISREG(ev_stat.st_mode) else None
    except Exception:
        ev_mtime = None
    start = parse_iso(info.get("开始")) or parse_iso((rec or {}).get("开始"))
    if start is None:
        try:
            start = os.path.getmtime(rdir)
        except Exception:
            start = None
    end = parse_iso((rec or {}).get("结束"))
    # 准备失败没有事件日志；旧回执也可能没有结束字段。
    # 使用固定的回执修改时间，不能用每次刷新的当前时间。
    if end is None and (rec or {}).get("状态") == "准备失败":
        try:
            receipt_stat = os.stat(os.path.join(rdir, "回执.json"), follow_symlinks=False)
            if stat.S_ISREG(receipt_stat.st_mode):
                end = receipt_stat.st_mtime
        except OSError:
            pass
    timeout = info.get("超时秒")
    if (type(timeout) not in (int, float) or timeout <= 0
            or (isinstance(timeout, float) and not math.isfinite(timeout))):
        timeout = None

    # 状态：有回执 = 结束了；没回执就看工人脚本还在不在
    if rec:
        s = rec.get("状态") or ""
        state = {"执行完成": "done", "超时": "timeout", "失败": "failed"}.get(s)
        if not state:
            state = "done" if rec.get("退出码") == 0 else "failed"
    elif info:
        state = "running" if proc_alive(info.get("进程号")) else "interrupted"
    else:   # 加"运行信息"之前的老轮次：10 分钟内还在写事件就当在跑
        state = "running" if ev_mtime and now() - ev_mtime < 600 else "interrupted"
    if state != "running" and end is None:
        end = ev_mtime

    result = read_text(os.path.join(rdir, "结果.txt"), result_limit()) if include_body and state != "running" else None
    src = rec or info
    return {
        "任务": task, "轮次": rnd, "目录": rdir, "状态": state,
        "工人": src.get("工人") or "", "模型": src.get("模型") or "",
        "档位": src.get("思考档位") or src.get("变体") or info.get("档位") or "",
        "开始": start, "结束": end, "超时秒": timeout,
        "用量": norm_usage((rec or {}).get("用量")), "退出码": (rec or {}).get("退出码"),
        "标题": card_title(run_card) or card_title(card), "项目": card_project(run_card) or card_project(card),
        "需输入": needs_input(result), "有结果": result is not None,
        "_ev": ev_path, "_result": result,
    }


_probe = {"时间": 0, "错误": None}


def probe_deliver():
    """先用一个子进程试读交付目录（最多等 5 秒）。
    为什么：交付目录在「文稿」里，macOS 第一次会弹窗问要不要允许；弹窗没点之前，读文件的调用会一直卡住。
    用子进程试探，卡住了就放弃这一次，看板照常显示别的内容。结果缓存 30 秒。"""
    if now() - _probe["时间"] < 30:
        return _probe["错误"]
    err = None
    try:
        p = subprocess.Popen(["/bin/ls", DELIVER], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.PIPE, start_new_session=True)
        try:
            _, e = p.communicate(timeout=5)
            if p.returncode != 0:
                msg = (e or b"").decode("utf-8", "replace")
                if "Operation not permitted" in msg or "Permission denied" in msg:
                    err = "看板读不到交付目录（macOS 没给 python3 访问「文稿」的权限）：到 系统设置 → 隐私与安全性 → 文件和文件夹 里允许"
                elif "No such file" in msg:
                    err = "交付目录不存在：" + DELIVER
                else:
                    err = "读交付目录出错：" + clip(msg.strip(), 200)
        except subprocess.TimeoutExpired:
            p.kill()
            p.communicate()
            err = "正在等系统授权：屏幕上可能有「python3 想访问文稿文件夹」的弹窗，点「允许」后看板就能显示交付记录"
    except Exception as e:
        err = "读交付目录出错：%s" % type(e).__name__
    _probe.update({"时间": now(), "错误": err})
    return err


def round_order(name):
    """正式轮次按数字排序，和日审一致；兼容的旧目录排在正式轮次之前。"""
    return (1, int(name[1:]), name) if re.fullmatch(r"r[0-9]+", name) else (0, 0, name)


def scan_rounds(task_id=None, selected_round=None):
    """扫一遍交付目录，返回所有轮次。读不到目录时返回错误说明。"""
    rounds = []
    err = probe_deliver()
    if err:
        return [], err
    try:
        names = [task_id] if task_id is not None else sorted(os.listdir(DELIVER))
    except PermissionError:
        return [], "看板读不到交付目录（macOS 没给 python3 访问「文稿」的权限）：到 系统设置 → 隐私与安全性 → 文件和文件夹 里允许"
    except FileNotFoundError:
        return [], "交付目录不存在：" + DELIVER
    for t in names:
        tdir = os.path.join(DELIVER, t)
        if t.startswith(".") or os.path.islink(tdir) or not os.path.isdir(tdir):
            continue
        try:
            subs = sorted(os.listdir(tdir))
        except Exception:
            continue
        subs = [r for r in subs if not r.startswith(".")
                and not os.path.islink(os.path.join(tdir, r)) and os.path.isdir(os.path.join(tdir, r))]
        latest = max(subs, key=round_order, default=None)
        selected = selected_round if selected_round in subs else latest
        for r in subs:
            rdir = os.path.join(tdir, r)
            if r.startswith(".") or os.path.islink(rdir) or not os.path.isdir(rdir):
                continue
            try:
                # 切到旧轮时仍保留最新轮的标题/需输入状态，避免任务总状态跟着选择变化。
                rounds.append(load_round(t, r, rdir, include_body=task_id is None or r in (selected, latest)))
            except Exception as e:
                rounds.append({"任务": t, "轮次": r, "目录": rdir, "状态": "interrupted", "工人": "", "模型": "",
                               "档位": "", "开始": None, "结束": None, "超时秒": None, "用量": None, "退出码": None,
                               "标题": "", "项目": "", "需输入": None, "有结果": False,
                               "_ev": "", "_result": None, "_err": str(e)})
    return rounds, None


def read_perf():
    """表现记录：{(任务号, 轮次): 那一行}，里面有"通过验收"。"""
    out = {}
    try:
        with open(PERF, "rb") as f:
            for number, line in enumerate(f, 1):
                try:
                    d = json.loads(line.decode("utf-8"))
                    if not isinstance(d, dict):
                        raise ValueError("record is not an object")
                    out[(d.get("任务号"), d.get("轮次"))] = d
                except Exception as exc:
                    # 只记录位置和类别，不把记录正文或异常原文带进日志。
                    log("跳过表现记录坏行：%s:%d（%s）" % (PERF, number, type(exc).__name__))
    except FileNotFoundError:
        pass
    except OSError as exc:
        log("表现记录读取失败：%s（%s）" % (PERF, type(exc).__name__))

    return out


# ---------- 6. 实时健康条：独立查询线程、回执监测与成功值缓存 ----------
HEALTH = {"codex": None, "余额": [], "日审": None}
_health_lock = threading.Lock()
AUDIT_INTERVAL = 3600  # 日审启动时立即运行，此后每小时一次。
HEALTH_INTERVAL = 60
QUERY_TIMEOUT = 15


class HealthQueryError(Exception):
    """只放可公开的固定原因，不能把接口响应或凭据带到日志、网页。"""


def _query_error(exc):
    """保留可诊断的错误类别；绝不输出可能夹带 key 的响应体或异常原文。"""
    if isinstance(exc, HealthQueryError):
        return str(exc)
    if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)):
        return "查询超时"
    if isinstance(exc, FileNotFoundError):
        return "找不到 codex 命令"
    if isinstance(exc, urllib.error.HTTPError):
        return "HTTP %d" % exc.code
    return type(exc).__name__


def quota_window_name(minutes):
    if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or not math.isfinite(minutes) or minutes <= 0:
        return "额度（窗口未知）"
    if minutes == 10080:
        return "本周额度"
    if minutes % 60 == 0:
        return "%g 小时额度" % (minutes / 60)
    return "%g 分钟额度" % minutes


def check_codex_quota():
    """一次查询对应一个限时 stdio 子进程；握手后读取实时额度，所有出口都回收进程。"""
    env = dict(os.environ)
    # 空代理明确表示直连；非空代理仅传给本次查询，不改用户全局配置。
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env.pop(name, None)
    proxy = CONFIG["codex"].get("proxy") or ""
    if proxy:
        env.update(HTTP_PROXY=proxy, HTTPS_PROXY=proxy, http_proxy=proxy, https_proxy=proxy)
    deadline = time.monotonic() + QUERY_TIMEOUT
    p = subprocess.Popen(["codex", "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, env=env, start_new_session=True)
    try:
        # 非阻塞读取可防止半行输出卡死；通知与无关响应跳过，仍受同一个总超时约束。
        os.set_blocking(p.stdout.fileno(), False)
        pending = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(p.stdout, selectors.EVENT_READ)

            def send(message):
                p.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
                p.stdin.flush()

            def receive(request_id):
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError()
                    if b"\n" in pending:
                        line, _, rest = pending.partition(b"\n")
                        pending[:] = rest
                        try:
                            data = json.loads(line)
                        except (ValueError, UnicodeError):
                            raise HealthQueryError("app-server 返回无效 JSON")
                        if not isinstance(data, dict) or data.get("id") != request_id:
                            continue
                        if "error" in data:
                            err = data["error"]
                            code = err.get("code") if isinstance(err, dict) else None
                            raise HealthQueryError("app-server RPC 错误%s" % ("（%s）" % code if isinstance(code, int) else ""))
                        return data.get("result")
                    if not selector.select(remaining):
                        raise TimeoutError()
                    chunk = os.read(p.stdout.fileno(), 65536)
                    if not chunk:
                        raise HealthQueryError("app-server 提前退出")
                    pending.extend(chunk)
                    if len(pending) > 1_000_000:
                        raise HealthQueryError("app-server 响应过大")

            send({"id": 1, "method": "initialize", "params": {
                "clientInfo": {"name": "board-probe", "title": "board", "version": "0.1"}}})
            receive(1)
            send({"method": "initialized"})
            send({"id": 2, "method": "account/rateLimits/read"})
            result = receive(2)
        rate = result.get("rateLimits") if isinstance(result, dict) else None
        if not isinstance(rate, dict) or not isinstance(rate.get("primary"), dict):
            raise HealthQueryError("额度响应缺少 primary")
        windows = []
        for slot in ("primary", "secondary"):
            window = rate.get(slot)
            if window is None:
                continue
            if not isinstance(window, dict):
                raise HealthQueryError("额度响应窗口无效")
            used = window.get("usedPercent")
            if isinstance(used, bool) or not isinstance(used, (int, float)) or not math.isfinite(used) or not 0 <= used <= 100:
                raise HealthQueryError("额度响应缺少有效 usedPercent")
            minutes = window.get("windowDurationMins")
            windows.append({"档位": slot, "已用": used, "窗口分钟": minutes,
                            "名字": quota_window_name(minutes), "重置": window.get("resetsAt")})
        # 保留原 primary 摘要；页面与提醒遍历两档窗口。
        return dict(windows[0], 窗口=windows, 套餐=rate.get("planType"), 更新时间=now())

    finally:
        # 结束整个查询进程组并 wait，正常、超时、协议错误都不留下后台进程。
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        finally:
            p.wait()
            p.stdin.close()
            p.stdout.close()


def _get_json(url, key, *, raw_auth=False):
    """余额接口按原行为直连；凭据只存在请求头和内存中。"""
    req = urllib.request.Request(url, headers={"Authorization": key if raw_auth else "Bearer " + key, "Accept": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _zai_quota(data):
    """官方用量脚本的 limits；可选重置字段缺失时不推算。"""
    def failure_message(value):
        msg = value.get("msg") if isinstance(value, dict) else None
        if not isinstance(msg, str):
            msg = ""
        msg = re.sub(r"[\x00-\x1f\x7f]", "", msg)[:60]
        if "不存在codingplan" in re.sub(r"\s+", "", msg):
            return "编程套餐已到期或未开通"
        return "额度接口返回失败" + (("：" + msg) if msg else "")

    if not isinstance(data, dict):
        raise HealthQueryError("额度响应无效")
    if data.get("success") is False or ("code" in data and (type(data["code"]) is not int or data["code"] not in (0, 200))):
        raise HealthQueryError(failure_message(data))
    if "error" in data:
        raise HealthQueryError("额度响应无效")
    payload = data.get("data", data)
    limits = payload.get("limits") if isinstance(payload, dict) else None
    if not isinstance(limits, list) or not limits:
        raise HealthQueryError("额度响应缺少 limits")

    def number(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise HealthQueryError("额度响应数字无效")
        return value

    windows = []
    for item in limits:
        if not isinstance(item, dict):
            raise HealthQueryError("额度响应格式无效")
        kind = item.get("type")
        if kind not in ("TOKENS_LIMIT", "CREDIT_LIMIT", "TIME_LIMIT"):
            continue
        pct = number(item.get("percentage"))
        if pct > 100:
            raise HealthQueryError("额度响应百分比无效")
        label = "MCP 月度" if kind == "TIME_LIMIT" else "编程额度"
        # 不把 token/credit 百分比换算成调用次数。
        window = {"名字": label, "已用": pct, "限额": 100, "单位": "%"}
        if kind == "TIME_LIMIT" and ("currentValue" in item or "usage" in item):
            used, total = number(item.get("currentValue")), number(item.get("usage"))
            if total <= 0 or used > total:
                raise HealthQueryError("额度响应计数无效")
            window.update(已用=used, 限额=total, 单位="次")
        reset = item.get("nextResetTime")
        if reset is not None:
            reset = number(reset) / 1000
            if not 0 < reset < 253402300800:
                raise HealthQueryError("额度响应重置时间无效")
            window["重置"] = reset
        windows.append(window)
    if not windows:
        raise HealthQueryError("额度响应没有可识别的额度")
    return windows


def _balance_api(provider, settings):
    # 已安装配置可能明确保存旧空值；在看板中兼容，不改用户配置文件。
    return settings.get("balance_api") or {
        "zai-coding-plan": "zai-quota", "zhipuai": "zhipu-no-api"
    }.get(provider, "")


def check_balances():
    """从配置取 provider 与主机，只通过 keys.get_key 取凭据，逐家返回成功或错误。"""
    out = []
    for provider, settings in CONFIG["providers"].items():
        api = _balance_api(provider, settings)
        if api == "zhipu-no-api":
            out.append({"provider": provider, "名字": "GLM 智谱按量付费",
                        "说明": "未查到公开余额查询接口，请到 bigmodel.cn 网站查看"})
            continue
        if api not in ("deepseek", "moonshot", "zai-quota"):
            continue
        name = {"deepseek": "DeepSeek", "moonshotai-cn": "Kimi", "zai-coding-plan": "GLM 编程套餐"}.get(provider, provider)
        row = {"provider": provider, "名字": name}
        try:
            key = keys.get_key(provider)
            if not key:
                raise HealthQueryError("未配置 API key")
            host = settings.get("api_host") or ""
            if not re.fullmatch(r"[A-Za-z0-9.-]+(?::[0-9]+)?", host):
                raise HealthQueryError("余额接口主机配置无效")
            if api == "zai-quota":
                data = _get_json("https://" + host + "/api/monitor/usage/quota/limit", key, raw_auth=True)
                row.update(额度=_zai_quota(data), 更新时间=now())
                out.append(row)
                continue
            path = "/user/balance" if api == "deepseek" else "/v1/users/me/balance"
            data = _get_json("https://" + host + path, key)
            if api == "deepseek":
                info = data["balance_infos"][0]
                value = float(info["total_balance"])
                row.update(币种=info.get("currency") or "CNY", 可用=bool(data.get("is_available")))
            else:
                value = float(data["data"]["available_balance"])
                row["币种"] = "CNY"
            if not math.isfinite(value):
                raise HealthQueryError("余额响应不是有效数字")
            row.update(余额=value, 更新时间=now())
        except Exception as exc:
            row["错误"] = _query_error(exc)
        finally:
            key = None
        out.append(row)
    # 没有公开余额接口的 provider 仍保留原有提示。
    for provider, settings in CONFIG["providers"].items():
        if _balance_api(provider, settings):
            continue
        try:
            if keys.key_source(provider)["kind"] in ("keychain", "env_file"):
                out.append({"provider": provider, "名字": {"xiaomi": "MiMo", "zai-coding-plan": "GLM（Z.AI Coding Plan）", "zhipuai": "GLM（智谱国内按量付费）"}.get(provider, provider),
                            "说明": "没有余额接口"})
        except Exception:
            pass
    return out


def _merge_health(old, fresh):
    """失败只覆盖错误和查询时间，成功替换旧值并清除旧错误。更新时间始终代表成功时间。"""
    if fresh.get("错误"):
        result = dict(old or {})
        result.update(fresh)
    else:
        result = dict(fresh)
    result["查询时间"] = now()
    return result


def refresh_health(kind):
    """网络查询在锁外执行，页面不会因接口慢而阻塞；提交时按 provider 保留旧余额。"""
    if kind == "codex":
        try:
            fresh = check_codex_quota()
        except Exception as exc:
            fresh = {"错误": _query_error(exc)}
        with _health_lock:
            HEALTH["codex"] = _merge_health(HEALTH["codex"], fresh)
    elif kind == "audit":
        # 日审与额度查询独立；异常明确显示失败，不能让健康条误报正常。
        try:
            fresh = audit.check()
        except Exception:
            fresh = {"时间": now(), "核对卡数": 0, "问题": ["日审执行失败"], "提醒": []}
        with _health_lock:
            HEALTH["日审"] = fresh
    else:
        fresh = check_balances()
        with _health_lock:
            old = {row["provider"]: row for row in HEALTH["余额"]}
            HEALTH["余额"] = [_merge_health(old.get(row["provider"]), row) for row in fresh]


_receipt_failures = {}


def receipt_dependency_signature(path):
    """缺少旧格式依赖也是一种状态；文件后来出现时必须重新读取。"""
    try:
        return file_signature(path, RESULT_LIMIT)
    except FileNotFoundError:
        return ("missing",)


def receipt_snapshot(previous=None):
    """按签名缓存回执元信息（含失败）；只有有效工人才触发结束通知。"""
    found, failures = {}, {}
    previous = previous or {}
    if probe_deliver():
        return None
    try:
        with os.scandir(DELIVER) as tasks:
            for task in tasks:
                if task.name.startswith(".") or not task.is_dir(follow_symlinks=False):
                    continue
                with os.scandir(task.path) as rounds:
                    for rnd in rounds:
                        if rnd.name.startswith(".") or not rnd.is_dir(follow_symlinks=False):
                            continue
                        try:
                            path = os.path.join(rnd.path, "回执.json")
                            try:
                                signature = file_signature(path, RESULT_LIMIT)
                            except FileNotFoundError:
                                continue
                            old = previous.get(path) or _receipt_failures.get(path)
                            info_path = os.path.join(rnd.path, "运行信息.json")
                            if old and old[0][0] == signature:
                                # 旧格式回执的工人来自运行信息，那个文件也必须未变。
                                dependency = old[0][1]
                                if dependency is None or receipt_dependency_signature(info_path) == dependency:
                                    (found if old[1] is not None else failures)[path] = old
                                    continue
                            receipt = read_json(path)
                            worker = receipt.get("工人") if isinstance(receipt, dict) else None
                            dependency = None
                            if isinstance(receipt, dict) and receipt and not worker:
                                dependency = receipt_dependency_signature(info_path)
                                info = read_json(info_path) if dependency != ("missing",) else None
                                worker = info.get("工人") if isinstance(info, dict) else None
                                if receipt_dependency_signature(info_path) != dependency:
                                    continue
                            if file_signature(path, RESULT_LIMIT) != signature:
                                continue
                            if worker not in ("codex", "opencode"):
                                worker = None
                                log("跳过一轮回执：元信息读取失败（%s）" %
                                    os.path.relpath(path, os.path.dirname(DELIVER)))
                            # 失败也保留签名；文件或旧格式依赖变化后才再次解析、记日志。
                            (found if worker is not None else failures)[path] = ((signature, dependency), worker)
                        except (OSError, ValueError, TypeError, AttributeError):
                            log("跳过一轮回执：元信息读取失败（%s）" %
                                os.path.relpath(path, os.path.dirname(DELIVER)))
                            continue
    except OSError:
        return None
    _receipt_failures.clear()
    _receipt_failures.update(failures)
    return found


class HealthMonitor:
    """每个查询通道独立计时；回执事件会唤醒线程，查询中的事件留到下一次处理。"""
    def __init__(self):
        self.stop_event = threading.Event()
        self.events = {kind: threading.Event() for kind in ("codex", "balances", "audit")}
        self.seen = receipt_snapshot() or {}
        self.threads = []

    def poll_receipts(self):
        current = receipt_snapshot(self.seen)
        if current is None:
            return
        # 首次已有回执仅作基线，启动时两个查询线程本来就会立即查一次。
        if self.seen is not None:
            for path, (signature, worker) in current.items():
                if self.seen.get(path) != (signature, worker):
                    self.events["codex" if worker == "codex" else "balances"].set()
        self.seen = current

    def query_loop(self, kind):
        trigger = self.events[kind]
        due = time.monotonic()
        while not self.stop_event.is_set():
            trigger.wait(max(0, due - time.monotonic()))
            if self.stop_event.is_set():
                return
            trigger.clear()
            due = time.monotonic() + (AUDIT_INTERVAL if kind == "audit" else HEALTH_INTERVAL)
            refresh_health(kind)

    def watch_loop(self):
        while not self.stop_event.wait(1):
            try:
                self.poll_receipts()
            except (OSError, ValueError, TypeError, AttributeError):
                log("回执监测遇到坏数据；下次继续扫描")

    def start(self):
        for target, args in [(self.watch_loop, ()), (self.query_loop, ("codex",)), (self.query_loop, ("audit",))] + (
                [(self.query_loop, ("balances",))] if CHECK_BALANCE else []):
            thread = threading.Thread(target=target, args=args, daemon=True)
            self.threads.append(thread)
            thread.start()

    def stop(self):
        """用于服务退出与测试回收；唤醒等待线程，不强行中断正在结束的限时查询。"""
        self.stop_event.set()
        for event in self.events.values():
            event.set()
        for thread in self.threads:
            thread.join(timeout=QUERY_TIMEOUT + 10)


def health_loop():
    """兼容原入口；监测不依赖浏览器轮询，新回执最迟下一秒触发补查。"""
    monitor = HealthMonitor()
    monitor.start()
    return monitor


# ---------- 7. 汇总成页面要的四块 ----------
def runs_card_project(task):
    """交付目录读不到时，从 runs/ 里这件活的任务卡找"项目："。"""
    try:
        names = sorted((n for n in os.listdir(RUNS) if n.startswith(task + "-") and n.endswith(".txt")),
                       key=lambda n: round_order(n[len(task) + 1:-4]))
    except Exception:
        return ""
    for n in reversed(names):
        p = card_project(read_text(os.path.join(RUNS, n), LOG_LIMIT))
        if p:
            return p
    return ""


def task_card_project(task, rounds):
    """项目是任务元数据，不依赖所选轮次是否加载正文。

    延续账本之后的回退顺序：由新到旧查任务卡，同轮优先独立任务卡，
    兼容只保存在交付目录的任务.txt；不读取历史结果或事件。
    """
    for row in reversed(rounds):
        for path in (os.path.join(RUNS, "%s-%s.txt" % (task, row["轮次"])),
                     os.path.join(row["目录"], "任务.txt")):
            project = card_project(read_text(path, LOG_LIMIT))
            if project:
                return project
    return runs_card_project(task)


def default_project(task):
    if task.startswith("HD-"):
        return "秘书杂活"
    if STD_ID.match(task):
        return "未归类"
    return "测试与验收"


def build_tasks(task_id=None, selected_round=None):
    """把看板卡片、交付轮次、表现记录按任务号合并成任务列表。"""
    errors = []
    cards = {}
    # 账本损坏显式提醒，不能悄悄当成没有账本。
    try:
        cards = read_ledger()
    except Exception as e:
        errors.append("读账本失败：%s" % e)
    rounds, err = scan_rounds(task_id, selected_round)
    if err:
        errors.append(err)
    perf = read_perf()

    projects = read_projects()
    tasks = {}
    for r in rounds:
        t = tasks.setdefault(r["任务"], {"任务": r["任务"], "轮次": [], "看板": None})
        r["验收"] = None
        p = perf.get((r["任务"], r["轮次"]))
        if p is not None:
            if p.get("通过验收") is True:
                r["验收"] = "通过"
            elif p.get("通过验收") is False:
                r["验收"] = "未通过"
        t["轮次"].append(r)
    for key, c in cards.items():
        if task_id is not None and key != task_id:
            continue
        t = tasks.setdefault(key, {"任务": key, "轮次": [], "看板": None})
        t["看板"] = c

    for t in tasks.values():
        t["轮次"].sort(key=lambda r: round_order(r["轮次"]))
        c = t["看板"]
        last = t["轮次"][-1] if t["轮次"] else None
        t["标题"] = (c and c["标题"]) or next((r["标题"] for r in reversed(t["轮次"]) if r["标题"]), "") or t["任务"]
        t["项目"] = (c and c["项目"]) or task_card_project(t["任务"], t["轮次"]) or default_project(t["任务"])
        t["负责对话"] = projects.get(t["项目"], "未登记")
        # 账本完成结论是主数据；即使表现记录相反也必须覆盖最新一轮。
        if last and c and c.get("数据源") == "账本" and c["状态"] == "done":
            last["验收"] = c["结论"]
        times = [x for x in [c and c["完成"], c and c["创建"], c and c.get("更新")] + [r["结束"] or r["开始"] for r in t["轮次"]] if x]
        t["最近活动"] = max(times) if times else 0
    return tasks, errors


VERDICT_TEXT = {"timeout": "超时", "failed": "失败", "interrupted": "中断"}


def task_status(t):
    """一件任务现在的总状态（给"最近完成"用）：通过 / 未通过 / 卡住 / 待验收 / 超时 / 失败 / 中断。"""
    c, last = t["看板"], (t["轮次"][-1] if t["轮次"] else None)
    if not c or c.get("数据源") != "账本":
        if any(r["状态"] == "running" for r in t["轮次"]):
            return "进行中", "run"
        label = "测试" if t["任务"].startswith("TEST-") else "未登记"
        suffix = VERDICT_TEXT.get(last["状态"]) if last else None
        return label + ("（%s）" % suffix if suffix else ""), "gray"
    # 人工账本决定任务层状态；旧轮次的失败/需输入不能推翻最终验收或放弃。
    if c and c.get("数据源") == "账本":
        if c["状态"] == "dropped":
            return "已放弃", "gray"
        if c["状态"] == "done":
            return c["结论"], "ok" if c["结论"] == "通过" else "bad"
        if c["状态"] == "blocked":
            return "卡住", "bad"
        if not last:
            return "准备中", "wait"
    if any(r["状态"] == "running" for r in t["轮次"]):
        return "进行中", "run"
    if c and c["状态"] == "blocked":
        return "卡住", "bad"
    if last and last["需输入"] and not last["验收"]:
        return "等输入", "wait"
    if last:
        if last["验收"] == "通过":
            return "通过", "ok"
        if last["验收"]:
            return last["验收"], "bad"
        if last["状态"] in VERDICT_TEXT:
            return VERDICT_TEXT[last["状态"]], "bad"
        if c and c["状态"] == "done":
            return "已完成", "ok"
        if not c:
            return ("测试" if t["任务"].startswith("TEST-") else "未登记"), "gray"
        return "待验收", "wait"
    if c and c["状态"] == "done":
        return "已完成", "ok"
    return c["状态"] if c else "未知", "wait"


# ---------- 版本与派活：只读、有界，不调用发布器或调度器 ----------
VERSION_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
PANEL_SCAN_LIMIT = 20000
PANEL_JSON_LIMIT = 65536
PANEL_BATCH_BYTES = 2 * 1024 * 1024
PANEL_ROW_LIMIT = 2000
RELEASE_CACHE_SECONDS = 30
RELEASE_PROBE_TIMEOUT = 3
_release_lock = threading.Lock()
_release_cache = OrderedDict()


def release_probe(root, operation):
    """独立进程可强制超时；连同 ps/lsof 子进程一起回收。"""
    process = None
    try:
        process = subprocess.Popen(
            [sys.executable, '-B', os.path.join(HERE, 'release_probe.py'), root, operation],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            start_new_session=True)
        output, _ = process.communicate(timeout=RELEASE_PROBE_TIMEOUT)
        if process.returncode != 0:
            raise ValueError()
        result = json.loads(output)
        if not isinstance(result, dict) or not isinstance(result.get('rows'), (list, type(None))):
            raise ValueError()
        return result
    except subprocess.TimeoutExpired:
        return {'rows': None, 'issue': '读取超时，未知'}
    except Exception:
        return {'rows': None, 'issue': '读取失败，未知'}
    finally:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            process.stdout.close()


def release_snapshot(root, operation):
    """请求不等待扫描；每个数据根/接口最多一个在途读取，成功及失败均缓存。"""
    key = (os.path.abspath(root), operation)
    with _release_lock:
        entry = _release_cache.get(key)
        if entry is not None:
            if entry['loading']:
                return {'rows': None, 'issue': '正在读取，未知'}
            if time.monotonic() - entry['at'] < RELEASE_CACHE_SECONDS:
                return entry['result']
        else:
            if len(_release_cache) >= 4:
                removable = next((k for k, v in _release_cache.items() if not v['loading']), None)
                if removable is None:
                    return {'rows': None, 'issue': '读取繁忙，未知'}
                del _release_cache[removable]
            entry = {}
            _release_cache[key] = entry
        entry['loading'] = True

        def refresh():
            try:
                result = release_probe(key[0], operation)
            except Exception:
                result = {'rows': None, 'issue': '读取失败，未知'}
            with _release_lock:
                entry.update(loading=False, at=time.monotonic(), result=result)

        try:
            threading.Thread(target=refresh, daemon=True).start()
        except Exception:
            entry.update(loading=False, at=time.monotonic(),
                         result={'rows': None, 'issue': '读取失败，未知'})
        return {'rows': None, 'issue': '正在读取，未知'}


def panel_number(value):
    try:
        if type(value) in (int, float) and math.isfinite(value) and value >= 0:
            return value
    except OverflowError:
        pass
    return None


def panel_text(value, limit=160):
    return value[:limit] if isinstance(value, str) else ""


def panel_timestamp(value):
    """接受 UTC ISO 8601 的 Z、+00:00 和小数秒；无时区的时间不猜测。"""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return panel_number(parsed.timestamp()) if parsed.tzinfo is not None else None
    except (ValueError, OverflowError, OSError):
        return None


def panel_files(path, pattern, count):
    """仅扫描文件元信息；保留最近 N 项，内存和扫描量都有上限。"""
    import heapq
    newest, leases = [], []
    issue = ""
    try:
        with directory(path) as parent, os.scandir(parent) as entries:
            for number, entry in enumerate(entries):
                if number >= PANEL_SCAN_LIMIT:
                    issue = "目录扫描已达上限，仅展示扫描范围内最近记录"
                    break
                try:
                    info = entry.stat(follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode):
                        continue
                except OSError:
                    continue
                name = entry.name
                if name.endswith('.lease') and VERSION_ID.fullmatch(name[:-6]):
                    leases.append(name[:-6])
                if pattern.fullmatch(name):
                    item = (info.st_mtime, name)
                    if len(newest) < count:
                        heapq.heappush(newest, item)
                    elif item > newest[0]:
                        heapq.heapreplace(newest, item)
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        issue = "目录暂时读不到"
    return sorted(newest, reverse=True), leases, issue


def panel_json(path, limit=PANEL_JSON_LIMIT):
    try:
        value = json.loads(safe_read(path, limit))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, RecursionError):
        return {}


def release_panel(root):
    root = os.path.abspath(root)
    folder = os.path.join(root, '运行时', '发布')
    files, leases, issue = panel_files(
        folder, re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\.json\Z'), 10)
    state = panel_json(os.path.join(folder, 'state.json'))
    current = None
    try:
        # 只读链接文字，不解析/遍历目标，也不把 state.current 冒充稳定入口。
        with directory(root) as parent:
            target = os.readlink('框架', dir_fd=parent)
        target = os.path.normpath(os.path.join(root, target))
        name = os.path.basename(target)
        if os.path.dirname(target) == os.path.join(root, '框架-版本') and VERSION_ID.fullmatch(name):
            current = name
    except (OSError, ValueError):
        pass

    health = release_snapshot(root, 'versions')
    usage = release_snapshot(root, 'usage')
    checked = {row['version']: row for row in (health.get('rows') or [])
               if isinstance(row, dict) and isinstance(row.get('version'), str)
               and VERSION_ID.fullmatch(row['version'])}

    def version(name):
        path = os.path.join(folder, name + '.json')
        item = panel_json(path)
        check = checked.get(name, {})
        timestamp = panel_timestamp(item.get('published_at') or check.get('published_at'))
        return {'提交': name[:7], '当前': name == current,
                '验收通过': item.get('accepted') if type(item.get('accepted')) is bool else None,
                '验收方式': {'paid': '付费验收', 'verify': '离线验收', 'migration': '迁移保留'}.get(
                    item.get('acceptance') if isinstance(item.get('acceptance'), str) else '', '未知'),
                '时间': timestamp, '约': False,
                '完整': check.get('valid'), '可回滚': check.get('rollbackable'),
                '原因': panel_text(check.get('problem'), 500), '清理中': check.get('deleting'),
                '工人数': check.get('workers'), '占用未知': check.get('usage_unknown'),
                '记录缺失': not bool(item)}

    previous = state.get('previous')
    previous = previous if isinstance(previous, str) and VERSION_ID.fullmatch(previous) else None
    old_leases = [name[:7] for name in leases if name != current]
    names = [name[:-5] for _, name in files]
    # 接口也会发现未登记或目录缺失的版本，不能只列元数据文件。
    names = list(dict.fromkeys(([current] if current else []) + names + list(checked)))[:10]
    old_workers, uncertain = [], []
    for worker in usage.get('rows') or []:
        if worker.get('active') is False:
            continue
        item = {'提交': panel_text(worker.get('version')), '工人': panel_text(worker.get('worker')),
                '任务': panel_text(worker.get('task')), '轮': panel_text(worker.get('round')),
                'PID': panel_number(worker.get('pid'))}
        if worker.get('active') is not True or current is None:
            uncertain.append(item)
        elif worker.get('version') != current:
            old_workers.append(item)
    return {'当前版本': version(current) if current else None,
            '上一个版本': previous[:7] if previous else None,
            '版本列表': [version(name) for name in names],
            '版本检查提示': panel_text(health.get('issue'), 500),
            '旧版本工人': old_workers if usage.get('rows') is not None and current else None,
            '无法确认工人': uncertain if usage.get('rows') is not None else None,
            '使用记录提示': panel_text(usage.get('issue'), 500),
            '使用记录说明': '仅列已登记工人；旧启动器可能没有登记，空列表不代表旧版本无人使用。版本检查及使用记录缓存 30 秒。',
            '旧版本锁记录': sorted(old_leases)[:10], '旧版本锁记录数': len(old_leases),
            '旧版本占用': '未知',
            '锁说明': '锁文件会保留，不能据此确认当前工人占用；无锁记录也不能排除旧版工人',
            '提示': issue or ('稳定入口与发布状态不一致，以入口为准' if current and state.get('current') not in (None, current) else '')}


def dispatch_tokens(row):
    """与 dispatch.token_count 相同口径；缺失用量留空，不冒充零。"""
    usage = row.get('用量')
    if not isinstance(usage, dict):
        return None
    if row.get('工人') == 'codex':
        fields = ('input_tokens', 'output_tokens')
    elif row.get('工人') == 'opencode':
        total = panel_number(usage.get('total'))
        if total is not None:
            return total
        fields = ('input', 'output', 'reasoning', 'cached_input_tokens', 'cache_write_input_tokens')
    else:
        return None
    values = [panel_number(usage.get(key)) for key in fields]
    return panel_number(sum(value for value in values if value is not None)) if any(value is not None for value in values) else None


def dispatch_quota(value):
    if not isinstance(value, dict):
        return None
    used = panel_number(value.get('已用'))
    if used is None or used > 100 or value.get('窗口分钟') not in (None, 10080):
        return None
    return {'已用': used, '时间': panel_number(value.get('时间')),
            '重置': panel_number(value.get('重置')), '窗口分钟': panel_number(value.get('窗口分钟'))}


def dispatch_rows(path):
    """读取有界前缀，坏行逐行跳过；写到一半的末行留到下次刷新。"""
    rows, bad, limited, issue = [], 0, False, ''
    try:
        with directory(os.path.dirname(path)) as parent:
            fd = os.open(os.path.basename(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, 'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError()
                data = stream.read(PANEL_BATCH_BYTES + 1)
        limited = len(data) > PANEL_BATCH_BYTES
        data = data[:PANEL_BATCH_BYTES]
        lines = data.split(b'\n')
        # 写入器每条必带换行；不解析未完成的最后一条。
        incomplete = bool(lines.pop())
        limited = limited or len(lines) > PANEL_ROW_LIMIT
        for line in lines[:PANEL_ROW_LIMIT]:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError()
                rows.append(row)
            except (ValueError, RecursionError):
                bad += 1
        if incomplete and not limited:
            issue = '末行尚未完整，等待下次刷新'
    except FileNotFoundError:
        issue = '批次文件已不存在'
    except (OSError, ValueError):
        issue = '批次文件暂时读不到'
    return rows, bad, limited, issue


def dispatch_panel(root, tasks):
    folder = os.path.join(root, '运行时', '派活')
    # 每批最多两个文件；先取十个文件再按批次去重，保留最近五批。
    files, _, issue = panel_files(folder, re.compile(r'\d{8}-\d{6}-[0-9a-f]{12}\.(?:jsonl|批次\.json)\Z'), 10)
    batches = {}
    for mtime, name in files:
        batch = name.split('.')[0]
        if batch not in batches and len(batches) < 5:
            batches[batch] = {'mtime': mtime, 'logs': [], 'journal': False, 'metadata': False}
        if batch in batches:
            batches[batch]['journal' if name.endswith('.jsonl') else 'metadata'] = True
    # 启动日志文件名包含带长度的任务号与轮次；不读取日志正文，不重新扫描交付。
    if batches:
        try:
            with directory(folder) as parent, os.scandir(parent) as entries:
                for number, entry in enumerate(entries):
                    if number >= PANEL_SCAN_LIMIT:
                        issue = '目录扫描已达上限，在跑关联可能不全'
                        break
                    name = entry.name
                    batch = name[:28]
                    if batch not in batches or not name.startswith(batch + '-') or not name.endswith('.log'):
                        continue
                    rest = name[29:-4]
                    length, sep, identity = rest.partition('-')
                    if not sep or not length.isdigit() or len(length) > 4:
                        continue
                    size = int(length)
                    task, rnd = identity[:size], identity[size + 1:]
                    if identity[size:size + 1] != '-' or not all(re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_-]*', x) for x in (task, rnd)):
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    if len(batches[batch]['logs']) < PANEL_ROW_LIMIT:
                        batches[batch]['logs'].append((task, rnd))
                    else:
                        issue = '启动记录已达上限，在跑关联可能不全'
        except (OSError, ValueError):
            issue = '启动记录暂时读不到，在跑关联可能不全'
    result = []
    for batch, source in batches.items():
        meta = panel_json(os.path.join(folder, batch + '.批次.json'), PANEL_BATCH_BYTES)
        total = meta.get('总件数')
        valid_meta = (meta.get('批次号') == batch and type(total) is int and total >= 0
                      and isinstance(meta.get('任务'), list) and len(meta['任务']) == total)
        ended = valid_meta and ('结束时间' in meta or '结束状态' in meta)
        roster = [(item.get('任务'), item.get('轮')) for item in meta.get('任务', [])
                  if isinstance(item, dict) and isinstance(item.get('任务'), str)
                  and isinstance(item.get('轮'), str) and item['任务'] and item['轮']] if valid_meta else []
        roster_valid = valid_meta and len(set(roster)) == total
        roster_set = set(roster)
        raw, bad, limited, warning = dispatch_rows(os.path.join(folder, batch + '.jsonl'))
        if valid_meta and warning == '批次文件已不存在':
            warning = ''  # 新批次在首项结果前、校验失败时可以没有 JSONL。
        if source['metadata'] and not valid_meta:
            warning += ' 批次信息不可读或格式不完整，总数与在跑未知。'
        rows, seen, before, after = [], set(), [], []
        for row in raw:
            # 身份关联和去重保留原文；展示字段才截短。
            task = row.get('任务') if isinstance(row.get('任务'), str) else ''
            rnd = row.get('轮') if isinstance(row.get('轮'), str) else ''
            if row.get('批次') not in (None, batch):
                bad += 1
                continue
            identity = (task, rnd)
            if roster_valid and identity not in roster_set:
                bad += 1
                continue
            if task and rnd and identity in seen:
                bad += 1
                continue
            seen.add(identity)
            status = panel_text(row.get('状态')) or '未知'
            rows.append({'任务': panel_text(task) or '未知任务', '轮': panel_text(rnd) or '未知轮次',
                         '工人': panel_text(row.get('工人')), '模型': panel_text(row.get('模型')),
                         '状态': status, '用时秒': panel_number(row.get('用时秒')),
                         'tokens': dispatch_tokens(row)})
            for key, target in (('额度前', before), ('额度后', after)):
                quota = dispatch_quota(row.get(key))
                if quota:
                    target.append(quota)
        recorded = len(rows)
        # 新批次只依赖队列与结果行；旧批次继续用日志关联提供至少数。
        pending = (roster if valid_meta else source['logs'])
        for task, rnd in pending[:PANEL_ROW_LIMIT]:
            if (task, rnd) in seen:
                continue
            seen.add((task, rnd))
            rounds = tasks.get(task, {}).get('轮次', [])
            active = next((r for r in rounds if r.get('轮次') == rnd), {})
            rows.append({'任务': panel_text(task), '轮': panel_text(rnd), '工人': panel_text(active.get('工人')),
                         '模型': panel_text(active.get('模型')), 'tokens': None, '用时秒': None,
                         '状态': ('无结果（已结束）' if ended else '等待结果') if valid_meta else
                         ('在跑' if active.get('状态') == 'running' else '等待结果')})
        counts = {key: 0 for key in ('完成', '失败', '跳过', '在跑', '未知')}
        for row in rows:
            key = {'额度跳过': '跳过', '被中断': '跳过'}.get(row['状态'], row['状态'])
            counts[key if key in counts else '未知'] += 1
        try:
            start = datetime.strptime(batch[:15], '%Y%m%d-%H%M%S').timestamp()
        except (ValueError, OverflowError):
            start = source['mtime']
        if valid_meta:
            start = panel_timestamp(meta.get('开始时间'))
            complete = ended or (roster_valid and not bad and not limited and not warning)
            counts['在跑'] = 0 if ended else (total - recorded if complete else None)
            warning += (' 批次已有结束记录，未写结果的项不算在跑。' if ended else
                        ' 没有结束记录；“在跑”指尚无结果的队列项（含待启动），不能证明进程仍在运行。')
            if len(roster) > PANEL_ROW_LIMIT:
                warning += ' 任务明细仅补列队列前 %d 项，批次总数不受影响。' % PANEL_ROW_LIMIT
        else:
            total, complete = None, False
        first = min(before, key=lambda q: q['时间'] if q['时间'] is not None else float('inf')) if before else None
        last = max(after, key=lambda q: q['时间'] or 0) if after else None
        known_tokens = [r['tokens'] for r in rows if r['tokens'] is not None]
        result.append({'批次': batch, '开始': start, '已记录总数': recorded if valid_meta else len(rows), '总数': total,
                       '结束': panel_timestamp(meta.get('结束时间')) if valid_meta else None,
                       '结束状态': panel_text(meta.get('结束状态')) if valid_meta else '',
                       '有结束记录': bool(ended), '有批次信息': valid_meta,
                       '计数': counts, '在跑完整': complete, 'tokens': panel_number(sum(known_tokens)) if known_tokens else None,
                       '用量缺失': len(rows) - len(known_tokens), '额度前': first, '额度后': last,
                       '任务': rows, '坏行': bad, '截断': limited, '提示': warning})
    return {'批次': result, '提示': issue,
            '说明': '完成指执行成功，不代表验收通过。新批次“在跑”为无结束记录时尚无结果的项数，不表示进程存活；旧批次总数未知，在跑仅提供已关联轮次的至少数。token 为已知用量合计。'}


def release_dispatch_panel(tasks):
    root = CONFIG.get('work_dir') or os.path.dirname(DELIVER)
    return {'发布': release_panel(root), '派活': dispatch_panel(root, tasks)}


def build_state(project=None):
    tasks, errors = build_tasks()
    # 费用摘要随仍存在的轮次保留；不能因固定容量在大量静态轮次间反复淘汰/重读。
    # 扫描失败时保留缓存，避免短暂目录错误清空整个基线。
    if not errors:
        paths = {r["_ev"] for t in tasks.values() for r in t["轮次"]}
        with _events_lock:
            for path in list(_cost_cache):
                if path not in paths:
                    del _cost_cache[path]
    t_now = now()
    cutoff = t_now - RECENT_DAYS * 86400

    # 项目列表（"全部"不含测试）
    counts = {}
    for t in tasks.values():
        counts[t["项目"]] = counts.get(t["项目"], 0) + 1
    projects = [{"名字": k, "件数": v} for k, v in sorted(counts.items(), key=lambda kv: (kv[0] == "测试与验收", kv[0]))]

    def in_scope(t):
        if project and project != "全部":
            return t["项目"] == project
        return t["项目"] != "测试与验收"

    attention, running, recent = [], [], []
    for t in tasks.values():
        c = t["看板"]
        last = t["轮次"][-1] if t["轮次"] else None
        target = conversation_target(t["负责对话"])
        ledger_card = c and c.get("数据源") == "账本"
        terminal = ledger_card and c["状态"] in ("done", "dropped")
        base = {"任务": t["任务"], "标题": t["标题"], "项目": t["项目"], "负责对话": t["负责对话"]}

        # ② 正在干活：看"全部"时所有在跑的轮次都显示（包括测试），选了项目就只看这个项目
        picked = project and project != "全部"
        for r in t["轮次"]:
            if r["状态"] == "running" and not terminal and not (picked and t["项目"] != project):
                ev = parse_events(r["_ev"], r["目录"])
                last_item = next((x for x in reversed(ev["条目"]) if x["k"] not in ("end",)), None)
                running.append(dict(base, 轮次=r["轮次"], 种类="run", 工人=r["工人"], 模型=r["模型"], 档位=r["档位"],
                                    开始=r["开始"], 超时秒=r["超时秒"], 步数=ev["步数"],
                                    最近一步=(last_item or {}).get("s") or "准备中…", 最近一步时间=(last_item or {}).get("t")))
        # 看板卡片"已认领"但一轮都还没跑：显示成准备中。跑过了只是还没收卡 = 待验收，归"最近完成"
        if c and (c["状态"] == "running" or (ledger_card and c["状态"] == "ready")) and not t["轮次"] \
                and not (picked and t["项目"] != project):
            age = t_now - (c["开始"] or c["创建"] or t_now)
            if ledger_card or age < 6 * 3600:
                running.append(dict(base, 轮次="", 种类="claimed", 工人=c["派给"], 模型="", 档位="",
                                    开始=c["开始"] or c["创建"], 超时秒=None, 步数=0, 最近一步="准备中：已开卡，还没开始执行" if ledger_card else "已认领，还没开始执行", 最近一步时间=None))

        if not in_scope(t) or t["最近活动"] < cutoff:
            continue
        std = bool(STD_ID.match(t["任务"]))

        # ① 要你处理（只看正式任务）
        if std and ledger_card and not terminal:
            if c and c["状态"] == "blocked":
                attention.append(dict(base, 级别="red", 轮次=last and last["轮次"], 键="blocked:%s:%s" % (t["任务"], len(c["事件"])),
                                      事情="卡住了", 细节=redact(c["卡住原因"]) or "看板上没写原因",
                                      怎么办="把原因%s，说明怎么处理（重试 / 改方案 / 放弃）" % target))
            elif last and last["状态"] == "interrupted":
                attention.append(dict(base, 级别="red", 轮次=last["轮次"], 键="intr:%s:%s" % (t["任务"], last["轮次"]),
                                      事情="工人中断了，没有交回执", 细节="%s · %s，进程已经不在了" % (last["工人"], last["模型"]),
                                      怎么办="把中断情况%s，请查原因、重开一轮" % target))
            elif last and last["需输入"]:
                attention.append(dict(base, 级别="amber", 轮次=last["轮次"], 键="input:%s:%s" % (t["任务"], last["轮次"]),
                                      事情="工人停下来等输入", 细节=redact(last["需输入"]),
                                      怎么办="看交付说明里缺什么，把补充信息%s" % target))
            elif last and last["状态"] in ("timeout", "failed") and last["验收"] is None \
                    and not (c and c["状态"] == "done") and t_now - (last["结束"] or t_now) > 600:
                attention.append(dict(base, 级别="amber", 轮次=last["轮次"], 键="fail:%s:%s" % (t["任务"], last["轮次"]),
                                      事情="工人%s，还没人处理" % VERDICT_TEXT[last["状态"]], 细节="%s · %s" % (last["工人"], last["模型"]),
                                      怎么办="把失败情况%s，提醒处理（重试或换模型）" % target))
            elif last and last["状态"] == "done" and last["验收"] is None and not (c and c["状态"] == "done") \
                    and t_now - (last["结束"] or t_now) > 3600:
                attention.append(dict(base, 级别="gray", 轮次=last["轮次"], 键="accept:%s:%s" % (t["任务"], last["轮次"]),
                                      事情="做完一个多小时了，还没验收", 细节="%s · %s" % (last["工人"], last["模型"]),
                                      怎么办="把交付结果%s，提醒验收" % target))
            elif c and c["状态"] == "running" and not t["轮次"] \
                    and t_now - (c["开始"] or c["创建"] or t_now) >= 6 * 3600:
                attention.append(dict(base, 级别="gray", 轮次=None, 键="stale:%s" % t["任务"],
                                      事情="认领了 6 小时以上，没在跑", 细节="派给 %s" % c["派给"],
                                      怎么办="把任务情况%s，询问这件活还做不做" % target))

        # ③ 最近完成：最新一轮已经结束，或者看板上已完成 / 卡住
        if not terminal and any(r["状态"] == "running" for r in t["轮次"]):
            continue
        if not last and not (c and c["状态"] in ("done", "blocked", "dropped")):
            continue
        verdict, level = task_status(t)
        secs = sum(((r["结束"] or r["开始"] or 0) - (r["开始"] or 0)) for r in t["轮次"] if r["开始"])
        tok_in = sum((r["用量"] or {}).get("输入", 0) for r in t["轮次"])
        tok_out = sum((r["用量"] or {}).get("输出", 0) for r in t["轮次"])
        recent.append(dict(base, 结论=verdict, 结论级别=level, 轮次=last and last["轮次"], 轮数=len(t["轮次"]),
                           工人=(last or {}).get("工人") or (c and c["派给"]) or "", 模型=(last or {}).get("模型") or "",
                           档位=(last or {}).get("档位") or "", 用时秒=round(secs) if secs else None,
                           tokens=(tok_in + tok_out) or None, 费用=None, 订阅=any(r["工人"] == "codex" for r in t["轮次"]),
                           结束=t["最近活动"]))

    # ④ 健康：额度、余额、日审；有问题的同时放进"要你处理"
    with _health_lock:
        health = json.loads(json.dumps(HEALTH))
    q = health.get("codex")
    for window in ((q.get("窗口") or [q]) if q else []):
        if isinstance(window.get("已用"), (int, float)) and window["已用"] >= 90:
            name = quota_window_name(window.get("窗口分钟"))
            advice = "这周少派 Codex" if window.get("窗口分钟") == 10080 else "%s重置前少派 Codex" % name
            attention.append({"级别": "amber", "键": "codex-quota:%s:%s" % (window.get("档位", "primary"), window.get("重置")),
                              "事情": "Codex %s快用完了" % name, "细节": "已用 %s%%" % window["已用"],
                              "怎么办": advice + "，改用 OpenCode + DeepSeek"})
    for b in health.get("余额") or []:
        if isinstance(b.get("余额"), (int, float)) and b["余额"] < LOW_BALANCE:
            attention.append({"级别": "amber", "键": "bal:%s:%d" % (b["名字"], b["余额"]), "事情": "%s 余额不足" % b["名字"],
                              "细节": "还剩 ¥%.2f" % b["余额"], "怎么办": "要继续用就去官网充值；不充值的话，派活时会避开它"})
    a = health.get("日审")
    if a:
        for x in a.get("问题") or []:
            attention.append({"级别": "red", "键": "audit-bad:" + x, "事情": "日审发现问题", "细节": x,
                              "怎么办": "把这句话发到负责这个项目的对话，请查原因"})
        for x in a.get("提醒") or []:
            explained = x.startswith("⚠️ 回执例外已说明：")
            attention.append({"级别": "gray", "键": "audit-warn:" + x,
                              "事情": "回执例外已说明" if explained else "待你决定", "细节": x,
                              "怎么办": "原因已登记在账本备注中" if explained else "想好了发到负责这个项目的对话"})
        if a.get("错误"):
            errors.append(a["错误"])
    for e in errors:
        attention.append({"级别": "red", "键": "err:" + e, "事情": "看板有数据读不到", "细节": e, "怎么办": "按提示处理，或者把这句话发到负责这个项目的对话"})

    order = {"red": 0, "amber": 1, "gray": 2}
    attention.sort(key=lambda x: order.get(x["级别"], 3))
    running.sort(key=lambda x: x["开始"] or 0)
    recent.sort(key=lambda x: x["结束"] or 0, reverse=True)
    recent = recent[:40]
    # 先确定页面实际显示的任务，再补费用，冷启动不解析未显示任务的历史日志。
    for row in recent:
        costs = [event_cost(r["_ev"]) for r in tasks[row["任务"]]["轮次"] if r["工人"] == "opencode"]
        row["费用"] = sum(x for x in costs if x) if any(costs) else None
    return {"生成时间": t_now, "项目": project or "全部", "项目列表": projects,
            "要你处理": attention, "正在干活": running, "最近完成": recent, "健康": health,
            "版本与派活": release_dispatch_panel(tasks)}


def build_task(task_id, selected_round=None):
    """仅选中轮次带正文、文件和时间线，其余轮次保留切换所需摘要。"""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", task_id or ""):
        return None
    tasks, _ = build_tasks(task_id, selected_round)
    t = tasks.get(task_id)
    if not t:
        return None
    if not any(r["轮次"] == selected_round for r in t["轮次"]):
        selected_round = t["轮次"][-1]["轮次"] if t["轮次"] else None
    out_rounds = []
    for r in t["轮次"]:
        if r["轮次"] != selected_round:
            secs = (r["结束"] or r["开始"] or 0) - (r["开始"] or 0)
            out_rounds.append({"轮次": r["轮次"], "状态": r["状态"], "验收": r.get("验收"),
                               "开始": r["开始"], "结束": r["结束"], "用时秒": secs if r["开始"] else None})
            continue
        ev = parse_events(r["_ev"], r["目录"]) if r["_ev"] else {"条目": [], "步数": 0, "费用": None}
        files = []
        files_limited = False
        for dp, dns, fns in os.walk(r["目录"]):
            dns[:] = [d for d in dns if not d.startswith(".") and d not in ("node_modules", "__pycache__", ".git")]
            for fn in fns:
                p = os.path.join(dp, fn)
                try:
                    file_stat = os.stat(p, follow_symlinks=False)
                    if stat.S_ISREG(file_stat.st_mode):
                        files.append({"路径": os.path.relpath(p, r["目录"]), "大小": file_stat.st_size})
                except Exception:
                    pass
                if len(files) >= 300:
                    files_limited = True
                    break
            if files_limited:
                break
        files.sort(key=lambda f: (f["路径"].count("/"), f["路径"]))
        result = r["_result"]
        out_rounds.append({
            "轮次": r["轮次"], "状态": r["状态"], "验收": r.get("验收"), "工人": r["工人"], "模型": r["模型"], "档位": r["档位"],
            "开始": r["开始"], "结束": r["结束"], "超时秒": r["超时秒"], "用量": r["用量"], "退出码": r["退出码"], "步数": ev["步数"],
            "费用": ev["费用"],
            "任务卡": display_text(redact(read_text(os.path.join(RUNS, "%s-%s.txt" % (t["任务"], r["轮次"])), LOG_LIMIT)
                          or read_text(os.path.join(r["目录"], "任务.txt"), LOG_LIMIT)), 100_000),
            "交付说明": display_text(redact(result), 200_000),
            "决策披露": display_text(redact(disclosure(result)), 200_000),
            "需输入": display_text(redact(r["需输入"]), 6000),
            "回执": redact_obj(read_json(os.path.join(r["目录"], "回执.json"))),
            "运行信息": read_json(os.path.join(r["目录"], "运行信息.json")),
            "文件": files, "文件列表已截断": files_limited, "时间线": ev["条目"][-400:], "时间线总数": len(ev["条目"]),
        })
    verdict, level = task_status(t)
    return {"任务": t["任务"], "标题": t["标题"], "项目": t["项目"], "负责对话": t["负责对话"], "结论": verdict, "结论级别": level,
            "看板": redact_obj(t["看板"]), "当前轮次": selected_round, "轮次": out_rounds}


def read_round_file(task_id, rnd, rel):
    """抽屉里点开一个文件：只允许交付目录里这一轮的文件，防止 ../ 跳出去。"""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", task_id or "") or not re.fullmatch(r"[A-Za-z0-9_.-]+", rnd or ""):
        return None, "编号不合法"
    if rnd in (".", ".."):
        return None, "编号不合法"
    # 只做词法边界检查；保留原路径交给 safe_read 逐段拒绝链接，避免 realpath 洗白链接。
    base = os.path.abspath(os.path.join(DELIVER, task_id, rnd))
    p = os.path.abspath(os.path.join(base, rel or ""))
    if not p.startswith(base + os.sep):
        return None, "只能查看这一轮目录里的文件"
    try:
        data = safe_read(p, 300_000)
    except Exception:
        return None, "文件不存在、不可读或超过大小上限"
    if b"\x00" in data[:4096]:
        return None, "二进制文件，不显示（大小 %d 字节）" % len(data)
    return redact(data.decode("utf-8", errors="replace")), None


# ---------- 8. 网页服务 ----------
_state_cache = {}
_state_lock = threading.Lock()


def cached_state(project):
    """2 秒内重复请求直接用上次的结果，几个浏览器标签同时开也不费劲。"""
    key = project or "全部"
    with _state_lock:
        hit = _state_cache.get(key)
        if hit and now() - hit[0] < 2:
            return hit[1]
    s = build_state(project)
    with _state_lock:
        _state_cache[key] = (now(), s)
    return s


_log_times = []
_log_lock = threading.Lock()


def _log_allowed():
    """页面报错每分钟最多记 20 条，防止刷爆日志文件。"""
    t = now()
    with _log_lock:
        _log_times[:] = [x for x in _log_times if t - x < 60]
        if len(_log_times) >= 20:
            return False
        _log_times.append(t)
        return True


def log(msg):
    sys.stderr.write("[%s] %s\n" % (datetime.now().strftime("%m-%d %H:%M:%S"), redact(msg)))
    sys.stderr.flush()


class Handler(BaseHTTPRequestHandler):
    server_version = "AgentBoard"

    def log_message(self, fmt, *args):   # 不记每一次请求，日志里只留错误
        pass

    def _host_ok(self):
        """只认本机地址访问，防止别的网页用"DNS 重绑定"偷读看板。"""
        host = (self.headers.get("Host") or "").lower()
        return host in ("127.0.0.1:%d" % PORT, "localhost:%d" % PORT)

    def _send(self, code, body, ctype):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                         "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(redact_obj(obj), ensure_ascii=False), "application/json; charset=utf-8")

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, "只允许用 http://127.0.0.1:%d 访问" % PORT, "text/plain; charset=utf-8")
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        arg = lambda k: (q.get(k) or [""])[0]
        try:
            if u.path in ("/", "/index.html"):
                html = read_text(os.path.join(HERE, "index.html"), 2_000_000) or "index.html 不见了"
                return self._send(200, html.replace("__PORT__", str(PORT)), "text/html; charset=utf-8")
            if u.path == "/api/state":
                return self._json(cached_state(arg("project") or None))
            if u.path == "/api/task":
                d = build_task(arg("id"), arg("round") or None)
                return self._json(d) if d else self._json({"错误": "没有这个任务"}, 404)
            if u.path == "/api/file":
                text, err = read_round_file(arg("id"), arg("round"), arg("path"))
                return self._json({"内容": text, "错误": err}, 200 if text is not None else 400)
            if u.path == "/api/log":   # 页面上的错误记进运行日志（截断、打码）
                # 浏览器会标明请求来自哪里：只收看板自己页面发的（same-origin），别的网页偷偷发来的不记
                if self.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
                    return self._send(403, b"", "text/plain")
                if _log_allowed():
                    log("页面报错：" + clip(redact(arg("m")), 800).replace("\n", " ⏎ "))
                return self._send(204, b"", "text/plain")
            if u.path == "/healthz":
                return self._send(200, "ok", "text/plain")
            return self._send(404, "没有这个页面", "text/plain; charset=utf-8")
        except Exception as e:
            log("请求 %s 出错：%r" % (u.path, e))
            return self._json({"错误": "看板服务内部出错：%s" % type(e).__name__}, 500)


def main():
    # 日志太大就清空（launchd 把输出追加到这个文件）
    try:
        if os.path.getsize(LOG_FILE) > 1_000_000:
            open(LOG_FILE, "w").close()
    except Exception:
        pass
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as e:
        log("端口 %d 被占用，看板可能已经在运行：%s" % (PORT, e))
        sys.exit(1)
    srv.daemon_threads = True
    monitor = health_loop() if BACKGROUND else None
    log("看板已启动：http://127.0.0.1:%d" % PORT)
    try:
        srv.serve_forever()
    finally:
        srv.server_close()
        if monitor:
            monitor.stop()


if __name__ == "__main__":
    main()
