#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地追加账本：编号与状态共用这一份逻辑，供命令行、看板、日审读取。"""
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import string
import sys
import time
import uuid
from datetime import datetime, timezone
from . import config

# 编号最多两层；序号至少两位，超过 99 后自然增长，不截断或回绕。
TASK_ID = re.compile(r"^([A-Z]{2})-(\d{8})-(\d{2,})(?:-([A-Z])([1-9]\d*)?)?$")
NAME_ID = re.compile(r"(?<![A-Za-z0-9])([A-Z]{2}-\d{8}-\d{2,}(?:-[A-Z](?:[1-9]\d*)?)?)(?=-r\d+(?:\D|$)|[^A-Za-z0-9-]|$)")
TERMINAL = {"完成", "放弃"}
ACTIONS = {"开卡", "卡住", "恢复", "完成", "放弃", "备注"}


class LedgerError(Exception):
    """可以直接展示给使用者的输入或数据错误。"""


class Paths:
    """路径只通过公共配置读取，保留看板使用的属性名。"""
    def __init__(self):
        self.ledger = Path(config.path_of("ledger_file"))
        self.runs = Path(config.path_of("cards_dir"))
        self.deliver = Path(config.path_of("deliver_dir"))


def utc(stamp=None):
    """事件用 UTC 秒级时间；编号另用本地日期，避免混淆两个约定。"""
    return datetime.fromtimestamp(time.time() if stamp is None else float(stamp), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def event(action, task, stamp=None, **fields):
    """固定三个公共字段，中文不转义，便于人直接读账本。"""
    return dict({"时间": utc(stamp), "动作": action, "任务": task}, **fields)


@contextlib.contextmanager
def guard(path, write=False):
    """辅助文件锁只保护 mkdir/回收/追加的瞬间，防止两个进程同时清死锁。

    真正的事务锁仍是 .账本.lock 目录。文件锁在进程死亡时由系统释放，
    不会再产生一个需要手动回收的死锁。只读消费者绝不创建文件。
    """
    gate = Path(path).parent / ".账本.guard"
    try:
        fh = open(gate, "a+b" if write else "rb")
    except FileNotFoundError:
        if write:
            raise
        yield
        return
    with fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX if write else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class LedgerLock:
    """mkdir 成功者才可分配编号；超 30 秒可回收，旧持有者不能继续写。"""
    def __init__(self, path):
        self.path = Path(path)
        self.directory = self.path.parent / ".账本.lock"
        self.token = uuid.uuid4().hex

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + 40
        while True:
            with guard(self.path, write=True):
                try:
                    self.directory.mkdir()
                except FileExistsError:
                    if self.directory.is_symlink() or not self.directory.is_dir():
                        raise LedgerError("锁路径不是普通目录：%s" % self.directory)
                    if time.time() - self.directory.stat().st_mtime > 30:
                        shutil.rmtree(self.directory)
                        continue
                else:
                    try:
                        (self.directory / "持有者").write_text(self.token, encoding="utf-8")
                    except BaseException:
                        shutil.rmtree(self.directory)
                        raise
                    return self
            if time.monotonic() >= deadline:
                raise LedgerError("等待账本锁超时，请稍后重试")
            time.sleep(0.025)

    def owns(self):
        """回收后旧进程醒来时，用随机令牌发现自己已经失去锁。"""
        try:
            return (self.directory / "持有者").read_text(encoding="utf-8") == self.token
        except FileNotFoundError:
            return False

    def append(self, events, *, separate_tail=False):
        """先序列化全部事件，再持锁追加并 fsync；永远不覆盖原有字节。"""
        data = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events).encode("utf-8")
        if not data:
            return
        with guard(self.path, write=True):
            if not self.owns() or time.time() - self.directory.stat().st_mtime > 30:
                raise LedgerError("账本锁已过期，本次没有追加；请重试")
            # O_APPEND 保证追加位置；处理短写，断电造成的坏尾行由读取校验明确报出。
            mode = os.O_RDWR if separate_tail else os.O_WRONLY
            fd = os.open(str(self.path), mode | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                # 表现记录允许保留坏行继续写；检查与补 LF 都在同一追加锁内。
                # 账本仍由 read_events 严格校验，不自动修补其尾行。
                if separate_tail and os.fstat(fd).st_size:
                    os.lseek(fd, -1, os.SEEK_END)
                    if os.read(fd, 1) != b"\n":
                        data = b"\n" + data
                view = memoryview(data)
                while view:
                    written = os.write(fd, view)
                    if not written:
                        raise OSError("账本写入长度为零")
                    view = view[written:]
                os.fsync(fd)
            finally:
                os.close(fd)

    def __exit__(self, *exc):
        with guard(self.path, write=True):
            if self.owns():
                shutil.rmtree(self.directory)


def read_events(path):
    """严格读取账本，坏行报出行号；共享锁避免读到正在追加的一半。"""
    with guard(path):
        try:
            data = Path(path).read_bytes().decode("utf-8")
        except FileNotFoundError:
            return []
        events = []
        lines = data.split("\n")
        if lines[-1] == "":
            lines.pop()
        for number, line in enumerate(lines, 1):
            try:
                e = json.loads(line)
                if not isinstance(e, dict) or e.get("动作") not in ACTIONS:
                    raise ValueError("动作无效")
                if not isinstance(e.get("任务"), str) or not e["任务"].strip():
                    raise ValueError("任务号为空")
                datetime.strptime(e["时间"], "%Y-%m-%dT%H:%M:%SZ")
                required = {"开卡": ("标题", "项目", "来源", "派活人", "父任务"),
                            "卡住": ("原因",), "恢复": (), "完成": ("结论", "摘要"),
                            "放弃": ("原因",), "备注": ("内容",)}[e["动作"]]
                if any(not isinstance(e.get(k), str) for k in required):
                    raise ValueError("必填字段缺失或不是文字")
                if e["动作"] == "完成" and e["结论"] not in ("通过", "未通过"):
                    raise ValueError("结论必须为通过或未通过")
            except (ValueError, TypeError, KeyError) as exc:
                raise LedgerError("账本第 %d 行无效：%s" % (number, exc)) from exc
            events.append(e)
        if data and not data.endswith("\n"):
            raise LedgerError("账本末行没有换行，可能写入中断；请先核查，拒绝继续追加")
        return events


def receipt_exception_path(path):
    """独立文件不引入账本事件，旧版本回滚时可直接忽略。"""
    return Path(path).parent / "回执例外.jsonl"


def read_receipt_exceptions(path):
    """共享账本读锁；逐行校验，坏行仅提醒，不丢弃其他有效记录。"""
    sidecar = receipt_exception_path(path)
    records, invalid = [], []
    with guard(sidecar):
        try:
            data = sidecar.read_bytes()
        except FileNotFoundError:
            return records, []
        lines = data.split(b"\n")
        if lines[-1] == b"":
            lines.pop()
        for number, line in enumerate(lines, 1):
            try:
                row = json.loads(line.decode("utf-8"))
                if not isinstance(row, dict):
                    raise ValueError("记录不是对象")
                for field in ("时间", "任务", "原因"):
                    if not isinstance(row.get(field), str) or not row[field].strip():
                        raise ValueError("字段为空或不是文字")
                if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", row["时间"]):
                    raise ValueError("时间格式无效")
                datetime.strptime(row["时间"], "%Y-%m-%dT%H:%M:%SZ")
                if "轮次清单" in row and (not isinstance(row["轮次清单"], list)
                        or any(not isinstance(name, str)
                               or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]*", name)
                               for name in row["轮次清单"])):
                    raise ValueError("轮次清单无效")
            except (ValueError, TypeError, UnicodeError, RecursionError):
                invalid.append(str(number))
                continue
            records.append({"时间": row["时间"], "任务": row["任务"],
                            "原因": row["原因"].strip(), "来源": sidecar.name,
                            **({"轮次清单": row["轮次清单"]} if "轮次清单" in row else {})})
    warnings = (["⚠️ 回执例外.jsonl 第 %s 行无效，已跳过" % "、".join(invalid)]
                if invalid else [])
    return records, warnings


