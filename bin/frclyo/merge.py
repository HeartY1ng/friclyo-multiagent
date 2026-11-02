"""把一轮完整交付与出发基准比较，再合入开发版；不读取账本或凭据。"""
import argparse
from dataclasses import dataclass, field
import fnmatch
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import stat
import subprocess
import sys
import tempfile

from . import config


class MergeError(Exception):
    """可直接展示的操作错误；错误发生时不能声称合并完成。"""


@dataclass(frozen=True)
class File:
    """保存原字节和权限；回滚时还原时间，避免无谓改变构建输入。"""
    data: bytes
    mode: int
    atime: int = field(compare=False)
    mtime: int = field(compare=False)


@dataclass
class Plan:
    """先计算清单，再集中写入；冲突文件永远不进入写入清单。"""
    writes: dict = field(default_factory=dict)
    deletes: list = field(default_factory=list)
    adopted: list = field(default_factory=list)
    merged: list = field(default_factory=list)
    added: list = field(default_factory=list)
    conflicts: dict = field(default_factory=dict)
    retained: list = field(default_factory=list)
    outside: list = field(default_factory=list)
    mode_only: list = field(default_factory=list)
    mode_changed: list = field(default_factory=list)


def directory(value):
    """根路径也不跟随链接，防止把交付里的链接当成授权目标。"""
    path = Path(value).expanduser().absolute()
    for part in (path, *path.parents):
        if part.is_symlink():
            raise MergeError('目录路径含符号链接：' + str(path))
    if not path.is_dir():
        raise MergeError('需要已有目录：' + str(path))
    return path.resolve()


def read_file(path):
    """只接受普通文件；先检查类型，避免 FIFO 阻塞和链接越界。"""
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise MergeError('不支持链接或特殊文件：' + str(path))
    return File(path.read_bytes(), stat.S_IMODE(info.st_mode), info.st_atime_ns, info.st_mtime_ns)


def snapshot(root):
    """遍历工作树但排除 Git 元数据；空目录不属于逐文件合并范围。"""
    files, directories = {}, set()
    def visit(parent, prefix=''):
        with os.scandir(parent) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                if entry.name == '.git':
                    continue
                name = prefix + entry.name
                if entry.is_dir(follow_symlinks=False):
                    directories.add(name)
                    visit(Path(entry.path), name + '/')
                else:
                    files[name] = read_file(Path(entry.path))
    visit(root)
    return files, directories


