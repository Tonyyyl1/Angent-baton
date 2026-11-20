---
name: relay-desk
description: 多设备接力台账与端到端加密云端异步接力（支持 Windows、macOS、Linux 任意多台电脑相互迁移与快照同步 / multi-device async handoff, snapshot sync & ledger）。当用户说「接力」「接棒」「待接棒」「交接」「接手」「配对」「台账」「接力状态」「历史对话」「同步快照」（或 handoff / pickup / inbox / publish / sync），或要把项目在多台电脑之间迁移、换电脑接着做、查询跨端历史对话或多机进度时使用。这份台账与 Claude Code、Codex 和网页工作台共用。
---

# relay-desk 多设备接力（Antigravity）

## 什么时候用

- 用户要离开当前电脑，去另一台电脑继续做（「接力」「交接」「把这个项目接力到另一台电脑」）。
- 用户到了目标电脑接手项目（「接棒」「接手」「待接棒」）。
- 用户配置或检查多机加密云接力配对（「配对」「云接力状态」）、发布/同步网络快照，或检索其他电脑带过来的完整历史对话（「历史对话」）。
- 用户查看多设备台账或项目状态（「台账」「接力状态」），以及日常开工、收工或工作区变更后刷新事实。

台账是一份本地 JSON 文件（`ledger.json`），与跨端历史对话归档一起由命令行 `{{RELAY}}` 维护，Claude Code、Codex、Antigravity 和本机网页工作台共用。

文中命令一律写为 `{{RELAY}} …`，安装时已替换成当前机器的调用方式。`<项目>` 省略时取当前工作目录所属的已登记项目。

> **Windows 沙盒终端提示**：在 Windows 的 Antigravity 默认终端沙盒中，若 `py -3` 报错 `No installed Python found!`，直接把命令里的 `py -3` 换成 `python`（即 `python "<relay.py 路径>" …`）即可在沙盒内正常运行，无需申请脱离沙盒。执行时直接调用目标二进制，不要包一层 `$()` 或额外包装器。

## 多机初始化、云端配对与设备检查

1. 若本机尚未绑定设备别名，运行 `{{RELAY}} init --device ALIAS`（`ALIAS` 可为 `a`、`b`、`c`、`linux-server` 等 1~40 位合法别名）。
2. 运行 `{{RELAY}} device --json` 查看所有已登记设备、本机 `deviceId` 及各对端状态（`paired` / `discovered` / `key_changed`）。
3. 运行 `{{RELAY}} pair --status`（或 `{{RELAY}} pair --with DEVICE --status`）检查加密云配对与 `PyNaCl` / `rclone` 就绪状态。
4. 如尚未配对（或某对端提示密钥变更 `key_changed`）：
   - 运行 `{{RELAY}} cloud remotes` 查看本机可用的 `rclone` 云端连接名（OneDrive、Google Drive、Dropbox、WebDAV、S3 等；各台电脑各自的连接名可以不同，只要指向同一个云端共享目录即可）。
   - 向用户确认连接名与共享目录后，运行 `{{RELAY}} pair --remote <名称> --root <共享目录>` 发布本机签名公钥。
   - 运行 `{{RELAY}} pair --with DEVICE --status` 获取与目标设备之间的 **24 位共享配对指纹**，展示给用户在两台设备上核对一致；如需用新设备替换同别名旧设备，须用户确认后加 `--replace-device`。
   - 随后运行 `{{RELAY}} pair --with DEVICE --accept --fingerprint FP` 完成该对端的公钥固定绑定。

## 快照发布与网络同步（保留本机编辑权）

- **发布快照缓存**：`{{RELAY}} publish <项目>` 将加密快照与三端可见历史对话上传至云端缓存并更新 `have` 清单，**本机继续保留编辑权**（不转移 `project.device`）。
- **同步网络快照**：`{{RELAY}} sync [项目]`（或 `{{RELAY}} sync --watch`）仅同步签收回执、拉取缓存快照并维护 `versionGraph` 分叉检测与对端转发，**绝不覆盖本地工作区文件**。
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

先运行 `{{RELAY}} device --json` 与 `{{RELAY}} pair --status` 查看：若存在已配对设备，**优先使用云端异步接力**；若未配对且不使用云端存储，则使用手动清单接力。

### 云端异步接力（推荐，已配对时优先使用）

