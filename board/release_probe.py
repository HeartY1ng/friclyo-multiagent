"""看板的只读发布探测；由父进程限制运行时间，不加载用户配置。"""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
from frclyo import release


def read(root, operation):
    root = Path(root).resolve()
    if operation == 'versions':
        rows, issue = release.version_rows(root)
        # path 为 Path；只传页面需要的字段，不传路径和原始管理记录。
        fields = ('version', 'published_at', 'valid', 'rollbackable', 'problem',
                  'deleting', 'workers', 'usage_unknown')
        return {'rows': [{key: row.get(key) for key in fields} for row in rows], 'issue': issue}
    if operation == 'usage':
        fields = ('version', 'worker', 'task', 'round', 'pid', 'started_at', 'active')
        return {'rows': [{key: row.get(key) for key in fields}
                         for row in release.worker_usage(root)], 'issue': ''}
    raise ValueError('unknown operation')


if __name__ == '__main__':
    try:
        result = read(sys.argv[1], sys.argv[2])
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    except Exception:
        # 不把异常中的文件内容或路径送入页面/日志。
        print(json.dumps({'rows': None, 'issue': '读取失败，未知'}, ensure_ascii=False))
