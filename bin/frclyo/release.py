"""版本发布：先验收独立快照，再原子换链接；所有管理操作共用一把锁。"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from urllib.parse import unquote, urlparse
import uuid

from . import changelog, config

COMMIT = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')
WORKER = re.compile(r'(?:codex|opencode)-worker\.sh(?:\s|$)')


class ReleaseError(ValueError):
    """只显示我们自己的错误说明，不回显外部命令中的配置或凭据。"""


def command(argv, cwd=None):
    # git/进程查询只执行本地短命令；stderr 不直接展示，避免带出私人配置。
    result = subprocess.run([str(x) for x in argv], cwd=cwd, capture_output=True,
                            text=True, timeout=30)
    if result.returncode:
        raise ReleaseError(Path(str(argv[0])).name + ' 检查失败；未把未知状态当作安全')
    return result.stdout.strip()


def git(repo, *args):
    return command(['git', '-C', repo, *args])


def plain_dir(path, create=False):
    # 从已经规范化的数据根向下检查，管理目录不能通过链接跑到其他位置。
    if create:
        path.mkdir(exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ReleaseError('管理路径必须是当前用户的真实目录')
    return path


def layout(root, create=False):
    plain_dir(root)
    versions = root / '框架-版本'
    runtime = root / '运行时'
    if create:
        plain_dir(versions, True)
        plain_dir(runtime, True)
        plain_dir(runtime / '发布', True)
    else:
        for path in (versions, runtime, runtime / '发布'):
            if path.exists() or path.is_symlink():
                plain_dir(path)
    return versions, runtime / '发布'


def read_json(path, default=None):
    # NOFOLLOW 拒绝被替换为链接的管理文件；读损坏文件必须停止，不能清空历史。
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {} if default is None else default
    with os.fdopen(fd) as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ReleaseError('发布状态不是普通文件')
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ReleaseError('发布状态格式不正确')
    return value


def write_json(path, value):
    # 同目录写临时文件再 rename；断电/中断最多留下旧的完整状态或新的完整状态。
    temporary = path.with_name('.' + path.name + '-' + uuid.uuid4().hex)
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def locked(root, explicit_rollback=False):
    # 非阻塞锁避免两个发布者交错切换；clean 的版本删除也用同一把锁。
    _, state_dir = layout(root, True)
    fd = os.open(state_dir / 'lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ReleaseError('发布锁不是普通文件')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ReleaseError('另一项发布、回滚或版本清理正在执行') from exc
        clean_candidates(root)
        # 回滚（含无参）接管 pending，不能先要求待撤回版本的收尾成功。
        yield None if explicit_rollback else recover(root)
    finally:
        os.close(fd)


def state_path(root):
    return root / '运行时/发布/state.json'


def current(root):
    # 返回受管理的链接名称，即使目标已丢失；完整性由 metadata 单独核对。
    # 这样回滚仍能补救悬空入口，但外部链接、嵌套链接仍不能被接管。
    link = root / '框架'
    if not link.is_symlink():
        return None
    target = link.parent / os.readlink(link)
    target = Path(os.path.abspath(target))
    if target.parent != root / '框架-版本' or not COMMIT.fullmatch(target.name):
        raise ReleaseError('框架链接不指向受管理的提交目录')
    if target.exists() or target.is_symlink():
        plain_dir(target)
    return target.name


def atomic_link(root, version):
    # 临时链接和正式链接在同一目录，因此 os.replace 是一次原子名称切换。
    temporary = root / ('.框架-link-' + uuid.uuid4().hex)
    try:
        temporary.symlink_to(Path('框架-版本') / version, target_is_directory=True)
        os.replace(temporary, root / '框架')
    finally:
        temporary.unlink(missing_ok=True)


def record(change, undo):
    # 直接复用 aw 台账的分号和落盘逻辑，绝不自行编辑 Markdown 台账。
    return changelog.append(change, undo)


def recover(root):
    # pending 在切换前落盘。进程被杀后，下次管理操作按真实链接恢复上一版记录。
    state = read_json(state_path(root))
    pending = state.get('pending')
    if not pending:
        return
    old, new, kind = pending['old'], pending['new'], pending['kind']
    actual = current(root)
    link, backup = root / '框架', root / '框架.迁移前'
    if kind == '迁移' and actual is None and not link.exists() and backup.is_dir():
        os.rename(backup, link)
    if kind == '撤销迁移' and actual is None and not link.exists() and backup.is_dir():
        # 撤销被杀在 unlink/rename 之间，先恢复已验收的版本入口，再由命令重试。
        metadata(root, old)
        atomic_link(root, old)
        actual = old
    completed = actual == new if new else link.is_dir() and not link.is_symlink()
    if not completed and actual != old:
        raise ReleaseError('中断后的路径与记录不一致；需要人工核对框架和框架.迁移前')
    if completed:
        finish(root, state, kind, old, new, pending.get('undo', '核对 aw 版本'), recovering=True)
    else:
        record('恢复中断的%s：切换未发生' % kind, pending.get('undo', '核对 aw 版本'))
        state.pop('pending')
        write_json(state_path(root), state)
    return dict(kind=kind, new=new, completed=completed)


def begin(root, kind, old, new, undo):
    state = read_json(state_path(root))
    record('%s开始（未代表完成）：%s → %s' % (kind, old or '真实目录', new or '真实目录'), undo)
    state['pending'] = dict(kind=kind, old=old, new=new, undo=undo,
                            operation_id=uuid.uuid4().hex)
    write_json(state_path(root), state)
    return state


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def completion_recorded(root, change, undo, operation_id=None):
    """持发布锁只读台账；追加仍走 changelog，不能只按版本对去重。"""
    path = root / '台账/改动台账.md'
    try:
        content = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return False
    completed = False
    for line in reversed(content.splitlines()):
        if not re.fullmatch(r'\s*\|\s*#?\d+\s*\|(?:[^|]*\|){2,}\s*', line):
            continue
        fields = [field.strip() for field in line.split('|')[1:-1]]
        if len(fields) != 3:
            continue
        _, recorded_change, recorded_undo = fields
        if operation_id:
            kind = change.split('完成：', 1)[0]
            if recorded_change in (change, change + '（恢复中断的%s）' % kind) and recorded_undo == undo:
                return True
        else:
            # 兼容升级前的 pending：必须在最近一次相同开始记录之后。
            kind = change.split('完成：', 1)[0]
            if recorded_undo == undo and recorded_change in (
                    change, change + '（恢复中断的%s）' % kind):
                completed = True
            if '开始（未代表完成）：' in recorded_change:
                return (completed and recorded_undo == undo and
                        recorded_change == change.replace('完成：', '开始（未代表完成）：', 1))
    return False


def finish(root, state, kind, old, new, undo, recovering=False):
    state.update(current=new, previous=old if new else None)
    if kind == '迁移':
        state['migration'] = new
    step = '切换状态/发布时间写入'
    try:
        pending = state['pending']
        change = '%s完成：%s → %s' % (kind, old or '真实目录', new or '真实目录')
        if not pending.get('operation_id'):
            if not pending.get('recorded') and completion_recorded(root, change, undo):
                pending['recorded'] = True
            pending['operation_id'] = uuid.uuid4().hex
        pending.setdefault('switched_at', utc_now())
        write_json(state_path(root), state)
        if new and kind in ('发布', '迁移'):
            path = root / '运行时/发布' / (new + '.json')
            item = read_json(path)
            if not item.get('published_at'):
                item['published_at'] = pending['switched_at']
                write_json(path, item)
        step = '完成台账写入'
        if not pending.get('recorded'):
            change += '（操作 ID：%s）' % pending['operation_id']
            if not completion_recorded(root, change, undo, pending['operation_id']):
                record(change + ('（恢复中断的%s）' % kind if recovering else ''), undo)
            pending['recorded'] = True
            write_json(state_path(root), state)
        step = '看板重启'
        if not pending.get('restarted'):
            restart_board(root)
            pending['restarted'] = True
            write_json(state_path(root), state)
        step = '收尾状态写入'
        state.pop('pending', None)
        write_json(state_path(root), state)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise ReleaseError('版本已切换；%s失败；保留新入口和待办，请修复故障后再次运行 aw 发布 补做' % step) from exc


def restart_board(root):
    try:
        command(['launchctl', 'kickstart', '-k', 'gui/%d/com.frclyo.board' % os.getuid()])
    except (OSError, subprocess.SubprocessError, ReleaseError) as exc:
        record('版本已切换；看板重启失败', '修复 launchd 后重新执行 launchctl kickstart -k gui/%d/com.frclyo.board' % os.getuid())
        raise ReleaseError('版本已切换并记账，但看板重启失败；请检查 aw 版本和 launchd 服务') from exc


def clean_repo(repo):
    # 拒绝未跟踪文件、暂存和未暂存改动，防止提交快照与开发者眼前的代码不同。
    plain_dir(repo)
    if Path(git(repo, 'rev-parse', '--show-toplevel')).resolve() != repo:
        raise ReleaseError('源目录必须是 git 仓库根目录')
    if git(repo, 'status', '--porcelain', '--untracked-files=all', '--ignore-submodules=none'):
        raise ReleaseError('源仓库有未提交改动；请先整理并提交')
    commit = git(repo, 'rev-parse', '--verify', 'HEAD^{commit}')
    if not COMMIT.fullmatch(commit):
        raise ReleaseError('HEAD 不是完整提交号')
    # git archive 不会带出子模块内容；不能把缺了一部分的框架当作完整快照。
    if any(line.startswith('160000 ') for line in git(repo, 'ls-tree', '-r', 'HEAD').splitlines()):
        raise ReleaseError('发布暂不支持 git 子模块，请先提供完整框架提交')
    return commit


def pushed(repo, commit):
    # 不 fetch、不联网。读取实际本地远端分支，避免把陈旧的 remote-tracking ref 当成已推送。
    branch = git(repo, 'symbolic-ref', '--quiet', '--short', 'HEAD')
    remote = git(repo, 'config', '--get', 'branch.' + branch + '.remote')
    merge = git(repo, 'config', '--get', 'branch.' + branch + '.merge')
    if remote == '.' or not merge.startswith('refs/heads/'):
        raise ReleaseError('开发分支必须跟踪本地远端的分支')
    url = git(repo, 'remote', 'get-url', '--push', remote)
    if url.startswith('file://'):
        parsed = urlparse(url)
        if parsed.netloc not in ('', 'localhost') or parsed.query or parsed.fragment:
            raise ReleaseError('远端必须是本机文件路径')
        path = Path(unquote(parsed.path))
    elif ':' in url or '\n' in url:
        raise ReleaseError('远端必须是本地路径；发布不会访问网络')
    else:
        path = Path(url).expanduser()
    if not path.is_absolute():
        path = repo / path
    path = path.resolve(strict=True)
    remote_tip = git(path, 'rev-parse', '--verify', merge + '^{commit}')
    # 在远端直接询问祖先关系，既支持远端领先，也不依赖本地追踪缓存。
    git(path, 'merge-base', '--is-ancestor', commit, remote_tip)


def unpack(repo, commit, destination):
    # 手工提取 archive：拒绝穿越、特殊文件和外指链接，不调用不受约束的 extractall。
    result = subprocess.run(['git', '-C', str(repo), 'archive', '--format=tar', commit],
                            capture_output=True, timeout=60)
    if result.returncode:
        raise ReleaseError('git archive 失败')
    with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
        members = archive.getmembers()
        for member in members:
            name = PurePosixPath(member.name)
            if name.is_absolute() or '..' in name.parts or '.git' in name.parts:
                raise ReleaseError('提交包含不安全的归档路径')
            if not (member.isdir() or member.isfile() or member.issym()):
                raise ReleaseError('提交包含不支持的归档节点')
            if member.issym():
                target = destination / name.parent / member.linkname
                if not target.resolve().is_relative_to(destination):
                    raise ReleaseError('提交中的链接指向版本目录以外')
        # 普通文件先建立，链接最后建立；不会借先前的链接覆盖其他位置。
        for member in sorted(members, key=lambda item: item.issym()):
            path = destination / member.name
            path.parent.mkdir(parents=True, exist_ok=True)
            if member.isdir():
                path.mkdir(exist_ok=True)
            elif member.issym():
                path.symlink_to(member.linkname)
            else:
                with archive.extractfile(member) as source, path.open('xb') as target:
                    shutil.copyfileobj(source, target)
                path.chmod(0o755 if member.mode & 0o111 else 0o644)
    for relative in ('bin/aw', 'board/board.py', 'bin/codex-worker.sh', 'bin/opencode-worker.sh'):
        if not (destination / relative).is_file():
            raise ReleaseError('提交缺少框架入口：' + relative)


def digest(tree):
    # 保存内容和执行位指纹；回滚及清理只接受我们登记且没有被改动的快照。
    value = hashlib.sha256()
    for path in sorted(tree.rglob('*')):
        relative = path.relative_to(tree).as_posix()
        info = path.lstat()
        if path.is_symlink():
            data = ('L' + os.readlink(path)).encode()
            if not path.resolve().is_relative_to(tree):
                raise ReleaseError('版本含外部链接')
        elif path.is_dir():
            data = b'D'
        elif path.is_file():
            data = ('F%d:' % bool(info.st_mode & 0o111)).encode() + path.read_bytes()
        else:
            raise ReleaseError('版本包含特殊文件')
        value.update(relative.encode() + b'\0' + data + b'\0')
    return value.hexdigest()


def permissions(tree, readonly):
    # 不跟随链接 chmod；只改变本次快照自己的目录和文件。
    for path in [tree, *tree.rglob('*')]:
        if path.is_symlink():
            continue
        mode = path.stat().st_mode
        path.chmod((0o555 if path.is_dir() or mode & 0o111 else 0o444) if readonly
                   else (0o755 if path.is_dir() or mode & 0o111 else 0o644))


def metadata(root, version):
    if not COMMIT.fullmatch(version):
        raise ReleaseError('版本必须是完整提交号；先运行 aw 版本')
    if deletion(root, version):
        raise ReleaseError('版本正在清理，不能回滚：' + version)
    path = root / '框架-版本' / version
    plain_dir(path)
    # 封存权限也是版本契约的一部分；仅改 chmod 也不能悄悄把可写目录当稳定快照。
    if any(node.lstat().st_mode & 0o222 for node in [path, *path.rglob('*')] if not node.is_symlink()):
        raise ReleaseError('版本目录不再只读：' + version)
    item = read_json(root / '运行时/发布' / (version + '.json'))
    if item.get('commit') != version or item.get('digest') != digest(path):
        raise ReleaseError('版本未登记或内容已变动：' + version)
    if not item.get('accepted'):
        raise ReleaseError('版本未通过验收且不是迁移保留的版本')
    return path


def remove_generated_bytecode(tree, original):
    # 既有入口使用 python -I，会忽略禁写缓存的环境变量；仅清掉验收新生的字节码。
    # 提交原有文件（包括缓存）不碰，其他源码或新增文件仍须通过完整指纹检查。
    for path in sorted(tree.rglob('*'), key=lambda item: len(item.parts), reverse=True):
        if path.relative_to(tree) in original or path.is_symlink():
            continue
        if '__pycache__' in path.relative_to(tree).parts and path.is_file() and path.suffix == '.pyc':
            path.unlink()
        elif path.name == '__pycache__' and path.is_dir() and not any(path.iterdir()):
            path.rmdir()


def prepare(root, repo, commit, verify=False, paid=False):
    versions, state_dir = layout(root, True)
    # 验收在版本容器中的独立候选目录执行；成功后才封为只读并赋予提交号名称。
    stage = Path(tempfile.mkdtemp(prefix='.candidate-' + commit[:12] + '-', dir=versions)).resolve()
    try:
        unpack(repo, commit, stage)
        before = digest(stage)
        original = {path.relative_to(stage) for path in stage.rglob("*")}
        if verify:
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
            result = subprocess.run([sys.executable, '-B', str(stage / 'bin/aw'), '验收',
                                     *(['--paid'] if paid else [])], cwd=stage, env=env)
            if result.returncode:
                raise ReleaseError('新版本验收失败；当前链接没有切换')
            remove_generated_bytecode(stage, original)
            if digest(stage) != before:
                raise ReleaseError('验收改动了候选源码；当前链接没有切换')
        destination = versions / commit
        existing = read_json(state_dir / (commit + '.json'))
        if destination.exists() or destination.is_symlink():
            plain_dir(destination)
            if deletion(root, commit):
                raise ReleaseError('相同提交正在清理；请先重试 aw clean')
            if existing:
                metadata(root, commit)
            elif any(node.lstat().st_mode & 0o222 for node in [destination, *destination.rglob('*')]
                     if not node.is_symlink()):
                raise ReleaseError('未登记的版本不是只读快照；保留目录，请核对')
            if digest(destination) != before:
                raise ReleaseError('相同提交号已有不同内容；保留原目录，请人工核对')
        else:
            permissions(stage, True)
            os.rename(stage, destination)
        write_json(state_dir / (commit + '.json'), dict(existing, commit=commit, digest=before,
                   prepared_at=existing.get('prepared_at') or utc_now(),
                   published_at=existing.get('published_at'),
                   accepted=True, leases=all('frclyo.release import pin_worker' in
                       (destination / 'bin' / (worker + '-worker.sh')).read_text()
                       for worker in ('codex', 'opencode')), acceptance='paid' if paid else ('verify' if verify else 'migration')))
        return destination
    finally:
        if stage.exists():
            permissions(stage, False)
            shutil.rmtree(stage)


def processes(root):
    # 只取进程路径，不读取环境或任务正文。查询失败时禁止迁移/删除，版本列表标未知。
    output = command(['ps', '-ww', '-axo', 'pid=,ppid=,command='])
    rows = {}
    for line in output.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) == 3 and fields[0].isdigit() and fields[1].isdigit():
            rows[int(fields[0])] = dict(parent=int(fields[1]), command=fields[2])
    workers, users, unknown = {}, set(), set()
    candidates = list((root / '框架-版本').glob('*')) if (root / '框架-版本').exists() else []
    paths = {str(path): path.name for path in candidates if COMMIT.fullmatch(path.name)}
    # lsof 的 cwd 补足相对路径启动的进程；只查询当前用户的工作目录，不读取打开的文件内容。
    result = subprocess.run(['lsof', '-a', '-u', str(os.getuid()), '-d', 'cwd', '-Fpn'],
                            capture_output=True, text=True, timeout=30)
    if result.returncode not in (0, 1) or result.stderr.strip():
        raise ReleaseError('无法检查进程工作目录')
    cwd, pid = {}, None
    for line in result.stdout.splitlines():
        if line.startswith('p') and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith('n') and pid is not None:
            cwd[pid] = line[1:]
    for pid, row in rows.items():
        text = row['command']
        refs = {name for path, name in paths.items()
                if path + '/' in text or cwd.get(pid) == path or cwd.get(pid, '').startswith(path + '/')}
        # 本管理进程可以从旧版本启动，但不能删除它仍在使用的模块。
        if refs:
            users.update(refs)
        if WORKER.search(text):
            if len(refs) == 1:
                workers[pid] = next(iter(refs))
            else:
                unknown.add(pid)
    # shell 包装器和同一个工人的子进程不重复计数；按工人入口 PID 计数。
    counts = {name: 0 for name in paths.values()}
    for pid, version in workers.items():
        ancestor, seen = rows[pid]['parent'], set()
        while ancestor in rows and ancestor not in seen and ancestor not in workers:
            seen.add(ancestor)
            ancestor = rows[ancestor]['parent']
        if ancestor not in workers:
            counts[version] += 1
    return counts, users, unknown


def idle(root):
    counts, _, unknown = processes(root)
    if any(counts.values()) or unknown:
        raise ReleaseError('仍有工人在运行或无法定位版本；首次迁移/撤销要求先停止派工并等工人结束')


def migrate(root):
    stable, backup = root / '框架', root / '框架.迁移前'
    if stable.is_symlink():
        raise ReleaseError('已经采用版本链接；不重复迁移')
    if backup.exists() or backup.is_symlink():
        raise ReleaseError('框架.迁移前已存在；保留原备份，请先人工核对')
    commit = clean_repo(stable)
    idle(root)
    prepare(root, stable, commit)
    idle(root)
    undo = 'aw 发布 --撤销迁移（先停止派工并等工人结束）'
    state = begin(root, '迁移', None, commit, undo)
    os.rename(stable, backup)
    try:
        atomic_link(root, commit)
    except BaseException:
        os.rename(backup, stable)
        raise
    finish(root, state, '迁移', None, commit, undo)


def undo_migration(root):
    state = read_json(state_path(root))
    old = current(root)
    backup = root / '框架.迁移前'
    if not old or not state.get('migration'):
        raise ReleaseError('没有可撤销的迁移')
    plain_dir(backup)
    idle(root)
    undo = 'aw 发布 --迁移（先停止派工并等工人结束）'
    state = begin(root, '撤销迁移', old, None, undo)
    (root / '框架').unlink()
    try:
        os.rename(backup, root / '框架')
    except BaseException:
        atomic_link(root, old)
        raise
    finish(root, state, '撤销迁移', old, None, undo)


def switch(root, target, kind):
    old = current(root)
    if old is None:
        raise ReleaseError('框架还是普通目录；请先停派工并运行 aw 发布 --迁移')
    metadata(root, target)
    if old == target:
        state = read_json(state_path(root))
        if not state.get('pending'):
            print('已经是当前版本：' + target)
            return
        if state['pending'].get('new') == target:
            recover(root)
            return
        # 切换尚未发生的旧 pending 也应由显式回滚接管；保留真实上一版。
        old = state.get('previous')
    undo = 'aw 回滚 ' + (old or target)
    state = begin(root, kind, old, target, undo)
    atomic_link(root, target)
    finish(root, state, kind, old, target, undo)


def publish(root, paid=False):
    if current(root) is None:
        raise ReleaseError('首次发布前请停派工并运行 aw 发布 --迁移')
    repo = root / '框架-开发'
    commit = clean_repo(repo)
    pushed(repo, commit)
    prepare(root, repo, commit, verify=True, paid=paid)
    switch(root, commit, '发布')


def version_rows(root):
    versions, _ = layout(root)
    state = read_json(state_path(root))
    active = current(root)
    try:
        counts, users, unknown = processes(root)
        issue = '有 %d 个无法定位版本的工人；禁止清理' % len(unknown) if unknown else ''
    except (OSError, subprocess.SubprocessError, ReleaseError):
        counts, users, unknown, issue = {}, set(), {-1}, '进程检查不可用；工人数未知，禁止清理'
    if state.get('pending'):
        issue = (issue + '；' if issue else '') + '有中断记录；下次发布或回滚先按真实链接恢复'
    try:
        usage = worker_usage(root)
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        usage, unknown = [], {-1}
        issue = (issue + '；' if issue else '') + '工人使用记录不可用；禁止清理'
    names = {path.name for path in versions.glob('*') if COMMIT.fullmatch(path.name)}
    names.update(path.name.split('.')[0]
                 for path in (root / '运行时/发布').glob('*.json')
                 if COMMIT.fullmatch(path.name.split('.')[0]))
    names.update(name for name in (active, state.get('previous')) if name and COMMIT.fullmatch(name))
    rows = []
    for name in sorted(names):
        path = versions / name
        item, problem, deleting = {}, '', False
        try:
            item = read_json(root / '运行时/发布' / (name + '.json'))
            deleting = bool(deletion(root, name))
            metadata(root, name)
        except (OSError, ValueError) as exc:
            problem = str(exc) if isinstance(exc, ReleaseError) else type(exc).__name__
        live = [entry for entry in usage if entry['version'] == name and entry['active'] is True]
        uncertain = any(entry['version'] == name and entry['active'] is None for entry in usage)
        in_use = name in users or bool(live)
        rows.append(dict(version=name, path=path, current=name == active,
                         previous=name == state.get('previous'),
                         workers=max(counts[name], len(live)) if name in counts else None,
                         in_use=in_use,
                         valid=not problem, rollbackable=not problem, problem=problem,
                         deleting=deleting, published_at=item.get('published_at'),
                         users=live, usage_unknown=uncertain,
                         eligible=not unknown and not uncertain and not in_use
                         and name not in (active, state.get('previous'))
                         and (not problem or deleting)))
    return rows, issue


def cleanup_preview(root):
    # 没启用版本发布时不查进程、不创建目录，保持原 clean 的只读预览行为。
    if not (root / '框架-版本').exists():
        return []
    rows, issue = version_rows(root)
    if issue:
        print(issue, file=sys.stderr)
    candidates = []
    for row in rows:
        if row['eligible']:
            if row['deleting']:
                candidates.append(row['version'])
                print('可清理旧版本：' + row['version'] + '（继续中断的清理）')
                continue
            try:
                metadata(root, row['version'])
                if not read_json(root / '运行时/发布' / (row['version'] + '.json')).get('leases'):
                    # 迁移自旧代码的快照没有启动锁；默认保留，不能与新派工互斥。
                    print('保留旧版本（无启动锁协议）：' + row['version'])
                    continue
            except (OSError, ValueError):
                continue
            candidates.append(row['version'])
            print('可清理旧版本：' + row['version'])
    return candidates


def cleanup_delete(root, selected):
    # 与发布互斥，并在实际删除前重新查进程；当前、上一版、未知或正在使用的一律保留。
    if not selected:
        return
    with locked(root):
        available = set(cleanup_preview(root))
        if not set(selected) <= available:
            raise ReleaseError('版本使用情况已变化；请重新预览，未删除版本')
        for version in selected:
            with unused_version(root, version):
                remove_version(root, version)


def remove_version(root, version):
    # 调用者同时持有发布锁和此版本独占锁，此后不会有新工人进入该版本。
    marker = root / '运行时/发布' / (version + '.delete.json')
    intent = deletion(root, version)
    if not intent:
        path = metadata(root, version)
        record('清理旧版本开始（未代表完成）：' + version, '从本地远端重新发布该提交；无自动恢复')
        write_json(marker, dict(version=version, deleting=True))
    else:
        path = root / '框架-版本' / version
    if path.exists() or path.is_symlink():
        plain_dir(path)
        permissions(path, False)
        shutil.rmtree(path)
    (root / '运行时/发布' / (version + '.json')).unlink(missing_ok=True)
    record('清理旧版本完成：' + version, '从本地远端重新发布该提交；无自动恢复')
    marker.unlink()


def deletion(root, version):
    if not COMMIT.fullmatch(version):
        raise ReleaseError('清理记录的版本号不正确')
    item = read_json(root / '运行时/发布' / (version + '.delete.json'))
    if item and (item.get('version') != version or item.get('deleting') is not True):
        raise ReleaseError('清理记录格式不正确；保留目录')
    return item


def clean_candidates(root):
    # 仅在持有全局发布锁时调用；此时没有合法的候选验收在运行。
    for path in sorted((root / '框架-版本').glob('.candidate-*')):
        plain_dir(path)
        permissions(path, False)
        shutil.rmtree(path)
        print('已清理中断的候选目录：' + path.name)


def process_identity(pid):
    """只查询进程开始时间和状态，不读取命令行、环境或任务正文。"""
    result = subprocess.run(['ps', '-p', str(pid), '-o', 'lstart=,stat='],
                            capture_output=True, text=True, timeout=30,
                            env=dict(os.environ, LC_ALL='C'))
    if result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip():
        return None
    if result.returncode or result.stderr.strip():
        raise ReleaseError('工人进程身份检查不可用')
    fields = result.stdout.split()
    if len(fields) != 6:
        raise ReleaseError('工人进程身份检查不可用')
    if fields[-1].startswith('Z'):
        return None
    return ' '.join(fields[:5])


def register_worker(root, version, worker, args):
    """锁已获取后登记；不保存任务卡路径、模型参数或正文。"""
    _, state_dir = layout(root)
    directory = plain_dir(state_dir / 'users', True)
    pid = os.getpid()
    try:
        identity = process_identity(pid)
    except (OSError, ValueError, subprocess.SubprocessError):
        identity = None
    safe_id = re.compile(r'[A-Za-z0-9_-]+\Z')
    item = dict(version=version, worker=worker, pid=pid, started_at=utc_now(),
                process_started_at=identity,
                task=args[0] if len(args) > 0 and safe_id.fullmatch(args[0]) else None,
                round=args[1] if len(args) > 1 and safe_id.fullmatch(args[1]) else None)
    write_json(directory / ('%d-%s.json' % (pid, uuid.uuid4().hex)), item)


def worker_usage(root):
    """只读接口：返回登记信息及 active=True/False/None（无法确认）。

    PID 与进程开始时间同时匹配才算活跃；进程已退出或 PID 被复用算结束。
    旧版没有登记的工人仍由 processes 的引用扫描保护，不能从空列表推断无人使用。
    """
    _, state_dir = layout(root)
    directory = state_dir / 'users'
    if not directory.exists() and not directory.is_symlink():
        return []
    plain_dir(directory)
    rows, identities = [], {}
    for path in sorted(directory.glob('*.json')):
        item = read_json(path)
        pid = item.get('pid')
        if (type(pid) is not int or pid <= 0 or not COMMIT.fullmatch(item.get('version', ''))
                or item.get('worker') not in ('codex', 'opencode')):
            raise ReleaseError('工人使用记录格式不正确')
        if pid not in identities:
            try:
                identities[pid] = (True, process_identity(pid))
            except (OSError, ValueError, subprocess.SubprocessError):
                identities[pid] = (False, None)
        known, identity = identities[pid]
        active = None
        if known and identity is None:
            active = False
        elif known and item.get('process_started_at'):
            active = identity == item['process_started_at']
        rows.append(dict(item, active=active))
    return rows


def pin_worker(bin_dir, worker, args):
    # fd 随 exec 传给 shell，并由它持有到退出；锁文件在版本目录外，源码仍可只读。
    bin_dir = Path(bin_dir).resolve(strict=True)
    version = bin_dir.parent
    script = bin_dir / (worker + '-worker.sh')
    fd = None
    try:
        if version.parent.name == '框架-版本' and COMMIT.fullmatch(version.name):
            _, state_dir = layout(version.parent.parent)
            plain_dir(state_dir)
            fd = os.open(state_dir / (version.name + '.lease'),
                         os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ReleaseError('版本锁不是普通文件')
            # 删除者持独占锁期间拒绝新工人；拿锁后再确认目录仍在，不能运行半删除的代码。
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            if deletion(version.parent.parent, version.name) or not script.is_file():
                raise ReleaseError('版本已被清理；请从当前框架入口重新派工')
            register_worker(version.parent.parent, version.name, worker, args)
            os.set_inheritable(fd, True)
        os.environ['FRCLYO_RELEASE_PIN'] = str(os.getpid()) + ':' + str(script)
        os.execv('/bin/bash', ['bash', str(script), *args])
    except BaseException:
        if fd is not None:
            os.close(fd)
        raise

@contextmanager
def unused_version(root, version):
    # 与启动器共享锁协议；锁文件保留，避免删除重建锁导致两个锁互不相识。
    fd = os.open(root / '运行时/发布' / (version + '.lease'),
                 os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ReleaseError('版本锁不是普通文件')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ReleaseError('旧版本仍被工人持有；未删除') from exc
        yield
    finally:
        os.close(fd)


def installed_entry(repo):
    # 安装器经版本路径运行时仍把 launchd/aw 指到稳定入口，重启后才能加载新版。
    stable = config.root_dir() / '框架'
    return stable if stable.is_symlink() and stable.resolve() == repo else repo


def main(args=None):
    parser = argparse.ArgumentParser(prog='aw release / rollback / versions')
    commands = parser.add_subparsers(dest='command', required=True)
    release = commands.add_parser('release')
    mode = release.add_mutually_exclusive_group()
    mode.add_argument('--migrate', '--迁移', action='store_true')
    mode.add_argument('--undo-migration', '--撤销迁移', action='store_true')
    release.add_argument('--paid', '--付费验收', action='store_true')
    rollback = commands.add_parser('rollback')
    rollback.add_argument('version', nargs='?')
    commands.add_parser('versions')
    options = parser.parse_args(args)
    if options.command == 'release' and options.paid and (options.migrate or options.undo_migration):
        parser.error('迁移操作不能同时指定付费验收')
    try:
        root = config.root_dir()
        if options.command == 'versions':
            rows, issue = version_rows(root)
            for row in rows:
                print('%s\t%s\t工人：%s%s' % (row['version'],
                      '当前' if row['current'] else ('上一个' if row['previous'] else '旧版本'),
                      row['workers'] if row['workers'] is not None else '未知',
                      ('（进程正在引用）' if row['in_use'] else '')
                      + ('；残缺/不可回滚：' + row['problem'] if not row['rollbackable'] else '')
                      + ('；使用登记存活状态未知' if row['usage_unknown'] else '')
                      + '；发布时间：' + (row['published_at'] or '未知（旧记录或尚未切换）')))
            if issue:
                print(issue, file=sys.stderr)
            if not rows:
                print('尚无版本目录；首次迁移：aw 发布 --迁移')
            return 1 if issue else 0
        with locked(root, explicit_rollback=options.command == 'rollback') as recovered:
            # 同一命令补做完收尾后直接成功，不再次验收，也不把上一版覆盖成自己。
            resumed = recovered and recovered['completed'] and (
                options.command == 'release' and (not options.migrate and not options.undo_migration
                    or options.migrate and recovered['kind'] == '迁移'
                    or options.undo_migration and recovered['kind'] == '撤销迁移')
                or options.command == 'rollback' and recovered['kind'] == '回滚'
                    and (not options.version or options.version == recovered['new']))
            if resumed:
                print('已补做中断的' + recovered['kind'] + '收尾')
            elif options.command == 'rollback':
                state = read_json(state_path(root))
                pending = state.get('pending', {})
                # 无参重试未完成的回滚仍指向原目标，不能又切回待撤回版本。
                target = options.version or (pending.get('new') if pending.get('kind') == '回滚'
                                             else pending.get('old') if pending
                                             else state.get('previous'))
                if not target:
                    raise ReleaseError('没有上一个版本；请用 aw 版本查看可选提交')
                switch(root, target, '回滚')
            elif options.migrate:
                migrate(root)
            elif options.undo_migration:
                undo_migration(root)
            else:
                publish(root, options.paid)
        print('完成；当前版本：' + (current(root) or '迁移前真实目录'))
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        detail = str(exc) if isinstance(exc, ReleaseError) else type(exc).__name__
        print('发布管理停止：' + detail, file=sys.stderr)
        return 1
