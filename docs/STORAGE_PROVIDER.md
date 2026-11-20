# Relay Desk 云存储适配层规范（`StorageProvider`）

本文档说明 Relay Desk 的可插拔云存储接口（[relaydesk/storage.py](../relaydesk/storage.py)）、原子性与错误处理契约、无锁对象存储下的并发与去重模型，以及如何使用共享契约测试集（[tests/storage_contract.py](../tests/storage_contract.py)）验证新实现。

---

## 1. 设计目标与职责边界

Relay Desk 将**交接协议、加密验签、包格式**与**底层云存储传输**彻底解耦：

1. **云存储只负责字节对象存取**：交接包在进入 `StorageProvider` 前已经由 `relaydesk.crypto` 完成端到端流式认证加密（`package.enc`）和 Ed25519 签名（`ready.json`、`receipt.json`、`v3/devices/<deviceId>.json`、`v3/have/<deviceId>.json`）。云端无需感知项目结构，也无法解密内容。
2. **账号授权与工具解耦**：默认内置的 `RcloneProvider` 直接复用用户本机已登录配置的 `rclone` 远端连接（支持 OneDrive、Google Drive、Dropbox、WebDAV、S3 兼容对象存储等）。各设备的连接名称（`remote`）可以不同，只需指向同一个共享云目录即可。
3. **无条件写入模型（No Conditional Writes / No Distributed Locks）**：`StorageProvider` 仅提供**无条件覆盖写入（unconditional `upload`）**，**不依赖且不假设**底层网盘或对象存储支持条件写（如 CAS / `If-None-Match` / `If-Match`）或跨设备分布式锁。
4. **两阶段发布与原子落盘**：发送端先上传 `package.enc` 并通过 `stat` 校验字节数，最后写入 `ready.json`；接收端下载时先写入目标同目录临时文件，校验通过后再通过 `os.replace` 原子替换到目标路径，绝不留下半截损坏文件。

---

## 2. 核心数据结构与异常

### 2.1 异常层级

| 异常类 | 继承关系 | 触发时机 |
| --- | --- | --- |
| `StorageError` | `Exception` | 所有存储层错误的基类（包括非法对象键、网络中断、鉴权失败、`rclone` 未安装、磁盘满等） |
| `ObjectNotFoundError` | `StorageError` | 调用 `download(remote_key, local_path)` 时远端对象不存在（或目标键对应的是目录而非普通文件） |

> **重要约定**：网络不可达、远端名称未配置、`401/403` 鉴权失败等故障**必须抛出 `StorageError`**，绝不能静默伪装成 `[]`、`None` 或 `False`。

### 2.1.1 错误分类约定

持续同步（`relay sync`，见 [SPEC §8](./SPEC.md)）把存储层异常归入不同类别，决定是自动重试还是请用户处理。分类器（`relaydesk/sync_runtime.py` 的 `classify_error`）按以下顺序判断：

1. 异常带有字符串属性 `kind` 且取值为 `"transport"`、`"auth"`、`"config"`、`"local"`、`"key"` 之一时，直接使用它。内置的 `RcloneProvider`、`LocalDirProvider` 目前**不设置** `kind`，分类器回退到第 2 步；新实现建议设置 `kind`。
2. 否则按异常文本匹配：先匹配传输类标记（connection reset/refused、broken pipe、timeout、timed out、no such host、dial tcp、network is unreachable、tls handshake、EOF、context deadline exceeded、temporary failure、too many requests、502/503/504 等），再匹配鉴权标记（unauthorized、401、403、forbidden、access denied、invalid_grant、token expired 等）和配置标记（未找到 rclone、didn't find section in config file、failed to create file system、config file not found 等）。先查传输标记，是因为 rclone 会把连接重置包装成「Failed to create file system … connection reset by peer」这样像配置错误的文本。
3. 原因是本地 `EACCES`/`EPERM`，或文本含 permission denied / 拒绝访问 → 本机权限问题。
4. 都不匹配时按暂时性传输失败处理。

