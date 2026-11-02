"""优先询问本机 CLI 发现模型候选；列出不代表实际执行已验证可用。"""
import contextlib
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import selectors
import signal
import sys
import time
import subprocess
import tempfile
from . import config, keys
from .worker import read_jsonc, worker_environment


def cli_version(worker):
    """版本来自实际 CLI，不能使用 App 写入的缓存版本。查询失败不复用旧结论。"""
    if worker not in ("codex", "opencode"):
        raise ValueError("未知工人")
    try:
        result = subprocess.run([worker, "--version"], capture_output=True, text=True, encoding='utf-8', timeout=10,
                                env=worker_environment() if worker == "opencode" else None)
        lines = result.stdout.strip().splitlines()
        if result.returncode == 0 and lines:
            return lines[0].split()[-1]
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        pass
    return "unknown"


def codex_cli_models(timeout=20):
    """限时 stdio JSON-RPC 查询；所有分页共用截止时间，所有出口回收进程组。"""
    deadline = time.monotonic() + timeout
    process = subprocess.Popen(['codex', 'app-server'], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               start_new_session=True)
    try:
        os.set_blocking(process.stdout.fileno(), False)
        os.set_blocking(process.stdin.fileno(), False)
        pending = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)

            def send(message):
                data = memoryview((json.dumps(message) + '\n').encode('utf-8'))
                with selectors.DefaultSelector() as writer:
                    writer.register(process.stdin, selectors.EVENT_WRITE)
                    while data:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0 or not writer.select(remaining):
                            raise TimeoutError('app-server 模型查询超时')
                        try:
                            count = os.write(process.stdin.fileno(), data)
                        except (BlockingIOError, InterruptedError):
                            continue
                        if count == 0:
                            raise BrokenPipeError('app-server 写入提前结束')
                        data = data[count:]

            def receive(request_id):
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError('app-server 模型查询超时')
                    if b'\n' in pending:
                        line, _, rest = pending.partition(b'\n')
                        pending[:] = rest
                        data = json.loads(line)
                        if not isinstance(data, dict) or data.get('id') != request_id:
                            continue
                        if 'error' in data:
                            raise ValueError('app-server 模型查询 RPC 错误')
                        return data.get('result')
                    if not selector.select(remaining):
                        raise TimeoutError('app-server 模型查询超时')
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        raise ValueError('app-server 提前退出')
                    pending.extend(chunk)
                    if len(pending) > 1_000_000:
                        raise ValueError('app-server 响应过大')

            send({'id': 1, 'method': 'initialize', 'params': {
                'clientInfo': {'name': 'frclyo-models', 'version': '0.1'}}})
            receive(1)
            send({'method': 'initialized'})
            rows, cursors, cursor, request_id = [], set(), None, 2
            while True:
                send({'id': request_id, 'method': 'model/list',
                      'params': {'includeHidden': False, 'cursor': cursor}})
                result = receive(request_id)
                if not isinstance(result, dict) or not isinstance(result.get('data'), list):
                    raise ValueError('app-server 模型列表格式无效')
                for row in result['data']:
                    if (not isinstance(row, dict) or not isinstance(row.get('model'), str)
                            or not row['model'] or not isinstance(row.get('hidden'), bool)
                            or not isinstance(row.get('supportedReasoningEfforts'), list)):
                        raise ValueError('app-server 模型条目格式无效')
                    levels = row['supportedReasoningEfforts']
                    if any(not isinstance(item, dict) or not isinstance(item.get('reasoningEffort'), str)
                           or not item['reasoningEffort'] for item in levels):
                        raise ValueError('app-server 思考档位格式无效')
                    rows.append({'slug': row['model'], 'visibility': 'hide' if row['hidden'] else 'list',
                                 'supported_reasoning_levels': [{'effort': item['reasoningEffort']} for item in levels]})
                cursor = result.get('nextCursor')
                if cursor is None:
                    return rows
                if not isinstance(cursor, str) or not cursor or cursor in cursors:
                    raise ValueError('app-server 分页游标无效')
                cursors.add(cursor)
                request_id += 1
    finally:
        # 清理失败不覆盖查询异常；wait 也必须有界，避免拒绝发信号时再次卡住。
        original_error = sys.exc_info()[0] is not None
        cleanup_error = None
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as exc:
            cleanup_error = exc
        for cleanup in (lambda: process.wait(timeout=0.2),
                        process.stdin.close, process.stdout.close):
            try:
                cleanup()
            except (OSError, subprocess.SubprocessError) as exc:
                cleanup_error = cleanup_error or exc
        if cleanup_error is not None and not original_error:
            raise cleanup_error


