# Relay Desk 云存储适配层规范（`StorageProvider`）

本文档说明 Relay Desk 的可插拔云存储接口（[relaydesk/storage.py](../relaydesk/storage.py)）、原子性与错误处理契约、如何接入新的云存储适配层，以及如何使用共享契约测试集（[tests/storage_contract.py](../tests/storage_contract.py)）验证新实现。

---

## 1. 设计目标与职责边界

Relay Desk 将**交接协议、加密验签、包格式**与**底层云存储传输**彻底解耦：

1. **云存储只负责字节对象存取**：交接包在进入 `StorageProvider` 前已经由 `relaydesk.crypto` 完成端到端流式认证加密（`package.enc`）和 Ed25519 签名（`ready.json`、`receipt.json`、`devices/<alias>.json`）。云端无需感知项目结构，也无法解密内容。
2. **账号授权与工具解耦**：默认内置的 `RcloneProvider` 直接复用用户本机已登录配置的 `rclone` 远端连接（支持 OneDrive、Google Drive、Dropbox、WebDAV、S3 兼容对象存储等）。设备 A 与设备 B 的连接名称（`remote`）可以不同，只需指向同一个共享云目录即可。
3. **两阶段发布与原子落盘**：发送端先上传 `package.enc` 并通过 `stat` 校验字节数，最后写入 `ready.json`；接收端下载时先写入目标同目录临时文件，校验通过后再通过 `os.replace` 原子替换到目标路径，绝不留下半截损坏文件。

---

## 2. 核心数据结构与异常

### 2.1 异常层级

| 异常类 | 继承关系 | 触发时机 |
| --- | --- | --- |
| `StorageError` | `Exception` | 所有存储层错误的基类（包括非法对象键、网络中断、鉴权失败、`rclone` 未安装、磁盘满等） |
| `ObjectNotFoundError` | `StorageError` | 调用 `download(remote_key, local_path)` 时远端对象不存在（或目标键对应的是目录而非普通文件） |

> **重要约定**：网络不可达、远端名称未配置、`401/403` 鉴权失败等故障**必须抛出 `StorageError`**，绝不能静默伪装成 `[]`、`None` 或 `False`。

### 2.2 `ObjectInfo`

`ObjectInfo(key, size, sha256=None, etag=None, mtime=None)` 描述单个远端文件对象的元数据，**同时支持属性访问与字典式访问**，方便不同调用方无缝使用：

```python
from relaydesk.storage import ObjectInfo

info = ObjectInfo(key="relays/r_20261118_ab12cd34/ready.json", size=512, sha256="...")

# 属性访问
assert info.key == "relays/r_20261118_ab12cd34/ready.json"
assert info.size == 512

# 字典式访问
assert info["key"] == "relays/r_20261118_ab12cd34/ready.json"
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
- 每个返回的 `ObjectInfo.key` 均为**相对于存储 `root` 的完整键**（例如 `"relays/r_001/ready.json"`，而非裁剪掉 `prefix` 后的相对名）。
- **幂等空目录规则**：如果云端共享根目录或指定的 `prefix` 尚未创建，**必须返回空列表 `[]`，严禁抛出异常**。

### 3.2 `upload(self, local_path: str, remote_key: str) -> ObjectInfo`
- 将本地普通文件 `local_path` 上传至 `remote_key`；若远端已存在同名对象，**无条件覆盖**（即使新旧文件大小相同且处于同一秒内，也必须覆盖生效）。
- 返回包含规范化 `key` 与准确字节数 `size` 的 `ObjectInfo`。
- 若 `local_path` 不存在或不是普通文件，抛出 `StorageError`。

### 3.3 `download(self, remote_key: str, local_path: str) -> ObjectInfo`
- **原子落盘规则**：必须在 `Path(local_path).parent` 同文件系统中创建临时文件完成下载，完整写入后再调用 `os.replace` 原子替换到 `local_path`，并在异常退出时清理临时文件。
- 如果下载中途失败或远端对象不存在：
  - 若 `local_path` 原本不存在，失败后仍不得存在任何半截文件或残留临时文件；
  - 若 `local_path` 原本已有旧文件，失败后原文件内容必须保持完好无损。
- 若远端对象不存在（或 `remote_key` 实际上是一个目录前缀），必须抛出 `ObjectNotFoundError`。

### 3.4 `stat(self, remote_key: str) -> Optional[ObjectInfo]`
- 查询单个远端普通文件对象的元数据；存在时返回 `ObjectInfo`，不存在时返回 `None`。
- **目录安全规则**：绝对不能把目录或 S3 虚拟前缀误判成文件对象——当 `remote_key` 对应目录时必须返回 `None`。

### 3.5 `delete(self, remote_key: str) -> bool`
- 删除远端普通文件对象 `remote_key`。
- 对象原本存在且成功删除时返回 `True`；对象原本不存在（或为目录）时返回 `False`，不抛异常。

---

## 4. 内置实现与配置工厂

`make_provider(cloud_cfg)` 根据本地 `cloud.json` 构建对应实例：

1. **`RcloneProvider`（`provider == "rclone"`）**
   - 参数：`remote`（自动去除末尾 `:`）、`root`、`configPath`（可选，也可通过环境变量 `RCLONE_CONFIG` 指定）、`rcloneBin`（可选，也可通过环境变量 `RELAY_DESK_RCLONE` 指定）。
   - 自动探测路径：`rclone_bin` 参数 -> `RELAY_DESK_RCLONE` -> `PATH` 中的 `rclone` -> `/opt/homebrew/bin/rclone` -> `/usr/local/bin/rclone`。
   - 底层命令映射：`rclone copyto --ignore-times`（上传/原子下载）、`rclone lsjson -R --files-only`（递归列举）、`rclone lsjson --stat`（单文件状态查询）、`rclone deletefile`（单文件删除）、`rclone listremotes`（通过 `discover_rclone_remotes` 发现本机已配置云盘）。
2. **`LocalDirProvider`（`provider == "local"`）**
   - 参数：`root`（本地共享目录或测试目录路径）。
   - 提供纯 Python 标准库的原子临时文件写入、原子下载、SHA-256 计算及符号链接越界拦截。

---

## 5. 如何开发并验证新的云存储适配器

如果未来需要不依赖 `rclone` 直接对接某个云厂商的原生 HTTP API / SDK（例如原生 S3 / WebDAV / 企业内部对象存储），只需三步即可接入，**无需修改任何 CLI、Web 工作台或加解密/打包代码**：

### 第一步：实现 `StorageProvider` 子类

```python
import os
import tempfile
from pathlib import Path
from typing import List, Optional

