# friclyo-multiagent

**Prepare your APIs and agent frameworks locally. Pick what each task needs.**

[中文](README.md) · [Changelog](CHANGELOG.en.md) · [Design notes (Chinese)](docs/思路.md) · [Case study (Chinese)](docs/案例/2026-10-13-切词实战.md)

> The project name is **friclyo** (since v0.3.1; formerly frclyo). Internal identifiers such as the code directory `bin/frclyo/`, `FRCLYO_*` environment variables and `frclyo-*` keychain entries keep the frclyo prefix; nothing changes in use.

Build a local pool of models and agents. The coordinator chooses a framework, model, and reasoning level based on the task and how its output can be verified. It defines the contract, dispatches work, and checks results. Workers deliver artifacts; the board shows recorded state.

**The first release supports macOS only, using Python 3.9+ standard libraries and system bash.** Coordinators: Claude Code and Codex. Workers: Codex and OpenCode, with API providers such as DeepSeek, Kimi, MiMo, and GLM (international coding subscription: Z.AI Coding Plan, `zai-coding-plan`; China pay-as-you-go: Zhipu AI, `zhipuai`) through OpenCode. Each model and reasoning configuration needs a real compatibility check.

```text
You → Coordinator (Claude or Codex) → Ledger
                                       ↓
       Workers (Codex; OpenCode with DeepSeek / Kimi / MiMo / GLM (Z.AI Coding Plan / Zhipu AI))
                                       ↓
                  Receipt → Acceptance checks → Board
```

The ledger records task IDs, rounds, and state. A finished process is distinct from an accepted result; the board does not replace artifact verification.

## Three principles

