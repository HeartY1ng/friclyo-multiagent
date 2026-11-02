"""只在入口 TMPDIR 的源码副本运行；不读配置、不改 HOME、不联网。"""
import contextlib
import importlib.util
import io
import http.client as http_client
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.gettempdir()).resolve()
sys.path.insert(0, str(ROOT / 'bin'))
from frclyo import config

@contextlib.contextmanager
def board_fixture():
    with tempfile.TemporaryDirectory(prefix='fixture-', dir=TMP) as tmp:
        base = Path(tmp).resolve()
        paths = {name:str(base/name) for name in ('deliver_dir','cards_dir','ledger_file','perf_file','board_log_file','config_dir')}
        for key in ('deliver_dir','cards_dir','config_dir'):
            Path(paths[key]).mkdir()
        spec = importlib.util.spec_from_file_location('review_board', ROOT/'board/board.py')
        b = importlib.util.module_from_spec(spec)
        with patch.object(config,'load',return_value={'board_port':18799,'codex':{},'providers':{}}), patch.object(config,'path_of',side_effect=lambda k:paths[k]):
            spec.loader.exec_module(b)
            # 不启动后台线程；探测 ls 与实际进程存活检查也不需要。
            with patch.object(b,'probe_deliver',return_value=None), patch.object(b,'proc_alive',return_value=False), \
                 patch.object(b,'release_probe',wraps=b.release_probe,return_value={'rows': None, 'issue': '测试未探测'}):
                yield b, base

def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False),encoding='utf-8')

def card(b, task='CL-20991231-01'):
    event=b.ledger.event('开卡',task,标题='中文任务',项目='测试项目',来源='CL',派活人='替身',父任务='')
    Path(b.LEDGER).write_text(json.dumps(event,ensure_ascii=False)+'\n',encoding='utf-8')
    return task

def http(b, path):
    class Socket:
        def __init__(self, data): self.data=data; self.out=bytearray()
        def makefile(self,*args): return io.BytesIO(self.data)
        def sendall(self,data): self.out.extend(data)
    sock=Socket(('GET '+path+' HTTP/1.1\r\nHost: 127.0.0.1:18799\r\nConnection: close\r\n\r\n').encode())
    b.Handler(sock,('127.0.0.1',12345),object())
    response=http_client.HTTPResponse(Socket(bytes(sock.out)))
    response.begin()
    return response.status, response.read().decode()

PRELUDE=r'''
const assert = require('assert');
const elements = {};
const document = {hidden:false, getElementById:id => elements[id] || (elements[id] = {
 innerHTML:'',textContent:'',addEventListener(){},setAttribute(){},
 classList:{add(){},remove(){},toggle(){}},scrollHeight:0,scrollTop:0,clientHeight:0,
 insertAdjacentHTML(where,html){this.innerHTML=html+this.innerHTML;}}),
 addEventListener(){},querySelectorAll:()=>[]};
const window={addEventListener(){}};
const localStorage={getItem:()=>null,setItem(){}};
const history={replaceState(){}};
const location={pathname:'/',hash:''};
const timers=new Map(); let nextTimer=1;
const setTimeout=(fn,ms)=>{const id=nextTimer++;timers.set(id,{fn,ms});return id;};
const clearTimeout=id=>timers.delete(id);
const setInterval=()=>0;
const requests=[];
const fetch=(url,opts)=>new Promise((resolve,reject)=>requests.push({url,opts,resolve,reject}));
const reply=(index,data,status=200)=>requests[index].resolve({ok:status<400,status,json:async()=>data});
const state=name=>({项目:name,项目列表:[{名字:'甲'},{名字:'乙'}],健康:{},要你处理:[],正在干活:[],最近完成:[]});
const detail=(id,round='r1')=>({任务:id,标题:id,项目:'甲',结论:'待验收',当前轮次:round,轮次:[{
 轮次:round,状态:'done',开始:1,结束:2,时间线:[],文件:[]}]});
'''

def run_js(case, checks):
    node=shutil.which('node')
    if not node: raise unittest.SkipTest('缺少 Node，跳过实际前端脚本回归测试')
    html=(ROOT/'board/index.html').read_text(encoding='utf-8')
    js=html.split('<script>',1)[1].split('</script>',1)[0]
    js=js[:js.rindex('readHash();')]
    source=PRELUDE+js+'\n(async()=>{\n'+checks+'\n})().catch(e=>{console.error(e.name+": "+e.message);process.exitCode=1;});\n'
    result=subprocess.run([node,'-e',source],capture_output=True,text=True,timeout=10)
    print(case+' node stdout:\n'+result.stdout,end='')
    print(case+' node stderr:\n'+result.stderr,end='')
    print('node exit='+str(result.returncode))
    return result
