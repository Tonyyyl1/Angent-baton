---
name: relay-desk
description: 多设备接力台账与端到端加密云端异步接力 (multi-device async handoff, snapshot sync & ledger). 用户说「交接」「接手」「接力」「接棒」「待接棒」「配对」「台账」「接力状态」「历史对话」「同步快照」，或要把项目交给另一台电脑、在另一台电脑上接着做、查询跨端历史对话或多机进度时使用。这份台账与对话归档同 Claude Code、Antigravity 共用。
---

# relay-desk 多设备接力（Codex）

## 什么时候用

- 用户要离开当前电脑，把代码与上下文交给另一台电脑继续做（「接力」「交接」「把这个项目接力到某台电脑」）。
- 用户在目标电脑上接收项目继续工作（「接棒」「接手」「待接棒」）。
- 用户首次配置多设备加密云接力（「配对」「云接力状态」）、发布/同步快照缓存，或查询跨设备归档的历史对话（「历史对话」）。
- 日常开工、收工，或工作区发生改动后刷新台账状态（「台账」「接力状态」）。

台账（`ledger.json`）与跨端对话归档由命令行 `{{RELAY}}` 统一维护，Claude Code、Codex、Antigravity 和本机网页工作台共用同一份数据。

命令一律写成 `{{RELAY}} …`，原样保留占位符，不要改写。`<项目>` 省略时自动匹配当前目录所属的已登记项目。

## 多机初始化、云端配对与设备检查

1. 如本机尚未绑定设备别名，先运行 `{{RELAY}} init --device ALIAS`（`ALIAS` 可为 `a`、`b`、`c`、`linux-server` 等合法设备别名）。
2. 运行 `{{RELAY}} device --json` 查看台账里所有已登记设备、本机 `deviceId` 以及各对端配对状态（`paired` / `discovered` / `key_changed`）。
3. 运行 `{{RELAY}} pair --status`（或 `{{RELAY}} pair --with DEVICE --status`）查看加密云接力是否就绪，以及 `PyNaCl` 与 `rclone` 是否可用。
4. 如尚未与目标设备配对（或对端发生密钥变更 `key_changed`）：
   - 运行 `{{RELAY}} cloud remotes` 列出本机 `rclone` 已配置的远端连接名（OneDrive、Google Drive、Dropbox、WebDAV、S3 等；各电脑连接名不必相同，指向同一个云端共享目录即可）。
   - 向用户确认连接名与共享目录后，运行 `{{RELAY}} pair --remote <名称> --root <共享目录>` 发布本机签名公钥。
   - 运行 `{{RELAY}} pair --with DEVICE --status` 取得与目标设备之间的 **24 位共享配对指纹**，请用户在两台设备上核对一致；若涉及同别名替换旧设备，需用户确认后加 `--replace-device`。
   - 核对一致后，运行 `{{RELAY}} pair --with DEVICE --accept --fingerprint FP` 完成公钥固定绑定。

## 快照发布与网络同步（保留本机编辑权）

- **发布快照缓存**：`{{RELAY}} publish <项目>` 将加密快照与完整可见历史对话发布到云端缓存并更新 `have` 清单，**本机继续保留编辑权**，不会交出项目归属。
- **同步网络快照**：`{{RELAY}} sync [项目]`（或 `{{RELAY}} sync --watch`）仅同步签收回执、拉取缓存快照并做 `versionGraph` 分叉检查与转发，**绝不覆盖本地工作区文件**。
  - 退出码：0 成功，3 部分失败（只剩会自动重试的问题），4 需要处理，130 已取消。输出「同步状态：需要处理」时，把每条未解决问题和它后面的「处理：」命令原样告诉用户，不要自行猜测处理办法。
  - 需要逐条读取事件时用 `{{RELAY}} sync --jsonl`（可加 `--watch`）：每个事件一行 JSON，最后一行 `kind` 为 `summary`。`--jsonl` 不能和 `--json` 同用。

## 诊断、换钥与缓存清理

- **只读诊断**：`{{RELAY}} doctor [项目]`（加 `--json` 供解析）检查身份、配对、当前编辑设备、在途交接、恢复事务、最近一次同步结果和缓存状态，每条问题给出原因和一条处理命令；退出码 0 正常、3 有警告、4 需要处理。用户问「接力状态」、接不到交接或同步出错时先运行它，把原因和处理命令原样转述给用户；处理命令本身按下文「命令块怎么处理」的分类执行。设备显示「未知」只表示没有它最近的缓存清单，不要说成离线或缺少快照。
- **缓存统计**：`{{RELAY}} cache status [项目]`，只读，可直接运行。
- **缓存清理**：
  - `{{RELAY}} cache prune [项目]`（默认就是 `--dry-run`）只预览、不删除，可直接运行；把可删除项、保留原因和预计释放空间告诉用户。
  - `{{RELAY}} cache prune [项目] --apply --expect-plan <计划摘要>` 会删除本机缓存，属于**只改本机**：先把预览结果和这条命令给用户看，得到同意后再执行，并始终带上预览给出的 `--expect-plan`。计划变化时它不删除（退出码 3），需要重新预览并再次征得同意。清理从不删除云端对象、防重放记录、恢复事务或凭据。
  - 清理中断后 `doctor` 报 `prune_interrupted`：再次运行 `--apply`（同样先征得用户同意）即可完成。
