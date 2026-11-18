# relay-desk

[English](./README.md) | **简体中文**

**relay-desk** 是一个本地优先的双机开发接力与台账工具，支持 **Windows**、**macOS**、**Linux** 任意两台桌面电脑组合（包括 Win ↔ Mac、Mac ↔ Mac、Win ↔ Win、Linux ↔ Mac 等），跨 **Claude Code**、**Codex**、**Antigravity** 和 **本机网页工作台** 无缝迁移代码现场与完整对话上下文。

四个入口共用本机 `ledger.json` 台账与跨端历史对话归档；既支持 **端到端加密云端异步接力**（A 机交棒后立即关机、B 机随时开机验签接棒，无需同时在线），也保留 **零外部依赖的手动清单接力**（完全离线可用）。

```
Claude Code ──┐                                              ┌── 加密云端异步接力 (rclone / S3 / WebDAV / 网盘)
Codex ────────┼── relay 命令 ──► ledger.json + 对话归档 ◄────┤
Antigravity ──┤                       ▲                      └── 手动清单接力 (remote / bundle / patch / folder)
网页工作台 ───┴──── relay serve ──────┘
```

---

## 为什么需要 relay-desk？

在两台电脑之间切换开发同一个项目时，最容易出问题的往往不是已经推送的提交，而是：还没写完的已暂存（staged）/ 未暂存（unstaged）工作区、不方便往团队仓库强推的个人脏分支、散落在本机的全局 Skills 与规则文件，以及换电脑后找不到上一台机器里代理讨论过的上下文细节。

**relay-desk** 把项目和设备上的所有状态明确分为四级，并用端到端加密包把可迁移部分一次带齐：

| 分级 | 含义 | 包含内容 |
|---|---|---|
| **原生可恢复（Native）** | 两台设备能直接打开同一个在线会话 | 云端 Session、Claude Code Remote Control |
| **可通过 Git 同步（Git）** | 随提交或 `git bundle` 跨机迁移 | 已提交代码、`CLAUDE.md`、`AGENTS.md`、`GEMINI.md`、`.claude/skills/`、`.codex/`、`.agents/`、`HANDOFF.md` |
| **需要额外同步（Extra）** | 尚未提交或在仓库之外；云接力自动加密打包，手动模式经 WIP 分支 / `bundle` / 补丁 / 中转目录带走 | 已暂存（staged）改动、未暂存（unstaged）改动、未跟踪文件、三端完整可见历史对话归档、全局 Skills 与规则文件 |
| **仅能手动重建（Manual）** | 绝不同步；每台设备各自登录与配置，原本地会话留在原机 | 登录凭据（`~/.claude.json`、`~/.codex/auth.json`、`~/.gemini/oauth_creds.json`）、`.env` 密钥、私钥、本地 SQLite 会话库、运行中的进程 |

---

## 核心特性

- **端到端加密云端异步接力（错时关机接棒）**：
  - 使用 `PyNaCl`（libsodium `crypto_secretstream_xchacha20poly1305` 流式认证加密 + X25519 密钥封装 + Ed25519 独立签名）在本地完成加密与签名，云存储只接触密文。
  - **精确还原工作区**：自动打包 Git 提交与分支、**已暂存（staged）改动**、**未暂存（unstaged）改动** 与 **未跟踪文件**（或非 Git 目录快照），并在打包前执行 **机密门禁**（自动排除 `.env`/凭据，若 Git 历史或暂存区含私钥或凭据立即拦截）。
  - **两阶段发布与远端复核**：先上传 `package.enc` 并核验大小，再上传签名的 `ready.json` 并完成远端存在性复核，确认无误后输出 **「已交棒，可以关机」**。中断重跑自动沿用同一交接编号；未发布 `ready.json` 的半成品包绝不会被对端误领。
  - **安全解包与本地改动保护**：接收端先在隔离临时目录完成验签、SHA-256 与流式解密校验；若本机原目录已有未同步改动，**完整保留原目录绝不覆盖**，并支持通过 `--into <新目录>` 安全接到新目录。