def receipt_exceptions(card, records=()):
    """合并两种来源的有效原因，按时间排序；同秒按来源内顺序，旁文件优先。"""
    combined = []
    for e in card["事件"]:
        if e["动作"] == "备注" and e["内容"].startswith("回执例外："):
            reason = e["内容"][len("回执例外："):].strip()
            if reason:
                combined.append({"时间": e["时间"], "任务": e["任务"],
                                 "原因": reason, "来源": "账本备注",
                                 **({"轮次清单": e["轮次清单"]} if "轮次清单" in e else {})})
    combined.extend(row for row in records if row["任务"] == card["任务"])
    return sorted(combined, key=lambda row: datetime.strptime(row["时间"], "%Y-%m-%dT%H:%M:%SZ"))


def append_receipt_exception(path, task, reason):
    """用同目录账本事务锁核对任务，再用既有追加/fsync 协议写独立文件。"""
    if not isinstance(reason, str) or not reason.strip():
        raise LedgerError("回执例外原因不能为空")
    with LedgerLock(receipt_exception_path(path)) as lock:
        require_card(fold(read_events(path)), task)
        rounds = [name for name in delivery_rounds(Paths().deliver, task) if not name.startswith("-")]
        lock.append([{"时间": utc(), "任务": task, "原因": reason.strip(),
                      "轮次清单": rounds}], separate_tail=True)


