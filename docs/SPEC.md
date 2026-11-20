# relay-desk 规格说明（v2 / v3 个人多设备接力网络）

多设备接力台账与云同步协议：统一的 N 设备网状网络（支持 `a`、`b`、`c` 及自定义设备别名与唯一加密 `deviceId`），供 **Claude Code、Codex、Antigravity 与网页** 共用。本文件是实现的权威依据。

---

## 1. 核心设计原则

1. **文件为准**：台账是磁盘上的 JSON 文件（`ledger.json`），版本规范为 v2；本机配置与加密状态存放在 `RELAY_DESK_HOME`（默认 `~/.relay-desk`）。
2. **不碰远程**：代码绝不执行 `git push`、`git fetch` 等网络 Git 命令，仅打印提示供人工或代理执行。
3. **不读凭据**：私钥、`.env`、`.env.*`（`.env.example`/`.env.sample`/`.env.template` 除外）、`auth.json`、`oauth_creds.json`、`.claude.json`、`.credentials.json`、`.pem`/`.key`/`.p12`/`.pfx`、`id_rsa`/`id_ed25519`/`id_ecdsa`/`id_dsa` 等敏感文件仅检查存在性，绝不读取、复制、打包或写入台账。
4. **摘要与 Session 解耦**：交接文件不替代本地 Session，本地原生 Session 仅在原设备恢复；跨机只同步可检索的历史对话归档（`conversations` / `transcripts`）。
5. **Python 3.9 兼容**：兼容 Python 3.9，不使用 `match` 或运行时 `X | Y` 联合类型语法，统一 UTF-8 编码与 NFC 路径规范化。
6. **简体中文输出**：用户可见提示一律使用简体中文，标识符与注释使用英文。

---

## 2. 数据结构与状态文件规范

### 2.1 台账结构 (`ledger.json` v2)

- **版本与备份**：`version: 2`。首次从 v1 升级时通过 `ensure_backup_once` 原子备份原始文件为 `<ledger>.v1.backup.json`。
- **设备清单 (`config['devices']`)**：支持任意 N 台设备，每条记录包含 `id`（别名）、`name`、`os`（`windows`/`mac`/`linux`）、`drop`、`host`、`deviceId`（唯一加密标识），以及可选字段 `osExplicit`、`keyFingerprint`、`rotatedAt`、`needsPairing`。
- **别名与冲突校验**：
  - `ALIAS_RE = ^[A-Za-z0-9_.-]{1,40}$`（由 `relaydesk/devices.py` 的 `validate_alias` 校验）。
  - `norm_config` 强制校验唯一性：同一台账内不允许出现大小写不敏感重复的 `id`（`alias_conflict`）、非空重复的 `deviceId`（`device_id_conflict`）或大小写不敏感非空重复的 `name`（`name_conflict`），违者抛出 `LedgerError` / `DeviceConflictError`。
- **幂等身份迁移 (`migrate_ledger_identities`)**：
  - 仅按精确别名或唯一大小写不敏感别名将已知 `deviceId`（来自本机 `identity.json` 与 `cloud.json`）绑定到 `config['devices']`，绝不猜测映射。
  - 同步回填 `projects[].pathsByDeviceId`、`sessions[].deviceId`、`history[].{fromDeviceId, toDeviceId}` 与 `handoff.{fromDeviceId, toDeviceId}`。
  - 若历史项目引用了尚未映射 `deviceId` 的旧设备别名，则标记该设备 `needsPairing: true`（在绑定 `deviceId` 后自动移除）。

### 2.2 云端配置与对端信任表 (`cloud.json` v3)

- **迁移与备份**：`version: 3`。从 v2（单对端 `peer` / `paired` / `pairFingerprint` / `pairedAt`）幂等迁移为以 `deviceId` 为键的 `peers` 字典与 `shownFingerprints` 字典；首次迁移时将原文件原子备份为 `<local_home>/cloud.v2.backup.json`。当且仅当恰好存在 1 个已配对对端时，`sync_legacy_cloud_fields` 同步写入兼容字段 `peer` / `paired` / `pairFingerprint` / `pairedAt`。
- **顶层字段**：`version` (`3`)、`provider` (`"rclone"` | `"local"`)、`remote`、`root`、`configPath`、`shownFingerprints` (`{deviceId: pairFingerprint}`)、`peers` (`{deviceId: PeerRecord}`)。
- **PeerRecord 字段**：`deviceId`、`alias`、`name`、`os`、`host`、`verifyPublicKey`、`boxPublicKey`、`keyFingerprint`、`pairFingerprint`、`paired` (`bool`)、`status` (`"paired"` | `"discovered"` | `"key_changed"`)、`pairedAt`、`protocolVersion` (`2` 或 `3`)、`capabilities`（如 `["v2"]` 或 `["v2", "v3"]`）、`pendingDescriptor`（密钥轮换待确认时保存新描述符，否则 `null`）、`keyHistory`（可选，见下）。
- **密钥轮换隔离 (`device --rotate-keys` / `rotate_identity_keys`)**：
  - 本机在 `<local_home>/identity.json` 中轮换 Ed25519 与 X25519 密钥对，保留原 `deviceId` 并记录 `rotatedAt`。被替换的公钥（不含私钥）追加到 `identity.json` 的 `previousKeys`（每项 `verifyPublicKey`、`boxPublicKey`、`keyFingerprint`、`createdAt`、`retiredAt`，最多保留 10 项），用于核对本机换钥前签名的旧证据；不会再用它们签名或加密任何新内容。公开描述文件不含 `previousKeys`。
  - 其他设备发现对端公钥变更时，**仅**将该对端标记为 `status: "key_changed"`、`paired: false`，将新公钥描述符暂存至 `pendingDescriptor` 并暂停与该节点的交棒与接收（抛出 `KeyChangedError`），直到用户重新核对指纹并执行 `pair --with <id|alias> --accept --fingerprint <FP>`；其余已配对节点不受影响。同步中对端签名的对象（ready、have、回执）验签失败时，会下载并自验该对端的 `v3/devices/<deviceId>.json`（每个对端 10 分钟内最多一次），公钥与固定公钥不同则同样标为 `key_changed`；这一探测只会降低信任。
  - **`keyHistory`**：只有用 `pair --accept` 重新确认同一 `deviceId` 的新公钥时，才把此前确认过的公钥追加到 `peers[*].keyHistory`（每项 `keyFingerprint`、`verifyPublicKey`、`boxPublicKey`、`pairedAt`、`retiredAt`、`reason: "rotated"`，最多 10 项）。只在 `pendingDescriptor` 中出现过的公钥不会进入历史；`--replace-device`（不同 `deviceId`）不记录历史。
  - **换钥保护**：本机有待完成发送、未签收且未被取代的交接、未上传的回执、未完成或损坏的接收事务（或本机状态损坏）时，`device --rotate-keys` 拒绝（CLI 退出码 4）并逐项给出处理命令；`--force` 才执行，并列出受影响的交接与 `send --reissue` 后续步骤。
- **别名/名称冲突拒绝 (`PeerConflictError`, `code="alias_conflict"`)**：
  - 绝不按别名自动继承信任。若新 `deviceId` 声明了与本机或其他已知节点冲突的 `alias` 或非通用 `name`，拒绝自动配对；必须显式使用 `--replace-device --accept --fingerprint <FP>` 替换旧设备，或通过 `--with <deviceId> --alias <新别名>` 分配独立别名。