- **三大代理完整历史对话跨端归档与去重检索**：
  - 交棒时自动检查已登记会话是否存在缺失或未刷盘截断（列出具体缺口供补齐重试），归档 **Claude Code**、**Codex**、**Antigravity** 全部已落盘可见对话，按 `(tool, sessionId, contentVersion)` 自动去重合并，往返多次接力零重复，随时用 `relay transcripts` 或网页弹窗全文检索。
- **真正可换云的存储接口（`StorageProvider`）**：
  - 默认通过 `rclone` 适配 OneDrive、Google Drive、Dropbox、WebDAV、S3 兼容对象存储等任意后端；两台设备各自的 `rclone` 连接名无需相同，指向同一云端共享目录即可。扩展新存储只需实现统一的 `StorageProvider` 接口（详见 [`docs/STORAGE_PROVIDER.md`](./docs/STORAGE_PROVIDER.md)）。
- **零依赖手动清单接力兜底**：
  - 不安装任何第三方库也可完整使用台账、扫描、网页工作台以及 `remote` / `bundle` / `patch` / `folder` 四种跨系统（PowerShell / zsh / bash）手动接力命令生成。

---

## 安装与环境准备

- **基础环境（必需）**：**Python 3.9+** 与 **Git**（仅使用标准库即可运行台账、扫描、手动接力与 `relay serve` 网页工作台）。
- **云端异步接力（可选，推荐安装）**：
  1. 安装 `PyNaCl`（提供 libsodium 端到端加密与签名）：
     ```bash
     # macOS / Linux
     python3 -m pip install --user pynacl
     # Windows
     py -3 -m pip install --user pynacl
     ```
  2. 安装并配置 [`rclone`](https://rclone.org/)（用于连接网盘、WebDAV 或 S3 存储）：
     ```bash
     # macOS
     brew install rclone
     # Windows
     winget install Rclone.Rclone
     # 按向导授权你的网盘、WebDAV 或 S3 连接：
     rclone config
     ```

---

## 第一次设置

以下命令在 `relay-desk` 目录中执行（`relay` 在 Windows 上代表 `py -3 relay.py` 或 `relay.cmd`；在 macOS / Linux 上代表 `python3 relay.py` 或 `./relay`）。

### 1. 初始化两台设备并安装代理 Skills

在 **设备 A** 上执行：

```bash
relay init --device a --host
relay install-skills
```

在 **设备 B** 上执行（首次在 macOS/Linux 上可先 `chmod +x relay`）：

```bash
relay init --device b --host
relay install-skills
```

- `install-skills` 默认同时把 `relay-desk` skill 安装到三大代理目录（也可通过 `--claude`、`--codex`、`--anti` 单独指定）：
  - Claude Code → `~/.claude/skills/relay-desk`
  - Codex → `~/.codex/skills/relay-desk`（或 `$CODEX_HOME/skills/relay-desk`）
  - Antigravity → `~/.gemini/config/skills/relay-desk`（或 `$GEMINI_CONFIG_DIR/skills/relay-desk`）

### 2. 配对双机加密云接力（推荐）

两台电脑可以使用各自不同的 `rclone` 连接名（例如 A 叫 `onedrive_work`，B 叫 `my_onedrive`），只要指向同一个云端共享目录：

```bash
# 1. 查看本机已配置的 rclone 连接名
relay cloud remotes

# 2. 在设备 A 发布本机签名公钥
relay pair --remote <A的连接名> --root relay-desk

# 3. 在设备 B 发布本机签名公钥（指向同一云端目录）
relay pair --remote <B的连接名> --root relay-desk

# 4. 在两台设备上查看并人工核对 24 位「共享配对指纹」完全一致后，分别确认绑定：
relay pair --accept
relay pair --status
```

（也可以运行 `relay serve` 打开 `http://127.0.0.1:8765/`，在 **「设置 → 云接力与设备配对」** 面板中点选完成。）

---

## 每天怎么用

### 登记项目

```bash
relay add my-app --path /path/to/my-app --tool all
```

### 方式一：加密云端异步接力（A 交棒即关机 → B 随时开机接棒）

#### 1. 离开设备 A：对代理说「接力」（或手动执行）

1. 代理更新 `HANDOFF.md` 摘要、`relay note` 任务备注，并用 `relay session add <项目> latest --tool <claude|codex|anti>` 登记当前会话。
2. 执行加密打包上传：
   ```bash
   relay send my-app --to b
   ```
3. 命令自动完成机密门禁扫描、三端会话完整性检查、`secretstream` 流式加密、`package.enc` 与签名 `ready.json` 上传及远端复核。看到终端（或网页接力页横幅）输出 **「已交棒，可以关机」** 后，即可直接关闭设备 A。

#### 2. 到达设备 B：对代理说「接棒」（或在网页「待接棒」标签页点击接收）

1. 查看待接棒交接包与解密后的任务预览：
   ```bash
   relay inbox
   ```
2. 验签解包并恢复完整工作区（提交、staged 暂存区、unstaged 改动、未跟踪文件与历史对话归档）：
   ```bash
   # 首次在本机接收该项目时用 --path 指定本地目录：
   relay receive <交接编号> --path /path/to/my-app

   # 后续再次接收直接运行：
   relay receive <交接编号>

   # 若本机原目录已有未同步改动，原目录会被完整保留，可用 --into 安全接到新目录：
   relay receive <交接编号> --into /path/to/my-app-incoming
   ```
3. 按需查阅跨设备同步过来的 **Claude Code、Codex、Antigravity 完整历史对话全文**：
   ```bash
   relay transcripts my-app                        # 列出全部已归档历史会话
   relay transcripts my-app --session <会话ID>      # 阅读某条会话完整对话 Markdown
   relay transcripts my-app --tool anti            # 按代理工具筛选
   relay transcripts my-app --grep "鉴权逻辑"       # 跨会话关键词全文搜索
   ```
   当设备 B 完成新开发再 `relay send --to a` 交棒回设备 A 时，旧会话按 `(tool, sessionId, contentVersion)` 自动去重、绝不产生重复条目，新增回合与新会话无缝并入归档。

---

### 方式二：手动清单接力（无需云配置，完全离线可用）

如果不配置 `PyNaCl` / `rclone`，可以通过设置中转目录（`relay device --drop <目录>`）和可选同步盘台账（`relay init --ledger <路径>`）使用传统四模式接力：

- **四种代码接力传输方式**：
  - `remote`：将未提交改动提交到 `wip/handoff-<tag>` 个人分支后由用户确认推送，不动主分支、不使用 `--force`。
  - `bundle`：把分支与未提交改动打包成单个 `.bundle` 文件放入中转目录——无远程仓库或不便推送个人分支的团队仓库首选。
  - `patch`：当两台设备停在同一基底提交时，导出二进制安全的 `git diff HEAD` 补丁。
  - `folder`：非 Git 目录通过 `robocopy`（Windows）/ `rsync`（macOS/Linux）经中转目录同步，默认排除 `.env`。
- **流程**：出发端运行 `relay handoff <项目>` → 逐项 `relay check <项目> pre <key>` → `relay depart <项目>`；到达端运行 `relay pickup <项目>` → 逐项执行命令块 → `relay arrive <项目>`。
- **回到原设备恢复原本地会话**：代码反向同步回原设备后，可在原机原样恢复本地会话：
  - **Claude Code**：`claude --resume <id>`（或 `claude --continue`）
  - **Codex**：`codex resume <id>`（或 `codex resume --last`）
  - **Antigravity**：`agy --conversation <id>`（或 `agy --continue`，或从桌面端 / IDE 左侧会话列表打开）

---

## 命令速查

`PROJECT` 省略时自动匹配当前工作目录所属的已登记项目（自动兼容 macOS `/var` ↔ `/private/var` 等符号链接路径及 NFC/NFD 中文路径）。

| 命令 | 作用 |
|---|---|
| `init [--device a\|b] [--ledger PATH] [--host]` | 写入本机配置 `~/.relay-desk/config.json`；`--host` 记录本机主机名。台账不存在时自动创建。 |
| `cloud remotes [--rclone-config PATH] [--json]` | 列出本机 `rclone` 已配置的远端存储连接名称。 |
| `pair [--remote R] [--root P] [--provider rclone\|local] [--rclone-config PATH] [--accept] [--status] [--json]` | 查看云配对状态、发布本机 Ed25519/X25519 公钥、核验 24 位共享配对指纹并确认绑定（`--accept`）。 |
| `send [PROJECT] [--to a\|b] [--json]` | 执行机密门禁与会话缺口检查，端到端加密打包并上传云端，远端复核通过后输出 **「已交棒，可以关机」**。 |
| `inbox [--json]` | 同步对端签收回执，并列出当前发往本机、可安全领取的加密交接包预览。 |
| `receive RELAY_ID [--path DIR] [--into DIR] [--json]` | 在临时目录验签解密并恢复项目（首次用 `--path`；原目录有改动时保留原目录并支持 `--into` 接到新目录）及回写签收回执。 |
| `transcripts [PROJECT] [--session ID] [--tool claude\|codex\|anti] [--grep PATTERN] [--json]` | 列出、全文阅读或按关键词搜索跨设备归档的三端完整可见历史对话。 |
| `status [PROJECT] [--json]` | 无参数：每行一个项目概览。带项目名：完整详情、四级分层看板、已登记 Session 与恢复命令。 |
| `scan [PROJECT] [--all]` | 刷新本机分支、未提交数、远程、状态文件存在性与 Session 信息。`--all` 扫描全部项目并刷新全局状态。 |
| `add NAME --path PATH [--tool T] [--id ID]` | 登记项目并立即扫描（`T` 支持 `claude`、`codex`、`anti`、`both`、`claude+anti`、`codex+anti`、`all`）。 |
| `set PROJECT [...]` | 修改项目字段（`--path-here`、`--path-other`、`--alt-path`、`--tool`、`--status`、`--transport`、`--worktree`、`--device`）。 |
| `note PROJECT [...]` | 记录最近任务（`--last`）、下一步（`--next`）、备注（`--note`）与验证结果（`--verify`、`--verify-cmd`、`--verify-note`）。 |
| `sessions [PROJECT] [--tool claude\|codex\|anti] [--json]` | 列出本机发现的该项目本地 Session；已登记的标 `*`。 |
| `session add PROJECT ID\|latest [--tool T] [...]` | 登记一个 Session，`latest` 自动选取该工具最新的一条会话。 |
| `session rm PROJECT ID` | 取消登记指定的 Session。 |
| `handoff PROJECT [--transport T] [--to D]` | 发起手动清单接力，打印离开清单与跨系统命令块。 |
| `plan PROJECT [--phase pre\|post] [--json]` | 重新打印当前手动接力的离开（`pre`）或到达（`post`）清单与命令。 |
| `check PROJECT pre\|post KEY [--undo]` | 勾选或取消勾选一条手动接力清单项。 |
| `depart PROJECT` | 将手动接力标记为在途（`transit`）。 |
| `pickup PROJECT` | 到达接收设备后打印手动交接摘要、原设备 Session 与到达清单。 |
| `arrive PROJECT` | 完成手动接力，写入历史（保留最近 10 条），切换项目所在设备并重新扫描。 |
| `cancel PROJECT` | 取消当前进行中的接力标记。 |
| `conflict add PROJECT --kind K [...]` | 记录一条冲突（`both`、`merge`、`instr`、`session`、`other`）。 |
| `device [--which a\|b] [--name N] [--os O] [--drop PATH]` | 查看或修改两台设备的名称、操作系统（`windows` / `mac` / `linux`）与中转目录。 |
| `export [FILE]` / `import FILE [--replace]` | 导出或导入台账 JSON 备份（默认按项目 `updatedAt` 新旧合并，`--replace` 整体替换）。 |
| `serve [--port 8765] [--no-open]` | 启动仅绑定 `127.0.0.1` 的本机网页工作台服务（含「待接棒」、云配对与历史对话全文查阅器）。 |
| `install-skills [--claude] [--codex] [--anti] [--dry-run] [--force]` | 安装三个代理的 skill，自动替换适配本机的调用命令。 |

---

## 安全与边界承诺

1. **绝不代推业务 Git 远程**：无论云接力还是手动模式，代理与 `relay` 都绝不执行 `git push` 改动你的代码远程仓库；云接力仅向你配对授权的私有云存储目录上传端到端加密包。
2. **绝不读取或上传凭据**：`~/.claude.json`、`.credentials.json`、`~/.codex/auth.json`、`~/.gemini/oauth_creds.json`、`.env` 和私钥在打包时自动排除（不打开读取）；若检测到 Git 提交历史或暂存区误含私钥或凭据文件，`relay send` 在上传前直接拒绝。
3. **诚实区分摘要、归档对话与原生 Session**：`HANDOFF.md` 是交接摘要，`relay transcripts` 是跨设备只读历史对话归档——它们让新设备上的新会话完整掌握前因后果，但绝不冒充「恢复了原设备的原生活动 Session」，也不跨机覆盖本地 SQLite 会话数据库。
4. **先验签校验、再落盘且绝不覆盖脏目录**：接收端先在隔离临时目录完成 Ed25519 验签、`secretstream` 解密与文件清单 SHA-256 校验；篡改包或半成品包一律丢弃，且当目标目录存在未同步改动时完整保留原目录。
5. **本机回环安全防护**：`relay serve` 仅监听 `127.0.0.1`，强制校验 `Host`、`Origin` 与 `X-Relay-Desk: 1` 请求头，不开放任意文件读取。

---

## 目录结构

```
relay-desk/
├── relay.py                 # 跨平台命令行入口
├── relay / relay.cmd        # macOS/Linux 与 Windows 快捷包装脚本
├── relay-desk.html          # 单文件网页工作台（同时兼容作为 claude.ai Artifact 发布）
├── relaydesk/
│   ├── ledger.py            # 台账原子读写、文件锁、跨平台路径规范化与设备识别
│   ├── scan.py              # 只读扫描：Git 事实、状态文件存在性、三大代理 Session 发现
│   ├── plan.py              # 四级分层计算、手动交接清单与跨系统 Shell 命令生成
│   ├── storage.py           # 可插拔云存储接口 StorageProvider 与 rclone / 本地目录适配器
│   ├── crypto.py            # 设备身份、配对指纹、Ed25519 签名与 libsodium secretstream 加密
│   ├── conversations.py     # 三大代理完整可见对话提取、缺口检测、版本去重归档与检索
│   ├── packager.py          # Git bundle + 暂存/未暂存/未跟踪打包、机密门禁与脏目录保护解包
│   ├── relay_engine.py      # 云端异步接力编排器（配对、两阶段发送、收件箱、恢复与回执同步）
│   ├── cli.py               # 命令行子命令与 Skill 安装器
│   └── server.py            # 127.0.0.1 本机网页与 JSON API 服务
├── skills/
│   ├── claude/relay-desk/   # Claude Code 专用 SKILL.md 模板
│   ├── codex/relay-desk/    # Codex 专用 SKILL.md 模板
│   └── anti/relay-desk/     # Antigravity 专用 SKILL.md 模板
├── docs/
│   ├── SPEC.md              # 架构与模块接口规格说明
│   └── STORAGE_PROVIDER.md  # 可换云存储接口规范与契约测试指南
└── tests/                   # 基于标准库 unittest 的单元与端到端验收测试集
```

## 运行测试

所有测试均在隔离的临时目录中运行，不会触碰真实业务仓库：

```bash
# macOS / Linux
python3 -m unittest discover -s tests -v

# Windows
py -3 -m unittest discover -s tests -v
```

## 开源协议

基于 [MIT License](./LICENSE) 发布。
