# 宿主验收入口

以下命令从仓库根目录在正常 macOS 宿主会话中**逐条单独运行并记录退出码**，不是让受限工人嵌套运行。缺工具、启动失败或沙箱拒绝都不能记作通过。不要串成一条后只看最后的退出码。下面是预期结果，不是本次已运行记录；以实际脚本输出的项数为准。

## 不产生真实模型费用

| 单独运行的命令 | 用途与依赖 | 预期 |
| --- | --- | --- |
| `python3 -B tests/run_all.py` | 三组必过离线测试：`bin/tests`、`board`、`tests`；假 HOME、假钥匙串、假 CLI；不包含本目录独立宿主入口 | 各组 `OK`，无零测试或跳过，退出 0 |
| `python3 -B -m unittest discover -s tests/host -p test_isolation_check.py -v` | 探针判定、配置与诊断逻辑回归；不证明内核隔离 | `OK`，退出 0 |
| `python3 -B tests/host/test_proxy_tcp.py` | 真实本地 TCP 代理生命周期、CONNECT 拒绝、停止监听和父进程退出；假 key，无真实 API | `OK`，退出 0 |
| `python3 -B tests/host/runtime_rounds.py` | 真实 `sandbox-exec`、替身 OpenCode，验证两轮状态/模板/快照/控制目录的读写边界 | 两轮均成功、`PASS`，退出 0，打印证据目录 |
| `python3 -B tests/host/proxy_credentials.py` | 生产入口、真实沙箱与代理、替身 OpenCode/假 key；检查环境、模板、ps/sysctl、端口限制、日志及进程清理 | 每项 `PASS`，退出 0，打印证据目录 |
| `bash tests/host/network_check.sh` | 顺序包装 TCP 与凭据探针；默认离线，无真实模型 | 前一套 `OK`，后一套逐项 `PASS`，退出 0 |
| `bash tests/host/isolation_check.sh --offline-only` | 两种工人的确定性内核读写探针；需 Codex CLI 和 `sandbox-exec`，不需真实登录/key | 内核项全为 true；两项真实复述为 false（未验证），**整体退出 1** |
| `python3 -B tests/host/sandbox_doctor.py` | 真实 OpenCode + 本地假上游的生产入口中英文路径对照，再跑两种工人的工具与隔离检查；需 Codex/OpenCode、`sandbox-exec` 及相关工具 | `PASS`、退出 0；查看逐项日志和路径对照，不能只看旧故障是否复现 |

离线指不调用真实模型，不等于完全不启动进程或监听端口。TCP、凭据和生产入口诊断会启动测试子进程或临时本地服务，由脚本结束；没有模型费用，也不应在不允许这些动作的工人环境运行。`network_check.sh` 包含前面两个入口，已分项跑过时无需为了覆盖率重复运行包装器。

诊断可另选以下命令，每条仍单独运行：

```sh
python3 -B tests/host/sandbox_doctor.py --opencode-only
python3 -B tests/host/sandbox_doctor.py --kernel-only
python3 -B tests/host/sandbox_doctor.py --opencode-only --opencode-matrix
```

第一条只做生产入口中英文路径对照，预期 `FIXED_PAIR_OK`、退出 0；第二条只做两种内核工具/隔离检查，预期 `PASS`、退出 0；第三条追加逐项诊断实验，固定 working-directory 组的中英文两组成功才退出 0，不要求历史 baseline 或每个实验都成功。可加 `--output /一个尚不存在的证据目录`；包含 OpenCode 对照时要求全 ASCII 物理路径。诊断修改只在合成夹具副本内，不是生产权限开关，不能把实验性放开直接用于真实数据。

工具项涵盖 bash/python3/git（含合成仓库中的 init/add/commit/status/diff，不 push）、设备访问、heredoc、`mktemp -p "$TMPDIR"`、sort、Python/Node 临时文件和 Swift；未安装 Node 时该可选项报告未运行。日志区分启动器退出码、子命令退出码和实际 errno；有警告的通过也会保留 stderr。查看 `summary.json`、逐项 `NN-kernel.log` 和 `opencode-matrix.json/.md`。中英文对照是否复现同一 `Unexpected` 另行记录，未复现不能凭对照成功推断旧根因。

## 会产生模型费用或消耗订阅额度

这些脚本不提供交互式费用确认；宿主应先取得运行授权和预算。预先准备已安装 CLI、本机模型缓存与凭据，脚本不下载模型缓存。

```sh
bash tests/host/isolation_check.sh
```