def git_merge_bytes(current, base, worker, union=False, marker_size=7):
    """git 只处理临时文件；-p 把结果送回内存，绝不改开发版文件。"""
    parent = Path(tempfile.gettempdir()).resolve()
    with tempfile.TemporaryDirectory(prefix='aw-merge-', dir=parent) as temporary:
        work = Path(temporary)
        for name, data in (('current', current), ('base', base), ('worker', worker)):
            (work / name).write_bytes(data)
        command = ['git', 'merge-file', '-p', '--diff3', '--marker-size=%d' % marker_size,
                   '-L', '开发版', '-L', '基准', '-L', '工人交付']
        if union:
            command.append('--union')
        # 限时运行并指定临时 cwd，不依赖开发版是否有 Git 仓库。
        result = subprocess.run(command + ['current', 'base', 'worker'], cwd=work,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        if result.returncode < 0 or result.returncode >= 128:
            raise MergeError('git merge-file 失败：' + result.stderr.decode('utf-8', 'replace').strip())
        return result.stdout, result.returncode != 0


def merge_bytes(current, base, worker, union=False):
    """成功并集必须是重跑固定点；不能安全识别的改动留给人工处理。"""
    if not union:
        return git_merge_bytes(current, base, worker)
    data = union_bytes(current, base, worker)
    # 相邻删除等冲突的并集可能恢复旧行，重跑又删除这些行；不能成功后再变。
    if data != current and union_bytes(data, base, worker) != data:
        return current, True
    return data, False


def union_bytes(current, base, worker):
    """按冲突块识别已合入的完整工人块，不对全文件做逐行去重。"""
    # 标记长度避开三份输入中的所有标记状行，防止正文被误当成分隔符。
    size = 7
    lines = (current + b'\n' + base + b'\n' + worker).splitlines()
    while any(line.startswith(char * size) for line in lines
              for char in (b'<', b'|', b'=', b'>')):
        size += 1
    data, conflicted = git_merge_bytes(current, base, worker, marker_size=size)
    if not conflicted:
        return data
    start = b'<' * size + ' 开发版'.encode()
    middle = b'|' * size + ' 基准'.encode()
    separator = b'=' * size
    end = b'>' * size + ' 工人交付'.encode()
    result, ours, ancestor, theirs = [], [], [], []
    active = None
    for line in data.splitlines(keepends=True):
        marker = line.rstrip(b'\r\n')
        if marker == start and active is None:
            ours, ancestor, theirs = [], [], []
            active = ours
        elif marker == middle and active is ours:
            active = ancestor
        elif marker == separator and active is ancestor:
            active = theirs
        elif marker == end and active is theirs:
            # 只比较当前冲突位置的完整连续块，保留块内部有意重复的语句。
            present = any(ours[i:i + len(theirs)] == theirs
                          for i in range(len(ours) - len(theirs) + 1))
            if present:
                result.extend(ours)
            else:
                combined, failed = git_merge_bytes(b''.join(ours), b''.join(ancestor),
                                                   b''.join(theirs), union=True)
                if failed:
                    raise MergeError('并集冲突块无法合并；开发版未写入')
                result.append(combined)
            active = None
        elif active is not None:
            active.append(line)
        else:
            result.append(line)
    if active is not None:
        raise MergeError('并集冲突标记不完整；开发版未写入')
    return b''.join(result)


def conflict_excerpt(data):
    """只展示冲突块，三个标签分别标明开发版、基准和工人内容。"""
    blocks, active = [], False
    for line in data.decode('utf-8', 'replace').splitlines():
        if line.startswith('<<<<<<< '):
            active = True
        if active:
            blocks.append(line)
        if line.startswith('>>>>>>> '):
            active = False
    return '\n'.join(blocks) or '内容冲突，无法生成文本片段。'


def make_plan(base, worker, current, directories, unions, patterns, allow_delete,
              received=None):
    """按基准判断谁改了内容；删除与开发版改动相撞时仍算冲突。"""
    plan = Plan()
    received = received or {}
    for name in sorted(base.keys() | worker.keys()):
        before, incoming, existing = base.get(name), worker.get(name), current.get(name)
        supplied = received.get(name)
        mode_changed = supplied is not None and incoming is not None and supplied.mode != incoming.mode
        if mode_changed:
            if before is not None and before.data == incoming.data:
                plan.mode_only.append(name)
            else:
                plan.mode_changed.append(name)
        content_unchanged = (before is None and incoming is None or
                             before is not None and incoming is not None and before.data == incoming.data)
        if content_unchanged and not mode_changed:
            continue
        if patterns and not any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns):
            plan.outside.append(name)
        # 现有权限属于开发版，工人只改 chmod 时提醒但不采用。
        if content_unchanged:
            continue
        if incoming is None:
            if not allow_delete:
                plan.retained.append(name)
            elif existing is not None and existing != before:
                plan.conflicts[name] = '工人删除，但开发版修改了内容或权限；保留开发版。'
            elif name in directories:
                plan.conflicts[name] = '工人删除，但开发版已改为目录；保留开发版。'
            elif existing is not None:
                plan.deletes.append(name)
            continue
        # 文件/目录互换不能通过覆盖解决，列为冲突供人工选择。
        if name in directories or any(str(p) in current for p in PurePosixPath(name).parents):
            plan.conflicts[name] = '文件与目录路径冲突；保留开发版。'
            continue
        if existing is None:
            if before is not None:
                plan.conflicts[name] = '开发版删除，但工人修改；保留开发版的删除。'
            else:
                plan.added.append(name)
                plan.writes[name] = incoming
            continue
        if existing.data == incoming.data:
            continue
        if before is not None and existing.data == before.data:
            plan.adopted.append(name)
            data = incoming.data
        else:
            # 二进制可以单边采用，但不能交给按行工作的 merge-file。
            ancestor = before.data if before is not None else b''
            if any(b'\0' in value for value in (existing.data, ancestor, incoming.data)):
                plan.conflicts[name] = '二进制文件两边都改了；保留开发版。'
                continue
            use_union = name in unions
            data, conflicted = merge_bytes(existing.data, ancestor, incoming.data, use_union)
            if use_union and script_language(name, data) is None:
                raise MergeError('并集需要可检查的 .py 或 .sh 文件，或带 Python/Shell shebang 的无扩展名脚本：' + name)
            if conflicted:
                plan.conflicts[name] = ('并集重跑结果不稳定，无法安全识别已合入改动；保留开发版，请人工处理。'
                                        if use_union else conflict_excerpt(data))
                continue
            if data == existing.data:
                continue
            plan.merged.append(name)
        plan.writes[name] = File(data, existing.mode, existing.atime, existing.mtime)
    # 文件/目录互换若有冲突，关联的删除也保留，不能只执行转换的一半。
    for name in list(plan.deletes):
        if any(name.startswith(conflict + '/') or conflict.startswith(name + '/')
               for conflict in plan.conflicts):
            plan.deletes.remove(name)
            plan.conflicts[name] = '关联的文件/目录转换有冲突；保留原文件。'
    return plan