| `kind` / 判定 | 同步中的类别 | code | 行为 |
| --- | --- | --- | --- |
| `transport`（默认） | `transient_transport` | `storage_transport` | 按上限退避加抖动重试，成功后恢复正常间隔 |
| `auth` | `needs_user_action` | `storage_auth` | 不循环重试；提示 `rclone config reconnect <远端>:` |
| `config` | `needs_user_action` | `storage_config` | 不循环重试；提示 `relay pair --status` |
| `local` | `needs_user_action` | `permission_denied` | 提示检查路径权限 |
| `key`（非法对象键、越界符号链接） | `integrity` | `invalid_key` | 隔离该对象，不重试 |
| `ObjectNotFoundError`（刚列出又消失） | `transient_transport` | `listing_lag` | 视为最终一致性或并发删除，稍后重试 |

因此实现方需要保证：传输失败的异常文本里保留底层原因（不要把连接错误改写成「配置错误」）；鉴权失败要能从文本或 `kind` 识别出来；`download` 遇到不存在的对象必须抛 `ObjectNotFoundError`，而不是普通 `StorageError`，否则「对象不存在」会被当成传输失败无限重试，反之亦然。

### 2.1.2 命令内的有界读重试

`publish`、`send`、`receive`、`inbox`、`recover`、`pair` 和状态查询使用的提供方由 `storage.with_read_retry()` 包装成 `RetryingProvider`：

- 只重试幂等读取 `list_objects`、`stat`、`download`，并且只重试上表中归为 `transient_transport` 的 `StorageError`。最多 4 次，总计不超过 12 秒；第 n 次失败后等待 `min(4, 0.5 × 2^(n-1))` 秒的一半到全长（随机抖动）。默认策略是 `storage.READ_RETRY`，测试可以注入假时钟。
- 从不重试：`ObjectNotFoundError`（服务器确认对象不存在，不是请求失败）、鉴权、配置、非法对象键、本机权限问题，以及签名或摘要等完整性失败。
- `upload`、`delete` 每次调用只执行一次，不在命令内重复。只写一次的对象（`v3/snapshots/<versionId>.json`）总是先读后写：下一次运行读到相同内容即视为已写入（上一次上传的应答丢失时就是这样确认的），不存在才上传，内容不同则报完整性冲突，从不覆盖。
- `relay sync` 的轮次不在轮内重试，失败后按 §2.1.1 在轮与轮之间退避，尽快释放本机工作区锁。

实现方无需自行重试，但必须照 §2.1.1 区分「不存在」和「请求失败」。

### 2.2 `ObjectInfo`

`ObjectInfo(key, size, sha256=None, etag=None, mtime=None)` 描述单个远端文件对象的元数据，**同时支持属性访问与字典式访问**：

```python
from relaydesk.storage import ObjectInfo

info = ObjectInfo(key="v3/inbox/dev_b123/r_20261118_ab12cd34/ready.json", size=512, sha256="...")

# 属性访问
assert info.key == "v3/inbox/dev_b123/r_20261118_ab12cd34/ready.json"
assert info.size == 512

# 字典式访问
assert info["key"] == "v3/inbox/dev_b123/r_20261118_ab12cd34/ready.json"
assert info.get("size") == 512
payload_dict = info.to_dict()
```

### 2.3 对象键规范化函数 `validate_key`

所有 `StorageProvider` 实现都必须调用 `validate_key(remote_key, allow_empty=...)` 统一校验路径键：