1. **Define the contract before splitting work.** Specify interfaces, formats, file ownership, and malformed-input handling. Keep tightly coupled work together. Cognition describes how implicit decisions can conflict across agents; contracts are our engineering response to that problem. [Source: Don’t Build Multi-Agents](https://cognition.ai/blog/dont-build-multi-agents)
2. **A worker's claim is not evidence. Check receipts and rerun verification.** Check contract compliance, decision consistency, and the complete pipeline. Compare bytes against a reference implementation when practical. MAST identifies task verification as a category of multi-agent failure; our receipts and three checks address that risk. [Source: MAST](https://arxiv.org/abs/2503.13657)
3. **Make work traceable and reversible.** Keep task and progress ledgers, isolate execution rounds, and accept changes before merging them. Magentic-One informs the ledger design. Installation tracking, uninstall support, and rollback boundaries are our own design goals, not capabilities established by that paper. [Source: Magentic-One](https://arxiv.org/abs/2411.04468)

## Three steps to get started

Install and log in to Codex, or prepare OpenCode and provider credentials. The parallel demo needs Codex (two workers are supported), Codex plus OpenCode, or two different OpenCode providers. The framework does not globally install CLIs or log in for you. Before first use of OpenCode, run `opencode models --refresh` outside the worker to populate its local model cache.

Clone the repository and enter it:

```sh
git clone https://github.com/HeartY1ng/friclyo-multiagent.git
cd friclyo-multiagent
```

1. Install:

   ```sh
   ./install.sh
   ```

   The installer checks the environment and automatically completes the work-directory layout; configuration, command links, the Claude skill, and board startup are confirmed separately, and new directories are recorded. Add `~/.local/bin` to PATH if prompted, or use `./bin/aw` directly.

   The installer does not create `配置/偏好.md`. Before enabling a coordinator, the user must copy `偏好.模板.md` from the repository to `配置/偏好.md` under the data root, fill it in and confirm personal choices. Preferences are separate from runtime configuration; template examples do not grant authorization.

2. Register an API:

   ```sh
   aw key add deepseek
   ```

   After confirmation, macOS Keychain handles hidden terminal input. Credentials stay out of conversations, project files and the installation journal.

3. Run the demo:

   ```sh
   aw demo
   ```

   Confirm the worker choices and cost. Two workers implement CSV normalization and category totals in parallel. Receipts are verified, then three gates run automatically: byte comparisons for samples, both workers' tests, and a byte comparison of the complete pipeline. Demo tasks use the independent `DM` source, leaving `CX` numbering untouched. The demo currently checks only whether the configured port is listening; any listener is treated as an existing board and opened in the browser. It does not verify service identity, and will not start a temporary board when another service occupies the port. With no listener, it starts a temporary board that stops after the demo; use `aw board start` to view the saved tasks again.

The data root is `FRCLYO_HOME` (default `~/AgentWork`), with configuration in `配置/config.json`. If absent, the legacy `~/.frclyo/config.json` is read without migration; doctor asks you to migrate. Tests can use `FRCLYO_CONFIG`. Legacy `work_dir`, `cards_dir`, and `perf_file` fields are warned about and ignored. Codex files are read from `CODEX_HOME` when set, otherwise from `~/.codex`. Credentials are looked up in the configured external Keychain entry, the default `frclyo-<provider>` / `frclyo` entry, then an explicitly configured `key_env_file`. Each Keychain layer falls through only on exit code 44 (item not found). `aw key list` prints one line per configured provider with its source, including unavailable and unknown states, without reading Keychain passwords. Codex model discovery queries the local `codex app-server` first and falls back to the shared cache on failure. A cache from a different CLI version supplies candidates but does not block execution. Listing a model does not establish verified compatibility. `aw doctor` lists local candidates; an unstarted board is only a warning and does not affect its exit code. `aw doctor --live` confirms costs and tests one combination per worker. `aw calibrate` selects one default level per candidate model for three questions, not every available level. Token usage preserves the receipt's raw object; unknown costs remain null.

Availability is stored in `$FRCLYO_HOME/模型可用性.json`, keyed by worker, model and actual CLI version, with a UTC timestamp and reason. Discovery returns `availability` and `cli_version`, ranks `ok` before `unknown`, and excludes `unusable` by default; `models.discover(include_unusable=True)` includes all candidates. A CLI version change makes old conclusions inapplicable. Reads and atomic writes are locked.

Demo, calibration and live checks switch models only after an explicit unsupported-model error: at most three replacements (four attempts total), each in a new round with preserved evidence. OpenCode may switch registered providers, while the parallel demo avoids providers occupied by its other worker. Other execution or grading failures do not trigger a retry. A normal worker completion records `ok`; artifact quality is graded separately. Demo failure returns a nonzero exit code. Calibration returns 0 once its report is written, even when combinations fail; the report lists failed rounds, reasons and evidence. Live checks return success only if every worker's final attempt passes.

You can reuse an existing Keychain entry with optional `keychain_entries` (these are fictitious names):

```json
{
  "keychain_entries": {
    "deepseek": {"service": "my-deepseek", "account": "me"}
  }
}
```

The provider must exist in `providers`. Service and account must be nonempty strings, contain no control characters, and must not start with `-`. An absent external entry falls through to the default entry; an absent default entry falls through to the configured file. Other errors, timeouts, and empty retrieved values stop lookup. `get_key` returns None and writes a fixed diagnostic to stderr suggesting allowing the Keychain prompt or unlocking Keychain; it never prints credentials. A failed or timed-out presence query is shown as unknown and is not counted as an available credential.

`aw key add` writes only the default entry. It refuses when an external entry exists or its presence is unknown; update it with the original tool, or remove that provider's `keychain_entries` mapping first. Unknown default entries also block writes. A file fallback allows adding a default entry, with a notice that Keychain will then take priority. For an existing default entry registered by this tool, run `aw key remove <provider>` before replacing it. For an unregistered default entry, use Keychain Access or `security` manually. Remove and uninstall delete only default entries registered by this tool whose registration has not been undone; they preserve external entries, unregistered default entries and fallback files.

Example `aw key list` output (the CLI uses Chinese labels; no passwords are shown):

```text
deepseek：钥匙串（外部）：my-deepseek（账户 me）
moonshotai-cn：钥匙串（默认）：frclyo-moonshotai-cn（账户 frclyo）
xiaomi：无可用 key
zai-coding-plan：无可用 key
zhipuai：无可用 key
```

Other states include `deepseek：回退文件：/example/legacy.env` (file fallback) and `deepseek：无法确认（钥匙串锁定或拒绝访问）` (unknown: Keychain locked or access denied). List, doctor model discovery, and board presence checks for providers without a balance API do not read Keychain passwords. Actual balance requests still retrieve credentials. Presence does not guarantee that a credential can be read or is valid.

## Command reference

The following block reproduces `aw help` line for line (the CLI help is Chinese). English aliases and usage are explained below.

```text
aw：本机多工人入口（中文/英文命令均可）
  aw key add|remove <家>          钥匙串登记或删除已登记条目
  aw key list                     列出各家的凭据来源
  aw board [start|stop|open|status] 前台启动、停自启、打开、状态
  aw ledger …                   原样传给账本
  aw card new <模板> <任务号> [--title …] [--round r1]  新建任务卡（中文：卡 新建、--标题、--轮）
  aw changelog append --change "…" --undo "…"  加锁分配台账编号
  aw 台账 追加 --改动 "…" --撤销 "…"            同上
  aw clean [--days N] [--yes]    清理收卡超过 N 天的私有目录；默认列出，删除另需确认
  aw merge <任务> <轮> --到 <开发版目录> [--基准 <目录>] [--试跑]  三方合并（中文：合并）
  aw repro <任务> <轮> [--测试 名字] [--框架 输入|交付|路径] [--不套沙箱]  复现交付测试（中文：复现；英文选项：--test、--framework、--no-sandbox）
  aw run codex|opencode …        原样传给执行脚本
  aw dispatch 队列.json [--并行 N] [--额度上限 P] [--试跑]  一次派一批（中文：派活）
  aw release [--paid|--migrate|--undo-migration] / rollback [版本] / versions（发布 / 回滚 / 版本）
  aw verify [--paid]            宿主统一验收（中文：验收）；默认不调用真实模型
  aw doctor [--live]             本机自检；live 确认费用后跑小题
  aw demo                       契约 → 两个工人并行 → 三关验收
  aw calibrate                  确认组合和费用后跑三题，生成路由建议
  aw uninstall [--dry-run] [--delete-data]  默认保留数据；删除另需完整路径确认
  aw help                       本说明
中文：密钥（添加/列表/删除）、看板（启动/停止/打开/状态）、账本、运行、自检、演示、校准、卸载、帮助
安装/演示/校准支持 --yes、--no-input；卸载 --yes 只确认撤销安装（含本工具登记的钥匙串条目），删除数据须另加 --delete-data 并逐字输入完整路径，使用前请先 --dry-run。

```

### Audits and receipt exceptions

The read-only audit checks cards marked completed with a passing verdict. If a card has only `r<number>` rounds, it checks the latest numeric round, so `r10` follows `r9`; a latest numeric directory without a receipt remains an error. Named rounds must use only ASCII letters, digits, hyphens or underscores, must not start with a hyphen, and contain `回执.json` or `运行信息.json`. Ordinary document directories do not count. When any named round exists, every round is checked, including numeric rounds. Each receipt must match the task and round identifiers, report successful execution, and match the result's SHA-256. Problems are counted per card. The report states the total and failing round counts, lists at most five failing round names with reasons, and appends `等` when more exist. Parent cards with no rounds retain their existing child-card completion checks.

After reviewing artifacts and completing host validation, the coordinator can explain a receipt that does not reflect the verified result:

```sh
aw ledger done <task> --结论 通过 --摘要 "Artifacts and host tests checked" --receipt-exception "Receipt finalization failed; host tests passed"
aw ledger receipt-exception <task> "Receipt finalization failed; artifacts and host checks verified"
aw ledger show <task>
```

The first command appends a note beginning exactly with `回执例外：` (including the full-width colon), then the completion event in the same ledger append. Before closing, `aw ledger note <task> "回执例外：<reason>"` also works. Reasons must contain non-whitespace text. The second command registers a later explanation for any opened task, including completed or abandoned tasks; unknown tasks are rejected. Reopening is not required. Chinese aliases are `完成 --回执例外`, `回执例外`, and `查`.

Later registrations append to `回执例外.jsonl` beside the ledger. Each line has `时间`, `任务`, and `原因`, using the ledger's UTC second-resolution time (for example `2026-11-01T10:00:00Z`), locking, append and fsync mechanisms. Ledger bytes, event formats and state rules remain unchanged. This separate file preserves rollback compatibility: ordinary events appended to closed tasks would make older versions reject the ledger as corrupt. Older versions ignore the separate file, so older audits will not recognize exceptions recorded only there.

Audits combine both sources and select the latest nonempty reason per task. Equal timestamps favor the separate file; within either source the last record wins. Empty reasons do not clear an explanation, and ordinary notes do not replace it. Malformed separate-file lines are skipped with line-number warnings; other records remain usable. `aw ledger show` lists valid records and identifies their source as `账本备注` or `回执例外.jsonl`.

**Exceptions waive only receipt or delivery-evidence problems, never unclosed child cards under a parent. After a card is reopened, earlier exceptions do not apply to subsequent new rounds; rounds that existed when the exception was registered remain waived. The dedicated commands (`done --receipt-exception`, `receipt-exception`) record the rounds that exist at registration and waive only those rounds; legacy records without a list fall back to time comparison, and the same second is not waived.** History is retained; exceptions do not change task state or receipts or replace artifact validation. Healthy evidence still produces success. Applicable evidence problems appear as `⚠️ 回执例外已说明：<reason>`, retaining the task and original issue without increasing the problem count. JSON places these entries in `提醒`; the audit exits 0 if no other problems remain. The board shows a gray explained exception under “有空看看”, rather than an urgent issue or a pending decision.

### Create a task card

`aw card new bughunt CL-20261101-05` creates `$FRCLYO_HOME/任务卡/CL-20261101-05-r1.txt` for an existing ledger task. Templates are `bughunt`, `fix`, `review`, and `experiment`, with Chinese short names and the corresponding `…卡.md` filenames also accepted. The project and default title come from the ledger; `--title` and `--round` override the title and round. This creates an editable draft only: it does not dispatch work, modify the ledger, or register an execution round, and it refuses any occupied destination name.

Round names must be nonempty, contain only ASCII letters, digits, `-` and `_`, and must not begin with `-`. Card creation, dispatch and worker entry points use this same constraint.

### Batch dispatch

```sh
aw dispatch queue.json --dry-run
aw dispatch queue.json --parallel 3 --quota-limit 95
# Chinese aliases:
aw 派活 队列.json --并行 3 --额度上限 95 --试跑
```

The queue is a JSON array using Chinese field names:

```json
[
  {"任务":"CL-20261101-01","工人":"codex","模型":"gpt-6-astra","档位":"high","超时":7200},
  {"任务":"CL-20261101-02","轮":"r1","工人":"opencode","模型":"deepseek/available-model","变体":"","超时":1800}
]
```

Use existing, nonterminal ledger tasks and models available on the host. `轮` defaults to `r1`, `卡` to `任务卡/<task>-<round>.txt`, and `输入` to `素材/<task>`. Relative card/input paths are based on `$FRCLYO_HOME`; absolute paths are accepted. Prepare an empty input directory if no materials are needed. Codex switches `网页` (web), `命令网络` (command network), `子agent` (subagents), and `快速档` (fast tier) accept booleans or `0/1` and default to off, overriding inherited environment settings. Enabling these switches for OpenCode fails validation because its existing worker interface does not support them.

Validation also checks fields, paths and task state. Dry runs do not query live quota or modify the ledger. Default concurrency is three. OpenCode jobs for the same provider are serialized across batches sharing a data root; direct worker-script calls do not participate in this lock. An unreadable or unidentified 10080-minute weekly quota fails that Codex item without launching it. The quota threshold stops launches, not consumption by running jobs. Per-item results remain in `<batch-id>.jsonl`, with exclusive `.log` files for stdout/stderr; dispatch adds ledger notes and does not close tasks. Exit 0 means all completed or a valid dry run; exit 1 includes execution failures and quota skips.

Both `aw dispatch queue.json --dry-run` and normal dispatch validate models and reasoning levels before starting any worker. Codex uses visible models and supported reasoning efforts returned by the local CLI's `model/list`; the App cache is not substituted for this CLI catalog. OpenCode uses the current worker configuration's model whitelist and explicit variants, following the framework's existing discovery logic. Disabled variants are rejected; an empty string selects the default variant. Each worker catalog is read once per validation pass. An unreadable catalog produces a warning without blocking dispatch on that check. Catalog membership does not establish successful live execution. Errors identify the queue item and reject the entire batch with exit code `2`. Codex `ultra` still requires `子agent=1`.

For Codex CLI `0.153.x`, enabling subagents is allowed only with `gpt-5.6-luna`. Other models have a known exec-mode failure (`no thread with id`) and astra may report fabricated success. Both dispatch validation and `codex-worker.sh` reject that combination with exit code `2`. Set `CODEX_WORKER_SUBAGENTS_FORCE=1` explicitly to bypass this version compatibility guard only to test a new version. This does not bypass model, reasoning-level, or ultra checks. Other CLI versions, including an unrecognized version, are not blocked by this rule. The switch does not grant delegation permission beyond the task card.

Any valid weekly quota reading at or above the limit, either before a launch or after a Codex job finishes, stops further Codex launches for the rest of that batch. A later weekly reset does not clear the stop. Running workers finish naturally; OpenCode scheduling continues.

Ctrl-C and SIGTERM both stop new launches, wait for running workers and supervisors, write all item records and the summary, then return `130`. Invalid queue JSON returns `2`; file-access, configuration, and ledger runtime failures continue to return `1`.

Normal dispatch atomically writes `运行时/派活/<batch-id>.批次.json`. Its initial fields are `批次号` (batch ID), `开始时间` (UTC ISO 8601 start time), `总件数` (total items), `并行数` (parallelism), `额度上限` (quota limit), and `任务` (an array of objects with `任务` and `轮`, the task and round IDs). On completion it atomically adds `结束时间` (end time) and `结束状态`: `完成` (scheduler finished), `被中断` (interrupted), or `校验失败` (validation failed). A finished batch may contain failed or quota-skipped items; consult the existing JSONL records. This status does not mean acceptance.

Example initial batch metadata:

```json
{
  "批次号": "20261101-120000-0123456789ab",
  "开始时间": "2026-11-01T20:00:00+00:00",
  "总件数": 2,
  "并行数": 3,
  "额度上限": 95,
  "任务": [
    {"任务": "CL-20261101-01", "轮": "r1"},
    {"任务": "CL-20261101-02", "轮": "r1"}
  ]
}
```

A rejected normal invocation writes metadata only, with no item journal or worker launch. Unparseable queues have zero total items and an empty task list; invalid identifier types in parseable lists become `null`. Dry runs write no batch metadata or runtime directories. Missing end fields mean there is no recorded completion; they do not prove a process is still running, since forced termination or storage failure can leave the record unfinished. The existing `<batch-id>.jsonl` line format and semantics are unchanged.

### Reproduce delivery tests

```sh
aw repro <task> <round> --framework 输入
aw repro <task> <round> --framework 交付
aw repro <task> <round> --framework /path/to/development-tree
aw repro <task> <round> --framework 交付 --no-sandbox
```

`--framework` (`--框架`) defaults to `输入`, selecting the round's original `输入/框架`. `交付` selects its delivered `交付/框架`. Any other value is a directory path, absolute or relative to the caller's working directory. The command copies the round's `输入/` and `交付/`; an explicitly selected path is also copied into `指定框架/` in the temporary directory. Tests run from the copies. Copies omit `.git` and `__pycache__`; other symlinks and special files remain rejected. A missing selected tree fails without falling back to the original tree.

`FRCLYO_REPRO_ROOT` and all compatibility variables (`REVIEW_SOURCE`, `REVIEW_REPO`, `AW_REVIEW_REPO`, `REVIEW_FRAMEWORK`, `FRCLYO_REVIEW_ROOT`, `REPRO_FRAMEWORK`) point to the selected temporary framework copy. Tests must load from this variable; scripts that hardcode the original input path need adaptation. Output identifies the source tree. Delivery and explicit-path runs use the expectation “all tests should no longer reproduce”; exit 1 is labeled “仍复现” (still reproduced). An explicit directory path is treated as a fixed-version check even if it names the original input tree.

By default, macOS `sandbox-exec` denies networking and permits writes only to the temporary copy and `/dev/null`. Only an explicit `--no-sandbox` (`--不套沙箱`) runs Python directly, after printing “测试代码将直接在本机运行” (test code will run directly on this machine). This mode has no network or filesystem sandbox imposed by this command; review the test code before using it. There is no automatic fallback when sandboxing fails. Copying, the clean child environment, the 300-second total timeout including the compatibility retry, interruption and descendant cleanup, and independent logs remain in place. TMPDIR must still be outside HOME and the original round.

Test exit 1 means reproduced; exit 0 means not reproduced; other exits mean a test error. Ordinary exception text, `FAIL`, and `ERROR` do not override exit status. The exception is a nonzero exit accompanied by a line ending in the `sandbox_apply: Operation not permitted` diagnostic, optionally prefixed with `sandbox-exec:`. The diagnostic may start the line or follow a colon, including the `AssertionError: … :` wrapper used by unittest for captured stderr. This is classified as “环境受限：沙箱不能嵌套” (environment restriction: nested sandboxing is unavailable), with a suggestion to explicitly rerun using `--no-sandbox` / `--不套沙箱`. It does not trigger the missing-module retry. The same text on exit 0 preserves success. Timeouts, launch failures, and unconfirmed process cleanup retain precedence as executor errors. Restricted, skipped, or unprepared tests do not prove a fix.

`--test` (`--测试`) selects an exact test filename, optionally omitting `.py`. A missing `frclyo` module still triggers one compatibility retry from the copied `交付/`, within the same 300-second budget. Logs live under `$FRCLYO_HOME/运行时/复现/<task>-<round>/run-<unique-id>/`: `results.json` remains an array of test results; `run.json` records the source, temporary framework path, sandbox mode, and expectation. Complete output is retained in `<test filename>.attempt-N.log`; sandboxed runs additionally retain `sandbox.sb`.

The command returns 0 for completed collection, which may include reproduced or still-reproduced tests. It returns 1 for preparation errors, test errors, or environment restrictions, and 2 for invalid usage. Verify that every individual result is “没复现” (not reproduced) when checking a fix.

Reproduction scripts must catch preparation, import and permission failures and return a code other than 0 or 1, such as 2. The default unittest exit code 1 covers both failures and errors and cannot distinguish them. Verdicts follow the test contract and do not independently prove the test correct. `--test` does not expand wildcards.

Logs use an `.attempt-N.log` suffix. Names up to 240 UTF-8 bytes retain the format `<test filename>.attempt-N.log`. Longer names use a prefix truncated at a valid UTF-8 boundary, a hyphen, the full hexadecimal SHA-256 digest of the original test filename, and the attempt suffix; the complete log filename remains within 240 bytes. Every attempt retains its own log. The `test` field in `results.json` preserves the original filename, and `attempts[].log` records the actual log filename.

Timeout, interruption, and normal completion reuse the execution supervisor's descendant snapshots and repeated cleanup, including tracked descendants in separate sessions. Unconfirmed cleanup produces an error diagnostic. The existing snapshot limitation still applies: a descendant created and reparented between samples can be missed. This mechanism is not a kernel-level process container.

See the [task-card template](docs/协议/模板/任务卡.md#复现测试产物与自检).

### Merge worker deliveries

```sh
aw merge <task> <round> --to /path/to/development-tree --dry-run
aw merge <task> <round> --to /path/to/development-tree
```

`aw merge` (Chinese alias:`aw 合并`) merges one worker's complete delivery into a development tree. The default base is `$FRCLYO_HOME/素材/<task>/框架/`; the worker tree is `$FRCLYO_HOME/交付/<task>/<round>/交付/框架/`. Preview with `--dry-run`. Permission handling is detailed below. Changes made on both sides use `git merge-file`. Conflicting files remain untouched, while other changes may still be applied. Changed Python and Shell files are syntax-checked; a failure rolls back the entire batch, including deletions.

Repeat `--union <relative-file>` to explicitly keep both sides of textual conflicts in `.py`/`.sh` files or extensionless scripts with a recognized Python, bash, or sh shebang (including `bin/aw`), such as independently added test methods. Extensionless detection accepts a direct interpreter path or `/usr/bin/env` (including `-S`) for `python`, `python3`, `python3.N`, `bash`, and `sh`; Shell checks still use `bash -n` without executing the shebang interpreter. Worker deletions are listed but retained unless `--allow-delete` is given; development-side edits still cause conflicts. Repeat `--only 'bin/tests/*'` to warn about changes outside the stated scope. These patterns warn rather than filter. Exit status 1 can mean that nonconflicting files were applied: always read the report. Serialize merges to the same destination. Symlinks, special files, and automatic file/directory conversions are not supported.

Permission notices compare the delivery against the actual input snapshot for that round at `$FRCLYO_HOME/交付/<task>/<round>/输入/框架/`. Files that were received and returned with mode 0600 do not produce per-file permission notices merely because the development tree or material baseline has executable bits. Only a difference from the same file in the received snapshot is reported. The summary reads `工人改了 N 个文件的权限` (“The worker changed permissions on N files”), including zero. Files with both content and permission changes are counted. Existing development files retain their current permissions; new files retain delivery permissions and are not counted as chmod changes without an input counterpart. `--base` selects the content merge ancestor only. If the round's input snapshot is missing, merging stops before writing and requests that snapshot; development or material permissions are not used as a substitute.

Repeating `--union` for the same task, round, and delivery retains a conflict block that already contains the complete, contiguous worker block instead of appending it again. An unchanged merge result is not rewritten, preserving bytes, permissions, and modification time. Before writing, the candidate result is checked for stability under another merge. Unstable cases, including some adjacent deletions, are reported as conflicts and retain the development file for review. Matching is local to each conflict; it does not deduplicate lines across the file or remove intentional repetitions inside a block. Union merging still requires tests to check behavior.

The bash/Python wrapper in `bin/aw` is recognized by its opening and closing boundaries, without requiring an exact `exec python3` command line. Launcher flags such as `-B` remain compatible with separate Python-file and Shell-prefix syntax checks. These checks do not execute delivery code.

## Versioned releases and rollback

The stable entry point at `$FRCLYO_HOME/框架` points to a read-only snapshot under `框架-版本/<full-commit-id>`. Workers resolve their physical version when they start, so publishing a new release does not change the scripts or modules used by running workers.

For the initial migration, pause dispatch and wait for all workers to finish. Run `框架-开发/bin/aw release --migrate` using a checkout that includes this feature. The original stable repository is retained as `框架.迁移前`. Use `框架-开发/bin/aw release --undo-migration` to restore it, again with dispatch paused. Use the development entry point to manage snapshots whose older `aw` does not include these commands.

```sh
aw release              # Clean development HEAD, already pushed to its local file remote
aw release --paid       # Run the candidate's aw verify --paid
aw rollback             # Return to the version active before the last switch
aw rollback <full-commit-id>
aw versions             # Current/previous versions and worker counts
aw clean                # Preview eligible runtime directories and old versions
```

Publishing validates a candidate snapshot before atomically replacing the stable symlink. Failed verification leaves the current version unchanged. Publishing, rollback, migration and migration reversal restart the board and append change-log entries.

Versions are never removed automatically. `aw clean --yes` still requires typing the complete data-root path. Only registered versions with startup locking, no active references, and neither current nor previous status are eligible. Unavailable process information and older snapshots without startup locking cause retention. Do not edit sealed snapshots.

### Recovery and version status

A release verifies its candidate before switching the stable entry. Failures before switching preserve the original entry. If restoring the pre-migration directory fails during undo, the previous version link is restored. If the current snapshot directory is missing, run `框架-开发/bin/aw rollback <full-old-commit>` from the development entry; the rollback target must be complete, registered and accepted.

After switching, failures in state persistence, the completion journal or board restart return a nonzero exit code and identify both the completed switch and the unfinished step. The accepted new entry and `pending` record remain. Fix the cause and run `aw release` again to finish the pending work, without verifying the development tree again or losing the previous-version reference. Migration and undo can also resume using their original commands. An interruption before switching restores the old entry or clears the unfinished switch record before retrying the operation.

If snapshot registration fails after the directory was renamed, retry the same commit. It is exported and verified again; registration is repaired only if the existing readonly snapshot matches the fresh candidate. Different content or writable snapshots are preserved and reported. Abandoned `.candidate-*` directories are removed the next time the release management lock is acquired, for example by `aw release`. Old-version deletion persists its intent before removing files. After an interrupted deletion or completion journal failure, `aw clean` offers to continue that deletion using its existing full-root confirmation. Current, previous, active and uncertain versions remain protected.

`aw versions` adds publication time and an explicit incomplete/not-rollbackable reason to the existing current/previous/old labels and worker counts. Missing, changed, writable, unregistered, unaccepted and partially deleted snapshots cannot be rollback targets. A damaged-version marker alone does not change the list command's exit status; unavailable process scans or unreadable usage records still return nonzero and disable cleanup. Missing timestamps and unverifiable worker liveness are shown as unknown.

Recovery uses at-least-once completion: if an action succeeds but the process dies before its progress flag is persisted, recovery may repeat the completion journal entry or board restart. It preserves the accepted version and stable entry.

An explicit `aw rollback [full-commit]` takes priority over unfinished release completion; a failed pending release finalization must not block the requested rollback.

Version timestamps and worker-registration fields are documented in [runtime records](docs/运行目录与边界.md#版本元数据与工人使用记录).

## Uninstall

```sh
./uninstall.sh --dry-run
./uninstall.sh
# Or: aw uninstall
```

The journal at `$FRCLYO_HOME/配置/安装记录.jsonl` drives removal: first confirm that tool-registered launchd services have stopped, then undo other installation entries (links, the Claude skill and tool-added Keychain entries) in reverse journal order. Retain dependent files if service shutdown cannot be confirmed. Modified installation files are retained. Without `--delete-data`, default uninstall and `--yes` retain configuration, the journal and working data; `--yes` confirms removal of installation entries, including tool-registered Keychain entries. After exact normalized full-path confirmation, `--delete-data` first removes eligible external per-round temporary directories indexed by this data root (Codex `.frclyo-work` and OpenCode fallback directories for non-ASCII roots), then recursively removes the data root. If external cleanup cannot safely complete, the root and index are retained. Back up both scopes, including the framework if it is inside the data root. `--yes` cannot supply this confirmation. Wrong paths, EOF, `--no-input`, dry runs, missing journals, failed removals, unsafe roots and symlink paths retain data. External legacy configuration and credential files are not deleted. See the Chinese README and [host isolation notes](docs/隔离验收.md) for the new layout and verification boundary.

The root cannot be a git repository or be located in Documents, Desktop or iCloud. The installer only creates missing `配置/`, `框架/`, `框架-开发/`, `任务卡/`, `交付/`, `素材/`, `项目/`, `运行时/opencode/`, `运行时/控制/opencode/`, `台账/备份/`, `台账/归档/` and necessary parent directories. It does not clone versions or generate coordinator entry files or the owner’s README.

## Security defaults

Both workers are denied access to the data-root ledger, configuration, other rounds and host control directories. OpenCode additionally restricts user-data content outside the root, while allowing metadata and necessary exact paths. Codex retains general reads outside the root because its permission profile cannot grant metadata-only access; sensitive paths remain denied. Delivery writes stay within the current round, with private runtime/temp paths and `/dev/null` as operational exceptions.

- **Worker networking:** Codex shell commands have networking disabled by default; web search is explicitly enabled per task. OpenCode's macOS sandbox blocks direct network access.
- **Reverse proxy:** OpenCode connects by HTTP only to its round’s loopback port. The host proxy adds Bearer authentication to a fixed provider HTTPS route, strips client authentication, rejects CONNECT and does not follow cross-host redirects. Direct outbound traffic, DNS and other loopback ports remain blocked.
- **Credentials:** The host reads the OpenCode provider key and passes it through an anonymous pipe to the proxy. Worker processes have no real provider key; templates contain a placeholder. Routes support `deepseek`, `moonshotai-cn`, `xiaomi`, `zai-coding-plan` and `zhipuai`. Configure existing Keychain entries with `keychain_entries`; `aw key list` shows sources without passwords. Codex uses its host CLI login. User Codex/Claude configuration is not modified.
- **Zero telemetry:** The framework does not upload tasks, usage records, performance data or statistics. Model requests and board quota/balance queries contact the respective services; third-party CLI behavior remains subject to its own settings and policies.
- **Reversibility:** Machine changes require confirmation and are recorded. Worker rounds are isolated. The board is read-only and binds only to `127.0.0.1`. Hourly audits check accepted tasks against the round-selection and receipt-exception rules above.

Receipt hashes cover the result report only; rerun acceptance checks to assess artifacts. The hourly audit is an evidence check, not a machine security audit. OpenCode model catalogs come from local cache; the launcher does not download them.

## Round storage and limits

- Deliveries: `$FRCLYO_HOME/交付/<task>/<round>/`.
- OpenCode private state: `运行时/opencode/rounds/<task>/<round>/`; its template and model snapshot are read-only.
- Host capture and receipts: `运行时/控制/<worker>/<task>/<round>/`, inaccessible to workers.
- ASCII temp paths: OpenCode uses `$FRCLYO_HOME/tmp/…`, with an ASCII fallback for non-ASCII roots. Codex uses host `.frclyo-work/…` outside the data root, with a fallback when needed. Codex may read other rounds’ external temp files, but cannot write them.

Round state, logs and temp files are retained by default. After confirming the process group is reaped and artifacts and evidence are retained, run `aw clean [--days N]` to preview private directories with completed cards and trustworthy exit evidence; `--yes` still requires the full data-root path. The delivery directory, ledger, and nodes that fail safety conditions are not deleted by this command. See [exact paths and retention guidance](docs/运行目录与边界.md).

Inputs default to 1 GiB and 10,000 file/directory entries (`WORKER_INPUT_MAX_BYTES`, `WORKER_INPUT_MAX_FILES`). Internal links and special files are rejected; preparation failures produce a `准备失败` receipt without starting the worker. Results default to 16 MiB (`WORKER_RESULT_MAX_BYTES`); worker stdout/stderr together are capped at 64 MiB. Public logs are published only after the round ends and its process group is confirmed reaped. Enable OpenCode debug logging with `OPENCODE_WORKER_DEBUG=1`.

Follow [worker rules](工人守则.md), section 3 and the final tool conventions: Codex git/build work uses a subdirectory of the supplied `$TMPDIR`, then copies artifacts back. Swift conventions and OpenCode `mktemp -p "$TMPDIR"` are defined there.

## Board projects

Unregistered rounds show “running” while active, then gray “test” (`TEST-` prefix) or “unregistered”, with failure suffixes; their results do not enter the action queue. Multiple active projects are grouped with stable colors and their responsible conversation. Optional `$FRCLYO_HOME/配置/项目表.json` maps projects to conversations:

```json
{"项目": {"Example": {"负责对话": "Framework maintenance"}}}
```

Missing or malformed maps are treated as empty. The board suggests the target conversation; it does not send messages.

### Versions and dispatch

The **Versions and dispatch** panel covers all projects. Versions appear on the left and the five most recently updated batches on the right; narrow screens stack the cards. Expanded batch and version sections remain open after refreshes.

Batches with `运行时/派活/<batch-id>.批次.json` show the queue total, start/end times and recorded end status. Completed, failed and skipped counts come from per-item JSONL results; quota-skipped and interrupted items count as skipped. Before any end field is recorded, **Running** means queue entries without a result, including entries not yet launched. It does not establish that a process is alive. Any recorded end field makes this count zero. A finished batch means scheduling has ended, not that every item succeeded or passed acceptance. Interrupted and validation-failed statuses remain explicit, and items without results are marked accordingly. Metadata-only batches are included even before a JSONL journal exists.

Legacy batches without metadata retain an unknown total. Their running count is only a lower bound based on associated running rounds; no matches means unknown. Malformed metadata, incomplete results and read limits produce explicit uncertainty rather than an unjustified exact running count. Token totals include only known usage.

The stable entry determines the current version. Publication times use only `published_at`; missing values display **Unknown (legacy record)**, with no inference from modification times or `prepared_at`. The bounded list shows up to ten versions and can include unregistered or incomplete versions discovered by the read-only API. Integrity, rollback eligibility, deletion status and reasons come from `version_rows(root)`.

**Workers still using older versions** includes only registrations returned by `worker_usage(root)` whose `active` value is exactly `True` and whose full version differs from the current version. Rows identify worker, task, round, PID and version. `False` entries are omitted; `None` entries appear separately as **Unconfirmed**. An unknown current entry also prevents classifying active registrations as old. A usage-read error or timeout makes both worker lists unknown, not empty or zero. Legacy launchers may not create registrations, so an empty list does not prove that an old version is unused. Lease files alone do not prove active use.

Page data refreshes every three seconds. The two release APIs run asynchronously with independent thirty-second caches, including failures, and a three-second timeout per read. Initial reads and expired-cache refreshes display **Reading, unknown**. Errors and timeouts affect only the corresponding information and do not block other dashboard data. Usage may lag actual process state by about thirty seconds. The panel offers no release, rollback or cleanup actions.

## Validation

```sh
python3 tests/run_all.py
```

This runs all required offline suites in `bin/tests`, `board/test_board.py`, and `tests`. Missing tests, failures and skips fail the command. Tests use temporary homes, fake Keychain commands and fake models, with no API charges. CI invokes the same entry point on macOS with Python 3.9 and 3.13.

Run host checks separately and record each exit code. See the [host command matrix](tests/host/README.md) and [isolation criteria](docs/隔离验收.md). TCP, credentials, runtime-round and sandbox-doctor checks use no real models; some start temporary local services. `network_check.sh` is offline by default. `isolation_check.sh --offline-only` intentionally exits 1 because two real recitations remain unverified.

Full `isolation_check.sh`, `codex_tmp_smoke.py` and `network_check.sh --live` use real models and consume API or subscription quota. OpenCode credentials come from host Keychain, not an environment API key. Prepare the CLI, cache and credentials required by each script; offline results do not establish host isolation or real-model compatibility.

### Interrupted validation

When `aw verify` (`tests/host/verify_all.py`) or `python3 tests/run_all.py` receives SIGINT / Ctrl-C or SIGTERM, it stops scheduling checks and cleans up the process groups and sampled descendants it started, including descendants in separate sessions. Completed results are retained, and the interrupted check and summary are marked `被中断` (interrupted). Exit codes are 130 for SIGINT and 143 for SIGTERM. Further signals during cleanup do not replace the first signal's exit code.

Host verification keeps its existing evidence location (`--output`, or `运行时/验收/` under the data root). The offline runner creates and prints a `.offline-evidence-*` directory under the system temporary directory. It retains per-suite `output.log` and `result.json`, plus `summary.json` and `summary.txt`, independently of the disposable test HOME. Host verification collects its logs and JSON summaries through the existing evidence protocol.

Cleanup reuses the PID and start-time snapshots in `bin/frclyo/execution.py` and requires permission to query system processes. Unavailable snapshots or unconfirmed cleanup are reported as failures. The existing sampling limitation remains: a process born and detached between samples may be missed; this is not a kernel process container. SIGKILL cannot be handled. Run `python3 -B tests/test_b131_interruption.py` on a host with process-query permissions to validate descendant cleanup.

## How it compares

Our choice is to retain task execution, contracts, and independent acceptance checks while keeping the board focused on read-only status. In the reviewed snapshots, Vibe Kanban emphasizes workspaces, diff review, and PR workflows. codex-orchestrator emphasizes ongoing Codex sessions, follow-up instructions, and terminal control. Those are useful capabilities; our priority is dispatch across models with explicit acceptance criteria. Plug-and-play integration is not implemented. [Scope and sources](docs/案例/工具对比.md)

The comparison is a static snapshot review, not a performance ranking. Network isolation and host verification scripts are included; host validation is still required before release. Receipt hashes cover only the result report, not every artifact. [Design and limitations](docs/思路.md)

## Documentation

- [Design decisions and lessons learned](docs/思路.md)
- [Segmentation case: two workers, one contract, 252 bigrams](docs/案例/2026-10-13-切词实战.md)
- [Tool comparison](docs/案例/工具对比.md)
- [Task classification](docs/协议/任务分类标准.md), [decomposition and integration protocol](docs/协议/任务拆分与集成规范.md), and [task template](docs/协议/模板/任务卡.md)

These documents are in Chinese. Protocols preserve a sanitized historical version: old skill names, routing choices, and networking prohibitions describe earlier rules or requirements, not implemented features of this release. Historical skills, runtime records, and corpora are not included. This README defines the current scope and development status.

## License

**Source available. Free for permitted personal and noncommercial use. Contact the author for commercial licensing.** See [PolyForm Noncommercial License 1.0.0](LICENSE) and [NOTICE](NOTICE). This is a source-available project; the license is not OSI-approved, so we do not describe the project as open source.

Personal use must still meet the license's purpose conditions, including the absence of an anticipated commercial application. The full license governs the permissions. See [COMMERCIAL.md](COMMERCIAL.md) to request commercial authorization.
