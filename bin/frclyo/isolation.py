"""两种执行器与本机探针共用权限生成；不提供宽松回退。"""
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path

# 保留原始密钥目录列表；显式 CODEX_HOME 和旧凭据路径另外加入。
SECRET_NAMES = ('.hermes', '.frclyo', '.codex', '.claude', '.ssh', '.gnupg',
                '.config', '.aws', '.docker', '.netrc', '.local/share/opencode', 'Library/Keychains')


def inject_task(source, destination):
    """读一次守则并计算同一字节的哈希，再拼接完整原始任务卡。"""
    rules = (Path(__file__).resolve().parents[2] / '工人守则.md').read_bytes()
    card = Path(source).read_bytes()
    run_dir = Path(destination).parent.resolve()
    location = f'\n本轮目录（交付文件只在这里读写，临时操作按守则第 3 条）：{run_dir}'.encode('utf-8')
    Path(destination).write_bytes(rules + location + b'\n\n---\n\n' + card)
    return hashlib.sha256(rules).hexdigest()


def validate_run(root, run):
    """本轮必须是交付下的真实子目录，不允许符号链接把写口引到其他位置。"""
    root, run = Path(root).resolve(), Path(run).absolute()
    if run != run.resolve() or not run.is_relative_to(root / '交付') or len(run.relative_to(root / '交付').parts) != 2:
        raise ValueError('本轮目录必须是根目录交付下的真实任务/轮次目录')


# OpenCode F01：系统目录沿用可读，用户数据内容默认禁读。
USER_DATA_ROOTS = ('/Users', '/Volumes', '/private/tmp', '/private/var/folders')
# 设备精确例外；不开放 /dev 子树，不开放用户的 xcrun 缓存。
READ_DEVICES = ('/dev/null', '/dev/zero', '/dev/random', '/dev/urandom')
WRITE_DEVICES = ('/dev/null',)


def canonical(path):
    value = os.path.realpath(os.path.expanduser(os.fspath(path)))
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('路径不能含控制字符')
    return value


def runtime_read_paths(worker):
    """仅定位 PATH 中实际执行文件；不放行其父目录、HOME 或整个安装树。

    不读取用户配置/安装包内容。非常规 JS 启动器所需的其他文件须由宿主
    根据失败证据另审；这里绝不通过放宽父目录来掩盖启动失败。
    """
    names = ('python3', 'git', 'bash', worker)
    if worker == 'opencode':
        names += ('bun', 'node')
    found = [sys.executable]
    found.extend(path for name in names if (path := shutil.which(name)))
    return sorted({canonical(path) for path in found})


def round_tmp(root, run):
    """ASCII 物理路径；不解析尚未占用的 tmp 子节点，链接由安全建目录拒绝。"""
    validate_run(root, run)
    root = Path(root).resolve()
    task, round_id = Path(run).relative_to(root / '交付').parts
    if any(not re.fullmatch(r'[A-Za-z0-9_-]+', name) for name in (task, round_id)):
        raise ValueError('任务和轮次编号必须是 ASCII 字母、数字、-、_')
    path = root / 'tmp' / task / round_id
    if not str(path).isascii():
        # 不同根目录与编号组合不会争用同一回退目录；最终节点仍须独占创建。
        identity = hashlib.sha256(str(root).encode()).hexdigest()[:16]
        path = Path('/private/tmp') / ('frclyo-%s-%s-%s-%s-%s' %
            (os.getuid(), identity, len(task), task, round_id))
    return path


def codex_tmp(root, run, home=None):
    """Codex 的 git/构建目录必须在数据根外，且物理路径全 ASCII。

    home 仅供宿主合成夹具指定；生产入口沿用宿主 HOME，不使用命令的私有 HOME。
    路径不 resolve 最终节点，预植链接由安全建目录函数拒绝。
    """
    validate_run(root, run)
    root = Path(root).resolve()
    task, round_id = Path(run).relative_to(root / '交付').parts
    if any(not re.fullmatch(r'[A-Za-z0-9_-]+', name) for name in (task, round_id)):
        raise ValueError('任务和轮次编号必须是 ASCII 字母、数字、-、_')
    parent = Path(canonical(home if home is not None else Path.home())) / '.frclyo-work'
    if not str(parent).isascii() or parent.is_relative_to(root):
        parent = Path('/private/tmp') / ('frclyo-work-%s' % os.getuid())
    if parent.is_relative_to(root):
        raise ValueError('无法在数据根外建立 Codex 私有目录')
    identity = hashlib.sha256(str(root).encode()).hexdigest()[:16]
    return parent / ('%s-%s-%s-%s' % (identity, len(task), task, round_id))


