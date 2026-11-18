# relay-desk

**English** | [简体中文](./README.zh-CN.md)

**relay-desk** is a local-first two-machine development relay and handoff ledger for moving coding projects seamlessly between any two desktop machines (**Windows**, **macOS**, or **Linux** — including Win ↔ Mac, Mac ↔ Mac, Win ↔ Win, and Linux ↔ Mac) across **Claude Code**, **Codex**, **Antigravity**, and a **local web workbench**.

All four surfaces share the same local `ledger.json` and cross-agent conversation archive—supporting both **end-to-end encrypted cloud async relay** (hand off on Machine A and shut down immediately; power on Machine B anytime to verify, unpack, and continue) and **zero-dependency manual checklist relay** (100% offline).

```
Claude Code ──┐                                                 ┌── Encrypted Cloud Async Relay (rclone / S3 / WebDAV / Drive)
Codex ────────┼── relay CLI ──► ledger.json + Archive ◄─────────┤
Antigravity ──┤                       ▲                         └── Manual Checklist Relay (remote / bundle / patch / folder)
Web Workbench ┴──── relay serve ──────┘
```

---

## Why relay-desk?

Switching between two computers mid-feature breaks down in predictable ways: half-finished staged and unstaged edits, team repos where pushing personal WIP branches is undesirable, machine-local skills/rules, or losing detailed conversation context discussed on the previous machine.

**relay-desk** classifies project and machine state into four explicit tiers—and carries everything portable in one authenticated, encrypted relay package:

| Tier | Meaning | Examples |
|---|---|---|
| **Native** | Opens the exact same live session on both machines | Cloud sessions, Claude Remote Control |
| **Git** | Travels with commits or `git bundle` | Committed code, `CLAUDE.md`, `AGENTS.md`, `GEMINI.md`, `.claude/skills/`, `.codex/`, `.agents/`, `HANDOFF.md` |
| **Extra** | Uncommitted or outside Git; bundled automatically in Cloud Relay or carried via WIP branch / `bundle` / patch / drop folder in Manual mode | **Staged** changes, **unstaged** changes, untracked files, deduplicated full visible conversation archives across all 3 agents, global skills & rules |
| **Manual** | Never synced; configured or resumed per machine | OAuth logins (`~/.claude.json`, `~/.codex/auth.json`, `~/.gemini/oauth_creds.json`), `.env` secrets, private keys, local SQLite session databases, running processes |

---

## Key Features

- **End-to-End Encrypted Cloud Async Relay (Time-Shifted Handoff)**:
  - Encrypts locally using `PyNaCl` (libsodium `crypto_secretstream_xchacha20poly1305` chunked streaming encryption + X25519 key wrapping + Ed25519 detached signatures). Cloud storage only ever sees ciphertext.
  - **Exact Workspace Restoration**: Carries Git commits/branches, **staged index (`git diff --cached`)**, **unstaged working tree (`git diff`)**, and **untracked files** (or non-Git folder snapshots). Enforces a **pre-pack secret gate** that excludes `.env`/credential files without reading them and aborts before upload if any tracked file or Git commit contains private keys or credential files.
  - **Two-Phase Atomic Publish & Shutdown Confirmation**: Uploads `package.enc` and verifies remote size first, then uploads signed `ready.json` and verifies remote presence before printing **`已交棒，可以关机`** (*"Handoff complete, safe to shut down"*). Interrupted uploads reuse the same `relayId` on retry; incomplete uploads without `ready.json` are never shown as claimable.
  - **Safe Temp Verification & Dirty-Target Protection**: Receivers verify Ed25519 signatures, SHA-256 digests, and `secretstream` integrity inside an isolated temporary directory before touching any workspace. If the local target directory has unsynced edits or commits, the original directory is preserved untouched and `--into <new_dir>` lets you receive cleanly into a new path.
- **Full Cross-Agent Conversation Archiving & Search**:
  - Detects missing or truncated/unflushed session logs before sending (reporting exact gaps so you can fix and retry), archives all visible conversations from **Claude Code**, **Codex**, and **Antigravity**, and deduplicates by `(tool, sessionId, contentVersion)` across `A → B → A` round-trips with zero duplicates. Browse and search anytime via `relay transcripts` or the Web UI viewer.