1. 写或更新项目根目录下的 `HANDOFF.md`，写成简洁摘要：做了什么、停在哪、下一步、怎么验证。若本次会话在 `<appDataDir>/brain/<会话ID>/` 下写过计划或验证产物（Artifacts），把关键结论摘入 `HANDOFF.md`。只写文件，不替用户 `git commit` 或 `git push`。
2. 运行 `{{RELAY}} note <项目> --last "…" --next "…" --verify <none|pass|partial|fail>` 更新台账。
3. （推荐）运行 `{{RELAY}} session add <项目> latest --tool anti` 登记当前会话（若未打开工作区导致未自动匹配到，可直接传入当前 Conversation ID：`{{RELAY}} session add <项目> <conversation-id> --tool anti --title "会话标题"`），确保会话缺口检测覆盖当前对话。
4. **确认目标设备**：先运行 `{{RELAY}} device --json` 查看所有已配对设备。
   - **当已配对活跃对端 >= 2 台时，必须先向用户明确询问本次要发往哪台设备（`DEVICE`），绝不自行猜测目标！**
   - 仅当只有 1 台已配对设备时才可省略 `--to`。
5. 运行 `{{RELAY}} send <项目> --to DEVICE`：
   - 自动检查机密门禁与会话完整性，把 Git 分支提交、已暂存（staged）改动、未暂存（unstaged）改动、未跟踪文件、摘要以及 Claude Code / Codex / Antigravity 的完整可见历史对话端到端加密并显式授权目标 `deviceId` 上传至云端。
   - 若命令提示 **多对端歧义（`ambiguous_peer`）**、**对端密钥变更（`key_changed`）**、**未签收交接冲突（`active_handoff_exists`）**、**会话缺口**（例如已登记会话缺失，或 `transcript.jsonl` 含截断字段且缺少 `transcript_full.jsonl`）或 **机密门禁拦截**（历史提交/暂存区含私钥或 `.env`），把具体列表清晰告知用户确认或处理后重试；若上传中断，重试会自动沿用同一交接编号。
   - 若提示 **在途交接因换钥无法领取（`handoff_recipient_key_changed` / `handoff_sender_key_rotated`）**，按上文「重新签发在途交接」处理。
   - **必须等到输出出现「已交棒，可以关机」**（代表远端加密包与签名就绪标记均已校验通过），才明确告诉用户：**已交棒，可以关机**。

### 手动清单接力（未配对或离线环境）

1. 运行 `{{RELAY}} handoff <项目>`（多台设备时加 `--to DEVICE`），打印离开前清单与命令块。如果提示项目的最新现场不在本机，停下来告知用户。
2. 逐项完成清单：写或更新 `HANDOFF.md`，运行 `{{RELAY}} note <项目> --last "…" --next "…" --verify <none|pass|partial|fail>`，运行 `{{RELAY}} session add <项目> latest --tool anti` 登记当前会话。
3. 按下节规则处理命令块，每完成一项运行 `{{RELAY}} check <项目> pre <key>`。
4. 全部完成后运行 `{{RELAY}} depart <项目>`，将项目标记为在途。

## 到达目标设备（「接棒」「接手」）

已配对云接力时，优先走云端异步接棒：

### 云端异步接棒（推荐，已配对时优先使用）

1. 运行 `{{RELAY}} inbox` 查看显式发往本机的待接棒交接包与解密摘要预览。
2. 运行 `{{RELAY}} receive RELAY_ID` 在临时目录完成验签、版本链（`versionGraph`）防回滚校验后事务化恢复代码与工作区状态（精确还原提交、staged 暂存区、unstaged 工作区与未跟踪文件）：
   - **首次在这台电脑接收该项目**：向用户确认本地存放路径后运行 `{{RELAY}} receive RELAY_ID --path <目录>`。
   - **本机原目录存在未同步的本地改动、恢复冲突（`recovery_conflict`）或版本分叉（`version_fork`）**：系统会自动拒绝覆盖并完整保留原目录；明确告知用户原目录完好无损后，可按用户意愿运行 `{{RELAY}} receive RELAY_ID --into <新目录>` 接收到新目录。
   - **中断恢复与事务回滚**：若接收过程中被中断，可先运行 `{{RELAY}} recover RELAY_ID --status` 查看状态与原因；重跑 `{{RELAY}} receive RELAY_ID` 可继续恢复或补传签收回执；若需放弃中断接收并还原到接收前状态，运行 `{{RELAY}} recover RELAY_ID --rollback`。
   - **原交接已被重新签发的交接取代**（`handoff_superseded` / `superseded_original_claimed`）：对方只能领取其中一个；运行 `{{RELAY}} inbox` 查看当前可领取的交接编号，告诉用户实际情况，不要尝试绕过。
   - **本机状态文件损坏**（`local_state_corrupted`、`delivery_state_corrupted`、`recovery_corrupted`）：命令已停止且文件未被改动。绝不删除或重建这些文件；运行 `{{RELAY}} doctor` 把原因和处理命令（通常是从备份恢复）交给用户，按「只改本机」得到同意后再执行。