### 2.3 运行状态机 (`relay_state.json` v3)

- **迁移与备份**：`version: 3`。首次从 v2 升级时原子备份为 `<local_home>/relay_state.v2.backup.json`。
- **顶层字段**：
  - `pendingSends`: `{projectId: ...}` — 发送检查点（`checkpointBeforeReady: true` 与 `prepared` 载荷）；新增 `recipientKeyFingerprint`，重新签发时另有 `reissueOf`（原交接编号）与 `reissueOrigin`。
  - `sentRelays`: `{relayId: ...}` — 已发出交接记录（含 `protocolVersion`、`readyKey`、`packageKey`、`snapshotId`、`packageSha256`、`signedReady`、`acked`、`ackedAt`）；新增 `recipientKeyFingerprint`（交接绑定的接收方公钥指纹）、`supersededBy`/`supersededAt`（被重新签发取代）、`withdrawn`（重新签发因原交接先被领取而作废）。
  - `appliedReceives`: `{relayId: ...}` — 兼容旧版未完成接收记录。
  - `receivedRelays`: `{relayId: ...}` — 已签收/待补传回执的接收记录（含 `receiptUploaded`、`signedReceipt`、`result`）。
  - `projectBaselines`: `{projectId: {headCommit, folderHashes, gitSnapshotHash}}` — 干净接收基线指纹。
  - `activeHandoffs`: `{projectId: relayId}` — 每个项目当前唯一在途未签收的活跃交接编号。
  - `versionGraph`: `{projectId: {nodes: {versionId: ...}, heads: [...]}}` — 作者签名版本 DAG。节点除 `versionId`、`projectId`、`authorDeviceId`、`parentVersionIds`、`parentRelayId`、`relayId`、`contentSha256`、`createdAt` 外，新增可核对证据：`authorKeyFingerprint`（验证该快照时使用的作者公钥指纹）、`trust`（`current` 当前固定公钥 / `historical` 已重新确认的历史公钥 / `attested` 本机版本图已记录、记录摘要一致）、`recordSha256`（签名快照记录规范 JSON 的 SHA-256）。
  - `localCurrentVersion`: `{projectId: versionId}` — 本机当前已落地/已发布的版本指针。
  - `processedDeliveries`: `{deliveryId: ...}` — 已处理缓存同步投递记录（防止重复下载解析，也是防重放记录；缓存清理从不改动）。投递只有在其快照缓存校验通过时才因此被跳过，缓存丢失或损坏时会重新处理。
  - `archivedRelayIds`: `{relayId: true}` — 已归档的历史 v2 中继编号。
  - `forwardedSnapshots`: `{peerDeviceId: [versionId, ...]}` — 本机已向指定节点转发过的快照记录。投递去重的权威记录已移到 `deliveries.json`（§2.4）；本字段仍持续追加，作为非权威镜像供降级后的旧版本读取。
- **v2 升级保护**：
  - **`pendingSends`**：带有 `"prepared"` 的旧记录保留原 `relayId` 并补全 `protocolVersion: 2`，重试发送时沿用原 `relayId` 完成发布；既无 `"prepared"` 又无 `"checkpointBeforeReady"` 的旧记录被标记为 `status: "needs_verification"`、`verificationRequired: true`——若云端能找到对应 `ready.json` 则接管补全，否则抛出 `PendingVerificationError`（`code="pending_send_needs_verification"`），需通过 `recover <RELAY_ID> --rollback` 清理。
  - **`appliedReceives`**：迁移后自动标记 `workspaceApplied: true` 与 `migratedFromV2: true`；恢复 `receive` 时校验 `packageSha256` 与 `projectId` 后**跳过工作区写入**，直接完成归档、台账与回执上传，完整保留任何应用后本地修改。
  - 迁移时未签收的 `sentRelays` 会自动回填 `activeHandoffs[projectId]`。

### 2.4 投递状态 (`deliveries.json`, format 1)

本机独立文件，记录缓存投递的去重、代次与确认状态，不随快照数 × 对端数膨胀 `relay_state.json`：

```json
{"format": 1, "createdAt": "…",
 "migration": {"checkpoints": 3, "skippedCheckpoints": 0, "forwardedSnapshots": 5, "processedDeliveries": 7, "at": "…"},
 "outbound": {"<接收设备>|<规范化指纹>|<快照>": {
    "recipientDeviceId", "recipientKeyFingerprint", "snapshotId", "projectId", "authorDeviceId", "senderKeyFingerprint",
    "status": "pending|published|peer_verified|covered|superseded",
    "generations": [{"gen", "deliveryId", "readyKey", "state": "preparing|published|abandoned", "reason",
                     "packageSha256", "packageSize", "createdAt", "publishedAt"}],
    "peerVerified": {"haveSeq", "at"}, "lastRegenHaveSeq", "coveredBy", "lastCheckedAt", "supersededFrom"}},
 "inbound": {"<投递编号或 head|<快照>>": {"state": "cached|obsolete|quarantined|repair_pending|blocked_local", "reason",
             "snapshotId", "projectId", "senderDeviceId", "objectTag", "problem"}},
 "peerHave": {"<设备>": {"keyFingerprint", "seq", "digest", "verifiedAt", "count", "needs"}},
 "self": {"haveSeq", "lastHaveDigest", "lastHaveAt", "lastHaveSeq"},
 "prunedSnapshots": {"<快照>": "<时间>"}}
```

- **规范化指纹**：去掉非字母数字字符后大写（`ABCD-EF01` → `ABCDEF01`）。
- **不存在**：首次在工作区锁内创建，并从本机发布检查点 `sync_publications/` 迁移（用本机当前或 `previousKeys` 公钥验证签名，按 ready 中的接收方指纹记为 `published`，原因 `legacy`）；`forwardedSnapshots`、`processedDeliveries` 留在原处，只记录数量。
- **损坏、结构错误或由更新的格式写入**：抛出 `DeliveryStateCorruptedError`（`code="delivery_state_corrupted"`，需要用户处理），本轮不加锁、不写任何文件。处理：把文件移开后重新运行 `relay sync`，会从检查点重建。
- **写入**：独立锁 `deliveries.json.lock`，原子替换。
- **`covered`**：本机没有自身记录时，若对端收件箱中已有另一台已配对设备（验签通过、绑定同一接收方指纹）或本机交接带来的同一快照，就不再投递。
- **`superseded`**：接收方换钥后，旧指纹下尚未确认的记录。
- **`prunedSnapshots`**：`relay cache prune` 有意清理载荷的快照。这些快照不会被重新下载、不会进入 `needs`；若某快照重新成为当前版本或版本图头，其清理标记会被撤销。清理只追加此字段，从不删除其他记录。

### 2.5 其他本机文件

- `cache/snapshots/<versionId>/verified.json`：缓存摘要备忘（大小、`mtime_ns`、sha256）。大小或修改时间变化即重算；备忘超过 7 天时，在每次操作 64 MB 的重算预算内重新计算。缓存清单与转发只使用校验通过的缓存。
- `cache/lineage/<versionId>.json`：被清理载荷的原样 `snapshot.json`（作者签名元数据），始终保留，用于父链验证。
- `sync_status.json`（`schema: relaydesk.sync.status/1`）：最近一次同步的汇总（不含完整事件环，只保留最后 20 条 `recentEventsTail`）以及 `running`、`heartbeatAt`、`pid`；见 §8。
- `sync_publications/<sha256(readyKey)>/`：投递检查点（§3.1），由 `relay cache prune` 按 §8.4 规则清理。

