#!/usr/bin/env python3
"""离线的一天：真实入口/监督器/账本/看板/清理器，外部工具全部替身。

只用标准库。必须先把此脚本、复现测试与框架复制到入口 TMPDIR 再运行：
  python3 -B 场景.py --framework /本轮私有临时目录/framework
报告含失败检查时退出 1，环境阻塞时退出 2；不会把跳过项计作通过。
假 HOME 只设置于被测子进程和受控的模块加载上下文，不改入口环境。
"""
import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from unittest import mock


STUB = r'''#!/usr/bin/env python3
import json, os, pathlib, signal, sys, time
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
base = pathlib.Path(__file__).resolve().parent.parent
state = base / 'system-state.json'
def read(): return json.loads(state.read_text())
def write(value): state.write_text(json.dumps(value))
if name == 'launchctl':
    s = read()
    if args[0] == 'print': sys.exit(0 if s['launchd'] else 113)
    if args[0] == 'bootstrap': s['launchd'] = True
    elif args[0] == 'bootout': s['launchd'] = False
    else: sys.exit(99)
    write(s); sys.exit(0)
if name == 'security':
    s = read()
    identity = args[args.index('-s')+1] + ':' + args[args.index('-a')+1]
    if args[0] == 'find-generic-password':
        if identity not in s['keys']: sys.exit(44)
        if '-w' in args: print('OFFLINE-NOT-A-CREDENTIAL')
    elif args[0] == 'add-generic-password':
        if identity in s['keys']: sys.exit(45)
        s['keys'].append(identity); write(s)
    elif args[0] == 'delete-generic-password':
        if identity not in s['keys']: sys.exit(44)
        s['keys'].remove(identity); write(s)
    else: sys.exit(99)
    sys.exit(0)
if name == 'sandbox-exec':
    assert args[0] == '-f'
    os.execv(args[2], args[2:])
if name == 'open': sys.exit(0)
if args == ['--version']:
    print(name + ' offline-1'); sys.exit(0)
if name == 'codex' and args == ['login', 'status']: sys.exit(0)
if name == 'codex' and args == ['app-server']:
    for line in sys.stdin:
        r = json.loads(line)
        if 'id' not in r: continue
        if r['method'] == 'initialize': result = {}
        elif r['method'] == 'model/list':
            result = {'data':[{'model':'offline-model','hidden':False,
                      'supportedReasoningEfforts':[{'reasoningEffort':'high'}]}], 'nextCursor':None}
        elif r['method'] == 'account/rateLimits/read':
            result = {'rateLimits':{'primary':{'usedPercent':20,'windowDurationMins':300,
                      'resetsAt':2000000000},'planType':'offline'}}
        else: sys.exit(99)
        print(json.dumps({'id':r['id'],'result':result}), flush=True)
    sys.exit(0)
if name not in ('codex', 'opencode'): sys.exit(99)
flag = '-C' if name == 'codex' else '--dir'
run = pathlib.Path(args[args.index(flag)+1])
plan = json.loads((run/'输入/plan.json').read_text())
sys.stdin.read()
(run/'fake-start.json').write_text(json.dumps({'pid':os.getpid(),'started':time.monotonic()}))
gate = pathlib.Path(plan['gate'])
deadline = time.monotonic() + 20
while not gate.exists():
    if time.monotonic() > deadline: sys.exit(98)
    time.sleep(.025)
if plan.get('timeout'): time.sleep(120)
if plan.get('delay'): time.sleep(plan['delay'])
def emit(event): print(json.dumps(event, ensure_ascii=False), flush=True)
if plan.get('large'):
    # 单条完整合法事件，共约 8 MiB；不通过超限制造预期失败。
    output = 'tool output line\n' * (512*1024)
    emit({'type':'tool_use','part':{'tool':'bash','state':{'status':'completed',
          'input':{'command':'offline generated output'},'output':output}}})
if plan.get('overflow'):
    for i in range(70):
        sys.stderr.write('x '* (512*1024)); sys.stderr.flush()
if plan.get('fail'):
    emit({'type':'error','error':{'message':'offline simulated failure'}}); sys.exit(7)
result = '1. 结论：完成\n2. 产物：answer.txt\n3. 自检：固定样例一致\n4. 决策披露：离线替身\n5. 状态：完成\n'
(run/'answer.txt').write_text('42\n')
if name == 'codex':
    pathlib.Path(args[args.index('-o')+1]).write_text(result)
    emit({'type':'thread.started','thread_id':'offline-thread'})
    emit({'type':'turn.completed','usage':{'input_tokens':30,'output_tokens':20}})
else:
    emit({'type':'text','part':{'text':result}})
    emit({'type':'step_finish','part':{'tokens':{'input':30,'output':20,
          'cache':{'read':5,'write':2}},'cost':0.001}})
'''


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def tree(path):
    """只读取测试自建的假 HOME；内容用哈希，不回显文件正文。"""
    out = {}
    for p in sorted(path.rglob('*')):
        key = str(p.relative_to(path))
        if p.is_symlink(): out[key] = ['link', os.readlink(p)]
        elif p.is_dir(): out[key] = ['dir', p.stat().st_mode & 0o777]
        else: out[key] = ['file', hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mode & 0o777]
    return out


