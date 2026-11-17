# relay-desk 规格（v1）

双机接力台账：同一份台账让 **Claude Code、Codex、Antigravity 和网页** 四方共用。本文件是实现的唯一依据；模块之间只按这里写明的接口互相调用。

## 1. 原则（所有模块都必须遵守）

1. **文件为准**。台账是磁盘上的一个 JSON 文件（`ledger.json`）。Claude Code、Codex 通过命令行 `relay` 读写它；网页通过本机服务读写同一个文件。
2. **不碰远程**。任何代码都不得执行 `git push`、`git fetch`、`git pull` 或其他访问网络的命令。命令行只**打印**接力命令，由人或代理在自己的权限下执行。
3. **不读凭据**。`~/.claude.json`、`.credentials.json`、`~/.codex/auth.json`、`.env`、私钥：只允许检查是否存在，绝不打开读取、绝不复制、绝不写进台账。
4. **摘要不是 Session**。任何输出都不得把 HANDOFF.md 或台账记录说成「恢复了 Session」。本地 Session 只在原设备能原样恢复。
5. **只写台账**。`relay` 唯一会写的文件是 `ledger.json`、它的锁文件、本机配置 `~/.relay-desk/config.json`，以及 `install-skills` 明确要装的 skill 目录。绝不改动任何项目仓库的文件或 Git 状态。
6. Python **3.9 兼容**，只用标准库。不用 `match`、不用运行时的 `X | Y` 类型写法、不用 `tomllib`。源码 UTF-8。
7. 面向用户的输出一律**简体中文**；标识符和注释用英文。

## 2. 目录与归属

```
relay-desk/
  relay.py                 入口：把本目录加入 sys.path 后调用 relaydesk.cli.main()      [A]
  relay.cmd / relay        Windows / POSIX 包装脚本                                    [E]
  relay-desk.html          网页（同时是 claude.ai Artifact 的源文件，不带 doctype/head/body）  [D 只改 Store 部分]
  ledger.json              台账数据（运行时生成，不属于任何代理）
  README.md                                                                              [E]
  relaydesk/__init__.py    空文件                                                        [A]
  relaydesk/ledger.py      读写、规范化、本机配置                                        [A]
  relaydesk/cli.py         命令行                                                        [A]
  relaydesk/scan.py        本机事实扫描：git、状态文件、Session 发现                     [B]
  relaydesk/plan.py        分级、清单与命令生成（从网页 JS 移植）                        [C]
  relaydesk/server.py      本机 HTTP 服务：网页 + JSON API                               [D]
  tests/test_ledger.py, tests/test_cli.py                                               [A]
  tests/test_scan.py                                                                     [B]
  tests/test_plan.py                                                                     [C]
  tests/test_server.py                                                                   [D]
  skills/claude/relay-desk/SKILL.md                                                      [E]
  skills/codex/relay-desk/SKILL.md                                                       [F]
  skills/anti/relay-desk/SKILL.md                                                        [G]
  docs/SPEC.md             本文件（只读）
```

方括号是负责的代理。**只写自己名下的文件**；别人的文件按本规格的接口假定它存在，不要自己造一份。

## 3. 台账结构（`ledger.json`）