各启动一次真实 Codex 和 OpenCode/DeepSeek，仅复述注入守则第一条，另跑确定性内核探针。需要宿主 Codex 登录（可沿用 `CODEX_HOME`）和 DeepSeek 钥匙串条目；脚本复制生产配置中的固定路由、模型快照来源及 `keychain_entries.deepseek` 的 service/account，不复制 `key_env_file`。不需要也不继承环境 API key。可加 `--models-source /path/to/models.json --codex-model gpt-6-astra --codex-effort high`。预期全部 true、退出 0；复述不证明命令工具可用。

```sh
python3 -B tests/host/codex_tmp_smoke.py --model gpt-6-astra --effort high
```

一次真实 Codex 调用；需宿主已有登录、候选模型缓存和 Swift 工具链。检查 TMPDIR/TMP/TEMP、临时读写、git/bash、getcwd、Swift 编译运行及拷回本轮。默认 `--swift-cwd tmp`，预期 `PASS`、退出 0，`tmp-smoke.json` 全部 true。`--swift-cwd run` 或 `root` 仅用于诊断旧行为，不是失败后的自动重试。

```sh
python3 -B tests/host/proxy_credentials.py --live --models /path/to/models.json --opencode /path/to/opencode
```

先跑离线凭据检查，再做真实 OpenCode/DeepSeek 短卡。此脚本创建自己的配置，使用宿主钥匙串默认 `frclyo-deepseek` / `frclyo`，不继承生产 `keychain_entries` 或环境 key；`--models` 必填，`--opencode` 可省略并从 PATH 定位。预期逐项 `PASS`、退出 0。

```sh
bash tests/host/network_check.sh --live --models /path/to/models.json --opencode /path/to/opencode
```

先跑 TCP，再运行上面的 `proxy_credentials.py --live`；费用、凭据与预期相同。与直接 live 命令是替代入口，不必重复付费。

## 辅助文件与证据判定

`isolation_check.sh` 是 `isolation_check.py` 的包装器，参数原样转交。`isolation_probe.py` 由它在沙箱内运行，不是独立验收命令。`offline_opencode.py` 提供零账号假上游与诊断夹具；`production_opencode.py` 由 `sandbox_doctor.py` 导入/启动，为临时副本提供假凭据和有界假上游，不应作为独立生产服务启动。`test_isolation_check.py`、`test_proxy_tcp.py` 分别对应上表两套 unittest。

禁读、禁写项只有 EACCES/EPERM 才通过；ENOENT、成功访问或无输出不能冒充拒绝。Codex 的方案 A 可读对照按“读取成功”判定，不能拿 OpenCode 根外禁读标准判断 Codex。无完整探针 JSON 时，状态为“探针未运行”或“探针未完成”，内核项为 `null`，单列未判定。真实复述独立检查正文、执行退出码、回执和守则哈希。完整检查只有所有项为 true 才退出 0。

Codex 内核启动器使用 `codex sandbox`（不是旧文档的 `sandbox macos`）、同源 `codex_permission()`、`-P frclyo_worker` 和 managed config；内核探针使用假 CODEX_HOME，真实复述使用宿主登录。OpenCode 使用同源 `seatbelt()`。配置格式或 CLI 不兼容时记录失败，不切换宽松权限。

脚本会打印证据目录，保留 `summary.json`、日志、规则、回执及合成哨兵；部分脚本另打印系统临时哨兵目录。按实际路径核对和归档；轮次目录默认保留，确认进程组已回收、产物和证据已留存后，可运行 `aw clean [--days N]` 预览已收卡且有可信退出证据的私有目录；`--yes` 仍需输入数据根完整路径。交付目录、账本和不满足安全条件的节点不会由该命令删除。临时 TCP 单测使用的 TemporaryDirectory 会自行回收，不代表其他证据目录也已清理。受限会话出现 `sandbox_apply: Operation not permitted` 时停止该动作，交宿主处理，不能认作框架内核项已通过或已失败。

## 一屏验收入口

在框架根目录运行 `python3 -B tests/host/verify_all.py`，或 `./bin/aw 验收` / `./bin/aw verify`。
默认运行八项免费检查，不调用真实模型。每项独立进程组，默认超时 600 秒，`--timeout 900` 可调整。
默认证据目录为 `$FRCLYO_HOME/运行时/验收/<UTC时间戳>/`；数据根使用框架 `config.root_dir()`，未设置时为 `~/AgentWork`。
`--output /path/to/new-evidence` 仍可指定尚不存在的目录，不覆盖已有目录。`.gitignore` 忽略遗留的 `verify-*`。
每项尝试保存 output.log 与 result.json；正常进入归档流程时另保存 sources.json 和日志/汇总副本。超时、启动异常或中断可能缺少归档，须依 result.json 追查。若要求失败也完整留证，则将归档移入受控 finally 并保留归档错误。全局保存 summary.json、summary.txt；不复制配置、凭据或模型缓存。
失败返回 1；免费项目全过返回 0，未请求的付费卡标为跳过。离线隔离按退出 1、两项复述 false、其余项 true 且两探针退出 0 判定。

