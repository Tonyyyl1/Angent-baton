# relay-desk 原生与真实多设备验收工具

> The acceptance tools include an environment probe, synthetic fixtures, a round-trip state
> comparator, a per-platform native runner (`run-native.sh` / `run-native.ps1`), and a three-device ring
> driver (`ring.py`) that runs either as a single-machine simulation or per device on real hardware. Every
> result carries a `level` detected from the running OS (never from an argument) and the working-tree
> source digest. Fingerprints are never auto-accepted. See the acceptance checklist for what
> still needs real Windows / Linux machines or a public cloud, and what evidence to send back.

所有脚本只调用真实的 `relay` 命令（统一列在 `adapter.py` 的命令表），不手工修改台账；只使用合成项目；
本机状态（`RELAY_DESK_HOME`、台账、用户目录）放在验收专用工作目录，不碰 `~/.relay-desk` 和仓库里的
`ledger.json`，不读取任何凭据文件。

## 1 文件

| 文件 | 作用 |
| --- | --- |
| `source_digest.py` | 记录工作区源码摘要，`--package` 打包验收源码 |
| `common.py` | 层级判定、源码摘要（含解包后的校验）、结果目录命名、命令原始日志 |
| `adapter.py` | **唯一的命令适配表**：常规命令和按版本探测的可选命令（`doctor`、`cache status/prune --dry-run`、流式 `sync --jsonl`）；存储规格与只读列举 |
| `collect_env.py` | 环境探测（JSON）：源码摘要、真实系统与版本、工作目录文件系统（大小写、符号链接、执行位、Unicode、长路径）、Python/Git/rclone/Node/PowerShell 版本 |
| `fixtures.py` | 合成 Git 项目与普通目录；"用户继续开发"的编辑 |
| `compare_state.py` | 采集与比较：提交、暂存/未暂存差异、未跟踪、忽略文件、模拟凭据、Git index、分支、标签、交接历史、签收摘要；输出 JSON 差异报告 |
| `platform_probes.py` | 原生平台探针（Windows 保留名/非法字符/长路径/PowerShell/`msvcrt` 锁/文件占用替换；Linux/macOS 大小写、符号链接、执行位、`flock`、进程被杀后的锁恢复、`/var` 别名） |
| `native_runner.py`、`run-native.sh`、`run-native.ps1` | 每个平台的原生运行器 |
| `unittest_capture.py` | 跑 unittest，保留原始日志并逐条记录跳过原因 |
| `ring.py` | 三设备环形接力与网络恢复；`simulate` / `device` / `runbook` / `aggregate` |
| `steps/ring-3device.json` | 步骤文件：Mac → Windows → Linux → Mac 及全部恢复场景（89 步） |
| `steps/loopback-2device.json` | 原生运行器用的单机双实例回环 |

## 2 结果层级（level）

| level | 含义 |
| --- | --- |
| `native-macos` / `native-windows` / `native-linux` | 在该系统上原生运行（`platform.system()` 判定）；`wsl-linux` 单独标注 |
| `macos-local-simulation`（及 `linux-`/`windows-local-simulation`） | 一台机器上多个隔离实例，本地共享目录作存储，系统标签为模拟 |
| `real-multidevice` | 聚合后三个角色来自三台不同机器，且每台的真实系统与角色名一致 |
| `partial-multidevice` / `single-machine-multiinstance` / `role-mismatch-simulation` | 达不到上一条时如实标注 |

每个结果在开始和结束时各记录一次源码摘要；运行期间源码有变化时写 `sourceChangedDuringRun: true`，
这样的结果只能作参考，需要在冻结的源码上重跑。

原生运行器里的回环（两个实例在同一台机器）标为 `native-<系统>` + `topology: single-machine-loopback`：
它证明该系统和文件系统上的行为，不能算多设备证据。只跑了部分 unittest 时状态为
`passed-subset-only`，不会写成 `passed`。

## 3 准备被测源码（每台设备相同）

在源码目录生成验收包。

```sh
python3 acceptance/source_digest.py --out <输出目录> --package
```

把 `acceptance-source.tar.gz` 拷到 Windows / Linux，解包后在 `relay-desk-src/` 目录里运行。解包目录没有
`.git` 时，工具会按同一公式重算摘要并与包内 `SOURCE-DIGEST.json` 比对（`env.json` 里
`source.verifiedAgainstPackage` 必须为 `true`）。

```powershell
# Windows（PowerShell）
tar -xzf acceptance-source.tar.gz          # 或 py -3 -c "import tarfile;tarfile.open('acceptance-source.tar.gz').extractall()"
cd relay-desk-src
py -3 -m pip install --user pynacl
```

```sh
# Linux
tar -xzf acceptance-source.tar.gz && cd relay-desk-src
python3 -m pip install --user pynacl
```

每台设备需要 Python 3.9+、Git、PyNaCl；云盘验收还需要已配置好的 rclone。

## 4 原生验收（每个系统各跑一次）