```jsonc
{
  "app": "relaydesk", "version": 1,
  "config": {
    "devices": [                      // 恰好两台
      { "id": "a", "name": "Windows · PC", "os": "windows", "drop": "", "host": "WORKSTATION" },
      { "id": "b", "name": "Mac", "os": "mac", "drop": "", "host": "" }
    ],                                // os: windows|mac|linux；drop: 中转目录；host: 主机名（可空）
    "reg": { "<kindId>": { "on": { "a": true }, "note": "" } }   // 每台设备上的全局状态
  },
  "projects": [ {
    "id": "animal-sim",               // [A-Za-z0-9_.-]+，唯一
    "name": "animal-sim",
    "device": "a",                    // 最新工作现场在哪台设备
    "paths": { "a": "D:\\workspace\\animal-sim", "b": "" },
    "altPaths": { "a": ["C:\\...\\codex-workspace"] },          // 可选：同一项目的其他启动目录，只用于发现 Session
    "branch": "main",                 // 空串 = 不是 Git 仓库
    "worktree": "", "remote": "",     // remote 空串 = 没有远程
    "tool": "claude",                 // claude|codex|anti|both|claude+anti|codex+anti|all
    "status": "active",               // active|paused|blocked|done
    "dirty": 2, "unpushed": null,     // unpushed: 整数或 null（未知/无上游）
    "lastTask": "", "nextAction": "", "note": "",
    "verify": { "state": "none", "cmd": "", "note": "" },       // state: none|pass|partial|fail
    "transport": "",                  // ''|remote|bundle|patch|folder
    "reg": { "<kindId>": { "on": { "a": true }, "note": "" } }, // 项目内状态存在于哪台设备
    "sessions": [ { "id": "s_xxxx", "tool": "claude", "device": "a", "kind": "local",   // kind: local|cloud|remote
                    "carry": "summary",                         // summary|transcript
                    "title": "", "sessionId": "", "cwd": "", "lastUsed": "2026-11-17", "note": "" } ],
    "conflicts": [ { "id": "c_xxxx", "kind": "both", "date": "2026-11-17", "device": "a", "kept": "尚未解决", "files": "", "resolution": "" } ],
    "history": [ { "tag": "20261117-win", "from": "a", "to": "b", "transport": "bundle", "doneAt": "ISO" } ],   // 最多保留 10 条
    "handoff": null,                  // 或 { tag, from, to, transport, state: "prep"|"transit", pre: {key:true}, post: {key:true}, hadDirty, branch, baseCommit, createdAt }
    "git": { "head": "", "lastCommit": "", "worktrees": 1, "scannedAt": "ISO", "scannedOn": "a" },   // 可选，scan 写入
    "updatedAt": "ISO", "updatedOn": "a"
  } ]
}
```

- 未知字段必须**原样保留**（读入、修改、写回时不丢）。
- kindId 的全集、分级和说明以 `relay-desk.html` 里的 `KINDS` 数组为准。
- 规范化规则与网页里的 `normConfig` / `normProject` 一致（缺字段补默认值；`device` 不在两台设备里时取第一台）。

## 4. 本机配置与设备识别

`~/.relay-desk/config.json`（每台设备各一份，不同步）：`{ "ledger": "<ledger.json 的绝对路径>", "device": "a" }`。

- 环境变量优先：`RELAY_DESK_HOME`（替代 `~/.relay-desk`，测试用）、`RELAY_DESK_LEDGER`、`RELAY_DESK_DEVICE`。
- 没有配置时：台账路径默认是工具目录下的 `ledger.json`；设备按 `devices[].host` 等于本机主机名（不分大小写）识别，其次按 `os` 唯一匹配，都不行则为 `None`（需要设备的命令报错并提示先 `relay init`）。

## 5. 模块接口

### 5.1 `relaydesk/ledger.py`  [A]

```python
APP = "relaydesk"; VERSION = 1
def tool_dir() -> Path                       # relay.py 所在目录
def default_config() -> dict
def norm_config(c) -> dict
def norm_project(p, config) -> dict
def load(path) -> dict                       # 文件不存在 → 空台账（default_config，projects=[]）；返回的台账已规范化
def save(path, ledger) -> None               # 原子写：同目录临时文件 + os.replace；UTF-8，ensure_ascii=False，indent=1
def update(path, fn) -> dict                 # 加锁 → load → fn(ledger)（就地修改）→ save → 解锁；返回保存后的台账。所有写入都必须走它
def rev(path) -> str                         # 变更标记："<mtime_ns>-<size>"；文件不存在返回 "0"
def find_project(ledger, key=None, cwd=None) -> dict   # 依次按 id、名称（不分大小写）、唯一前缀匹配；key 为空时取路径包含 cwd 的项目；失败抛 LookupError(中文说明)
def dev(config, dev_id) -> dict
def other(config, dev_id) -> dict
def local_dir() -> Path                      # RELAY_DESK_HOME 或 ~/.relay-desk
def load_local() -> dict
def save_local(d) -> None
def ledger_path(local=None) -> Path
def here(ledger, local=None) -> "str | None" # 本机设备 id
def touch(project, here_id) -> None          # 写 updatedAt / updatedOn
def new_id(prefix) -> str                    # prefix + 8 位小写字母数字
```

锁：`<ledger>.lock`，用 `os.open(..., O_CREAT|O_EXCL)` 创建；等待最多 5 秒；锁文件超过 30 秒视为残留并清除。

### 5.2 `relaydesk/scan.py`  [B]

全部只读；不联网；`home` 参数默认为用户主目录（测试时传临时目录）。