def python_launcher(data):
    """识别 bin/aw 的 bash/Python 包装，返回还须单独检查的 bash 前缀。"""
    if not data.startswith(b'#!/bin/bash\n""":"\n'):
        return None
    end = data.find(b'\n":\"\"\"\n')
    # 用双语言字符串边界识别包装，不把启动命令及其参数写死。
    if end >= 0:
        return data[:end + len(b'\n":\"\"\"\n')]
    return None


def script_language(name, data):
    """保留扩展名规则；无扩展名按首行识别，不执行 shebang 指定的程序。"""
    suffix = Path(name).suffix
    if suffix in ('.py', '.sh'):
        return 'python' if suffix == '.py' else 'shell'
    if suffix:
        return None
    first = data.partition(b'\n')[0]
    if not first.startswith(b'#!'):
        return None
    try:
        words = shlex.split(first[2:].decode('utf-8'))
    except (UnicodeError, ValueError):
        return None
    if not words:
        return None
    interpreter = Path(words[0]).name
    if interpreter == 'env':
        words = words[1:]
        if words and words[0] == '-S':
            words = words[1:]
        if not words:
            return None
        interpreter = words[0]
    if re.fullmatch(r'python(?:3(?:\.\d+)?)?', interpreter):
        return 'python'
    if interpreter in ('bash', 'sh'):
        # bin/aw 的双语言入口先由 bash exec Python；余下正文须按 Python 检查。
        if python_launcher(data) is not None:
            return 'python'
        return 'shell'
    return None


def check_syntax(name, data):
    """只编译语法，不导入或执行交付代码，也不生成 __pycache__。"""
    language = script_language(name, data)
    if language == 'python':
        try:
            compile(data, name, 'exec', dont_inherit=True)
        except (SyntaxError, ValueError) as exc:
            line = getattr(exc, 'lineno', None)
            raise MergeError('Python 语法错误：%s（行 %s）' % (name, line or '?')) from exc
    launcher = python_launcher(data) if language == 'python' else None
    if language == 'shell' or launcher is not None:
        # bash -n 从 stdin 读脚本；清除启动文件变量，检查过程不能执行用户脚本。
        env = dict(os.environ)
        env.pop('BASH_ENV', None)
        env.pop('ENV', None)
        result = subprocess.run(['bash', '--noprofile', '--norc', '-n'],
                                input=launcher if launcher is not None else data,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=30)
        if result.returncode:
            raise MergeError('Shell 语法错误：' + name + '\n' + result.stderr.decode('utf-8', 'replace'))