- **Swappable Cloud Storage (`StorageProvider`)**:
  - Ships with an `rclone` adapter supporting OneDrive, Google Drive, Dropbox, WebDAV, and S3-compatible object storage out of the box—Device A and Device B can even use different `rclone` remote names pointing to the same shared cloud folder. Implement `StorageProvider` to plug in any custom backend (see [`docs/STORAGE_PROVIDER.md`](./docs/STORAGE_PROVIDER.md)).
- **Zero-Dependency Manual Fallback**:
  - Ledger management, Git/session scanning, the loopback Web UI (`relay serve`), and manual cross-shell (`PowerShell` / `zsh` / `bash`) handoff checklists work on pure Python 3.9+ standard library with zero `pip` packages.

---

## Requirements & Setup

- **Core (Required)**: **Python 3.9+** and **Git**.
- **Encrypted Cloud Async Relay (Optional, Recommended)**:
  1. Install `PyNaCl` (libsodium bindings):
     ```bash
     # macOS / Linux
     python3 -m pip install --user pynacl
     # Windows
     py -3 -m pip install --user pynacl
     ```
  2. Install and configure [`rclone`](https://rclone.org/) for your preferred cloud drive, WebDAV, or S3 bucket:
     ```bash
     # macOS
     brew install rclone
     # Windows
     winget install Rclone.Rclone
     # Authorize your cloud remote:
     rclone config
     ```

---

## Quick Start

Run the commands below inside the `relay-desk` directory (`relay` stands for `py -3 relay.py` or `relay.cmd` on Windows, and `python3 relay.py` or `./relay` on macOS/Linux).

### 1. Initialize Both Machines & Install Agent Skills

On **Device A**:

```bash
relay init --device a --host
relay install-skills
```

On **Device B** (run `chmod +x relay` once on macOS/Linux):

```bash
relay init --device b --host
relay install-skills
```

- `install-skills` installs the `relay-desk` skill for all three agents by default (or individually via `--claude`, `--codex`, `--anti`):
  - Claude Code → `~/.claude/skills/relay-desk`
  - Codex → `~/.codex/skills/relay-desk` (or `$CODEX_HOME/skills/relay-desk`)
  - Antigravity → `~/.gemini/config/skills/relay-desk` (or `$GEMINI_CONFIG_DIR/skills/relay-desk`)

### 2. Pair Cloud Async Relay (Recommended)

Device A and Device B can use different `rclone` remote names as long as they point to the same shared cloud folder:

```bash
# 1. List configured rclone remotes on this machine
relay cloud remotes

# 2. Publish Device A's signed public descriptor
relay pair --remote <remote_on_a> --root relay-desk

# 3. Publish Device B's signed public descriptor to the same shared folder
relay pair --remote <remote_on_b> --root relay-desk

# 4. Verify the 24-character pairing fingerprint matches on BOTH screens, then accept on each:
relay pair --accept
relay pair --status
```

*(You can also complete pairing visually in `relay serve` under **Settings → 云接力与设备配对**.)*

---

## Daily Workflow

### Register a Project

```bash
relay add my-app --path /path/to/my-app --tool all
```

### Workflow A: Encrypted Cloud Async Relay (Recommended)

#### 1. Leaving Machine A: Say *"接力"* to Your Agent (or run CLI)

1. Update `HANDOFF.md`, record notes (`relay note`), and optionally register the active session (`relay session add my-app latest --tool <claude|codex|anti>`).
2. Encrypt, upload, and verify remotely:
   ```bash
   relay send my-app --to b
   ```
3. Once remote verification succeeds, the CLI (and Web UI banner) outputs **`已交棒，可以关机`** (*"Handoff complete, safe to shut down"*). You can power off Machine A immediately.

#### 2. Arriving at Machine B: Say *"接棒"* to Your Agent (or open the **待接棒** tab in Web UI)

1. Check incoming encrypted packages and decrypted previews:
   ```bash
   relay inbox
   ```
2. Verify signatures, decrypt in temp staging, restore code + staged + unstaged + untracked files, and upload a signed receipt:
   ```bash
   # First time receiving this project on Machine B:
   relay receive <relay_id> --path /path/to/my-app

   # Subsequent receives:
   relay receive <relay_id>

   # If local directory has unsynced edits, original dir is preserved untouched; receive into a new dir:
   relay receive <relay_id> --into /path/to/my-app-incoming
   ```
3. Browse or grep full archived conversations across **Claude Code**, **Codex**, and **Antigravity**:
   ```bash
   relay transcripts my-app                        # List archived sessions
   relay transcripts my-app --session <session_id> # Read full Markdown conversation
   relay transcripts my-app --tool codex           # Filter by agent tool
   relay transcripts my-app --grep "auth token"    # Search across all archived turns
   ```

---

### Workflow B: Manual Checklist Relay (Zero Dependencies / Offline)

Without `PyNaCl` or `rclone`, configure a drop folder (`relay device --drop <dir>`) and use the 4 manual transport modes (`remote`, `bundle`, `patch`, `folder`):

1. **Leaving**: `relay handoff <project>` → step through safety-tagged commands (`只读` / `只改本机` / `写入远程`) → `relay check <project> pre <key>` → `relay depart <project>`.
2. **Arriving**: `relay pickup <project>` → apply bundle/patch/fetch commands → `relay arrive <project>`.
3. **Returning to the Original Machine**: After syncing code back, resume native local sessions on their origin machine via `claude --resume <id>`, `codex resume <id>`, or `agy --conversation <id>`.

---

## CLI Reference

When `PROJECT` is omitted, `relay` resolves the project from your current working directory (transparently handling macOS `/var` ↔ `/private/var` symlinks and Unicode NFC/NFD paths).

| Command | Description |
|---|---|
| `init [--device a\|b] [--ledger PATH] [--host]` | Write machine-local config (`~/.relay-desk/config.json`); `--host` records hostname. Creates ledger if missing. |
| `cloud remotes [--rclone-config PATH] [--json]` | Discover configured `rclone` remote names on this machine. |
| `pair [--remote R] [--root P] [--provider rclone\|local] [--rclone-config PATH] [--accept] [--status] [--json]` | Inspect cloud pairing status, publish signed Ed25519/X25519 public keys, verify the shared SAS fingerprint, and pin peer keys (`--accept`). |
| `send [PROJECT] [--to a\|b] [--json]` | Run secret gate & session gap checks, encrypt & upload package + signed `ready.json`, verify remotely, and output **`已交棒，可以关机`**. |
| `inbox [--json]` | Sync signed receipts for sent relays and list claimable incoming relays addressed to this device. |
| `receive RELAY_ID [--path DIR] [--into DIR] [--json]` | Verify & unpack incoming relay (`--path` on first receive; preserves dirty local dir and supports `--into` new dir) and upload signed `receipt.json`. |
| `transcripts [PROJECT] [--session ID] [--tool claude\|codex\|anti] [--grep PATTERN] [--json]` | List, read full Markdown, or search deduplicated historical conversations across Claude Code, Codex, and Antigravity. |
| `status [PROJECT] [--json]` | One-line overview per project, or full details, 4-tier board, and session resume commands for `PROJECT`. |
| `scan [PROJECT] [--all]` | Refresh local Git facts, state file presence, and discovered sessions (`--all` scans all projects + global state). |
| `add NAME --path PATH [--tool T] [--id ID]` | Register a project and scan immediately (`T`: `claude`, `codex`, `anti`, `both`, `claude+anti`, `codex+anti`, `all`). |
| `set PROJECT [...]` | Update project fields (`--path-here`, `--path-other`, `--alt-path`, `--tool`, `--status`, `--transport`, `--worktree`, `--device`). |
| `note PROJECT [...]` | Record `--last` task, `--next` action, `--note`, and `--verify` (`none\|pass\|partial\|fail`, `--verify-cmd`, `--verify-note`). |
| `sessions [PROJECT] [--tool claude\|codex\|anti] [--json]` | List discovered local sessions on this machine (`*` marks registered sessions). |
| `session add PROJECT ID\|latest [--tool T] [...]` | Register a session (`latest` selects the newest discovered session for that tool). |
| `session rm PROJECT ID` | Unregister a session by internal ID or `sessionId`. |
| `handoff PROJECT [--transport T] [--to D]` | Start a manual checklist handoff and print pre-departure commands. |
| `plan PROJECT [--phase pre\|post] [--json]` | Reprint checklist & command blocks for an active manual handoff. |
| `check PROJECT pre\|post KEY [--undo]` | Check or uncheck a manual checklist item. |
| `depart PROJECT` / `pickup PROJECT` / `arrive PROJECT` | Mark manual handoff in transit, view arrival checklist on receiver, or complete arrival. |
| `cancel PROJECT` | Cancel an active handoff marker. |
| `conflict add PROJECT --kind K [...]` | Log a conflict (`both`, `merge`, `instr`, `session`, `other`). |
| `device [--which a\|b] [--name N] [--os O] [--drop PATH]` | View or update device settings (`windows`, `mac`, `linux`, and drop folder). |
| `export [FILE]` / `import FILE [--replace]` | Export or import ledger JSON backups (merges by `updatedAt` by default; `--replace` overwrites). |
| `serve [--port 8765] [--no-open]` | Start the loopback-only Web UI (`127.0.0.1`) with Inbox (**待接棒**), Cloud Pairing, and Transcript Viewer. |
| `install-skills [--claude] [--codex] [--anti] [--dry-run] [--force]` | Install agent skills with `{{RELAY}}` resolved for this machine. |

---

## Security & Design Guarantees

1. **Never Pushes to Your Code Git Remote**: Neither `relay` nor its agent skills ever run `git push` against your code repository's remote. Cloud relay uploads only libsodium-encrypted packages to your explicitly paired cloud storage root.
2. **Strict Credential Gate**: `~/.claude.json`, `.credentials.json`, `~/.codex/auth.json`, `~/.gemini/oauth_creds.json`, `.env`, and private keys are never opened or copied; if private keys or credential files are found tracked in Git history or staged changes, `relay send` aborts before uploading.
3. **Summaries & Transcripts Are Not Native Live Sessions**: `HANDOFF.md` and `relay transcripts` provide honest context summaries and searchable full dialogue archives without corrupting local SQLite session databases or falsely claiming native session restoration.
4. **Temp-Staged Verification & Non-Destructive Receive**: Signatures, SHA-256 hashes, and `secretstream` frames are verified in a temp directory before touching any project path, and dirty local directories are never overwritten.
5. **Loopback-Protected Web UI**: `relay serve` binds strictly to `127.0.0.1` and enforces `Host`, `Origin`, and `X-Relay-Desk: 1` checks.

---

## Project Structure

```
relay-desk/
├── relay.py                 # Cross-platform CLI entry point
├── relay / relay.cmd        # POSIX / Windows wrapper scripts
├── relay-desk.html          # Single-file Web UI (also compatible as a claude.ai Artifact)
├── relaydesk/
│   ├── ledger.py            # Atomic JSON storage, file locking, path canonicalisation & device lookup
│   ├── scan.py              # Read-only Git facts, state presence & Claude/Codex/Antigravity session discovery
│   ├── plan.py              # 4-tier classification, manual checklists & cross-OS shell command generator
│   ├── storage.py           # Pluggable StorageProvider interface + rclone & LocalDir adapters
│   ├── crypto.py            # Device identity, SAS pairing fingerprints, Ed25519 & libsodium secretstream
│   ├── conversations.py     # Full visible conversation extraction, gap detection, dedup archive & grep
│   ├── packager.py          # Git bundle + staged/unstaged/untracked packager, secret gate & clean unpacker
│   ├── relay_engine.py      # Async cloud relay orchestrator (pair, 2-phase send, inbox, receive & receipts)
│   ├── cli.py               # Command-line interface & skill installer
│   └── server.py            # Local 127.0.0.1 HTTP server & JSON API
├── skills/
│   ├── claude/relay-desk/   # Claude Code SKILL.md template
│   ├── codex/relay-desk/    # Codex SKILL.md template
│   └── anti/relay-desk/     # Antigravity SKILL.md template
├── docs/
│   ├── SPEC.md              # Architecture & module specification
│   └── STORAGE_PROVIDER.md  # Pluggable cloud storage contract & adapter guide
└── tests/                   # Standard-library unittest & end-to-end acceptance suite
```

## Running Tests

All tests run in isolated temporary directories using Python's built-in `unittest` framework:

```bash
# macOS / Linux
python3 -m unittest discover -s tests -v

# Windows
py -3 -m unittest discover -s tests -v
```

## License

Released under the [MIT License](./LICENSE).
