"""批量派活：先整批校验，再按空闲槽启动已有工人脚本；不替大脑验收。"""
import argparse
import fcntl
import json
import math
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

from . import config, ledger, models
from .execution import directory, safe_read
from .isolation import validate_run

BIN = Path(__file__).resolve().parents[1]
SWITCHES = {'网页': 'CODEX_WORKER_WEB', '命令网络': 'CODEX_WORKER_NET',
            '子agent': 'CODEX_WORKER_SUBAGENTS', '快速档': 'CODEX_WORKER_FAST'}


def weekly_quota(result):
    """按 10080 分钟识别周窗口；primary 可能只是五小时，不能按位置猜。"""
    rate = result.get('rateLimits') if isinstance(result, dict) else None
    if isinstance(rate, dict):
        for slot in ('primary', 'secondary'):
            window = rate.get(slot)
            if not isinstance(window, dict) or window.get('windowDurationMins') != 10080:
                continue
            used = window.get('usedPercent')
            if type(used) not in (int, float) or not math.isfinite(used) or not 0 <= used <= 100:
                raise ValueError('周额度百分比无效')
            return {'已用': used, '重置': window.get('resetsAt'), '窗口分钟': 10080,
                    '档位': slot, '时间': time.time()}
    raise ValueError('额度响应没有可识别的周窗口')


def read_quota(cfg, timeout=20):
    """沿用看板 initialize → rateLimits/read 协议；读写均限时，不读登录文件。"""
    env = dict(os.environ)
    for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
        env.pop(key, None)
    proxy = cfg['codex'].get('proxy') or ''
    if proxy:
        env.update(HTTP_PROXY=proxy, HTTPS_PROXY=proxy, http_proxy=proxy, https_proxy=proxy)
    process = None
    try:
        deadline = time.monotonic() + timeout
        process = subprocess.Popen(['codex', 'app-server'], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   env=env, start_new_session=True)
        os.set_blocking(process.stdin.fileno(), False)
        os.set_blocking(process.stdout.fileno(), False)
        pending = bytearray()
        with selectors.DefaultSelector() as reader, selectors.DefaultSelector() as writer:
            reader.register(process.stdout, selectors.EVENT_READ)
            writer.register(process.stdin, selectors.EVENT_WRITE)

            def send(message):
                # 非阻塞写避免 CLI 不读 stdin 时绕过总超时。
                data = memoryview((json.dumps(message) + '\n').encode())
                while data:
                    left = deadline - time.monotonic()
                    if left <= 0 or not writer.select(left):
                        raise TimeoutError()
                    try:
                        count = os.write(process.stdin.fileno(), data)
                    except (BlockingIOError, InterruptedError):
                        continue
                    if not count:
                        raise BrokenPipeError()
                    data = data[count:]

            def receive(request_id):
                # 通知、半行、无关响应共用截止时间和缓冲上限。
                while True:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise TimeoutError()
                    if b'\n' in pending:
                        line, _, rest = pending.partition(b'\n')
                        pending[:] = rest
                        data = json.loads(line)
                        if not isinstance(data, dict) or data.get('id') != request_id:
                            continue
                        if 'error' in data:
                            raise ValueError('额度 RPC 错误')
                        return data.get('result')
                    if not reader.select(left):
                        raise TimeoutError()
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        raise ValueError('额度查询提前退出')
                    pending.extend(chunk)
                    if len(pending) > 1_000_000:
                        raise ValueError('额度响应过大')

            send({'id': 1, 'method': 'initialize', 'params': {
                'clientInfo': {'name': 'frclyo-dispatch', 'version': '0.1'}}})
            receive(1)
            send({'method': 'initialized'})
            send({'id': 2, 'method': 'account/rateLimits/read'})
            return weekly_quota(receive(2))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        # 只记异常种类，不把 RPC 或环境中的敏感原文带入批次日志。
        return {'错误': '周额度不可用（%s）' % type(exc).__name__, '时间': time.time()}
    finally:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            finally:
                process.wait(timeout=1)
                process.stdin.close()
                process.stdout.close()