def codex_catalog(version=None, *, timeout=20, missing_ok=False):
    """统一清单与来源；异版缓存仅供发现候选，不作为执行拦截依据。"""
    try:
        return codex_cli_models(timeout), '命令行'
    except (OSError, ValueError) as exc:
        print('⚠️ 命令行模型查询失败（' + type(exc).__name__ + '），退回模型缓存', file=sys.stderr)
    path = config.codex_home() / 'models_cache.json'
    try:
        catalog = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        if not missing_ok:
            raise ValueError('读不到 Codex 模型清单 ' + str(path))
        catalog = {'models': []}
    if not isinstance(catalog, dict) or not isinstance(catalog.get('models'), list):
        raise ValueError('Codex 模型缓存格式无效；请刷新 CLI 登录和模型缓存')
    version = cli_version('codex') if version is None else version
    if version != 'unknown' and catalog.get('client_version') == version:
        source = '缓存（版本一致）'
    else:
        source = '缓存（版本不同，未拦）'
        print(f'⚠️ 模型清单是 Codex {catalog.get("client_version")} 写的，本机命令行是 {version}，'
              '可用模型可能对不上；仅提醒、不拦截，由 codex 自行验证', file=sys.stderr)
    return catalog['models'], source


def check_codex_model(model, effort, subagents, version):
    rows, source = codex_catalog(version)
    if source != '缓存（版本不同，未拦）':
        listed = {row['slug']: row for row in rows if isinstance(row, dict)
                  and row.get('visibility') == 'list' and isinstance(row.get('slug'), str)}
        if model not in listed:
            raise ValueError(f'不认识的模型：{model}。可用：{"、".join(listed)}')
        levels = [item['effort'] for item in listed[model].get('supported_reasoning_levels') or []
                  if isinstance(item, dict) and isinstance(item.get('effort'), str)]
        if effort not in levels:
            raise ValueError(f'{model} 不支持思考档位 {effort}。可用：{"/".join(levels)}')
    # 这是派工安全开关，不随缓存版本不一致而放宽。
    if effort == 'ultra' and subagents != '1':
        raise ValueError('ultra 档会自动把任务分给子 agent，要同时设 CODEX_WORKER_SUBAGENTS=1 才放行')
    return source


def _path():
    return Path(config.path_of("availability_file"))


@contextlib.contextmanager
def _locked(path, write=False):
    """稳定的旁路 flock 覆盖读/改/原子替换；离线首次发现不创建工作目录。"""
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
    try:
        stream = path.with_suffix(".lock").open("a+b" if write else "rb")
    except FileNotFoundError:
        # 尚无写入者；原子替换保证即使此刻首次写入也不会读到半份 JSON。
        yield
        return
    with stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX if write else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _read(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("records"), list):
        raise ValueError("模型可用性记录格式无效")
    for row in data["records"]:
        if (not isinstance(row, dict) or row.get("status") not in ("ok", "unusable") or
                any(not isinstance(row.get(k), str) for k in
                    ("worker", "model", "cli_version", "time", "reason"))):
            raise ValueError("模型可用性记录格式无效")
    return data["records"]


def records():
    """读取可用性快照；调用者不能修改磁盘记录。"""
    path = _path()
    with _locked(path):
        return _read(path)


