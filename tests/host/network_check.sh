#!/bin/bash
# 真实 macOS 宿主网络检查；默认纯离线，不需要真 key，不调用模型。
# bash tests/host/network_check.sh
# 收费真实冒烟（沿用宿主配置的 keychain_entries；未配置时使用默认条目，不通过环境传 key）：
# bash tests/host/network_check.sh --live --models /path/to/models.json --opencode /path/to/opencode
set -eu
HOST_DIR=$(cd "$(dirname "$0")" && pwd -P)
export PYTHONDONTWRITEBYTECODE=1
python3 -B "$HOST_DIR/test_proxy_tcp.py"
exec python3 -B "$HOST_DIR/proxy_credentials.py" "$@"
