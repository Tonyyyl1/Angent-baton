---
name: relay-desk
description: 用于多台电脑之间的工作接力（支持端到端加密云端异步接力、多机快照同步与手动清单接力），并维护 Claude Code、Codex 和 Antigravity 共用的台账与跨端历史对话归档。触发词：「接力」「接棒」「待接棒」「配对」「台账」「接力状态」「历史对话」（或 handoff / pickup / inbox / publish / sync）。
---

# relay-desk

本 skill 负责多台桌面电脑（Windows / macOS / Linux 任意多台设备组合）之间的代码现场接力、快照网络同步、跨端历史对话归档查阅与共用台账。台账与对话归档由 Claude Code、Codex 和 Antigravity 共用。

## 什么时候用

- 用户说「接力」「把这个项目接力到另一台电脑」（离开这台电脑）。
- 用户说「接棒」「待接棒」（在目标电脑接收工作现场）。
- 用户说「配对」「云接力状态」「台账」「接力状态」「同步快照」或想查其他电脑上的历史对话。
- 没有 Git 远程、或者不该往代码远程推送个人脏分支的项目，要换电脑时，用「接力」，不用 handoff 插件。

## 什么时候不用

- 「交接」「接手」由 Claude 的 handoff 插件负责。那个插件会写 `HANDOFF.md` 并直接 `git commit` + `git push` 到代码远程仓库。本 skill 负责加密云接力包、台账和本地命令，绝不自动提交或推送用户的 Git 代码远程。

## 命令约定

所有命令都写成 `{{RELAY}} …`，安装时会自动替换成本机的实际调用方式。`PROJECT` 省略时自动匹配当前目录所属的已登记项目。

## 多机初始化、云端配对与设备检查

首次使用或新增设备（例如第 3 台电脑 `c` 或 `linux-server`）时：

1. 本机初始化设备别名并查询全部设备状态：
   - 若本机尚未绑定设备别名，运行 `{{RELAY}} init --device ALIAS`（`ALIAS` 可为 `a`、`b`、`c`、`linux-server` 等 1~40 位合法别名）。
   - 运行 `{{RELAY}} device --json` 查看台账中所有已登记设备、本机 `deviceId`、已配对/待确认/密钥变更（`key_changed`）的对端列表。
2. 检查云端配对与环境就绪状态：
   - 运行 `{{RELAY}} pair --status`（或针对某台设备运行 `{{RELAY}} pair --with DEVICE --status`）查看是否已配对，以及 `PyNaCl` 与 `rclone` 是否就绪。
3. 若尚未配置或需配对新设备（或对端密钥已轮换）：
   - 运行 `{{RELAY}} cloud remotes` 查看本机已授权的 `rclone` 云存储连接名（OneDrive、Google Drive、Dropbox、WebDAV、S3 等；各台电脑的连接名可以不同，只需指向同一个云端共享目录）。
   - 向用户确认连接名与共享目录后，运行 `{{RELAY}} pair --remote <连接名> --root <共享目录>` 生成并发布本机签名公钥。
   - 发现对端设备后，运行 `{{RELAY}} pair --with DEVICE --status` 获取与该设备之间的 **24 位共享配对指纹**，展示给用户并在两台设备上核对完全一致；如有同别名冲突需替换旧设备，请用户明确确认后加 `--replace-device`（或用 `--alias <新别名>` 区分）。
   - 用户确认指纹一致后，运行 `{{RELAY}} pair --with DEVICE --accept --fingerprint FP` 完成该对端的公钥固定绑定。

## 快照发布与网络同步（不交出编辑权）

在不转移项目编辑归属权的前提下同步工作快照与历史对话：

- **发布当前项目快照缓存**：运行 `{{RELAY}} publish PROJECT`
  - 打包受端到端加密保护的快照与三端可见历史对话上传至云端缓存并更新 `have` 清单，**本机继续保留项目编辑权（`project.device` 不变）**。
