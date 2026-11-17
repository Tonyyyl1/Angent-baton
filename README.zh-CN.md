# relay-desk

[English](./README.md) | **简体中文**

**relay-desk** 是一个本地优先、零外部依赖的双机接力台账工具，用于在 **Windows** 与 **macOS** 两台电脑之间，跨 **Claude Code**、**Codex**、**Antigravity** 和 **本机网页工作台** 无缝迁移开发进度。

四个入口读写磁盘上的同一份 `ledger.json` 台账文件——清楚记录每个项目当前停在哪台设备、哪个分支、有多少未提交改动、验证结果如何、下一步做什么，以及各代理留在原设备的本地 Session ID。

```
Claude Code ──┐
Codex ────────┼── relay 命令 ──► ledger.json ◄── relay serve ◄── 网页工作台
Antigravity ──┘
```

---

## 为什么需要 relay-desk？

在台式机（Windows）和笔记本（Mac）之间切换开发同一个项目时，最容易出问题的往往不是已推送的代码，而是：还没写完的脏工作区、不方便推个人脏分支的团队仓库、散落在本机的全局 Skills 与规则文件，以及误以为换电脑还能接着打开原本地会话。

**relay-desk** 把项目和设备上的所有状态明确分为四级：

| 分级 | 含义 | 包含内容 |
|---|---|---|
| **原生可恢复（Native）** | 两台设备能直接打开同一个会话 | 云端 Session、Claude Code Remote Control |
| **可通过 Git 同步（Git）** | 提交后随仓库或 bundle 到另一台设备（只带文件，不含对话） | 已提交代码、`CLAUDE.md`、`AGENTS.md`、`GEMINI.md`、`.claude/skills/`、`.codex/`、`.agents/`、`HANDOFF.md` |
| **需要额外同步（Extra）** | 在仓库之外或尚未提交，靠个人 WIP 分支、`git bundle`、补丁或中转目录带走 | 未提交改动、`CLAUDE.local.md`、Claude 自动记忆、全局 Skills（`~/.claude/skills/`、`~/.codex/skills/`、`~/.gemini/config/skills/`）、可选搬运的转录或 `brain/` 产物 |
| **仅能手动重建（Manual）** | 绝不同步；在每台设备各自登录、配置或读摘要开新会话 | 登录凭据（`~/.claude.json`、`~/.codex/auth.json`、`~/.gemini/oauth_creds.json`）、`.env` 密钥、本地 SQLite 会话库、运行中的进程与终端 |

---

## 核心特性

- **四方共用同一份台账**：带文件锁与原子写入保护的 `ledger.json`，命令行与本机网页（`relay serve`）实时互通。
- **原生支持三大编程代理**：内置 **Claude Code**（`claude`）、**Codex**（`codex`）与 **Antigravity**（`anti` / `agy`）的本地会话自动发现、原机恢复命令、状态扫描与专属 Skill。
- **四种代码接力传输方式**：
  - `remote`：将未提交改动提交到 `wip/handoff-<tag>` 个人分支后推送，不动主分支、不使用 `--force`。
  - `bundle`：把分支与未提交改动打包成单个 `.bundle` 文件放入中转目录——无远程仓库或不便推送个人分支的团队仓库首选。
  - `patch`：当两台设备停在同一基底提交时，导出二进制安全的 `git diff HEAD` 补丁。
  - `folder`：非 Git 目录通过 `robocopy`（Windows）/ `rsync`（macOS/Linux）经中转目录同步，默认排除 `.env`。
- **跨系统命令自动生成**：根据出发端与接收端操作系统自动生成 **PowerShell**（Windows）与 **zsh / bash**（macOS/Linux）命令块，并逐块标注安全级别（`只读` / `只改本机` / `写入远程`）。
- **零第三方依赖**：兼容 Python 3.9+，仅使用 Python 标准库，无需 `pip install`，无需构建步骤，完全离线运行。

---

## 第一次设置

需要安装 **Python 3.9+**（及 **Git**）。以下命令在 `relay-desk` 目录中执行（`relay` 在 Windows 上代表 `py -3 relay.py` 或 `relay.cmd`；在 macOS/Linux 上代表 `python3 relay.py` 或 `./relay`）。

### 1. Windows（设备 A）

```powershell
py -3 relay.py init --device a --host
py -3 relay.py device --which a --drop D:\handoff
py -3 relay.py install-skills
py -3 relay.py serve
```

### 2. macOS（设备 B）

首次执行一次 `chmod +x relay`，然后运行：

```zsh
python3 relay.py init --device b --host
python3 relay.py device --which b --drop "/Users/<你的用户名>/Handoff"
python3 relay.py install-skills
python3 relay.py serve
```

- **让两台电脑共用一份台账**：通过 `--ledger` 把路径指到同步盘（iCloud Drive、OneDrive、Dropbox、Syncthing 等）里的同一个文件，两台设备各执行一次：
  ```bash
  relay init --device a --ledger "<同步盘路径>/ledger.json" --host
  ```
  每台机器各自的指针与设备编号保存在本机 `~/.relay-desk/config.json`，不会互相覆盖。
