#!/bin/bash
# opencode-worker.sh —— OpenCode 国产模型工人的唯一入口；兼容 macOS bash 3.2。
# 每轮 key 只经宿主监督器的匿名管道交给反向代理；工人仅拿固定假 apiKey。
# 工人和子命令只能连接本轮 IPv4 回环端口；代理校验证书并添加本家认证。
# 用法：脚本 <任务编号> <轮次编号> <任务说明文件> <家/模型> [变体] [超时秒，默认1800]
# 退出码：0 成功；2 参数/准备失败；3 轮次已存在；4 建目录失败；142 超时；128+信号 中断。
set -u
umask 077

# ---------- 1. 检查参数，阻止路径穿越；0 秒不算正整数，避免意外取消超时 ----------
# 只接受固定位置参数；在模型/配置查询前拒绝未知选项和多余参数。
if [ "$#" -lt 4 ] || [ "$#" -gt 6 ]; then
  echo "用法：opencode-worker.sh <任务编号> <轮次编号> <任务说明文件> <家/模型> [变体] [超时秒数]" >&2; exit 2
fi
for argument in "$@"; do
  case "$argument" in -*) echo "用法错误：不支持选项 $argument" >&2; exit 2 ;; esac
done
TASK_ID="${1:-}"; RUN_ID="${2:-}"; TASK_FILE="${3:-}"; MODEL="${4:-}"; VARIANT="${5:-}"; TIMEOUT_SEC="${6:-1800}"
if [ -z "$TASK_ID" ] || [ -z "$RUN_ID" ] || [ -z "$TASK_FILE" ] || [ -z "$MODEL" ]; then
  echo "用法：opencode-worker.sh <任务编号> <轮次编号> <任务说明文件> <家/模型> [变体] [超时秒数]" >&2; exit 2
fi
for v in "$TASK_ID" "$RUN_ID"; do
  [[ "$v" =~ ^[A-Za-z0-9_-]+$ ]] || { echo "编号只能含字母、数字、-、_：$v" >&2; exit 2; }
done
[[ "$TIMEOUT_SEC" =~ ^[1-9][0-9]*$ ]] || { echo "超时秒数必须是正整数：$TIMEOUT_SEC" >&2; exit 2; }
[[ "$MODEL" =~ ^[a-z0-9-]+/[A-Za-z0-9._-]+$ ]] || { echo "模型格式应为 家/模型：$MODEL" >&2; exit 2; }
[[ -z "$VARIANT" || "$VARIANT" =~ ^[a-z]+$ ]] || { echo "变体格式不对：$VARIANT" >&2; exit 2; }
[ -f "$TASK_FILE" ] || { echo "找不到任务说明文件：$TASK_FILE" >&2; exit 2; }

# ---------- 2. 提供方集合、路径、主机和模型模板统一通过 config 读取 ----------
BIN_DIR=$(cd "$(dirname "$0")" && pwd -P) || exit 2
# 以真实路径重开脚本并持有版本读锁；切换不影响本轮，清理也不能删掉运行中的代码。
if [ "${FRCLYO_RELEASE_PIN:-}" != "$$:$BIN_DIR/opencode-worker.sh" ]; then
  exec python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1]); from frclyo.release import pin_worker; pin_worker(sys.argv[1], "opencode", sys.argv[2:])' "$BIN_DIR" "$@"
