# Changelog

## Unreleased

### Added

- Encrypted asynchronous cloud handoff with per-device identities, a shared pairing fingerprint, Ed25519 signatures, and libsodium streaming encryption.
- `StorageProvider` adapters for local directories and `rclone` remotes. The integration suite exercises WebDAV and S3-compatible services on loopback.
- Git bundle and workspace transfer for eligible staged, unstaged, and untracked changes, plus snapshots of non-Git folders.
- Searchable archives of supported Claude Code, Codex, and Antigravity conversations, with gap reporting and deduplication across transfers.
- Cloud pairing, inbox, and transcript views in the local workbench. Agent skills can invoke the cloud workflow after pairing.

### Changed

- The CLI adds `cloud remotes`, `pair`, `send`, `inbox`, `receive`, and `transcripts`. The manual checklist workflow remains available without cloud dependencies.
- Project and session matching now resolves macOS `/var` and `/private/var` aliases and normalizes Unicode paths.
- Device setup records platform information used by cross-platform filename checks.

### Fixed

- Reject in-place receives that would remove Git-ignored local files, replace divergent branches or tags, or collide with locally preserved credential files. Compatible branch updates can fast-forward. Local-only refs are retained.
- Keep the original encrypted package and signed ready marker when resuming an interrupted publication. Validate a signed receipt against the handoff ID, recipient, project, and package SHA-256 before marking the transfer acknowledged.
- Complete archive writes before applying the workspace. Prepare new destinations before moving them into place. Resume ledger updates and receipt delivery after the workspace has been applied, preserving subsequent local edits.

## v1

- Introduced the two-device handoff ledger and manual checklists for Git remote, bundle, patch, and folder transfers.
- Added CLI, local workbench, and agent skills for project registration, handoff records, and session lookup.
