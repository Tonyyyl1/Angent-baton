---
name: relay-desk
description: 双机接力台账与端到端加密云端异步接力（支持 Windows、macOS、Linux 任意两台电脑相互迁移 / two-machine async handoff & ledger）。当用户说「接力」「接棒」「待接棒」「交接」「接手」「配对」「台账」「接力状态」「历史对话」（或 handoff / pickup / inbox），或要把项目在两台电脑之间迁移、换电脑接着做、查询跨端历史对话或双机进度时使用。这份台账与 Claude Code、Codex 和网页工作台共用。
---

# relay-desk 双机接力（Antigravity）

## 什么时候用

- 用户要离开当前电脑，去另一台电脑继续做（「接力」「交接」「把这个项目接力到另一台电脑」）。
- 用户到了另一台电脑接手项目（「接棒」「接手」「待接棒」）。
- 用户配置或检查双机加密云接力配对（「配对」「云接力状态」），或检索另一台电脑带过来的完整历史对话（「历史对话」）。
- 用户查看双机台账或项目状态（「台账」「接力状态」），以及日常开工、收工或工作区变更后刷新事实。

台账是一份本地 JSON 文件（`ledger.json`），与跨端历史对话归档一起由命令行 `{{RELAY}}` 维护，Claude Code、Codex、Antigravity 和本机网页工作台共用。

文中命令一律写为 `{{RELAY}} …`，安装时已替换成当前机器的调用方式。`<项目>` 省略时取当前工作目录所属的已登记项目。

> **Windows 沙盒终端提示**：在 Windows 的 Antigravity 默认终端沙盒中，若 `py -3` 报错 `No installed Python found!`，直接把命令里的 `py -3` 换成 `python`（即 `python "<relay.py 路径>" …`）即可在沙盒内正常运行，无需申请脱离沙盒。执行时直接调用目标二进制，不要包一层 `$()` 或额外包装器。

## 双机云端配对检查与首次设置

1. 运行 `{{RELAY}} pair --status` 检查是否已完成双机加密云配对（`paired`），以及 `PyNaCl` 与 `rclone` 是否就绪。
2. 如尚未配对：
   - 运行 `{{RELAY}} cloud remotes` 查看本机可用的 `rclone` 云端连接名（OneDrive、Google Drive、Dropbox、WebDAV、S3 等；两台电脑各自的连接名可以不同，只要指向同一个云端共享目录即可）。
   - 向用户确认连接名与共享目录后，运行 `{{RELAY}} pair --remote <名称> --root <共享目录>` 发布本机签名公钥。
   - 两台设备均发布公钥后，将输出的 **24 位共享配对指纹** 展示给用户在双机上核对一致，随后运行 `{{RELAY}} pair --accept` 完成公钥固定绑定。

## 离开这台设备（「接力」「交接」）

先运行 `{{RELAY}} pair --status` 查看：若已配对，**优先使用云端异步接力**；若未配对且不使用云端存储，则使用手动清单接力。

### 云端异步接力（推荐，已配对时优先使用）

1. 写或更新项目根目录下的 `HANDOFF.md`，写成简洁摘要：做了什么、停在哪、下一步、怎么验证。若本次会话在 `<appDataDir>/brain/<会话ID>/` 下写过计划或验证产物（Artifacts），把关键结论摘入 `HANDOFF.md`。只写文件，不替用户 `git commit` 或 `git push`。
2. 运行 `{{RELAY}} note <项目> --last "…" --next "…" --verify <none|pass|partial|fail>` 更新台账。
3. （推荐）运行 `{{RELAY}} session add <项目> latest --tool anti` 登记当前会话（若未打开工作区导致未自动匹配到，可直接传入当前 Conversation ID：`{{RELAY}} session add <项目> <conversation-id> --tool anti --title "会话标题"`），确保会话缺口检测覆盖当前对话。
4. 运行 `{{RELAY}} send <项目> --to <a|b>`（`--to` 可省略，默认发往另一台设备）：
   - 自动检查机密门禁与会话完整性，把 Git 分支提交、已暂存（staged）改动、未暂存（unstaged）改动、未跟踪文件、摘要以及 Claude Code / Codex / Antigravity 的完整可见历史对话端到端加密上传至云端。
   - 若命令提示 **会话缺口**（例如已登记会话缺失，或 `transcript.jsonl` 含截断字段且缺少 `transcript_full.jsonl`）或 **机密门禁拦截**（历史提交/暂存区含私钥或 `.env`），把具体列表清晰告知用户补齐或处理后重试；若上传中断，重试会自动沿用同一交接编号。
   - **必须等到输出出现「已交棒，可以关机」**（代表远端加密包与签名就绪标记均已校验通过），才明确告诉用户：**已交棒，可以关机**。

