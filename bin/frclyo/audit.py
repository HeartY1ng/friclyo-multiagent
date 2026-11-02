"""只读核对账本的完成声明与轮次证据。"""
import hashlib
import json
import re
import sys
import time
from datetime import datetime
from .execution import result_limit, safe_read
from .ledger import TERMINAL, LedgerError, Paths, fold, read_events, delivery_rounds
from .ledger import read_receipt_exceptions, receipt_exceptions


class ReceiptEvidenceError(LedgerError):
    """只有回执/交付证据错误可由回执例外豁免。"""


def _check_round(run_dir, key, rnd, label="最新一轮"):
    try:
        _read_round(run_dir, key, rnd, label)
    except (LedgerError, OSError, ValueError, UnicodeError) as exc:
        raise ReceiptEvidenceError(str(exc)) from exc


def _read_round(run_dir, key, rnd, label):
    """所有轮次共用身份、状态与结果校验规则。"""
    receipt_path = run_dir / "回执.json"
    try:
        receipt = json.loads(safe_read(receipt_path).decode("utf-8"))
    except FileNotFoundError:
        raise LedgerError("%s %s 没有回执" % (label, rnd))
    if not isinstance(receipt, dict):
        raise LedgerError("回执不是 JSON 对象")
    # 与 aw.verify_receipt 一致：缺字段也不构成本任务、本轮的证据。
    for field, expected in (("任务编号", key), ("轮次编号", rnd)):
        if receipt.get(field) != expected:
            raise LedgerError("回执%s不匹配：期望「%s」，实际「%s」" %
                              (field, expected, receipt.get(field)))
    try:
        result = safe_read(run_dir / "结果.txt", result_limit())
    except FileNotFoundError:
        raise LedgerError("%s没有结果.txt，无法核对校验码" % label)
    sha = hashlib.sha256(result).hexdigest()
    same = sha == receipt.get("结果sha256")
    if receipt.get("状态") != "执行完成" or not same:
        raise LedgerError("回执状态是「%s」、校验码%s" %
                          (receipt.get("状态"), "一致" if same else "不一致"))


def _round_start(run_dir):
    """读取生产写入的 UTC 开始时间，不依赖复制时会变化的目录时间。"""
    stamps = []
    for name in ("运行信息.json", "回执.json"):
        try:
            row = json.loads(safe_read(run_dir / name).decode("utf-8"))
            stamp = row.get("开始") if isinstance(row, dict) else None
            if isinstance(stamp, str):
                parsed = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")
                stamps.append(parsed.strftime("%Y-%m-%dT%H:%M:%SZ"))
        except (OSError, ValueError, UnicodeError, RecursionError):
            continue
    # 两份记录不一致时取较晚值，不能借较早记录扩大豁免。
    return max(stamps) if stamps else None


def _receipt_exception(card, records=(), run_dir=None, rnd=None):
    """逐轮选登记时已有轮次的例外；缺少轮次时仍使用当前验收周期。"""
    events = card["事件"]
    stamp = _round_start(run_dir) if run_dir is not None else None
    # 恢复事件提供新轮的独立边界，也兼容没有“开始”的旧回执。
    last = next((i for i in range(len(events) - 1, -1, -1)
                 if events[i]["动作"] == "恢复"
                 and (rnd is None or events[i].get("轮次") == rnd
                      or stamp is not None and events[i]["时间"] <= stamp)), None)
    if last is not None:
        boundary = events[last]["时间"]
        # 清单是登记时的覆盖范围；旧记录无法证明同秒先后，须严格晚于恢复。
        records = [row for row in records if "轮次清单" in row or row["时间"] > boundary]
        events = [event for i, event in enumerate(events) if "轮次清单" in event
                  or i > last and event["时间"] > boundary]
    rows = receipt_exceptions(dict(card, 事件=events), records)
    def covers(row):
        if "轮次清单" in row:
            names = row["轮次清单"]
            if not isinstance(names, list) or any(
                    not isinstance(name, str)
                    or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]*", name)
                    for name in names):
                return False
            # 无轮次时登记的例外仍可说明“没有交付”，但不能豁免后来创建的轮次。
            return rnd in names if rnd is not None else not names
        return stamp is None or row["时间"] > stamp
    rows = [row for row in rows if covers(row)]
    # 无开始时间、也未被恢复事件标为新轮的旧记录沿用历史例外。
    return rows[-1]["原因"] if rows else ""