`./bin/aw 验收 --paid` 在免费检查后，分别调用一次 OpenCode（deepseek/deepseek-flash，nothink）和 Codex（gpt-6-astra，low），会产生 API 费用或消耗订阅额度。
正式卡在 `tests/host/冒烟卡/`；通过生产 worker 脚本启动，任务号 `TEST-<UTC时间戳>-<工人>`，轮次 r1，不登记账本。
需要本机 CLI、Codex 登录、DeepSeek 钥匙串配置、本机 OpenCode 模型缓存、git、Python 和 Swift 工具链。
两卡均要求入口退出 0、哨兵恰为 `MODEL_API_OK\n`、宿主回执状态“执行完成”。Codex 另要求 calc.py、test_calc.py、changes.diff、swift-smoke、tools.txt 都是非空普通文件，Swift 文件可执行；tools.txt 的 git_init/git_add/git_status/git_diff/unittest/swift_compile/swift_run 各有唯一 `EXIT <步骤>=0` 行。
判定后（包括失败、超时和可捕获中断）删除本次 model-sentinel.txt，保留回执、工具日志和拷回文件，路径见 result.json。进程被 SIGKILL 或机器断电时无法保证清理，需宿主按记录路径清理。工具日志为工人记录，替身测试与日志核验不等于宿主独立复跑产物。
为保证两张卡各只调用一次，统一入口不额外启动旧的 codex_tmp_smoke.py 或完整复述；它们仍是上述独立验收入口。

替身回归：`python3 -B -m unittest discover -s tests -p test_verify_all.py -v`，也包含于 `python3 -B tests/run_all.py`。不调用真实模型、git 或编译器。

### 验收被中断

`aw 验收`（`tests/host/verify_all.py`）及离线测试入口 `python3 tests/run_all.py` 收到 SIGINT / Ctrl-C 或 SIGTERM 后停止启动后续检查，收尾已启动的进程组及已采样的后代进程，包括另开会话的后代。已有检查结果保留，当前检查及汇总标记“被中断”；SIGINT 返回 130，SIGTERM 返回 143。收尾期间再次收到信号，不改变第一次信号对应的退出码。

验收证据仍写在 `--output` 指定位置或数据根的 `运行时/验收/`。离线测试入口在系统临时目录下新建 `.offline-evidence-*`，打印“证据目录：”；逐组保存 `output.log`、`result.json`，并保存 `summary.json` / `summary.txt`。该证据目录独立于测试使用的临时 HOME，测试结束后保留；宿主验收会按原有证据协议汇集日志与 JSON 汇总。

后代回收复用 `bin/frclyo/execution.py` 的 PID 与启动时间快照，需要允许系统进程查询。快照失败或不能确认全部回收时明确记失败，不报告通过。该机制沿用现有采样边界：两次采样间产生并脱离父系的进程可能漏记，不是内核级进程容器；SIGKILL 无法捕获。需要完整验证时，在具备进程查询权限的宿主运行 `python3 -B tests/test_b131_interruption.py`。

## B21 宿主复核（付费，未由离线测试替代）

在已获运行授权的正常 macOS 宿主会话，保留真实 HOME 和 FRCLYO_HOME，不设置测试覆盖 FRCLYO_CONFIG，运行：

```sh
python3 -B tests/host/isolation_check.py --models-source /本机/OpenCode/models.json --codex-model gpt-6-astra --codex-effort low
```

配置仍由真实 `$FRCLYO_HOME/配置/config.json` 读取 `keychain_entries.deepseek` 的 service/account，随后仅复制路由、条目名称和模型缓存路径到假根。
OpenCode 生产宿主启动环境保留真实 HOME，使 security 能找到登录钥匙串；生产脚本在工人启动前仍设置私有 `RT/home`，确定性探针仍使用假 HOME。验收脚本与探针均不取 key；生产宿主按既有匿名管道向代理交接 key，不放进工人环境、任务、argv 或日志。
此命令调用一次 DeepSeek（预计几分钱，以实际计费为准）和一次 Codex。预期“opencode 真实守则复述”为 true，宿主回执“执行完成”，正文匹配守则第一条且哈希一致；完整检查只有所有内核及复述项都通过才退出 0。缺凭据、缓存或工具须记录失败，不能以本次替身结果代替真实验证。
离线回归：`python3 -B -m unittest discover -s tests/host -p test_isolation_check.py -v`。