- **本机换钥**：`{{RELAY}} device --rotate-keys` 属于**只改本机**，先说明影响（所有对端都要重新核对新指纹）并得到用户同意。有在途交接、未上传回执或未完成接收时会被拒绝（退出码 4）并列出处理命令，优先按这些命令处理完再换钥。`--force` 会让在途交接都需要重新签发，同样属于**只改本机**：只有在用户看过受影响的交接列表并明确同意后才可加 `--force`。
- **对端换钥**：`device --json` 或 `pair --status` 显示 `key_changed` 时，请用户在两台设备上核对新指纹，一致后再运行 `{{RELAY}} pair --with DEVICE --accept --fingerprint FP`，绝不跳过核对。确认后 `sync` 会自动为新公钥重新投递缓存快照，原投递和回执保持不变。
- **重新签发在途交接**：`send` 或 `doctor` 报 `handoff_recipient_key_changed`（目标设备换钥）或 `handoff_sender_key_rotated`（本机换钥）时，原交接已无法领取。确认对端新指纹已核对并重新确认后，征得用户同意再运行 `{{RELAY}} send <项目> --to DEVICE --reissue`：同一快照、新交接编号、新密文，原交接包不变，对方只能领取其中一个。普通 `send` 不会自动重新签发；目标设备必须已升级到支持重新签发的版本。

## 离开这台设备（「接力」「交接」）

先用 `{{RELAY}} device --json` 与 `{{RELAY}} pair --status` 判断：若存在已配对设备，**优先使用云端异步接力**；若未配对且不使用云存储，则使用手动清单接力。

### 云端异步接力（推荐，已配对时优先使用）

1. 写或更新项目根目录下的 `HANDOFF.md`（简明总结：做了什么、停在哪、下一步、怎么验证；只写文件，绝不替用户提交或推送到 Git 远程）。
2. 运行 `{{RELAY}} note <项目> --last "最近做了什么" --next "下一步" --verify <none|pass|partial|fail>` 更新台账。
3. （推荐）运行 `{{RELAY}} session add <项目> latest --tool codex` 登记当前线程，让会话完整性检查覆盖当前对话。
4. **明确选择目标设备**：先运行 `{{RELAY}} device --json`。
   - **当已配对设备有 2 台及以上时，必须先向用户询问要发往哪台设备（`DEVICE`），绝不要自行猜测目标设备！**
   - 仅当只有 1 台已配对设备时，才可省略 `--to`。
5. 运行 `{{RELAY}} send <项目> --to DEVICE`：
   - 命令会自动打包 Git 分支与提交、已暂存（staged）改动、未暂存（unstaged）改动、未跟踪文件、摘要以及 Claude Code / Codex / Antigravity 的完整可见历史对话，端到端加密后显式授权给目标 `deviceId` 上传云端。
   - 若报错提示 **多对端歧义（`ambiguous_peer`）**、**对端密钥变更（`key_changed`）**、**未签收交接冲突（`active_handoff_exists`）**、**会话缺口** 或 **机密门禁拦截**（Git 历史/暂存区含私钥或 `.env`），把具体信息展示给用户处理后重试；若上传中断，重试会自动沿用同一交接编号。
   - 若提示 **在途交接因换钥无法领取（`handoff_recipient_key_changed` / `handoff_sender_key_rotated`）**，按上文「重新签发在途交接」处理。
   - **必须等到命令输出「已交棒，可以关机」**（确认加密包与签名清单已完成远端复核），再明确告知用户：**已交棒，可以关机**。

### 手动清单接力（未配对或离线环境）

1. 运行 `{{RELAY}} handoff <项目>`（多台设备时加 `--to DEVICE`）打印离开清单。如果提示项目不在本机，停下来告诉用户。
2. 逐项完成清单：写或更新 `HANDOFF.md`，运行 `{{RELAY}} note <项目> --last … --next … --verify <none|pass|partial|fail>`，运行 `{{RELAY}} session add <项目> latest --tool codex`。
3. 按下节「命令块怎么处理」规则处理命令块，每完成一项运行 `{{RELAY}} check <项目> pre <key>`。
4. 全部完成后运行 `{{RELAY}} depart <项目>`。

## 到达目标设备（「接棒」「接手」）

已配对云接力时，优先走云端异步接棒：

### 云端异步接棒（推荐，已配对时优先使用）