```sh
# macOS / Linux：完整 unittest（macOS 自动再跑一遍 TMPDIR=/private/tmp）+ 平台探针 + 单机回环
sh acceptance/run-native.sh
sh acceptance/run-native.sh --quick        # 省掉约 35 秒的台账锁恢复探针
```

```powershell
# Windows
powershell -NoProfile -ExecutionPolicy Bypass -File acceptance\run-native.ps1
```

输出目录 `acceptance/results/<UTC>_<level>_<摘要12位>_native/`：

- `env.json`：环境探测
- `unittest-default.log` / `.json`（macOS 另有 `unittest-private-tmp.*`）：原始日志；JSON 逐条列出每个跳过的测试及原因
- `probes.json`：平台探针
- `loopback/`：两个实例经真实 CLI 完成 Git 与普通目录往返、`kill -9` 后恢复、工作区文件被另一进程占用时接棒（`L18`，Windows 文件占用的关键证据）
- `commands.jsonl` + `raw/`：每条命令及原始 stdout/stderr
- `summary.json`：各部分状态与 `stateGuard`（确认 `~/.relay-desk`、仓库 `ledger.json` 未被改动，只看大小和时间，不读内容）

## 5 三设备接力

### 5.1 本机模拟（Mac）

```sh
python3 acceptance/ring.py simulate                       # 结果在 acceptance/results/..._macos-local-simulation_..._ring-sim/
python3 acceptance/ring.py simulate --only-phase pair --only-phase ring   # 只跑配对和主环
```

三个实例各有独立的 `RELAY_DESK_HOME`、台账、用户目录和 `HOME`；存储是本地共享目录。离线用"把设备目录
移走"模拟，断网用"去掉共享目录权限"模拟，指纹由"读取另一实例自己的显示值、两边一致才确认"模拟。
这些都写在结果的 `simulation.json` 里。

### 5.2 真实设备

1. 选一个运行编号，例如 `2026-11-20-a`。存储二选一：
   - 云盘：`rclone:<本机的远端名>:relay-acceptance/2026-11-20-a`（远端名每台可以不同，前缀必须相同；前缀必须含 `relay-acceptance`，工具会拒绝其他前缀）
   - 局域网共享目录：`local:<挂载路径>`（空目录或之前验收建立的目录）
2. 打印步骤表给大家看：`python3 acceptance/ring.py runbook --run-id 2026-11-20-a --out RUNBOOK.md`
3. 三台同时启动（各自只跑自己的步骤，通过共享存储等待对方）：

```sh
# Mac
python3 acceptance/ring.py device --role mac   --workdir ~/relay-acceptance/2026-11-20-a --storage rclone:<远端>:relay-acceptance/2026-11-20-a
# Linux
python3 acceptance/ring.py device --role linux --workdir ~/relay-acceptance/2026-11-20-a --storage rclone:<远端>:relay-acceptance/2026-11-20-a
```

```powershell
# Windows
py -3 acceptance\ring.py device --role win --workdir $env:USERPROFILE\relay-acceptance\2026-11-20-a --storage rclone:<远端>:relay-acceptance/2026-11-20-a
```

4. 需要人工的地方，脚本会停下来提示：
   - **配对指纹**：本机先打印自己显示的指纹，然后请你输入**另一台设备屏幕上**的指纹；不一致或留空都不会接受。
     也可以用 `--fingerprint win=XXXX-...` 传入（值必须是从对方屏幕读到的）。换钥后（K01）Linux 会打印新的指纹，
     Mac 和 Windows 要据此重新核对。
   - **离线 / 断网**：按提示拔网线或关 Wi-Fi，脚本会确认存储确实不可达，再提示何时恢复。
5. 中断后用同一条命令重跑即可，已通过的步骤会跳过。源码摘要变了会拒绝续跑，必须换新的工作目录。
6. 角色名与真实系统不符时脚本拒绝运行；加 `--allow-role-mismatch` 才会继续，结果标为 `role-mismatch-simulation`。

### 5.3 覆盖的断言

- 主环 R01–R11：每跳比较提交、暂存/未暂存差异、未跟踪、忽略文件不传输、模拟凭据不传输、Git index、分支、标签；
  接收端用包内携带的发送端摘要现场核对（`note` 字段），回到 Mac 后再做完整逐项比较。
- R03：非目标设备即使已缓存快照也不能领取（交接编号和快照编号都试）。
- F03–F10：写文件中途和工作区提交后进程被杀，重启后继续 `receive` 完成。
- F13–F15：台账更新开始后 `recover --rollback` 被拒，`receive` 完成并上传回执。
- F18–F23：回滚开始后（包括回滚中途被杀）`receive` 拒绝签收，云端和本地都没有回执；完成回滚后可重新接棒。
- K01–K07：换钥后旧配对暂停、交棒被拒，人工重新核对后恢复。
- N01–N17：原发布者离线时其余设备同步快照并继续接力；接收方离线后上线再接棒；迟到回执不回退所有权。
- N18–N27：断网时交棒/接棒失败且不留半成品、工作区不变；恢复后重试沿用同一交接编号，收件箱恰好一个。
- E01–E09：交棒后继续编辑：在途交接时禁止发布、编辑保留；回来时目标有未发送编辑则拒绝覆盖，`--into` 接收。
- C01–C03：所有发出的交接都已签收、所有回执都已上传、同步无错误。