def _check_paths(paths):
    """核对通过卡；命名轮次逐轮检查，已说明例外仍列出但不计问题。"""
    cards = fold(read_events(paths.ledger))
    exceptions, warnings = read_receipt_exceptions(paths.ledger)
    children = {}
    for key, c in cards.items():
        parent = c.get("父任务")
        if parent:
            children.setdefault(parent, []).append(key)
    targets = {key: (c["标题"], c["时间"]) for key, c in cards.items()
               if c["状态"] == "完成" and c["结论"] == "通过"}
    lines, problems = list(warnings), []
    for key, (title, _stamp) in sorted(targets.items(), key=lambda kv: kv[1][1]):
        try:
            rounds = delivery_rounds(paths.deliver, key)
            if not rounds:
                kids = children.get(key, [])
                if kids:
                    unfinished = ["%s（%s）" % (k, cards[k]["状态"]) for k in kids
                                  if cards[k]["状态"] not in TERMINAL]
                    if unfinished:
                        raise LedgerError("总卡，子卡尚未收卡：%s" % "、".join(unfinished))
                    lines.append("✅ %s「%s」：总卡，证据在子卡 %s" %
                                 (key, title, "、".join(kids)))
                    continue
                raise ReceiptEvidenceError("没有交付轮次，也没有回执")
            named = any(not re.fullmatch(r"r[0-9]+", rnd) for rnd in rounds)
            checked = rounds if named else rounds[-1:]
            failures, exempted = [], {}
            for rnd in checked:
                run_dir = paths.deliver / key / rnd
                try:
                    _check_round(run_dir, key, rnd, "轮次" if named else "最新一轮")
                except ReceiptEvidenceError as exc:
                    reason = _receipt_exception(cards[key], exceptions, run_dir, rnd)
                    failure = "%s（%s）" % (rnd, exc)
                    if reason:
                        exempted.setdefault(reason, []).append(failure)
                    else:
                        failures.append(failure)
            def summary(items):
                return "共 %d 轮，其中 %d 轮有问题：%s%s" % (
                    len(rounds), len(items), "、".join(items[:5]), "等" if len(items) > 5 else "")
            for reason, items in exempted.items():
                lines.append("⚠️ 回执例外已说明：%s（%s「%s」；%s）" %
                             (reason, key, title, summary(items)))
            if failures:
                # 已逐轮判断，不能让外层再用整卡例外豁免剩余的新轮问题。
                raise LedgerError(summary(failures))
            if exempted:
                continue
            success = ("共 %d 轮，回执和校验码都对得上" % len(rounds) if named else
                       "最新一轮 %s 回执和校验码都对得上" % rounds[-1])
        except (LedgerError, OSError, ValueError, UnicodeError) as exc:
            reason = _receipt_exception(cards[key], exceptions) if isinstance(exc, ReceiptEvidenceError) else ""
            if reason:
                lines.append("⚠️ 回执例外已说明：%s（%s「%s」；%s）" % (reason, key, title, exc))
            else:
                lines.append("❌ %s「%s」：%s" % (key, title, exc))
                problems.append(lines[-1])
        else:
            lines.append("✅ %s「%s」：%s" % (key, title, success))
    return lines, problems, len(targets)



def audit(paths):
    """保留账本 CLI 原报告接口，公共结构化结果由 check 提供。"""
    lines, problems, count = _check_paths(paths)
    lines.append("已核对完成卡 %d 张，问题 %d 个" % (count, len(problems)))
    return lines, len(problems)


def check():
    """只读日审；损坏的配置或账本作为问题返回，不能伪装成健康。"""
    report = {"时间": time.time(), "核对卡数": 0, "问题": [], "提醒": []}
    try:
        lines, report["问题"], report["核对卡数"] = _check_paths(Paths())
        report["提醒"] = [line for line in lines if line.startswith("⚠️")]
    except (LedgerError, OSError, ValueError, UnicodeError) as exc:
        report["问题"] = ["自述与证据核对失败：%s" % exc]
    return report


def main():
    """命令行与进程内返回同一 JSON 结构；发现问题时退出非零。"""
    report = check()
    print(json.dumps(report, ensure_ascii=False))
    return 1 if report["问题"] else 0


if __name__ == "__main__":
    sys.exit(main())