- **安装代理 Skills**：`install-skills` 默认同时安装三个代理的 skill，也可通过 `--claude`、`--codex`、`--anti` 单独指定：
  - Claude Code → `~/.claude/skills/relay-desk`
  - Codex → `~/.codex/skills/relay-desk`（或 `$CODEX_HOME/skills/relay-desk`）
  - Antigravity → `~/.gemini/config/skills/relay-desk`（或 `$GEMINI_CONFIG_DIR/skills/relay-desk`）
- **启动本机网页工作台**：`relay serve` 默认监听 `http://127.0.0.1:8765/` 并自动打开浏览器（加 `--no-open` 可不自动弹窗，按 `Ctrl+C` 停止）。

---

## 每天怎么用

### 登记新项目

```bash
relay add my-app --path /path/to/my-app --tool all
# 补上另一台设备上的项目路径：
relay set my-app --path-other "D:\workspace\my-app"
```

### 离开这台电脑：对代理说「接力」

1. 代理运行 `relay handoff <项目>`，自动扫描本机状态并打印离开前清单与命令。
2. 代理写入或更新项目根目录下的 `HANDOFF.md`（浓缩总结：做了什么、停在哪、下一步、怎么验证），运行 `relay note` 记录最近任务与下一步，并用 `relay session add <项目> latest --tool <claude|codex|anti>` 登记当前会话。
3. 代理严格按命令块标记执行：
   - **只读**：代理直接运行。
   - **只改本机**：代理先把命令给你看，征得你同意后再运行。
   - **写入远程**（如 `git push`）：代理绝不执行，原样交给你在终端自行运行。
4. 每完成一项用 `relay check <项目> pre <key>` 勾选，全部完成后运行 `relay depart <项目>`，项目进入 **在途（transit）** 状态。

### 到另一台电脑：对代理说「接棒」

1. 代理运行 `relay pickup <项目>`，打印交接上下文、原设备 Session 信息与到达清单。
2. 按到达清单执行（确认工作区干净、取回代码/套用 bundle 或补丁、放回额外同步文件）。
3. 代理阅读项目的 `GEMINI.md` / `AGENTS.md`、`HANDOFF.md` 以及台账里的下一步动作，向你汇报接手计划。
4. 全部完成后代理运行 `relay arrive <项目>`，项目归属切换到当前设备并刷新本机扫描记录。

### 回到原设备继续原会话

先反向接力一次让代码同步，然后用台账中登记的 Session ID 在原设备原样恢复：

- **Claude Code**：`claude --resume <id>`（或 `claude --continue`）
- **Codex**：`codex resume <id>`（或 `codex resume --last`）
- **Antigravity**：`agy --conversation <id>`（或 `agy --continue`，也可直接在桌面端 / IDE 左侧会话列表打开）

---

## 命令速查

`PROJECT` 省略时自动匹配当前工作目录所属的已登记项目。

| 命令 | 作用 |
|---|---|
| `init [--device a\|b] [--ledger PATH] [--host]` | 写入本机配置 `~/.relay-desk/config.json`；`--host` 记录本机主机名。台账不存在时自动创建。 |
| `status [PROJECT] [--json]` | 无参数：每行一个项目概览。带项目名：完整详情、四级分层看板、已登记 Session 与恢复命令。 |
| `scan [PROJECT] [--all]` | 刷新本机分支、未提交数、远程、状态文件存在性与 Session 信息。`--all` 扫描全部项目并刷新全局状态。 |
| `add NAME --path PATH [--tool T] [--id ID]` | 登记项目并立即扫描（`T` 支持 `claude`、`codex`、`anti`、`both`、`claude+anti`、`codex+anti`、`all`）。 |
| `set PROJECT [...]` | 修改项目字段（`--path-here`、`--path-other`、`--alt-path`、`--tool`、`--status`、`--transport`、`--worktree`、`--device`）。 |
| `note PROJECT [...]` | 记录最近任务（`--last`）、下一步（`--next`）、备注（`--note`）与验证结果（`--verify`、`--verify-cmd`、`--verify-note`）。 |
| `sessions [PROJECT] [--tool claude\|codex\|anti] [--json]` | 列出本机发现的该项目本地 Session；已登记的标 `*`。 |
| `session add PROJECT ID\|latest [--tool T] [...]` | 登记一个 Session，`latest` 自动选取该工具最新的一条会话。 |
| `session rm PROJECT ID` | 取消登记指定的 Session。 |
| `handoff PROJECT [--transport T] [--to D]` | 从本机发起接力，打印离开清单与命令块。 |
| `plan PROJECT [--phase pre\|post] [--json]` | 重新打印当前接力的离开（`pre`）或到达（`post`）清单与命令。 |
| `check PROJECT pre\|post KEY [--undo]` | 勾选或取消勾选一条清单项。 |
| `depart PROJECT` | 将接力标记为在途（`transit`）。 |
| `pickup PROJECT` | 到达接收设备后打印交接摘要、原设备 Session 与到达清单。 |
| `arrive PROJECT` | 完成接力，写入历史（保留最近 10 条），切换项目所在设备并重新扫描。 |
| `cancel PROJECT` | 取消当前进行中的接力。 |
| `conflict add PROJECT --kind K [...]` | 记录一条冲突（`both`、`merge`、`instr`、`session`、`other`）。 |
| `device [--which a\|b] [--name N] [--os O] [--drop PATH]` | 查看或修改两台设备的名称、操作系统与中转目录。 |
| `export [FILE]` | 导出台账 JSON 备份到文件或标准输出。 |
| `import FILE [--replace]` | 导入台账备份；默认按项目 `updatedAt` 新旧合并，`--replace` 整体替换。 |
| `serve [--port 8765] [--no-open]` | 启动仅绑定 `127.0.0.1` 的本机网页工作台服务。 |
| `install-skills [--claude] [--codex] [--anti] [--dry-run] [--force]` | 安装三个代理的 skill，自动写入适配当前系统的调用命令。 |

