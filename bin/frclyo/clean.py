"""只清理已收卡且有可信退出证据的私有轮次；交付仅作只读索引。"""
import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import sys
import time

from . import changelog, config, isolation, ledger, release
from .execution import directory, safe_read, atomic_write


class Unsafe(ValueError):
    pass


def identity(info):
    return info.st_dev, info.st_ino, info.st_mode


def read_json(path):
    value = json.loads(safe_read(path))
    if not isinstance(value, dict):
        raise Unsafe('证据不是对象')
    return value


def closed_cards(root, days, now):
    path = root / '账本.jsonl'
    # 先逐段拒绝链接，再交给现有账本校验器；只读，不创建账本或锁。
    safe_read(path, 64 * 1024 * 1024)
    events = ledger.read_events(path)
    # 清理已有明确数据根：先在内存转换旧路径，避免 fold 再解析业务配置。
    # 保留旧格式的越界校验；不改写历史账本，也不改变终态校验。
    normalized = []
    for event in events:
        card = event.get('卡片文件')
        if event['动作'] == '开卡' and isinstance(card, str) and card.startswith('runs/'):
            relative = Path(card[5:])
            if relative.is_absolute() or '..' in relative.parts:
                raise ledger.LedgerError('旧卡片路径含越界片段')
            event = dict(event, 卡片文件=str(root / '任务卡' / relative))
        normalized.append(event)
    cards = ledger.fold(normalized)
    result = {}
    for task, card in cards.items():
        if not ledger.TASK_ID.fullmatch(task) or card['状态'] not in ledger.TERMINAL:
            continue
        stamp = datetime.strptime(card['更新'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc).timestamp()
        if now - stamp > days * 86400:
            result[task] = stamp
    return result


def names(path):
    try:
        with directory(path) as fd:
            return sorted(name for name in os.listdir(fd)
                          if stat.S_ISDIR(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode))
    except FileNotFoundError:
        return []


def dead_pid(value):
    if type(value) is not int or value <= 1:
        return False
    try:
        os.kill(value, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def retry_path(control):
    return control.with_name('.' + control.name + '.clean.json')


def exit_evidence(control):
    """控制目录外的删除凭据只由清理器写入，绑定原目录身份以便断点重试。"""
    try:
        proof = read_json(retry_path(control))
    except FileNotFoundError:
        return read_json(control / 'status.json'), read_json(control / 'receipt.json')
    try:
        with directory(control.parent) as parent:
            current = os.stat(control.name, dir_fd=parent, follow_symlinks=False)
        if list(identity(current)) != proof.get('identity'):
            raise Unsafe('重试目录身份已变化')
    except FileNotFoundError:
        pass
    if not isinstance(proof.get('status'), dict) or not isinstance(proof.get('receipt'), dict):
        raise Unsafe('重试退出证据无效')
    return proof['status'], proof['receipt']


def prepare_retry(control, expected):
    """先持久化完整退出证据；写入失败则不开始删除控制目录。"""
    marker = retry_path(control)
    status, receipt = exit_evidence(control)
    data = json.dumps(dict(identity=expected, status=status, receipt=receipt)).encode()
    try:
        safe_read(marker)
    except FileNotFoundError:
        # 同一清理锁内原子发布；中断不能留下截断的重试凭据。
        atomic_write(marker, data)
    return marker


def round_paths(root, task, round_id, closed):
    """只从固定布局推导路径；不采用回执或运行信息内提供的路径。"""
    run = root / '交付' / task / round_id
    info = read_json(run / '运行信息.json')
    worker = info.get('工人')
    if worker not in ('codex', 'opencode') or not dead_pid(info.get('进程号')):
        raise Unsafe('工人仍在运行或无法确认进程已退出')
    control = root / '运行时/控制' / worker / task / round_id
    status, receipt = exit_evidence(control)
    if status.get('group_reaped') is not True or receipt.get('进程组已回收') is not True:
        raise Unsafe('缺少可信进程组回收证据')
    if (receipt.get('任务编号'), receipt.get('轮次编号'), receipt.get('工人')) != (task, round_id, worker):
        raise Unsafe('可信回执身份不匹配')
    end = datetime.strptime(receipt['结束'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc).timestamp()
    if end > closed:
        raise Unsafe('轮次结束晚于收卡时间')
    runtime = root / '运行时/opencode'
    if worker == 'opencode':
        paths = [(runtime / 'rounds' / task / round_id, 'dir'),
                 (runtime / ('worker-' + task + '.' + round_id + '.json'), 'file'),
                 (runtime / ('worker-' + task + '-' + round_id + '.json'), 'file'),
                 (isolation.round_tmp(root, run), 'dir')]
    else:
        paths = [(isolation.codex_tmp(root, run), 'dir')]
    # 控制证据最后删除，前面部分失败时仍可重试；不删除共享父目录。
    result = paths + [(control, 'dir')]
    if retry_path(control).exists() or retry_path(control).is_symlink():
        result.append((retry_path(control), 'file'))
    return result


def measure(fd, device):
    """按磁盘分配块计字节；链接只计算自身，绝不跟随，拒绝跨设备目录。"""
    total = os.fstat(fd).st_blocks * 512
    for name in os.listdir(fd):
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if info.st_dev != device:
            raise Unsafe('跨设备节点，保留')
        if stat.S_ISDIR(info.st_mode):
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                if identity(os.fstat(child)) != identity(info):
                    raise Unsafe('扫描时目录已变化')
                total += measure(child, device)
            finally:
                os.close(child)
        else:
            total += info.st_blocks * 512
    return total


def inspect(path, kind):
    with directory(path.parent) as parent:
        info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        if info.st_uid != os.getuid():
            raise Unsafe('节点不属于当前用户')
        if kind == 'file':
            if not stat.S_ISREG(info.st_mode):
                raise Unsafe('模板不是普通文件')
            return identity(info), info.st_blocks * 512
        if not stat.S_ISDIR(info.st_mode):
            raise Unsafe('候选不是普通目录')
        child = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            if identity(os.fstat(child)) != identity(info):
                raise Unsafe('候选已变化')
            return identity(info), measure(child, info.st_dev)
        finally:
            os.close(child)


def template_references(root):
    # 旧模板使用 task-round 拼接，合法编号也可能重名；包括未登记/其他工人轮次。
    references = {}
    for task in names(root / '交付'):
        if not re.fullmatch(r'[A-Za-z0-9_-]+', task):
            continue
        for round_id in names(root / '交付' / task):
            if re.fullmatch(r'[A-Za-z0-9_-]+', round_id):
                for separator in ('.', '-'):
                    name = 'worker-' + task + separator + round_id + '.json'
                    references.setdefault(name, set()).add((task, round_id))
    return references


def plan(root, days, now=None):
    rows = []
    closed = closed_cards(root, days, time.time() if now is None else now)
    references = template_references(root)
    for task, stamp in sorted(closed.items()):
        for round_id in names(root / '交付' / task):
            if not re.fullmatch(r'[A-Za-z0-9_-]+', round_id):
                continue
            try:
                paths = round_paths(root, task, round_id, stamp)
                group = []
                for path, kind in paths:
                    try:
                        node, size = inspect(path, kind)
                    except FileNotFoundError:
                        continue
                    if kind == 'file' and path.name.startswith('worker-') and references.get(path.name) != {(task, round_id)}:
                        raise Unsafe('模板有其他轮次引用')
                    group.append(dict(task=task, round=round_id, path=path, kind=kind,
                                      identity=node, bytes=size, closed=stamp))
                rows.extend(group)
            except (OSError, ValueError, KeyError, TypeError):
                print('保留 %s/%s：运行证据或路径不满足清理条件' % (task, round_id), file=sys.stderr)
    return rows


def remove_at(parent, name, expected, device):
    """逐级 NOFOLLOW + dir_fd 删除；叶链接仅 unlink，不解析目标。"""
    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if identity(info) != expected or info.st_dev != device:
        raise Unsafe('删除前节点已变化')
    if stat.S_ISDIR(info.st_mode):
        child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            if identity(os.fstat(child)) != expected:
                raise Unsafe('删除时目录已变化')
            for entry in sorted(os.listdir(child), key=lambda item: (item in ('status.json', 'receipt.json'), item)):
                sub = os.stat(entry, dir_fd=child, follow_symlinks=False)
                remove_at(child, entry, identity(sub), device)
            if identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != expected:
                raise Unsafe('删除时目录被替换')
            os.rmdir(name, dir_fd=parent)
        finally:
            os.close(child)
    else:
        os.unlink(name, dir_fd=parent)


def record(change):
    # 与 aw 台账 追加 完全相同的命令处理器；不自行编辑台账格式或分配编号。
    return changelog.main(['追加', '--改动', change, '--撤销', '不可自动撤销；从清理前备份恢复'])


def delete(root, days, rows):
    # 防并发清理；只在实际删除模式建立锁。启动器不复用已存在的交付轮次。
    with directory(root / '运行时') as parent:
        fd = os.open('.清理.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise Unsafe('清理锁不是普通文件')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            current = {str(row['path']): row for row in plan(root, days)}
            selected = [row for row in rows if str(row['path']) in current
                        and row['identity'] == current[str(row['path'])]['identity']]
            if len(selected) != len(rows):
                raise Unsafe('候选已变化，请重新预览；本次未删除')
            # 先记意图：台账写入失败时不删，进程中断也留下追踪线索。
            label = json.dumps([str(row['path']) for row in rows], ensure_ascii=True).replace('|', '\\u007c')
            record('清理开始（不代表已删除）：' + label)
            deleted = []
            try:
                for row in rows:
                    # 每个节点删除前再次核对退出证据和终结状态。
                    closed = closed_cards(root, days, time.time())
                    if row['task'] not in closed:
                        raise Unsafe('任务状态已变化')
                    round_paths(root, row['task'], row['round'], closed[row['task']])
                    path = row['path']
                    # 控制树最后处理；外部凭据留到整棵树删除后。
                    is_control = (row['kind'] == 'dir' and path.parent.parent.parent == root / '运行时/控制')
                    marker = prepare_retry(path, row['identity']) if is_control else None
                    with directory(path.parent) as parent_fd:
                        remove_at(parent_fd, path.name, row['identity'], row['identity'][0])
                    # 已进入本次计划的重试凭据由其自己的行删除。
                    if marker is not None and not any(other['path'] == marker for other in rows):
                        with directory(marker.parent) as marker_parent:
                            os.unlink(marker.name, dir_fd=marker_parent)
                    deleted.append(str(path))
                    print('已删除：' + str(path))
            finally:
                detail = json.dumps(deleted, ensure_ascii=True).replace('|', '\\u007c')
                record('清理结束；完整删除 %d/%d 个节点：%s；未完成项可能部分删除' %
                       (len(deleted), len(rows), detail))
        finally:
            os.close(fd)


def uninstall_external(root):
    """删除数据根之前，只清理索引所指的根外节点；失败保留索引供重试。"""
    candidates = set()
    for task in names(root / '交付'):
        for round_id in names(root / '交付' / task):
            run = root / '交付' / task / round_id
            # 同时检查两种固定布局；不信任运行信息中的任意路径。
            for path in (isolation.codex_tmp(root, run), isolation.round_tmp(root, run)):
                if path.is_relative_to(root):
                    continue
                try:
                    path.lstat()
                except FileNotFoundError:
                    continue
                except OSError:
                    print('无法检查根外临时目录：' + str(path), file=sys.stderr)
                    raise
                candidates.add(path)
    if not candidates:
        return
    try:
        rows = [row for row in plan(root, 0) if row['path'] in candidates]
        eligible = {row['path'] for row in rows}
        for path in sorted(candidates - eligible):
            print('未清理（缺少安全退出或归属证据）：' + str(path), file=sys.stderr)
        if candidates != eligible:
            raise Unsafe('根外临时目录尚不满足清理条件')
        # 只删根外节点，保留控制证据直到数据根最终删除，支持部分失败后重试。
        delete(root, 0, rows)
    except (OSError, ValueError, KeyError, TypeError, ledger.LedgerError) as exc:
        for path in sorted(candidates):
            print('需核对清理结果（索引已保留）：' + str(path), file=sys.stderr)
        raise Unsafe('根外清理未完成') from exc


def main(args=None):
    parser = argparse.ArgumentParser(prog='aw clean', description='只清已收卡且确认退出的私有轮次；默认预览')
    parser.add_argument('--days', '--天数', type=int, default=7)
    parser.add_argument('--yes', action='store_true', help='请求删除，仍须输入根目录确认')
    options = parser.parse_args(args)
    if options.days < 0:
        parser.error('天数不能小于 0')
    try:
        root = config.root_dir()
        # 版本预览和轮次清理共用一次确认；没有账本时仍能预览旧版本。
        rows = plan(root, options.days) if (root / "账本.jsonl").exists() else []
        versions = release.cleanup_preview(root)
        for row in rows:
            print('%s/%s\t%d 字节\t%s' % (row['task'], row['round'], row['bytes'], row['path']))
        print('共 %d 个节点，占用 %d 字节（按分配块统计）' % (len(rows), sum(row['bytes'] for row in rows)))
        if not options.yes:
            return 0
        try:
            answer = input('删除不可自动撤销。请输入数据根完整路径确认：%s\n> ' % root)
        except EOFError:
            answer = ''
        if answer != str(root):
            print('未确认；未删除')
            return 1
        if rows:
            delete(root, options.days, rows)
        release.cleanup_delete(root, versions)
        return 0
    except (OSError, ValueError, KeyError, TypeError, ledger.LedgerError) as exc:
        print('清理停止：' + type(exc).__name__ + '；请核查账本、路径及权限，保留未完成项', file=sys.stderr)
        return 1