### 2.6 本机状态的严格读取

`identity.json`、`cloud.json`、`relay_state.json`、`config.json`、`deliveries.json`、事务 `meta.json` 与同步检查点区分两种情况：

- **从未创建**（文件不存在）：按空状态处理，行为与以前相同；v2 的 `cloud.json`、`relay_state.json` 照常迁移。
- **存在但损坏或不可读**（JSON 无效、空文件、截断、非对象、非法 UTF-8、字段类型错误、已配对对端 `deviceId` 非法、悬空符号链接、目录）：抛出 `LocalStateCorruptError`（`category="needs_user_action"`，`code="local_state_corrupted"`，带 `.path` 与 `.kind` ∈ {`cloud`, `relay_state`, `config`, `identity`, `recovery`, `deliveries`}），文件逐字节与 inode 均不变，不得回退成默认值后继续写入。子类：`crypto.IdentityCorruptError`、`ledger.LocalConfigCorruptError`、`delivery_state.DeliveryStateCorruptedError`。事务 `meta.json` 损坏时 `receive` 与 `recover --rollback` 都在任何写入之前以 `CorruptedRecoveryError`（`recovery_corrupted`）停止。同步检查点损坏为 `RelayError(code="checkpoint_corrupted")`。
- 只读严格加载器（从不写入）：`crypto.load_identity`、`relay_engine.load_cloud_config`、`relay_engine.load_relay_state`、`workspace_tx.load_transaction_meta`、`delivery_state.load`。`ensure_identity` 在内容不变时不再重写 `identity.json`。
- `RELAY_DESK_HOME` 下的 `cache/snapshots`、`cache/lineage`、`archives/<projectId>`、`transactions/`、`sync_publications/` 从 home 到目标的任何一级都不能是符号链接（`path_escape` / `symlink_in_path`）；home 本身可以是符号链接。缓存与归档目录（`cache/snapshots`、`cache/lineage`、`archives/<projectId>`）经过符号链接时抛出 `validation.LocalPathError`（`PathEscapeError` 子类，`category="needs_user_action"`，`code="local_dir_symlink"`，带 `.link` 与 `.target`）：这是本机目录问题，不是完整性失败。同步在下载载荷之前检查目标目录，被拒绝的投递记为 `deliveries.json` 的 `blocked_local`（不在 `needs` 中，不请求对端重新投递）；目录恢复为普通目录后，下一轮直接用同一个云端投递重新处理；若投递已离开收件箱，改记 `repair_pending` 并经 `needs` 请求。`cache/snapshots` 本身经过符号链接时，本轮不判断任何缓存丢失、不发布 have，并报告一条 `local_dir_symlink`。

---

## 3. 云端对象布局与协议协商

### 3.1 云端对象路径

- **v3 设备描述符**：`v3/devices/<deviceId>.json`（由设备自身 Ed25519 签名，包含 `protocolVersion: 3`、`capabilities: ["v2", "v3"]` 及可选 `rotatedAt`）。
- **v3 拥有快照清单**：`v3/have/<deviceId>.json`（Ed25519 签名 JSON）。当前写入 have v2，字段见 §4.4；旧格式只有 `formatVersion: 3`、`deviceId`、`snapshots: [versionId, ...]`、`updatedAt`。这是唯一被原地改写的对象，仅由该 `deviceId` 自身更新。
- **v3 个人收件箱投递**：`v3/inbox/<receiverDeviceId>/<deliveryId>/{package.enc, ready.json, receipt.json}`。
- **v3 签名祖先索引**：`v3/snapshots/<versionId>.json` 保存原作者签名的不可变快照元数据。接收端验证项目、版本和已配对作者后补齐父链，不下载祖先项目载荷。旧 v3 包可通过已有 `ready.json` 补齐索引；缺失证据报 `lineage_incomplete`，不得直接认定为分叉。`lineage_incomplete` 只表示读到了「不存在」；读取失败是暂时性错误，稍后重试。索引写入并读回后，版本图节点记 `recordPublished: true`。索引上传失败（或应答丢失）的 `publish` 仍保留该快照为本机当前版本，但不标记。`publish`、`send`（包括对卡住的交接重新运行 `send`）在链接父版本前，用本机缓存补写所有未确认的本机作者祖先索引；补写失败时不提交任何新内容。
- **同步端的祖先判定**：`sync` 记录新快照时，若本机版本图无法把它与本机当前版本或已有 head 放在同一条链上，先按同样的来源（`cache/lineage`、快照缓存、`v3/snapshots/`、`ready.json`）读取并验证缺少的祖先（作者已重新确认的历史公钥只作为 `trust: "historical"` 证据，不授予任何发送权限），只读取对方未经过的未知祖先。结果分三种：同一条链（不记录）；双方祖先都已知且互不可达 → `version_fork`（同上）；判定取决于找不到可验证记录的祖先 → 证据不足，记入 `relay_state.json` 的 `lineagePending`（`{projectId: {"<版本A>|<版本B>": {localVersion, incomingVersion, missing: {<版本>: {reason, detail}}, authorDeviceId, deliveryId, detectedAt, checkedAt}}}`），不写台账冲突，发出事件 `lineage_unproven`，`relay doctor` 报 `lineage_unproven`（警告）。之后每轮先用本机版本图复核（无 I/O），缺失证据最多每 600 秒或版本图增长时重新读取一次；证明为同一条链或分叉后移除记录并发出 `lineage_resolved`（分叉时同时记台账冲突和 `fork_detected`）。
- **缓存投递检查点**：本机 `sync_publications/<投递路径SHA-256>/` 保存准备好的 `package.enc` 和 `ready.json`，上传前整体落盘。任何重试沿用原密文与签名，已发布标记不允许覆盖。
  - 活跃交接使用 `deliveryId = relayId`（`r_<YYYYMMDD>_<8位>`），重新签发的交接是新的 `relayId`。
  - 缓存投递编号：`(sync|fwd)_<versionId>_<发送端deviceId后6位>_k<接收方公钥指纹前8位小写>[_g<代>]`，例如 `fwd_snap_20261119_ab12cd34_a1b2c3_k0f3e9a12_g2`。`sync_` 表示发送端就是快照作者，`fwd_` 表示转发他人的快照；第 1 代省略 `_g1`。编号绑定接收方公钥，换钥后不可能复用旧公钥的编号；去重键为（接收设备、接收方指纹、快照），记录在 `deliveries.json`（§2.4）。
  - 旧格式 `sync_<versionId>_<后6位>`、`fwd_<versionId>_<后6位>` 的已有投递照常读取，并按 ready 中的 `recipientKeyFingerprint` 采用为已有记录，不重复投递。
  - **新一代投递**（新编号、新密文，旧对象不动）只在以下情况产生：该（设备、指纹、快照）没有任何记录且未被覆盖（`new`）；对端在序号更新的 have 中把快照列入 `needs`（`peer_needs`，同一 have 序号只响应一次）；本机换钥后尚未确认的旧代（`sender_key_rotated`）；已发布超过 10 分钟仍未确认、云端对象缺失且本机没有检查点（`objects_missing`）。有检查点时按原字节补传，不算新代（`objects_restored`）。超过 4 代仍未确认报 `repeated_redelivery`（需要用户处理）。
  - 崩溃顺序：先把代次（含投递编号）写成 `preparing`，再加密、写检查点、上传包、上传 ready；`preparing` 一律沿用原编号续传，检查点存在时不重新加密。
