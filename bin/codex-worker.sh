#!/bin/bash
# Codex 工人入口：每轮独立目录，隔离用户配置并保留事件、结果和回执。
# 用法：脚本 <任务编号> <轮次编号> <任务说明文件> <模型> <思考档位> [超时秒，默认1800]
# 模型与档位必须显式指定；NET/WEB/FAST/SUBAGENTS/WORKER_INPUT_DIR 开关沿用原接口。
set -u
umask 077

# ---------- 1. 读参数并检查 ----------
# 只接受固定位置参数；在模型/配置查询前拒绝未知选项和多余参数。
if [ "$#" -lt 5 ] || [ "$#" -gt 6 ]; then
  echo "用法：codex-worker.sh <任务编号> <轮次编号> <任务说明文件> <模型> <思考档位> [超时秒数]" >&2; exit 2
fi
for argument in "$@"; do
  case "$argument" in -*) echo "用法错误：不支持选项 $argument" >&2; exit 2 ;; esac
done
TASK_ID="${1:-}"; RUN_ID="${2:-}"; TASK_FILE="${3:-}"; MODEL="${4:-}"; EFFORT="${5:-}"; TIMEOUT_SEC="${6:-1800}"
if [ -z "$TASK_ID" ] || [ -z "$RUN_ID" ] || [ -z "$TASK_FILE" ] || [ -z "$MODEL" ] || [ -z "$EFFORT" ]; then
  echo "用法：codex-worker.sh <任务编号> <轮次编号> <任务说明文件> <模型> <思考档位> [超时秒数]" >&2; exit 2
fi
# 编号只允许字母、数字、横线、下划线，防止路径穿越（比如 ../../）
for v in "$TASK_ID" "$RUN_ID"; do
  [[ "$v" =~ ^[A-Za-z0-9_-]+$ ]] || { echo "编号只能含字母、数字、-、_：$v" >&2; exit 2; }
done
# 超时秒数必须是正整数（看板和计时器都要用它）
[[ "$TIMEOUT_SEC" =~ ^[1-9][0-9]*$ ]] || { echo "超时秒数必须是正整数：$TIMEOUT_SEC" >&2; exit 2; }
# 模型与档位优先询问本机 CLI；查询失败仅按同版本缓存拦截，异版缓存只提醒。
BIN_DIR=$(cd "$(dirname "$0")" && pwd -P) || exit 2
# 以真实路径重开脚本并持有版本读锁；切换不影响本轮，清理也不能删掉运行中的代码。
if [ "${FRCLYO_RELEASE_PIN:-}" != "$$:$BIN_DIR/codex-worker.sh" ]; then
  exec python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1]); from frclyo.release import pin_worker; pin_worker(sys.argv[1], "codex", sys.argv[2:])' "$BIN_DIR" "$@"
fi
host_exec() { python3 -I -c 'import sys; sys.path.insert(0, sys.argv.pop(1)); from frclyo.execution import main; sys.exit(main())' "$BIN_DIR" "$@"; }
CLI_VER=$(python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1]); from frclyo.models import cli_version; print(cli_version("codex"))' "$BIN_DIR") || exit 2
# B85：Codex 0.153.x exec 的非 luna 子 agent 会报 no thread with id，且可能伪报成功。
if [ "${CODEX_WORKER_SUBAGENTS:-0}" = "1" ] && [ "${CODEX_WORKER_SUBAGENTS_FORCE:-0}" != "1" ] && [ "$MODEL" != "gpt-5.6-luna" ]; then
  if [[ "$CLI_VER" =~ ^0\.153\.[0-9]+([.+-].*)?$ ]]; then
    echo "Codex $CLI_VER 的 exec 非 luna 子 agent 存在 no thread with id / 伪报成功问题；请使用 gpt-5.6-luna 或关闭子 agent。显式设 CODEX_WORKER_SUBAGENTS_FORCE=1 可跳过此检查。" >&2
    exit 2
  fi