### 5.4 汇总

把三台的结果目录拿回 Mac：

```sh
python3 acceptance/ring.py aggregate <mac结果目录> <win结果目录> <linux结果目录> --out <汇总目录>
```

生成 `ring-summary.json` / `ring-summary.md`：层级（只有三台不同机器且系统与角色一致才是 `real-multidevice`）、
每步状态、各场景结论、跨设备逐跳比较（`compare-aggregate/`）。

## 6 真实云盘

- 只用每台设备上**已经配置好**的 rclone 远端；工具不创建远端、不读 rclone 配置，只用 `relay pair --remote` 和只读的
  `rclone lsf` / `rclone cat`（限于验收前缀，用来确认回执是否存在、断网是否生效）。
- 前缀固定为 `relay-acceptance/<运行编号>`，只放合成项目。验收结束后由你自己清理：
  `rclone purge <远端>:relay-acceptance/<运行编号>`（工具不会删除云端内容）。
- 本机 WebDAV/S3 回环结果不能代替公网云盘结果。

## 7 打包验收结果

将工作目录（`--workdir`）和结果目录放在本机磁盘上。运行结束后，可打包整个结果目录。

```sh
python3 -m zipfile -c relay-acc-<系统>.zip <结果目录>      # Windows 用 py -3
```

将压缩包复制到汇总结果的电脑，再按平台或设备解压。结果目录包含合成项目的状态、命令行和日志，模拟凭据以 `DUMMY-NOT-A-SECRET-` 开头。

## 8 待外部验收清单

| # | 项目 | 需要 | 运行 | 送回的证据 | 通过标准 |
| --- | --- | --- | --- | --- | --- |
| X1 | 原生 Windows | 真实 Windows 10/11（非虚拟化的 NTFS 盘） | `run-native.ps1`（不加 `--quick`） | `..._native-windows_..._native/` 整个目录 | `summary.overall = passed`；`unittest-default.json` 里 Windows 专用测试（如 `test_scan` 的 Windows 路径语义）实际执行未跳过；探针 `windows-reserved-and-invalid-names`、`powershell-invokes-relay-cmd`、`workspace-lock-released-on-kill`（msvcrt）、`replace-file-held-open` 为 ok；回环 `L18` 记录了文件占用时的结果且最终完成 |
| X2 | 原生 Linux | 真实 Linux（ext4/xfs/btrfs，不是 WSL） | `sh acceptance/run-native.sh` | `..._native-linux_..._native/` | `level = native-linux`；`overall = passed`；`fs-case-sensitivity`、`symlink-preserve-and-escape`、`exec-bit-preserved`、`workspace-lock-released-on-kill`（flock）、`ledger-lock-left-by-killed-process` 为 ok；回环 `L13–L15` kill -9 后恢复 |
| X3 | macOS 完整回归 | 本 Mac，源码定稿后 | `sh acceptance/run-native.sh` | `..._native-macos_..._native/` | 默认临时目录与 `/private/tmp` 两轮完整套件通过，跳过项逐条有原因 |
| X4 | 三设备实际接力 | Mac + Windows + Linux 三台真机，同一份源码包 | §5.2 三台各跑 `ring.py device`，然后 §5.4 汇总 | 三个 `..._ring-<角色>/` 目录 + 汇总目录 | `level = real-multidevice`；`sameSource = true`；89 步除已知问题外全部 passed；跨设备比较全部 ok |
| X5 | 实际网络恢复 | 同 X4，拔网线/关 Wi-Fi | X4 中的 N04–N27 步骤按提示真实断网 | X4 的结果（含 `offlineAt`/`onlineAt` 时间） | 断网期间存储确认不可达；N 阶段全部 passed |
| X6 | 公网云盘 | 一个已配置的公网 rclone 远端（任意提供商） | X4 使用 `rclone:<远端>:relay-acceptance/<编号>` | X4 结果 + 提供商类型（不要发凭据） | 同 X4；`device-summary.json` 的 `storage.kind = rclone` |
| X7 | 硬件断电（可选） | 任意一台真机 | 在一个较大项目的 `receive` 过程中直接断电，开机后 `relay recover <编号> --status`，再 `relay receive <编号>` | 两条命令的输出 + `compare_state.py capture` 的前后状态 | 能继续完成或回滚；没有半写文件；回滚开始后没有回执 |

## 9 可选命令

`adapter.OPTIONAL_COMMANDS` 已登记 `doctor`、`cache status`、`cache prune --dry-run`、`sync --watch --jsonl`。
每次运行先用 `--help` 探测，存在才调用，并在结果里写明 `available: true/false`；不会因为命令缺失而判为通过。