- **保留的 v2 兼容路径**：
  - 当本机别名为 `a` 或 `b` 时同时发布 `devices/{a,b}.json`。
  - 对仅支持 `"v2"` 的对端保留 `relays/<relayId>/{package.enc, ready.json, receipt.json}`。

### 3.2 能力协商与绝不静默降级原则

- 优先检测对端已验签描述符中的 `capabilities`：包含 `"v3"` 时严格走 `v3/inbox/<deviceId>/<relayId>/` 协议；**v3 传输失败时绝不降级回退到 `relays/`**。
- 当对端仅支持 `"v2"` 且双方别名均在 `("a", "b")` 内时，自动启用 v2 协议适配器（`relays/<relayId>/`）。
- 若任一方使用动态别名（不在 `a`/`b` 内）而对端仅支持 v2，则拒绝发送并抛出 `v2_incompatible_alias`，提示升级旧版设备。

---

## 4. 核心签名与清单 Schema

### 4.1 作者签名快照 (`snapshot`)

由 `crypto.sign_snapshot` / `verify_snapshot` 强制校验，`authorDeviceId` 必须与 `signerDeviceId` 一致：

```json
{
  "formatVersion": 3,
  "projectId": "demo",
  "projectName": "Demo App",
  "versionId": "snap_20261118_ab12cd34",
  "authorDeviceId": "dev_...",
  "authorAlias": "a",
  "parentVersionIds": ["snap_20261117_00112233"],
  "parentRelayId": "r_20261117_99887766",
  "relayId": "",
  "contentSha256": "<64-hex SHA-256 of plaintext payload.tar.gz>",
  "contentSize": 12345,
  "createdAt": "2026-11-18T00:00:00.000Z",
  "signerDeviceId": "dev_...",
  "signature": "<base64 Ed25519>"
}
```
- `publish` 生成的快照 `relayId` 为 `""`；`send` 生成的快照携带该次交接的 `relayId`。

### 4.2 发送方/转发方签名就绪清单 (`ready.json`, v3)

包含 `encrypt_package` 生成的加密信封字段与投递元数据，并由当前发送方或转发方 Ed25519 签名：

- **信封字段**：`packageSize`、`packageSha256`、`streamHeader`、`wrappedKeyRecipient`、`wrappedKeySender`、`senderDeviceId`、`recipientDeviceId`、`chunkSize`（默认 `65536`）。
- **投递与授权字段**：
  - `formatVersion`: `3`
  - `purpose`: `"handoff"`（活跃工作区交接）或 `"sync"`（快照缓存同步/转发）
  - `deliveryId`, `relayId`
  - `senderDeviceId`, `senderAlias`, `fromAlias`, `fromDeviceId`, `toAlias`, `toDeviceId`
  - `authorizedDeviceId`: `purpose == "handoff"` 时为目标 `deviceId`；`purpose == "sync"` 时为 `""`
  - `parentRelayId`（handoff 携带）、`isForwarded` (`true` 表示离线转发，`false` 表示直接发送/发布)、`createdAt`、`packageKey`
  - `snapshot`: 完整的作者签名 `snapshot` 对象（转发时保持原作者签名不变）
  - `meta`: `encrypt_meta` 加密的项目摘要（`projectId`, `projectName`, `lastTask`, `nextAction`, `branch`, `dirty`, `sessionCount`）
  - `recipientKeyFingerprint`: 加密时使用的接收方公钥指纹（同步投递与 v3 交接都带；旧 v3 接收方忽略）。接收方在解封之前比对：不是本机当前指纹时，同步投递记为过期（`obsolete(recipient_key_retired)`）并通过 have `needs` 请求补发，交接则需要发送方重新签发。
  - `supersedesRelayId`、`supersedesRelayIds`: 仅重新签发的交接带有，分别为直接取代的原交接与完整取代链（§5.6）。
  - `signerDeviceId`, `signature`
- **接收端交叉校验**：`relayId` 必须等于云端目录名；签名设备、`fromDeviceId`/`senderDeviceId`、`toDeviceId`/`recipientDeviceId`、`authorizedDeviceId`、`packageKey` 相互一致；`snapshot.projectId` 与载荷 metadata、对话归档、加密 meta 一致；快照 `relayId` 非空时必须等于交接编号（重新签发例外见 §5.6）。

### 4.3 接收方签名签收回执 (`receipt.json`, v3)

```json
{
  "formatVersion": 3,
  "purpose": "handoff",
  "relayId": "r_20261118_ab12cd34",
  "projectId": "demo",
  "snapshotId": "snap_20261118_ab12cd34",
  "fromAlias": "a",
  "fromDeviceId": "dev_a...",
  "toAlias": "b",
  "receiverDeviceId": "dev_b...",
  "packageSha256": "<64-hex SHA-256 of package.enc>",
  "receivedAt": "2026-11-18T00:05:00.000Z",
  "signerDeviceId": "dev_b...",
  "signature": "<base64 Ed25519>"
}
```

回执由接收方当前公钥验签。接收方领取后才换钥时，回执可由 `keyHistory` 中的历史公钥验证，但只对绑定了该公钥指纹的交接接受（旧记录没有 `recipientKeyFingerprint` 时，按 `sentAt` 是否落在该公钥在本机的确认期内判断）。

### 4.4 缓存清单 have v2 (`v3/have/<deviceId>.json`)

```json
{
  "formatVersion": 3,
  "haveFormat": 2,
  "deviceId": "dev_b...",
  "keyFingerprint": "6F96-62CE-04E8-FD9D",
  "seq": 12,
  "snapshots": ["snap_20261119_aroemag8", "snap_20261119_yymdp9nq"],
  "needs": ["snap_20261119_mqisvue1"],
  "updatedAt": "2026-11-19T02:29:39.169Z",
  "signerDeviceId": "dev_b...",
  "signature": "<base64 Ed25519>"
}
```

- `snapshots` 仍是字符串列表，旧版读取方照常解析；只列出摘要校验通过的缓存。
- `seq` 先持久化再上传，单调递增；丢失 `deliveries.json` 后从本机已发布的 have 继续，不会回退。
- `needs`（最多 200 个）：本机丢失、损坏、无法验证或收到过期投递的快照（不含 `prunedSnapshots` 中有意清理、且不是当前版本或头版本的快照）。
- 内容摘要（deviceId、指纹、快照、needs）变化或距上次上传超过 900 秒才重新上传；上传失败只记录，不阻止转发。
- **读取对端 have**：不存在 → 状态未知；传输失败 → 该对端退避；只能由历史公钥验证、`keyFingerprint` 与固定指纹不符、或 `seq` 小于已见值 → 视为过期，忽略且不告警；无 `seq` 的旧格式可确认缓存但不触发重发（见过带序号的 have 后再出现旧格式视为过期）；JSON 解析失败先按暂时性 `torn_read`，连续 3 次升级为完整性 `remote_json_invalid`。
- **三种投递状态**互相独立：「已发布」（本机检查点与云端 ready 字节一致）、「对端已验证缓存」（对端用当前固定公钥验签、指纹相符、序号不倒退、状态 `paired` 的 have 列出该快照，记为 `peer_verified`，只能被更新的 have 中的 `needs` 推翻）、「已接棒」（接收方签名、逐项匹配的回执）。have 永远不会签收交接或改变台账所在设备；回执也不推出其他快照已缓存。