fi
CODEX_CACHE=$(python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1]); from frclyo.config import codex_home; print(codex_home() / "models_cache.json")' "$BIN_DIR") || exit 2
CODEX_MODEL_SOURCE=$(python3 -I - "$BIN_DIR" "$MODEL" "$EFFORT" "${CODEX_WORKER_SUBAGENTS:-0}" "$CLI_VER" <<'EOF'
import sys
sys.path.insert(0, sys.argv[1])
from frclyo.models import check_codex_model
try:
    print(check_codex_model(*sys.argv[2:]))
except (OSError, ValueError) as exc:
    sys.exit(str(exc))
EOF
) || exit 2
export CODEX_MODEL_SOURCE
[ -f "$TASK_FILE" ] || { echo "找不到任务说明文件：$TASK_FILE" >&2; exit 2; }

# ---------- 2. 算出本轮专属目录；已存在就拒绝（防重复执行、防覆盖旧结果）----------
# Python 隔离模式只信任脚本相邻的公共库；配置值不作为 shell 代码执行。
SETTINGS=$(python3 -I -c 'import sys; sys.path.insert(0, sys.argv.pop(1)); from frclyo.worker import main; sys.exit(main())' "$BIN_DIR" codex) || exit 2
field() { python3 -I -c 'import json,sys; v=json.loads(sys.argv[1])[sys.argv[2]]; print(int(v) if isinstance(v,bool) else v)' "$SETTINGS" "$1"; }
DATA_ROOT=$(field root) || exit 2
DELIVER=$(field deliver) || exit 2
RUNTIME=$(field runtime) || exit 2
PROXY=$(field proxy) || exit 2
DEFAULT_FAST=$(field fast) || exit 2
SENSITIVE=$(field sensitive) || exit 2
RUN_DIR="$DELIVER/$TASK_ID/$RUN_ID"
# 创建或复制输入之前拒绝链接祖先，避免先写出边界再发现错误。
python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1]); from frclyo.isolation import validate_run; validate_run(sys.argv[2], sys.argv[3])' "$BIN_DIR" "$DATA_ROOT" "$RUN_DIR" || exit 2
if [ -e "$RUN_DIR" ]; then echo "已存在 ${RUN_DIR}，拒绝重复执行" >&2; exit 3; fi
python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1]); from frclyo.ledger import claim_round; claim_round(sys.argv[2])' "$BIN_DIR" "$RUN_DIR" || exit 4
# 守则先于任务卡；哈希在启动工人前捕获，回执不信任工人可改写的副本。
RULE_SHA=$(python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1]); from frclyo.isolation import inject_task; print(inject_task(sys.argv[2], sys.argv[3]))' "$BIN_DIR" "$TASK_FILE" "$RUN_DIR/任务.txt") || exit 4
# 输入文件：派活前设 WORKER_INPUT_DIR=<目录>，复制到本轮 输入/；git/构建临时操作按守则第 3 条。
host_exec inputs "$RUN_DIR" codex "$TASK_ID" "$RUN_ID" "$MODEL" "$EFFORT" "$RULE_SHA" || exit 2
CONTROL=$(host_exec control codex "$RUN_DIR") || exit 2
# Codex 私有状态放数据根外的 ASCII 路径；父级同 uid/0700，轮次独占且拒绝链接。
# 结束后保留供宿主核对；不自动删除工人可写树，最终交付由工人复制回 RUN_DIR。
ROUND_TMP=$(python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1]); from frclyo.isolation import prepare_codex_tmp; print(prepare_codex_tmp(sys.argv[2], sys.argv[3]))' "$BIN_DIR" "$DATA_ROOT" "$RUN_DIR") || exit 2
export TMPDIR="$ROUND_TMP" TMP="$ROUND_TMP" TEMP="$ROUND_TMP"
export TMPPREFIX="$ROUND_TMP/zsh" NPM_CONFIG_CACHE="$ROUND_TMP/npm-cache" PIP_CACHE_DIR="$ROUND_TMP/pip-cache"
# HOME/CODEX_HOME 保持宿主值；模型命令的私有 HOME/ZDOTDIR 只通过 TEMP_ENV 设置。
export CLANG_MODULE_CACHE_PATH="$ROUND_TMP/clang-module-cache" SWIFT_MODULECACHE_PATH="$ROUND_TMP/swift-module-cache" XDG_CACHE_HOME="$ROUND_TMP/cache"
TEMP_ENV=$(python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1]); from frclyo.isolation import codex_temp_environment; print(codex_temp_environment(sys.argv[2], sys.argv[3]))' "$BIN_DIR" "$DATA_ROOT" "$RUN_DIR") || exit 2