def fold(events):
    """按追加顺序重放；备注不改变状态，恢复回到开卡状态。"""
    cards = {}
    for number, e in enumerate(events, 1):
        task, action = e["任务"], e["动作"]
        if action == "开卡":
            if task in cards:
                raise LedgerError("账本第 %d 行重复开卡：%s" % (number, task))
            cards[task] = dict(e, 状态="开卡", 事件=[], 原因="", 结论="", 摘要="")
            card = cards[task].get("卡片文件")
            if isinstance(card, str) and card.startswith("runs/"):
                relative = Path(card[5:])
                if relative.is_absolute() or ".." in relative.parts:
                    raise LedgerError("旧卡片路径含越界片段")
                cards[task]["卡片文件"] = str(Path(config.path_of("cards_dir")) / relative)
        elif task not in cards:
            raise LedgerError("账本第 %d 行任务未开卡：%s" % (number, task))
        c = cards[task]
        if action != "开卡":
            reopening = (action == "恢复" and c["状态"] in TERMINAL and
                         isinstance(e.get("轮次"), str) and
                         re.fullmatch(r"[A-Za-z0-9_-]+", e["轮次"]))
            if c["状态"] in TERMINAL and not reopening:
                raise LedgerError("账本第 %d 行操作了已结束的卡：%s" % (number, task))
            if action == "恢复" and c["状态"] != "卡住" and not reopening:
                raise LedgerError("账本第 %d 行恢复了未卡住的卡" % number)
            if action == "卡住" and c["状态"] == "卡住":
                raise LedgerError("账本第 %d 行重复卡住" % number)
            if action != "备注":
                c["状态"] = "开卡" if action == "恢复" else action
                c["原因"] = e.get("原因", "")
                c["结论"] = e.get("结论", "")
                c["摘要"] = e.get("摘要", "")
        c["事件"].append(e)
        c["更新"] = e["时间"]
    return cards


def used_ids(paths, events):
    """合并三处用过的号；子孙号也占用其根号，避免缺父卡时撞号。"""
    used = {e["任务"] for e in events}
    for directory in (paths.runs, paths.deliver):
        try:
            names = [p.name for p in directory.iterdir()]
        except FileNotFoundError:
            names = []
        # 权限错误不能假装目录为空，否则会分配已经用过的号。
        for name in names:
            used.update(m.group(1) for m in NAME_ID.finditer(name))
    return used


def allocate(source, parent, used, cards):
    """来源只认参数/环境；子孙继承父号，第三层和字母耗尽明确报错。"""
    if not source or not re.fullmatch(r"[A-Z]{2}", source):
        raise LedgerError("请用 --来源 或 AGENT_BRAIN 指定两个大写字母的来源")
    if parent:
        match = TASK_ID.fullmatch(parent)
        if not match:
            raise LedgerError("父任务号格式错误")
        if match[5]:
            raise LedgerError("最多允许子任务和孙任务两层，不能再往下拆")
        if parent not in used:
            raise LedgerError("父任务不存在：" + parent)
        if parent in cards and cards[parent]["状态"] in TERMINAL:
            raise LedgerError("父任务已结束：" + parent)
        # 来源标明实际派活大脑，编号前缀仍继承父任务，不改既定层级格式。
        if match[4]:
            nums = [int(m[5]) for item in used for m in [TASK_ID.fullmatch(item)]
                    if m and item.startswith(parent) and m[5]]
            return parent + str(max(nums, default=0) + 1)
        letters = {m[4] for item in used for m in [TASK_ID.fullmatch(item)]
                   if m and item.startswith(parent + "-") and m[4]}
        for letter in string.ascii_uppercase:
            if letter not in letters:
                return parent + "-" + letter
        raise LedgerError("子任务字母 A～Z 已用完")
    prefix = source + "-" + datetime.now().strftime("%Y%m%d") + "-"
    nums = [int(m[3]) for item in used for m in [TASK_ID.fullmatch(item)] if m and item.startswith(prefix)]
    return prefix + "%02d" % (max(nums, default=0) + 1)


