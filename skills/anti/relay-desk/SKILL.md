---
name: relay-desk
description: 双机接力台账（Windows 与 macOS 相互迁移 / two-machine handoff ledger）。当用户说「接力」「接棒」「交接」「接手」「台账」「接力状态」（或 handoff / pickup），或要把项目在 Mac 与 Windows 之间迁移、换电脑接着做、询问双机进度时使用。这份台账与 Claude Code、Codex 和网页共用。
---

# relay-desk 双机接力（Antigravity）

## 什么时候用

- 用户要离开当前电脑（Windows 或 Mac），去另一台电脑继续做（「接力」「交接」）。
- 用户到了另一台电脑接手项目（「接棒」「接手」）。
- 用户查看双机台账或项目状态（「台账」「接力状态」）。
- 日常开工、收工，或工作区发生变更后刷新事实。

台账是一份本地 JSON 文件（`ledger.json`），由命令行 `{{RELAY}}` 读写，Claude Code、Codex、Antigravity 和本机网页共用同一份文件。

文中命令一律写为 `{{RELAY}} …`，安装时已替换成当前机器的调用方式。`<项目>` 省略时取当前工作目录所属的已登记项目。

> **Windows 沙盒终端提示**：在 Windows 的 Antigravity 默认终端沙盒中，若 `py -3` 报错 `No installed Python found!`，直接把命令里的 `py -3` 换成 `python`（即 `python "<relay.py 路径>" …`）即可在沙盒内正常运行，无需申请脱离沙盒。执行时直接调用目标二进制，不要包一层 `$()` 或额外包装器。

## 平时

- 开工：先运行 `{{RELAY}} status`（或 `{{RELAY}} status <项目>`）。
- 收工：运行 `{{RELAY}} note <项目> --last "最近做了什么" --next "下一步"`。
- 工作区有变化（改文件、提交、切分支、新增 `.agents/` 或 `GEMINI.md`）：运行 `{{RELAY}} scan <项目>`。

## 离开这台设备（「接力」「交接」：Win → Mac 或 Mac → Win）

1. 运行 `{{RELAY}} handoff <项目>`，打印离开前清单与命令块。如果提示项目的最新现场不在本机，停下来告知用户。
2. 逐项完成清单：
   - 写或更新项目根目录下的 `HANDOFF.md`，写成简洁摘要：做了什么、停在哪、下一步、怎么验证。若本次会话在 `<appDataDir>/brain/<会话ID>/` 下写过计划或验证产物（Artifacts），把关键结论摘入 `HANDOFF.md`（因为 `brain/` 默认留在原设备）。只写文件，不替用户 `git commit` 或 `git push`。
   - 运行 `{{RELAY}} note <项目> --last "…" --next "…" --verify <none|pass|partial|fail>` 更新台账。
   - 运行 `{{RELAY}} session add <项目> latest --tool anti` 登记当前会话。若当前未打开工作区导致未自动匹配到，可直接传入系统上下文中的当前 Conversation ID：`{{RELAY}} session add <项目> <conversation-id> --tool anti --title "会话标题"`。
3. 按下一节规则处理清单里的命令块。
4. 每完成一项，运行 `{{RELAY}} check <项目> pre <key>`。
5. 全部完成后，运行 `{{RELAY}} depart <项目>`，将项目标记为在途。

## 到达另一台设备（「接棒」「接手」：Mac → Win 或 Win → Mac）

1. 运行 `{{RELAY}} pickup <项目>`，查看交接上下文与到达清单。
2. 按到达清单（`post`）逐项处理命令块，每完成一项运行 `{{RELAY}} check <项目> post <key>`。
3. 阅读项目里的 `GEMINI.md`、`AGENTS.md`、`HANDOFF.md` 以及台账里的下一步动作（若额外同步了原会话的 `brain/` 目录，也可按需查阅其中的产物）。
4. 运行 `{{RELAY}} arrive <项目>`，把项目归属切到本机并自动扫描。
5. 明确告诉用户：原 Antigravity 会话留在原设备，只能在原设备原样恢复；现在是读取摘要（`HANDOFF.md` 与台账）开启的新会话，不是原会话的恢复。

## 命令块怎么处理

每条命令块都标有类别，严格按以下规则执行：

- **只读**：可以直接执行。
- **只改本机**：先把命令展示给用户看，得到同意后再执行。项目或工作区自身规则更严时从严执行。
- **写入远程**：绝不执行，原样展示给用户，由用户在自己的终端执行。

## 三条硬规则

1. 不执行任何写入远程的命令（例如 `git push`）。`git fetch` 只读取远程、不改动远程，出现在「只改本机」的命令块里时按那一类处理：先给用户看，同意后执行。
2. 不读取、不复制任何凭据文件：`~/.gemini/oauth_creds.json`、`~/.claude.json`、`.credentials.json`、`~/.codex/auth.json`、`mcp_config.json` 中的密钥、`.env`、SSH 私钥。只能检查是否存在，绝不打开读取，也不写进台账或 `HANDOFF.md`。
3. 不把 `HANDOFF.md`、台账记录或搬运的 `brain/` 产物说成「恢复了原 Session」。本地会话只能在原设备原样恢复（`agy --conversation <id>` 或从桌面端 / IDE 左侧会话列表打开）。

## 与 Claude Code、Codex 共用台账

- Claude Code、Codex 和 Antigravity 读写同一份 `ledger.json`，彼此登记的 Session 和备注都能看到。
- 不要删除或改写其他工具登记的 Session 条目；只维护 `--tool anti` 的条目。
- 查看全部项目用 `{{RELAY}} status`；查看本机发现的会话用 `{{RELAY}} sessions <项目> --tool anti`。