```python
def enc_path(p: str) -> str                  # 把非 [A-Za-z0-9] 的字符逐个换成 '-'（Claude Code 的项目目录编码）
def git_facts(path: str) -> dict
    # {"isGit": bool, "branch": str, "dirty": int, "unpushed": int|None, "remote": str, "remotes": [str],
    #  "worktrees": [str], "head": str(短哈希), "lastCommit": "<短哈希>（YYYY-MM-DD）：<标题>"}
    # 不是仓库或目录不存在：isGit=False，其余取空值。remote 取 "origin"（若存在），否则第一个远程，否则 ""。
    # unpushed：`git rev-list --count @{u}..HEAD`；没有上游 → None。分离头指针时 branch 取 ""。
def presence(path: str, home=None) -> dict   # 项目内状态是否存在，键为 kindId：
    # claudeMd, claudeLocal, ccSkills, ccSettings, ccSettingsLocal, ccCmdAgents(commands 或 agents 目录), ccMemory(~/.claude/projects/<enc>/memory 非空),
    # mcpJson, agentsMd, cxProject(.codex 目录), handoffMd, worktree(git worktree 数 > 1), envFile(.env 存在；只检查存在)
def global_presence(home=None) -> dict       # acctSkills, userSkills(skills 下除 synced 外有内容), userClaudeMd, userSettings, ccPlugins, ccProjects,
    # ccCreds(~/.claude.json 存在；只检查存在), cxAgents, cxConfig, cxSkills(skills 下除 .system 外有内容), cxSessions, cxAuth(只检查存在), cxState(*.sqlite)
def claude_sessions(cwd_paths: list, home=None) -> list
    # 对每个 cwd：读 ~/.claude/projects/<enc_path(cwd)>/*.jsonl
    # 每项 {"tool":"claude","sessionId":<文件名去扩展名>,"title":str,"cwd":cwd,"lastUsed":"YYYY-MM-DD","mtime":float}
    # title：文件里最后出现的 "customTitle"、"aiTitle"、"summary" 字段值（按此优先级）；找不到为 ""。大文件要流式读，不要整个载入内存。
def codex_sessions(cwd_paths: list, home=None) -> list
    # ~/.codex/sessions/YYYY/MM/DD/rollout-*-<uuid>.jsonl；文件开头几行里有 "cwd":"..."；
    # 标题来自 ~/.codex/session_index.jsonl（每行 {"id","thread_name","updated_at"}），同一 id 取最后一行。
    # 只返回 cwd 等于或位于 cwd_paths 某一项之下的 Session（Windows 上比较时不分大小写、统一分隔符）。
    # 每项 {"tool":"codex","sessionId":<uuid>,"title":str,"cwd":str,"lastUsed":"YYYY-MM-DD","mtime":float}
```

两个 `*_sessions` 的结果按 `mtime` 从新到旧排序。

### 5.3 `relaydesk/plan.py`  [C]

`relay-desk.html` 里这些 JS 函数的**逐字移植**（中文文案、命令文本、顺序都必须一致）：`TIER`、`TRANSPORT`、`VERIFY`、`tierBoard`、`sessionTiers`、`resumeLines`、`defaultTransport`、`planFor`，以及它们依赖的 `isWin`、`shellOf`、`osSlug`、`joinP`、`encPath`、`encOf`、`homeP`、`fileSlug`、`cpFile`、`mkDir`、`cpDir`、`guard`、`regOn`。入参都是已规范化的 dict，纯函数，不做 I/O。

```python
def tier_board(project, config) -> dict      # {"native": [[标题, 说明, fixed?], ...], "git": [...], "extra": [...], "manual": [...]}
def session_tiers(session, config) -> list   # [[设备名, tier, 说明], ...]
def resume_lines(project, session) -> list   # 元素是 str（命令）或 {"n": 说明}
def default_transport(project) -> str
def new_handoff(project, config, transport=None, today=None) -> dict
    # 与 JS 的 ACT.startHandoff 一致：tag = YYYYMMDD + "-" + osSlug(from)；state="prep"；pre/post 空；hadDirty；branch；baseCommit=""；createdAt
    # transport 不可用时的回退规则同 JS；today 为 date，默认今天
def plan_for(project, config) -> dict
    # project["handoff"] 必须存在。返回 {"F","T","miss","pre","post","preChecks","postChecks","tr","dirty","WIP","BR"}
    # pre / post：[{"title","kind":"read"|"local"|"remote","where","lines":[str | {"n": str}]}]
    # preChecks / postChecks：[[key, 文案, 补充说明], ...]
def render(plan, phase, checked=None) -> str # phase: "pre"|"post"。纯文本，给终端和代理看：
    # 先是清单，每行 "[x] key  文案" 或 "[ ] key  文案"，补充说明缩进另起一行；
    # 然后每个命令块：一行 "--- 标题 [只读|只改本机|写入远程，由用户确认后执行] where ---"，随后逐行命令；{"n": ...} 输出为 "# 说明"。
```