- **同步网络快照与转发**：运行 `{{RELAY}} sync [PROJECT]`（或后台持续监听 `{{RELAY}} sync --watch`）
  - 仅同步签收回执、拉取并校验缓存他机快照、维护 `versionGraph` 分叉检测，并把已知最新头快照转发给缺失的已配对 v3 设备；**只写本地缓存与归档，绝不自动覆盖本地工作区文件**。
  - 退出码：0 成功，3 部分失败（只剩会自动重试的问题），4 需要处理，130 已取消。输出「同步状态：需要处理」时，把每条未解决问题和它后面的「处理：」命令原样告诉用户，不要自行猜测处理办法。
  - 需要逐条读取事件时用 `{{RELAY}} sync --jsonl`（可加 `--watch`）：每个事件一行 JSON，最后一行 `kind` 为 `summary`。`--jsonl` 不能和 `--json` 同用。

## 诊断、换钥与缓存清理

- **只读诊断**：`{{RELAY}} doctor [PROJECT]`（加 `--json` 供解析）检查身份、配对、当前编辑设备、在途交接、恢复事务、最近一次同步结果和缓存状态，每条问题给出原因和一条处理命令；退出码 0 正常、3 有警告、4 需要处理。用户问「接力状态」、接不到交接或同步出错时先运行它，把原因和处理命令原样转述给用户；处理命令本身按下文「命令块怎么处理」的分类执行。设备显示「未知」只表示没有它最近的缓存清单，不要说成离线或缺少快照。
- **缓存统计**：`{{RELAY}} cache status [PROJECT]`，只读，可直接运行。
- **缓存清理**：
  - `{{RELAY}} cache prune [PROJECT]`（默认就是 `--dry-run`）只预览、不删除，可直接运行；把可删除项、保留原因和预计释放空间告诉用户。
  - `{{RELAY}} cache prune [PROJECT] --apply --expect-plan <计划摘要>` 会删除本机缓存，属于**只改本机**：先把预览结果和这条命令给用户看，得到同意后再执行，并始终带上预览给出的 `--expect-plan`。计划变化时它不删除（退出码 3），需要重新预览并再次征得同意。清理从不删除云端对象、防重放记录、恢复事务或凭据。
  - 清理中断后 `doctor` 报 `prune_interrupted`：再次运行 `--apply`（同样先征得用户同意）即可完成。
- **本机换钥**：`{{RELAY}} device --rotate-keys` 属于**只改本机**，先说明影响（所有对端都要重新核对新指纹）并得到用户同意。有在途交接、未上传回执或未完成接收时会被拒绝（退出码 4）并列出处理命令，优先按这些命令处理完再换钥。`--force` 会让在途交接都需要重新签发，同样属于**只改本机**：只有在用户看过受影响的交接列表并明确同意后才可加 `--force`。
- **对端换钥**：`device --json` 或 `pair --status` 显示 `key_changed` 时，请用户在两台设备上核对新指纹，一致后再运行 `{{RELAY}} pair --with DEVICE --accept --fingerprint FP`，绝不跳过核对。确认后 `sync` 会自动为新公钥重新投递缓存快照，原投递和回执保持不变。
- **重新签发在途交接**：`send` 或 `doctor` 报 `handoff_recipient_key_changed`（目标设备换钥）或 `handoff_sender_key_rotated`（本机换钥）时，原交接已无法领取。确认对端新指纹已核对并重新确认后，征得用户同意再运行 `{{RELAY}} send PROJECT --to DEVICE --reissue`：同一快照、新交接编号、新密文，原交接包不变，对方只能领取其中一个。普通 `send` 不会自动重新签发；目标设备必须已升级到支持重新签发的版本。

## 离开这台电脑（「接力」）

先运行 `{{RELAY}} device --json` 与 `{{RELAY}} pair --status` 检查：若存在已配对设备，**优先使用方式 A（云端异步接力）**；若未配对且用户希望离线/手动搬运，使用方式 B（手动清单接力）。

### 方式 A：云端异步接力（推荐，已配对时优先使用）

无需两台电脑同时在线，自动打包 Git 分支提交、已暂存（staged）改动、未暂存（unstaged）改动、未跟踪文件、摘要以及 Claude Code / Codex / Antigravity 的完整可见历史对话，端到端加密并显式授权目标设备上传云端：

