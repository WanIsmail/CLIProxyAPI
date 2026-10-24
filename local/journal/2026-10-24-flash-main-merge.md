# Flash maintenance main merge — 2026-10-24 NZDT

The operator authorized merging the worktree changes into main, committing and
pushing. The owning repository is CLIProxyAPI; Dragon's unrelated work remains
untouched. The push target is `fork/main`, the user's `WanIsmail/CLIProxyAPI`
repository. Upstream `origin` is not a push target.

## Contents and conflict resolution

- Compaction branch: `7e2c5f0b` plus probe-format correction `a71950f2`.
  Its already committed native-task parent `65674222` is included by ancestry.
  The runtime launcher remains represented by its exact reproducible patch
  artifacts; credentials and runtime directories are excluded from Git.
- Streaming branch: `657a01ef`, based on official v7.3.3 `7bbfeaf8` and the
  existing local account-limits/transport overlay. The server implementation
  after merging is byte-identical to that tested branch. Additional retained
  transport regression tests are the only internal source difference.
- Resolved the ignore-rule conflict by retaining both sets of exclusions.
- Retained v7.3.3's per-request connection implementation, fail-closed explicit
  proxy handling and redirect suppression. Preserved the stronger local
  invalid-proxy request assertion and adapted the prior cancellation tests to
  the new connection implementation, with atomic counters for race safety.
- Removed duplicate account-limit test definitions from `server_test.go` while
  retaining all corresponding cases in `codex_account_limits_route_test.go`.
- Python probe helpers now accept the installed native client rather than a
  removed version-specific executable. Explicit client selection remains
  available through `PROBE_CLIENT_BINARY`.

This is source integration with the v7.3.3 baseline already used in production.
It does not upgrade to v8.0.41, install the prepared streaming binary, restart
services, alter credentials or send input to existing tabs.

## Validation

- Full merged Go suite: 96 packages pass; only the previously documented
  `TestOpenAICompatExecutorToolResultContentByInputModalities` failure remains.
  The earlier source-worktree count of 97 included an untracked preimage test
  package under its scratch directory; that package is not part of the merge.
- Server build and scoped vet pass. Local transport, account-limit handlers and
  API package race checks pass, including the retained cancellation regressions.
- Two native clients using the binary built from the merged source each received
  tool arguments before finish and completed successfully at 15:05:10 NZDT.
  The synthetic test consumed no Dragon model capacity and closed its processes.
- Six installed compaction-policy test methods, the launcher shell suite,
  19 model-memory tests, 25 tab-resume tests, Bash syntax, ShellCheck and Python
  lint pass. Native 180K display verification also passed during pre-merge checks.
- The existing model-memory concurrent-writer test failed intermittently during
  validation, including on the unchanged isolated launcher copy. Helper hashes
  matched exactly. An instrumented private copy recorded no missing writes or
  lock-deadline skips; the original isolated test and complete 19-test suite then
  passed without changing the helper or test. The cause of the intermittent
  failure was not established; it is not claimed to be fixed by this merge.
- Source whitespace checks pass. Exact `.patch` artifacts retain unified-diff
  context prefixes and are checked separately rather than stripping those bytes.
  No unresolved merge markers or credential-bearing runtime files are staged.

The baseline test failure was already disclosed when the operator requested
this merge. It remains an explicit qualification limit, not a full-suite PASS.
Dragon's deployment gate is outside this source-only CLIProxyAPI integration.

The merge-time hook additionally reported upstream Python lint finding F841 at
`examples/plugin/scripts/generate_examples.py:522` (unused `ident`). This file's
Git blob is identical to official v7.3.3, so it is an inherited finding rather
than a maintenance-patch change. Direct lint checks on all four local probe and
policy scripts pass. The hooks printed success despite that lint finding and
reported no recognized project type during pre-push; their success banners are
not used as validation evidence. The explicit checks above are authoritative.

Merge commit `58ebfa9bc213a025af98dd37cc0901bfa9ad37e2` was pushed to
`WanIsmail/CLIProxyAPI` and verified by reading remote `refs/heads/main`.
This follow-up documentation commit records the hook finding without changing
the tested server implementation or runtime state.

## Preservation and cleanup

The original checkout stays on `feat/claudex-native-tasks-20261014` so its seven
unrelated dirty tracked files remain byte-for-byte intact. Integration occurs
in a separate `main` worktree. Existing untracked files outside this task are
not staged, stashed, deleted or overwritten.

Private custody root: `tmp/flash-merge-20261024/custody/`. All untracked files
from the two completed source worktrees were moved there with per-file hashes,
sizes and modes verified: 631 compaction files and 61 streaming files. These
include rollback preimages, native fixtures, receipts and the prepared binary;
they are intentionally excluded from the commit. The integration worktree's
validation logs and binary are archived under `custody/merge-flash-20261024/`.

Only these three task-owned worktrees and the two merged source branches are
eligible for removal after push and remote commit verification. Other Dragon
and CLIProxyAPI worktrees, branches and active tabs remain in place. No force
push, history rewrite or destructive cleanup is part of this operation.