import http.client as http_client


def api(board, path):
    class Socket:
        def __init__(self, data): self.data, self.out = data, bytearray()
        def makefile(self, *args): return io.BytesIO(self.data)
        def sendall(self, data): self.out.extend(data)
    request = ('GET ' + path + ' HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nConnection: close\r\n\r\n' % board.PORT).encode()
    sock = Socket(request)
    board.Handler(sock, ('127.0.0.1', 12345), object())
    response = http_client.HTTPResponse(Socket(bytes(sock.out)))
    response.begin()
    return response.status, json.loads(response.read().decode('utf-8'))


class Lab:
    def __init__(self, framework):
        self.private = Path(tempfile.gettempdir()).resolve()
        self.framework = Path(framework).resolve()
        self.temp = tempfile.TemporaryDirectory(prefix='day-', dir=self.private)
        self.base = Path(self.temp.name).resolve()
        self.home, self.work = self.base/'home', self.base/'home/work'
        self.home.mkdir()
        self.fakebin = self.base/'fakebin'
        self.fakebin.mkdir()
        # 场景验证生命周期，不验证编译器的 ASCII 要求。将运行时临时目录
        # 固定在本夹具内，避免中文物理路径触发生产 /private/tmp 回退。
        hooks = self.base/'fixture_paths.py'
        hooks.write_text('import sys\nfrom pathlib import Path\n' +
            'sys.path.insert(0, ' + repr(str(self.framework/'bin')) + ')\n' +
            'from frclyo import isolation\n' +
            'base = Path(' + repr(str(self.base/'runtime-tmp')) + ')\n' +
            'def local_tmp(root, run, home=None):\n' +
            '    isolation.validate_run(root, run)\n' +
            '    return base / (Path(run).parent.name + "-" + Path(run).name)\n' +
            'isolation.codex_tmp = local_tmp\n' +
            'isolation.round_tmp = local_tmp\n', encoding='utf-8')
        for name in ('codex', 'opencode', 'claude', 'security', 'launchctl', 'open', 'sandbox-exec'):
            p = self.fakebin/name
            p.write_text(STUB, encoding='utf-8')
            p.chmod(0o700)
        # 网络接口替身：只替换 shell 的 proxy 桥接命令，不编辑框架副本。
        # 输出就绪端口和退出计数以满足执行器协议，绝不 bind/connect。
        # 其他所有 Python 调用（包括监督器和 finalize）原样 exec。
        python = self.fakebin/'python3'
        python.write_text('#!'+sys.executable+' -B\n'+r'''
import json, os, pathlib, runpy, signal, subprocess, sys, time
runpy.run_path(str(pathlib.Path(__file__).resolve().parent.parent/'fixture_paths.py'))
args = sys.argv[1:]
if len(args)>5 and args[:2]==['-I','-c'] and args[4]=='proxy':
    parent = int(args[7])
    stop = [False]
    for sig in (signal.SIGTERM,signal.SIGINT,signal.SIGHUP):
        signal.signal(sig, lambda *_: stop.__setitem__(0,True))
    print('19123',flush=True)
    while not stop[0] and os.getppid()==parent: time.sleep(.025)
    print(json.dumps({'拒绝次数':0}),flush=True)
    sys.exit(0)
if len(args)>5 and args[:2]==['-I','-c'] and args[4]=='supervise':
    # 测试环境不能读取全机 ps -axo；只提供本夹具的已知无子进程 worker。
    # 这不验证内核隔离或脱组后代回收；真实信号和 wait 仍由生产代码执行。
    base = pathlib.Path(__file__).resolve().parent.parent
    real_run = subprocess.run
    def fixture_ps(command, *a, **kw):
        if command and command[0]=='/bin/ps' and '-axo' in command:
            rows = [(os.getpid(),os.getppid(),os.getpgrp())]
            for f in [pathlib.Path(args[11]).parent/'fake-start.json']:
                try:
                    pid = json.loads(f.read_text())['pid']
                    os.kill(pid,0)
                except (OSError,ValueError,KeyError): continue
                rows.append((pid,os.getpid(),pid))
            if command[-1]=='pgid=,stat=':
                out = ''.join('%s S\n'%g for p,pp,g in rows)
            else:
                out = ''.join('%s %s %s S Sat Oct 31 00:00:00 2026\n'%(p,pp,g) for p,pp,g in rows)
            return subprocess.CompletedProcess(command,0,stdout=out,stderr='')
        return real_run(command,*a,**kw)
    subprocess.run = fixture_ps
    sys.argv = ['-c',*args[3:]]
    exec(args[2])
while args and args[0] in ('-I', '-B'): args.pop(0)
if args[0] == '-c':
    sys.argv = ['-c', *args[2:]]
    exec(args[1], {'__name__':'__main__'})
elif args[0] == '-':
    sys.argv = args
    exec(sys.stdin.read(), {'__name__':'__main__'})
else:
    sys.argv = args
    runpy.run_path(args[0], run_name='__main__')
''', encoding='utf-8')
        python.chmod(0o700)
        self.system = self.base/'system-state.json'
        dump(self.system, {'launchd':False,'keys':['preexisting:other']})
        # CLI 自有缓存是测试手造的 models.dev 样例，不复制任何真实账号材料。
        cache = self.home/'.cache/opencode/models.json'
        cache.parent.mkdir(parents=True)
        dump(cache, {'deepseek':{'id':'deepseek','api':'https://api.deepseek.com',
             'npm':'@ai-sdk/openai-compatible','models':{'deepseek-flash':{'id':'deepseek-flash'}}}})
        (self.home/'unrelated.txt').write_text('preexisting data\n')
        # 正常 macOS 用户已有 Library，不能把解释器创建的缓存误算成安装所有权。
        (self.home/'Library').mkdir()
        self.before = tree(self.home)
        self.env = {'HOME':str(self.home), 'FRCLYO_HOME':str(self.work),
                    'TMPDIR':str(self.private), 'PATH':str(self.fakebin)+':/usr/bin:/bin:/usr/sbin:/sbin',
                    'PYTHONDONTWRITEBYTECODE':'1', 'BOARD_NO_BACKGROUND':'1', 'BOARD_BALANCE':'0',
                    'CODEX_WORKER_FAST':'0', 'LANG':'en_US.UTF-8'}
        self.commands, self.checks, self.jobs, self.snapshots = [], [], [], {}
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(mock.patch.dict(os.environ, self.env, clear=True))
        self.oldpath = list(sys.path)
        self.oldmodules = {k:v for k,v in sys.modules.items() if k=='frclyo' or k.startswith('frclyo.')}
        for k in self.oldmodules: del sys.modules[k]
        sys.path.insert(0, str(self.framework/'bin'))
        import runpy
        runpy.run_path(str(hooks))
        self.board = None

    def close(self):
        for job in self.jobs:
            p = job['process']
            if p.poll() is None:
                p.terminate()
                try: p.wait(timeout=8)
                except subprocess.TimeoutExpired: p.kill(); p.wait(timeout=3)
            for handle in job['handles']: handle.close()
        self.stack.close()
        sys.path[:] = self.oldpath
        for k in list(sys.modules):
            if k=='frclyo' or k.startswith('frclyo.'): del sys.modules[k]
        sys.modules.update(self.oldmodules)
        self.temp.cleanup()

    def check(self, name, condition, actual=None):
        row = {'检查':name, '通过':bool(condition), '实际':actual}
        self.checks.append(row)
        print(('PASS ' if condition else 'FAIL ') + name + (': '+json.dumps(actual, ensure_ascii=False) if actual is not None else ''), flush=True)
        return condition

    def run(self, *args, input='', timeout=40, entry='aw'):
        path = self.framework/('bin/aw' if entry=='aw' else entry)
        cmd = ['/bin/bash', str(path), *map(str,args)]
        p = subprocess.run(cmd, env=self.env, cwd=self.base, input=input, text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        self.commands.append({'命令':shlex.join(cmd),'退出码':p.returncode,'stdout':p.stdout,'stderr':p.stderr})
        return p

    def install(self):
        p = self.run('--yes', entry='install.sh')
        self.check('install.sh --yes', p.returncode==0, p.returncode)
        if p.returncode: raise RuntimeError(p.stderr)
        # 安装后的正常配置选择：只启用本次替身所需的 DeepSeek。
        cfg = self.work/'配置/config.json'
        value = json.loads(cfg.read_text())
        value['providers'] = {'deepseek':value['providers']['deepseek']}
        dump(cfg, value)
        p = self.run('key','add','deepseek',input='y\n')
        self.check('fake security 注册', p.returncode==0, p.returncode)
        p = self.run('doctor')
        self.check('aw doctor', p.returncode==0, p.stdout)
        self.check('命令链接与假 launchd', (self.home/'.local/bin/aw').is_symlink() and json.loads(self.system.read_text())['launchd'])
        spec = importlib.util.spec_from_file_location('e2e_board', self.framework/'board/board.py')
        self.board = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.board)
        # 不运行余额后台线程；主动查询时只回固定模型接口结果。
        self.stack.enter_context(mock.patch.object(self.board, '_get_json', return_value={
            'balance_infos':[{'total_balance':'50','currency':'CNY'}], 'is_available':True}))
        self.board.refresh_health('codex')
        self.board.refresh_health('balances')

    def new(self, title):
        p = self.run('ledger','开卡','--项目','offline-day','--标题',title,'--来源','CL','--派活人','offline-Ray')
        if p.returncode: raise RuntimeError(p.stderr)
        return p.stdout.strip()

    def launch(self, task, worker='codex', **plan):
        nxt = self.run('ledger','下一轮',task)
        if nxt.returncode: raise RuntimeError(nxt.stderr)
        rnd = nxt.stdout.strip()
        gate = self.base/(task+'-'+rnd+'.gate')
        inputs = self.base/(task+'-'+rnd+'-input')
        inputs.mkdir()
        dump(inputs/'plan.json', dict(plan,gate=str(gate)))
        card = self.work/'任务卡'/(task+'-'+rnd+'.txt')
        card.write_text('项目：offline-day\n# '+task+' '+rnd+'\n离线端到端固定样例\n', encoding='utf-8')
        stdout = self.base/(task+'-'+rnd+'.stdout')
        stderr = self.base/(task+'-'+rnd+'.stderr')
        handles = (stdout.open('w'), stderr.open('w'))
        model = 'offline-model' if worker=='codex' else 'deepseek/deepseek-flash'
        budget = '12' if plan.get('timeout') else '35'
        cmd = ['/bin/bash',str(self.framework/'bin/aw'),'run',worker,task,rnd,str(card),model,
               'high' if worker=='codex' else '',budget]
        p = subprocess.Popen(cmd, cwd=self.base, env=dict(self.env,WORKER_INPUT_DIR=str(inputs)),
                             stdout=handles[0],stderr=handles[1])
        job = dict(task=task,round=rnd,worker=worker,gate=gate,process=p,handles=handles,
                   stdout=stdout,stderr=stderr,cmd=cmd,run=self.work/'交付'/task/rnd)
        self.jobs.append(job)
        return job

    def started(self, *jobs):
        until = time.monotonic()+20
        while time.monotonic()<until:
            if all((j['run']/'fake-start.json').exists() for j in jobs): return True
            if any(j['process'].poll() is not None for j in jobs): break
            time.sleep(.05)
        return False

    def finish(self, job):
        job['gate'].touch()
        p = job['process']
        p.wait(timeout=50)
        for handle in job['handles']: handle.flush()
        self.commands.append({'命令':shlex.join(job['cmd']),'退出码':p.returncode,
                             'stdout':job['stdout'].read_text(),'stderr':job['stderr'].read_text()})
        path = job['run']/'回执.json'
        job['receipt'] = json.loads(path.read_text()) if path.exists() else None
        return job['receipt']

    def state(self, name):
        # 让正常的 2 秒缓存自然失效；不修改生产缓存/时间。
        time.sleep(2.05)
        status, body = api(self.board, '/api/state')
        self.check(name+' HTTP /api/state', status==200, status)
        self.snapshots[name] = {'state':body, 'tasks':{}}
        return body

    def detail(self, name, task):
        status, body = api(self.board, '/api/task?id='+task)
        self.check(name+' HTTP /api/task '+task, status==200, status)
        self.snapshots[name]['tasks'][task] = body
        return body

    def verify_receipt(self, job, expected):
        rec = job['receipt']
        if not self.check(job['task']+'/'+job['round']+' 回执存在', bool(rec)): return
        control = self.work/'运行时/控制'/job['worker']/job['task']/job['round']
        trusted = json.loads((control/'receipt.json').read_text())
        self.check('两份回执相同', rec==trusted)
        self.check('回执身份、状态、退出码',
                   (rec['任务编号'],rec['轮次编号'],rec['状态'],rec['退出码']) ==
                   (job['task'],job['round'],expected,job['process'].returncode),
                   {'状态':rec['状态'],'退出码':rec['退出码'],'错误':rec['错误']})
        self.check('监督器确认回收', rec['进程组已回收'])
        result = job['run']/'结果.txt'
        if result.exists(): self.check('结果哈希', hashlib.sha256(result.read_bytes()).hexdigest()==rec['结果sha256'])
        pid = json.loads((job['run']/'fake-start.json').read_text())['pid'] if (job['run']/'fake-start.json').exists() else None
        if pid:
            try: os.kill(pid,0); alive=True
            except ProcessLookupError: alive=False
            self.check('替身进程退出', not alive, pid)
        copied = json.loads((job['run']/'输入/plan.json').read_text())
        self.check('输入已复制', bool(copied.get('gate')))

    def audit(self, label):
        from frclyo import audit
        report = audit.check()
        self.snapshots[label] = report
        print('AUDIT '+label+' '+json.dumps(report,ensure_ascii=False),flush=True)
        return report

    def artifact(self):
        return {'检查':self.checks, '命令':self.commands, '接口快照':self.snapshots,
                '说明':'真实产品源码未修改；fake CLI/security/launchctl/sandbox-exec；看板 HTTP 使用内存 socket；余额固定响应；proxy 桥接使用无 socket 替身；监督器 ps -axo 使用夹具进程表。未验证真实代理网络、内核隔离或脱组后代发现。'}