def round_numbers(deliver, task):
    """只认实际 r数字 目录，按数值比较，r10 不会排在 r9 前面。"""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", task):
        raise LedgerError("任务号不合法")
    directory = Path(deliver) / task
    try:
        return sorted((int(p.name[1:]), p.name) for p in directory.iterdir()
                      if re.fullmatch(r"r[0-9]+", p.name) and p.is_dir())
    except FileNotFoundError:
        return []


def delivery_rounds(deliver, task):
    """日审轮次：保留数字目录规则，命名目录须有回执或运行信息。"""
    numeric = [name for _, name in round_numbers(deliver, task)]
    directory = Path(deliver) / task
    try:
        named = sorted(p.name for p in directory.iterdir()
                       if re.fullmatch(r"[A-Za-z0-9_-]+", p.name)
                       and not re.fullmatch(r"r[0-9]+", p.name) and p.is_dir()
                       and any(os.path.lexists(p / marker)
                               for marker in ("回执.json", "运行信息.json")))
    except FileNotFoundError:
        named = []
    return numeric + named


def occupied_round_numbers(deliver, task):
    """分配时统计所有占名节点，包含普通文件和断链；不改变日审目录语义。"""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", task):
        raise LedgerError("任务号不合法")
    try:
        return [int(p.name[1:]) for p in (Path(deliver) / task).iterdir()
                if re.fullmatch(r"r[0-9]+", p.name)]
    except FileNotFoundError:
        return []


def require_card(cards, task, active=False):
    """所有写操作只面向账本已开卡任务；读取命令仍能查询已结束卡。"""
    if task not in cards:
        raise LedgerError("任务不存在：" + task)
    card = cards[task]
    if active and card["状态"] in TERMINAL:
        raise LedgerError("任务已完成或放弃，不能再操作：" + task)
    return card


def claim_round(run):
    """宿主占用新轮时恢复已收卡任务；下一轮查询仍只读，不预先恢复。

    目录占用和账本写入共用账本锁。恢复先于建目录：中断最多留下待处理卡，
    不会留下已沿用旧验收的新轮。已有轮次在写账本之前拒绝。
    未登记的测试任务不凭空开卡。
    """
    from .isolation import validate_run
    paths = Paths()
    run = Path(run)
    validate_run(config.root_dir(), run)
    task, round_id = run.parts[-2:]
    with LedgerLock(paths.ledger) as lock:
        cards = fold(read_events(paths.ledger))
        if os.path.lexists(run):
            raise FileExistsError(str(run))
        card = cards.get(task)
        if card and card["状态"] in TERMINAL:
            lock.append([event("恢复", task, 轮次=round_id, 原因="派发新轮，清除旧轮验收")])
        run.parent.mkdir(parents=True, exist_ok=True)
        run.mkdir()


def audit(paths):
    """保留已有调用接口，证据核对实现集中到独立模块。"""
    from .audit import audit as check
    return check(paths)


def nonempty(value):
    """空标题、空原因等不算有效参数，交给 argparse 返回非零退出码。"""
    if not value.strip():
        raise argparse.ArgumentTypeError("不能为空")
    return value


def parser(prog="aw ledger"):
    """中文命令和英文别名共用参数定义，避免两套行为分叉。"""
    p = argparse.ArgumentParser(prog=prog, description="本地任务账本（只追加）")
    sub = p.add_subparsers(dest="command", required=True)
    definitions = [("开卡", "new"), ("卡住", "block"), ("恢复", "unblock"), ("完成", "done"),
                   ("放弃", "drop"), ("备注", "note"), ("查", "show"), ("列表", "list"),
                   ("下一轮", "next-round"), ("回执例外", "receipt-exception")]
    for chinese, english in definitions:
        s = sub.add_parser(chinese, aliases=[english])
        s.set_defaults(action=chinese)
        if chinese == "开卡":
            for name in ("项目", "标题"):
                s.add_argument("--" + name, required=True, type=nonempty)
            for name in ("来源", "父", "派活人", "卡片"):
                s.add_argument("--" + name, type=nonempty)
        elif chinese == "列表":
            s.add_argument("--项目", type=nonempty)
            s.add_argument("--在途", action="store_true")
        else:
            s.add_argument("任务", type=nonempty)
            if chinese in ("卡住", "放弃", "备注", "回执例外"):
                s.add_argument("文字", type=nonempty)
            if chinese == "完成":
                s.add_argument("--结论", required=True, choices=["通过", "未通过"])
                s.add_argument("--摘要", default="")
                s.add_argument("--回执例外", "--receipt-exception", dest="回执例外", type=nonempty)
    return p


