# Local CLIProxyAPI and Claudex changes

## 2026-10-24

- Installed Claudex launcher build 2026.10.24.1: Flash model-specific automatic
  compaction window reduced from 200000 to 180000. No service restart. Existing
  process settings require an idle `/autocompact 180k` or launch/resume.
- Prepared proxy build `7.3.3-local.account-limits.flash-stream.3`, not installed:
  stream active tool arguments before finish, preserving quote repair and
  sequential blocks. Two native clients demonstrate early argument delivery.
- Audited upstream v8.0.41. Reasoning aliases and configurable bootstrap
  keepalives exist upstream; tool-argument buffering remains. Account-limits,
  local transport protections and separate gateway patches require preservation.
- [Detailed note and upgrade requirements](flash-compaction-and-streaming-2026-10-24.md)
  and [journal](journal/2026-10-24-flash-compaction-and-streaming.md).

## 2026-10-22

- Installed the 200K per-model launcher policy, superseded by Oct24's 180K
  default. [Original installation note](claudex-flash-autocompact-2026-10-22.md).
- Installed proxy build `7.3.3-local.account-limits.flash-stream.2`, restoring
  bootstrap keepalives and forwarding modern reasoning fields. This remains
  the production proxy. [Original repair note](flash-stream-repair-2026-10-22.md).

## 2026-09-20

- Restored the local account-limits endpoint and transport protections on
  v7.3.3. These remain mandatory upgrade dependencies.
  [Original note](upgrade-2026-09-20-v7.3.3-account-limits.md).
