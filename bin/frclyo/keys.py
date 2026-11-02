"""钥匙只在内存中传递；读取失败返回 None，不输出命令错误或秘密。"""
import re
import subprocess
import sys
from . import config


# 仅兼容历史变量名；新增家在 .env 中使用大写提供方名加 _API_KEY。
ENV_NAMES = {"deepseek": "DEEPSEEK_API_KEY", "moonshotai-cn": "KIMI_CN_API_KEY", "xiaomi": "XIAOMI_API_KEY", "zai-coding-plan": "ZHIPU_API_KEY", "zhipuai": "ZHIPU_API_KEY"}


def list_providers():
    """按来源检查可用性，仅返回名字，不读取钥匙串密码。"""
    return [provider for provider in config.load()["providers"]
            if key_source(provider)["kind"] in ("keychain", "env_file")]


def _entries(cfg, provider):
    external = cfg.get("keychain_entries", {}).get(provider)
    if external:
        yield dict(kind="keychain", service=external["service"],
                   account=external["account"], external=True)
    yield dict(kind="keychain", service="frclyo-" + provider, account="frclyo", external=False)


def _keychain_exists(entry):
    """True = 存在，False = 不存在，None = 无法确认；不读取密码。"""
    try:
        code = subprocess.run(["security", "find-generic-password", "-s", entry["service"],
                               "-a", entry["account"]], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=10).returncode
        return True if code == 0 else False if code == 44 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _env_value(cfg, provider, presence_only=False):
    """仅解析显式回退文件；来源查询只返回布尔值，不向调用方传值。"""
    path = cfg.get("key_env_file")
    if not path:
        return False if presence_only else None
    name = ENV_NAMES.get(provider, provider.upper().replace("-", "_") + "_API_KEY")
    try:
        with open(path, encoding="utf-8") as stream:
            for line in stream:
                match = re.fullmatch(r"\s*(?:export\s+)?" + re.escape(name) + r"\s*=\s*(.*?)\s*", line)
                if match:
                    value = match[1]
                    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                        value = value[1:-1]
                    return bool(value) if presence_only else value or None
    except (OSError, UnicodeError):
        pass
    return False if presence_only else None


def key_source(provider):
    """返回 keychain、unknown（条目及 external）、env_file（path）或 none。"""
    cfg = config.load()
    if provider in cfg["providers"]:
        for entry in _entries(cfg, provider):
            exists = _keychain_exists(entry)
            if exists is True:
                return entry
            if exists is None:
                return dict(entry, kind="unknown")
        if _env_value(cfg, provider, presence_only=True):
            return {"kind": "env_file", "path": cfg["key_env_file"]}
    return {"kind": "none"}


def source_label(source):
    """CLI 共用来源文字，只包含来源元数据。"""
    if source["kind"] == "unknown":
        return "无法确认（钥匙串锁定或拒绝访问）"
    if source["kind"] == "keychain":
        return "钥匙串（%s）：%s（账户 %s）" % (
            "外部" if source["external"] else "默认", source["service"], source["account"])
    if source["kind"] == "env_file":
        return "回退文件：" + source["path"]
    return "无可用 key"


def get_key(provider):
    """只有条目不存在（44）才回退；读取失败返回 None 并输出固定补救提示。"""
    cfg = config.load()
    if provider not in cfg["providers"]:
        return None
    for entry in _entries(cfg, provider):
        try:
            result = subprocess.run(["security", "find-generic-password", "-s", entry["service"],
                                     "-a", entry["account"], "-w"], stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, text=True, timeout=10)
            if result.returncode == 44:
                continue
            if result.returncode == 0:
                value = result.stdout.rstrip("\r\n")
                if value:
                    return value
        except (OSError, UnicodeError, subprocess.SubprocessError):
            pass
        print("钥匙串读取失败（被拒、超时、空值或命令不可用）；"
              "可能要在弹框里点“允许”或解锁钥匙串；未使用其他来源的 key。", file=sys.stderr)
        return None
    return _env_value(cfg, provider)


def set_key(provider):
    """确认后由 security 隐藏输入；返回是否新增成功，记账由 aw 负责。"""
    if provider not in config.load()["providers"]:
        raise ValueError("提供方未配置")
    if input("将向钥匙串新增 frclyo-%s，继续？[y/N] " % provider).strip().lower() not in ("y", "yes"):
        return False
    # 不覆盖同名条目；密码不经过本程序、argv、日志或安装记录。
    result = subprocess.run(["security", "add-generic-password", "-s", "frclyo-" + provider,
                             "-a", "frclyo", "-w"], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    if result.returncode:
        raise RuntimeError("钥匙串写入失败；已有同名条目时请先核对")
    return True


def remove_key(provider):
    """只撤销本工具的钥匙串条目；移出配置的旧家仍可卸载，不触碰回退文件。"""
    if not isinstance(provider, str) or not re.fullmatch(r"[a-z0-9-]+", provider):
        raise ValueError("提供方名字无效")
    result = subprocess.run(["security", "delete-generic-password", "-s", "frclyo-" + provider,
                             "-a", "frclyo"], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=10)
    if result.returncode == 44:
        return False
    if result.returncode:
        raise RuntimeError("钥匙串删除失败")
    return True