### 4.5 标识符与字段校验

签名验证通过后，`crypto.verify_snapshot` 与各接收、同步、缓存、恢复入口在首次写入前仍检查字段类型、长度、路径合法性与关联一致性（`relaydesk/validation.py`）。违反时抛出 `validation.ValidationError` 子类（`category="integrity"`），签名对象内的违规由 `crypto.InvalidSignedFieldError`（`TamperError` 子类）透传 code：

| 类型 | 规则 |
| --- | --- |
| token：`versionId`、`deviceId`、`relayId` | ASCII 字母、数字或 `_.-`；以字母或数字开头；不含 `..`；不以 `.` 结尾；不是 Windows 设备名；`versionId`、`deviceId` 不超过 128 字符；用户输入的 `relayId` 不超过 80 字符 |
| 投递编号 | 同 token 规则，上限 160 字符（不限定 `sync_`/`fwd_` 形状） |
| `projectId` | Unicode 字母、数字、组合符及 `_ - .` 和空格；必须 NFKC 稳定（拒绝 NFD 形式与全角符号）；首尾不能是点或空格，开头不能是 `-`；不含 `..`；不是设备名；不超过 128 字符 |
| `parentVersionIds` | 列表；每项合法、不重复、不等于自身；最多 64 项 |
| 路径 | 拒绝绝对路径、`..`、反斜杠、盘符、UNC、NUL；统一 NFC；落盘路径必须位于指定根目录内，且从根到目标的任何一级都不能是符号链接 |
| 载荷中的符号链接 | 目标必须是相对路径且不逃逸项目；符号链接条目下方不允许写入 |

错误 code：`invalid_identifier`、`field_mismatch`、`path_escape`、`symlink_in_path`、`invalid_path`（`invalid_input` 为基类）。收件箱列举时投递编号不合法的目录不下载，记为 `invalid_identifier`。设备描述文件 `v3/devices/<x>.json` 内的 `deviceId` 必须与键名一致且合法，否则发现时跳过。

---

## 5. 交接授权、迟到回执与版本谱系冲突规则

1. **单项目唯一活跃交接**：若 `pendingSends[proj_id]` 或 `activeHandoffs[proj_id]`（且对应 `sentRelays[rid]` 未 `acked`）存在，向**另一台**设备调用 `send` 会抛出 `ActiveHandoffConflictError`（`code="active_handoff_exists"`）；向**同一**目标设备重试则幂等沿用或补发原交接包。
2. **独占接收授权**：`list_inbox` 忽略 `sync_`/`fwd_` 及 `purpose != "handoff"` 的缓存投递；`receive` 拒绝 `purpose != "handoff"`、`authorizedDeviceId != 本机 deviceId` 或仅存在于本地缓存未授权的交接包（`UnauthorizedReceiverError`）。
3. **迟到回执 (`late` receipt)**：在 `sync_receipts` 中，若 `activeHandoffs[proj_id]` 已指向更新的交接，或 `_is_version_ancestor(versionGraph[proj_id], sent_snap_id, localCurrentVersion[proj_id])` 为真，则标记 `late: true`——仅回填 `projects[].history` 并标记 `sentRelays[rid]["acked"] = True`，**绝不回退或覆盖**项目当前的 `device` 所有权与新 `handoff` 状态。
4. **有向无环版本谱系校验 (`versionGraph`)**：
   - `project["history"]` 仅保留最近 10 条用于展示；**版本防回退、防重放与分叉检测完全基于 `relay_state.json` 中作者签名的 `versionGraph` 与 `localCurrentVersion`**，不受 10 条截断影响。
   - **版本回退/重放 (`version_regression`)**：当 `incoming == localCurrentVersion`（未指定 `--into`）或 `_is_version_ancestor(graph, incoming, localCurrentVersion)` 为真时，直接拒绝接收。
   - **并发分叉 (`version_fork`)**：当 `localCurrentVersion` 与 `incoming` 互不为祖先时（或 `sync_network` 发现新 head 与本地/已有 head 无祖先关系时），将新版本节点记入 `versionGraph`、在 `ledger` 的 `projects[].conflicts` 写入 `kind: "version_fork"` 记录，并**完整保留当前工作区不改动**。`receive` 抛出 `VersionConflictError`（`code="version_fork"`），提示用户使用 `receive <RELAY_ID> --into <新目录>` 隔离落盘对比。
5. **作者公钥与历史证据**：所有作者验证（同步入站、转发、父链、收件箱、接棒）统一按以下顺序：本机作者用当前身份公钥，再用 `previousKeys`；已配对作者用固定公钥，再用重新确认过的 `keyHistory`（得到 `trust: "historical"`）；`key_changed`（未重新确认）的作者只接受本机版本图已记录且 `recordSha256` 一致的旧快照（`attested`），不获得任何新信任；其他一律 `untrusted_author`。历史公钥只证明「该记录在该公钥被确认期间就存在」：可用于缓存、转发与父链；交接中的历史快照只有作者本人发送时接受（否则 `historical_snapshot_not_current`）；ready、have、设备描述文件不接受历史公钥；回执规则见 §4.3。
6. **重新签发与取代（`send --reissue`）**：
   - 只处理当前密钥已无法验证或打开的在途交接（`handoff_recipient_key_changed`：ready 绑定的接收方指纹不是目标当前指纹；`handoff_sender_key_rotated`：ready 由本机旧密钥签名）；否则 `reissue_not_needed`。普通 `send` 只报出这两个错误与处理命令，从不自动重新签发。目标必须是已重新确认的 `paired`。
   - 拒绝且不写任何状态：`nothing_to_reissue`、`handoff_already_claimed`、`handoff_already_superseded`、`reissue_receipt_exists`（云端已有原交接回执但本机未确认，先 `relay sync`）、`reissue_payload_missing`（本机缓存没有完整可验证的同一快照载荷）、`reissue_not_supported`（旧版协议或快照不是本机所写）。
   - 产物：新 `relayId`、用目标当前公钥的新密文、同一个签名快照；新 ready 带签名字段 `supersedesRelayId`（直接取代的交接）和 `supersedesRelayIds`（完整取代链）以及新的 `recipientKeyFingerprint`。原交接包、ready 与任何回执不动；原发送记录加 `supersededBy`；`activeHandoffs` 与台账在途交接指向新交接。
   - 崩溃安全：先写 `pendingSends`（含 `reissueOf` 与原交接信息），再上传包、写 `prepared`、发布 ready；中断后再次 `--reissue` 沿用同一新交接编号。
   - 阶段 A 的「快照 `relayId` 必须等于交接编号」规则只对这样的 ready 放宽：由快照作者本人以当前密钥签发，且 `supersedesRelayIds` 含 `snapshot.relayId`。
   - **只能领取一个**：领取重新签发的交接前，若本机已领取（或正在领取、或云端有本机签的回执）取代链中的任何交接，或已领取取代同一原交接的另一份重新签发 → `superseded_original_claimed`（在下载加密包之前）；领取原交接前，若本机已领取取代它的交接 → `handoff_superseded`。发送方换钥、目标在重新确认前已用旧固定公钥领取原交接时，发送方以原交接回执为准，把重新签发记为 `withdrawn`（先领取的生效）。非目标设备仍不能领取。
   - 目标设备的 `list_inbox` 只列出重新签发的交接，被取代的原交接列在 `superseded`（`{relayId, supersededBy}`）；同步把被取代的原交接记为 `obsolete(superseded)`，不再报为问题。被取代或作废的发送记录不再阻止 `publish`，也不再让缓存清理保留其载荷。

