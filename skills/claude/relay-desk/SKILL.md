---
name: relay-desk
description: 用于两台电脑之间的工作接力，并维护 Claude Code、Codex 和 Antigravity 共用的台账。触发词：「接力」「接棒」「台账」「接力状态」（或 handoff / pickup）。
---

# relay-desk

本 skill 负责两台电脑之间的接力台账和命令。台账文件由 Claude Code、Codex 和 Antigravity 共用。

## 什么时候用

- 用户说「接力」（离开这台电脑）、「接棒」（到了另一台电脑）、「台账」或「接力状态」。
- 没有远程、或者不该推送的项目，要换电脑时，用「接力」，不用 handoff 插件。

## 什么时候不用

- 「交接」「接手」由 handoff 插件负责。插件会写 HANDOFF.md 并提交推送。本 skill 负责台账和命令，不负责提交或推送。

## 命令

所有命令都写成 `{{RELAY}} …`，安装时会替换成本机的调用方式。`PROJECT` 省略时取当前目录所在的项目。

## 命令块怎么处理

每条清单命令块都有一个 kind 标记：

- **只读**：可以直接执行。
- **只改本机**：先把命令给用户看，得到同意后再执行。如果项目自己的规则更严，按项目规则办。
- **写入远程**：绝不执行。原样交给用户，由用户在自己的终端执行。

## 离开（「接力」）

1. 运行 `{{RELAY}} handoff`，得到离开清单。
2. 逐项完成清单：
   - 写或更新项目里的 `HANDOFF.md`，写成摘要：做了什么、停在哪、下一步、怎么验证。只写文件，不替用户提交或推送。
   - 运行 `{{RELAY}} note PROJECT --last "最近任务" --next "下一步"`。
   - 运行 `{{RELAY}} session add PROJECT latest --tool claude` 登记当前 Session。
3. 按上面的规则处理命令块。
4. 每完成一项，运行 `{{RELAY}} check PROJECT pre KEY`。
5. 全部完成后运行 `{{RELAY}} depart PROJECT`。

## 到达（「接棒」）

1. 运行 `{{RELAY}} pickup PROJECT`，得到上下文和到达清单。
2. 按 post 清单做。命令块按上面的规则处理。每完成一项，运行 `{{RELAY}} check PROJECT post KEY`。
3. 读 `HANDOFF.md` 和台账里的下一步。
4. 运行 `{{RELAY}} arrive PROJECT`。
5. 告诉用户：原 Session 留在原设备，现在是读摘要的新 Session。

## 平时

- 开工：运行 `{{RELAY}} status`，需要时加上 `PROJECT`。
- 收工：运行 `{{RELAY}} note PROJECT --last "…" --next "…"`，记下最近任务和下一步。
- 工作区有变化：运行 `{{RELAY}} scan PROJECT`。

## 三条硬规则

1. 不执行任何写入远程的命令，例如 `git push`，原样交给用户。`git fetch` 只读取远程、不改动它，出现在「只改本机」的命令块里时按那一类处理：先给用户看，同意后执行。
2. 不读取、不复制凭据文件。`~/.claude.json`、`.credentials.json`、`~/.codex/auth.json`、`.env` 和私钥只能检查是否存在。
3. 不把 HANDOFF.md 或台账说成「恢复了 Session」。它们只是摘要。

## 对用户怎么说

到达后，必须明确说：

- 原 Session 留在原设备，不会被搬过来。
- 这里是读摘要的新 Session，只知道 HANDOFF.md 和台账里写下的内容。
