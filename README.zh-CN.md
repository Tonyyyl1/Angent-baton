# relay-desk

[English](./README.md) | **简体中文** | [更新日志](./CHANGELOG.md)

relay-desk 用来在多台电脑之间接续开发中的 Git 项目或普通文件夹。可通过命令行、本机网页，或 Claude Code、Codex、Antigravity 的 skills 操作。手动清单支持离线转移。云接力会加密传输工作区改动，并归档受支持的对话记录，方便继续查阅。

## v3 有哪些变化

- **多设备接力。** 按设备名配对并发布签名快照。原设备离线后，已缓存快照的设备仍可转发。只有指定的接收设备可以领取活跃交接。
- **持续同步与恢复。** 用 `sync --watch` 按对端自动重试，用 `--jsonl` 查看逐条事件。接收中断后可根据本机事务日志继续。
- **换钥与本机维护。** 重新确认对端公钥后可重新投递缓存快照。用 `send --reissue` 重新签发在途交接，用 `doctor` 检查本机状态，用 `cache prune` 预览清理。

现有 A/B 环境可查看 [v2/v3 兼容矩阵](#v2--v3-长期共存与兼容矩阵)。升级前请备份本机状态，并按[迁移与恢复说明](./docs/MIGRATION.md)操作。

## 在多台设备上安装

安装 Python 3.9 或更新版本，以及 Git。分别在各设备拉取工具源码。

```sh
git clone https://github.com/Tonyyyl1/Angent-baton.git
cd Angent-baton
```

**设备 A · macOS/Linux**

```sh
python3 relay.py init --device a --host
python3 relay.py install-skills
```

**设备 B · Windows PowerShell**

```powershell
py -3 relay.py init --device b --host
py -3 relay.py install-skills
```

默认台账是工具目录内的 `ledger.json`。可以用 `init --ledger 路径` 指定本机台账位置。运行 `install-skills --dry-run` 可预览安装位置；用 `--claude`、`--codex` 或 `--anti` 可只安装单个工具的 skill。

启动本机网页工作台时，macOS/Linux 运行 `python3 relay.py serve`，Windows PowerShell 运行 `py -3 relay.py serve`。默认地址为 `http://127.0.0.1:8765/`。

## 手动清单接力

手动模式只需要 Python 标准库和 Git。登记项目后，按工具输出的离场和到达清单操作。

1. 发送端运行 `python3 relay.py add my-app --path /path/to/my-app` 和 `python3 relay.py handoff my-app --to b` (`handoff`)。
2. 完成离场清单后，运行 `python3 relay.py depart my-app`。
3. 接收端打开含有该交接记录的台账，运行 `py -3 relay.py pickup my-app` (`pickup`)。完成到达清单后运行 `py -3 relay.py arrive my-app`。

可通过共享台账或 `export FILE` / `import FILE` 同步记录。离线场景可在各设备配置 `device --drop PATH` 后使用 `bundle`、`patch` 或 `folder` 传输方式；`remote` 模式会打印供手动执行的 Git 命令。

## 加密云接力与 N 设备网状网络 (v3)

参与 v3 网络的设备需要包含 v3 的工具源码，并为运行 relay-desk 的 Python 安装 PyNaCl（`python3 -m pip install --user pynacl`，对应依赖 `pynacl`）。安装 `rclone` 后，在各设备用 `rclone config` 配置指向同一共享云目录的连接（`cloud remotes`）。

### 快照缓存与活跃交接分流

- **快照发布与同步（`publish`、`sync`、`sync --watch`）**：发布不可变、作者签名的快照到本地缓存与已配对 v3 节点，不转移活跃编辑所有权，也绝不改动任何工作区。即使原发布者离线，其他已配对设备运行 `sync` 时也会将本地已缓存的 head 快照转发给缺失该快照的 v3 节点。
- **活跃工作区交接（`send --to DEVICE`、`inbox`、`receive`）**：独占式活跃工作区转移（`purpose: "handoff"`，绑定 `authorizedDeviceId`）。即使非目标设备已缓存快照，也无法领取未授权给自己的交接；同一项目同时只允许存在一个未签收的在途交接。

项目有待发送或在途交接时，`publish` 会拒绝推进快照版本，本机后续编辑仍保留。`publish` 会先同步回执；对端已签收后，需要由当前拥有项目的设备继续发布。

`receive` 会自动取得并验证缺少的父版本签名元数据，三设备循环无需先手动运行 `sync`。缺少证据时报告 `lineage_incomplete`，保留工作区并等待恢复云端记录；不会将缺少祖先信息直接认定为分叉。`sync` 在报告分叉之前也做同样的事：取得并验证缺少的父版本（包括作者用已重新确认的旧密钥签名的父版本；旧密钥只作为历史证据，不因此获得任何其他权限）。找不到可验证的记录时，这一对版本记为「证据不足」（同步汇总中的 `lineageUnproven`、`relay doctor` 中的 `lineage_unproven`），与分叉分开，并在之后的同步中自动复核。

### 完整三设备演练步骤 (`a`、`b`、`c`)

1. **初始化设备（`init`、`device --json`）**

   除 `a`、`b`、`c` 外，也支持合法自定义别名（`[A-Za-z0-9_.-]{1,40}`，如 `mac-studio`）：

   ```sh
   # 分别在设备 A、B、C 上运行：
   python3 relay.py init --device a --host
   python3 relay.py init --device b --host
   python3 relay.py init --device c --host
   # 自定义别名示例：python3 relay.py init --device mac-studio --host

   # 查看设备身份、配对状态、公钥指纹及 v2/v3 能力：
   python3 relay.py device --json
   ```

2. **配置云存储并配对 N 设备网状网络（`cloud remotes`、`pair`）**

   ```sh
   # 检查本机 rclone 连接，并在各设备发布签名公钥描述符：
   python3 relay.py cloud remotes
   python3 relay.py pair --remote s3_remote --root relay-baton

   # 查看指定对端的配对状态与 24 位十六进制指纹：
   python3 relay.py pair --with b --status

   # 核对两端 24 位指纹完全一致后，确认配对：
   python3 relay.py pair --with b --accept --fingerprint <FP>
   # 依次完成网状配对（a <-> b、a <-> c、b <-> c）。
   # 若云端仅有一台已展示指纹的对端设备，也可简写为：
   python3 relay.py pair --accept

   # 如需轮换本机密钥（保留 deviceId；对端仅暂停该节点直到重新核对指纹）。
   # 有未完成的交接或接收时会被拒绝，见下文「换钥与重新投递」：
   python3 relay.py device --rotate-keys
   ```

3. **发布不可变快照与后台缓存同步（`publish`、`sync`）**

   ```sh
   # 在设备 A 登记项目并发布快照（A 继续保留活跃编辑权）：
   python3 relay.py add demo --path /path/to/demo-a --id demo
   python3 relay.py publish demo

   # 在设备 B 执行同步（仅缓存在本地，绝不改动工作区；
   # 即使 A 随后离线，B 执行 sync 也会将缓存快照转发给 C）：
   python3 relay.py sync
   python3 relay.py sync demo
   python3 relay.py sync --watch
   # 供脚本读取：每个事件一行 JSON，最后一行是汇总：
   python3 relay.py sync --watch --jsonl
   ```

   `sync` 的退出码：0 成功，3 部分失败（只剩可自动重试的问题），4 需要处理，130 已取消。详见下文「持续同步」。

4. **独占式活跃工作区环形交接（`send`、`inbox`、`receive`）**

   ```sh
   # 设备 A -> B（控制台输出「已交棒，可以关机」）：
   python3 relay.py send demo --to b

   # 设备 B 查看收件箱并领取交棒：
   python3 relay.py inbox
   python3 relay.py receive <RELAY_ID> --path /path/to/demo-b

   # 设备 B 编辑后交棒给 C；C 领取并编辑后交棒回 A：
   python3 relay.py send demo --to c
   # 在设备 C 上：
   python3 relay.py inbox
   python3 relay.py receive <RELAY_ID> --path /path/to/demo-c
   python3 relay.py send demo --to a
   # 在设备 A 上：
   python3 relay.py inbox
   python3 relay.py receive <RELAY_ID>
   ```

5. **事务性工作区崩溃恢复与冲突隔离（`recover`、`receive --into`）**

   工作区变更受 `<local_home>/transactions/<relay_id>/` 中带 SHA-256 校验和的事务日志保护：

   ```sh
   # 查看恢复事务状态，或安全回滚到接收前状态：
   python3 relay.py recover <RELAY_ID> --status
   python3 relay.py recover <RELAY_ID> --rollback

   # 当 receive 报告 target_dirty、version_fork 或 recovery_conflict 时，
   # 安全落盘到干净新目录，不改动现有工作区：
   python3 relay.py receive <RELAY_ID> --into /path/to/demo-clean
   ```

   文件复制先写入日志记录的临时目录，再在同一文件系统原子替换目标文件。复制中断时，重跑 `receive` 可继续；进程退出留下的临时文件不会被误判为本机编辑。

   回滚中断后，运行 `recover <RELAY_ID> --rollback` 继续回滚；此时 `receive` 不会签发回执。进入台账更新阶段后只能重跑 `receive` 完成接收，不能再回滚。恢复日志格式升级为 2，兼容读取旧格式 1；已有事务应使用当前修复版完成恢复，不要降级处理新日志。

   `sync` 转发会在本机 `sync_publications/` 保存原密文和签名标记，失败后继续运行 `sync` 将复用原投递。已发布包不会重新加密覆盖；这些恢复文件会占用本机额外空间。`relay cache prune` 只在接收方已用当前公钥确认缓存后才删除对应检查点（见「诊断与缓存清理」）。

   `relay doctor` 会列出未完成的事务，并给出应运行的 `recover` 或 `receive` 命令。

6. **查阅归档历史对话（`transcripts`）**

   ```sh
   python3 relay.py transcripts demo
   ```

   存储适配契约见 [StorageProvider 规范](./docs/STORAGE_PROVIDER.md)（`StorageProvider`），协议与状态机详见 [规格说明](./docs/SPEC.md)。

### 持续同步（`sync`、`sync --watch`）

`relay sync` 运行一轮。`relay sync --watch` 每隔 `--interval` 秒（默认 5）运行一轮，直到按 Ctrl+C、收到 SIGTERM 或达到 `--max-cycles N`。每轮都会只读地重新读取 `identity.json`、`cloud.json` 和 `relay_state.json`：持续同步运行期间换钥或重新配对，下一轮即生效；本机 `deviceId` 被替换时持续同步会停止。每轮只在本轮运行期间持有本机工作区锁。

每轮在事件发生时逐条产生事件：缓存快照、转发快照、同步回执、检测到分叉、记录或解除「父版本证据不足」、对端确认缓存、问题出现或解决、对端重试状态、信任配置变化、本机忙而跳过本轮。

| 输出参数 | 输出内容 |
| --- | --- |
| 不加 | 文字。单次同步输出汇总行、`同步状态：…`，以及每个未解决问题和对应的处理命令。持续同步每次有变化输出一行带时间的记录，停止时输出一行汇总。 |
| `--json` | 结束或中断时输出一个汇总对象（`schema: relaydesk.sync.summary/1`）。与 `--watch` 同用时也不逐轮输出。 |
| `--jsonl` | 每个事件一行 JSON（`schema: relaydesk.sync/1`，`seq` 连续）。最后一行是 `{"kind": "summary", "summary": {…}}`，其中的汇总对象与 `--json` 输出的相同。可与 `--watch` 同用。 |

`--json` 与 `--jsonl` 不能同用（用法错误，退出码 1）。`--jsonl` 的读取端关闭管道时（例如 `| head -1`），本次运行按取消处理。第一轮开始前出错时，`--json` 输出 `{"ok": false, "status", "code", "errorClass", "action"}`，`--jsonl` 输出一行 `"kind": "error"`。

| 状态 | 退出码 | 含义 |
| --- | --- | --- |
| `success` | 0 | 没有未解决的问题。 |
| `partial` | 3 | 只剩可自动重试的问题：暂时性传输失败或本机忙。 |
| `needs_action` | 4 | 至少一个问题需要你处理：配置、信任、本机状态损坏或完整性失败。第一轮前出现的此类错误（例如未配置云端、状态文件损坏）也返回 4。 |
| `cancelled` | 130 | Ctrl+C、SIGTERM 或 `--jsonl` 管道被关闭。仍会输出最后的汇总，并释放锁。 |

其他用法错误返回 1；第一轮前的台账或文件读写错误返回 2。汇总中的 `ok` 现在等于 `status == "success"`，以前总是 `true`。

错误分为四类（`errorClass`）：

| 类别 | 典型原因 | 处理方式 |
| --- | --- | --- |
| `transient_transport`（暂时性传输失败） | 网络或存储超时、连接被重置；刚列出的对象随即消失；读到对端正在改写的缓存清单 | 按上限退避加抖动自动重试。同一个未知异常连续 3 次会升级为 `needs_user_action`，缓存清单连续 3 次读到半个文件会升级为 `integrity`。 |
| `needs_user_action`（需要操作） | 未配置、对端换钥、发送方不受信任、云存储授权或配置错误、磁盘满、无权限、本机状态损坏、交接因换钥无法领取、本机缓存或归档目录是符号链接（`local_dir_symlink`） | 不循环重试。问题附带 `action` 处理命令。 |
| `integrity`（完整性失败） | 签名无效、摘要不符、标识符或路径非法（包括收到的载荷内的符号链接）、远端 JSON 损坏、回执与发送记录不符 | 按对象的列举标签（大小、etag、sha256、mtime）隔离，对象不变就不再下载；只报告一次。 |
| `local_busy`（本机忙） | 本机另一个发布、交棒、接棒或同步操作持有工作区锁；台账锁超时 | 跳过本轮，连续忙只提示一次。 |

退避按对端分别计算；收件箱无法列出时全局退避。间隔最长增加到 `--max-backoff` 秒（默认 300，不得小于 `--interval`），成功后恢复为 `--interval`。一个对端失败不会阻塞其他正常的对端。换钥的对端会被暂停，直到你重新确认。需要处理的全局问题（例如 `cloud.json` 损坏、云存储授权失败）不会结束持续同步：它每隔 `--max-backoff` 秒重新检查一次，修好后自动恢复。

`publish`、`send`、`receive` 等单次命令遇到暂时性读取失败时，会在命令内有限次重试读取：最多 4 次、12 秒，带抖动。不存在、鉴权、配置和完整性错误不重试，上传也不会盲目重复。`receive` 遇到同步轮次占用本机锁时最多等待 20 秒，超时仍报 `local_busy`。详见 [docs/STORAGE_PROVIDER.md](./docs/STORAGE_PROVIDER.md) §2.1.2。

汇总最多保留最近 200 条事件（`recentEvents`），`recentEventsTruncated` 和 `recentEventsDropped` 表示是否丢弃了更早的事件。累计计数 `counters` 单独保存，统计全部事件。旧的列表字段（`syncedReceipts`、`syncedSnapshots`、`forwarded`、`conflictsDetected`、`errors`，以及新增的 `lineageUnproven`）各自最多保留最近 200 条，被截断时由 `legacyListsTruncated` 标出。同一个问题只在首次出现或类别变化时报告，不会每轮重复。

`<RELAY_DESK_HOME>/sync_status.json`（`schema: relaydesk.sync.status/1`）保存最近一次运行的结果，以及 `running`、`heartbeatAt`、`pid`。持续同步有变化时最多每 10 秒写一次，没有变化时每 60 秒写一次心跳，结束时一定写入。超过 3 × max(间隔, 最长退避) 秒没有心跳即视为过期。`relay doctor` 和 `GET /api/cloud/status`（`lastSync` 字段）读取这个文件。

`relay serve` 提供持续同步接口：`POST /api/cloud/sync/watch`，参数 `{interval, maxBackoff, project}`（2 ≤ interval ≤ maxBackoff ≤ 3600）；`GET /api/cloud/sync/watch?since=SEQ` 返回序号 `SEQ` 之后的事件，`gap > 0` 表示有事件已被挤出 200 条缓冲；`POST /api/cloud/sync/watch/stop` 停止。持续同步运行期间，单次 `POST /api/cloud/sync` 返回 409 `watch_running`。云端接口的错误体都带 `errorClass`。网页「待接棒」页有「持续同步」开关，显示状态、计数、各对端状态、未解决问题及可复制的命令，以及最近 50 条事件。

`publish` 在 `deliveryErrors`（含 `errorClass` 和 `code`）与 `status` 中报告向各对端投递的失败。快照仍然算已发布（`ok: true`），下一次 `relay sync` 会继续向失败的设备投递。

两轮持续同步的实际输出：

```console
$ relay sync --watch --interval 1 --max-cycles 2
持续同步已启动：每 1 秒一轮，暂时失败时最长退避 300 秒（Ctrl+C 停止）。
[02:29:40] + 快照 snap_20261119_mqisvue1（项目 demo，作者 dev_dc3bb4d1dfc2e644）
[02:29:40] = 对端 dev_125cf833db8a0708 已确认缓存快照 snap_20261119_yymdp9nq
持续同步已停止（成功）：共 2 轮，缓存 1、转发 0、回执 0；重试 0 次；未解决 0 项
同步状态：成功
```

### 换钥与重新投递

`relay device --rotate-keys` 更换本机的签名密钥和加密密钥，`deviceId` 不变。被替换的公钥（从不保存旧私钥）记在 `identity.json` 的 `previousKeys` 中（最多 10 个）。

**换钥保护。** 本机有未完成的发送、尚未被领取也未被取代的交接、尚未上传的回执或未完成的接收事务时，换钥会被拒绝（退出码 4），并逐项列出原因和处理命令。加 `--force` 会照常换钥，并列出受影响的交接；对方重新确认本机新指纹后，这些交接都需要用 `relay send 项目 --to 设备 --reissue` 重新签发。

```console
$ relay device --rotate-keys
本机还有未完成的交接或恢复事务；现在换钥，这些交接包将无法被对方领取或验证：
  - 交接 r_20261119_3x5cwc5t 已发给 beta，等待对方领取并回传签收回执。
    处理命令：relay sync demo
请先按上面的命令处理完再换钥。确需立即换钥请加 --force，之后在途交接需要用 relay send <项目> --to <设备> --reissue 重新签发（原交接包保持不变）。
```

**重新确认。** 对端在运行 `relay pair` 时，或在 `sync` 中遇到签名失败、重新读取该设备的签名描述文件时，会发现新公钥，并把该设备标为 `key_changed`。只有这一条配对暂停：不能交棒，也不获得新的信任；其他配对照常工作。在两台设备的屏幕上核对新指纹，然后在每个对端运行 `relay pair --with 设备 --status` 和 `relay pair --with 设备 --accept --fingerprint FP`。确认后，此前确认过的公钥会追加到 `cloud.json` 中该对端的 `keyHistory`（最多 10 个）。没有重新确认的设备不会恢复任何信任；只在 `pendingDescriptor` 中出现过的公钥不会进入历史。

**按接收方公钥重新投递。** 缓存投递在 `deliveries.json` 中按（接收设备、接收方公钥指纹、快照）记录，投递编号也写明接收方公钥：`(sync|fwd)_<快照>_<发送方6位>_k<指纹前8位>[_gN]`。重新确认换钥后，接收方把封给自己旧公钥、或由发送方旧密钥签名的投递标为过期，并在缓存清单中再次请求该快照；持有该快照的发送方随后用新的投递编号和新密文投递。原投递、回执和加密包从不改写。以下情况也会产生新一代（`_gN`）：发送方自己换钥；或已发布超过 10 分钟仍未确认、云端对象缺失且本机没有检查点（本机有检查点时按原字节重新上传，不算新一代）。同一接收方公钥超过 4 代仍未确认时报告 `repeated_redelivery`（需要操作）。

**三种投递状态。** 以下三种状态分开记录：

| 状态 | 依据 |
| --- | --- |
| 已发布 | 本机为该接收方公钥发布的 ready 标记和加密包都在。 |
| 对端已验证缓存 | 接收方签名的缓存清单（用其当前固定公钥验签）列出了该快照。 |
| 已接棒 | 接收方签名的交接回执与发送记录逐项一致。 |

缓存清单永远不算接棒，也不会改变项目所在设备；回执也不代表其他快照已被缓存。

**签名缓存清单 v2。** `v3/have/<deviceId>.json` 新增 `haveFormat: 2`、`keyFingerprint`、递增的 `seq`，以及 `needs`：本机丢失或无法验证、需要补发的快照编号（最多 200 个）。清单只列出摘要已验证的缓存。由旧密钥签名、指纹不符或 `seq` 小于已见过的清单视为过期，忽略。内容不变的清单最多每 15 分钟重新上传一次。

**历史证据。** 版本图节点记录 `authorKeyFingerprint`、`trust`（`current`、`historical` 或 `attested`）和 `recordSha256`。由已重新确认的旧公钥签名的快照可作为 `historical` 证据用于缓存、转发和父链验证，但旧密钥不会因此获得新的权限：由旧密钥签名的 ready 标记、缓存清单和设备描述文件一律拒绝；含历史快照的交接只在快照作者本人发送时接受；由旧密钥签名的回执只对绑定该公钥的交接接受。

**重新签发在途交接。** 目标设备换钥（`handoff_recipient_key_changed`）或本机换钥（`handoff_sender_key_rotated`）后，普通的 `relay send` 会报出错误和应运行的命令，从不自动重新签发。`relay send 项目 --to 设备 --reissue` 为同一个签名快照生成新的交接编号和新密文，新 ready 标记带签名字段 `supersedesRelayId` 和 `supersedesRelayIds`。原交接包、ready 标记和任何回执都不变，原发送记录标记 `supersededBy`。

- 目标设备的收件箱只列出重新签发的交接；`inbox --json` 在 `superseded` 中列出原交接。
- 目标设备只能领取其中一个。领取原交接后再领取重新签发的交接，报 `superseded_original_claimed`；领取重新签发的交接后再领取原交接，报 `handoff_superseded`。
- 发送方换钥、而目标设备在重新确认之前已用旧固定公钥领取原交接时，以原交接的回执为准，重新签发的交接记为 `withdrawn`。也就是先领取的那个生效。
- 以下情况 `--reissue` 拒绝执行，且不写任何东西：`nothing_to_reissue`、`reissue_not_needed`、`handoff_already_claimed`、`handoff_already_superseded`、`reissue_receipt_exists`（先运行 `relay sync`）、`reissue_payload_missing`（本机缓存已没有完整可验证的快照载荷）、`reissue_not_supported`（旧版协议，或快照不是本机所写）。目标设备必须先被重新确认（`paired`）。
- `--reissue` 中断后再次运行，沿用同一个新交接编号。
- 目标设备必须运行本版本引擎。较早的 v3 引擎会拒绝重新签发的交接；这一失败方向是安全的，不会出现两份都被领取。

```console
$ relay doctor demo
relay doctor：本机 alpha（dev_5a2947f8a0798308）  状态：需要处理（需要处理 1，警告 0，提示 0）
项目 demo「演示项目」：当前编辑设备 设备 alpha（本机）；在途交接 r_20261119_3x5cwc5t：alpha → beta
  已确认缓存：—；等待确认：beta、gamma；待补发：—；未知：—
[需要处理] handoff_recipient_key_changed  项目 demo  交接 r_20261119_3x5cwc5t
  原因：交接 r_20261119_3x5cwc5t 是发给 beta 换钥前公钥的，对方已无法领取；原交接包保持不变，需要为它当前已重新确认的公钥重新签发（新交接编号、新密文）。
  处理命令：relay send demo --to beta --reissue

$ relay send demo --to beta --reissue
已重新签发交接：新交接 r_20261119_4o23zedg 取代 r_20261119_3x5cwc5t（原交接包和回执保持不变，对方只能领取新交接）。
交接包已上传并校验通过：演示项目（demo）
交接编号：r_20261119_4o23zedg
流向：alpha → beta    大小：1793 字节    归档会话：0 条
SHA-256：39ba1543f0c099254482605c305da11e32e6e63606d2748be863c177172bf030
已为目标设备当前公钥重新签发交接（取代 r_20261119_3x5cwc5t，原交接包保持不变）。已交棒，可以关机
```

### 诊断与缓存清理（`doctor`、`cache status`、`cache prune`）

**`relay doctor [项目] [--json]`** 严格只读：不加锁、不联网、不写任何文件。检查内容：

- 身份与配对（未配置、待确认、`key_changed`）；
- 各项目的当前编辑设备和在途交接；
- 中断的发送和重新签发、因换钥卡住的交接、尚未上传的回执；
- 恢复事务；
- `sync_status.json` 中的最近同步结果，包括心跳过期；
- 未解决的版本分叉、父版本证据不足的版本对、中断的清理、状态不明的缓存条目；
- 是符号链接的本机缓存或归档目录，以及台账中不符合 `projectId` 规则的项目 ID。

每条结果有严重度（`info`、`warning` 或 `error`）、稳定的 `code`、原因和恰好一条处理命令。命令行报错、API 和网页显示的是同一个原因和同一条命令。退出码：只有提示为 0，有警告为 3，有需要处理的项为 4，项目不存在为 1。每个项目下，每台已配对设备显示为「已确认缓存」「等待确认」「待补发」「未知」或「需要操作」之一。「未知」表示没有该设备最近一份经过验证的缓存清单，从不显示成离线或缺少快照。

```console
$ relay doctor
relay doctor：本机 alpha（dev_5a2947f8a0798308）  状态：正常（需要处理 0，警告 0，提示 0）
项目 demo「演示项目」：当前编辑设备 设备 alpha（本机）
  已确认缓存：beta、gamma；等待确认：—；待补发：—；未知：—
未发现需要处理的问题。
```

**`relay cache status [项目] [--json]`** 只读。统计快照载荷、签名元数据（含 `cache/lineage`）、密文检查点（`sync_publications/`）、恢复事务（从不清理）以及上次清理中断留下的目录，并显示按当前保留规则可以释放多少空间。

```console
$ relay cache status
本机缓存（/tmp/relay-demo/home_alpha）：
  快照载荷：3 个，9.9 KB（已验证 3，损坏或状态不明 0）；保留 1，可清理 2
  签名元数据：3 个，1.7 KB（其中谱系文件 0 个，始终保留）
  密文检查点：6 个，25.0 KB；保留 0，可清理 6
  恢复数据：0 个事务，0 字节（未完成 0，无法读取 0；从不清理）
预计可释放：31.6 KB（运行 relay cache prune --dry-run 查看明细）
```

**`relay cache prune [项目] [--dry-run | --apply] [--expect-plan 摘要] [--json]`** 默认只预览，不写任何文件。预览列出将删除的项目及原因、保留的项目及原因、因状态不明而保留的条目、预计释放的空间，以及计划摘要 `planDigest`。`--apply` 先取得本机工作区锁（被占用时退出码 3），在锁内重新读取全部输入并重新计算计划，从不照搬之前的预览。加 `--expect-plan` 时，重新计算的计划与预览不同就什么也不删（`prune_plan_changed`，退出码 3；API 返回 409）。

```console
$ relay cache prune --dry-run
清理预览（不会删除任何内容）：可删除快照载荷 2 个、检查点 6 个、残留目录 0 个，预计释放 31.6 KB。
  [删除] 快照 snap_20261119_ba93xpyo（项目 demo，3.3 KB）：不在保留集合中；签名元数据已保存到 cache/lineage
  [删除] 快照 snap_20261119_k0qtz09h（项目 demo，3.3 KB）：不在保留集合中；签名元数据已保存到 cache/lineage
  [删除] 检查点 19adc15fbc39（投递 sync_snap_20261119_k0qtz09h_798308_k3cbc8510 → dev_d6110c0c1766037b，4.2 KB）：接收方已用当前公钥确认缓存
  [删除] 检查点 2bad693aea9e（投递 sync_snap_20261119_tooj99i1_798308_k3cbc8510 → dev_d6110c0c1766037b，4.2 KB）：接收方已用当前公钥确认缓存
  [删除] 检查点 2f99e24c73b8（投递 sync_snap_20261119_k0qtz09h_798308_k76fc40f0 → dev_60ca04be088808b5，4.2 KB）：接收方已用当前公钥确认缓存
  [删除] 检查点 6a029d2b57cd（投递 sync_snap_20261119_ba93xpyo_798308_k3cbc8510 → dev_d6110c0c1766037b，4.2 KB）：接收方已用当前公钥确认缓存
  [删除] 检查点 b2af0301ed3b（投递 sync_snap_20261119_tooj99i1_798308_k76fc40f0 → dev_60ca04be088808b5，4.2 KB）：接收方已用当前公钥确认缓存
  [删除] 检查点 ec5f57e6f5a2（投递 sync_snap_20261119_ba93xpyo_798308_k76fc40f0 → dev_60ca04be088808b5，4.1 KB）：接收方已用当前公钥确认缓存
  [保留] 快照 snap_20261119_tooj99i1：项目当前版本、版本图头（含分叉头）
计划摘要：13fd1aad4d8360076479b7e67735e0128e9a4465e5ce5eb3fe43f1a4829f444c（确认后运行 relay cache prune --apply --expect-plan 13fd1aad4d8360076479b7e67735e0128e9a4465e5ce5eb3fe43f1a4829f444c；计划变化时不会删除）

$ relay cache prune --apply --expect-plan 13fd1aad4d8360076479b7e67735e0128e9a4465e5ce5eb3fe43f1a4829f444c
已清理：删除快照载荷 2 个、检查点 6 个、残留目录 0 个，释放 31.6 KB。保留 1 个快照载荷、0 个检查点。
签名元数据已保存到 cache/lineage；缓存清单会在下次 relay sync 时重新发布。
```

清理始终保留：

- 每个项目当前版本和全部版本图头（含分叉头）的载荷；
- 台账中每个未解决版本分叉的两侧；
- 待完成发送、尚未被领取也未被取代的交接、回执尚未上传的接收、未完成接收事务所用的快照，以及这些事务的回滚目标版本；
- 已配对对端尚未用当前公钥确认缓存的快照，以及本机或对端当前缓存清单 `needs` 中请求补发的快照；
- `preparing` 代次的检查点，以及接收方当前公钥尚未确认的已发布代次的检查点；
- 全部签名元数据。删除载荷之前，先把其中的 `snapshot.json` 复制为 `cache/lineage/<版本>.json` 并回读比较，父链验证仍然可用；
- 损坏、目录名不合法、是符号链接、含有不属于缓存的文件（例如凭据），或检查点与投递记录不符的条目。这些条目在 `unclear` 中报告。只要有一个恢复事务无法读取，本次就不清理任何载荷。

检查点只有同时满足以下两点才删除：其代次为已发布（或已放弃且从未续传），投递编号和摘要与记录一致；并且接收方已用当前公钥确认缓存、接收方已换钥使旧投递作废、接收设备已不在配对列表中，或已被更新的代次取代。

清理从不触碰 `relay_state.json`（含防重放记录 `processedDeliveries`）、`cloud.json`、`identity.json`、`deliveries.json` 中的记录（只会追加 `prunedSnapshots`）、`transactions/`、`archives/`、凭据，以及云端的任何对象。清理不联网：缓存清单会在下一次 `relay sync` 时重新发布。被清理的快照不会被重新下载，也不会出现在 `needs` 中请求补发，除非它重新成为当前版本或版本图头。

每个载荷分四步删除：把 `snapshot.json` 存入谱系目录，在 `prunedSnapshots` 登记，把载荷目录改名为 `.pruning-<名称>.<随机串>` 墓碑，删除墓碑。检查点先改名为墓碑再删除。清理中断后，`relay doctor` 报告 `prune_interrupted`；再次运行 `relay cache prune --apply` 会重新扫描并完成清理。结果：`success` 退出码 0；`partial`（有条目删除失败）退出码 3；`blocked` 什么也不删，本机状态损坏时退出码 4，本机从未同步、没有 `deliveries.json` 时退出码 3。

网页「待接棒」页显示「接力网络状态」面板，数据来自 `GET /api/cloud/doctor`，与 `relay doctor --json` 调用同一个函数。每个项目显示当前编辑设备、在途交接，哪些设备已确认缓存、等待确认、待补发或未知，以及每条诊断结果和可复制的命令。「重新诊断」重新运行诊断。「预览缓存清理」只做预览，并显示 `relay cache prune --apply --expect-plan <摘要>` 命令，页面从不删除任何内容。交棒因换钥失败时，页面提供「为当前公钥重新签发交接」按钮，这是唯一会发送 `reissue: true` 的控件。接口：`GET /api/cloud/doctor?project=`、`GET /api/cloud/cache?project=`、`POST /api/cloud/cache/prune`（`{mode: "dry-run" | "apply", project?, expectPlan?}`）；`POST /api/cloud/send` 接受 `reissue: true`，错误体带 `action`。

### 更严格的标识符与本机状态校验

签名验证通过后，仍会检查每个签名字段的类型、长度、路径安全性和相互一致性，然后才写入缓存、归档或工作区。

| 标识符 | 规则 |
| --- | --- |
| `versionId`、`deviceId`、`relayId` | ASCII 字母、数字和 `_`、`.`、`-`；以字母或数字开头；不含 `..`；不以 `.` 结尾；不是 Windows 设备名；最多 128 个字符（手动输入的交接编号最多 80 个）。投递编号规则相同，最多 160 个字符。 |
| `projectId` | Unicode 字母、数字、组合符，以及 `_`、`-`、`.` 和空格；必须 NFKC 稳定，因此拒绝 NFD 形式和全角符号；不能以 `.`、空格或 `-` 开头，不能以 `.` 或空格结尾；不含 `..`；不是设备名；最多 128 个字符。 |
| `parentVersionIds` | 合法且不重复的编号列表，不包含快照自身，最多 64 个。 |

对使用者的影响：

- **项目 ID 更严格。** `relay add`（带或不带 `--id`）、`relay import` 和本机 API（`PUT /api/projects/<id>`、`PUT /api/ledger`）都会拒绝不符合规则的新项目 ID，因此 `relay add … --id .hidden` 现在直接报「projectId 不能以点、空格或连字符开头…」；由项目名称自动生成的 ID 会被整理成合法形式（`.hidden` 变为 `hidden`）。台账中已有的此类 ID 仍可读取和编辑，但不能发布、交棒或接收；`relay doctor` 会以 `project_id_invalid` 报告，并给出用合法 ID 重新登记的 `relay add … --id <新项目ID>` 命令。
- **本机目录不能是符号链接。** `RELAY_DESK_HOME` 下的 `cache/snapshots`、`cache/lineage`、`archives/<项目>`、`transactions/` 和 `sync_publications/` 必须是普通目录；`RELAY_DESK_HOME` 本身可以是符号链接。若曾用符号链接把缓存或归档放到其他磁盘，同步会报 `local_dir_symlink`（`needs_user_action`，退出码 4，不是完整性失败），不经过链接写入任何内容；`relay doctor` 显示同一问题和把目录移回原处的命令。改回普通目录后，下次同步直接用云端原投递重新处理，不需要对端重新投递（期间已离开收件箱的投递会重新请求）。如果想把数据放在其他磁盘，请移动整个 home 目录，并把 `RELAY_DESK_HOME` 指向新位置。
- **接收时拒绝绝对路径符号链接。** 交接包中的符号链接指向绝对路径或项目之外，或者在符号链接条目下写入文件时，接收会在任何写入之前被拒绝。发送端仍会打包「指向项目内部的绝对路径」符号链接，而接收端会拒绝，因此请把这类符号链接改成相对路径。
- **本机状态损坏时明确停止。** `identity.json`、`cloud.json`、`relay_state.json`、`config.json`、`deliveries.json`、事务的 `meta.json` 和同步检查点都严格读取。文件不存在时仍按空状态开始；文件存在但已损坏（JSON 无效、空文件、被截断、字段类型错误、悬空符号链接或是目录）时，命令以 `local_state_corrupted` 停止（退出码 4），文件逐字节保持不变。以前损坏的 `relay_state.json` 或 `cloud.json` 会被静默重置，导致在途交接或配对丢失。恢复方法见 [MIGRATION.md](./docs/MIGRATION.md)。

```console
$ relay sync
本机接力状态 relay_state.json已损坏或无法读取（JSON 解析失败：Expecting property name enclosed in double quotes: line 1 column 3 (char 2)）：/tmp/relay-demo/home_gamma/relay_state.json。为避免覆盖已有数据，操作已停止；请先备份并修复或恢复该文件，不要直接删除。
处理：备份并修复报告中的本机状态文件后重新运行（不要直接删除）：/tmp/relay-demo/home_gamma/relay_state.json
```

### 接收到含 Git 忽略文件的目录

原地接收到已有的 Git 工作区时，Git 忽略文件（例如 `build/`、`*.log`、`node_modules/`）不再导致拒绝。它们在应用、回滚和继续恢复的全过程中都保持原样（内容、权限和修改时间）。未提交的改动和未被忽略的未跟踪文件仍然会阻止原地接收。

交接包出现以下任一情况时，接收会在任何写入之前被拒绝，目标目录完全不变：

1. 在本机保留文件的路径上写入文件或符号链接；
2. 在本机保留文件的位置建立目录；
3. 在含有本机保留文件的目录的位置写入文件；
4. 交接包的 `.gitignore` 规则不再忽略某个本机保留文件。接收后该文件会变成未跟踪文件，并在下次交棒时被打包带走。凭据从不打包，不受此规则影响。

路径按 NFC 规范化后比较；在不区分大小写的文件系统上按不区分大小写比较（`Debug.log` 与 `debug.log` 冲突）。被拒绝后，可用 `--into` 接收到一个干净的新目录。

## v2 ↔ v3 长期共存与兼容矩阵

| 拓扑组合 | 协议与行为 | 说明 |
| --- | --- | --- |
| **旧版 A ↔ 旧版 B** | 保持原有 v2 协议不变（`devices/{a,b}.json`、`relays/<relayId>/`） | 现有双机环境无需任何改动即可继续使用。 |
| **旧版 A ↔ 新版 B** | 新版 B 基于已验证能力自动启用 v2 协议适配器 | 新版 B 检测到旧版 A 仅支持 `["v2"]` 后，同时发布 `devices/b.json` 与 `v3/devices/<deviceId>.json`，并通过 `relays/<relayId>/` 与旧版 A 双向交接。 |
| **新版 A ↔ 新版 B ↔ 新版 C** | 动态 v3 个人多设备网络（`v3/devices/`、`v3/have/`、`v3/inbox/`） | 支持任意别名、快照发布/同步/转发、版本谱系 DAG 与 `workspace_tx` 崩溃恢复；v3 对端出错绝不降级到 `relays/`。 |
| **旧版 A 经新版 B ↔ 新版 C** | 保留原有 `A ↔ B` 配对边，经由新版 B 中转接力 | 旧版 A 按 v2 交棒给新版 B，再由新版 B 按 v3 交棒给新版 C（反之亦然）。 |
| **旧版 A 直接与动态别名 C 配对** | 必须升级 A 的接力引擎 | 旧版 A 仅支持固定槽位 `a`/`b` 且忽略 `v3/devices/`；新版 C 向旧版 A 交棒会收到 `v2_incompatible_alias` 升级提示。 |

**同机共存规则**：若在同一台设备上同时保留旧版 v2 与新版 v3，两套版本**必须**各自使用独立的 `RELAY_DESK_HOME` 与 `--ledger`（或 `RELAY_DESK_LEDGER`）路径，旧版 v2 客户端绝不能与新版共享多设备 v2 台账。

**同一网络中的较早 v3 版本。** 运行较早 v3 版本的设备可以继续与本版本交换快照和交接。新投递编号保留 `sync_` 和 `fwd_` 前缀，旧版收件箱仍把它们当作缓存投递跳过；缓存清单 v2 的 `snapshots` 仍是编号列表，旧版引擎忽略 ready 和缓存清单中的新字段。但较早版本不发布 `seq` 和 `needs`，所以它自己的缓存丢失后要升级才能自动补齐；它也会拒绝重新签发的交接（`send --reissue`），因此重新签发前请先升级目标设备。旧 A/B 的 v2 协议不变。详见 [MIGRATION.md](./docs/MIGRATION.md)。

## 验证当前版本

安装 Python、Git、PyNaCl、rclone 和 Node.js 后，可运行完整测试套件。

```sh
python3 -m unittest discover -s tests -v
```

Windows 上把 `python3` 换成 `py -3`。[验收指南](./acceptance/README.md)说明了各系统的原生检查、单机三设备模拟，以及在真实设备和公网云盘上分别验证的方法。

## 性能基准测试

复现不同设备数（`N=2,3,10`）与历史交接数（`H=100,1000,10000`）下 `old_v2`、`new_v2_compat` 与 `new_v3` 在 LocalDir 及 WebDAV/S3 回环后端上的收件箱读取与网络 I/O 对比：

```sh
python3 benchmarks/inbox_benchmark.py --full
```

## 命令速查

| 用途 | 命令 |
| --- | --- |
| 设备与项目登记 | `init`、`device`、`add`、`set` |
| 台账、备注与会话 | `status`、`scan`、`note`、`sessions`、`session add`、`session rm` |
| 手动接力清单 | `handoff`、`plan`、`check`、`depart`、`pickup`、`arrive`、`cancel` |
| 云接力与归档 | `cloud remotes`、`pair`、`publish`、`sync`、`send`、`inbox`、`receive`、`recover`、`transcripts` |
| 诊断与本机缓存 | `doctor`、`cache status`、`cache prune` |
| 网页、skills 与备份 | `serve`、`install-skills`、`export`、`import` |

新增参数：`sync --jsonl`、`sync --max-backoff 秒数`、`send --reissue`、`device --rotate-keys --force`、`cache prune --dry-run | --apply`、`cache prune --expect-plan 摘要`。

[MIT License](./LICENSE)
