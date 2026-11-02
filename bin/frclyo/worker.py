"""执行脚本内部桥接：路径、权限模板、模型快照与固定宿主认证路由。"""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
from urllib.parse import urlsplit
from . import config

# 来自本轮配置/模板；禁止通过配置、argv、环境把认证代理改指其他主机。
# 五家均使用 openai-compatible 的 Bearer 认证。
PROXY_ROUTES = {
    "deepseek": ("api.deepseek.com", "", "Authorization", "Bearer "),
    "moonshotai-cn": ("api.moonshot.cn", "/v1", "Authorization", "Bearer "),
    "xiaomi": ("api.xiaomimimo.com", "/v1", "Authorization", "Bearer "),
    "zai-coding-plan": ("api.z.ai", "/api/coding/paas/v4", "Authorization", "Bearer "),
    "zhipuai": ("open.bigmodel.cn", "/api/paas/v4", "Authorization", "Bearer "),
}
DUMMY_API_KEY = "frclyo-local-proxy-placeholder"


def local_config(content, provider, port):
    """端口就绪后生成本轮模板；参数不包含 key，失效端口不能启动工人。"""
    if not 1 <= int(port) <= 65535:
        raise ValueError("代理端口无效")
    host, prefix, _, _ = PROXY_ROUTES[provider]
    template = json.loads(content)
    options = template["provider"][provider].setdefault("options", {})
    options["baseURL"] = "http://127.0.0.1:%s/%s%s" % (int(port), provider, prefix)
    options["apiKey"] = DUMMY_API_KEY
    # 模板里的自定义认证头也不能进入工人；生产认证只有固定代理策略。
    options.pop("headers", None)
    return json.dumps(template, ensure_ascii=False)


def worker_environment(source=None):
    """显式保留运行必需环境；不继承任意名字下的宿主凭据或认证覆盖项。"""
    source = os.environ if source is None else source
    allowed = {"HOME", "PATH", "TMPDIR", "TMP", "TEMP", "TMPPREFIX", "ZDOTDIR",
               "NPM_CONFIG_CACHE", "PIP_CACHE_DIR",
               "CLANG_MODULE_CACHE_PATH", "SWIFT_MODULECACHE_PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM",
               "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME",
               "OPENCODE_CONFIG", "OPENCODE_CONFIG_CONTENT", "OPENCODE_MODELS_PATH",
               "OPENCODE_DISABLE_PROJECT_CONFIG", "OPENCODE_DISABLE_CLAUDE_CODE",
               "OPENCODE_DISABLE_EXTERNAL_SKILLS", "OPENCODE_DISABLE_AUTOUPDATE",
               "OPENCODE_DISABLE_SHARE", "OPENCODE_DISABLE_LSP_DOWNLOAD",
               "OPENCODE_DISABLE_DEFAULT_PLUGINS", "OPENCODE_DISABLE_MODELS_FETCH"}
    result = {name: value for name, value in source.items() if name in allowed}
    result["OPENCODE_AUTH_CONTENT"] = "{}"
    return result


def sensitive_paths(cfg):
    """配置、Codex 登录目录和兼容凭据文件搬家后也必须显式禁读。"""
    paths = [str(config.config_file()),
             str(config.codex_home())]
    if cfg.get("key_env_file"):
        paths.append(cfg["key_env_file"])
    return paths


def read_jsonc(path):
    """只移除字符串外的注释，保留 URL 的 //；不执行配置中的任何代码。"""
    source = Path(path).read_text(encoding="utf-8")
    out, index, quoted, escaped = [], 0, False, False
    while index < len(source):
        char = source[index]
        if quoted:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
            out.append(char)
        elif source[index:index + 2] == "//":
            end = source.find("\n", index)
            index = len(source) if end < 0 else end
            out.append("\n")
            continue
        elif source[index:index + 2] == "/*":
            end = source.find("*/", index + 2)
            if end < 0:
                raise ValueError("配置注释未结束")
            out.append(" ")
            index = end + 2
            continue
        else:
            out.append(char)
        index += 1
    return json.loads("".join(out))


