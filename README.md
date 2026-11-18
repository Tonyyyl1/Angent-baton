# relay-desk

**English** | [简体中文](./README.zh-CN.md) | [Changelog](./CHANGELOG.md)

relay-desk keeps a development handoff ledger for two devices, `a` and `b`. It works through the CLI, a local web workbench, and skills for Claude Code, Codex, and Antigravity. The manual workflow tracks the transfer with a checklist. The v2 cloud workflow adds encrypted transfer of Git workspaces or folders and searchable archives of supported conversation logs.

## Set up both devices

Install Python 3.9 or later and Git, then clone the repository on each device.

```sh
git clone https://github.com/Tonyyyl1/Angent-baton.git
cd Angent-baton
```

Run the examples from the tool directory. The examples use macOS/Linux as Device B and Windows as Device A. The same aliases work with other OS combinations.

**Device B · macOS/Linux**

```sh
python3 relay.py init --device b --host
python3 relay.py install-skills
```

**Device A · Windows PowerShell**

```powershell
py -3 relay.py init --device a --host
py -3 relay.py install-skills
```

The default ledger is `ledger.json` in the tool checkout. To place it elsewhere, supply a device-local path with `init --ledger PATH`. Run `install-skills --dry-run` to preview where the skills will be installed. Use `--claude`, `--codex`, or `--anti` to install only the skill you use.

## Manual handoff

The manual workflow uses Python's standard library and Git. Register the project, select a transport, then follow the printed checklist from each device.

1. On the sender, run `python3 relay.py add my-app --path /path/to/my-app` and `python3 relay.py handoff my-app --to a`.
2. Complete the pre-departure checklist and run `python3 relay.py depart my-app`.
3. On the receiver, open the matching ledger entry and run `py -3 relay.py pickup my-app`. Complete the arrival checklist and run `py -3 relay.py arrive my-app`.

Use a shared ledger or transfer a ledger backup with `export FILE` and `import FILE` so the receiver has the handoff record. For offline transfer, configure `device --drop PATH` on each device and use the `bundle`, `patch`, or `folder` transport. The `remote` transport prints Git commands to run against your remote.

## Encrypted cloud handoff

Both devices need a checkout containing v2. Install PyNaCl for the Python interpreter you will use to run relay-desk. Install `rclone` and use `rclone config` on each device to configure access to the same storage location. The two remote names may differ. If your Python installation requires a virtual environment, use its Python executable for both `pip install pynacl` and the relay-desk commands.

**Device B · macOS/Linux**

```sh
python3 -m pip install --user pynacl
python3 relay.py cloud remotes
python3 relay.py pair --remote drive_b --root relay-baton
```

**Device A · Windows PowerShell**

```powershell
py -3 -m pip install --user pynacl
py -3 relay.py cloud remotes
py -3 relay.py pair --remote drive_a --root relay-baton
```

Replace `drive_a` and `drive_b` with your configured names. Once both devices have posted their public keys, run `pair --status` on each. Compare the full 24-digit hexadecimal pairing fingerprint on both screens and run `pair --accept` on each only when they match.

### Send from Device B

```sh
python3 relay.py add my-app --path /path/to/my-app
python3 relay.py note my-app --next "Run the tests on Device A"
python3 relay.py send my-app --to a
```

The sender uploads the encrypted package and checks its remote size and any SHA-256 supplied by the storage provider. It saves the signed ready marker locally before publishing it. The CLI prints **已交棒，可以关机** after verifying publication and updating local state. The receiver can come online later and verifies the package contents during receive.

If publication is interrupted after the marker is saved, rerun `send` to finish the same handoff with the original package. Keep subsequent changes for the next handoff. Running `inbox` on the sender also syncs signed receipts that match the handoff ID and package digest.

### Receive on Device A

```powershell
py -3 relay.py inbox
py -3 relay.py receive RELAY_ID --path 'D:\workspace\my-app-incoming'
py -3 relay.py transcripts my-app
```

Replace `RELAY_ID` with the ID from `inbox`. Use a new path for the first receive. For an existing Git checkout, receiver-only ignored files, divergent branches, and conflicting tags can block the receive. Compatible branches can fast-forward and local-only refs are kept. Use `--into NEW_DIR` to receive separately when a conflict is reported.

The receiver verifies and decrypts the package, then merges the conversation archive before applying workspace changes. For a destination that does not exist yet, it builds the workspace on the destination filesystem and moves it into place after preparation. An existing destination is updated in place after the checks.

If the archive write fails, rerun the original receive command after resolving the error. If the workspace is already restored and a later ledger or receipt step fails, rerun `receive RELAY_ID` to finish without reapplying the workspace.

## Scope of a transfer

- Git mode packages bundled commits, branches and tags, staged and unstaged changes, and eligible untracked files. Git-ignored files are outside the normal Git transfer. Folder mode snapshots eligible files from a regular directory.
- The send checks recognized credential filenames and private-key patterns in supported project files and Git history. The archive and any secrets present in project prose or conversation text should be treated as sensitive.
- Supported visible conversation logs can be archived and searched with `transcripts PROJECT --session ID`, `--tool codex`, or `--grep TEXT`. Detected gaps in selected logs can block a send. The other device starts a new agent session with the archived context. Native local sessions can be resumed on their original device.
- Packages and project previews are encrypted for the paired devices. Cloud storage also holds public device descriptors, signed ready markers, and signed receipts. The protocol keeps transport fields such as device identifiers, package size, and timestamps in those records. See the [StorageProvider contract](./docs/STORAGE_PROVIDER.md).
- `serve` starts the local workbench at `http://127.0.0.1:8765/` with pairing settings, the **待接棒** inbox, and transcript browsing. The CLI and local workbench use the same ledger.

## Command reference

Use `python3 relay.py COMMAND --help` or `py -3 relay.py COMMAND --help` for flags and arguments.

| Task | Commands |
| --- | --- |
| Device and project setup | `init`, `device`, `add`, `set` |
| View and update local records | `status`, `scan`, `note`, `sessions`, `session add`, `session rm` |
| Manual checklist | `handoff`, `plan`, `check`, `depart`, `pickup`, `arrive`, `cancel` |
| Cloud transfer and archives | `cloud remotes`, `pair`, `send`, `inbox`, `receive`, `transcripts` |
| Other tools | `serve`, `install-skills`, `export`, `import` |

## Upgrade from v1

Update the tool checkout on both devices and keep using the existing ledger path. Run `install-skills --dry-run` to check the installed copies. Use `install-skills --force` if you intend to replace the previous copies. Manual handoffs remain available. Each device needs its own pairing identity for the cloud workflow.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

On Windows use `py -3 -m unittest discover -s tests -v`. The cloud and web integration tests use loopback services and temporary fixtures. For full coverage, make PyNaCl, `rclone`, and Node.js available. The cross-OS simulation sets device OS labels on one host. Native filesystem and shell behavior require separate runs on the corresponding OS.

[MIT License](./LICENSE)