def scenario(lab, control=False):
    lab.install()
    a, b = lab.new('超时后放弃'), lab.new('大输出后完成')
    ja = lab.launch(a,timeout=True)
    jb = lab.launch(b,'opencode',large=True)
    ready = lab.started(ja,jb)
    lab.check('两个真实入口同时运行至 CLI 替身',ready)
    if not ready:
        for j in (ja,jb):
            if j['process'].poll() is not None: lab.finish(j)
        raise RuntimeError('某个工人未启动；参见 stderr 和临时代理日志，依赖该工人的检查未完成')
    s = lab.state('运行中')
    lab.check('运行中首页有两轮', {r['任务'] for r in s['正在干活']}=={a,b}, s['正在干活'])
    for j in (ja,jb):
        d = lab.detail('运行中',j['task'])
        lab.check('运行中详情',d['轮次'][-1]['状态']=='running',d['结论'])
        lab.check('运行中尚未发布公共日志',not (j['run']/'events.jsonl').exists())
        j['gate'].touch()
    lab.finish(ja); lab.finish(jb)
    lab.verify_receipt(ja,'超时'); lab.verify_receipt(jb,'执行完成')
    lab.check('大输出完整发布', (jb['run']/'events.jsonl').stat().st_size>8*1024*1024,
              (jb['run']/'events.jsonl').stat().st_size)
    s = lab.state('首轮结束')
    lab.check('首轮结束没有运行任务',not s['正在干活'])
    for task, verdict in ((a,'超时'),(b,'待验收')):
        d = lab.detail('首轮结束',task)
        lab.check('首轮任务结论',d['结论']==verdict,d['结论'])
    closing = [('ledger','放弃',a,'离线超时，放弃')]
    if not control: closing.append(('ledger','完成',b,'--结论','通过','--摘要','样例42一致'))
    for args in closing:
        p = lab.run(*args); lab.check('收卡 '+args[1],p.returncode==0,p.returncode)
    lab.check('收卡后日审',not lab.audit('收卡后日审')['问题'])
    s = lab.state('收卡后')
    for task, verdict in ((a,'已放弃'),(b,'待验收' if control else '通过')):
        d = lab.detail('收卡后',task)
        lab.check('收卡后结论',d['结论']==verdict,d['结论'])
    # 严格照任务卡顺序：收卡 → 下一轮 → 再派一轮，不改账本历史造状态。
    jc = lab.launch(b,fail=True)
    if not lab.started(jc): raise RuntimeError('第二轮替身未启动')
    s = lab.state('再派运行中')
    d = lab.detail('再派运行中',b)
    lab.check('再派时首页应显示正在运行', any(r['任务']==b for r in s['正在干活']),
              {'首页正在干活':s['正在干活'],'任务结论':d['结论'],'最新轮状态':d['轮次'][-1]['状态']})
    lab.check('新轮不能继承旧轮验收',d['轮次'][-1]['验收'] is None,d['轮次'][-1]['验收'])
    lab.finish(jc); lab.verify_receipt(jc,'失败')
    s = lab.state('再派结束')
    d = lab.detail('再派结束',b)
    lab.check('失败新轮不能显示已通过', d['结论']!='通过',d['结论'])
    report = lab.audit('再派后日审')
    lab.check('日审不沿用旧收卡',not report['问题'],report['问题'])
    if control:
        # 对照分支：失败卡在收卡前正常重试，r3 成功后再完成。
        jd = lab.launch(b)
        if not lab.started(jd): raise RuntimeError('对照分支第三轮未启动')
        lab.finish(jd); lab.verify_receipt(jd,'执行完成')
        p = lab.run('ledger','完成',b,'--结论','通过','--摘要','第三轮样例一致')
        lab.check('对照分支最后收卡',p.returncode==0)
        lab.check('对照分支日审无问题',not lab.audit('对照日审')['问题'])
    else:
        p = lab.run('ledger','放弃',b,'第二轮失败后收尾')
        lab.check('新轮可正常收卡',p.returncode==0,p.stderr)
    # 最新一轮收卡后，清理与卸载都能回收该任务的私有轮次。
    before = tree(lab.home)
    p = lab.run('clean','--days','0')
    lab.check('clean 预览只读',p.returncode==0 and tree(lab.home)==before,p.stdout)
    p = lab.run('clean','--days','0','--yes',input=str(lab.work)+'\n')
    lab.check('clean 真删',p.returncode==0,{'stdout':p.stdout,'stderr':p.stderr})
    lab.check('clean 保留交付物',all((j['run']/'回执.json').exists() for j in (ja,jb,jc)))
    before = tree(lab.home)
    system_before = lab.system.read_bytes()
    p = lab.run('uninstall','--dry-run')
    lab.check('uninstall 预演无变化',p.returncode==0 and tree(lab.home)==before and lab.system.read_bytes()==system_before)
    p = lab.run('uninstall',input='y\ny\n')
    lab.check('普通 uninstall',p.returncode==0,{'stdout':p.stdout,'stderr':p.stderr})
    lab.check('普通卸载保留配置和账本',(lab.work/'配置/config.json').exists() and (lab.work/'账本.jsonl').exists())
    p = lab.run('uninstall','--delete-data',input='y\n'+str(lab.work)+'\n')
    lab.check('delete-data 完成',p.returncode==0 and not lab.work.exists(),{'rc':p.returncode,'stdout':p.stdout,'stderr':p.stderr})
    after = tree(lab.home)
    lab.snapshots['恢复对比'] = {'before':lab.before,'after':after}
    lab.check('假机器恢复安装前状态', after==lab.before,
              {'新增或变化': sorted(k for k,v in after.items() if lab.before.get(k)!=v)[:20],
               '丢失':sorted(set(lab.before)-set(after))})
    lab.check('假系统服务和钥匙串恢复',json.loads(lab.system.read_text())=={'launchd':False,'keys':['preexisting:other']})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--framework',default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument('--report',type=Path)
    parser.add_argument('--control',action='store_true',help='额外对照：保持第二张卡在途，成功重试后才收卡')
    args = parser.parse_args()
    if os.environ.get('FRCLYO_E2E') != '1':
        print('SKIP: 完整场景请设 FRCLYO_E2E=1')
        return 0
    lab = None
    code = 2
    try:
        lab = Lab(args.framework)
        scenario(lab,control=args.control)
        code = 0 if all(c['通过'] for c in lab.checks) else 1
    except Exception as exc:
        print('BLOCKED '+type(exc).__name__+': '+str(exc),flush=True)
        if lab:
            lab.check('场景未完整完成',False,str(exc))
            # 代理失败的原始结果属于被测程序输出，记录后停止依赖项。
            for p in lab.work.glob('运行时/控制/*/*/*/代理.stderr'):
                lab.commands.append({'命令':'读取替身场景代理 stderr','退出码':None,'stdout':'','stderr':p.read_text()})
    finally:
        if lab:
            if args.report:
                report = args.report.resolve()
                if not report.is_relative_to(lab.private): raise RuntimeError('运行报告先写入口 TMPDIR，再拷回交付')
                dump(report,lab.artifact())
            passed = sum(c['通过'] for c in lab.checks)
            print('RESULT checks=%d passed=%d failed=%d exit=%d' % (len(lab.checks),passed,len(lab.checks)-passed,code),flush=True)
            lab.close()
    return code


if __name__=='__main__': sys.exit(main())