def snapshot(cfg, family, model):
    """仅复制本机 OpenCode models.dev 缓存；不下载、不把模型目录提交仓库。"""
    destination = Path(cfg["runtime_dir"]) / "models.snapshot.json"
    source = Path(cfg.get("models_snapshot_source", str(
        Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "opencode" / "models.json")))
    chosen = destination if os.path.lexists(destination) else source
    remedy = ("模型快照缺失、损坏或不含本轮模型。请在工人外运行 opencode models --refresh，"
              "再重试；自定义缓存可在配置中设置 models_snapshot_source 指向 models.dev JSON。"
              "已有旧快照时删除 runtime_dir/models.snapshot.json 后重试。")
    from .execution import safe_read, LOG_LIMIT

    def read_catalog(path):
        try:
            data = safe_read(path, LOG_LIMIT)
            catalog = json.loads(data)
            entry = catalog[family]
            if not isinstance(entry, dict) or model not in entry["models"] or not isinstance(entry["models"][model], dict):
                raise ValueError()
            if not isinstance(entry.get("npm"), str) or not isinstance(entry.get("api"), str):
                raise ValueError()
            return data, entry
        except (OSError, ValueError, TypeError, KeyError):
            raise ValueError(remedy) from None

    data, entry = read_catalog(chosen)
    if chosen != destination:
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # 同目录临时文件写完后原子发布，不覆盖已有快照。
        fd, temp = tempfile.mkstemp(prefix=".models-", dir=str(destination.parent))
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temp, destination)
            except FileExistsError:
                # 胜者已写完并发布，复核其模型后复用，不能返回失败方的源条目。
                _, entry = read_catalog(destination)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
    return str(destination), entry


def prepare(model):
    """只启用本轮一家；保留模型白名单/变体/权限，固定生产认证路由。"""
    cfg = config.ensure_layout()
    provider, model_id = model.split("/", 1)
    if provider not in cfg["providers"]:
        raise ValueError("不允许的提供方")
    entry = cfg["providers"][provider]
    family = entry["opencode_family"]
    route = PROXY_ROUTES.get(provider)
    if route is None or family != provider or entry["api_host"] != route[0]:
        raise ValueError("认证代理只支持固定官方提供方")
    template = read_jsonc(cfg["opencode_config"])
    settings = copy.deepcopy(template.get("provider", {}).get(family, {}))
    if settings.get("whitelist") is not None and model_id not in settings["whitelist"]:
        raise ValueError("模型不在工人配置白名单内")
    models_path, catalog = snapshot(cfg, family, model_id)
    # 上游地址必须与审定路由完全一致；配置和快照不能改变认证目的地。
    options = settings.setdefault("options", {})
    endpoint = urlsplit(options.get("baseURL", catalog["api"]))
    if (endpoint.scheme != "https" or endpoint.netloc != route[0] or
            endpoint.path.rstrip("/") != route[1] or endpoint.query or endpoint.fragment):
        raise ValueError("模型 API 地址与固定官方路由不符")
    options["baseURL"] = "http://127.0.0.1:1/" + provider + route[1]
    options["apiKey"] = DUMMY_API_KEY
    options.pop("headers", None)
    template["enabled_providers"] = [family]
    template["provider"] = {family: settings}
    return {"root": cfg["work_dir"], "deliver": cfg["deliver_dir"], "runtime": cfg["runtime_dir"],
            "control": cfg["control_dir"], "template": cfg["opencode_config"], "snapshot": models_path,
            "target": entry["api_host"] + ":443", "model": family + "/" + model_id,
            "content": json.dumps(template, ensure_ascii=False),
            "protected": json.dumps([cfg["work_dir"], cfg["runtime_dir"], cfg["opencode_config"]], ensure_ascii=False),
            "sensitive": json.dumps(sensitive_paths(cfg), ensure_ascii=False)}


def main():
    """私有 shell 桥接只输出不含凭据的 JSON 标量；绝不提供打印 key 的子命令。"""
    try:
        mode = sys.argv[1]
        if mode == "codex":
            cfg = config.ensure_layout()
            print(json.dumps({"root": cfg["work_dir"], "deliver": cfg["deliver_dir"], "runtime": cfg["runtime_dir"],
                              "proxy": cfg["codex"]["proxy"], "fast": cfg["codex"]["default_fast"],
                              "sensitive": json.dumps(sensitive_paths(cfg), ensure_ascii=False)}))
        elif mode == "opencode":
            print(json.dumps(prepare(sys.argv[2]), ensure_ascii=False))
        else:
            raise ValueError("未知内部命令")
        return 0
    except (OSError, ValueError, KeyError, TypeError):
        # 错误文本不包含配置值；详细补救文字固定，防止敏感配置进入日志。
        print("运行时准备失败：检查配置/提供方/模型白名单；模型快照需来自本机 OpenCode。"
              "请运行 opencode models --refresh，或设置 models_snapshot_source；"
              "旧快照不含模型时删除 runtime_dir/models.snapshot.json 后重试。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