对拍样例（Windows `a` → Mac `b`，tag `20261117-win`，Mac 路径和中转目录未填，Windows 中转目录 `D:\handoff`）：

animal-sim，`transport=bundle`，`hadDirty=true`，分支 `main`，无远程，路径 `D:\workspace\animal-sim`。`pre` 里标题为「打成 bundle 文件」的块，`lines` 中的命令依次是：
```
cd "D:\workspace\animal-sim"
git switch -c wip/handoff-20261117-win
if ($?) {
  git add -A
  git commit -m "WIP handoff 20261117-win (do not merge)"
  git switch main
}
git bundle create "D:\handoff\animal-sim-20261117-win.bundle" main wip/handoff-20261117-win
git bundle verify "D:\handoff\animal-sim-20261117-win.bundle"
```
`post` 里「从 bundle 取回」块的命令依次是：
```
cd "<Mac 上的项目路径>"
git bundle verify "<Mac 的中转目录>/animal-sim-20261117-win.bundle"
git fetch "<Mac 的中转目录>/animal-sim-20261117-win.bundle" "refs/heads/*:refs/remotes/handoff/*"
git switch main && {
  git merge --ff-only "handoff/wip/handoff-20261117-win~1" && {
    git cherry-pick -n handoff/wip/handoff-20261117-win && git reset -q
  }
}
git status --short --branch
```
Scala cw，`transport=remote`，`hadDirty=true`，分支 `main`，远程 `origin`，路径 `D:\workspace\Scala cw\repo`。`pre` 里「推送这条 WIP 分支」块（`kind=remote`）的命令是 `cd "D:\workspace\Scala cw\repo"` 和 `git push -u origin wip/handoff-20261117-win`；`post` 里「取回代码和未提交改动」块的命令依次是：
```
cd "<Mac 上的项目路径>"
git fetch origin
git switch main && {
  git merge --ff-only "origin/wip/handoff-20261117-win~1" && {
    git cherry-pick -n origin/wip/handoff-20261117-win && git reset -q
  }
}
git status --short --branch
```
（`git fetch` 等只出现在**打印的文本**里，代码本身从不执行它们。）

### 5.4 `relaydesk/server.py`  [D]

```python
def serve(html_path, store, port=8765, open_browser=True) -> None   # 阻塞运行；只绑定 127.0.0.1
def make_server(html_path, store, port=0) -> http.server.HTTPServer # 测试用：返回未启动的 server，port=0 取随机端口
```

`store` 是鸭子类型对象（由 cli 提供，测试里用内存假对象）：

```python
store.read() -> (ledger: dict, rev: str)
store.update(fn) -> (ledger: dict, rev: str)     # fn 就地修改台账
```

路由：

| 方法 路径 | 行为 |
|---|---|
| `GET /` | 返回完整 HTML：`<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><style>:root{color-scheme:light}body{margin:0;font:14px system-ui,sans-serif}img{max-width:100%}[hidden]{display:none!important}</style></head><body>` + `relay-desk.html` 的内容 + `</body></html>`。每次请求都重新读文件。 |
| `GET /api/ledger` | `{"rev","config","projects"}`。带 `?rev=<当前 rev>` 且未变化时返回 `304`。 |
| `PUT /api/projects/<id>` | 请求体是项目对象（不含 id 也可以）；整条替换或新建。返回 `{"rev"}`。 |
| `DELETE /api/projects/<id>` | 删除。返回 `{"rev"}`。 |
| `PUT /api/config` | 替换 `config`。返回 `{"rev"}`。 |
| `PUT /api/ledger` | 请求体 `{"config","projects"}`，整体替换。返回 `{"rev"}`。 |

安全（必须全部实现并有测试）：`Host` 头的主机部分必须是 `127.0.0.1` 或 `localhost`，否则 `403`；所有非 GET 请求必须带 `X-Relay-Desk: 1`，且 `Origin` 头若存在必须是本服务自己的源，否则 `403`；不发送任何 `Access-Control-Allow-*` 头；请求体上限 2 MiB；`<id>` 必须匹配 `[A-Za-z0-9_.-]{1,80}`；除上表以外的路径一律 `404`（不提供任意文件读取）。响应带 `Cache-Control: no-store`。

### 5.5 网页的文件模式  [D]