# ---------- 3. 网络代理：只给 codex 进程本身连 OpenAI 用（命令默认断网，CODEX_WORKER_NET=1 才开）----------
# 空配置明确关闭代理，也清掉继承的大小写代理变量。
unset HTTPS_PROXY HTTP_PROXY ALL_PROXY NO_PROXY https_proxy http_proxy all_proxy no_proxy
if [ -n "$PROXY" ]; then
  export HTTPS_PROXY="$PROXY" HTTP_PROXY="$PROXY" https_proxy="$PROXY" http_proxy="$PROXY"
  export NO_PROXY="localhost,127.0.0.1" no_proxy="localhost,127.0.0.1"
fi
START=$(date -u +%Y-%m-%dT%H:%M:%SZ)
# 给看板用：记下谁在跑、几点开始、最多跑多久、本脚本的进程号。
# 看板据此判断：有这个文件、还没有回执、进程还在 = 正在干活；进程没了却没有回执 = 中断，要提醒 使用者
printf '{"工人":"codex","模型":"%s","档位":"%s","开始":"%s","超时秒":%s,"进程号":%s}\n' \
  "$MODEL" "$EFFORT" "$START" "$TIMEOUT_SEC" "$$" > "$RUN_DIR/运行信息.json"

# ---------- 4. 严格档：覆盖 workspace 的特殊临时写口，再禁止敏感路径读取 ----------
# 用 JSON 字符串转义 TOML 路径；运行时位置随配置变更仍然禁读。
PERM=$(python3 -I - "$BIN_DIR" "$DATA_ROOT" "$RUN_DIR" "$SENSITIVE" "$RUNTIME" <<'PYDENY'
import json, sys
sys.path.insert(0, sys.argv[1])
from frclyo.isolation import codex_permission
print(codex_permission(sys.argv[2], sys.argv[3], json.loads(sys.argv[4]), sys.argv[5]))
PYDENY
) || exit 2

# ---------- 5. 子 agent 开关：默认不派（控制用量、行为可预期）；派活方需要时设 CODEX_WORKER_SUBAGENTS=1 ----------
SUBAGENT_FLAG=(--disable multi_agent)
[ "${CODEX_WORKER_SUBAGENTS:-0}" = "1" ] && SUBAGENT_FLAG=()
# 网页搜索开关（使用者 2026-10-13 选"只开网页搜索、按任务开"）：默认关；设 CODEX_WORKER_WEB=1 时打开。
# 打开后模型能用 OpenAI 自带的网页搜索（搜索在 OpenAI 那边做、结果交给模型），命令网络由独立的 CODEX_WORKER_NET=1 控制。
# 注意 --search 是 codex 的顶层参数，必须写在 exec 前面（写在后面会报 unexpected argument，2026-10-13 实测）
WEB_TOP=(); WEB_ARGS=(-c 'web_search="disabled"')
[ "${CODEX_WORKER_WEB:-0}" = "1" ] && { WEB_TOP=(--search); WEB_ARGS=(); }
# 快速档：默认开；只有模型清单里写了支持 Fast 档（priority）才加，不支持就照常跑，不报错
FAST_ARGS=()
if [ "${CODEX_WORKER_FAST:-$DEFAULT_FAST}" = "1" ] && python3 -I -c "import json,sys; m=[x for x in json.load(open(sys.argv[2], encoding='utf-8'))['models'] if x.get('slug')==sys.argv[1]]; sys.exit(0 if m and any(t.get('id')=='priority' for t in m[0].get('service_tiers') or []) else 1)" "$MODEL" "$CODEX_CACHE" 2>/dev/null; then
  FAST_ARGS=(-c 'service_tier="priority"')
fi