def validate_queue(raw, cfg):
    """只读校验并收集所有条目的错误；本函数不创建任何批次文件。"""
    if not isinstance(raw, list):
        return [], ['队列必须是 JSON 列表']
    cards = ledger.fold(ledger.read_events(Path(cfg['ledger_file'])))
    jobs, errors, seen = [], [], set()
    root = Path(cfg['work_dir'])
    allowed = {'任务', '轮', '工人', '模型', '档位', '变体', '超时', '卡', '输入', *SWITCHES}
    catalogs, codex_version = {}, None
    for number, raw_job in enumerate(raw, 1):
        problems = []
        if not isinstance(raw_job, dict):
            errors.append('第 %d 项：必须是对象' % number)
            continue
        job = dict(raw_job)
        if set(job) - allowed:
            problems.append('未知字段：' + '、'.join(sorted(set(job) - allowed)))
        task, rnd = job.get('任务'), job.get('轮', 'r1')
        worker, model = job.get('工人'), job.get('模型')
        valid_names = all(isinstance(v, str) and re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_-]*', v)
                          for v in (task, rnd))
        if not valid_names:
            problems.append('任务/轮只能含字母数字和 -_，且不能以 - 开头')
        else:
            if (task, rnd) in seen:
                problems.append('同一任务/轮重复')
            seen.add((task, rnd))
            if task not in cards:
                problems.append('任务未在账本开卡')
            elif cards[task]['状态'] in ledger.TERMINAL:
                problems.append('任务已收卡；需大脑先恢复为在途任务')
            run = Path(cfg['deliver_dir']) / task / rnd
            try:
                validate_run(root, run)
                if os.path.lexists(run):
                    problems.append('轮次路径已存在（包括文件或断链）')
            except (OSError, ValueError):
                problems.append('轮次路径不是交付下的真实目录路径')
        if worker not in ('codex', 'opencode'):
            problems.append('工人必须是 codex / opencode')
        if not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9._/-]*', model):
            problems.append('模型必须为有效的非空名称')
        elif worker == 'opencode' and not re.fullmatch(r'[a-z0-9-]+/[A-Za-z0-9._-]+', model):
            problems.append('OpenCode 模型应为 家/模型')
        if '档位' not in job and '变体' not in job:
            problems.append('必须显式提供档位或变体（OpenCode 默认变体可写空串）')
        level = job.get('档位', job.get('变体'))
        if '档位' in job and '变体' in job and job['档位'] != job['变体']:
            problems.append('档位和变体不能冲突')
        if not isinstance(level, str) or not re.fullmatch(r'[a-z]*', level) or (worker == 'codex' and not level):
            problems.append('档位/变体必须为小写字母；Codex 不可为空')
        if type(job.get('超时')) is not int or job['超时'] <= 0:
            problems.append('超时必须为正整数秒')
        for key in SWITCHES:
            value = job.get(key, 0)
            if type(value) not in (bool, int) or value not in (0, 1):
                problems.append(key + '必须为 0/1 或布尔值')
            elif worker == 'opencode' and value:
                problems.append('现有 OpenCode 入口不支持开启' + key)
            job[key] = value
        if worker in ('codex', 'opencode') and isinstance(model, str) and isinstance(level, str):
            if worker not in catalogs:
                try:
                    catalogs[worker] = models.dispatch_catalog(worker, cfg)
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    catalogs[worker] = None
                    print('警告：%s 模型/档位清单不可读（%s）；本次不拦截清单校验，请人工核对' %
                          (worker, type(exc).__name__), file=sys.stderr)
            catalog = catalogs[worker]
            if catalog is not None:
                if model not in catalog:
                    problems.append('%s 不认识的模型：%s' % (worker, model))
                elif level not in catalog[model]:
                    # 空串是 OpenCode 的默认变体，直接拼接会显示成空白，所以写明
                    problems.append('%s 不支持档位/变体 %s；可用：%s' %
                                    (model, level, '/'.join(v or '默认（空串）' for v in catalog[model]) or '空串'))
            if worker == 'codex':
                if level == 'ultra' and job['子agent'] != 1:
                    problems.append('ultra 档须同时开启子agent=1')
                if job['子agent'] == 1 and model != 'gpt-5.6-luna' and os.environ.get('CODEX_WORKER_SUBAGENTS_FORCE') != '1':
                    if codex_version is None:
                        codex_version = models.cli_version('codex')
                    if re.fullmatch(r'0\.153\.[0-9]+(?:[.+-].*)?', codex_version):
                        problems.append('Codex %s 的 exec 非 luna 子 agent 存在 no thread with id / 伪报成功问题；'
                                        '请使用 gpt-5.6-luna 或关闭子agent；'
                                        'CODEX_WORKER_SUBAGENTS_FORCE=1 可跳过此检查' % codex_version)
        # 默认路径相对数据根；显式相对路径也采用同一规则，不受调用 cwd 影响。
        if valid_names:
            for key, default in (('卡', '任务卡/%s-%s.txt' % (task, rnd)), ('输入', '素材/' + task)):
                value = job.get(key, default)
                if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
                    problems.append(key + '路径必须是非空文字且不含控制字符')
                    continue
                path = Path(value).expanduser()
                path = path if path.is_absolute() else root / path
                job[key] = str(path.absolute())
                if key == '卡' and not path.is_file():
                    problems.append('卡文件不存在或不是普通文件：' + str(path))
                if key == '输入' and not path.is_dir():
                    problems.append('输入目录不存在：' + str(path))
        job.update(轮=rnd, 档位=level)
        if problems:
            errors.extend('第 %d 项：%s' % (number, reason) for reason in problems)
        else:
            jobs.append(job)
    return jobs, errors