fi
host_exec() { python3 -I -c 'import sys; sys.path.insert(0, sys.argv.pop(1)); from frclyo.execution import main; sys.exit(main())' "$BIN_DIR" "$@"; }
SETTINGS=$(exec python3 -I -c 'import sys; sys.path.insert(0, sys.argv.pop(1)); from frclyo.worker import main; sys.exit(main())' "$BIN_DIR" opencode "$MODEL") || exit 2
field() { python3 -I -c 'import json,sys; print(json.loads(sys.argv[1])[sys.argv[2]])' "$SETTINGS" "$1"; }
DATA_ROOT=$(field root) || exit 2
DELIVER=$(field deliver) || exit 2
OC=$(field runtime) || exit 2
CONTROL_ROOT=$(field control) || exit 2
OC_CONFIG=$(field template) || exit 2
MODELS_PATH=$(field snapshot) || exit 2
API_TARGET=$(field target) || exit 2
ACTUAL_MODEL=$(field model) || exit 2
CONFIG_CONTENT=$(field content) || exit 2
SENSITIVE=$(field sensitive) || exit 2
PROTECTED=$(field protected) || exit 2
OC_BIN=$(command -v opencode) || { echo "找不到 opencode，请先安装并加入 PATH" >&2; exit 2; }
CLI_VER=$(exec python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1]); from frclyo.models import cli_version; print(cli_version("opencode"))' "$BIN_DIR") || exit 2
SANDBOX_BIN=$(command -v sandbox-exec) || { echo "需要 macOS sandbox-exec" >&2; exit 2; }
[ -x "$OC_BIN" ] && [ -f "$OC_CONFIG" ] && [ -f "$BIN_DIR/allowlist-proxy.py" ] || {
  echo "缺少 OpenCode 可执行文件、权限模板或同目录代理" >&2; exit 2;
}
# ---------- 3. 原子占用本轮目录，禁止并发覆盖 ----------
RUN_DIR="$DELIVER/$TASK_ID/$RUN_ID"
# 创建或复制输入之前拒绝链接祖先，避免先写出边界再发现错误。
python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1]); from frclyo.isolation import validate_run; validate_run(sys.argv[2], sys.argv[3])' "$BIN_DIR" "$DATA_ROOT" "$RUN_DIR" || exit 2
if [ -e "$RUN_DIR" ]; then echo "已存在 ${RUN_DIR}，拒绝重复执行" >&2; exit 3; fi
# 账本恢复和独占建目录共用锁；两个并发派活不能同时占用同一轮。
python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1]); from frclyo.ledger import claim_round; claim_round(sys.argv[2])' "$BIN_DIR" "$RUN_DIR" || exit 4
RUN_DIR=$(cd "$RUN_DIR" && pwd -P) || exit 4
# 守则先于任务卡；哈希在启动工人前捕获，回执不信任工人可改写的副本。
RULE_SHA=$(exec python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1]); from frclyo.isolation import inject_task; print(inject_task(sys.argv[2], sys.argv[3]))' "$BIN_DIR" "$TASK_FILE" "$RUN_DIR/任务.txt") || exit 4
host_exec inputs "$RUN_DIR" opencode "$TASK_ID" "$RUN_ID" "$MODEL" "$VARIANT" "$RULE_SHA" || exit 2

# 每轮 mkdir 原子占用，模板 O_EXCL|O_NOFOLLOW；旧同名节点一律拒绝。
CONTROL=$(host_exec control opencode "$RUN_DIR") || exit 2
PROFILE="$CONTROL/sandbox.sb"
START=$(date -u +%Y-%m-%dT%H:%M:%SZ)
PROXY_PID=""; WORKER_PID=""; PROXY_PORT=""; RUN_STARTED=0

write_receipt() {
  host_exec finalize opencode "$RUN_DIR" "$CONTROL" "$TASK_ID" "$RUN_ID" "$MODEL" "$VARIANT" \
    "$START" "$2" "$RULE_SHA" "$CLI_VER" "$API_TARGET" "$1"
}