- 统一使用正斜杠 `/` 分隔，合并多余的连续 `/`，并去除首尾 `/`。
- 统一将 Unicode 字符串规范化为 **UTF-8 NFC**（消除 macOS APFS NFD 与 Windows/Linux NFC 之间的中文及重音字符差异）。
- 严格拒绝包含反斜杠 `\`、`.`、`..` 路径段或空字符 `\x00` 的路径，防止路径穿越逃逸出共享根目录。
- `upload`、`download`、`stat`、`delete` 严禁传入空键（`allow_empty=False`）；仅 `list_objects(prefix="")` 允许空前缀以列出根目录全部对象。

---

## 3. `StorageProvider` 抽象接口契约

任何存储提供方均继承 `relaydesk.storage.StorageProvider` 并实现以下 5 个方法：

### 3.1 `list_objects(self, prefix: str = "") -> list[ObjectInfo]`
- 递归列出 `prefix` 下的全部**普通文件对象**，返回结果按 `info.key` 字典序升序排列。
- 每个返回的 `ObjectInfo.key` 均为**相对于共享根目录 `root` 的完整键**（例如 `"v3/devices/dev_a999.json"`）。
- **幂等空目录规则**：如果云端共享根目录或指定的 `prefix` 尚未创建，**必须返回空列表 `[]`，严禁抛出异常**。
- **注意（S10，按设计保留）**：`LocalDirProvider` 的根目录不存在时同样返回 `[]`。因此本地目录存储的根路径写错、外置盘或网络共享盘未挂载时，看起来与「云端为空」完全相同：同步不会报错，只是收不到任何东西，之后的上传会在该路径重新建目录。`relay doctor` 不联网，也不会发现这种情况。使用本地目录存储时，请确认 `cloud.json` 中的 `root` 指向已挂载的共享目录。同步新代码把「列不到」当作「状态未知」，不会据此判定对象缺失而重发；维护检查只对已发布超过 10 分钟仍未确认的条目执行一次 `stat`。

### 3.2 `upload(self, local_path: str, remote_key: str) -> ObjectInfo`
- 将本地普通文件 `local_path` 上传至 `remote_key`；若远端已存在同名对象，**无条件覆盖**（不提供条件写入保证）。
- 返回包含规范化 `key` 与准确字节数 `size` 的 `ObjectInfo`。
- 若 `local_path` 不存在或不是普通文件，抛出 `StorageError`。

### 3.3 `download(self, remote_key: str, local_path: str) -> ObjectInfo`
- **原子落盘规则**：必须在 `Path(local_path).parent` 同文件系统中创建临时文件完成下载，完整写入后再调用 `os.replace` 原子替换到 `local_path`。
- 若远端对象不存在（或 `remote_key` 实际上是一个目录前缀），必须抛出 `ObjectNotFoundError`。

### 3.4 `stat(self, remote_key: str) -> Optional[ObjectInfo]`
- 查询单个远端普通文件对象的元数据；存在时返回 `ObjectInfo`，不存在时返回 `None`。
- **目录安全规则**：绝对不能把目录或 S3 虚拟前缀误判成文件对象。

### 3.5 `delete(self, remote_key: str) -> bool`
- 删除远端普通文件对象 `remote_key`。
- 成功删除返回 `True`；对象原本不存在返回 `False`，不抛异常。

---

## 4. 并发控制、不可变性与显式分叉模型

由于 `StorageProvider` 仅提供**无条件覆盖写入**且**没有条件写（CAS）或分布式锁**，v3 多设备网络通过以下四层机制确保安全与收敛（**从不对并发后台缓存同步宣称跨网络严格“恰好一次 / exactly-once”投递**）：

1. **本机操作系统文件锁（Local OS Locks）**：
   - `relaydesk/relay_engine.py` 使用 `<local_home>/workspace-operation.lock`（基于 `fcntl.flock` / `msvcrt.locking` 非阻塞排他锁）串行化本机的 `publish`、`sync`、`send`、`receive`、`recover` 操作，防止同一设备上的并发进程同时读写工作区与云投递。
   - `ledger.update()`、`_update_cloud()`、`_update_relay_state()` 使用 `<file>.lock`（`O_CREAT | O_EXCL` 互斥锁，最多等待 5 秒、超期 30 秒清理残留）保护本地 JSON 状态文件。
2. **按发送方/投递隔离的唯一不可变对象键（Unique Immutable Object Keys）**：
   - 活跃交接使用 `v3/inbox/<receiverDeviceId>/<relayId>/...`（重新签发的交接是新的 `relayId`，原交接对象不动）；同步与转发使用发送方/转发方专属、并绑定接收方公钥的键 `(sync|fwd)_<versionId>_<senderSuffix>_k<接收方指纹前8位>[_g<代>]`（旧格式 `sync_<versionId>_<senderSuffix>`、`fwd_<versionId>_<forwarderSuffix>` 照常读取）。接收方换钥后的重新投递使用新编号，从不覆盖旧对象。每台设备只写入自己拥有的键前缀或专属投递路径，避免多设备并发写入相互覆盖。
   - 每个设备的描述文件（`v3/devices/<deviceId>.json`）和拥有清单（`v3/have/<deviceId>.json`）也仅由该 `deviceId` 自身更新。
   - `v3/snapshots/<versionId>.json` 保存不可变的原作者签名元数据，用于按版本编号验证祖先关系；不包含项目文件载荷。此索引是 v3 的兼容扩展，原有包和路径保持不变。缺少旧版本索引时，可以从已有 `ready.json` 中取得并验证原作者签名。
   - 缓存同步和转发在任何上传之前，将原密文与签名标记原子保存到 `<local_home>/sync_publications/<投递路径SHA-256>/`。重试沿用该检查点；已存在的云端标记必须完全匹配，否则报告冲突并拒绝覆盖。网络或鉴权错误不会当作对象不存在。
3. **快照内容哈希去重（Snapshot Content-Hash Deduplication）**：
   - 快照元数据携带作者签名的 `versionId` 与明文载荷 `contentSha256`。
   - 各设备通过签名缓存清单 `v3/have/<deviceId>.json`（have v2 带 `seq` 与 `needs`）、本地快照缓存（`<local_home>/cache/snapshots/<versionId>/`，带摘要备忘 `verified.json`）、`processedDeliveries` 与本机投递状态 `deliveries.json`（按接收设备、接收方公钥指纹、快照去重）对已缓存或已投递快照去重；`forwardedSnapshots` 仍持续写入，但不再是去重依据。即便多台持有者同时响应同一个 `needs`（每个持有者对同一 have 序号最多一份），接收端也会通过 `versionId` 与 `contentSha256` 幂等合并缓存，不会产生副作用。
   - have 清单是唯一被原地改写的对象，读取方可能读到正在改写的半个文件：JSON 解析失败先按暂时性处理，连续 3 次才判为完整性失败；`seq` 小于已见值的清单视为过期并忽略。
4. **显式分叉检测而非隐式覆盖（Explicit Fork Detection）**：
   - 对于离线并发修改或并发后台同步，v3 绝不依赖网盘锁去“避免分叉”，而是利用作者签名的 `parentVersionIds` 构成有向无环谱系图（`versionGraph`）；一旦检测到彼此不构成祖先关系的并发版本，立即将 `version_fork` 冲突写入台账并保护工作区不改动，交由用户通过 `receive --into` 显式检出对比。

---

## 5. 内置实现与契约验证

`make_provider(cloud_cfg)` 根据本地 `cloud.json` 构建对应实例：

1. **`RcloneProvider`（`provider == "rclone"`）**：复用 `rclone` 远端连接。
2. **`LocalDirProvider`（`provider == "local"`）**：纯 Python 标准库的本地目录存储。

任何新增 `StorageProvider` 实现均应通过 [tests/storage_contract.py](../tests/storage_contract.py) 提供的共享契约测试集验证（包括空前缀幂等、目录/文件区分、路径穿越拒绝、NFC 编码一致性、原子替换与错误传播）。
