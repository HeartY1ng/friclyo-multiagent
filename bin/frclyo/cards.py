"""从内置模板新建任务卡；账本只读，已有文件绝不覆盖。"""
import argparse
import os
from pathlib import Path
import re
import sys
import tempfile

from . import config, ledger


# 模板只从当前框架读取；短名、文件名和英文名指向同一份内容。
TEMPLATE_DIR = Path(__file__).resolve().parents[2] / 'docs/协议/模板'
TEMPLATES = {'找bug': '找bug卡.md', '修复': '修复卡.md',
             '改动审查': '改动审查卡.md', '实验': '实验卡.md'}
ALIASES = {'bughunt': '找bug', 'fix': '修复', 'review': '改动审查', 'experiment': '实验'}
TOKENS = re.compile(r'<(任务号|轮次|项目|标题)>')


class CardError(Exception):
    """可直接展示的生成错误；不把底层文件内容带入诊断。"""


def template_name(value):
    # 接受展示给大脑的四个名字，不把用户参数当任意文件路径。
    name = value[:-3] if value.endswith('.md') else value
    name = name[:-1] if name.endswith('卡') else name
    name = ALIASES.get(name, name)
    if name not in TEMPLATES:
        raise argparse.ArgumentTypeError('模板须为：找bug、修复、改动审查、实验')
    return name


def task_id(value):
    # 与账本共用编号格式，父卡和子卡都可生成任务卡。
    if not ledger.TASK_ID.fullmatch(value):
        raise argparse.ArgumentTypeError('任务号格式无效；请使用账本已分配的编号')
    return value


def round_id(value):
    # 轮次直接参与文件名，只允许执行器已有的英文字母、数字和 -_。
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_-]*', value):
        raise argparse.ArgumentTypeError('轮次只能含英文字母、数字和 -_，不能为空或以 - 开头')
    return value


def single_line(value):
    # 标题与项目各占一行；拒绝空白或换行，避免生成结构不完整的卡。
    if not value.strip() or any(ord(c) < 32 or 127 <= ord(c) <= 159
                                or c in '\u2028\u2029' for c in value):
        raise argparse.ArgumentTypeError('标题和项目必须是非空单行文字')
    return value


def create(template, task, title=None, round_name='r1'):
    """先核对账本与模板，再一次性发布完整卡片，不改账本或预留执行轮次。"""
    template = template_name(template)
    task_id(task)
    round_id(round_name)
    # 公共配置负责数据根约束；账本模块负责事件校验与已开卡检查。
    cfg = config.load()
    entry = ledger.require_card(ledger.fold(ledger.read_events(cfg['ledger_file'])), task)
    values = {'任务号': task, '轮次': round_name, '项目': single_line(entry['项目']),
              '标题': single_line(entry['标题'] if title is None else title)}
    try:
        source = (TEMPLATE_DIR / TEMPLATES[template]).read_text(encoding='utf-8')
    except (OSError, UnicodeError) as exc:
        raise CardError('内置模板不可读；请检查框架模板文件是否完整') from exc
    if set(TOKENS.findall(source)) != set(values):
        raise CardError('内置模板缺少任务号、轮次、项目或标题占位符')
    # 单次替换避免标题中恰好出现的占位符被再次解释；其余待填空位原样保留。
    content = TOKENS.sub(lambda match: values[match[1]], source)
    directory = Path(cfg['cards_dir'])
    target = directory / (task + '-' + round_name + '.txt')
    directory.mkdir(parents=True, exist_ok=True)
    # 先写同目录临时文件，再用硬链接原子发布；并发创建、目录和断链都算已占名。
    fd, temporary = tempfile.mkstemp(prefix='.card-', dir=str(directory))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError as exc:
            raise CardError('目标文件已存在，拒绝覆盖：' + str(target)) from exc
    finally:
        Path(temporary).unlink()
    return target


def main(args=None):
    # 子命令自己解析中英文参数，aw 总入口只负责分派。
    parser = argparse.ArgumentParser(prog='aw card', description='从模板生成已开卡任务的待填写任务卡')
    commands = parser.add_subparsers(dest='command', required=True)
    new = commands.add_parser('new', aliases=['新建'], help='新建任务卡，不覆盖已有文件')
    new.add_argument('template', type=template_name, help='找bug / 修复 / 改动审查 / 实验')
    new.add_argument('task', type=task_id, help='账本中已开卡的任务号')
    new.add_argument('--title', '--标题', type=single_line, help='默认使用账本标题')
    new.add_argument('--round', '--轮', type=round_id, default='r1', dest='round_name')
    options = parser.parse_args(args)
    try:
        target = create(options.template, options.task, options.title, options.round_name)
    except (CardError, ledger.LedgerError) as exc:
        print('错误：' + str(exc), file=sys.stderr)
        return 1
    except (OSError, ValueError, argparse.ArgumentTypeError):
        # 运行失败统一返回 1；参数语法错误已由 argparse 返回 2，不显示底层数据。
        print('错误：任务卡生成失败；请检查配置、账本字段和目录访问权限', file=sys.stderr)
        return 1
    print(target)
    return 0
