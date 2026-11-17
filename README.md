# relay-desk

**English** | [简体中文](./README.zh-CN.md)

**relay-desk** is a local-first, zero-dependency handoff ledger for moving coding projects smoothly between **Windows** and **macOS** across **Claude Code**, **Codex**, **Antigravity**, and a **local web dashboard**.

All four surfaces read and write the exact same `ledger.json` file on disk—tracking which machine currently holds your latest working state, uncommitted changes, branch, verification status, next action, and local agent session IDs.

```
Claude Code ──┐
Codex ────────┼── relay CLI ──► ledger.json ◄── relay serve ◄── Web UI
Antigravity ──┘
```

---

## Why relay-desk?

Switching between a Windows desktop and a MacBook mid-feature usually breaks down in predictable ways: dirty working trees, unpushed branches on team repos where force-pushing is off-limits, forgotten local rules/skills, or confusion over what transfers via Git versus what stays locked inside a local agent database.

**relay-desk** classifies every piece of project and machine state into four explicit tiers:

| Tier | Meaning | Examples |
|---|---|---|
| **Native** | Opens the exact same live session on both machines | Cloud sessions, Claude Remote Control |
| **Git** | Travels with commits once pushed or bundled | Committed code, `CLAUDE.md`, `AGENTS.md`, `GEMINI.md`, `.claude/skills/`, `.codex/`, `.agents/`, `HANDOFF.md` |
| **Extra** | Lives outside Git or is uncommitted; carried via WIP branch, `git bundle`, patch, or drop folder | Dirty files, `CLAUDE.local.md`, Claude memory, global skills (`~/.claude/skills/`, `~/.codex/skills/`, `~/.gemini/config/skills/`), optional transcript / `brain/` artifacts |
| **Manual** | Never synced; configured or resumed per machine | OAuth logins (`~/.claude.json`, `~/.codex/auth.json`, `~/.gemini/oauth_creds.json`), `.env` secrets, local SQLite state, running dev servers |

---

## Features

- **Shared Single-File Ledger**: Atomic, lock-protected JSON storage (`ledger.json`) shared live by the CLI and the `127.0.0.1` web UI (`relay serve`).
- **Three Coding Agents Built In**: Native session discovery, resume commands, state scanning, and agent skills for **Claude Code** (`claude`), **Codex** (`codex`), and **Antigravity** (`anti` / `agy`).
- **Four Transport Modes**:
  - `remote`: Commits dirty work to a personal `wip/handoff-<tag>` branch and prints the push/fetch/apply sequence without touching `main`.
  - `bundle`: Packs commits and uncommitted changes into a single `.bundle` file in your synced drop folder—ideal for repos without a remote or shared team repos where you do not want personal WIP branches.
  - `patch`: Exports binary-safe `git diff HEAD` patches when both machines are on the same base commit.
  - `folder`: Copies non-Git directories via `robocopy` (Windows) / `rsync` (macOS/Linux), excluding `.env` by default.
- **Cross-Shell Command Generation**: Emits ready-to-run **PowerShell** commands for Windows and **zsh / bash** commands for macOS/Linux, each tagged with execution safety (`read`, `local`, or `remote`).
- **Zero Dependencies**: Python 3.9+ standard library only. No `pip install`, no build step, no telemetry, no network calls.

---

## Quick Start

Requires **Python 3.9+** and **Git**. Run the commands below inside the `relay-desk` folder (`relay` stands for `py -3 relay.py` or `relay.cmd` on Windows, and `python3 relay.py` or `./relay` on macOS/Linux).

### 1. Windows Setup (Device A)

```powershell
py -3 relay.py init --device a --host
py -3 relay.py device --which a --drop D:\handoff
py -3 relay.py install-skills
py -3 relay.py serve
```

### 2. macOS Setup (Device B)

Run `chmod +x relay` once, then:

```zsh
python3 relay.py init --device b --host
python3 relay.py device --which b --drop "/Users/<you>/Handoff"
python3 relay.py install-skills
python3 relay.py serve
```

- **Sharing the ledger across both machines**: Point both machines at a `ledger.json` inside a synced folder (iCloud Drive, OneDrive, Dropbox, Syncthing, etc.):
  ```bash
  relay init --device a --ledger "<path-to-synced-folder>/ledger.json" --host
  ```
  Each machine stores its own pointer and device ID locally in `~/.relay-desk/config.json` (which is never synced).
