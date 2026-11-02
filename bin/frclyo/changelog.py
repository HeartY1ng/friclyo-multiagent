"""改动台账：独立 flock 覆盖读号与追加，进程退出自动释放锁。"""
import argparse
import fcntl
import os
from pathlib import Path
import re

from . import config


def append(change, undo):
    # 拒绝 Markdown 分列/换行，避免生成多行或伪造编号。
    for value in (change, undo):
        if '|' in value or any(c in value for c in '\r\n\v\f\x85\u2028\u2029'):
            raise ValueError('改动和撤销不能含 | 或换行')
    path = config.root_dir() / '台账' / '改动台账.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    # 与 ledger.guard 相同的系统文件锁；独立锁名，不锁其他账本。
    with (path.parent / '.改动台账.lock').open('a+b') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            with path.open('a+', encoding='utf-8') as stream:
                stream.seek(0)
                content = stream.read()
                numbers = []
                for line_number, line in enumerate(content.splitlines(), 1):
                    # 编号后的分隔符也可能被截掉；不能把该编号当作尚未使用。
                    match = re.match(r'^\s*\|\s*#?(\d+)(?=\s|\||$)', line)
                    if match:
                        numbers.append(int(match.group(1)))
                        # 完整：编号后至少还有两栏并以 | 结尾（旧版台账是 4 栏，也算完整）；写到一半的行没有结尾的 |
                        if not re.fullmatch(r'\s*\|\s*#?\d+\s*\|(?:[^|]*\|){2,}\s*', line):
                            raise ValueError('改动台账第 %d 行（编号 %s）记录残缺；'
                                             '已停止追加，请补全原记录并保留编号后重试' %
                                             (line_number, match.group(1)))
                number = max(numbers, default=0) + 1
                prefix = '' if content.endswith('\n') else '\n'
                if not content:
                    prefix = '| 编号 | 改动 | 撤销 |\n|---|---|---|\n'
                stream.write(prefix + '| %d | %s | %s |\n' % (number, change, undo))
                stream.flush()
                os.fsync(stream.fileno())
                return number
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def main(args=None):
    parser = argparse.ArgumentParser(prog='aw changelog')
    commands = parser.add_subparsers(dest='command', required=True)
    command = commands.add_parser('append', aliases=['追加'])
    command.add_argument('--change', '--改动', required=True)
    command.add_argument('--undo', '--撤销', required=True)
    options = parser.parse_args(args)
    try:
        number = append(options.change, options.undo)
    except ValueError as exc:
        parser.error(str(exc))
    print(number)
    return 0
