# Changelog

[中文](CHANGELOG.md)

Versions follow `major.minor.patch`. During `0.x`, commands and file formats may still change; incompatibilities are listed under "Upgrade notes".

## v0.3.0 (2026-11-01)

This release turns dispatching into a full pipeline (batch dispatch → reproduce → merge → verify → release / roll back) and adds about 100 reliability fixes.

### New
- **Batch dispatch** `aw dispatch queue.json [--并行 N] [--额度上限 P] [--试跑]` (Chinese alias `aw 派活`): runs a batch, stops refilling at a Codex weekly-quota cap, and serializes jobs per OpenCode provider. Models, effort levels / variants and round names are checked before anything starts; one bad entry stops the whole batch. Progress and usage go to `运行时/派活/<batch>.批次.json`.
- **Reproduce** `aw repro <task> <round> [--framework 输入|交付|path] [--no-sandbox]`: reruns a worker's reproduction tests in a host sandbox. The original tree should reproduce; a fixed tree should not.
- **Merge** `aw merge <task> <round> --到 <dev dir> [--基准 <dir>] [--试跑] [--并集 file]`: three-way merge of a worker's delivery. It keeps file modes, never writes conflicted files, and is idempotent per round.
- **Versioned releases** `aw release [--paid]`, `aw rollback [version]`, `aw versions` (Chinese `aw 发布 [--付费验收] / 回滚 / 版本`): the stable entry is a link to `框架-版本/<commit>`. A release verifies itself first, switches atomically, restarts the board and writes the change log. Running workers keep the version they started with. A failed release can be rerun to finish its pending steps; an explicit rollback takes priority over unfinished steps.
- **Task card templates** `aw card new <template> <task> [--title …] [--round r1]`: bug hunt, fix, change review and experiment.
- **One-command verification** `aw verify` (`aw 验收`): offline checks with a one-screen summary; `--paid` adds real-model smoke cards. Child processes are cleaned up if verification is interrupted.
- **Receipt exceptions**: `aw ledger done --结论 通过 --回执例外 "reason"` when closing a card, or `aw ledger receipt-exception <task> "reason"` for an already closed card. They are stored in `回执例外.jsonl` beside the ledger, so the ledger format is unchanged and older versions can still read it after a rollback. Exceptions waive only receipt / evidence problems, and only for rounds that existed when they were registered.
- The **daily audit** understands named rounds (such as `astra-m`) and checks each round.
- **New providers**: GLM Z.AI Coding Plan (`zai-coding-plan`) and Zhipu AI pay-as-you-go (`zhipuai`); the board shows GLM plan usage.
- `aw key list` shows where each key comes from; keychain entry names are configurable (`keychain_entries`).
- `aw changelog append --change "…" --undo "…"`: locked, auto-numbered change-log entries.
- `aw clean [--days N] [--yes]`: lists private temp dirs of rounds closed more than N days ago; deleting them needs `--yes` and typing the data root path.

### Security and isolation
- API keys no longer enter OpenCode worker processes; a host-side reverse proxy injects authentication.
- OpenCode workers cannot read user data outside the data root; Codex can use git and build tools inside its private temp dir.
- Codex workers get command network access only when a task asks for it.
- Codex subagent limit: with Codex CLI 0.153.x in exec mode only `gpt-5.6-luna` can start subagents. Other models fail, and astra may report false success. Dispatch and the worker script block this combination; `CODEX_WORKER_SUBAGENTS_FORCE=1` skips the check when trying a new CLI version.
- Worker script hardening: descendant process reaping, timeouts, argument checks, private directory permissions.
- The board does not follow symlinks when reading round files, and escapes every value inserted into the page.

### Board
- New "versions" and "dispatch" panels: current version, version list, which old versions are still in use, and per-batch progress and usage.
- Projects are grouped and labeled with their owning conversation; test and unregistered rounds are shown in gray.
- Faster, lighter rendering with many rounds; an unreadable receipt is logged once instead of on every scan.

### Reliability
- About 100 fixes, mainly in: process reaping, truncated proxy responses, JSONL I/O, atomic install files, failed uninstalls, keychain registration order, quota windows, interrupted dispatch, interrupted releases, rollback, and receipt-exception edge cases.
- Tests: guard tests added from mutation testing, new end-to-end scenarios in `tests/e2e`, and no dependence on the host environment.
- Every fix round gets an independent review; this release shipped after the re-review count dropped from 20 to 0.

### Docs
- README, `大脑手册.md` and `docs/运行目录与边界.md` cover the new commands and failure recovery.
- The routing table is updated from two experiments: a same-task comparison of four providers (similar quality; speed and cost differ) and a 480-review effort experiment (astra medium is the review default; no xhigh for sol; terra and luna are not worth it for reviews).
- OpenCode DeepSeek v4-pro now uses its default variant, because local OpenCode no longer offers the `low` variant used in the experiments. Dispatch errors show the default variant as "默认（空串）" instead of a blank.

### Upgrade notes
- **From v0.1**: since v0.2 all data lives in the data root `~/AgentWork`, and uninstalling never deletes data. After `git pull`, rerun `./install.sh` to complete the layout. An in-place upgrade from v0.1 to v0.3 has not been tested on a clean machine, so back up `~/.frclyo` first.
- **Versioned releases** (optional, for people maintaining the framework itself): pause dispatch and wait for running workers to exit, then run `框架-开发/bin/aw 发布 --迁移`. The old repo is kept as `框架.迁移前`; `--撤销迁移` restores it.
- OpenCode entries in a dispatch queue must set `档位` or `变体` explicitly (use `""` for the default variant).

### Known limits
- macOS only.
- Merge and repro expect the project tree in a round directory named `框架`; other projects must use that directory name too.

## v0.2.0 (2026-10-13)
- One data root, `~/AgentWork`, for config, ledger, task cards, deliveries and change logs.
- Uninstall never deletes data; it only removes what the installer registered.
- Worker rules are injected into each task card.
- Worker sandboxes deny reads outside the data root, with probe tests.

## v0.1.0 (2026-10-13)
- First public release: "keep your APIs and agent frameworks ready locally, and pick per task."
- Brains: Claude Code, Codex. Workers: Codex, OpenCode (DeepSeek, Kimi, MiMo).
- Built-in ledger, read-only board, task classification standard, contract-first splitting rules, demo and calibration.