def worker_command(job):
    """只拼现有脚本参数；权限、输入复制、监督和回执仍由脚本负责。"""
    env = dict(os.environ, WORKER_INPUT_DIR=job['输入'])
    env.update({variable: str(int(job[key])) for key, variable in SWITCHES.items()})
    return ([str(BIN / (job['工人'] + '-worker.sh')), job['任务'], job['轮'], job['卡'],
             job['模型'], job['档位'], str(job['超时'])], env)


def note_start(job, cfg, batch):
    """启动瞬间再查账本和占用；只追加备注，不开卡、不恢复、不收卡。"""
    path = Path(cfg['ledger_file'])
    with ledger.LedgerLock(path) as lock:
        ledger.require_card(ledger.fold(ledger.read_events(path)), job['任务'], active=True)
        run = Path(cfg['deliver_dir']) / job['任务'] / job['轮']
        validate_run(cfg['work_dir'], run)
        if os.path.lexists(run):
            raise FileExistsError('轮次在校验后被占用')
        lock.append([ledger.event('备注', job['任务'], 内容='批次 %s 派出 %s' % (batch, job['轮']))])


def new_row(job, status, before=None):
    """未启动项也保留相同列，退出码为 null，避免被误算成成功。"""
    return dict({key: job[key] for key in ('任务', '轮', '工人', '模型', '档位')},
                状态=status, 退出码=None, 用时秒=0, 回执状态=None, 用量=None,
                额度前=before, 额度后=None)


def read_receipt(job, cfg):
    """优先可信控制目录回执；准备失败可能只有公开副本，读取时拒绝链接/FIFO。"""
    paths = [Path(cfg['work_dir']) / '运行时/控制' / job['工人'] / job['任务'] / job['轮'] / 'receipt.json',
             Path(cfg['deliver_dir']) / job['任务'] / job['轮'] / '回执.json']
    for path in paths:
        try:
            receipt = json.loads(safe_read(path, 1_000_000))
        except FileNotFoundError:
            continue
        if not isinstance(receipt, dict) or any(receipt.get(field) != job[key] for field, key in (
                ('任务编号', '任务'), ('轮次编号', '轮'), ('工人', '工人'), ('模型', '模型'))):
            raise ValueError('回执身份不匹配')
        # 拒绝非 JSON 数字和无法编码的文本，防止坏回执让整批日志追加失败。
        json.dumps(receipt, ensure_ascii=False, allow_nan=False).encode('utf-8')
        return receipt
    raise ValueError('缺少回执')


def token_count(row):
    """统一两种回执口径；Codex 缓存/推理是子集，OpenCode 缓存单列计入。"""
    usage = row.get('用量')
    if not isinstance(usage, dict):
        return 0
    def count(key):
        value = usage.get(key, 0)
        return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else 0
    if row['工人'] == 'codex':
        return count('input_tokens') + count('output_tokens')
    if type(usage.get('total')) in (int, float):
        return count('total')
    return sum(count(key) for key in ('input', 'output', 'reasoning', 'cached_input_tokens', 'cache_write_input_tokens'))


def provider_lock(parent, family):
    """同一数据根的多个派活批次也共用家锁；忙时跳过候选，让其他家先跑。"""
    fd = os.open('.provider-' + family + '.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                 0o600, dir_fd=parent)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('提供方锁不是普通文件')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BlockingIOError:
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise


def print_summary(rows, batch, interrupted):
    """完成指执行成功，不是大脑验收；每项和分类合计同时打印。"""
    print('\n批次 %s%s' % (batch, '（被中断）' if interrupted else ''))
    print('任务\t轮\t工人\t状态\t退出码\t用时秒\ttoken')
    for row in rows:
        print('\t'.join(str(v) for v in (row['任务'], row['轮'], row['工人'], row['状态'],
              row['退出码'], row['用时秒'], token_count(row))))
    states = {state: sum(row['状态'] == state for row in rows)
              for state in ('完成', '失败', '额度跳过', '被中断')}
    print('完成 {完成} | 失败 {失败} | 额度跳过 {额度跳过} | 被中断 {被中断} | token 合计 {tokens}'.format(
        **states, tokens=sum(token_count(row) for row in rows)))


def batch_info(jobs, parallel, limit):
    return {'批次号': datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:12],
            '开始时间': datetime.now(timezone.utc).isoformat(), '总件数': len(jobs),
            '并行数': parallel, '额度上限': limit,
            '任务': [{key: job.get(key, default) if isinstance(job.get(key, default), str) else None
                      for key, default in (('任务', None), ('轮', 'r1'))}
                     if isinstance(job, dict) else {'任务': None, '轮': None} for job in jobs]}