---

## 6. 工作区事务与崩溃恢复 (`workspace_tx.py`)

事务目录位于 `<local_home>/transactions/<relay_id>/`（若使用 `--into` 指向不同路径且原事务目录仍存在，则使用 `<relay_id>__into_<sha256前8位>` 隔离），包含 `journal.json`（含规范 JSON 计算的 `journalSha256`）、`meta.json`、`pre_blobs/` 与 `post_blobs/`：

1. **阶段 A（隔离准备，未改动目标目录）**：
   - **新目录**：在目标父目录创建临时兄弟目录并应用载荷，记录 `postFiles`、`post_blobs/`、Git 状态与目录身份。目录已原子移动而完成检查点尚未写入时，可通过原目录身份识别成功应用，继续后续阶段并保留用户的新编辑。
   - **已有目录**：扫描原目录并分离 `preFiles` 与 `preservedEntries`（敏感文件与 `.gitignore` 忽略文件，永久保留不动）；若为 `git` 模式，读取并备份 `.git/index`（`pre_blobs/git_index.bin`）、`preHead`、`preRefs`；在隔离临时目录克隆 `.git` 并试运行 `apply_verified_payload`，仅对发生修改或删除的文件保存 `pre_blobs/`、对新增或修改的文件保存 `post_blobs/`，计算 `fileOps`（先 `delete` 后 `create`/`modify`）与 `git` 元数据（`postIndexEntries`、`postIndexBlob`、`repo.bundle`、`stagedBlobs`、`affectedRefs`、`untouchedRefs`）。
   - 最后原子写入 `journal.json`（`phase: "PREPARED"`，附带 `journalSha256`）。
2. **阶段 B（日志保护提交）**：
   - 状态机推进：`PREPARED` → `APPLYING_FILES` → `APPLYING_GIT`（通过 `refs/relay-tx-objects/` 临时命名空间解包 `repo.bundle`、写入 `stagedBlobs` Git 对象、更新 `affectedRefs`、清理临时引用、更新 `HEAD` 并原子替换 `.git/index`）→ `WORKSPACE_COMMITTED` → `ARCHIVED`（合并对话归档）→ `UPDATING_LEDGER` → `LEDGER_UPDATED` → `COMPLETED`（清理恢复数据及临时写入目录）。
   - 文件内容先完整写入目标文件系统上、日志明确登记的 `writeStagingRel` 临时目录，再原子替换目标；恢复只排除这个事务拥有的目录，不忽略任意本机新增文件。日志格式为 2，兼容读取格式 1；新日志不支持交回旧引擎恢复。
   - 回滚先持久化 `ROLLING_BACK`，再还原文件和 Git 状态，最后写入 `ROLLED_BACK`。中断后只能继续回滚，不得进入签收流程。`UPDATING_LEDGER` 及之后的接收阶段只允许继续完成接收，避免回滚与已写入台账或云端回执矛盾。
3. **崩溃恢复、漂移检测与日志损坏保护**：
   - **损坏立即停机 (`CorruptedRecoveryError`, `code="recovery_corrupted"`)**：加载时强制校验 `journal.json` 必填字段、`journalSha256` 及所需 `pre_blobs`/`post_blobs`/`bundle` 的 SHA-256，任何篡改或缺失均拒绝恢复或回滚。
   - **中断后用户漂移保护 (`RecoveryConflictError`, `code="recovery_conflict"`)**：`_check_workspace_drift` 检查常规文件的新增、修改、删除以及 `.git/index`、`HEAD` 与 `refs` 变更（预提交阶段要求每项匹配 `pre` 或 `post`；提交后回滚要求严格匹配 `post`）。检测到用户中断后人工改动时拒绝自动覆盖或回滚，完整保留现场并提示 `--into`。
   - **确定性故障注入**：支持环境变量 `RELAY_DESK_FAULT_POINT`（如 `tx_after_journal_before_apply`、`tx_before_file_replace`、`tx_mid_files`、`tx_after_files_before_git`、`tx_after_git_refs_before_index`、`tx_after_workspace_commit`、`tx_before_rollback`、`tx_mid_rollback`、`rx_before_archive`、`rx_before_ledger`、`rx_before_receipt`；同步投递 `sync_fwd_after_entry_preparing`、`sync_fwd_after_checkpoint`、`sync_fwd_after_package_upload`、`sync_fwd_after_ready_upload`、`sync_rx_after_cache_save`、`sync_rx_after_state_update`、`sync_have_before_upload`；缓存清理 `prune_after_lineage`、`prune_after_mark`、`prune_after_rename`、`prune_mid_delete`、`prune_ckpt_after_rename`、`prune_before_finish`）及 `RELAY_DESK_FAULT_MODE=sigkill`。
   - **路径约束**：日志中的全部路径、blob 引用、Git 引用名和 `targetPath` 在加载时校验；所有文件操作经过 `_target_join`，拒绝经符号链接父目录写入；`transactions/` 本身不能是符号链接。
4. **保留条目（`preservedEntries`）与原地接收**：
   - 原地接收到已有 Git 工作区时，Git 忽略文件与凭据文件进入 `preservedEntries`，不进入 `preFiles`/`fileOps`，应用、回滚和继续恢复都不碰它们；`_check_workspace_drift` 也跳过它们。因此忽略文件不再导致 `target_dirty`（接收路径调用 `check_target_clean_for_receive(preserve_ignored=True)`）；未提交改动、未忽略的未跟踪文件、本地提交与引用冲突仍然拒绝。直接调用 `apply_verified_payload` 时默认仍拒绝忽略文件，因为该路径会删除它们。
   - 写入 `journal.json` 之前，按隔离目录试运行得到的 `postFiles` 检查与保留条目的冲突，任一命中即拒绝（`TargetDirtyError`，目标目录完全不变，可用 `--into`）：(1) 交接包在保留文件的同一路径写文件或符号链接；(2) 交接包需要在保留文件的位置建目录；(3) 交接包在含保留文件的目录位置写文件；(4) 交接包的忽略规则不再忽略某个本机忽略文件（凭据除外）——检查前先把目标中保留的 `.gitignore` 复制进隔离目录。比较按 NFC，文件系统不区分大小写时按 casefold。
   - 中断期间新建、不在 `preservedEntries` 中的忽略文件仍被漂移检查视为本机新增文件，恢复与回滚会拒绝并提示 `--into`（保守行为）。

---

## 7. 模块接口摘要

