# relay-desk

[English](./README.md) | **简体中文** | [更新日志](./CHANGELOG.md)

relay-desk 是一套面向两台设备的开发接力工具。命令行、本机网页和 Claude Code、Codex、Antigravity 的 skills 共用接力台账。手动模式用清单协助转移项目。v2 云接力新增加密传输，可以移交 Git 工作区或普通目录，以及受支持的对话归档。

## 在两台设备上安装

安装 Python 3.9 或更新版本，以及 Git。分别在两台设备拉取工具源码。

```sh
git clone https://github.com/Tonyyyl1/Angent-baton.git
cd Angent-baton
```

以下命令在工具目录下运行。示例使用 macOS/Linux 作为设备 B，Windows 作为设备 A，设备别名可以按实际情况分配。

**设备 B · macOS/Linux**

```sh
python3 relay.py init --device b --host
python3 relay.py install-skills
```

**设备 A · Windows PowerShell**

```powershell
py -3 relay.py init --device a --host
py -3 relay.py install-skills
```

默认台账是工具目录内的 `ledger.json`。可以用 `init --ledger 路径` 指定本机台账位置。运行 `install-skills --dry-run` 可预览安装位置。只装某个代理的 skill 时，可加 `--claude`、`--codex` 或 `--anti`。

## 手动清单接力

手动模式只需要 Python 标准库和 Git。登记项目后，按工具输出的离场和到达清单操作。

1. 发送端运行 `python3 relay.py add my-app --path /path/to/my-app` 和 `python3 relay.py handoff my-app --to a`。
2. 完成离场清单后，运行 `python3 relay.py depart my-app`。
3. 接收端打开含有该交接记录的台账，运行 `py -3 relay.py pickup my-app`。完成到达清单后运行 `py -3 relay.py arrive my-app`。

两台设备可以共用台账，也可以用 `export 文件` 与 `import 文件` 转移台账记录。离线时，在各自设备用 `device --drop 路径` 设置中转目录，再选 `bundle`、`patch` 或 `folder` 模式。选择 `remote` 时，工具会打印需要针对 Git 远端执行的命令。

## 加密云接力

两台设备都需要包含 v2 的工具源码。分别为运行 relay-desk 的 Python 安装 PyNaCl。安装 `rclone` 后，用 `rclone config` 配置存储连接。两边的连接名可以不同，但必须指向同一个共享目录。若系统 Python 要求使用虚拟环境，请用虚拟环境里的 Python 安装 `pynacl`，并用同一个解释器运行接力命令。

**设备 B · macOS/Linux**

```sh
python3 -m pip install --user pynacl
python3 relay.py cloud remotes
python3 relay.py pair --remote drive_b --root relay-baton
```

**设备 A · Windows PowerShell**

```powershell
py -3 -m pip install --user pynacl
py -3 relay.py cloud remotes
py -3 relay.py pair --remote drive_a --root relay-baton
```

将 `drive_a` 和 `drive_b` 换成各自配置的连接名。两台设备都发布公钥后，分别运行 `pair --status`，核对两个屏幕上的 24 位十六进制配对指纹完全一致，再在各自设备运行 `pair --accept`。

### 从设备 B 发送

```sh
python3 relay.py add my-app --path /path/to/my-app
python3 relay.py note my-app --next "在设备 A 上运行测试"
python3 relay.py send my-app --to a
```

发送端先上传加密包，检查远端大小，并在存储提供 SHA-256 时核对摘要。签名的就绪标记先写入本机，再发布到云端。确认发布且写入本机状态后，命令行显示 **已交棒，可以关机**。接收端可以稍后开机，接棒时会对交接包进行完整性验证。

签名就绪标记保存后若发布中断，重新运行 `send` 会沿用原编号和原交接包继续发布。发送端后来新增的改动留待下一次交接。在发送端运行 `inbox` 时，工具会同步交接编号和包摘要都匹配的签名回执。

### 在设备 A 接收

```powershell
py -3 relay.py inbox
py -3 relay.py receive RELAY_ID --path 'D:\workspace\my-app-incoming'
py -3 relay.py transcripts my-app
```

把 `RELAY_ID` 换成收件箱里显示的交接编号。首次接收请使用尚未创建的目录。接收到已有 Git 工作区时，本机独有的 Git 忽略文件、分叉的同名分支或有冲突的标签会阻止原地接收。可快进的分支正常更新，仅本机存在的分支和标签会保留。遇到冲突可用 `--into 新目录` 单独接收。

接收端在临时目录验签、解密和校验，完成对话归档后再应用工作区。目标路径尚不存在时，工具在同一文件系统准备好工作区后才移入目标位置。已有目录会在预检通过后原地写入。

若归档写入失败，排除错误后重跑原接收命令。若文件已恢复但台账或回执写入失败，重跑 `receive RELAY_ID` 可继续完成交接，保留接收端后续编辑。

## 接力包含哪些内容

- **Git 项目**通过 Git bundle 携带提交、分支和标签，并记录暂存、未暂存改动与符合条件的未跟踪文件。Git 忽略文件通常不随包传输。普通目录使用文件快照。
- **凭据检查**覆盖受支持的项目文件和 Git 历史中的已知凭据文件名和私钥格式。对话或项目文字中的敏感内容仍应按敏感数据处理。
- **对话归档**读取受支持的可见日志，发现缺失或截断时可能阻止发送。用 `transcripts 项目 --session ID`、`--tool codex` 或 `--grep 文本` 查看。换设备时在新会话中继续，原生本地会话可回原设备恢复。
- **云端存储**保存加密包和加密项目预览，也保存公开设备描述、签名就绪标记和签名回执。设备标识、包大小和时间戳等传输字段保留在协议记录中。适配方式见 [StorageProvider 文档](./docs/STORAGE_PROVIDER.md)。
- **本机工作台**通过 `serve` 打开 `http://127.0.0.1:8765/`。在网页设置里配对，在「待接棒」查看来件，也可查阅对话归档。

## 命令速查

使用 `python3 relay.py 命令 --help` 或 `py -3 relay.py 命令 --help` 查看参数。

| 用途 | 命令 |
| --- | --- |
| 设备与项目登记 | `init`、`device`、`add`、`set` |
| 台账、备注与会话 | `status`、`scan`、`note`、`sessions`、`session add`、`session rm` |
| 手动接力清单 | `handoff`、`plan`、`check`、`depart`、`pickup`、`arrive`、`cancel` |
| 云接力与归档 | `cloud remotes`、`pair`、`send`、`inbox`、`receive`、`transcripts` |
| 网页、skills 与备份 | `serve`、`install-skills`、`export`、`import` |

## 从 v1 升级

两台设备都更新工具源码，并继续使用原有台账路径。运行 `install-skills --dry-run` 查看已安装的版本。确定要替换旧 skill 时，运行 `install-skills --force`。手动清单接力仍可继续使用。启用云接力时，两台设备需要分别生成身份并核对配对指纹。

## 运行测试

```sh
python3 -m unittest discover -s tests -v
```

Windows 用 `py -3 -m unittest discover -s tests -v`。云接力和网页集成测试使用本机回环服务和临时数据。完整运行需要 PyNaCl、`rclone` 和 Node.js。跨系统模拟用例会在同一宿主机指定不同的设备系统标签，实际文件系统与 Shell 行为需在对应系统单独验证。

[MIT License](./LICENSE)