def save_batch(parent, info):
    """同目录独占临时文件 + 原子替换；旁路信息不混入原 JSONL 行。"""
    name = info['批次号'] + '.批次.json'
    temporary = '.' + name + '-' + uuid.uuid4().hex
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o600, dir_fd=parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(info, stream, ensure_ascii=False, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass


def validation_failed(raw, cfg, parallel, limit):
    info = batch_info(raw if isinstance(raw, list) else [], parallel, limit)
    info.update(结束时间=datetime.now(timezone.utc).isoformat(), 结束状态='校验失败')
    with directory(Path(cfg['work_dir']) / '运行时/派活', create=True) as parent:
        save_batch(parent, info)


def dispatch(jobs, cfg, parallel, limit):
    """单调度线程串行决定启动和写日志，已启动子进程在独立会话中继续受监督。"""
    info = batch_info(jobs, parallel, limit)
    batch = info['批次号']
    output = Path(cfg['work_dir']) / '运行时/派活'
    pending, running, rows = list(jobs), [], []
    interrupted, quota_stop = False, None
    def stop(signum, frame):
        # Ctrl-C / SIGTERM 只置位；不向工人转发信号，也不提前退出让监督器误判父进程死亡。
        nonlocal interrupted
        interrupted = True
    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        with directory(output, create=True) as parent:
            save_batch(parent, info)
            fd = os.open(batch + '.jsonl', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=parent)
            with os.fdopen(fd, 'w', encoding='utf-8') as journal:
                print('批次记录：' + str(output / (batch + '.jsonl')), flush=True)
                def save(row):
                    row['批次'] = batch
                    row['批次被中断'] = interrupted
                    journal.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n')
                    journal.flush()
                    os.fsync(journal.fileno())
                    rows.append(row)
                while pending or running:
                    # 先结算完成项以释放并行槽与家锁；回执坏了也必须记录失败并继续。
                    for active in list(running):
                        process, job, start, before, family_fd = active
                        rc = process.poll()
                        if rc is None:
                            continue
                        running.remove(active)
                        if family_fd is not None:
                            os.close(family_fd)
                        row = new_row(job, '失败', before)
                        row.update(退出码=rc, 用时秒=round(time.monotonic() - start, 3))
                        try:
                            receipt = read_receipt(job, cfg)
                            row.update(回执状态=receipt.get('状态'), 用量=receipt.get('用量'))
                            if rc == 0 and receipt.get('退出码') == 0 and receipt.get('状态') == '执行完成':
                                row['状态'] = '完成'
                        except (OSError, ValueError) as exc:
                            row['错误'] = '回执不可用（%s）' % type(exc).__name__
                        if job['工人'] == 'codex':
                            row['额度后'] = read_quota(cfg)
                            after = row['额度后']
                            if quota_stop is None and '错误' not in after and after['已用'] >= limit:
                                quota_stop = after
                        save(row)
                    for job in list(pending):
                        if interrupted:
                            pending.remove(job)
                            save(new_row(job, '被中断'))
                            continue
                        if job['工人'] == 'codex' and quota_stop is not None:
                            pending.remove(job)
                            save(new_row(job, '额度跳过', quota_stop))
                            continue
                        if len(running) >= parallel:
                            break
                        family_fd, before = None, None
                        if job['工人'] == 'opencode':
                            family = job['模型'].split('/')[0]
                            # 运行列表负责本批互斥，文件锁负责跨批互斥，避免重复取同一把锁。
                            if any(item[1]['工人'] == 'opencode' and
                                   item[1]['模型'].split('/')[0] == family for item in running):
                                continue
                            try:
                                family_fd = provider_lock(parent, family)
                            except (OSError, ValueError) as exc:
                                pending.remove(job)
                                row = new_row(job, '失败')
                                row['错误'] = '提供方锁不可用（%s）' % type(exc).__name__
                                save(row)
                                continue
                            if family_fd is None:
                                continue
                        else:
                            before = read_quota(cfg)
                            if interrupted:
                                continue
                            if '错误' in before:
                                pending.remove(job)
                                row = new_row(job, '失败', before)
                                row['错误'] = '未取得周额度，未启动；请核对后重派'
                                save(row)
                                continue
                            if before['已用'] >= limit:
                                quota_stop = before
                                pending.remove(job)
                                save(new_row(job, '额度跳过', before))
                                continue
                        pending.remove(job)
                        start = time.monotonic()
                        try:
                            # 每轮输出进入独占文件，不混进表格；stdin 不继承终端。
                            cmd, env = worker_command(job)
                            # 加任务名长度，避免“父任务/A-r1”和“子任务-A/r1”拼接后撞名。
                            log_name = '%s-%d-%s-%s.log' % (batch, len(job['任务']), job['任务'], job['轮'])
                            log_fd = os.open(log_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                             0o600, dir_fd=parent)
                            with os.fdopen(log_fd, 'wb') as log:
                                note_start(job, cfg, batch)
                                if interrupted:
                                    save(new_row(job, '被中断', before))
                                    continue
                                process = subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL,
                                                           stdout=log, stderr=log, start_new_session=True)
                            running.append((process, job, start, before, family_fd))
                            family_fd = None  # 锁转交运行项，等脚本结束再释放。
                        except (OSError, ValueError, ledger.LedgerError) as exc:
                            row = new_row(job, '失败', before)
                            row['错误'] = '启动失败（%s）' % type(exc).__name__
                            save(row)
                        finally:
                            if family_fd is not None:
                                os.close(family_fd)
                    if running or pending:
                        time.sleep(0.05)
            info.update(结束时间=datetime.now(timezone.utc).isoformat(),
                        结束状态='被中断' if interrupted else '完成')
            save_batch(parent, info)
    finally:
        # 日志写入等宿主错误也不能让已启动脚本突然失去父进程；只等待，不代替监督器发信号。
        for process, job, start, before, family_fd in running:
            try:
                process.wait()
            finally:
                if family_fd is not None:
                    os.close(family_fd)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    print_summary(rows, batch, interrupted)
    return 130 if interrupted else int(any(row['状态'] != '完成' for row in rows))