3. 阅读项目里的 `GEMINI.md`、`AGENTS.md`、`HANDOFF.md` 以及台账里的下一步动作。
4. 按需通过 `{{RELAY}} transcripts <项目>` 查阅跨设备同步归档的 **Claude Code、Codex、Antigravity 完整历史对话全文**：
   - 列出归档会话：`{{RELAY}} transcripts <项目>`（可加 `--tool <claude|codex|anti>`）
   - 阅读指定会话完整对话：`{{RELAY}} transcripts <项目> --session <id>`
   - 按关键词跨会话检索：`{{RELAY}} transcripts <项目> --grep "<关键词>"`
5. 明确告诉用户：原 Antigravity 本地会话留在原设备，只能在原设备原样恢复；现在是读取摘要（`HANDOFF.md` 与台账）并可按需检索归档历史对话的新会话，不是原本地会话的原生恢复。

### 手动清单接棒（未配对或离线环境）

1. 运行 `{{RELAY}} pickup <项目>`，查看交接上下文与到达清单。
2. 按到达清单（`post`）逐项处理命令块，每完成一项运行 `{{RELAY}} check <项目> post <key>`。
3. 阅读 `GEMINI.md`、`AGENTS.md`、`HANDOFF.md` 与台账下一步，完成后运行 `{{RELAY}} arrive <项目>`。

## 平时

- 开工：先运行 `{{RELAY}} status`（或 `{{RELAY}} status <项目>`；已配对时可运行 `{{RELAY}} inbox` 或 `{{RELAY}} sync`；觉得不对劲时运行 `{{RELAY}} doctor`）。
- 阶段快照备份：运行 `{{RELAY}} publish <项目>`（保留本机继续编辑）。
- 收工：运行 `{{RELAY}} note <项目> --last "最近做了什么" --next "下一步"`。
- 工作区有变化（改文件、提交、切分支、新增 `.agents/` 或 `GEMINI.md`）：运行 `{{RELAY}} scan <项目>`。

## 手动清单命令块怎么处理

每条手动清单命令块都标有类别，严格按以下规则执行：

- **只读**：可以直接执行。
- **只改本机**：先把命令展示给用户看，得到同意后再执行。项目或工作区自身规则更严时从严执行。
- **写入远程**：绝不执行，原样展示给用户，由用户在自己的终端执行。

relay 自身命令也按同样的分类：`{{RELAY}} doctor`、`cache status`、`cache prune`（预览）属于**只读**；`{{RELAY}} cache prune --apply`、`{{RELAY}} device --rotate-keys`（包括加 `--force`），以及 `doctor` 给出的恢复本机状态文件的命令，属于**只改本机**，必须先给用户看并得到同意。

## 三条硬规则

1. **绝不执行任何写入用户代码 Git 远程的命令**（例如 `git push`）。`git fetch` 只读取远程、不改动远程，出现在「只改本机」的命令块里时按那一类处理：先给用户看，同意后执行。（`{{RELAY}} send` / `publish` / `sync` / `receive` 仅读写用户配对的加密云接力包，绝不向业务代码远程仓库推送分支。）
2. **绝不读取、不复制任何凭据文件**：`~/.gemini/oauth_creds.json`、`~/.claude.json`、`.credentials.json`、`~/.codex/auth.json`、`mcp_config.json` 中的密钥、`.env`、SSH / PEM 私钥。只能检查是否存在，绝不打开读取，也不写进台账或 `HANDOFF.md`。
3. **绝不把 `HANDOFF.md`、台账记录、搬运的 `brain/` 产物或归档对话（`transcripts`）说成「恢复了原 Session」**。本地会话只能在原设备原样恢复（`agy --conversation <id>` 或从桌面端 / IDE 左侧会话列表打开）。

## 与 Claude Code、Codex 共用台账

- Claude Code、Codex 和 Antigravity 读写同一份 `ledger.json` 与历史对话归档，彼此登记的 Session、备注和对话记录都能互通查阅。
- 不要删除或改写其他工具登记的 Session 条目；只维护 `--tool anti` 的条目。
- 查看全部项目用 `{{RELAY}} status`；查看本机发现的会话用 `{{RELAY}} sessions <项目> --tool anti`；检索跨设备归档历史对话全文用 `{{RELAY}} transcripts <项目>`。