1. 写或更新项目根目录下的 `HANDOFF.md`（简洁摘要：做了什么、停在哪、下一步、怎么验证；只写文件，不替用户 `git commit` 或 `git push`）。
2. 运行 `{{RELAY}} note PROJECT --last "最近任务" --next "下一步" --verify <none|pass|partial|fail>` 更新台账。
3. （推荐）运行 `{{RELAY}} session add PROJECT latest --tool claude` 登记当前会话，以便缺口检测覆盖当前对话。
4. **确认目标设备**：先运行 `{{RELAY}} device --json` 查看所有已配对设备。
   - **当存在多个（>=2）已配对活跃对端时，必须把候选设备列表展示给用户，请用户明确指定要交棒给哪台设备（`DEVICE`），绝不可自行猜测目标！**
   - 仅当只有 1 台已配对设备时才可省略 `--to`。
5. 运行 `{{RELAY}} send PROJECT --to DEVICE`：
   - 若提示 **多对端歧义（`ambiguous_peer`）**、**对端密钥变更（`key_changed`）**、**已有未签收交接冲突（`active_handoff_exists`）**、**会话缺口** 或 **机密门禁拦截**（Git 历史或暂存区含私钥、`.env` 等凭据），把具体信息原样列给用户确认或处理后再重试；若上传中途中断，重试会自动沿用同一交接编号。
   - 若提示 **在途交接因换钥无法领取（`handoff_recipient_key_changed` / `handoff_sender_key_rotated`）**，按上文「重新签发在途交接」处理。
   - **必须等待命令输出包含「已交棒，可以关机」**（代表加密包与签名清单均已上传并通过远端完整性复核），再明确告诉用户：**已交棒，可以关机**。

### 方式 B：手动清单接力（未配对或离线环境）

1. 运行 `{{RELAY}} handoff PROJECT`（多台设备时加上 `--to DEVICE`），得到离开清单和命令块。
2. 逐项完成清单：
   - 写或更新项目里的 `HANDOFF.md`（只写文件，不替用户提交或推送）。
   - 运行 `{{RELAY}} note PROJECT --last "最近任务" --next "下一步"`。
   - 运行 `{{RELAY}} session add PROJECT latest --tool claude` 登记当前 Session。
3. 按下文的「命令块怎么处理」规则处理每条命令块，每完成一项运行 `{{RELAY}} check PROJECT pre KEY`。
4. 全部完成后运行 `{{RELAY}} depart PROJECT`。

## 到达目标电脑（「接棒」）

先看是否有云端待接棒交接包（已配对时优先运行 `{{RELAY}} inbox`）：

### 方式 A：云端异步接棒（推荐，已配对时优先使用）

1. 运行 `{{RELAY}} inbox` 查看显式发往并授权给本机的待接棒交接包及解密摘要预览。
2. 运行 `{{RELAY}} receive RELAY_ID` 验签解包、校验版本链（`versionGraph`）并原子事务化恢复代码现场（包含提交、staged 暂存区、unstaged 工作区与未跟踪文件）：
   - **首次在这台电脑接收该项目**：向用户确认本地存放路径后，运行 `{{RELAY}} receive RELAY_ID --path <本地目录>`。
   - **本机原目录已有未同步改动、触发恢复冲突（`recovery_conflict`）或版本分叉（`version_fork`）**：系统会自动拒绝覆盖并完整保留原目录；告知用户原目录未受影响，并按用户意愿运行 `{{RELAY}} receive RELAY_ID --into <新目录>` 安全接收到新目录。
   - **断电/中断恢复与事务回滚**：若接收过程中被中断，可先运行 `{{RELAY}} recover RELAY_ID --status` 查看事务状态与冲突原因；重跑 `{{RELAY}} receive RELAY_ID` 可继续恢复或补传签收回执；若用户决定放弃此次中断接收并回到接收前状态，运行 `{{RELAY}} recover RELAY_ID --rollback`。
   - **原交接已被重新签发的交接取代**（`handoff_superseded` / `superseded_original_claimed`）：对方只能领取其中一个；运行 `{{RELAY}} inbox` 查看当前可领取的交接编号，告诉用户实际情况，不要尝试绕过。
   - **本机状态文件损坏**（`local_state_corrupted`、`delivery_state_corrupted`、`recovery_corrupted`）：命令已停止且文件未被改动。绝不删除或重建这些文件；运行 `{{RELAY}} doctor` 把原因和处理命令（通常是从备份恢复）交给用户，按「只改本机」得到同意后再执行。