def record(worker, model, version, status, reason):
    """每个 (工人, 模型, CLI 版本) 保存最新结论，旧版本留作证据。"""
    if worker not in ("codex", "opencode") or status not in ("ok", "unusable"):
        raise ValueError("模型可用性结论无效")
    if any(not isinstance(v, str) or not v for v in (model, version, reason)):
        raise ValueError("模型可用性字段无效")
    row = dict(worker=worker, model=model, cli_version=version, status=status,
               time=datetime.now(timezone.utc).isoformat(), reason=reason)
    path = _path()
    with _locked(path, write=True):
        rows = [r for r in _read(path) if (r["worker"], r["model"], r["cli_version"]) !=
                (worker, model, version)]
        rows.append(row)
        fd, temporary = tempfile.mkstemp(prefix=".availability-", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"version": 1, "records": rows}, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return row


def availability(worker, model, version, rows=None):
    """只信当前版本；版本未知时也不能把历史失败当成当前失败。"""
    if version != "unknown":
        for row in records() if rows is None else rows:
            if (row["worker"], row["model"], row["cli_version"]) == (worker, model, version):
                return row["status"]
    return "unknown"


def rank(candidates, include_unusable=False):
    """在同一快照上稳定排序，保留同状态候选的发现顺序。"""
    rows, versions, found = records(), {}, []
    for candidate in candidates:
        worker = candidate["worker"]
        if worker not in versions:
            versions[worker] = cli_version(worker)
        version = versions[worker]
        status = availability(worker, candidate["model"], version, rows)
        if status != "unusable" or include_unusable:
            found.append(dict(candidate, cli_version=version, availability=status))
    return sorted(found, key=lambda row: {"ok": 0, "unknown": 1, "unusable": 2}[row["availability"]])


def unsupported_reason(captured, evidence='宿主捕获事件'):
    """只解析监督器传入的字节，不再从工人可写目录打开任何事件文件。"""
    if not isinstance(captured, bytes):
        raise TypeError('模型观察只接受宿主捕获的 bytes')
    for line in captured.decode('utf-8', errors='replace').splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        item = event.get("item")
        if kind not in ("error", "turn.failed") and not (
                kind == "item.completed" and isinstance(item, dict) and item.get("type") == "error"):
            continue
        # 嵌套 JSON 字符串也包含在序列化结果里；只匹配语义，不依赖模型名字。
        message = json.dumps(event, ensure_ascii=False).lower().replace('\\"', '"')
        direct = re.search(r"\bmodel\b(?:\s+['\"`][^'\"`]+['\"`])?\s+(?:is\s+)?(?:not\s+supported|unsupported|not\s+available)\b|\bunsupported\s+model\b", message)
        context = re.search(r"\b400\b|\baccount\b|\bcli\b|\bcodex\b", message)
        if any(feature in message for feature in ("service tier", "reasoning effort", "response_format", "tool calls")):
            continue
        if "model_not_supported" in message or (direct and context):
            return "模型不被当前账号或 CLI 支持（错误事件）；证据=" + str(evidence)
    return None


def observe(worker, model, version, directory, succeeded=False, *, captured=None):
    """旧调用只查询本轮宿主观察；不以外部 succeeded/工人回执更新可用性。"""
    from .execution import capture_directory, safe_read, write_json
    try:
        private = capture_directory(worker, directory)
    except ValueError:
        if captured is None:
            return 'unknown'
        raise  # 写入路径仍严格校验，不能把可信记录放到任意目录。
    observation = private / 'observation.json'
    if captured is None:
        try:
            row = json.loads(safe_read(observation))
        except (OSError, ValueError):
            return 'unknown'
        if not isinstance(row, dict):
            return 'unknown'
        if (row.get('worker'), row.get('model'), row.get('cli_version')) != (worker, model, version):
            return 'unknown'
        return row['status'] if row.get('status') in ('ok', 'unusable') else 'unknown'
    reason = unsupported_reason(captured, private / 'events.jsonl')
    status = 'unusable' if reason else ('ok' if succeeded else 'unknown')
    if status != 'unknown':
        record(worker, model, version, status, reason or '工人正常完成；宿主证据=' + str(private))
    write_json(observation, dict(worker=worker, model=model, cli_version=version, status=status))
    return status