`relay-desk.html` 的 `Store` 现有两种后端（claude.ai 的 `db`、浏览器 `localStorage`）。增加第三种，**只动 `Store` 及与之直接相关的少量代码**（`renderChrome` 里的状态文字、设置页里「数据存在哪里」的说明），其余不改：

- `Store.init()` 的判定顺序：`window.claude.use('db')` 成功 → 云端；否则若页面由 `http://127.0.0.1` 或 `http://localhost` 提供且 `GET /api/ledger` 在 2 秒内成功 → `S.mode = 'file'`；否则本地模式。
- 文件模式下 `saveProject` / `deleteProject` / `saveConfig` / `replaceAll` 调用上表对应接口（同一文档的写入串行，沿用现有 `serial`）。
- 每 3 秒用 `?rev=` 轮询；页面不可见时暂停；内容变化时更新 `S.projects` / `S.config` 并 `renderAll()`。自己刚写入得到的 `rev` 不触发重复渲染。
- 状态徽标文字：`文件模式，Claude Code 和 Codex 共用这份台账`。
- 文件不带 doctype/html/head/body（发布到 claude.ai 时由平台包裹），保持这一点；云端模式和本地模式的行为不得改变。

## 6. 命令行  [A]

调用方式：Windows `py -3 relay.py …`，macOS/Linux `python3 relay.py …`。`PROJECT` 省略时取当前目录所在的项目。

| 命令 | 作用 |
|---|---|
| `init [--device a\|b] [--ledger PATH] [--host]` | 写本机配置。`--host` 把本机主机名记到该设备的 `host`。台账文件不存在时创建空台账。 |
| `status [PROJECT] [--json]` | 无参数：每个项目一行（所在设备、分支、未提交数、状态、是否交接中、下一步）。带项目：详情 + 四级分层（`plan.tier_board`）+ 已登记 Session 及恢复命令（`plan.resume_lines`）。 |
| `scan [PROJECT] [--all]` | 用 `scan` 模块刷新本机事实。`branch/remote/dirty/unpushed/git` 只在 `project.device == 本机` 时更新；`reg[*].on[本机]`（只动 `scan.presence` 返回的键）和本机已登记 Session 的 `title/lastUsed` 总是更新。不带参数时扫当前目录所在项目；`--all` 扫全部并更新 `config.reg`（`scan.global_presence`）。已有的 `remote` 文字若首个词是真实存在的远程名则保留。打印发生了哪些变化。 |
| `add NAME --path PATH [--tool T] [--id ID]` | 登记项目（本机路径），随即 scan。 |
| `set PROJECT [--path-here P] [--path-other P] [--alt-path P]… [--tool T] [--status S] [--transport T] [--worktree P] [--device D]` | 改字段。 |
| `note PROJECT [--last T] [--next T] [--note T] [--verify S] [--verify-cmd C] [--verify-note T]` | 记最近任务、下一步、验证结果、交接备注。 |
| `sessions [PROJECT] [--tool T] [--json]` | 列出本机发现的 Session（`paths[本机]` + `altPaths[本机]`），已登记的标 `*`。 |
| `session add PROJECT ID\|latest [--tool T] [--kind K] [--title T] [--carry C] [--cwd P]` | 登记 Session。`latest` 取该工具最新的一个；`title/cwd/lastUsed` 默认取自发现结果。同一 `sessionId` 已存在则更新。 |
| `session rm PROJECT ID` | 取消登记。 |
| `handoff PROJECT [--transport T] [--to D]` | 先 scan，再用 `plan.new_handoff` 建立接力（已在接力中则不重建），打印 `plan.render(…, "pre")`。要求 `project.device == 本机`。 |
| `plan PROJECT [--phase pre\|post] [--json]` | 重新打印清单与命令。 |
| `check PROJECT pre\|post KEY [--undo]` | 勾选或取消清单项。 |
| `depart PROJECT` | `handoff.state = "transit"`。 |
| `pickup PROJECT` | 打印上下文（最近任务、下一步、验证结果、交接备注、原设备上的 Session）和 `plan.render(…, "post")`，并明确写出：原 Session 留在原设备，这里是读摘要的新 Session。 |
| `arrive PROJECT` | 接力完成：写入 `history`（最多 10 条），`device = handoff.to`，`handoff = null`，随后 scan。 |
| `cancel PROJECT` | 取消接力。 |
| `conflict add PROJECT --kind K [--files F] [--resolution R] [--kept K]` | 记一条冲突。 |
| `device [--which a\|b] [--name N] [--os O] [--drop PATH]` | 查看或修改设备设置；不带修改项时打印两台设备。 |
| `export [FILE]` / `import FILE [--replace]` | 导出为网页「备份」同样的格式；导入默认按项目 id 合并（`updatedAt` 新的胜出），`--replace` 整体替换。 |
| `serve [--port 8765] [--no-open]` | 启动 `server.serve`，`store` 用 `ledger.load` / `ledger.update` / `ledger.rev` 封装。 |
| `install-skills [--claude] [--codex] [--anti] [--dry-run] [--force]` | 把 `skills/claude/relay-desk` 复制到 `~/.claude/skills/relay-desk`，`skills/codex/relay-desk` 复制到 `$CODEX_HOME`（默认 `~/.codex`）`/skills/relay-desk`；复制时把 `SKILL.md` 里的 `{{RELAY}}` 换成本机调用方式（Windows：`py -3 "<工具目录>\relay.py"`；其他：`python3 "<工具目录>/relay.py"`）。两个开关都不给时两个都装。目标已存在且内容不同时，没有 `--force` 就拒绝并说明。`--dry-run` 只打印将要写入的路径。 |

