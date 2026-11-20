# relay-desk

**English** | [简体中文](./README.zh-CN.md) | [Changelog](./CHANGELOG.md)

relay-desk moves an in-progress Git project or folder between your computers. Use the CLI, the local web workbench, or skills for Claude Code, Codex, and Antigravity. The manual checklist supports offline transfers. Cloud handoffs encrypt workspace changes and archive supported conversation logs for later search.

## What’s new in v3

- **Multi-device relay.** Pair devices by name, publish signed snapshots, and forward cached snapshots even when the original device is offline. An active handoff can be claimed only by its intended recipient.
- **Continuous sync and recovery.** Run `sync --watch` for per-peer retries and stream events with `--jsonl`. Interrupted receives can resume from a local transaction journal.
- **Key rotation and local maintenance.** Reconfirm a peer’s new key to redeliver cached snapshots. Reissue pending handoffs with `send --reissue`, inspect state with `doctor`, and preview cleanup with `cache prune`.

See the [v2/v3 compatibility matrix](#v2--v3-long-term-coexistence--compatibility) for existing A/B setups. Back up your local state before upgrading and follow the [migration and recovery notes](./docs/MIGRATION.md).

## Set up devices

Install Python 3.9 or later and Git, then clone the repository on each device.

```sh
git clone https://github.com/Tonyyyl1/Angent-baton.git
cd Angent-baton
```

Run the examples from the tool directory.

**Device A · macOS/Linux**

```sh
python3 relay.py init --device a --host
python3 relay.py install-skills
```

**Device B · Windows PowerShell**

```powershell
py -3 relay.py init --device b --host
py -3 relay.py install-skills
```

The default ledger is `ledger.json` in the tool checkout. To place it elsewhere, supply a device-local path with `init --ledger PATH`. Run `install-skills --dry-run` to preview where the skills will be installed. Use `--claude`, `--codex`, or `--anti` to install only the skill you use.

Open the local web workbench with `python3 relay.py serve` on macOS/Linux or `py -3 relay.py serve` in PowerShell. The default address is `http://127.0.0.1:8765/`.

## Manual handoff

The manual workflow uses Python's standard library and Git. Register the project, select a transport, then follow the printed checklist from each device.

1. On the sender, run `python3 relay.py add my-app --path /path/to/my-app` and `python3 relay.py handoff my-app --to b` (`handoff`).
2. Complete the pre-departure checklist and run `python3 relay.py depart my-app`.
3. On the receiver, open the matching ledger entry and run `py -3 relay.py pickup my-app` (`pickup`). Complete the arrival checklist and run `py -3 relay.py arrive my-app`.

Use a shared ledger or transfer a ledger backup with `export FILE` and `import FILE` so the receiver has the handoff record. For offline transfer, configure `device --drop PATH` on each device and use the `bundle`, `patch`, or `folder` transport. The `remote` transport prints Git commands to run against your remote.

## Encrypted Cloud Handoff & N-Device Mesh (v3)

Participating v3 devices need a checkout containing v3 and `pynacl` (`python3 -m pip install --user pynacl`) for the Python interpreter running relay-desk. Install `rclone` and run `rclone config` on each device to configure access to the same shared cloud folder (`cloud remotes`).

### Snapshot Caching vs. Active Handoff

- **Snapshot Publishing & Sync (`publish`, `sync`, `sync --watch`)**: Publishes immutable, author-signed snapshots to local cache and paired v3 peers without transferring active editing ownership or modifying any workspace. Paired peers cache incoming snapshots and forward missing head snapshots to other paired v3 peers even when the original publisher is offline.
- **Active Workspace Handoff (`send --to DEVICE`, `inbox`, `receive`)**: Exclusive active workspace transfer (`purpose: "handoff"`, bound to `authorizedDeviceId`). Non-target devices cannot claim a handoff even if they cached the snapshot, and only one unacknowledged handoff per project may be in flight at a time.

`publish` synchronizes receipts before checking ownership and refuses to advance a project with a pending or active handoff. Later local edits remain intact. After the recipient acknowledges a handoff, publishing continues on the device that now owns the project.

`receive` automatically retrieves and verifies missing signed ancestor metadata; a three-device round trip does not require a preliminary `sync`. Missing evidence produces `lineage_incomplete`, leaving the workspace intact until the cloud records are available, rather than declaring a fork. `sync` does the same before it reports a fork: a missing ancestor (also one signed with the author's re-confirmed previous key, which counts as historical evidence and grants that key nothing else) is fetched and verified. When no verifiable copy exists, the pair is recorded as insufficient evidence (`lineageUnproven` in the sync summary, `lineage_unproven` in `relay doctor`), kept apart from forks, and re-checked by later syncs.

### Executable 3-Device Walkthrough (`a`, `b`, `c`)

1. **Initialize devices (`init`, `device --json`)**

   Arbitrary valid aliases (`[A-Za-z0-9_.-]{1,40}`, e.g. `mac-studio`) are supported alongside `a`, `b`, `c`:

   ```sh
   # On Device A, B, C respectively:
   python3 relay.py init --device a --host
   python3 relay.py init --device b --host
   python3 relay.py init --device c --host
   # Or custom alias: python3 relay.py init --device mac-studio --host

   # Inspect local identity, pairing status, fingerprints, and v2/v3 capabilities:
   python3 relay.py device --json
   ```

2. **Configure cloud storage and pair the N-device mesh (`cloud remotes`, `pair`)**

   ```sh
   # Check configured rclone remotes and publish each device's signed descriptor:
   python3 relay.py cloud remotes
   python3 relay.py pair --remote s3_remote --root relay-baton

   # Inspect a specific peer's status and 24-hex pairing fingerprint:
   python3 relay.py pair --with b --status

   # Verify the 24-hex fingerprint matches on both devices, then accept:
   python3 relay.py pair --with b --accept --fingerprint <FP>
   # Repeat for each pair (a <-> b, a <-> c, b <-> c).
   # Shorthand when only a single peer exists and its fingerprint was already displayed:
   python3 relay.py pair --accept

   # Optional key rotation (keeps deviceId; peers pause that edge until re-verified).
   # Refused while a handoff or receive is unfinished; see "Key rotation and redelivery" below:
   python3 relay.py device --rotate-keys
   ```

3. **Publish immutable snapshots & background sync (`publish`, `sync`)**

   ```sh
   # On Device A (retains active editing ownership):
   python3 relay.py add demo --path /path/to/demo-a --id demo
   python3 relay.py publish demo

   # On Device B (caches snapshots locally without touching workspaces,
   # and forwards cached head snapshots to Device C even if A goes offline):
   python3 relay.py sync
   python3 relay.py sync demo
   python3 relay.py sync --watch
   # Machine-readable: one JSON event per line, ending with a summary line:
   python3 relay.py sync --watch --jsonl
   ```

   `sync` exits 0 (success), 3 (partial: only retryable problems remain), 4 (needs action), or 130 (cancelled). See "Continuous sync" below.

4. **Exclusive active handoff ring (`send`, `inbox`, `receive`)**

   ```sh
   # Device A -> B (prints "已交棒，可以关机"):
   python3 relay.py send demo --to b

   # Device B checks inbox and claims exclusive ownership:
   python3 relay.py inbox
   python3 relay.py receive <RELAY_ID> --path /path/to/demo-b

   # Device B edits and hands off to C; C claims and hands off back to A:
   python3 relay.py send demo --to c
   # On Device C:
   python3 relay.py inbox
   python3 relay.py receive <RELAY_ID> --path /path/to/demo-c
   python3 relay.py send demo --to a
   # On Device A:
   python3 relay.py inbox
   python3 relay.py receive <RELAY_ID>
   ```

5. **Transactional crash recovery & conflict isolation (`recover`, `receive --into`)**

   Interrupted workspace updates are protected by SHA-256 checksummed journals in `<local_home>/transactions/<relay_id>/`:

   ```sh
   # Inspect transaction state or roll back safely to pre-transaction state:
   python3 relay.py recover <RELAY_ID> --status
   python3 relay.py recover <RELAY_ID> --rollback

   # If receive reports target_dirty, version_fork, or recovery_conflict,
   # materialize the incoming handoff into a clean directory without touching the existing workspace:
   python3 relay.py receive <RELAY_ID> --into /path/to/demo-clean
   ```

   File writes are staged in a journal-owned directory on the target filesystem, then atomically replace their destinations. Retry `receive` after an interrupted copy; leftover transaction scratch files are distinguished from subsequent user edits.

   If rollback is interrupted, continue it with `recover <RELAY_ID> --rollback`; `receive` cannot acknowledge that transaction. Once ledger updating begins, finish with `receive`; rollback is no longer allowed. Journal format 2 reads existing format-1 journals. Use this fixed version to finish outstanding recovery and do not downgrade while new-format journals are pending.

   Cache forwarding keeps the original ciphertext and signed marker in local `sync_publications/`. Retry `sync` to resume the same publication. Published packages are never re-encrypted in place. These recovery files consume additional local space. `relay cache prune` deletes a checkpoint only after the receiver has confirmed the snapshot with its current key (see "Diagnostics and cache cleanup").

   `relay doctor` lists unfinished transactions with the exact `recover` or `receive` command to run.

6. **Query archived conversation transcripts (`transcripts`)**

   ```sh
   python3 relay.py transcripts demo
   ```

   See [StorageProvider contract](./docs/STORAGE_PROVIDER.md) (`StorageProvider`) and [Protocol & State Spec](./docs/SPEC.md).

### Continuous sync (`sync`, `sync --watch`)

`relay sync` runs one round. `relay sync --watch` runs a round every `--interval` seconds (default 5) until you press Ctrl+C, send SIGTERM, or reach `--max-cycles N`. Each round re-reads `identity.json`, `cloud.json`, and `relay_state.json` without writing them. A key rotation or re-pairing made while a watch is running takes effect on the next round. If this device's `deviceId` is replaced, the watch stops. Each round holds the local workspace lock only while it runs.

Each round emits events as they happen: snapshot cached, snapshot forwarded, receipt synced, fork detected, insufficient lineage evidence recorded or resolved, peer verified a cache, problem opened or resolved, peer retry state, trust change, and round skipped because the device was busy.

| Output flag | What is printed |
| --- | --- |
| none | Text. A single run prints the summary lines, `同步状态：…`, and each unresolved problem with the command that fixes it. A watch prints one timestamped line per change and a one-line summary when it stops. |
| `--json` | One summary object (`schema: relaydesk.sync.summary/1`) when the run ends or is interrupted. With `--watch`, nothing is printed per round. |
| `--jsonl` | One JSON object per event (`schema: relaydesk.sync/1`, consecutive `seq`). The last line is `{"kind": "summary", "summary": {…}}` and holds the same summary object that `--json` prints. Works with `--watch`. |

`--json` and `--jsonl` cannot be combined (usage error, exit 1). If the reader of `--jsonl` closes the pipe, for example `| head -1`, the run is cancelled. An error before the first round is printed as `{"ok": false, "status", "code", "errorClass", "action"}` with `--json`, or as one `"kind": "error"` line with `--jsonl`.

| Status | Exit code | Meaning |
| --- | --- | --- |
| `success` | 0 | No unresolved problem. |
| `partial` | 3 | Only retryable problems remain: transient transport failures or a busy device. |
| `needs_action` | 4 | At least one problem needs you: configuration, trust, damaged local state, or an integrity failure. Errors before the first round, such as no cloud configuration or a damaged state file, also exit 4. |
| `cancelled` | 130 | Ctrl+C, SIGTERM, or a closed `--jsonl` pipe. The final summary is still printed and the locks are released. |

Other usage errors exit 1, and ledger or file I/O errors before the first round exit 2. The summary field `ok` is now `status == "success"`; it used to be always `true`.

Errors fall into four classes (`errorClass`):

| Class | Typical causes | Handling |
| --- | --- | --- |
| `transient_transport` | Network or storage timeouts and connection resets; an object listed a moment ago that is now gone; a peer's have manifest read while it was being rewritten | Retried with capped backoff and jitter. The same unexplained failure 3 times in a row becomes `needs_user_action`, and a torn have read 3 times in a row becomes `integrity`. |
| `needs_user_action` | Not configured, peer key changed, untrusted sender, storage authorization or configuration, disk full, permission denied, damaged local state, a handoff stuck on a key change, a local cache or archive directory that is a symlink (`local_dir_symlink`) | Not retried in a loop. The problem carries an `action` command. |
| `integrity` | Bad signature, digest mismatch, invalid identifier or path (including a symlink inside a received payload), malformed remote JSON, a receipt that does not match the send record | The object is quarantined by its listing tag (size, etag, sha256, mtime) and is not downloaded again while unchanged. It is reported once. |
| `local_busy` | Another local publish, send, receive, or sync holds the workspace lock; ledger lock timeout | The round is skipped. A run of busy rounds is reported once. |

Backoff is per peer, or global when the inbox cannot be listed. It grows up to `--max-backoff` seconds (default 300, must not be smaller than `--interval`) and returns to `--interval` after a success. A failing peer does not block healthy peers. A peer whose key changed is parked until you re-confirm it. A device-wide problem that needs action, such as a damaged `cloud.json` or a storage authorization failure, does not end a watch: it rechecks every `--max-backoff` seconds and resumes once the problem is fixed.

Single commands such as `publish`, `send` and `receive` retry a transient read failure in place: up to 4 attempts within 12 s, with jitter. Not-found, authorization, configuration and integrity errors are not retried, and uploads are never repeated blindly. When a sync round holds the local lock, `receive` waits up to 20 s before reporting `local_busy`. See [docs/STORAGE_PROVIDER.md](./docs/STORAGE_PROVIDER.md) §2.1.2.

The summary keeps at most the 200 most recent events in `recentEvents`. `recentEventsTruncated` and `recentEventsDropped` say whether older events were dropped. The cumulative `counters` are kept separately and count every event. The older list fields (`syncedReceipts`, `syncedSnapshots`, `forwarded`, `conflictsDetected`, `errors`, and the new `lineageUnproven`) keep at most their last 200 entries each, and `legacyListsTruncated` flags any that were cut. A problem is reported when it first appears or when its class changes, not every round.

`<RELAY_DESK_HOME>/sync_status.json` (`schema: relaydesk.sync.status/1`) holds the last run's result plus `running`, `heartbeatAt`, and `pid`. A running watch writes it at most every 10 seconds while something changes, writes a heartbeat every 60 seconds otherwise, and always writes it at the end. It counts as stale after 3 × max(interval, max-backoff) seconds without a heartbeat. `relay doctor` and `GET /api/cloud/status` (`lastSync`) read it.

`relay serve` provides a watch API: `POST /api/cloud/sync/watch` with `{interval, maxBackoff, project}` (2 ≤ interval ≤ maxBackoff ≤ 3600), `GET /api/cloud/sync/watch?since=SEQ` (events after `SEQ`; `gap > 0` means some events were pushed out of the 200-event buffer), and `POST /api/cloud/sync/watch/stop`. While a watch runs, a single `POST /api/cloud/sync` returns 409 `watch_running`. Cloud error bodies include `errorClass`. On the web page, the 待接棒 tab has a 持续同步 toggle that shows the status, counters, per-peer state, unresolved problems with copyable commands, and the 50 most recent events.

`publish` reports per-peer delivery failures in `deliveryErrors` (with `errorClass` and `code`) and `status`. The snapshot is still published (`ok: true`), and the next `relay sync` retries the failed peers.

A watch run with two rounds (real output):

```console
$ relay sync --watch --interval 1 --max-cycles 2
持续同步已启动：每 1 秒一轮，暂时失败时最长退避 300 秒（Ctrl+C 停止）。
[02:29:40] + 快照 snap_20261119_mqisvue1（项目 demo，作者 dev_dc3bb4d1dfc2e644）
[02:29:40] = 对端 dev_125cf833db8a0708 已确认缓存快照 snap_20261119_yymdp9nq
持续同步已停止（成功）：共 2 轮，缓存 1、转发 0、回执 0；重试 0 次；未解决 0 项
同步状态：成功
```

### Key rotation and redelivery

`relay device --rotate-keys` replaces this device's signing and encryption keys and keeps its `deviceId`. The retired public keys, never the private keys, are kept in `identity.json` under `previousKeys` (last 10).

**Rotation guard.** Rotation is refused (exit 4) while this device has an unfinished send, a handoff that is neither claimed nor superseded, a receipt that has not been uploaded, or an unfinished receive transaction. Each reason is listed with its command. `--force` rotates anyway and prints the affected handoffs. Each of those then needs `relay send PROJECT --to DEVICE --reissue` after the peer re-confirms the new fingerprint.

```console
$ relay device --rotate-keys
本机还有未完成的交接或恢复事务；现在换钥，这些交接包将无法被对方领取或验证：
  - 交接 r_20261119_3x5cwc5t 已发给 beta，等待对方领取并回传签收回执。
    处理命令：relay sync demo
请先按上面的命令处理完再换钥。确需立即换钥请加 --force，之后在途交接需要用 relay send <项目> --to <设备> --reissue 重新签发（原交接包保持不变）。
```

**Re-confirmation.** Peers notice the new key during `relay pair`, or during `sync` when a signature fails and they re-read the device's signed descriptor. They mark the device `key_changed`. That one edge pauses: no handoffs and no new trust. All other pairs keep working. Compare the new fingerprint on both screens, then run `relay pair --with DEVICE --status` and `relay pair --with DEVICE --accept --fingerprint FP` on each peer. On acceptance, the previously confirmed key is added to that peer's `keyHistory` in `cloud.json` (last 10). A device that is never re-confirmed regains no trust. A key seen only in `pendingDescriptor` never enters the history.

**New deliveries per receiver key.** Cache deliveries are recorded in `deliveries.json` per (receiver, receiver key fingerprint, snapshot), and the delivery ID names the receiver's key: `(sync|fwd)_<snapshot>_<sender6>_k<fp8>[_gN]`. After a re-confirmed rotation, the receiver marks a delivery sealed to its old key, or signed by the sender's old key, as obsolete and requests the snapshot again in its have manifest. A sender with the snapshot then publishes a new delivery ID with new ciphertext. Original deliveries, receipts, and packages are never rewritten. A new generation (`_gN`) is also created when the sender itself rotated, or when a delivery published more than 10 minutes ago is still unconfirmed, its cloud objects are gone, and no local checkpoint remains. If a local checkpoint exists, the original bytes are uploaded again instead. More than 4 generations for one receiver key raises `repeated_redelivery` (needs action).

**Three delivery states.** These states are kept separate:

| State | Evidence |
| --- | --- |
| published | This device's ready marker and package exist for that receiver key. |
| peer-verified cache | The receiver's signed have manifest, verified with its current pinned key, lists the snapshot. |
| claimed | A receiver-signed handoff receipt matches the send record. |

A have manifest never counts as a claim and never moves project ownership. A receipt never implies that other snapshots are cached.

**Signed have v2.** `v3/have/<deviceId>.json` adds `haveFormat: 2`, `keyFingerprint`, an increasing `seq`, and `needs`: up to 200 snapshot IDs that this device lost or could not verify. Only cache entries whose digest has been verified are listed. A have signed by a retired key, naming a different fingerprint, or carrying a lower `seq` than one already seen is ignored as stale. An unchanged have is uploaded again at most every 15 minutes.

**Historical evidence.** Version-graph nodes record `authorKeyFingerprint`, `trust` (`current`, `historical`, or `attested`), and `recordSha256`. A snapshot signed by a re-confirmed retired key is accepted as `historical` evidence for caching, forwarding, and lineage. That gives the old key no new rights. Ready markers, have manifests, and device descriptors signed by a retired key are refused. A handoff carrying a historical snapshot is accepted only when the snapshot's author sends it. A receipt signed by a retired key is accepted only for a handoff that was bound to that key.

**Re-issuing an in-flight handoff.** If the target rotated (`handoff_recipient_key_changed`) or this device rotated (`handoff_sender_key_rotated`), a plain `relay send` stops with the error and the command to run. It never re-issues on its own. `relay send PROJECT --to DEVICE --reissue` creates a new relay ID and new ciphertext for the same signed snapshot. The new ready marker carries signed `supersedesRelayId` and `supersedesRelayIds` fields. The original package, ready marker, and any receipt stay untouched, and the original send record gets `supersededBy`.

- The target's inbox lists only the re-issued handoff; `inbox --json` shows the original under `superseded`.
- The target can claim only one. Claiming the re-issue after the original fails with `superseded_original_claimed`, and claiming the original after the re-issue fails with `handoff_superseded`.
- If the sender rotated and the target claimed the original with the old pinned key before re-confirming, the original's receipt wins and the re-issue is recorded as `withdrawn`. The first claim wins.
- `--reissue` refuses, without writing anything, in these cases: `nothing_to_reissue`, `reissue_not_needed`, `handoff_already_claimed`, `handoff_already_superseded`, `reissue_receipt_exists` (run `relay sync` first), `reissue_payload_missing` (the local cache no longer holds the verified payload), and `reissue_not_supported` (old protocol, or a snapshot this device did not author). The target must be re-confirmed (`paired`) first.
- An interrupted `--reissue` resumes with the same new relay ID.
- The target device must run this engine version. An older v3 engine refuses a re-issued handoff; this fails safe and never lets it claim both.

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

### Diagnostics and cache cleanup (`doctor`, `cache status`, `cache prune`)

**`relay doctor [PROJECT] [--json]`** is strictly read-only: no lock, no network, no writes. It checks:

- identity and pairing (not configured, pending confirmation, `key_changed`);
- the current editor and any in-flight handoff of each project;
- interrupted sends and re-issues, handoffs stuck on a key change, and receipts not yet uploaded;
- recovery transactions;
- the last sync result from `sync_status.json`, including a stale heartbeat;
- unresolved forks, version pairs with insufficient lineage evidence, interrupted prunes, and cache entries in an unclear state;
- symlinked local cache or archive directories, and project IDs in the ledger that break the `projectId` rule.

Each finding has a severity (`info`, `warning`, or `error`), a stable `code`, a reason, and exactly one command. The CLI errors, the API, and the web page show the same reason and command. Exit codes: 0 for info only, 3 for warnings, 4 when something needs you, 1 for an unknown project. For each project, every paired device is shown as 已确认缓存 (confirmed cache), 等待确认 (awaiting confirmation), 待补发 (needs redelivery), 未知 (unknown), or 需要操作 (needs action). 未知 means there is no recent verified have manifest from that device. It is never reported as offline or as missing a snapshot.

```console
$ relay doctor
relay doctor：本机 alpha（dev_5a2947f8a0798308）  状态：正常（需要处理 0，警告 0，提示 0）
项目 demo「演示项目」：当前编辑设备 设备 alpha（本机）
  已确认缓存：beta、gamma；等待确认：—；待补发：—；未知：—
未发现需要处理的问题。
```

**`relay cache status [PROJECT] [--json]`** is read-only. It counts snapshot payloads, signed metadata (including `cache/lineage`), ciphertext checkpoints (`sync_publications/`), recovery transactions (never pruned), and leftovers from an interrupted prune, and shows how much the current retention rules would free.

```console
$ relay cache status
本机缓存（/tmp/relay-demo/home_alpha）：
  快照载荷：3 个，9.9 KB（已验证 3，损坏或状态不明 0）；保留 1，可清理 2
  签名元数据：3 个，1.7 KB（其中谱系文件 0 个，始终保留）
  密文检查点：6 个，25.0 KB；保留 0，可清理 6
  恢复数据：0 个事务，0 字节（未完成 0，无法读取 0；从不清理）
预计可释放：31.6 KB（运行 relay cache prune --dry-run 查看明细）
```

**`relay cache prune [PROJECT] [--dry-run | --apply] [--expect-plan DIGEST] [--json]`** previews by default and writes nothing. The preview lists what would be deleted and why, what is kept and why, entries kept because their state is unclear, the estimated space freed, and a `planDigest`. `--apply` takes the local workspace lock (exit 3 if busy), re-reads every input, and recomputes the plan inside the lock. It never replays an earlier preview. With `--expect-plan`, nothing is deleted if the recomputed plan differs (`prune_plan_changed`, exit 3; 409 from the API).

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

Prune always keeps:

- the payload of each project's current version and of every version-graph head, including fork heads;
- both sides of every unresolved fork recorded in the ledger;
- the snapshots of pending sends, unclaimed and unsuperseded handoffs, receives whose receipt is not uploaded, and unfinished receive transactions, plus their rollback targets;
- snapshots a paired peer has not yet confirmed with its current key, and snapshots requested in this device's or a peer's current have `needs`;
- checkpoints of `preparing` generations, and of published generations not yet confirmed by the receiver's current key;
- all signed metadata. Before a payload is deleted, its `snapshot.json` is copied to `cache/lineage/<version>.json` and read back, so parent-chain checks still work;
- anything damaged, oddly named, symlinked, containing files that do not belong to the cache (such as credentials), or a checkpoint that does not match its delivery record. These are reported under `unclear`. If any recovery transaction cannot be read, no payload is pruned.

A checkpoint is deleted only when its generation is published (or abandoned and never resumed) with a matching delivery ID and digest, and the receiver has confirmed the cache with its current key, has rotated so the old delivery is obsolete, is no longer paired, or a newer generation replaced it.

Prune never touches `relay_state.json` (including `processedDeliveries`, the anti-replay records), `cloud.json`, `identity.json`, the records in `deliveries.json` (only `prunedSnapshots` grows), `transactions/`, `archives/`, credentials, or anything in the cloud. It does not connect to the cloud: the have manifest is republished on the next `relay sync`. A pruned snapshot is not downloaded again and is not requested through `needs`, unless it becomes a current version or head again.

Each payload is deleted in four steps: copy `snapshot.json` to lineage, record the snapshot in `prunedSnapshots`, rename the payload to a `.pruning-<name>.<random>` tombstone, delete the tombstone. A checkpoint is renamed to a tombstone and then deleted. If a prune is interrupted, `relay doctor` reports `prune_interrupted`; run `relay cache prune --apply` again to rescan and finish. Results: `success` exits 0; `partial` (some deletions failed) exits 3; `blocked` deletes nothing and exits 4 when local state is damaged, or 3 when this device has never synced and has no `deliveries.json`.

On the web page, the 待接棒 tab shows a 接力网络状态 panel. It is rendered from `GET /api/cloud/doctor`, which calls the same function as `relay doctor --json`. Per project it shows the current editor, any in-flight handoff, which devices have a confirmed cache, are awaiting confirmation, need redelivery, or are unknown, and each finding with a copyable command. 重新诊断 reruns the diagnosis. 预览缓存清理 runs only a dry run and shows the `relay cache prune --apply --expect-plan <digest>` command; the page never deletes anything. After a send fails because of a key change, the page offers 为当前公钥重新签发交接, the only control that sends `reissue: true`. The API routes are `GET /api/cloud/doctor?project=`, `GET /api/cloud/cache?project=`, and `POST /api/cloud/cache/prune` with `{mode: "dry-run" | "apply", project?, expectPlan?}`. `POST /api/cloud/send` accepts `reissue: true`, and error bodies include `action`.

### Stricter validation of identifiers and local state

After a signature is verified, every signed field is still checked for type, length, path safety, and consistency before anything is written to the cache, archives, or a workspace.

| Identifier | Rule |
| --- | --- |
| `versionId`, `deviceId`, `relayId` | ASCII letters, digits, `_`, `.`, `-`; starts with a letter or digit; no `..`; does not end with `.`; not a Windows device name; at most 128 characters (a relay ID you type: at most 80). Delivery IDs follow the same rule, up to 160 characters. |
| `projectId` | Unicode letters, digits, combining marks, `_`, `-`, `.`, and spaces; must be NFKC-stable, so NFD forms and full-width symbols are refused; must not start with `.`, a space, or `-`, or end with `.` or a space; no `..`; not a device name; at most 128 characters. |
| `parentVersionIds` | A list of valid, unique IDs that does not include the snapshot itself, at most 64. |

What you will notice:

- **Stricter project IDs.** `relay add` (with or without `--id`), `relay import`, and the local API (`PUT /api/projects/<id>`, `PUT /api/ledger`) refuse a new project ID that breaks the rule, so `relay add … --id .hidden` now fails with `projectId 不能以点、空格或连字符开头…`; an ID derived from the project name is made valid (`.hidden` becomes `hidden`). A project already in the ledger under such an ID still loads and can be edited, but cannot be published, sent, or received; `relay doctor` reports it as `project_id_invalid` with a `relay add … --id <新项目ID>` command to register it under a valid ID.
- **Symlinked local directories are refused.** `cache/snapshots`, `cache/lineage`, `archives/<project>`, `transactions/`, and `sync_publications/` under `RELAY_DESK_HOME` must be real directories. `RELAY_DESK_HOME` itself may be a symlink. If you linked the cache or archives to another disk, sync reports `local_dir_symlink` (`needs_user_action`, exit 4, not an integrity failure) and writes nothing through the link; `relay doctor` shows the same problem with a command that moves the directory back. Once it is a real directory again, the next sync processes the same cloud deliveries, with no redelivery from peers (a delivery that has left the inbox meanwhile is requested again). To keep the data on another disk, move the whole home instead and point `RELAY_DESK_HOME` at it.
- **Absolute symlink targets are refused on receive.** A package with a symlink that points to an absolute path or outside the project, or that writes below a symlink entry, is refused before anything is written. The sender still packs a symlink with an absolute target inside the project, and the receiver refuses it, so make such symlinks relative.
- **Damaged local state stops with a clear error.** `identity.json`, `cloud.json`, `relay_state.json`, `config.json`, `deliveries.json`, a transaction's `meta.json`, and sync checkpoints are loaded strictly. A missing file still starts empty. A file that exists but is damaged (invalid JSON, empty, truncated, wrong field types, a dangling symlink, or a directory) stops the command with `local_state_corrupted` (exit 4), and the file is left byte-for-byte unchanged. Previously a damaged `relay_state.json` or `cloud.json` was silently replaced, which lost pending handoffs or pairings. See [MIGRATION.md](./docs/MIGRATION.md) for recovery.

```console
$ relay sync
本机接力状态 relay_state.json已损坏或无法读取（JSON 解析失败：Expecting property name enclosed in double quotes: line 1 column 3 (char 2)）：/tmp/relay-demo/home_gamma/relay_state.json。为避免覆盖已有数据，操作已停止；请先备份并修复或恢复该文件，不要直接删除。
处理：备份并修复报告中的本机状态文件后重新运行（不要直接删除）：/tmp/relay-demo/home_gamma/relay_state.json
```

### Receiving into a folder with Git-ignored files

`receive` into an existing Git working tree no longer refuses because of Git-ignored files such as `build/`, `*.log`, or `node_modules/`. They are kept untouched (content, permissions, and mtime) through apply, rollback, and resume. Uncommitted changes and untracked files that are not ignored still block an in-place receive.

The receive is refused before anything is written, and the folder stays untouched, if the package would:

1. write a file or symlink at the path of a kept local file;
2. create a directory where a kept local file is;
3. write a file where a kept directory holding kept files is;
4. ship `.gitignore` rules that no longer ignore a kept local file. That file would become untracked and be packed into the next handoff. Credentials are never packed and are not affected.

Paths are compared NFC-normalized, and case-insensitively on case-insensitive file systems (`Debug.log` collides with `debug.log`). After a refusal, receive into a clean folder with `--into`.

## v2 ↔ v3 Long-Term Coexistence & Compatibility

| Topology | Behavior & Protocol | Notes |
| --- | --- | --- |
| **Old A ↔ Old B** | Original v2 protocol unchanged (`devices/{a,b}.json`, `relays/<relayId>/`) | No changes required on existing 2-device setups. |
| **Old A ↔ New B** | Automatic v2 protocol adapter on New B | New B inspects verified `capabilities`, sees `["v2"]` on Old A, publishes `devices/b.json` alongside `v3/devices/<deviceId>.json`, and routes handoffs to Old A via `relays/<relayId>/`. |
| **New A ↔ New B ↔ New C** | Dynamic v3 personal device network (`v3/devices/`, `v3/have/`, `v3/inbox/`) | Supports arbitrary aliases, snapshot publishing/sync/forwarding, lineage DAG, and `workspace_tx` crash recovery. Never downgrades v3 peers to `relays/` on error. |
| **Old A via New B ↔ New C** | Bridged relay through New B | Retains the original `A ↔ B` v2 pairing edge; Old A hands off to New B via v2, and New B hands off to New C via v3 (and back). |
| **Old A ↔ dynamic C directly** | Unsupported without upgrading A | Old A only supports slots `a`/`b` and ignores `v3/devices/`. New C targeting Old A gets a `v2_incompatible_alias` error instructing to upgrade A's relay engine. |

**Co-installation rule**: When running old v2 and new v3 clients on the same machine, each version MUST use its own `RELAY_DESK_HOME` directory and its own `--ledger` (or `RELAY_DESK_LEDGER`) path. Old v2 clients must not share a multi-device v2 ledger.

**Earlier v3 builds in the same network.** Devices on the earlier v3 build keep exchanging snapshots and handoffs with this version. New delivery IDs keep the `sync_` and `fwd_` prefixes, so older inboxes still skip them as cache deliveries. A have v2 still lists `snapshots` as plain IDs, and older engines ignore the new ready and have fields. However, an earlier build does not publish `seq` or `needs`, so its own lost cache is not repaired until it upgrades, and it refuses a re-issued handoff (`send --reissue`). Upgrade the target before re-issuing to it. The old A/B v2 protocol is unchanged. Details: [MIGRATION.md](./docs/MIGRATION.md).

## Verify a checkout

Run the test suite with Python, Git, PyNaCl, rclone, and Node.js installed.

```sh
python3 -m unittest discover -s tests -v
```

On Windows, use `py -3` in place of `python3`. The [acceptance guide](./acceptance/README.md) covers native platform checks, a three-device simulation on one machine, and separate runs on physical devices and public cloud storage.

## Benchmark

Reproduce the inbox scalability and network I/O comparison (`old_v2` vs `new_v2_compat` vs `new_v3`) across LocalDir (`N=2,3,10`, `H=100,1000,10000`) and WebDAV/S3 loopback backends:

```sh
python3 benchmarks/inbox_benchmark.py --full
```

## Command reference

| Task | Commands |
| --- | --- |
| Device and project setup | `init`, `device`, `add`, `set` |
| View and update local records | `status`, `scan`, `note`, `sessions`, `session add`, `session rm` |
| Manual checklist | `handoff`, `plan`, `check`, `depart`, `pickup`, `arrive`, `cancel` |
| Cloud transfer and archives | `cloud remotes`, `pair`, `publish`, `sync`, `send`, `inbox`, `receive`, `recover`, `transcripts` |
| Diagnostics and local cache | `doctor`, `cache status`, `cache prune` |
| Other tools | `serve`, `install-skills`, `export`, `import` |

New options: `sync --jsonl`, `sync --max-backoff S`, `send --reissue`, `device --rotate-keys --force`, `cache prune --dry-run | --apply`, and `cache prune --expect-plan DIGEST`.

[MIT License](./LICENSE)
