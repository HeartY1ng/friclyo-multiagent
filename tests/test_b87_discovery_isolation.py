"""期望：坏掉的另一套工人清单不能抹掉已发现的可用 Codex 候选。"""
import contextlib
import io
import json
import unittest
from unittest import mock
from proxy_regression_support import aw_fixture


class DiscoveryIsolation(unittest.TestCase):
    def test_enabled_opencode_template_errors_keep_codex(self):
        for error in (FileNotFoundError(), PermissionError(), ValueError()):
            with self.subTest(error=type(error).__name__), aw_fixture() as (aw, root, cfg, stack):
                models = aw.runtime('models')
                cfg.write_text('{}')  # 缺省 providers 非空，必须实际尝试模板读取。
                stack.enter_context(mock.patch.object(models.shutil, 'which', return_value='/fixture/installed'))
                stack.enter_context(mock.patch.object(models, 'codex_catalog', return_value=(
                    [{'slug': 'healthy-codex', 'visibility': 'list'}], '命令行')))
                stack.enter_context(mock.patch.object(models, 'cli_version', return_value='fixture'))
                read = stack.enter_context(mock.patch.object(models, 'read_jsonc', side_effect=error))
                keys = stack.enter_context(mock.patch.object(models.keys, 'key_source'))
                with contextlib.redirect_stderr(io.StringIO()) as diagnostic:
                    found = models.discover()
                read.assert_called_once()
                keys.assert_not_called()
                self.assertEqual([(row['worker'], row['model']) for row in found], [('codex', 'healthy-codex')])
                self.assertIn('OpenCode 候选不可用', diagnostic.getvalue())

    def test_missing_opencode_template_keeps_codex_candidate(self):
        with aw_fixture() as (aw, root, cfg, stack):
            models = aw.runtime('models')
            cfg.write_text(json.dumps({'providers': {}, 'opencode_config': str(root / 'missing.jsonc')}))
            catalog = [{'slug': 'healthy-codex', 'visibility': 'list',
                        'supported_reasoning_levels': [{'effort': 'medium'}]}]
            stack.enter_context(mock.patch.object(models.shutil, 'which', return_value='/fixture/installed'))
            stack.enter_context(mock.patch.object(models, 'codex_catalog', return_value=(catalog, '命令行')))
            stack.enter_context(mock.patch.object(models, 'cli_version', return_value='fixture'))
            read = stack.enter_context(mock.patch.object(models, 'read_jsonc', side_effect=AssertionError('无提供商不应读模板')))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = aw.doctor([])
            print('doctor rc =', rc)
            print(out.getvalue().strip())
            # 不要求坏 OpenCode 的整体诊断变绿，只要求继续展示健康工人。
            self.assertIn('候选组合：codex × healthy-codex', out.getvalue())
            read.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