- `relaydesk/devices.py`：设备别名校验 (`ALIAS_RE`)、设备与目标解析 (`resolve_device_entry`, `resolve_target_device`)、v2→v3 配置/状态/台账迁移与一次性备份 (`ensure_backup_once`)。
- `relaydesk/crypto.py`：密钥轮换 (`rotate_identity_keys`)、v2/v3 公钥描述符 (`public_descriptor`, `public_descriptor_v3`)、快照签名验签 (`sign_snapshot`, `verify_snapshot`)、流式加密 (`encrypt_package`, `decrypt_package`)。
- `relaydesk/workspace_tx.py`：两阶段事务提交 (`prepare_and_apply_workspace`)、检查 (`inspect_transaction`)、断点继续 (`resume_transaction`) 与回滚 (`rollback_transaction`)。
- `relaydesk/relay_engine.py`：N 设备配对 (`pair_device`)、快照发布 (`publish_project`)、网络同步与离线转发 (`sync_network`，逐轮驱动 `SyncSession`)、交棒与重新签发 (`send_project`，`reissue=True`)、收件箱扫描 (`list_inbox`)、事务接收 (`receive_relay`)、恢复 (`recover_relay`) 与对话检索 (`query_transcripts`)；只读加载器 `load_cloud_config`、`load_relay_state`。
- `relaydesk/validation.py`：标识符规则、跨字段一致性、相对路径规范化、落盘路径约束、本机 JSON 状态「未创建 / 已损坏」判定与错误类型。
- `relaydesk/sync_runtime.py`：错误分类 `classify_error`、有界事件环 `EventLog`、退避 `Backoff`、对端健康表、完整性隔离区、完成状态与退出码、`sync_status.json` 读写。
- `relaydesk/delivery_state.py`：`deliveries.json` 的严格加载、锁与原子写入，投递编号生成与解析，缓存摘要备忘，供清理使用的 `retention_inputs`、`delivery_view`、`mark_pruned`。
- `relaydesk/diagnostics.py`：共享的「原因 + 处理命令」建议表、只读 `run_doctor()`、按项目的网络视图、换钥保护 `rotation_blockers()`。
- `relaydesk/cache_prune.py`：只读盘点、保留集合与清理计划、`cache_status()`、`prune()`（默认预览，`apply` 在锁内重算）。

---

## 8. 同步事件、错误分类与缓存清理

### 8.1 每轮流程（`SyncSession.step`）

1. **信任重查**：只读读取 `identity.json`、`cloud.json`、`relay_state.json`（从不调用 `ensure_identity`）。`deviceId` 被替换 → 结束运行（`identity_replaced`）；本机换钥 → 切换内存身份、重置全部对端健康状态并用新密钥重签 have；连接参数变化 → 重建存储提供方；对端状态、指纹、公钥或能力变化 → 重置该对端并清空其隔离项；`key_changed` 对端每轮停放。任一文件损坏或缺失：本轮不加锁、不写入，记为全局问题。
2. **加锁**：只在本轮内持有 `workspace-operation.lock`；被占用则本轮跳过（`round_skipped`，`local_busy`）。
3. **回执**：逐个交接验签并与发送记录逐项一致后才确认；失败归到该交接的目标对端。
4. **收件箱列举**：失败即全局失败，本轮中止（`roundsAborted`）。
5. **入站**：每个投递单独处理；传输失败让发送方退避；完整性失败按（ready 与包的 size/etag/sha256/mtime）隔离；未配对或换钥的发送方停放；解封前比对 `recipientKeyFingerprint`。
6. **have 发布**（§4.4）。
7. **转发**：逐个对端读取其 have，单个快照失败只影响该（对端，快照）。
8. 本轮有操作成功且没有失败的对端恢复为健康。

同步进程从不应用工作区、从不上传 `receipt.json`。

### 8.2 事件与汇总

- 事件（`schema: relaydesk.sync/1`）：`runId`、连续的 `seq`、`at`、`round`、`kind`，以及各类事件字段。`kind` 包括 `run_started`、`snapshot_cached`（`repaired: true` 表示修复本机缓存）、`snapshot_forwarded`、`receipt_synced`、`fork_detected`、`lineage_unproven`、`lineage_resolved`、`peer_verified`、`delivery_obsolete`、`have_published`、`problem`、`resolved`、`peer_state`、`trust_changed`、`round_skipped`、`backoff`、`round_finished`、`summary`。
- 汇总（`schema: relaydesk.sync.summary/1`）：保留原字段 `ok`、`cycles`、`syncedReceipts`、`syncedSnapshots`、`forwarded`、`conflictsDetected`、`errors`（条目新增 `errorClass`、`code`、`stage`、`peerDeviceId`）；新增 `lineageUnproven`（证据不足的版本对）、`status`、`counters`（`rounds`、`roundsSkippedBusy`、`roundsAborted`、`receiptsSynced`、`snapshotsCached`、`snapshotsRepaired`、`forwarded`、`peerVerified`、`forksDetected`、`lineageUnproven`、`havePublished`、`retriesScheduled`、按类别的 `failedOperations`/`problemsOpened`、`problemsResolved`、`eventsEmitted`、`eventsNotRetained`）、`recentEvents`（≤200）、`recentEventsTruncated`、`recentEventsDropped`、`unresolved`、`unresolvedTruncated`、`peers`、`legacyListsTruncated`、`runId`、`mode`、`startedAt`、`finishedAt`。`ok` 等于 `status == "success"`。
- 旧列表字段各保留最近 200 条；`errors` 只在问题首次出现或类别变化时追加。问题 key 约定为 `stage|peerDeviceId|objectId`，对端级问题为 `peer|<deviceId>|trust`。
- CLI：`--json` 结束时输出一个汇总对象；`--jsonl` 逐条输出事件，最后一行 `{"kind": "summary", "summary": {…}}`（汇总中省略已逐条输出的 `recentEvents`）；两者互斥。首轮前出错时 `--json` 输出 `{"ok": false, "status", "code", "errorClass", "action"}`，`--jsonl` 输出一行 `kind: "error"`。
- 本机 API：`POST /api/cloud/sync/watch`（`{interval, maxBackoff, project}`，2 ≤ interval ≤ maxBackoff ≤ 3600）、`GET /api/cloud/sync/watch?since=SEQ`（返回 `events` 与 `gap`）、`POST /api/cloud/sync/watch/stop`；watch 运行中 `POST /api/cloud/sync` 返回 409 `watch_running`；`/api/cloud/status` 带 `syncWatch` 与 `lastSync`；云端错误体带 `errorClass`。

### 8.3 错误类别、完成状态与退出码

| 类别 | 代表 code | 处理 |
| --- | --- | --- |
| `transient_transport` | `storage_transport`、`listing_lag`、`local_io_retry`、`torn_read`、`unexpected` | 上限退避加等量抖动；`unexpected` 连续 3 次升级为 `needs_user_action`/`internal_error`，`torn_read` 连续 3 次升级为 `integrity`/`remote_json_invalid` |
| `needs_user_action` | `not_configured`、`need_device`、`peer_key_changed`、`key_changed`、`untrusted_sender`、`untrusted_author`、`local_state_corrupted`、`recovery_corrupted`、`delivery_state_corrupted`、`storage_auth`、`storage_config`、`disk_full`、`permission_denied`、`repeated_redelivery`、`handoff_recipient_key_changed`、`handoff_sender_key_rotated`、`historical_snapshot_not_current`、`signed_by_retired_key`、`sealed_to_retired_key`、`local_dir_symlink` 等 | 不循环重试；附 `action` 处理命令；对端级问题停放该对端 |
| `integrity` | `signature_invalid`、`digest_mismatch`、`unseal_failed`、`tamper`、`invalid_identifier`、`field_mismatch`、`path_escape`、`symlink_in_path`（载荷或检查点路径；本机缓存与归档目录为 `local_dir_symlink`）、`invalid_path`、`checkpoint_corrupted`、`remote_json_invalid`、`receipt_mismatch`、`invalid_key` | 按对象标签隔离，对象不变不再下载；同一问题只报一次（新进程中隔离项再报一次） |
| `local_busy` | `busy`、`lock_timeout` | 跳过本轮；连续忙只提示一次 |