### 手动清单接力（未配对或离线环境）

1. 运行 `{{RELAY}} handoff <项目>`，打印离开前清单与命令块。如果提示项目的最新现场不在本机，停下来告知用户。
2. 逐项完成清单：写或更新 `HANDOFF.md`，运行 `{{RELAY}} note <项目> --last "…" --next "…" --verify <none|pass|partial|fail>`，运行 `{{RELAY}} session add <项目> latest --tool anti` 登记当前会话。
3. 按下节规则处理命令块，每完成一项运行 `{{RELAY}} check <项目> pre <key>`。
4. 全部完成后运行 `{{RELAY}} depart <项目>`，将项目标记为在途。

## 到达另一台设备（「接棒」「接手」）

已配对云接力时，优先走云端异步接棒：

### 云端异步接棒（推荐，已配对时优先使用）

1. 运行 `{{RELAY}} inbox` 查看待接棒交接包与解密摘要预览。
2. 运行 `{{RELAY}} receive <交接编号>` 在临时目录完成验签、防篡改校验后恢复代码与工作区状态（精确还原提交、staged 暂存区、unstaged 工作区与未跟踪文件）：
   - **首次在这台电脑接收该项目**：向用户确认本地存放路径后运行 `{{RELAY}} receive <交接编号> --path <目录>`。
   - **本机原目录存在未同步的本地改动**：系统会自动拒绝覆盖并完整保留原目录；明确告知用户原目录完好无损后，可按用户意愿运行 `{{RELAY}} receive <交接编号> --into <新目录>` 接收到新目录。
   - **回执补传恢复**：若上次本地恢复已成功但回执上传中断，再次运行 `{{RELAY}} receive <交接编号>` 会直接补传签收回执，不会重覆盖工作区。
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

- 开工：先运行 `{{RELAY}} status`（或 `{{RELAY}} status <项目>`；已配对时可运行 `{{RELAY}} inbox`）。
- 收工：运行 `{{RELAY}} note <项目> --last "最近做了什么" --next "下一步"`。
- 工作区有变化（改文件、提交、切分支、新增 `.agents/` 或 `GEMINI.md`）：运行 `{{RELAY}} scan <项目>`。

## 手动清单命令块怎么处理

每条手动清单命令块都标有类别，严格按以下规则执行：

- **只读**：可以直接执行。
- **只改本机**：先把命令展示给用户看，得到同意后再执行。项目或工作区自身规则更严时从严执行。
- **写入远程**：绝不执行，原样展示给用户，由用户在自己的终端执行。

## 三条硬规则

1. **绝不执行任何写入用户代码 Git 远程的命令**（例如 `git push`）。`git fetch` 只读取远程、不改动远程，出现在「只改本机」的命令块里时按那一类处理：先给用户看，同意后执行。（`{{RELAY}} send` / `receive` 仅读写用户配对的加密云接力包，绝不向业务代码远程仓库推送分支。）
2. **绝不读取、不复制任何凭据文件**：`~/.gemini/oauth_creds.json`、`~/.claude.json`、`.credentials.json`、`~/.codex/auth.json`、`mcp_config.json` 中的密钥、`.env`、SSH / PEM 私钥。只能检查是否存在，绝不打开读取，也不写进台账或 `HANDOFF.md`。
3. **绝不把 `HANDOFF.md`、台账记录、搬运的 `brain/` 产物或归档对话（`transcripts`）说成「恢复了原 Session」**。本地会话只能在原设备原样恢复（`agy --conversation <id>` 或从桌面端 / IDE 左侧会话列表打开）。

## 与 Claude Code、Codex 共用台账

- Claude Code、Codex 和 Antigravity 读写同一份 `ledger.json` 与历史对话归档，彼此登记的 Session、备注和对话记录都能互通查阅。
- 不要删除或改写其他工具登记的 Session 条目；只维护 `--tool anti` 的条目。
- 查看全部项目用 `{{RELAY}} status`；查看本机发现的会话用 `{{RELAY}} sessions <项目> --tool anti`；检索跨设备归档历史对话全文用 `{{RELAY}} transcripts <项目>`。
