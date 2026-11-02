#!/bin/bash
# 本机验收：确定性内核探针与真实模型守则复述分开计分。
# 完整模式有两次真实模型调用；OpenCode 走生产钥匙串 -> 匿名管道 -> 代理，不需环境 key。
set -eu
HERE=$(cd "$(dirname "$0")" && pwd -P)
exec python3 -B "$HERE/isolation_check.py" "$@"