对端签名对象验签失败时先分诊（§2.2）：描述文件公钥变化 → `peer_key_changed`；能由该对端 `keyHistory` 验证 → 过期对象，不是问题。

完成状态：有 `needs_user_action` 或 `integrity` 未解决 → `needs_action`；只剩 `transient_transport` 或 `local_busy` → `partial`；没有 → `success`；被取消 → `cancelled`。CLI 退出码：`success` 0、`partial` 3、`needs_action` 4、`cancelled` 130；首轮前需要用户处理的错误 4，其他用法错误 1，台账或文件读写错误 2。退避上限为 `--max-backoff`（默认 300 秒，不小于 `--interval`），成功后恢复正常间隔；全局需要处理的问题不结束 watch，每 `maxBackoff` 秒重查一次。

`sync_status.json` 在运行中有变化时最多每 10 秒写一次，无变化时每 60 秒写一次心跳，结束时必写；超过 3 × max(interval, maxBackoff) 无心跳视为过期。

### 8.4 诊断与缓存清理

- `relay doctor [PROJECT] [--json]`（`schema: relaydesk.doctor/1`）：严格只读，不加锁、不建目录、不写摘要备忘、不联网。每条 finding 含 `severity`（`info`/`warning`/`error`）、稳定 `code`、`reason`、恰好一条 `command`，以及可用的 `projectId`、`relayId`、`deviceId`、`path`。原因与命令来自 `diagnostics.ADVICE`，CLI 错误、API 错误体的 `action` 与网页使用同一来源。退出码：只有 `info` 为 0，有 `warning` 为 3，有 `error` 为 4，项目不存在为 1。Fix G 新增 finding：`lineage_unproven`（警告，证据不足而非分叉）、`local_dir_symlink`（错误，给出把目录移回原处的命令）、`local_retry_pending`（提示，目录已恢复，下次同步重新处理原投递）、`project_id_invalid`（警告，台账中已有的不合规项目 ID，给出 `relay add … --id <新项目ID>`）。`projects[].devices[].status` ∈ `confirmed`（已确认缓存）、`awaiting`（已投递等待确认）、`resend`（待补发）、`unknown`（没有该设备最近的已验证 have，从不显示为离线或缺少快照）、`needs_action`。
- `relay cache status [PROJECT] [--json]`：只读统计快照载荷、签名元数据、密文检查点、恢复事务与清理残留的数量和字节数，以及按当前保留集合可释放的量。
- `relay cache prune [PROJECT] [--dry-run | --apply] [--expect-plan DIGEST] [--json]`：默认预览，不写任何文件，返回 `planDigest`。`--apply` 在 `workspace-operation.lock` 内重新读取全部输入并重算计划（忙则 `busy`），从不重放预览列表；给了 `--expect-plan` 且计划已变化 → `prune_plan_changed`，不删除（CLI 退出码 3，API 409）。结果状态：`success`（0）、`partial`（3）、`blocked`（本机状态损坏为 4；没有 `deliveries.json` 为 3），被阻止时什么也不删。
- **保留集合**：各项目当前版本与全部版本图头（含分叉头）；台账中未解决 `version_fork` 两侧；待完成发送、未签收且未被取代的交接、未上传回执的接收、未完成接收事务的快照及其回滚目标（`transactions/*/meta.json` 的 `extra.snapshot`、`extra.prevLocalVersion`）；已配对对端当前公钥尚未确认的投递；本机或对端（当前公钥验签的新鲜 have）`needs` 中的快照；`preparing` 代次与接收方当前公钥尚未确认的已发布代次的检查点；全部 `cache/lineage`。损坏、目录名非法、符号链接、含非缓存文件（如凭据）、谱系记录冲突、没有投递记录或与记录不符的检查点保留并在 `unclear` 中报告；有无法读取的恢复事务时本次不清理任何载荷。
- **检查点删除条件**：代次为 `published`（或从未续传的 `abandoned`），检查点 ready 的投递编号与包摘要和代次记录一致；并且接收方当前公钥已确认缓存、接收方已换钥使旧投递作废、接收设备已不在配对列表，或已被新一代取代。
- **从不触碰**：`relay_state.json`（含 `processedDeliveries` 防重放记录）、`cloud.json`、`identity.json`、`deliveries.json` 中除 `prunedSnapshots` 外的记录、`transactions/`、`archives/`、凭据、任何云端对象。清理不联网，have 在下一次 `relay sync` 时重新发布。
- **删除顺序与中断恢复**：每个载荷先把 `snapshot.json` 原样保存为 `cache/lineage/<versionId>.json` 并回读比较 → 在 `prunedSnapshots` 登记 → 改名为 `.pruning-<名称>.<随机>` 墓碑 → 删除墓碑；检查点改名为墓碑后删除；最后撤销仍有载荷且属于保留集合的快照的清理标记。中断后 `relay doctor` 报 `prune_interrupted`，再次 `--apply` 重新扫描并完成；清理中断不会被误认为接棒完成。
- **本机 API**：`GET /api/cloud/doctor?project=`（与 `relay doctor --json` 同一函数）、`GET /api/cloud/cache?project=`、`POST /api/cloud/cache/prune`（`{mode: "dry-run" | "apply", project?, expectPlan?}`）；`POST /api/cloud/send` 接受布尔 `reissue: true`；错误体带 `action`。网页只调用预览，从不执行清理。

---

## 9. 兼容性

- **§2 产品约定保持不变**：稳定 `deviceId`、各对端独立核对指纹；最新版本由签名父链确定；`publish`/`sync` 只传播快照，`send --to` 授权指定设备接棒；已发布包不可变，回执匹配项目、交接编号、接收设备、快照和包摘要；单对端可省略目标；不承诺全网严格互斥或恰好一次投递。
- **恢复日志**：写格式 2，读取格式 1 和 2。回滚中断继续执行 `recover --rollback`；开始更新台账后通过 `receive` 完成交接。待恢复的新日志不能交给旧引擎处理。
- **旧 A/B 协议不变**：v2 ready、v2 回执、`devices/{a,b}.json`、`relays/<relayId>/` 路径不变。
- **与较早 v3 引擎混用**：新投递编号保留 `sync_`/`fwd_` 前缀，旧版收件箱照常把它当缓存投递跳过，常规快照编号下的投递编号（约 45–50 字符）也在旧版 80 字符限制内；have v2 的 `snapshots` 仍是字符串列表；旧版忽略 `recipientKeyFingerprint`、`haveFormat`、`seq`、`needs`、`keyHistory`、`previousKeys` 等新字段。旧版不发布 `seq`/`needs`，其缓存丢失需要升级后才能自修复；旧版不认识 `supersedesRelayIds`，会按快照 `relayId` 绑定规则拒绝重新签发的交接（失败方向安全）。
- **本机状态**：`relay_state.json`、`cloud.json` 版本仍为 3，新增字段对旧版透明；`deliveries.json` 为新文件；`forwardedSnapshots` 仍持续追加供降级使用。迁移细节见 [MIGRATION.md](./MIGRATION.md)。