from relaydesk.storage import (
    ObjectInfo,
    ObjectNotFoundError,
    StorageError,
    StorageProvider,
    validate_key,
)


class CustomCloudProvider(StorageProvider):
    def __init__(self, endpoint: str, bucket: str, prefix_root: str = "") -> None:
        self.endpoint = endpoint
        self.bucket = bucket
        self.prefix_root = validate_key(prefix_root, allow_empty=True)

    def list_objects(self, prefix: str = "") -> List[ObjectInfo]:
        clean_prefix = validate_key(prefix, allow_empty=True)
        # 调用后端列表接口；若 bucket/前缀尚无对象则返回 []，并按 key 升序排序
        ...

    def stat(self, remote_key: str) -> Optional[ObjectInfo]:
        key = validate_key(remote_key, allow_empty=False)
        # HEAD 查询对象元数据；404 或目录前缀返回 None，连接/鉴权错误抛 StorageError
        ...

    def upload(self, local_path: str, remote_key: str) -> ObjectInfo:
        key = validate_key(remote_key, allow_empty=False)
        # 读取 local_path 上传并覆盖远端 key，返回 ObjectInfo(key=key, size=...)
        ...

    def download(self, remote_key: str, local_path: str) -> ObjectInfo:
        key = validate_key(remote_key, allow_empty=False)
        dst = Path(local_path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=str(dst.parent), prefix=".relay-dl-")
        try:
            os.close(fd)
            # 下载到 tmp_path；若 404 抛 ObjectNotFoundError
            ...
            os.replace(tmp_path, str(dst))
            return ObjectInfo(key=key, size=dst.stat().st_size)
        except BaseException:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    def delete(self, remote_key: str) -> bool:
        key = validate_key(remote_key, allow_empty=False)
        # 删除远端对象；成功删除返回 True，404 返回 False
        ...
```

### 第二步：在 `make_provider` 中注册类型名

在 `relaydesk/storage.py` 的 `make_provider(cloud_cfg)` 中增加对应的 `provider_type` 分支（例如 `"custom_s3"`）。

### 第三步：复用 `StorageProviderContractMixin` 运行契约验证

新建测试类继承 `StorageProviderContractMixin` 与 `unittest.TestCase`，即可一键验证空仓库、嵌套目录、中文 UTF-8/NFD 键、同秒同大小覆盖、原子下载失败无残留、缺失对象语义与非法键拦截：

```python
import unittest
from tests.storage_contract import StorageProviderContractMixin


class TestCustomCloudContract(StorageProviderContractMixin, unittest.TestCase):
    def make_contract_provider(self):
        return CustomCloudProvider(
            endpoint="http://127.0.0.1:9000",
            bucket="relay-test",
            prefix_root="case_1",
        )
```

运行验证命令：

```bash
python3 -m unittest tests/test_storage.py -v
```