约定：成功退出码 0；用法或查找错误 1（说明写到 stderr）；台账读写错误 2。stdout 不是终端时用 UTF-8 输出（`reconfigure(encoding="utf-8", errors="replace")`），是终端时不改编码。调用 git 一律 `subprocess.run([...], capture_output=True, text=True, encoding="utf-8", errors="replace")`，带 `-c core.quotepath=false`。写操作结束后打印一行结果。

## 7. Skills  [E：Claude Code；F：Codex]

两个 `SKILL.md` 的 frontmatter 都只有 `name: relay-desk` 和 `description`。正文里调用命令行一律写 `{{RELAY}}`（安装时替换），例如 `{{RELAY}} status`。

口令：
- **「接力」**（离开这台设备）→ `{{RELAY}} handoff`；**「接棒」**（到了另一台设备）→ `{{RELAY}} pickup`；「台账」「接力状态」→ `{{RELAY}} status`。
- Claude Code 已有 handoff 插件占用「交接」「接手」，Claude 版 skill **不要**声明这两个词，只在正文里说明：handoff 插件负责写 HANDOFF.md 并提交推送，本 skill 负责台账和命令；没有远程或不该推送的项目用「接力」。
- Codex 没有对应插件，Codex 版 skill 的 description 同时声明「交接」「接手」「接力」「接棒」。

流程（两个 skill 一致，按各自工具的说法改写）：
- 离开：`handoff` → 把清单逐项做完：写或更新项目里的 `HANDOFF.md`（摘要），`note` 记下最近任务和下一步，`session add PROJECT latest --tool <本工具>` 登记当前 Session → 命令块按 `kind` 处理：**只读**的可以直接执行；**只改本机**的先给用户看再执行（遇到项目自己的规则更严时从严）；**写入远程**的绝不执行，原样交给用户 → 每完成一项 `check` → `depart`。
- 到达：`pickup` → 按 post 清单做 → 读 `HANDOFF.md` 和台账里的下一步 → `arrive`。告诉用户：原 Session 留在原设备，现在是读摘要的新 Session。
- 平时：开工先 `status`；收工 `note`；工作区有变化 `scan`。

三条硬规则必须写进两个 skill：不执行任何写入远程的命令；不读取、不复制凭据文件；不把摘要说成 Session 恢复。

补充：第 1 节「不碰远程」约束的是 `relay` 自己的代码。代理按清单执行命令块时，`git fetch` 只读取远程、不改动它，它出现在「只改本机」的块里就按那一类处理（先给用户看，同意后执行）；`HANDOFF.md` 只写文件，不替用户提交或推送。

## 8. 测试与交付

- 测试用 `unittest`，运行：`py -3 -m unittest discover -s tests -v`（在工具目录下）。测试只在 `tempfile` 临时目录里活动；需要 Git 仓库时在临时目录 `git init`（提交时用 `-c user.name=t -c user.email=t@example.invalid`）；用 `RELAY_DESK_HOME` 指向临时目录；**绝不**对 `D:\workspace` 下的真实仓库做任何写操作。
- 你的模块依赖别的代理的模块时，测试里用假对象替代，不要等它。
- 交付时报告：写了哪些文件；跑了哪些测试、原始结果摘要；与本规格不一致的地方及原因；你发现的规格漏洞。不要写规格之外的文档。