def _temp_environment(temporary):
    temporary = str(temporary)
    return dict(TMPDIR=temporary, TMP=temporary, TEMP=temporary,
                TMPPREFIX=temporary + '/zsh',
                CLANG_MODULE_CACHE_PATH=temporary + '/clang-module-cache',
                SWIFT_MODULECACHE_PATH=temporary + '/swift-module-cache',
                NPM_CONFIG_CACHE=temporary + '/npm-cache',
                PIP_CACHE_DIR=temporary + '/pip-cache',
                XDG_CACHE_HOME=temporary + '/cache')


def temp_environment(root, run):
    """OpenCode 保持原目录与环境。"""
    return _temp_environment(round_tmp(root, run))


def codex_command_environment(root, run, home=None):
    """只用于模型命令；宿主 HOME/CODEX_HOME 不变，登录与权限生成仍用宿主身份。"""
    temporary = codex_tmp(root, run, home)
    private_home = str(temporary / 'home')
    return dict(_temp_environment(temporary), HOME=private_home, ZDOTDIR=private_home,
                XDG_CONFIG_HOME=private_home + '/.config', XDG_DATA_HOME=private_home + '/.local/share',
                XDG_STATE_HOME=private_home + '/.local/state')


def prepare_round_tmp(root, run):
    """只占用全新轮次；包括缓存子目录，全部 0700、逐段 NOFOLLOW。"""
    return _prepare_tmp(round_tmp(root, run))


def _prepare_tmp(path):
    from .execution import exclusive_directory
    exclusive_directory(path)
    for name in ('clang-module-cache', 'swift-module-cache', 'cache', 'npm-cache', 'pip-cache',
                 'home', 'home/.config', 'home/.local', 'home/.local/share', 'home/.local/state'):
        exclusive_directory(path / name)
    return str(path)


def prepare_codex_tmp(root, run, home=None):
    from .execution import private_directory
    path = codex_tmp(root, run, home)
    private_directory(path.parent)
    return _prepare_tmp(path)


def prepare_opencode_tmp(root, run, runtime):
    from .execution import exclusive_directory
    temporary = prepare_round_tmp(root, run)
    exclusive_directory(Path(runtime) / 'home')
    return temporary


def secret_paths(home, sensitive):
    paths = [os.path.join(home, name) for name in SECRET_NAMES] + list(sensitive)
    paths.append(os.environ.get('CODEX_HOME') or os.path.join(home, '.codex'))
    return sorted({canonical(path) for path in paths})


def check_exceptions(exceptions, secrets):
    # Codex 的子放行能覆盖父拒绝；禁止任何例外落入敏感目录。
    # 在敏感目录的父目录放行仍由更具体的 deny 收口。
    for path in exceptions:
        if any(Path(canonical(path)).is_relative_to(Path(secret)) for secret in secrets):
            raise ValueError('读取/写入例外落入敏感目录；拒绝生成权限')


