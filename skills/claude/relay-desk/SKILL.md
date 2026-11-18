---
name: relay-desk
description: 用于两台电脑之间的工作接力（支持端到端加密云端异步接力与手动清单接力），并维护 Claude Code、Codex 和 Antigravity 共用的台账与跨端历史对话归档。触发词：「接力」「接棒」「待接棒」「配对」「台账」「接力状态」「历史对话」（或 handoff / pickup / inbox）。
---

# relay-desk

本 skill 负责两台桌面电脑（Windows / macOS / Linux 任意两台组合）之间的代码现场接力、跨端历史对话归档查阅与共用台账。台账与对话归档由 Claude Code、Codex 和 Antigravity 共用。

## 什么时候用

- 用户说「接力」「把这个项目接力到另一台电脑」（离开这台电脑）。
- 用户说「接棒」「待接棒」（到了另一台电脑接收工作现场）。
- 用户说「配对」「云接力状态」「台账」「接力状态」或想查另一台电脑上的历史对话。
- 没有 Git 远程、或者不该往代码远程推送个人脏分支的项目，要换电脑时，用「接力」，不用 handoff 插件。

## 什么时候不用

- 「交接」「接手」由 Claude 的 handoff 插件负责。那个插件会写 `HANDOFF.md` 并直接 `git commit` + `git push` 到代码远程仓库。本 skill 负责加密云接力包、台账和本地命令，绝不自动提交或推送用户的 Git 代码远程。

## 命令约定

所有命令都写成 `{{RELAY}} …`，安装时会自动替换成本机的实际调用方式。`PROJECT` 省略时自动匹配当前目录所属的已登记项目。

## 双机云端配对检查与首次设置

首次使用云端异步接力前（或用户询问配对状态时）：

1. 运行 `{{RELAY}} pair --status` 查看本机是否已完成云接力配对（`paired`），以及 `PyNaCl` 与 `rclone` 是否就绪。
2. 若尚未配置或未配对：
   - 运行 `{{RELAY}} cloud remotes` 查看本机已授权的 `rclone` 云存储连接名（OneDrive、Google Drive、Dropbox、WebDAV、S3 等；两台电脑的连接名可以不同，只需指向同一个云端共享目录）。
   - 向用户确认连接名与共享目录后，运行 `{{RELAY}} pair --remote <连接名> --root <共享目录>` 生成并发布本机签名公钥。
   - 两台电脑都发布公钥后，把命令输出的 **24 位共享配对指纹** 展示给用户，请用户在两台设备上核对完全一致后，再运行 `{{RELAY}} pair --accept` 完成绑定。

## 离开这台电脑（「接力」）

先运行 `{{RELAY}} pair --status` 检查：若已完成双机云配对，**优先使用方式 A（云端异步接力）**；若未配对且用户希望离线/手动搬运，使用方式 B（手动清单接力）。

### 方式 A：云端异步接力（推荐，已配对时优先使用）

无需两台电脑同时在线，自动打包 Git 分支提交、已暂存（staged）改动、未暂存（unstaged）改动、未跟踪文件、摘要以及 Claude Code / Codex / Antigravity 的完整可见历史对话，端到端加密上传云端：

1. 写或更新项目根目录下的 `HANDOFF.md`（简洁摘要：做了什么、停在哪、下一步、怎么验证；只写文件，不替用户 `git commit` 或 `git push`）。
2. 运行 `{{RELAY}} note PROJECT --last "最近任务" --next "下一步" --verify <none|pass|partial|fail>` 更新台账。
3. （推荐）运行 `{{RELAY}} session add PROJECT latest --tool claude` 登记当前会话，以便缺口检测覆盖当前对话。
4. 运行 `{{RELAY}} send PROJECT --to <a|b>`（`--to` 可省略，默认发往另一台设备）：
   - 若提示 **会话缺口**（已登记会话缺失、未刷盘截断）或 **机密门禁拦截**（Git 历史或暂存区含私钥、`.env` 等凭据），把具体项原样列给用户补齐或排除后再重试；若上传中途中断，重试会自动沿用同一交接编号。
   - **必须等待命令输出包含「已交棒，可以关机」**（代表加密包与签名清单均已上传并通过远端完整性复核），再明确告诉用户：**已交棒，可以关机**。