- **Installing agent skills**: `install-skills` installs the `relay-desk` skill for all three agents by default, or individually via `--claude`, `--codex`, and `--anti`:
  - Claude Code → `~/.claude/skills/relay-desk`
  - Codex → `~/.codex/skills/relay-desk` (or `$CODEX_HOME/skills/relay-desk`)
  - Antigravity → `~/.gemini/config/skills/relay-desk` (or `$GEMINI_CONFIG_DIR/skills/relay-desk`)
- **Web dashboard**: `relay serve` starts a loopback-only server at `http://127.0.0.1:8765/` and opens your browser (pass `--no-open` to skip opening the browser).

---

## Daily Workflow

### Registering a Project

```bash
relay add my-app --path /path/to/my-app --tool all
# On the other machine (or with --path-other):
relay set my-app --path-other "D:\workspace\my-app"
```

### Leaving This Machine: Say *"handoff"* (or「接力」) to Your Agent

1. The agent runs `relay handoff <project>` to scan local Git/session facts and print the pre-departure checklist.
2. The agent writes or updates `HANDOFF.md` in the project root (a concise summary of completed work, current state, next steps, and how to verify), records notes via `relay note`, and registers the active session (`relay session add <project> latest --tool <claude|codex|anti>`).
3. Command blocks are handled strictly by safety tag:
   - **Read-only (`只读`)**: Safe for the agent to run directly.
   - **Local-only (`只改本机`)**: Shown to you for confirmation before execution.
   - **Remote-write (`写入远程`)**: Never executed by the agent; printed verbatim for you to run in your own terminal.
4. Each completed item is checked off with `relay check <project> pre <key>`, followed by `relay depart <project>` to mark the project as **in transit**.

### Arriving at the Other Machine: Say *"pickup"* (or「接棒」) to Your Agent

1. The agent runs `relay pickup <project>` to display handoff context and the post-arrival checklist.
2. You and the agent step through the arrival commands (verifying a clean working tree, pulling/fetching or applying the bundle/patch, restoring any extra local files).
3. The agent reads `GEMINI.md` / `AGENTS.md`, `HANDOFF.md`, and the next action in the ledger.
4. Once complete, the agent runs `relay arrive <project>`, transferring ownership to the receiving device and re-scanning local Git state.

### Returning to the Original Machine

Run a reverse handoff so your code catches up, then resume your original local session natively:

- **Claude Code**: `claude --resume <id>` (or `claude --continue`)
- **Codex**: `codex resume <id>` (or `codex resume --last`)
- **Antigravity**: `agy --conversation <id>` (or `agy --continue`, or open from the Desktop / IDE sidebar)

---

## CLI Reference

When `PROJECT` is omitted, `relay` resolves the project from your current working directory.

| Command | Description |
|---|---|
| `init [--device a\|b] [--ledger PATH] [--host]` | Write machine-local config (`~/.relay-desk/config.json`); `--host` records this machine's hostname. Creates an empty ledger if missing. |
| `status [PROJECT] [--json]` | Without `PROJECT`: one-line summary per project. With `PROJECT`: full details, 4-tier board, and registered sessions with resume commands. |
| `scan [PROJECT] [--all]` | Refresh local Git branch, dirty count, remotes, state file presence, and session metadata. `--all` scans all projects plus global agent state. |
| `add NAME --path PATH [--tool T] [--id ID]` | Register a project and scan immediately (`T`: `claude`, `codex`, `anti`, `both`, `claude+anti`, `codex+anti`, `all`). |
| `set PROJECT [...]` | Update project fields (`--path-here`, `--path-other`, `--alt-path`, `--tool`, `--status`, `--transport`, `--worktree`, `--device`). |
| `note PROJECT [...]` | Record `--last` task, `--next` action, `--note`, and `--verify` (`none\|pass\|partial\|fail`, `--verify-cmd`, `--verify-note`). |
| `sessions [PROJECT] [--tool claude\|codex\|anti] [--json]` | Discover local sessions for this project on the current machine (`*` marks registered sessions). |
| `session add PROJECT ID\|latest [--tool T] [...]` | Register a session (`latest` selects the newest discovered session for that tool). |
| `session rm PROJECT ID` | Unregister a session by internal ID or `sessionId`. |
| `handoff PROJECT [--transport T] [--to D]` | Start a handoff from this device and print the pre-departure checklist & commands. |
| `plan PROJECT [--phase pre\|post] [--json]` | Reprint the checklist and command blocks for an active handoff. |
| `check PROJECT pre\|post KEY [--undo]` | Check or uncheck a checklist item. |
| `depart PROJECT` | Mark the active handoff as `transit`. |
| `pickup PROJECT` | Print handoff context, source sessions, and the post-arrival checklist on the receiving machine. |
| `arrive PROJECT` | Complete the handoff, append to history (last 10 kept), switch project `device`, and scan. |
| `cancel PROJECT` | Cancel an in-progress handoff. |
| `conflict add PROJECT --kind K [...]` | Log a conflict (`both`, `merge`, `instr`, `session`, `other`) with files and resolution notes. |
| `device [--which a\|b] [--name N] [--os O] [--drop PATH]` | View or update device settings (`windows`, `mac`, `linux`, and drop folder path). |
| `export [FILE]` | Export ledger backup JSON to `FILE` or stdout. |
| `import FILE [--replace]` | Import a backup (merges by project ID keeping newer `updatedAt` by default; `--replace` overwrites all). |
| `serve [--port 8765] [--no-open]` | Start the local web UI server bound strictly to `127.0.0.1`. |
| `install-skills [--claude] [--codex] [--anti] [--dry-run] [--force]` | Install agent skills with `{{RELAY}}` resolved for this machine. |