# ---------- 6. codex exec 的全部参数（数组写法，每项的作用写在旁边）----------
ARGS=(
  ${WEB_TOP[@]+"${WEB_TOP[@]}"}   # 开网页搜索时这里是 --search（顶层参数，必须在 exec 前面）
  exec
  --ephemeral                     # 不在 ~/.codex 留会话（使用者 的决定；记录写在本轮目录里）
  --ignore-user-config            # 不读 ~/.codex/config.toml：不带插件/MCP/记忆/通知/信任表
  --ignore-rules                  # 不读 ~/.codex/rules（曾有让 cp 绕过沙箱的放行规则）
  --disable hooks                 # 不跑任何钩子
  --disable memories              # 不读也不写 使用者 的共享记忆
  --disable apps                  # 不暴露 ChatGPT 连接器（使用者 的各类账号）
  --disable plugins               # 不加载插件
  --disable computer_use --disable browser_use --disable browser_use_external  # 不能操控桌面和浏览器
  ${SUBAGENT_FLAG[@]+"${SUBAGENT_FLAG[@]}"}   # 写成这样：开子 agent 时数组为空，bash 3.2 直接写 "${A[@]}" 会报错退出
  -C "$RUN_DIR"                   # 工作根目录 = 本轮专属目录
  -c "$PERM"                      # 定义严格档
  -c 'default_permissions="frclyo_worker"'               # 启用严格档（不能再传 -s，否则严格档被忽略）
  -c 'approvals_reviewer="user"'  # 不允许被切成"模型替人审批"
  -c 'approval_policy="never"'    # 越界请求一律拒绝并交回模型，不会卡住等人
  -c 'shell_environment_policy.inherit="core"'          # 命令只拿到 PATH/HOME/LANG 等基础变量
  -c "$TEMP_ENV"                 # 模型命令显式继承同一私有临时目录
  -c 'shell_environment_policy.ignore_default_excludes=false'  # 再滤掉名字含 KEY/SECRET/TOKEN 的变量
  -c 'history.persistence="none"' # 不写 ~/.codex/history.jsonl
  ${WEB_ARGS[@]+"${WEB_ARGS[@]}"}  # 网页搜索：默认关，CODEX_WORKER_WEB=1 才开（见上面）
  ${FAST_ARGS[@]+"${FAST_ARGS[@]}"}  # 快速档：默认开，CODEX_WORKER_FAST=0 关（见上面）
  -m "$MODEL"                     # 模型由派活方按任务给（上面已对照清单检查）
  -c "model_reasoning_effort=\"$EFFORT\""                # 思考档位由派活方按任务给
  --skip-git-repo-check           # 交付目录在 git 仓库里也可能不在，统一跳过检查
  --json                          # 标准输出是 JSONL 事件流
  -o "$RUN_DIR/结果.txt"           # 最后一条回复写进 结果.txt
  -                               # 任务说明从标准输入读（避免引号问题）
)
export CODEX_WORKER_FAST_ON=$([ ${#FAST_ARGS[@]} -gt 0 ] && echo 1 || echo 0)   # 给回执记"这次开没开快速档"
# 监督器在宿主私有控制目录捕获输出和状态，独立进程组覆盖所有普通子命令。
WORKER_PID=""
cleanup() {
  local saved_rc=$?
  trap - EXIT
  trap '' HUP INT TERM
  if [ -n "$WORKER_PID" ]; then
    kill -CONT "$WORKER_PID" 2>/dev/null || :
    kill -TERM "$WORKER_PID" 2>/dev/null || :
    wait "$WORKER_PID" 2>/dev/null || :
  fi
  host_exec finalize codex "$RUN_DIR" "$CONTROL" "$TASK_ID" "$RUN_ID" "$MODEL" "$EFFORT" \
    "$START" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$RULE_SHA" "$CLI_VER" "" "$saved_rc"
  local receipt_rc=$?
  saved_rc=$receipt_rc
  host_exec summary "$CONTROL" "$RUN_DIR"
  exit "$saved_rc"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
python3 -I -c 'import sys; sys.path.insert(0, sys.argv.pop(1)); from frclyo.execution import main; sys.exit(main())' \
  "$BIN_DIR" supervise codex "$CONTROL" "$TIMEOUT_SEC" "$$" 0 "" "$RUN_DIR/任务.txt" codex "${ARGS[@]}" &
WORKER_PID=$!
wait "$WORKER_PID"
RC=$?
WORKER_PID=""
exit "$RC"
