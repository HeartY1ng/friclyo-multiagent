"""配置、钥匙串和快照的离线边界测试；所有文件和命令替身都在临时目录。"""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

# 从交付目录导入公共库，不读取真实用户配置或钥匙串。
BIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BIN))
from frclyo import config, execution, keys, worker


class Isolated(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="frclyo-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.commands = self.root / "commands"
        self.commands.mkdir()
        self.cfg = self.root / "settings.json"
        self.environment = mock.patch.dict(os.environ, {
            "HOME": str(self.root), "FRCLYO_HOME": str(self.root / "AgentWork"), "FRCLYO_CONFIG": str(self.cfg),
            "PATH": str(self.commands) + os.pathsep + os.environ.get("PATH", ""),
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        os.environ.pop('CODEX_HOME', None)

    def write_config(self, **values):
        """测试仅在给定临时配置写样例，不复制任何真实配置。"""
        self.cfg.write_text(json.dumps(values), encoding="utf-8")

    def security(self, returncode=0):
        """实际执行一个假 security，验证参数和 stdout/stderr 的捕获方式。"""
        command = self.commands / "security"
        command.write_text("#!/bin/bash\n# 假钥匙串仅处理测试哨兵。\n"
                           "printf '%s\\n' \"$@\" > \"$HOME/security-args\"\n"
                           "if [ \"$1\" = find-generic-password ]; then\n"
                           "  printf '%s\\n' TEST-KEY-SENTINEL\n"
                           "  printf '%s\\n' TEST-ERROR-SENTINEL >&2\nfi\n"
                           f"exit {returncode}\n")
        command.chmod(0o700)

    def catalog(self):
        """最小 models.dev 形状足够测试校验与复制，不包含网络或私密数据。"""
        source = self.root / "models.json"
        source.write_text(json.dumps({"deepseek": {"id": "deepseek", "api": "https://api.deepseek.com",
            "npm": "@ai-sdk/openai-compatible", "models": {"deepseek-flash": {"id": "deepseek-flash"}}}}))
        return source


class ConfigTests(Isolated):
    def test_defaults_expansion_and_no_side_effects(self):
        cfg = config.load()
        self.assertEqual(cfg["work_dir"], str(self.root / "AgentWork"))
        self.assertEqual(cfg["codex"], {"proxy": "", "default_fast": True})
        self.assertEqual(len(cfg["providers"]), 5)
        self.assertEqual(cfg["providers"]["zhipuai"], {
            "opencode_family": "zhipuai", "api_host": "open.bigmodel.cn", "balance_api": "zhipu-no-api"})
        self.assertEqual(cfg["providers"]["zai-coding-plan"], {
            "opencode_family": "zai-coding-plan", "api_host": "api.z.ai", "balance_api": "zai-quota"})
        self.assertEqual(config.path_of("ledger_file"), str(self.root / "AgentWork/账本.jsonl"))
        self.assertFalse((self.root / "AgentWork").exists())
        cfg["codex"]["default_fast"] = False
        self.assertTrue(config.load()["codex"]["default_fast"])
        with self.assertRaises(ValueError):
            config.path_of("arbitrary")

    def test_partial_fields_and_provider_subset(self):
        self.write_config(work_dir="~/work", cards_dir="~/cards", perf_file="~/perf.jsonl",
                          key_env_file="~/legacy.env", codex={"default_fast": False},
                          providers={"deepseek": {"api_host": "API.EXAMPLE.COM"}})
        cfg = config.load()
        self.assertEqual(list(cfg["providers"]), ["deepseek"])
        self.assertEqual(cfg["providers"]["deepseek"]["opencode_family"], "deepseek")
        self.assertEqual(cfg["providers"]["deepseek"]["api_host"], "api.example.com")
        self.assertEqual(cfg["codex"]["proxy"], "")
        self.assertEqual(cfg["key_env_file"], str(self.root / "legacy.env"))
        for field, suffix in (("work_dir", "AgentWork"), ("deliver_dir", "AgentWork/交付"),
                              ("cards_dir", "AgentWork/任务卡"), ("ledger_file", "AgentWork/账本.jsonl"), ("perf_file", "AgentWork/表现记录.jsonl")):
            self.assertEqual(config.path_of(field), str(self.root / suffix))

    def test_empty_providers_does_not_restore_defaults(self):
        self.write_config(providers={})
        self.assertEqual(keys.list_providers(), [])

    def test_invalid_configuration_fails_without_values(self):
        for raw in ('[]', '{broken', '{"version":2}', '{"codex":null}', '{"providers":[]}',
                    '{"board_port":0}', '{"board_port":true}', '{"codex":{"default_fast":0}}'):
            with self.subTest(raw=raw):
                self.cfg.write_text(raw)
                with self.assertRaises(ValueError):
                    config.load()
        for host in ("127.0.0.1", "api.example.com:443", "https://api.example.com", "*.example.com", "api.example.com."):
            self.write_config(providers={"deepseek": {"api_host": host}})
            with self.assertRaises(ValueError):
                config.load()

    def test_layout_creation_and_custom_family(self):
        self.write_config(work_dir="~/new", perf_file="~/separate/perf.jsonl",
                          providers={"custom": {"opencode_family": "deepseek", "api_host": "custom.example.com"}})
        cfg = config.ensure_layout()
        self.assertTrue(Path(cfg["cards_dir"]).is_dir())
        self.assertTrue(Path(cfg["deliver_dir"]).is_dir())
        self.assertFalse(Path(cfg["ledger_file"]).exists())
        self.assertTrue(Path(cfg["perf_file"]).parent.is_dir())
        self.assertFalse(Path(cfg["perf_file"]).exists())


class KeyTests(Isolated):
    def test_each_keychain_layer_only_falls_through_on_44(self):
        fallback = self.root / 'fallback.env'
        fallback.write_text('DEEPSEEK_API_KEY=ENV-SENTINEL\n')
        self.write_config(key_env_file=str(fallback), keychain_entries={
            'deepseek': {'service': 'my-deepseek', 'account': 'me'}})
        missing = subprocess.CompletedProcess([], 44, stdout='')
        failures = [subprocess.CompletedProcess([], code, stdout='PRIVATE-SENTINEL')
                    for code in (1, 36, 51)]
        failures += [subprocess.CompletedProcess([], 0, stdout='\r\n'),
                     FileNotFoundError('PRIVATE-SENTINEL'),
                     subprocess.TimeoutExpired('PRIVATE-SENTINEL', 10),
                     UnicodeError('PRIVATE-SENTINEL')]
        for layer in (0, 1):
            for failure in failures:
                with self.subTest(layer=layer, failure=type(failure).__name__), \
                        mock.patch.object(keys.subprocess, 'run', side_effect=[missing] * layer + [failure]) as run, \
                        mock.patch.object(keys, '_env_value', side_effect=AssertionError('no fallback')), \
                        contextlib.redirect_stderr(io.StringIO()) as err:
                    self.assertIsNone(keys.get_key('deepseek'))
                    self.assertEqual(run.call_count, layer + 1)
                    self.assertIn('钥匙串读取失败', err.getvalue())
                    self.assertIn('允许', err.getvalue())
                    self.assertNotIn('PRIVATE-SENTINEL', err.getvalue())
        for replies, expected in (([missing, subprocess.CompletedProcess([], 0, stdout='DEFAULT-SENTINEL\n')],
                                    'DEFAULT-SENTINEL'), ([missing, missing], 'ENV-SENTINEL')):
            with mock.patch.object(keys.subprocess, 'run', side_effect=replies), \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(keys.get_key('deepseek'), expected)
                self.assertEqual(err.getvalue(), '')

    def test_source_unknown_stops_at_each_layer_without_reading_password(self):
        self.write_config(keychain_entries={'deepseek': {'service': 'my-deepseek', 'account': 'me'}})
        for layer in (0, 1):
            for failure in (subprocess.CompletedProcess([], 36), FileNotFoundError(),
                            subprocess.TimeoutExpired('security', 10)):
                with self.subTest(layer=layer, failure=type(failure).__name__), \
                        mock.patch.object(keys.subprocess, 'run', side_effect=[subprocess.CompletedProcess([], 44)] * layer + [failure]) as run, \
                        mock.patch.object(keys, '_env_value', side_effect=AssertionError('no fallback')):
                    source = keys.key_source('deepseek')
                    self.assertEqual(source['kind'], 'unknown')
                    self.assertEqual(source['external'], layer == 0)
                    self.assertEqual(run.call_count, layer + 1)
                    self.assertTrue(all('-w' not in call.args[0] for call in run.call_args_list))
                    self.assertIn('无法确认', keys.source_label(source))

    def test_list_providers_uses_key_source_and_returns_only_names(self):
        with mock.patch.object(keys, 'key_source', side_effect=[{'kind': 'keychain'}] + [{'kind': 'none'}] * (len(config.load()['providers']) - 1)) as source:
            self.assertEqual(keys.list_providers(), ['deepseek'])
        self.assertEqual([call.args[0] for call in source.call_args_list], list(config.load()['providers']))

    def test_list_providers_unknown_does_not_use_env_fallback(self):
        fallback = self.root / 'legacy.env'
        fallback.write_text('DEEPSEEK_API_KEY=ENV-SENTINEL\nKIMI_CN_API_KEY=\nXIAOMI_API_KEY=XIAOMI-SENTINEL\n')
        self.write_config(key_env_file=str(fallback))
        for failure in (FileNotFoundError(), subprocess.TimeoutExpired('security', 10)):
            with self.subTest(failure=type(failure).__name__), mock.patch.object(keys.subprocess, 'run', side_effect=failure):
                self.assertEqual(keys.list_providers(), [])
                self.assertEqual(keys.key_source('deepseek')['kind'], 'unknown')
        fallback.unlink()
        with mock.patch.object(keys.subprocess, 'run', side_effect=FileNotFoundError()):
            self.assertEqual(keys.list_providers(), [])

    def test_fake_security_arguments_and_no_key_output(self):
        self.security()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            value = keys.get_key("deepseek")
        self.assertEqual(value, "TEST-KEY-SENTINEL")
        self.assertEqual(out.getvalue() + err.getvalue(), "")
        self.assertEqual((self.root / "security-args").read_text().splitlines(),
                         ["find-generic-password", "-s", "frclyo-deepseek", "-a", "frclyo", "-w"])

    def test_failure_fallback_is_explicit_and_never_executes_env(self):
        self.security(44)
        self.assertIsNone(keys.get_key("deepseek"))
        env = self.root / "legacy.env"
        env.write_text('export DEEPSEEK_API_KEY="ENV-SENTINEL"\nKIMI_CN_API_KEY=\'KIMI-SENTINEL\'\nXIAOMI_API_KEY=$(touch SHOULD-NOT-EXIST)\n')
        self.assertIsNone(keys.get_key("deepseek"))
        self.write_config(key_env_file=str(env))
        self.assertEqual(keys.get_key("deepseek"), "ENV-SENTINEL")
        self.assertEqual(keys.get_key("moonshotai-cn"), "KIMI-SENTINEL")
        self.assertEqual(keys.get_key("xiaomi"), "$(touch SHOULD-NOT-EXIST)")
        self.assertFalse((self.root / "SHOULD-NOT-EXIST").exists())
        self.security(0)
        self.assertEqual(keys.get_key("deepseek"), "TEST-KEY-SENTINEL")

    def test_missing_security_timeout_and_unknown_provider(self):
        for exception in (FileNotFoundError(), subprocess.TimeoutExpired("security", 10)):
            with mock.patch.object(keys.subprocess, "run", side_effect=exception):
                self.assertIsNone(keys.get_key("deepseek"))
        with mock.patch.object(keys.subprocess, "run") as command:
            self.assertIsNone(keys.get_key("unknown"))
            command.assert_not_called()

    def test_set_key_confirmation_without_install_record(self):
        self.security()
        with mock.patch("builtins.input", return_value="n"):
            self.assertFalse(keys.set_key("deepseek"))
        self.assertFalse((self.root / "security-args").exists())
        with mock.patch("builtins.input", return_value="y"):
            keys.set_key("deepseek")
        args = (self.root / "security-args").read_text().splitlines()
        self.assertEqual(args, ["add-generic-password", "-s", "frclyo-deepseek", "-a", "frclyo", "-w"])
        log = self.root / "AgentWork/配置/安装记录.jsonl"
        self.assertFalse(log.exists())

    def test_failed_set_does_not_claim_success(self):
        self.security(1)
        with mock.patch("builtins.input", return_value="y"), self.assertRaises(RuntimeError):
            keys.set_key("deepseek")
        self.assertFalse((self.root / "AgentWork/配置/安装记录.jsonl").exists())


class WorkerTests(Isolated):
    def test_key_read_failure_is_reported_without_injection_or_fallback(self):
        self.security(51)
        with contextlib.redirect_stderr(io.StringIO()) as err, \
                mock.patch.dict(os.environ, DEEPSEEK_API_KEY='ENV-SENTINEL'), \
                mock.patch.object(keys, '_env_value') as fallback:
            os.environ.pop('FRCLYO_PROVIDER_API_KEY', None)
            # 先实际运行假 security；其 subprocess 自己需要 pipe，不能一并替换。
            value = keys.get_key('deepseek')
            self.assertIsNone(value)
            with mock.patch.object(keys, 'get_key', return_value=value) as get_key, \
                 mock.patch.object(execution.os, 'getppid', return_value=123), \
                 mock.patch.object(execution.os, 'pipe') as pipe, \
                 mock.patch.object(execution.os, 'execve') as execute, \
                 mock.patch.object(execution.subprocess, 'Popen') as popen:
                self.assertEqual(execution.start_proxy('deepseek', str(self.root / 'control'), '123'), 2)
            self.assertNotIn('FRCLYO_PROVIDER_API_KEY', os.environ)
        get_key.assert_called_once_with('deepseek')
        fallback.assert_not_called()
        pipe.assert_not_called()
        execute.assert_not_called()
        popen.assert_not_called()
        self.assertIn('钥匙串读取失败', err.getvalue())
        self.assertIn('代理和工人未启动', err.getvalue())
        self.assertNotIn('SENTINEL', err.getvalue())

    def test_sensitive_paths_protect_custom_codex_home(self):
        custom = self.root / 'custom Codex 中文'
        with mock.patch.dict(os.environ, CODEX_HOME=str(custom)):
            self.assertIn(str(custom), worker.sensitive_paths(config.load()))

    def test_snapshot_copy_and_selected_provider(self):
        source = self.catalog()
        self.write_config(models_snapshot_source=str(source))
        prepared = worker.prepare("deepseek/deepseek-flash")
        content = json.loads(worker.local_config(prepared["content"], "deepseek", 43210))
        self.assertEqual(prepared["target"], "api.deepseek.com:443")
        self.assertEqual(prepared["model"], "deepseek/deepseek-flash")
        self.assertEqual(content["enabled_providers"], ["deepseek"])
        options = content["provider"]["deepseek"]["options"]
        self.assertEqual(options["baseURL"], "http://127.0.0.1:43210/deepseek")
        self.assertEqual(options["apiKey"], worker.DUMMY_API_KEY)
        self.assertEqual(Path(prepared["snapshot"]).read_bytes(), source.read_bytes())
        source.unlink()
        self.assertEqual(worker.prepare("deepseek/deepseek-flash")["snapshot"], prepared["snapshot"])
        self.assertEqual(content["permission"]["task"], "deny")
        self.assertEqual(content["agent"]["worker"]["steps"], 40)

    def test_custom_auth_destination_is_rejected(self):
        source = self.catalog()
        self.write_config(models_snapshot_source=str(source),
                          providers={"deepseek": {"api_host": "custom.example.com"}})
        with self.assertRaises(ValueError):
            worker.prepare("deepseek/deepseek-flash")

    def test_default_local_cache_and_missing_model_fail_closed(self):
        source = self.catalog()
        cache = self.root / "cache" / "opencode" / "models.json"
        cache.parent.mkdir(parents=True)
        shutil.copyfile(source, cache)
        self.write_config(work_dir="~/work")
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.root / "cache")}):
            worker.prepare("deepseek/deepseek-flash")
            with self.assertRaises(ValueError):
                worker.prepare("deepseek/deepseek-v4-pro")
        with self.assertRaises(ValueError):
            worker.prepare("deepseek/unlisted-model")
        with self.assertRaises(ValueError):
            worker.prepare("unknown/deepseek-flash")

    def test_missing_corrupt_snapshot_actionable_and_atomic(self):
        self.write_config(models_snapshot_source=str(self.root / "missing"))
        with self.assertRaisesRegex(ValueError, "opencode models --refresh"):
            worker.prepare("deepseek/deepseek-flash")
        (self.root / "missing").write_text("broken")
        with self.assertRaisesRegex(ValueError, "models_snapshot_source"):
            worker.prepare("deepseek/deepseek-flash")
        self.assertFalse((self.root / "AgentWork/运行时/opencode/models.snapshot.json").exists())

    def test_worker_environment_contains_no_host_credentials(self):
        source = dict(HOME='/fake', PATH='/bin', OTHER_API_KEY='secret', ANOTHER_TOKEN='secret',
                      BENIGN_LOOKING='secret', HTTPS_PROXY='http://evil', OPENCODE_AUTH_CONTENT='secret',
                      OPENCODE_CONFIG='/fake/config', OPENCODE_DISABLE_SHARE='1')
        with mock.patch.object(keys, 'get_key', side_effect=AssertionError('工人监督不应取 key')):
            environment = worker.worker_environment(source)
        self.assertNotIn('secret', repr(environment))
        self.assertEqual(environment['OPENCODE_AUTH_CONTENT'], '{}')
        self.assertEqual(environment['OPENCODE_CONFIG'], '/fake/config')
        self.assertNotIn('HTTPS_PROXY', environment)
        self.assertIn('OTHER_API_KEY', source)  # 不修改宿主环境

    def test_jsonc_preserves_urls_and_string_comment_markers(self):
        path = self.root / "template.jsonc"
        path.write_text('{/* 中文块注释 */ "url":"https://example.com/a//b", // 行注释\n "text":"/* 保留 */"}')
        self.assertEqual(worker.read_jsonc(path), {"url": "https://example.com/a//b", "text": "/* 保留 */"})

    def test_shells_and_embedded_python_parse(self):
        # bash -n 不检查 heredoc 内的 Python，因此二者分别检查，适用 Python 3.9 语法。
        import ast
        for path in (BIN / "codex-worker.sh", BIN / "opencode-worker.sh", BIN.parent / "tests/host/network_check.sh"):
            subprocess.run(["/bin/bash", "-n", str(path)], check=True)
            source = path.read_text()
            import re
            for tag, body in re.findall(r"<<'([A-Z]+)'[^\n]*\n(.*?)\n\1(?:\n|$)", source, re.S):
                ast.parse(body, feature_version=(3, 9))
        for path in BIN.rglob("*.py"):
            ast.parse(path.read_text(), feature_version=(3, 9))

    def test_generated_sandbox_uses_exact_port_and_protected_paths(self):
        """直接执行生产脚本中的规则生成段，不用测试副本替代它。"""
        source = (BIN / "opencode-worker.sh").read_text()
        code = source.split('python3 -I - "$BIN_DIR" "$HOME"', 1)[1].split("<<'PY'\n", 1)[1].split("\nPY", 1)[0]
        home = self.root / "home"
        run = self.root / "outside home" / "交付" / "TASK" / "r1"
        runtime = self.root / "runtime" / "runtime"
        control = self.root / "runtime" / ".network-test"
        for path in (home, run, runtime, control):
            path.mkdir(parents=True, exist_ok=True)
        profile = control / "sandbox.sb"
        sensitive = [str(self.root / "separate credentials.env")]
        protected = [str(run.parent), str(runtime.parent), str(self.root / "template.jsonc")]
        args = [str(BIN), str(home), str(run.parents[2]), str(run), str(runtime), str(control), "43210", str(profile), "123",
                "api.example.com:443", json.dumps(sensitive), json.dumps(protected)]
        proc = subprocess.run([sys.executable, "-I", "-c", code] + args, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rules = profile.read_text()
        self.assertIn('(deny network*)', rules)
        self.assertEqual(rules.count('(allow network-outbound'), 1)
        self.assertIn('(remote tcp4 "localhost:43210")', rules)
        def quoted(path):
            return json.dumps(str(Path(path).resolve()), ensure_ascii=False)

        # 元数据可读不代表内容或扩展属性可读；控制目录三种禁令必须同时存在。
        self.assertIn('(deny file-read-data file-read-xattr file-write* (subpath ' + quoted(control) + '))', rules)
        secret_paths = [home / name for name in (
            '.hermes', '.frclyo', '.ssh', '.codex', '.claude', '.gnupg', '.config',
            '.aws', '.docker', '.netrc', '.local/share/opencode', 'Library/Keychains')]
        secret_paths.extend(Path(path) for path in sensitive)
        for path in secret_paths:
            with self.subTest(secret=str(path)):
                selector = '(subpath ' + quoted(path) + ')'
                for operations in ('file-read-data file-read-xattr', 'file-write*'):
                    self.assertTrue(any(line.startswith('(deny ' + operations + ' ')
                                        and selector in line for line in rules.splitlines()),
                                    (str(path), operations))
        for path in protected:
            self.assertTrue(any(line.startswith('(deny file-write* ')
                                and '(require-all (subpath ' + quoted(path) + ')' in line
                                for line in rules.splitlines()), path)
        self.assertIn('(deny file-write* (literal ' + quoted(run / '代理拒绝.log')
                      + ') (literal ' + quoted(run / '网络信息.json') + '))', rules)
        info = json.loads((run / "网络信息.json").read_text())
        self.assertEqual(info["放行"], ["api.example.com:443"])


if __name__ == "__main__":
    unittest.main()