---

## Antigravity、Claude Code 与 Codex 双机迁移说明（Win ↔ Mac）

- **项目内规则与定制**：`CLAUDE.md`、`AGENTS.md`、`GEMINI.md`、`.claude/`、`.codex/` 以及 `.agents/`（Skills、Rules、Plugins、Hooks）均纳入扫描与分级，提交后随 Git 或 `bundle` 文件直达另一台设备。
- **全局规则、Skills 与 MCP 配置**：在网页「状态登记」标签页点亮本机存在的全局项（如 `~/.claude/skills/`、`~/.codex/skills/`、`~/.gemini/GEMINI.md`、`~/.gemini/config/skills/`、`~/.gemini/config/mcp_config.json`），即可自动生成 Windows（PowerShell `robocopy` / `Copy-Item` / `Compare-Object`）与 macOS（zsh `rsync` / `cp` / `diff`）的双向同步命令；可能含本机路径或令牌的配置文件只会生成副本供对照合并，绝不直接覆盖。
- **会话转录与 Antigravity `brain/` 产物**：本地会话默认留在原设备。若把某条会话的带走方式设为 `transcript`，清单会生成经中转目录同步 Claude `.jsonl` 转录或 Antigravity `~/.gemini/antigravity/brain/<id>/`（含计划、验证报告等 Artifacts 与 `transcript.jsonl`）的双向命令，方便另一台设备上的新会话直接查阅参考，不触碰本机 SQLite 会话数据库。

---

## 安全与边界承诺（它绝不会做的事）

1. **绝不联网**：不执行 `git push`、`git fetch`、`git pull` 或任何网络请求，只生成并打印命令。
2. **绝不读凭据**：`~/.claude.json`、`.credentials.json`、`~/.codex/auth.json`、`~/.gemini/oauth_creds.json`、`.env` 和私钥仅检查文件是否存在，绝不打开读取、复制或写进台账。
3. **摘要不是 Session**：绝不把 `HANDOFF.md` 或台账记录说成「恢复了原 Session」。本地会话只能在原设备原样恢复。
4. **绝不改动业务仓库**：`relay` 只写 `ledger.json`（及锁文件）、`~/.relay-desk/config.json` 与 `install-skills` 目标目录，不改任何项目仓库文件或 Git 状态。
5. **本机回环安全防护**：`relay serve` 仅监听 `127.0.0.1`，强制校验 `Host`、`Origin` 与 `X-Relay-Desk` 请求头，不开放任意文件读取。

---

## 目录结构

```
relay-desk/
├── relay.py                 # 跨平台命令行入口
├── relay / relay.cmd        # macOS/Linux 与 Windows 快捷包装脚本
├── relay-desk.html          # 单文件网页工作台（同时兼容作为 claude.ai Artifact 发布）
├── relaydesk/
│   ├── ledger.py            # 台账原子读写、文件锁、规范化与本机设备识别
│   ├── scan.py              # 只读扫描：Git 事实、状态文件存在性、三大代理 Session 发现
│   ├── plan.py              # 四级分层计算、交接清单与跨系统 Shell 命令生成
│   ├── cli.py               # 命令行子命令与 Skill 安装器
│   └── server.py            # 127.0.0.1 本机网页与 JSON API 服务
├── skills/
│   ├── claude/relay-desk/   # Claude Code 专用 SKILL.md 模板
│   ├── codex/relay-desk/    # Codex 专用 SKILL.md 模板
│   └── anti/relay-desk/     # Antigravity 专用 SKILL.md 模板
├── docs/
│   └── SPEC.md              # 架构与模块接口规格说明
└── tests/                   # 基于标准库 unittest 的完整测试集
```

## 运行测试

所有测试均在隔离的临时目录中运行，不会触碰真实项目仓库：

```bash
# macOS / Linux
python3 -m unittest discover -s tests -v

# Windows
py -3 -m unittest discover -s tests -v
```

## 开源协议

基于 [MIT License](./LICENSE) 发布。