def atomic_write(path, item):
    """在目标同目录写临时文件，设置原权限并落盘，再原子替换。"""
    fd, temporary = tempfile.mkstemp(prefix='.aw-merge-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(item.data)
            stream.flush()
            os.fchmod(stream.fileno(), item.mode)
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def apply_plan(root, current, plan):
    """整个批次保留备份；写入、语法检查或权限检查失败均尝试全部回滚。"""
    touched, created = [], []
    targets = list(plan.writes) + plan.deletes
    try:
        for name in targets:
            path = root / name
            # 写之前再核对一次，避免覆盖计划形成后已经改动的文件。
            for parent in path.parents:
                if parent == root:
                    break
                if parent.is_symlink():
                    raise MergeError('目标父目录变成了链接：' + name)
            actual = read_file(path) if os.path.lexists(path) else None
            if actual != current.get(name):
                raise MergeError('开发版在计划后发生变化：' + name)
            if name in plan.deletes:
                touched.append(name)
                path.unlink()
                continue
            missing, parent = [], path.parent
            while not parent.exists():
                missing.append(parent)
                parent = parent.parent
            for parent in reversed(missing):
                parent.mkdir()
                created.append(parent)
            touched.append(name)
            atomic_write(path, plan.writes[name])
        # 按契约在写入后检查实际文件；失败时撤销包含删除在内的全部改动。
        for name, expected in plan.writes.items():
            actual = read_file(root / name)
            check_syntax(name, actual.data)
            if actual.mode != expected.mode:
                raise MergeError('权限变化不为 0：' + name)
        return 0
    except BaseException as original:
        failures = []
        for name in reversed(touched):
            try:
                path = root / name
                if name in current:
                    atomic_write(path, current[name])
                    os.utime(path, ns=(current[name].atime, current[name].mtime))
                elif os.path.lexists(path):
                    path.unlink()
            except OSError:
                failures.append(name)
        for parent in reversed(created):
            try:
                parent.rmdir()
            except OSError:
                failures.append(str(parent.relative_to(root)) + '/')
        if failures:
            raise MergeError('回滚未完成，需要人工处理：' + '、'.join(failures)) from original
        print('本次合并已整体回滚（内容、文件权限和新增目录）。', file=sys.stderr)
        raise


def report(plan, dry_run):
    """先打印可审查计划；范围提醒不阻止用户明确要求的合并。"""
    print('试跑计划（开发版未写入）' if dry_run else '合并计划')
    for label, names in (('直接采用', plan.adopted), ('三方合并', plan.merged),
                         ('新增', plan.added), ('删除', plan.deletes),
                         ('工人删除，默认保留', plan.retained),
                         ('仅工人权限改变，保留开发版权限', plan.mode_only),
                         ('工人权限改变，保留开发版权限', plan.mode_changed),
                         ('范围外改动提醒（--只许）', plan.outside)):
        for name in names:
            print('%s：%s' % (label, name))
    for name, reason in plan.conflicts.items():
        print('冲突（不写入）：' + name + '\n' + reason)
    print('计划：采用 %d，三方合并 %d，冲突 %d，新增 %d，删除 %d；权限变化预计 0' %
          (len(plan.adopted), len(plan.merged), len(plan.conflicts), len(plan.added), len(plan.deletes)))
    print('工人改了 %d 个文件的权限' % (len(plan.mode_only) + len(plan.mode_changed)))


def main(args=None):
    """中英文共用参数；数据根只用于定位素材与交付，不读取业务配置。"""
    parser = argparse.ArgumentParser(prog='aw merge', description='三方合并一轮工人交付到开发版')
    parser.add_argument('task', help='任务编号')
    parser.add_argument('round', help='轮次，例如 r1')
    parser.add_argument('--到', '--to', dest='destination', required=True)
    parser.add_argument('--基准', '--base', dest='base')
    parser.add_argument('--试跑', '--dry-run', dest='dry_run', action='store_true')
    parser.add_argument('--并集', '--union', dest='unions', action='append', default=[], metavar='文件')
    parser.add_argument('--允许删除', '--allow-delete', dest='allow_delete', action='store_true')
    parser.add_argument('--只许', '--only', dest='patterns', action='append', default=[], metavar='通配')
    options = parser.parse_args(args)
    for name, value in (('任务', options.task), ('轮次', options.round)):
        if not re.fullmatch(r'[A-Za-z0-9_-]+', value):
            parser.error(name + '必须是单个编号，不能含路径分隔符')
    for name in options.unions:
        path = PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts or str(path) != name or name == '.':
            parser.error('--并集需要框架内的规范相对文件路径')
    try:
        root = config.root_dir()
        base_root = directory(options.base or root / '素材' / options.task / '框架')
        worker_root = directory(root / '交付' / options.task / options.round / '交付/框架')
        received_path = root / '交付' / options.task / options.round / '输入/框架'
        if not os.path.lexists(received_path):
            raise MergeError('需要输入：缺少工人收到的权限基准目录：' + str(received_path))
        received_root = directory(received_path)
        destination = directory(options.destination)
        # 三棵树必须独立，否则写目标会同时改变输入，使回滚语义失效。
        roots = (base_root, worker_root, destination)
        if any(a != b and (a.is_relative_to(b) or b.is_relative_to(a))
               for a in roots for b in roots) or len(set(roots)) != 3:
            raise MergeError('基准、交付和开发版目录不能相同或互相包含')
        if destination == received_root or destination.is_relative_to(received_root) or received_root.is_relative_to(destination):
            raise MergeError('工人输入和开发版目录不能相同或互相包含')
        base, _ = snapshot(base_root)
        worker, _ = snapshot(worker_root)
        current, directories = snapshot(destination)
        received, _ = snapshot(received_root)
        for name in options.unions:
            if name not in worker:
                parser.error('--并集文件不在工人交付中：' + name)
        plan = make_plan(base, worker, current, directories, set(options.unions),
                         options.patterns, options.allow_delete, received)
        report(plan, options.dry_run)
        if options.dry_run:
            for name, item in plan.writes.items():
                check_syntax(name, item.data)
            print('试跑语法检查通过；开发版未写入；权限变化 0。')
        else:
            changes = apply_plan(destination, current, plan)
            print('已应用无冲突文件；语法检查通过；权限变化 %d。' % changes)
        return 1 if plan.conflicts else 0
    except (MergeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print('合并失败：' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
