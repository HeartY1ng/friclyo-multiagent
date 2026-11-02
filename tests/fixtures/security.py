#!/usr/bin/env python3
"""离线钥匙串替身：只记录条目标识和命令参数，不读写真实钥匙串或任何密码。"""
import json
import os
from pathlib import Path
import sys
path = Path(os.environ['FRCLYO_CONFIG']).parent / 'fake-keychain.json'
names = json.loads(path.read_text()) if path.exists() else []
# 仅用于离线断言；不记录密码，add 的 -w 没有参数值。
with (path.parent / 'fake-security-calls.jsonl').open('a') as log:
    log.write(json.dumps(sys.argv[1:]) + '\n')
service = sys.argv[sys.argv.index('-s') + 1]
account = sys.argv[sys.argv.index('-a') + 1]
entry = {'service': service, 'account': account}
# 兼容旧测试的默认账户服务名列表；新用例同时核对 service/account。
identity = service if account == 'frclyo' and service in names else entry
command = sys.argv[1]
if command == 'find-generic-password':
    # 按条目及是否读取密码注入错误；只接受测试配置旁的替身规则。
    rules_path = path.parent / 'fake-security-results.json'
    rules = json.loads(rules_path.read_text()) if rules_path.exists() else []
    for rule in rules:
        if (rule['service'] == service and rule['account'] == account
                and rule['read'] == ('-w' in sys.argv)):
            if rule['code'] == 0 and '-w' in sys.argv:
                print(rule.get('value', 'OFFLINE-SENTINEL'))
            sys.exit(rule['code'])
    if identity not in names:
        sys.exit(44)
    if '-w' in sys.argv:
        print('OFFLINE-SENTINEL')
elif command == 'add-generic-password':
    if identity in names:
        sys.exit(45)
    names.append(entry)
    path.write_text(json.dumps(names))
elif command == 'delete-generic-password':
    if identity not in names:
        sys.exit(44)
    names.remove(identity)
    path.write_text(json.dumps(names))
else:
    sys.exit(1)