def _opencode_candidates(template, providers):
    """先完整校验候选所需结构，失败时不留下半份 OpenCode 清单。"""
    def object_at(value, path):
        if not isinstance(value, dict):
            raise ValueError(path + ' 必须是对象')
        return value

    template = object_at(template, '模板根')
    settings_by_family = object_at(template.get('provider', {}), 'provider')
    enabled = template.get('enabled_providers', [])
    if not isinstance(enabled, list) or any(not isinstance(family, str) for family in enabled):
        raise ValueError('enabled_providers 必须是字符串数组')
    candidates = []
    for provider, entry in providers.items():
        family = entry['opencode_family']
        if family not in enabled:
            continue
        settings = object_at(settings_by_family.get(family, {}), 'provider 设置')
        whitelist = settings.get('whitelist', [])
        if not isinstance(whitelist, list):
            raise ValueError('whitelist 必须是数组')
        model_settings = object_at(settings.get('models', {}), 'models')
        # 只采用模板的显式白名单，不把整个提供商目录默认开放。
        for name in whitelist:
            if not isinstance(name, str) or not name:
                continue
            model = object_at(model_settings.get(name, {}), '模型设置')
            variants = object_at(model.get('variants', {}), 'variants')
            levels = ['']
            for level, value in variants.items():
                value = object_at(value, 'variant 设置')
                if not value.get('disabled', False):
                    levels.append(level)
            candidates.append({'worker': 'opencode', 'model': provider + '/' + name,
                               'levels': levels, 'provider': provider, 'source': 'worker_config'})
    return candidates


def discover(include_unusable=False):
    """查询已安装 Codex CLI（失败回退缓存）与 OpenCode 白名单。"""
    cfg, found = config.load(), []
    if shutil.which("codex"):
        catalog, source = codex_catalog(missing_ok=True)
        for model in catalog:
            if not isinstance(model, dict) or model.get("visibility") != "list":
                continue
            slug = model.get("slug")
            if not isinstance(slug, str) or not slug:
                continue
            levels = [item["effort"] for item in model.get("supported_reasoning_levels", [])
                      if isinstance(item, dict) and isinstance(item.get("effort"), str)]
            found.append({"worker": "codex", "model": slug, "levels": levels,
                          "provider": None, "key_ok": True, "source": "app_server" if source == "命令行" else "models_cache"})
    if shutil.which("opencode") and cfg.get("providers"):
        try:
            template = read_jsonc(cfg["opencode_config"])
            candidates = _opencode_candidates(template, cfg['providers'])
        except (OSError, ValueError):
            print("OpenCode 候选不可用：模板缺失、不可读或结构/格式无效；请检查 opencode_config", file=sys.stderr)
        else:
            available = {}
            for candidate in candidates:
                provider = candidate['provider']
                if provider not in available:
                    available[provider] = keys.key_source(provider)['kind'] in ('keychain', 'env_file')
                found.append(dict(candidate, key_ok=available[provider]))
    return rank(found, include_unusable=include_unusable)


def dispatch_catalog(worker, cfg):
    """只读派活清单；不查密钥、可用性记录，不创建快照或回退到 App 缓存。"""
    if worker == 'codex':
        return {row['slug']: [item['effort'] for item in row['supported_reasoning_levels']]
                for row in codex_cli_models() if row['visibility'] == 'list'}
    if worker == 'opencode':
        # 与现有 discover 共用工人模型白名单、显式变体和 disabled 规则。
        return {row['model']: row['levels'] for row in
                _opencode_candidates(read_jsonc(cfg['opencode_config']), cfg['providers'])}
    raise ValueError('未知工人')