# ---------- 6. 所有可捕获退出都先收工人、再收代理；强杀则靠两者各自的父进程检查 ----------
cleanup() {
  local saved_rc=$?
  trap - EXIT
  trap '' HUP INT TERM
  if [ -n "$WORKER_PID" ]; then
    kill -CONT "$WORKER_PID" 2>/dev/null || :
    kill -TERM "$WORKER_PID" 2>/dev/null || :
    wait "$WORKER_PID" 2>/dev/null || :
  fi
  if [ -n "$PROXY_PID" ]; then
    stop_child "$PROXY_PID"
  fi
  if [ "$RUN_STARTED" = 1 ]; then
    write_receipt "$saved_rc" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    local receipt_rc=$?
    saved_rc=$receipt_rc
  fi
  # 控制目录留作诊断，位于根内 运行时/控制/opencode/，工人不可读写。
  host_exec summary "$CONTROL" "$RUN_DIR"
  exit "$saved_rc"
}
# 先恢复可能被暂停的子进程，让 TERM 有机会被处理；最多等五秒，再强杀，防止清理永久卡住。
stop_child() {
  local child_pid="$1" attempt
  kill -CONT "$child_pid" 2>/dev/null || :
  kill -TERM "$child_pid" 2>/dev/null || :
  for ((attempt=0; attempt<50; attempt++)); do
    kill -0 "$child_pid" 2>/dev/null || break
    sleep 0.1
  done
  kill -KILL "$child_pid" 2>/dev/null || :
  wait "$child_pid" 2>/dev/null || :
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
# 控制目录已就绪：代理/运行时准备失败也应留下失败回执，未启动工人的回收字段为 False。
RUN_STARTED=1

# ---------- 7/8. 宿主取 key，经匿名管道交接后 exec 代理；key 不进 argv/env ----------
# 后台进程直接 exec，PROXY_PID 也是代理 PID；其父进程仍是当前 shell。
(exec python3 -I -c 'import sys; sys.path.insert(0, sys.argv.pop(1)); from frclyo.execution import main; sys.exit(main())' \
  "$BIN_DIR" proxy "${MODEL%%/*}" "$CONTROL" "$$") \
  > "$CONTROL/代理.stdout" 2> "$CONTROL/代理.stderr" &
PROXY_PID=$!
for ((i=0; i<100; i++)); do
  if [ -s "$CONTROL/代理.stdout" ]; then IFS= read -r PROXY_PORT < "$CONTROL/代理.stdout" || :; fi
  [ -n "$PROXY_PORT" ] && break
  kill -0 "$PROXY_PID" 2>/dev/null || break
  sleep 0.1
done
[[ "$PROXY_PORT" =~ ^[0-9]+$ ]] && [ "$PROXY_PORT" -gt 0 ] && [ "$PROXY_PORT" -le 65535 ] || {
  echo "代理未能就绪，查看 $CONTROL/代理.stderr" >&2; exit 2;
}

# 只有代理就绪后才发布每轮配置，文件和环境中的 baseURL 完全相同。
CONFIG_CONTENT=$(host_exec local-config "$CONFIG_CONTENT" "${MODEL%%/*}" "$PROXY_PORT") || exit 2
RT=$(host_exec runtime "$OC" "$TASK_ID" "$RUN_ID" "$CONFIG_CONTENT") || exit 2
ROUND_TMP=$(python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1]); from frclyo.isolation import prepare_opencode_tmp; print(prepare_opencode_tmp(sys.argv[2], sys.argv[3], sys.argv[4]))' "$BIN_DIR" "$DATA_ROOT" "$RUN_DIR" "$RT") || exit 2

# ---------- 9. 网络默认全禁，只允许精确的 IPv4 回环 TCP 端口；不开放 DNS、其他回环端口或 Unix socket ----------
# macOS 的 remote 地址语法只接受 localhost/*，不接受字面量 127.0.0.1；tcp4 限定 IPv4，
# 因此 tcp4 + localhost + 精确端口对应 127.0.0.1:本轮端口，不放行 IPv6 或整段 localhost。
# 用 Python 转义路径，避免空格、引号或反斜线破坏规则；realpath 同时覆盖 macOS 的 /tmp 别名。
# 拒绝日志只让沙箱外代理写，私有控制目录禁止读写，防止工人篡改规则或计数。
python3 -I - "$BIN_DIR" "$HOME" "$DATA_ROOT" "$RUN_DIR" "$RT" "$CONTROL" "$PROXY_PORT" "$PROFILE" "$PROXY_PID" "$API_TARGET" "$SENSITIVE" "$PROTECTED" "$OC/worker-$TASK_ID.$RUN_ID.json" "$MODELS_PATH" <<'PY'
import json, os, sys
sys.path.insert(0, sys.argv[1])
from frclyo.isolation import seatbelt
from frclyo.execution import atomic_write, write_json
home, root, run, runtime, control, port, profile, pid, target, sensitive, protected = sys.argv[2:13]
readonly = sys.argv[13:]
atomic_write(profile, seatbelt(home, root, run, runtime, control, port,
    json.loads(sensitive), json.loads(protected), readonly).encode())