def main(argv=None, prog="aw ledger"):
    """开卡 stdout 严格只输出任务号；诊断全部走 stderr。"""
    command_parser = parser(prog)
    args = command_parser.parse_args(argv)
    if args.action == "开卡":
        source = args.来源 if args.来源 is not None else os.environ.get("AGENT_BRAIN")
        if not isinstance(source, str) or not re.fullmatch(r"[A-Z]{2}", source):
            command_parser.error("请指定两个大写字母的来源（--来源 或 AGENT_BRAIN）")
    try:
        paths = Paths()
        action = args.action
        if action == "回执例外":
            append_receipt_exception(paths.ledger, args.任务, args.文字)
            return 0
        # 只读命令不创建锁或账本；轮次只是查询建议值，不预留目录。
        if action in ("查", "列表", "下一轮"):
            events = read_events(paths.ledger)
            cards = fold(events)
            if action == "下一轮":
                if args.任务 not in used_ids(paths, events):
                    raise LedgerError("任务不存在：" + args.任务)
                rounds = occupied_round_numbers(paths.deliver, args.任务)
                print("r%d" % (max(rounds, default=0) + 1))
            elif action == "查":
                card = require_card(cards, args.任务)
                print("%s  %s  [%s]  派活人：%s" % (args.任务, card["标题"], card["状态"], card["派活人"] or "未记录"))
                print("项目：%s  来源：%s  父任务：%s" % (card["项目"], card["来源"], card["父任务"] or "无"))
                # 人读历史先看动作和关键信息；完整原始迁移快照仍保留在账本及看板抽屉。
                for e in card["事件"]:
                    keys = {"开卡": ("标题", "卡片文件"), "卡住": ("原因",), "恢复": ("轮次", "原因"),
                            "完成": ("结论", "摘要"), "放弃": ("原因",), "备注": ("内容",)}[e["动作"]]
                    details = "；".join("%s：%s" % (k, e[k]) for k in keys if e.get(k))
                    print("%s  %s%s" % (e["时间"], e["动作"], "  " + details if details else ""))
                exceptions, warnings = read_receipt_exceptions(paths.ledger)
                for row in receipt_exceptions(card, exceptions):
                    print("%s  回执例外（来源：%s）：%s" % (row["时间"], row["来源"], row["原因"]))
                for warning in warnings:
                    print(warning, file=sys.stderr)
            else:
                print("任务号\t项目\t状态\t标题\t派活人\t原因/摘要")
                for key, card in sorted(cards.items(), key=lambda kv: kv[1]["时间"]):
                    if args.项目 is not None and card["项目"] != args.项目:
                        continue
                    if args.在途 and card["状态"] in TERMINAL:
                        continue
                    state = card["状态"] + ("（%s）" % card["结论"] if card["结论"] else "")
                    values = [key, card["项目"], state, card["标题"], card["派活人"], card["原因"] or card["摘要"]]
                    print("\t".join(v.replace("\n", "\\n").replace("\t", "\\t") for v in values))
            return 0
        with LedgerLock(paths.ledger) as lock:
            events = read_events(paths.ledger)
            cards = fold(events)
            if action == "开卡":
                source = args.来源 if args.来源 is not None else os.environ.get("AGENT_BRAIN")
                task = allocate(source, args.父, used_ids(paths, events), cards)
                fields = dict(标题=args.标题, 项目=args.项目, 来源=source, 派活人=args.派活人 or "", 父任务=args.父 or "")
                if args.卡片:
                    fields["卡片文件"] = args.卡片
                lock.append([event("开卡", task, **fields)])
                print(task)
            else:
                card = require_card(cards, args.任务, active=True)
                if action == "恢复" and card["状态"] != "卡住":
                    raise LedgerError("任务没有卡住，不能恢复")
                if action == "卡住" and card["状态"] == "卡住":
                    raise LedgerError("任务已经卡住；补充原因请用备注")
                fields = {}
                if action in ("卡住", "放弃"):
                    fields["原因"] = args.文字
                elif action == "备注":
                    fields["内容"] = args.文字
                elif action == "完成":
                    fields.update(结论=args.结论, 摘要=args.摘要)
                pending = []
                if action == "完成" and args.回执例外 is not None:
                    rounds = [name for name in delivery_rounds(paths.deliver, args.任务)
                              if not name.startswith("-")]
                    pending.append(event("备注", args.任务, 内容="回执例外：" + args.回执例外,
                                         轮次清单=rounds))
                pending.append(event(action, args.任务, **fields))
                lock.append(pending)
        return 0
    except (LedgerError, OSError, UnicodeError, ValueError) as exc:
        print("错误：%s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