def main(args=None):
    """帮助不加载配置；试跑只校验和展示计划，不查额度、不写备注、不建目录。"""
    parser = argparse.ArgumentParser(prog='aw dispatch', description='一次派一批；中文别名：aw 派活')
    parser.add_argument('queue', metavar='队列.json')
    parser.add_argument('--并行', '--parallel', dest='parallel', type=int, default=3)
    parser.add_argument('--额度上限', '--quota-limit', dest='limit', type=float, default=95)
    parser.add_argument('--试跑', '--dry-run', dest='dry_run', action='store_true')
    options = parser.parse_args(args)
    if options.parallel < 1 or not math.isfinite(options.limit) or not 0 <= options.limit <= 100:
        parser.error('并行必须为正整数，额度上限必须在 0～100 之间')
    try:
        cfg = config.load()
        queue_path = Path(options.queue)
        try:
            queue_text = queue_path.read_text(encoding='utf-8')
        except FileNotFoundError as exc:
            print('队列文件不存在：%s' % (exc.filename or queue_path), file=sys.stderr)
            if not options.dry_run:
                validation_failed(None, cfg, options.parallel, options.limit)
            return 2
        try:
            raw = json.loads(queue_text)
        except json.JSONDecodeError as exc:
            print('队列 JSON 语法错误：第 %d 行，第 %d 列' % (exc.lineno, exc.colno), file=sys.stderr)
            if not options.dry_run:
                validation_failed(None, cfg, options.parallel, options.limit)
            return 2
        jobs, errors = validate_queue(raw, cfg)
        if errors:
            if not options.dry_run:
                validation_failed(raw, cfg, options.parallel, options.limit)
            print('整批未启动：\n' + '\n'.join(errors), file=sys.stderr)
            return 2
        if options.dry_run:
            print('试跑：%d 件，并行 %d，Codex 周额度上限 %g%%；OpenCode 同家串行' % (
                len(jobs), options.parallel, options.limit))
            for job in jobs:
                print(json.dumps(job, ensure_ascii=False))
            return 0
        return dispatch(jobs, cfg, options.parallel, options.limit)
    except (OSError, ValueError, ledger.LedgerError) as exc:
        filename = getattr(exc, 'filename', None)
        detail = '：%s' % filename if filename else ''
        print('派活失败（%s%s）；请检查队列、账本和路径权限' % (type(exc).__name__, detail), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
