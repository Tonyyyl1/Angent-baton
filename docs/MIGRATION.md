# relay-desk 迁移与恢复说明 / Migration and Recovery Notes

[中文](#中文) | [English](#english)

本文说明从上一个 v3 版本（CHANGELOG 中「v3 Personal Multi-Device Relay Network」一节所述的修复版）升级到本版本时需要知道的事。This page covers upgrading from the previous v3 build (the repair build described under "v3 Personal Multi-Device Relay Network" in the CHANGELOG) to this version.

---

## 中文

### 1 升级步骤

1. **先处理中断的接收（可选）。** 恢复日志格式仍是 2，本版本可以继续处理旧版本留下的事务；如果方便，仍建议先用旧版本运行 `relay recover <交接编号> --status`，把中断的接收或回滚做完。
2. **备份本机状态目录。** 复制整个 `RELAY_DESK_HOME`（默认 `~/.relay-desk`）。本版本严格读取本机状态文件，文件损坏时会停止而不是重置，备份是之后恢复的唯一来源。该目录中的 `identity.json` 含有本机私钥，备份只能留在本机或你自己的私有存储中，不要放进共享云目录。
3. **在每台设备上更新工具源码。** 没有需要手动执行的迁移命令。
4. **运行 `relay doctor`，再运行 `relay sync`。** 第一次同步会：
   - 在本机工作区锁内创建 `deliveries.json`，并从本机已有的发布检查点（`sync_publications/`）迁移投递记录；
   - 对每个已缓存的快照载荷完整计算一次摘要，写入 `cache/snapshots/<版本>/verified.json`（缓存较大时第一轮会慢一些）；
   - 发布签名缓存清单 v2（`v3/have/<deviceId>.json`）。
5. **检查项目 ID。** 不符合新规则的项目 ID（例如以 `.` 或 `-` 开头、含 `..`、全角或 NFD 字符）仍可在台账中读取，但发布、交棒和接收会报错；`relay doctor` 以 `project_id_invalid` 列出这些项目。新登记（`relay add`、`relay import`、本机 API）不再接受这类 ID。用合法的 ID 重新登记该目录：`relay add <名称> --path <目录> --id <新ID>`。新 ID 在接力网络中是一个新项目，其他设备需要从新 ID 开始接收。
6. **检查符号链接目录。** `RELAY_DESK_HOME` 下的 `cache/snapshots`、`cache/lineage`、`archives/<项目>`、`transactions/`、`sync_publications/` 不能是符号链接；`relay doctor` 以 `local_dir_symlink` 报告并给出移回命令，同步在改回之前暂停写入（需要处理，退出码 4），改回后直接重新处理原投递。若曾把缓存链接到其他磁盘，请把整个 `RELAY_DESK_HOME` 移过去，再设置环境变量 `RELAY_DESK_HOME` 指向新位置。

### 2 本机状态文件的变化

| 文件 | 变化 | 说明 |
| --- | --- | --- |
| `deliveries.json` | 新建，`format: 1` | 第一次同步、发布或交棒时在锁内创建，从发布检查点迁移（用本机当前或历史公钥验证签名，记为 `published`，原因 `legacy`）。`relay_state.json` 中的 `forwardedSnapshots` 和 `processedDeliveries` 留在原处继续读取。 |
| `cache/snapshots/<版本>/verified.json` | 新建 | 缓存摘要备忘：大小、修改时间、sha256。大小或修改时间变化时重新计算。 |
| `cache/lineage/<版本>.json` | 新建 | 由 `relay cache prune` 创建：被清理载荷的签名元数据，用于父链验证，始终保留。 |
| `sync_status.json` | 新建 | 最近一次同步的结果与心跳，供 `relay doctor` 和网页读取。 |
| `relay_state.json` | 版本仍为 3，新增字段 | `sentRelays`/`pendingSends` 增加 `recipientKeyFingerprint`；版本图节点增加 `authorKeyFingerprint`、`trust`、`recordSha256`；重新签发时原发送记录增加 `supersededBy`。仍会写 `processedDeliveries`，仍会追加 `forwardedSnapshots`（不再是权威记录，留给旧版本读取）。 |
| `cloud.json` | 版本仍为 3，新增字段 | 用 `relay pair --accept` 重新确认对端新公钥时，旧公钥追加到 `peers[*].keyHistory`（最多 10 个）。 |
| `identity.json` | 新增字段 | 换钥时旧公钥（不含私钥）追加到 `previousKeys`（最多 10 个）。内容不变时不再重写该文件，因此也不再每次把权限改回 0600。 |
| 恢复日志 `transactions/<交接编号>/journal.json` | 不变 | 写格式 2，读取格式 1 和 2。 |
| `config.json`、台账 `ledger.json` | 格式不变 | 损坏时会报错停止，不再退回到工具目录下的默认台账。 |

云端对象：新的同步投递使用新编号格式 `(sync|fwd)_<快照>_<发送方6位>_k<指纹前8位>[_gN]`；缓存清单增加 `haveFormat`、`keyFingerprint`、`seq`、`needs`；v3 交接 ready 增加签名字段 `recipientKeyFingerprint`，重新签发的 ready 增加 `supersedesRelayId`/`supersedesRelayIds`。已发布的对象从不改写，旧编号的投递照常读取。

### 3 恢复损坏的状态文件

本版本发现本机状态文件损坏时，会以 `local_state_corrupted`（或 `delivery_state_corrupted`）停止，退出码 4，文件逐字节保持不变。`relay doctor` 会报告是哪个文件，并给出一条处理命令。总原则：**先备份损坏的文件，不要直接删除。** 除了 v2 升级时生成的一次性备份（`cloud.v2.backup.json`、`relay_state.v2.backup.json`，内容停留在升级 v3 之前），relay-desk 不会自动备份这些文件，所以要用你自己的备份。

| 文件 | 处理方法 |
| --- | --- |
| `deliveries.json` | 可以重建：把它移开，再运行 `relay sync`，会从本机发布检查点重新创建（见下方示例）。 |
| `relay_state.json` | 从备份恢复：`cp "<relay_state.json 的备份>" "<RELAY_DESK_HOME>/relay_state.json"`。损坏很小时（例如末尾被截断）也可以手工修复 JSON。没有备份时不要直接删除：该文件不存在时会被当作从未创建，本机会丢失在途交接、已接收记录、`processedDeliveries` 和版本图。 |
| `cloud.json` | 从备份恢复。没有备份时，重新配置意味着重新运行 `relay pair --remote … --root …`，并与每台设备重新核对指纹。 |
| `identity.json` | 只能从备份恢复。对端固定了本机公钥，不能删除或重新生成；重新生成会得到新的 `deviceId`，发给旧 `deviceId` 的交接都无法领取。 |
| `config.json` | 从备份恢复，或按原内容手工修复（它记录本机设备别名和台账路径）。 |
| `transactions/<交接编号>/meta.json` 或 `journal.json` | 继续恢复和回滚都会被拒绝（`recovery_corrupted`）。先备份整个事务目录（`cp -R "<事务目录>" "<事务目录>.backup"`），再从备份恢复 `meta.json` 或 `journal.json`；确需放弃时人工处理，不要直接删除。 |

`deliveries.json` 损坏与重建的实际输出（临时目录，非真实环境）：

```console
$ relay doctor
relay doctor：本机 gamma（dev_125cf833db8a0708）  状态：需要处理（需要处理 1，警告 0，提示 0）
[需要处理] delivery_state_corrupted
  原因：投递状态 deliveries.json 已损坏（本机投递状态 deliveries.json已损坏或无法读取（顶层不是对象）：/tmp/relay-demo/home_gamma/deliveries.json。为避免覆盖已有数据，操作已停止；请先备份并修复或恢复该文件，不要直接删除。 可把该文件移到别处后重新运行 relay sync，会从本机发布检查点重建。）；同步与缓存清理已停止。该文件可以从本机发布检查点重建。
  处理命令：mv "/tmp/relay-demo/home_gamma/deliveries.json" "/tmp/relay-demo/home_gamma/deliveries.json.corrupt" && relay sync

$ mv …/deliveries.json …/deliveries.json.broken
$ relay sync
网络同步完成（共 1 轮）：
  签收回执：0 条    同步快照：0 个    转发快照：0 个    版本分叉：0 个
同步状态：成功
```

`relay_state.json` 损坏时，`relay doctor` 报告 `relay_state_corrupted`，处理命令为 `cp "<relay_state.json 的备份>" "<路径>"`；用备份恢复后再运行 `relay doctor`，结果恢复为「正常」。

### 4 换钥时的在途交接

1. **换钥前先确认没有在途交接。** 运行 `relay doctor`。本机有未完成的发送、未被领取也未被取代的交接、未上传的回执或未完成的接收事务时，`relay device --rotate-keys` 会拒绝（退出码 4），并列出每项的处理命令。最简单的做法是先让对方领取（发送方再运行 `relay sync` 同步回执），然后换钥。
2. **确需立即换钥：** `relay device --rotate-keys --force`。命令会列出受影响的交接。之后：
   - 对端核对并重新确认本机新指纹：`relay pair --with <本机别名> --status`，然后 `relay pair --with <本机别名> --accept --fingerprint <FP>`；
   - 本机运行 `relay send <项目> --to <设备> --reissue`，为同一快照生成新交接编号和新密文；
   - 如果对方在重新确认之前已经用旧固定公钥领取了原交接，以原交接为准，重新签发的交接记为 `withdrawn`。先领取的那个生效，对方不会两者都领取。
3. **目标设备换钥：** 发送方的 `relay send` 和 `relay doctor` 报 `handoff_recipient_key_changed`。发送方核对目标的新指纹并重新确认后，运行 `relay send <项目> --to <目标> --reissue`。目标设备的 `relay doctor` 报 `incoming_sealed_to_retired_key`，并给出发送方要运行的同一条命令。
4. **原交接保持不变。** 原交接包、ready 标记和回执都不会被改写；目标设备收件箱只列出新交接。
5. **目标设备必须是本版本。** 较早的 v3 引擎会拒绝重新签发的交接（失败方向安全）。先升级目标设备再重新签发。
6. **同步投递不需要手动处理。** 换钥并重新确认后，旧的同步投递被标为过期，接收方在缓存清单 `needs` 中请求补发，发送方自动用新编号重新投递。

### 5 回滚与恢复命令

| 情况 | 命令 |
| --- | --- |
| 查看中断事务的阶段、能否继续、能否回滚 | `relay recover <交接编号> --status` |
| 台账更新开始之前中断，想回到接收前状态 | `relay recover <交接编号> --rollback` |
| 回滚本身中断 | 再次运行 `relay recover <交接编号> --rollback`；回滚开始后 `receive` 不会签收 |
| 台账更新已开始（`UPDATING_LEDGER` 及之后），或工作区已落地但回执未上传 | `relay receive <交接编号>` 继续完成；此时不能再回滚 |
| 目标目录有冲突、分叉或中断后被修改 | `relay receive <交接编号> --into <新目录>` |
| 发送端回滚未完成的发送时提示 `rollback_cloud_check_failed`（无法确认云端是否已发布） | 发送记录已保留。网络恢复后再次运行 `relay recover <交接编号> --rollback`，或重新运行 `send` 完成发送 |
| 不确定该运行哪条 | `relay doctor`，按其中给出的命令处理 |

带有新格式日志、尚未完成的事务不要交给旧引擎处理。

### 6 新旧引擎混用规则

- **旧 A/B（v2）：** 协议不变，`devices/{a,b}.json` 与 `relays/<relayId>/` 路径照常使用。
- **较早的 v3 版本与本版本：** 可以继续互相交换快照和交接。新投递编号保留 `sync_`/`fwd_` 前缀，旧版本收件箱仍把它们当缓存投递跳过；缓存清单 v2 的 `snapshots` 仍是编号列表；旧版本忽略 ready 与缓存清单中的新字段。限制：
  - 较早版本不发布 `seq`/`needs`，它自己的缓存丢失后，要升级才能自动补齐；
  - 较早版本拒绝重新签发的交接；
  - 较早版本仍会把损坏的本机状态文件静默重置，所以混用期间更要保留备份。
- **同一台机器上安装两个版本：** 必须使用各自独立的 `RELAY_DESK_HOME` 和台账（`--ledger` 或 `RELAY_DESK_LEDGER`）。
- **降级：** 不要在有新格式恢复日志待处理时降级。较早版本不读取 `deliveries.json`，本版本仍持续写入 `forwardedSnapshots`，供降级后的旧版本使用。

---

## English

### 1 Upgrade steps

1. **Optionally finish interrupted receives first.** The recovery journal format is still 2, so this version can resume transactions left by the previous build. If convenient, run `relay recover <RELAY_ID> --status` with the previous build and finish any interrupted receive or rollback first.
2. **Back up the local state directory.** Copy the whole `RELAY_DESK_HOME` (default `~/.relay-desk`). This version loads local state strictly: a damaged file stops the command instead of being reset, and your backup is then the only way to restore it. `identity.json` in that directory holds this device's private key, so keep the backup on this device or in your own private storage, never in the shared cloud folder.
3. **Update the tool checkout on every device.** There is no migration command to run.
4. **Run `relay doctor`, then `relay sync`.** The first sync:
   - creates `deliveries.json` inside the local workspace lock and migrates delivery records from this device's existing publish checkpoints (`sync_publications/`);
   - hashes every cached snapshot payload once and writes `cache/snapshots/<version>/verified.json`, so the first round is slower with a large cache;
   - publishes the signed have manifest v2 (`v3/have/<deviceId>.json`).
5. **Check project IDs.** A project ID that breaks the new rules (for example one that starts with `.` or `-`, contains `..`, or has full-width or NFD characters) can still be read from the ledger, but publish, send, and receive fail; `relay doctor` lists such projects as `project_id_invalid`. New registrations (`relay add`, `relay import`, the local API) no longer accept such IDs. Register the folder again under a valid ID: `relay add <NAME> --path <DIR> --id <NEW_ID>`. The new ID is a new project for the network, so other devices start receiving it under the new ID.
6. **Check for symlinked directories.** `cache/snapshots`, `cache/lineage`, `archives/<project>`, `transactions/`, and `sync_publications/` under `RELAY_DESK_HOME` must not be symlinks. `relay doctor` reports one as `local_dir_symlink` with a command that moves the directory back; sync stops writing through it (needs action, exit 4) and processes the original deliveries again once it is a real directory. If you linked the cache to another disk, move the whole `RELAY_DESK_HOME` there and point the `RELAY_DESK_HOME` environment variable at it.

### 2 Local state files

| File | Change | Notes |
| --- | --- | --- |
| `deliveries.json` | New, `format: 1` | Created inside the lock on the first sync, publish, or send, and migrated from publish checkpoints (signatures verified with this device's current or previous keys; recorded as `published` with reason `legacy`). `forwardedSnapshots` and `processedDeliveries` stay in `relay_state.json` and are still read. |
| `cache/snapshots/<version>/verified.json` | New | Cache digest memo: size, mtime, sha256. Recomputed when the size or mtime changes. |
| `cache/lineage/<version>.json` | New | Written by `relay cache prune`: the signed metadata of a pruned payload, kept for parent-chain checks and never pruned. |
| `sync_status.json` | New | Result and heartbeat of the latest sync, read by `relay doctor` and the web page. |
| `relay_state.json` | Still version 3, new fields | `sentRelays` and `pendingSends` gain `recipientKeyFingerprint`; version-graph nodes gain `authorKeyFingerprint`, `trust`, `recordSha256`; a re-issue adds `supersededBy` to the original send record. `processedDeliveries` is still written and `forwardedSnapshots` is still appended (no longer authoritative; kept for older builds). |
| `cloud.json` | Still version 3, new field | When you re-confirm a peer's new key with `relay pair --accept`, the old key is appended to `peers[*].keyHistory` (last 10). |
| `identity.json` | New field | A rotation appends the old public keys, never the private keys, to `previousKeys` (last 10). The file is no longer rewritten when nothing changed, so its permissions are no longer reset to 0600 on every run. |
| Recovery journals `transactions/<relay_id>/journal.json` | Unchanged | Written as format 2; formats 1 and 2 are read. |
| `config.json`, ledger `ledger.json` | Format unchanged | A damaged file now stops the command instead of falling back to the default ledger in the tool folder. |

Cloud objects: new cache deliveries use the ID format `(sync|fwd)_<snapshot>_<sender6>_k<fp8>[_gN]`; have manifests add `haveFormat`, `keyFingerprint`, `seq`, and `needs`; v3 handoff ready markers add the signed field `recipientKeyFingerprint`, and re-issued ones add `supersedesRelayId` and `supersedesRelayIds`. Published objects are never rewritten, and deliveries with the old ID format are still read.

### 3 Recovering a damaged state file

When this version finds a damaged local state file it stops with `local_state_corrupted` (or `delivery_state_corrupted`), exit 4, and leaves the file byte-for-byte unchanged. `relay doctor` names the file and gives one command. The general rule: **back up the damaged file and do not delete it.** relay-desk makes no automatic backup of these files apart from the one-time v2 upgrade backups (`cloud.v2.backup.json`, `relay_state.v2.backup.json`), which date from before the v3 upgrade, so use your own backup.

| File | What to do |
| --- | --- |
| `deliveries.json` | It can be rebuilt: move it aside and run `relay sync`, which recreates it from this device's publish checkpoints (see the example in the Chinese section above). |
| `relay_state.json` | Restore from a backup: `cp "<backup of relay_state.json>" "<RELAY_DESK_HOME>/relay_state.json"`. Small damage, such as a truncated end, can also be fixed by hand. Without a backup, do not just delete it: a missing file is treated as never created, and this device loses its in-flight handoffs, received-handoff records, `processedDeliveries`, and version graph. |
| `cloud.json` | Restore from a backup. Without one, you must run `relay pair --remote … --root …` again and re-confirm every peer's fingerprint. |
| `identity.json` | Restore from a backup; there is no other way. Peers pinned this device's public key, so do not delete or regenerate it. A new identity gets a new `deviceId`, and handoffs addressed to the old `deviceId` can no longer be claimed. |
| `config.json` | Restore from a backup, or fix it by hand (it records this device's alias and the ledger path). |
| `transactions/<relay_id>/meta.json` or `journal.json` | Resume and rollback are both refused (`recovery_corrupted`). Back up the whole transaction directory (`cp -R "<dir>" "<dir>.backup"`), then restore `meta.json` or `journal.json` from a backup. If you must give the transaction up, handle it by hand; do not just delete it. |

For a damaged `relay_state.json`, `relay doctor` reports `relay_state_corrupted` with the command `cp "<relay_state.json 的备份>" "<path>"`. After restoring the backup, `relay doctor` reports 正常 (healthy) again.

### 4 In-flight handoffs and key rotation

1. **Before rotating, make sure nothing is in flight.** Run `relay doctor`. `relay device --rotate-keys` refuses (exit 4) while this device has an unfinished send, a handoff that is neither claimed nor superseded, a receipt not yet uploaded, or an unfinished receive transaction, and lists a command for each. The simplest path is to let the target claim the handoff (the sender then runs `relay sync` to pick up the receipt) and rotate afterwards.
2. **If you must rotate now:** `relay device --rotate-keys --force`. It lists the affected handoffs. Then:
   - each peer compares and re-confirms this device's new fingerprint: `relay pair --with <this alias> --status`, then `relay pair --with <this alias> --accept --fingerprint <FP>`;
   - this device runs `relay send <PROJECT> --to <DEVICE> --reissue`, which creates a new relay ID and new ciphertext for the same snapshot;
   - if the target claimed the original with the old pinned key before re-confirming, the original stands and the re-issue is recorded as `withdrawn`. The first claim wins; the target never claims both.
3. **If the target rotated:** the sender's `relay send` and `relay doctor` report `handoff_recipient_key_changed`. After comparing and re-confirming the target's new fingerprint, the sender runs `relay send <PROJECT> --to <TARGET> --reissue`. On the target, `relay doctor` reports `incoming_sealed_to_retired_key` with the same command for the sender to run.
4. **The original stays untouched.** The original package, ready marker, and receipt are never rewritten, and the target's inbox lists only the new handoff.
5. **The target must run this version.** An earlier v3 engine refuses a re-issued handoff (failing safe). Upgrade the target before re-issuing.
6. **Cache deliveries need nothing from you.** After a re-confirmed rotation, old cache deliveries are marked obsolete, the receiver asks for them again in its have `needs`, and the sender redelivers them under new IDs.

### 5 Rollback and recovery commands

| Situation | Command |
| --- | --- |
| See an interrupted transaction's phase and whether it can resume or roll back | `relay recover <RELAY_ID> --status` |
| Interrupted before the ledger update started; return to the pre-receive state | `relay recover <RELAY_ID> --rollback` |
| The rollback itself was interrupted | Run `relay recover <RELAY_ID> --rollback` again; once a rollback starts, `receive` will not acknowledge |
| The ledger update started (`UPDATING_LEDGER` or later), or the workspace is in place but the receipt was not uploaded | `relay receive <RELAY_ID>` to finish; rollback is no longer allowed |
| The target folder has a conflict or fork, or was changed after the interruption | `relay receive <RELAY_ID> --into <NEW_DIR>` |
| Rolling back an unfinished send on the sender reports `rollback_cloud_check_failed` (the cloud could not be checked) | The send record is kept. Run `relay recover <RELAY_ID> --rollback` again once the network is back, or re-run `send` to finish |
| Not sure which applies | `relay doctor`, then run the command it gives |

Do not hand an unfinished transaction with a new-format journal to an older engine.

### 6 Mixing old and new engines

- **Old A/B (v2):** the protocol is unchanged; `devices/{a,b}.json` and `relays/<relayId>/` work as before.
- **Earlier v3 build with this version:** they keep exchanging snapshots and handoffs. New delivery IDs keep the `sync_`/`fwd_` prefixes, which older inboxes still skip as cache deliveries; have v2 still lists `snapshots` as plain IDs; older engines ignore the new ready and have fields. Limits:
  - an earlier build does not publish `seq` or `needs`, so its own lost cache is repaired only after it upgrades;
  - an earlier build refuses a re-issued handoff;
  - an earlier build still silently resets a damaged local state file, so keep backups while you run mixed versions.
- **Both versions on one machine:** each needs its own `RELAY_DESK_HOME` and its own ledger (`--ledger` or `RELAY_DESK_LEDGER`).
- **Downgrading:** do not downgrade while a new-format recovery journal is pending. An earlier build does not read `deliveries.json`; this version keeps writing `forwardedSnapshots` so that an older build can still use it after a downgrade.
