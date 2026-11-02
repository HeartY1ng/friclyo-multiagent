"""真实 OpenCode 的零账号离线夹具；仅宿主诊断导入，不用于生产放宽规则。"""
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import signal
import subprocess

from frclyo.isolation import USER_DATA_ROOTS, canonical, runtime_read_paths

TOKEN = 'FRCLYO_OFFLINE_OK'


class FakeCompletion(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(5)
        super().setup()

    def log_message(self, *args):
        pass  # 不保存请求头、请求体或认证值。

    def do_POST(self):
        self.connection.settimeout(5)
        if self.path != '/deepseek/chat/completions':
            self.send_error(404)
            return
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 4 * 1024 * 1024:
                raise ValueError()
            payload = json.loads(self.rfile.read(size))
            if payload.get('model') != 'deepseek-flash':
                raise ValueError()
        except (OSError, ValueError):
            self.send_error(400)
            return
        # 只记录请求数；即使诊断出错，也不能把任何入站头写进日志。
        self.server.requests += 1
        if hasattr(self.server, 'record'):
            self.server.record(self.server.requests)
        common = dict(id='offline-completion', created=1, model='deepseek-flash')
        if payload.get('stream'):
            chunks = [dict(common, object='chat.completion.chunk', choices=[dict(
                index=0, delta=dict(role='assistant', content=TOKEN), finish_reason=None)]),
                dict(common, object='chat.completion.chunk', choices=[dict(
                    index=0, delta={}, finish_reason='stop')],
                    usage=dict(prompt_tokens=1, completion_tokens=1, total_tokens=2))]
            body = ''.join('data: ' + json.dumps(chunk) + '\n\n' for chunk in chunks)
            body += 'data: [DONE]\n\n'
            content_type = 'text/event-stream'
        else:
            body = json.dumps(dict(common, object='chat.completion', choices=[dict(
                index=0, message=dict(role='assistant', content=TOKEN), finish_reason='stop')],
                usage=dict(prompt_tokens=1, completion_tokens=1, total_tokens=2)))
            content_type = 'application/json'
        body = body.encode()
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass


def response_seen(text):
    for line in text.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if (isinstance(item, dict) and item.get('type') == 'text' and
                isinstance(item.get('part'), dict) and TOKEN in item['part'].get('text', '')):
            return True
    return False


def execute(argv, env, cwd, log, timeout):
    """只给 setsid 创建并核对过的子组发信号；不查询后再杀调用者的组。

    超时先 TERM，留出生产入口回收监督器及其独立工人组的时间。
    """
    proc = None
    owned_group = None
    def send(sig):
        if (owned_group is None or owned_group <= 1 or
                owned_group in (os.getpid(), os.getpgrp())):
            return
        try:
            os.killpg(owned_group, sig)
        except ProcessLookupError:
            pass
    with log.open('wb') as stream:
        try:
            proc = subprocess.Popen(argv, env=env, cwd=cwd, stdout=stream,
                                    stderr=subprocess.STDOUT, start_new_session=True)
            child_group, child_session = os.getpgid(proc.pid), os.getsid(proc.pid)
            stream.write(('FRCLYO_DIAGNOSTIC_GROUP=' + json.dumps(dict(
                caller_pgid=os.getpgrp(), child_pid=proc.pid, child_pgid=child_group,
                child_sid=child_session)) + '\n').encode())
            stream.flush()
            if child_group != proc.pid or child_session != proc.pid:
                raise RuntimeError('诊断子进程未独占 session/group；拒绝组信号')
            owned_group = proc.pid
            return proc.wait(timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            stream.write(('launcher: %s errno=%s\n' %
                          (type(exc).__name__, getattr(exc, 'errno', None))).encode())
            return 125
        finally:
            if proc is not None:
                if owned_group is None and proc.poll() is None:
                    # 身份核对失败只能回收 Popen 返回的直接子进程。
                    proc.terminate()
                send(signal.SIGCONT)
                send(signal.SIGTERM)
                try:
                    proc.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    if owned_group is None:
                        proc.kill()
                    else:
                        send(signal.SIGKILL)
                finally:
                    # leader 正常退出后仍回收同组后代；从不使用负的调用者 PGID。
                    send(signal.SIGKILL)
                proc.wait(timeout=3)


def _forms(text):
    """拆分生成器中已知的 S 表达式；引号内括号和转义不能改变深度。"""
    depth = 0
    quoted = escaped = False
    start = None
    for index, char in enumerate(text):
        if quoted:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == '(':
            if depth == 0:
                start = index
            depth += 1
        elif char == ')':
            depth -= 1
            if depth == 0:
                yield text[start:index + 1]
    if depth or quoted:
        raise ValueError('无效诊断规则')


def diagnostic_profile(profile, category, home, root, fixture_roots):
    """逐项实验仅可读合成夹具；独立 guard 继续禁止所有真实用户内容。

    HOME 可能位于 /private/tmp，因此只解除合成 HOME 的祖先覆盖，仍保留框架根禁读。
    tmp/folders 移除相应 F01 根拒绝，但 guard 仅开放本次生成的夹具。
    生产 seatbelt 不接受放宽参数。
    """
    if category not in ('home', 'tmp', 'folders', 'caller'):
        raise ValueError('未知诊断类别')
    q = lambda p: json.dumps(canonical(p), ensure_ascii=False)
    lines = profile.splitlines()
    prefix = '(deny file-read-data file-read-xattr '
    index = next(i for i, line in enumerate(lines) if line.startswith(prefix))
    forms = list(_forms(lines[index][len(prefix):-1]))
    updated = []
    for form in forms:
        if category == 'caller':
            form = form[:-1] + ' (require-not (subpath ' + q(Path(root) / 'framework-caller') + ')))'
        elif category in ('tmp', 'folders'):
            target = '/private/tmp' if category == 'tmp' else '/private/var/folders'
            if form.startswith('(require-all (subpath ' + q(target) + ') '):
                continue
        elif not form.startswith('(require-all (subpath ' + q(root) + ') '):
            form = form[:-1] + ' (require-not (subpath ' + q(home) + ')))'
        updated.append(form)
    if not updated:
        raise ValueError('诊断规则不能移除全部用户根拒绝')
    lines[index] = prefix + ' '.join(updated) + ')'
    # guard 只允许本次夹具和精确执行文件；原始秘密/控制/框架/写/网络拒绝全部保留。
    exclusions = ' '.join('(require-not (subpath %s))' % q(p) for p in fixture_roots)
    exclusions += ''.join(' (require-not (literal %s))' % q(p)
                          for p in runtime_read_paths('opencode'))
    roots = sorted({canonical(p) for p in USER_DATA_ROOTS + (str(Path.home()),)})
    guard = ' '.join('(require-all (subpath %s) %s)' % (q(p), exclusions) for p in roots)
    lines.append(prefix + guard + ')')
    return '\n'.join(lines) + '\n'