def codex_permission(root, run, sensitive, runtime, temporary_home=None):
    """方案 A：数据根禁读，根外沿用 F01 前可读范围，敏感目录仍禁读。

    Codex 原生权限无法单独允许元数据；不能禁读 /Users 等祖先后指望 git 可用。
    保留 workspace 断网基线，写口限本轮、私有 tmp 及精确 /dev/null。
    必须覆盖 workspace 的特殊临时根；普通 /tmp 只读规则不能消除继承的特殊写口。
    命名档父拒绝/子放行及继承合并的实际结果仍由宿主探针裁定。
    """
    validate_run(root, run)
    home = str(Path.home())
    secrets = secret_paths(home, sensitive)
    temporary = canonical(codex_tmp(root, run, temporary_home))
    check_exceptions([run, temporary] + list(READ_DEVICES), secrets)
    paths = {'/': 'read', ':tmpdir': 'read', ':slash_tmp': 'read'}
    # 普通路径及词法别名也设为只读；特殊继承键必须在上方按同名覆盖。
    for path in ('/tmp', '/private/tmp', '/var/folders', '/private/var/folders'):
        paths[path] = 'read'
        paths[canonical(path)] = 'read'
    for path in (str(root), str(runtime)):
        paths[os.fspath(path)] = 'deny'
        paths[canonical(path)] = 'deny'
    paths[canonical(run)] = 'write'
    paths[temporary] = 'write'
    paths.update({path: 'read' for path in READ_DEVICES})
    paths.update({path: 'write' for path in WRITE_DEVICES})
    paths.update({'~/' + name: 'deny' for name in SECRET_NAMES})
    paths.update({path: 'deny' for path in secrets})
    values = ', '.join(json.dumps(p, ensure_ascii=False) + '=' + json.dumps(v) for p, v in paths.items())
    network = 'true' if os.environ.get('CODEX_WORKER_NET') == '1' else 'false'
    return ('permissions.frclyo_worker={extends=":workspace", network={enabled=' + network
            + '}, filesystem={' + values + '}}')


def codex_temp_environment(root, run, home=None):
    """core 继承不保证带入 TMP/TEMP，显式设置命令环境，值使用 TOML 字符串转义。"""
    values = ', '.join(name + '=' + json.dumps(value, ensure_ascii=False)
                       for name, value in codex_command_environment(root, run, home).items())
    return 'shell_environment_policy.set={' + values + '}'


def seatbelt(home, root, run, runtime, control, port, sensitive, protected, readonly=()):
    """用户数据内容/xattr 禁读；元数据沿用 allow default，支持 getcwd。

    元数据许可会暴露存在性、大小等信息，不允许列目录或读取文件内容。
    写口仅本轮交付、运行时、ASCII 临时目录和 /dev/null；系统工具树沿用可读。
    """
    validate_run(root, run)
    def quote(path):
        path = canonical(path)
        return json.dumps(path, ensure_ascii=False)
    def subpaths(paths):
        return ' '.join('(subpath %s)' % quote(p) for p in paths)
    readonly = tuple(readonly)
    secrets = secret_paths(home, sensitive)
    binaries = runtime_read_paths('opencode')
    temporary = str(round_tmp(root, run))
    check_exceptions([run, runtime, temporary] + list(readonly) + binaries + list(READ_DEVICES), secrets)
    # 例外只从根目录拒绝中排除，密钥拒绝独立生效；不依赖 allow/deny 的排列优先级。
    writable = ' '.join('(require-not (subpath %s))' % quote(path)
                        for path in (run, runtime, temporary))
    read_exceptions = writable + ''.join(' (require-not (literal %s))' % quote(p) for p in list(readonly) + binaries)
    denied_roots = sorted({canonical(p) for p in USER_DATA_ROOTS + (home, root)})
    outside = ' '.join('(require-all (subpath %s) %s)' % (quote(p), read_exceptions)
                       for p in denied_roots)
    rules = ['(version 1)', '(allow default)',
             '(deny file-read-data file-read-xattr %s)' % outside,
             '(deny file-read-data file-read-xattr %s)' % subpaths(secrets),
             # 仅精确 /dev/null 退出全局写拒绝，支持 shell 带 O_CREAT 的重定向。
             '(deny file-write* (require-all %s (require-not (literal "/dev/null"))))' % writable,
             '(deny file-write* %s)' % ' '.join(
                 '(require-all (subpath %s) %s)' % (quote(p), writable) for p in protected),
             '(deny file-read-data file-read-xattr file-write* (subpath %s))' % quote(control),
             '(deny file-write* %s)' % subpaths(secrets),
             '(deny file-write* (literal %s) (literal %s))' % (
                 quote(os.path.join(run, '代理拒绝.log')), quote(os.path.join(run, '网络信息.json'))),
             '(deny network*)',
             '(allow network-outbound (remote tcp4 "localhost:%s"))' % int(port)]
    # 空保护列表不生成无过滤器的 deny，否则会禁止所有文件写入。
    rules = [rule for rule in rules if rule != '(deny file-write* )']
    return '\n'.join(rules) + '\n'