write_json(os.path.join(run, '网络信息.json'), {'代理进程号': int(pid), '端口': int(port), '放行': [target], '规则': profile})
PY
[ "$?" = 0 ] || exit 2
printf '{"工人":"opencode","模型":"%s","档位":"%s","开始":"%s","超时秒":%s,"进程号":%s}\n' \
  "$MODEL" "${VARIANT:-默认}" "$START" "$TIMEOUT_SEC" "$$" > "$RUN_DIR/运行信息.json"

# ---------- 10. 本地 HTTP baseURL 直连；监督器为工人重建环境，不继承任何宿主认证 ----------
(
  unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL OPENAI_API_KEY GITHUB_TOKEN GH_TOKEN \
        MOONSHOT_API_KEY KIMI_API_KEY OPENCODE_API_KEY OPENROUTER_API_KEY GEMINI_API_KEY \
        DEEPSEEK_API_KEY KIMI_CN_API_KEY XIAOMI_API_KEY
  unset ALL_PROXY all_proxy NO_PROXY no_proxy HTTP_PROXY http_proxy HTTPS_PROXY https_proxy
  export XDG_CONFIG_HOME="$RT/config" XDG_DATA_HOME="$RT/data" XDG_STATE_HOME="$RT/state" XDG_CACHE_HOME="$ROUND_TMP/cache"
  export HOME="$RT/home" TMPDIR="$ROUND_TMP" TMP="$ROUND_TMP" TEMP="$ROUND_TMP"
  export ZDOTDIR="$RT/home" TMPPREFIX="$ROUND_TMP/zsh"
  export NPM_CONFIG_CACHE="$ROUND_TMP/npm-cache" PIP_CACHE_DIR="$ROUND_TMP/pip-cache"
  export CLANG_MODULE_CACHE_PATH="$ROUND_TMP/clang-module-cache" SWIFT_MODULECACHE_PATH="$ROUND_TMP/swift-module-cache"
  export OPENCODE_CONFIG="$OC/worker-$TASK_ID.$RUN_ID.json" OPENCODE_CONFIG_CONTENT="$CONFIG_CONTENT"
  export OPENCODE_DISABLE_PROJECT_CONFIG=1 OPENCODE_DISABLE_CLAUDE_CODE=1 OPENCODE_DISABLE_EXTERNAL_SKILLS=1
  export OPENCODE_DISABLE_AUTOUPDATE=1 OPENCODE_DISABLE_SHARE=1 OPENCODE_DISABLE_LSP_DOWNLOAD=1
  export OPENCODE_DISABLE_DEFAULT_PLUGINS=1 OPENCODE_AUTH_CONTENT='{}'
  export OPENCODE_DISABLE_MODELS_FETCH=1 OPENCODE_MODELS_PATH="$MODELS_PATH"
  # 参数数组始终非空；不展开 bash 3.2 在 set -u 下会报错的空数组。
  ARGS=("$OC_BIN" run --dir "$RUN_DIR" -m "$ACTUAL_MODEL")
  if [ "${OPENCODE_WORKER_DEBUG:-0}" = 1 ]; then ARGS+=(--print-logs --log-level DEBUG); fi
  if [ -n "$VARIANT" ]; then ARGS+=(--variant "$VARIANT"); fi
  ARGS+=(--agent worker --title "worker-$TASK_ID-$RUN_ID" --format json)
  # 监督器在沙箱外，工人在独立进程组内。超时/收到信号/父 shell 被强杀时均收整个进程组。
  # Python 也不用系统代理；此处只管理进程，不联网。stdin 显式打开任务文件，后台运行不会丢输入。
  exec python3 -I -c 'import sys; sys.path.insert(0, sys.argv.pop(1)); from frclyo.execution import main; sys.exit(main())' \
    "$BIN_DIR" supervise opencode "$CONTROL" "$TIMEOUT_SEC" "$$" "$PROXY_PID" "${MODEL%%/*}" "$RUN_DIR/任务.txt" \
    "$SANDBOX_BIN" -f "$PROFILE" "${ARGS[@]}"
) &
WORKER_PID=$!
# Bash 等后台进程时能立即执行 trap。
wait "$WORKER_PID"
RC=$?
WORKER_PID=""
exit "$RC"