1. 运行 `{{RELAY}} inbox` 查看显式发往本机的待接棒交接包列表与解密后的任务摘要。
2. 运行 `{{RELAY}} receive RELAY_ID` 下载验签、校验版本链（`versionGraph`）并事务化恢复代码与工作区状态：
   - **首次在本机接收该项目**：先向用户确认存放目录，运行 `{{RELAY}} receive RELAY_ID --path <目录>`。
   - **本机原目录有未同步的本地改动、恢复冲突（`recovery_conflict`）或版本分叉（`version_fork`）**：原目录会被完整保留、绝不覆盖；告知用户后，可按用户指定运行 `{{RELAY}} receive RELAY_ID --into <新目录>` 安全接收到新目录。
   - **中断恢复与事务回滚**：若接收曾中断，运行 `{{RELAY}} recover RELAY_ID --status` 查看状态；重跑 `{{RELAY}} receive RELAY_ID` 可继续恢复或补传签收回执；如需撤销中断事务并还原接收前工作区，运行 `{{RELAY}} recover RELAY_ID --rollback`。
   - **原交接已被重新签发的交接取代**（`handoff_superseded` / `superseded_original_claimed`）：对方只能领取其中一个；运行 `{{RELAY}} inbox` 查看当前可领取的交接编号，告诉用户实际情况，不要尝试绕过。
   - **本机状态文件损坏**（`local_state_corrupted`、`delivery_state_corrupted`、`recovery_corrupted`）：命令已停止且文件未被改动。绝不删除或重建这些文件；运行 `{{RELAY}} doctor` 把原因和处理命令（通常是从备份恢复）交给用户，按「只改本机」得到同意后再执行。
3. 阅读项目的 `AGENTS.md`、`HANDOFF.md`，以及台账里的下一步动作。
4. 按需使用 `{{RELAY}} transcripts <项目>` 检索跨设备同步过来的 **Claude Code、Codex、Antigravity 完整历史对话全文**：
   - 列出归档会话：`{{RELAY}} transcripts <项目>`（可加 `--tool <claude|codex|anti>`）
   - 查看指定会话全文：`{{RELAY}} transcripts <项目> --session <id>`
   - 按关键词全文搜索：`{{RELAY}} transcripts <项目> --grep "<关键词>"`
5. 明确告诉用户：原来的本地线程留在原设备；现在是读取摘要开启的新线程（可随时通过 `transcripts` 查阅原设备完整历史对话），不是原本地线程的恢复。

### 手动清单接棒（未配对或离线环境）

1. 运行 `{{RELAY}} pickup <项目>` 打印上下文和到达清单。
2. 按到达清单（`post`）逐项完成，遵守命令块规则；每完成一项运行 `{{RELAY}} check <项目> post <key>`。
3. 读 `AGENTS.md`、`HANDOFF.md` 和台账下一步，最后运行 `{{RELAY}} arrive <项目>`。

## 平时

- 开工：先运行 `{{RELAY}} status`（已配对时也可运行 `{{RELAY}} inbox` 或 `{{RELAY}} sync`；觉得不对劲时运行 `{{RELAY}} doctor`）。
- 阶段快照备份：运行 `{{RELAY}} publish <项目>`（保留本机继续编辑）。
- 收工：运行 `{{RELAY}} note <项目> --last "最近做了什么" --next "下一步"`。
- 工作区有变化（改文件、提交、切分支）：运行 `{{RELAY}} scan <项目>`。

## 手动清单命令块怎么处理

每个手动清单命令块都带有 kind 标记，严格按下面处理：

- **只读**：可以直接执行。
- **只改本机**：先把命令给用户看，得到同意后再执行。项目自己的规则更严时，从严执行。
- **写入远程**：绝不执行，原样交给用户，由用户自己在自己的权限下运行。

relay 自身命令也按同样的分类：`{{RELAY}} doctor`、`cache status`、`cache prune`（预览）属于**只读**；`{{RELAY}} cache prune --apply`、`{{RELAY}} device --rotate-keys`（包括加 `--force`），以及 `doctor` 给出的恢复本机状态文件的命令，属于**只改本机**，必须先给用户看并得到同意。

## 三条硬规则

1. **绝不执行任何写入用户代码 Git 远程的命令**，例如 `git push`。`git fetch` 只读取远程、不改动它，出现在「只改本机」的命令块里时按那一类处理：先给用户看，同意后执行。（`{{RELAY}} send` / `publish` / `sync` / `receive` 只操作加密云接力包，绝不推送用户的代码 Git 远端。）
2. **绝不读取、不复制任何凭据文件**：`auth.json`、`~/.codex/auth.json`、`.env`、`.claude.json`、`.credentials.json`、`oauth_creds.json` 及私钥。只能检查它们是否存在，绝不能打开读取，也不能写进台账或摘要。
3. **绝不把 `HANDOFF.md`、台账记录或归档的历史对话（`transcripts`）说成「恢复了原 Session」**。本地 Session / 线程只能在原设备原样恢复。

## 与 Claude Code、Antigravity 共用台账

- 其他代理登记的 Session、备注以及归档对话，在这里都能看到；这里登记与发出的，它们也能看到。
- 不要删除或改写不是自己登记的 Session 条目。只对自己登记的条目使用 `session add` 和 `session rm`。
- 看台账用 `{{RELAY}} status`；看本机发现的会话列表用 `{{RELAY}} sessions <项目>`；查跨端归档对话全文用 `{{RELAY}} transcripts <项目>`。