### 方式 B：手动清单接力（未配对或离线环境）

1. 运行 `{{RELAY}} handoff PROJECT`，得到离开清单和命令块。
2. 逐项完成清单：
   - 写或更新项目里的 `HANDOFF.md`（只写文件，不替用户提交或推送）。
   - 运行 `{{RELAY}} note PROJECT --last "最近任务" --next "下一步"`。
   - 运行 `{{RELAY}} session add PROJECT latest --tool claude` 登记当前 Session。
3. 按下文的「命令块怎么处理」规则处理每条命令块，每完成一项运行 `{{RELAY}} check PROJECT pre KEY`。
4. 全部完成后运行 `{{RELAY}} depart PROJECT`。

## 到达另一台电脑（「接棒」）

先看是否有云端待接棒交接包（已配对时优先运行 `{{RELAY}} inbox`）：

### 方式 A：云端异步接棒（推荐，已配对时优先使用）

1. 运行 `{{RELAY}} inbox` 查看发往本机的待接棒交接包及解密摘要预览。
2. 运行 `{{RELAY}} receive <交接编号>` 验签解包并恢复代码现场（包含提交、staged 暂存区、unstaged 工作区与未跟踪文件）：
   - **首次在这台电脑接收该项目**：向用户确认本地存放路径后，运行 `{{RELAY}} receive <交接编号> --path <本地目录>`。
   - **本机原目录已有未同步改动**：系统会自动拒绝覆盖并完整保留原目录；告知用户原目录未受影响，并按用户意愿运行 `{{RELAY}} receive <交接编号> --into <新目录>` 安全接收到新目录。
   - **回执补传恢复**：若上次本地解包已成功但回执上传中断（`inbox` 标记待补传回执），再次运行 `{{RELAY}} receive <交接编号>` 即可直接补传签收回执，不会重复覆盖工作区。
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

- 开工：运行 `{{RELAY}} status`，需要时加上 `PROJECT`（已配对时也可顺手跑 `{{RELAY}} inbox` 看有无新交接包）。
- 收工：运行 `{{RELAY}} note PROJECT --last "…" --next "…"`，记下最近任务和下一步。
- 工作区有变化：运行 `{{RELAY}} scan PROJECT`。

## 手动清单的命令块怎么处理

每条手动交接清单命令块都有一个 kind 标记：

- **只读**：可以直接执行。
- **只改本机**：先把命令给用户看，得到同意后再执行。如果项目自己的规则更严，按项目规则办。
- **写入远程**：绝不执行。原样交给用户，由用户在自己的终端执行。

## 三条硬规则

1. **绝不往用户的代码 Git 远程执行写入命令**（例如 `git push`），这类命令一律原样交给用户自行决定。`git fetch` 只读取远程、不改动它，出现在「只改本机」的命令块里时按那一类处理：先给用户看，同意后执行。（`{{RELAY}} send` / `receive` 仅读写用户配对的加密接力存储包，绝不触碰业务 Git 远端分支。）
2. **绝不读取、不复制任何凭据与密钥文件**：`~/.claude.json`、`.credentials.json`、`~/.codex/auth.json`、`~/.gemini/oauth_creds.json`、`.env` 及各类私钥（SSH / PEM / KEY）最多只能检查文件是否存在，绝不打开读取、复制或写进台账与 `HANDOFF.md`。
3. **绝不把 `HANDOFF.md`、台账记录或归档的历史对话（`transcripts`）说成「恢复了原 Session」**。原本地 Session 留在原设备，只能在原设备原样恢复。

## 对用户怎么说

- 在出发端执行 `{{RELAY}} send` 时：必须等命令真正输出 **「已交棒，可以关机」** 后，才告诉用户可以放心关机。
- 在接收端完成接棒后，必须明确告诉用户：
  - 原本地 Session 留在原设备，不会被直接搬成当前设备的活动原生会话。
  - 当前是读取 `HANDOFF.md` 与台账的新 Session，但可以随时通过 `{{RELAY}} transcripts` 按需检索原设备上 Claude Code、Codex 和 Antigravity 的完整历史对话全文。