---

## Antigravity, Claude Code & Codex Migration Notes (Win ↔ Mac)

- **Workspace Instructions & Customizations**: `CLAUDE.md`, `AGENTS.md`, `GEMINI.md`, `.claude/`, `.codex/`, and `.agents/` (skills, rules, plugins, hooks) are tracked in project state and travel cleanly via Git or `git bundle`.
- **Global Rules, Skills & MCP Configs**: In the Web UI's **Registry (状态登记)** tab, toggling on detected global items (`~/.claude/skills/`, `~/.codex/skills/`, `~/.gemini/GEMINI.md`, `~/.gemini/config/skills/`, `~/.gemini/config/mcp_config.json`, etc.) generates bidirectional PowerShell (`robocopy` / `Copy-Item` / `Compare-Object`) and macOS zsh (`rsync` / `cp` / `diff`) commands. Configs that may contain machine-specific paths or tokens are copied side-by-side for diffing rather than blindly overwritten.
- **Session Transcripts & Antigravity `brain/` Artifacts**: Local sessions remain on their origin machine by default. If you explicitly set a session's carry mode to `transcript`, `relay-desk` generates drop-folder copy commands for Claude Code `.jsonl` transcripts or Antigravity `~/.gemini/antigravity/brain/<id>/` directories (preserving implementation plans, reports, and `transcript.jsonl` for the new session to reference without touching local SQLite session databases).

---

## Security & Design Guarantees (What It Never Does)

1. **No Network Access**: `relay-desk` never runs `git push`, `git fetch`, `git pull`, or any outbound network request. It only prints commands.
2. **Never Reads Credentials**: `~/.claude.json`, `.credentials.json`, `~/.codex/auth.json`, `~/.gemini/oauth_creds.json`, `.env`, and SSH keys are checked for file existence *only*—never opened, copied, or recorded.
3. **Summaries Are Not Sessions**: `HANDOFF.md` and `ledger.json` are honest context summaries. Local sessions can only be resumed natively on the machine where they were created.
4. **Never Mutates Your Repos**: `relay` only writes to `ledger.json` (and its `.lock` file), `~/.relay-desk/config.json`, and the skill folders targeted by `install-skills`.
5. **Loopback-Protected Web UI**: `relay serve` binds exclusively to `127.0.0.1`, validates `Host`, `Origin`, and `X-Relay-Desk` headers, and serves no arbitrary filesystem paths.

---

## Project Structure

```
relay-desk/
├── relay.py                 # Cross-platform CLI entry point
├── relay / relay.cmd        # POSIX / Windows wrapper scripts
├── relay-desk.html          # Single-file Web UI (also compatible as a claude.ai Artifact)
├── relaydesk/
│   ├── ledger.py            # Atomic JSON storage, file locking, normalisation & device lookup
│   ├── scan.py              # Read-only Git facts, state presence & Claude/Codex/Antigravity session discovery
│   ├── plan.py              # 4-tier classification, handoff checklists & cross-OS shell command generator
│   ├── cli.py               # Command-line interface & skill installer
│   └── server.py            # Local 127.0.0.1 HTTP server & JSON API
├── skills/
│   ├── claude/relay-desk/   # Claude Code SKILL.md template
│   ├── codex/relay-desk/    # Codex SKILL.md template
│   └── anti/relay-desk/     # Antigravity SKILL.md template
├── docs/
│   └── SPEC.md              # Architecture & module specification
└── tests/                   # Standard-library unittest suite
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