3. 阅读项目的 `CLAUDE.md`、`HANDOFF.md` 以及台账里的下一步动作。
4. 按需通过 `{{RELAY}} transcripts PROJECT` 查阅跨设备同步归档的 **Claude Code、Codex、Antigravity 完整历史对话全文**：
   - 查看归档会话列表：`{{RELAY}} transcripts PROJECT`（可加 `--tool <claude|codex|anti>`）
   - 阅读某条完整历史对话：`{{RELAY}} transcripts PROJECT --session <会话ID>`
   - 按关键词跨会话检索：`{{RELAY}} transcripts PROJECT --grep "<关键词>"`

### 方式 B：手动清单接棒（未配对或离线环境）

1. 运行 `{{RELAY}} pickup PROJECT`，得到上下文和到达清单。
2. 按 `post` 清单做，命令块按下面规则处理；每完成一项运行 `{{RELAY}} check PROJECT post KEY`。
3. 读 `HANDOFF.md` 和台账里的下一步，完成后运行 `{{RELAY}} arrive PROJECT`。

## 平时

- 开工：运行 `{{RELAY}} status`，需要时加上 `PROJECT`（已配对时也可顺手跑 `{{RELAY}} inbox` 或 `{{RELAY}} sync`；觉得不对劲时运行 `{{RELAY}} doctor`）。
- 阶段备份快照：运行 `{{RELAY}} publish PROJECT`（保留本机继续编辑）。
- 收工：运行 `{{RELAY}} note PROJECT --last "…" --next "…"`，记下最近任务和下一步。
- 工作区有变化：运行 `{{RELAY}} scan PROJECT`。

## 手动清单的命令块怎么处理

每条手动交接清单命令块都有一个 kind 标记：

- **只读**：可以直接执行。
- **只改本机**：先把命令给用户看，得到同意后再执行。如果项目自己的规则更严，按项目规则办。
- **写入远程**：绝不执行。原样交给用户，由用户在自己的终端执行。

relay 自身命令也按同样的分类：`{{RELAY}} doctor`、`cache status`、`cache prune`（预览）属于**只读**；`{{RELAY}} cache prune --apply`、`{{RELAY}} device --rotate-keys`（包括加 `--force`），以及 `doctor` 给出的恢复本机状态文件的命令，属于**只改本机**，必须先给用户看并得到同意。

## 三条硬规则

1. **绝不往用户的代码 Git 远程执行写入命令**（例如 `git push`），这类命令一律原样交给用户自行决定。`git fetch` 只读取远程、不改动它，出现在「只改本机」的命令块里时按那一类处理：先给用户看，同意后执行。（`{{RELAY}} send` / `publish` / `sync` / `receive` 仅读写用户配对的加密接力存储包，绝不触碰业务 Git 远端分支。）
2. **绝不读取、不复制任何凭据与密钥文件**：`~/.claude.json`、`.credentials.json`、`~/.codex/auth.json`、`~/.gemini/oauth_creds.json`、`.env` 及各类私钥（SSH / PEM / KEY）最多只能检查文件是否存在，绝不打开读取、复制或写进台账与 `HANDOFF.md`。
3. **绝不把 `HANDOFF.md`、台账记录或归档的历史对话（`transcripts`）说成「恢复了原 Session」**。原本地 Session 留在原设备，只能在原设备原样恢复。

## 对用户怎么说

- 在出发端执行 `{{RELAY}} send` 时：必须等命令真正输出 **「已交棒，可以关机」** 后，才告诉用户可以放心关机。
- 在接收端完成接棒后，必须明确告诉用户：
  - 原本地 Session 留在原设备，不会被直接搬成当前设备的活动原生会话。
  - 当前是读取 `HANDOFF.md` 与台账的新 Session，但可以随时通过 `{{RELAY}} transcripts` 按需检索原设备上 Claude Code、Codex 和 Antigravity 的完整历史对话全文。
