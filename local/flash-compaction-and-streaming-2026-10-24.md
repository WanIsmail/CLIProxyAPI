# Flash compaction and tool-stream investigation

2026-10-24, Pacific/Auckland (NZDT, UTC+13).

## Deployment state

The generic Claudex launcher's Flash model settings are now 180000 tokens for
both `dragon-qwen3.8-flash-next-nvfp4` and
`claude-gateway-dragon-qwen3-8-flash-next-nvfp4`. Launcher build 2026.10.24.1
was installed at 13:56:38 NZDT without restarting services. Other providers
retain their existing default and explicit operator overrides still win.
The backend remains at 262144 tokens, three sequences and its previous memory
allocation. The separately qualified `claudex-flash-next.sh` explicit profile
is unchanged; this is the generic `claudex` default used by the reported tabs.

Launcher SHA-256:
`9e3ef101dda960776b5349a867103ac025e93cc95c07fb2f784e43b811b5de6b`.
Rollback and exact preimages:
`runtime/backups/flash-autocompact-180k-20261024/`.
Reproduction delta:
`local/patches/claudex-flash-autocompact-180k-2026-10-24.patch`, applied after
`local/patches/claudex-flash-autocompact-2026-10-22.patch`.

Fresh launches and resumes receive 180K. Existing processes retain startup
settings: use `/autocompact 180k` when the client is idle, or resume through the
updated launcher. No input was injected into existing tabs. The setting is a
compaction window, not a guarantee of compaction at exactly 180000 input tokens;
native reserves and model-capacity clamping still apply.

The running proxy remains `7.3.3-local.account-limits.flash-stream.2`, SHA-256
`56fa56e3daa2ebfc67e6032fa0316b79456a3acb9967e1313b4ca56f6ffc03f3`.
Neither a proxy repair nor an upstream upgrade has been installed today.

## Timeout investigation and prepared repair

The reported Oct24 keywatcher and z transcript errors were separated from their
last yielded responses by approximately 12m30s. Their recorded input was about
96.5K, with no context-length exception in those error records. These are
historical observations from the 13:46-13:47 NZDT inspection, not current pane
health. This supports investigating stream inactivity independently of the
compaction precaution. It does not identify the exact missing upstream chunk
for either original request.

A deterministic executor test held upstream completion until the downstream
received a tool-argument fragment. The installed source failed: arguments
were accumulated until finish, hiding real generation from the native client.
The new implementation streams fragments for the currently open tool block,
retains the existing single-quote repair across fragment boundaries, flushes
arguments when a delayed identity arrives, and avoids duplicating input at
completion. Parallel blocks remain sequential. Tests cover escaping, Unicode,
unfinished strings, delayed identity, parallel calls and interleaved content.

Candidate version: `7.3.3-local.account-limits.flash-stream.3`.
Candidate SHA-256:
`3f470cc539ca937db2052c22b155bd3e4365b6a1af22495e169ec68bc8a3b04d`.
Patch: `local/patches/v7.3.3-local-tool-stream-2026-10-24.patch`.
Patch SHA-256:
`03a11db1480e8f8c6dd7fcf611ac67c4c5f59ea7f79a37e09509d7473bc1d70f`.
Source and candidate are in `.worktrees/flash-stream-keepalive/`; exact custody
is recorded in its `tmp/tool-stream-manifest.json`.

Apply this delta after the existing v7.3.3 account-limits overlay and Oct22
Flash-stream overlay. It is not a patch for v8.0.41. The change includes the
executor regression and shared JSON streaming utility as well as translation.

## Verification and limits

- Launcher tests: 6 new-policy methods, complete model shell suite, 19
  model-memory tests and 25 tab-resume tests pass; syntax and ShellCheck pass.
  Installed-byte policy tests also pass. Native Claude Code 2.1.310 showed
  `180k tokens (from settings)` at 14:02:51 NZDT in an isolated terminal.
  The dialog was canceled and the disposable process closed.
- The new executor regression failed before repair and passes afterward.
  Full utility and OpenAI-to-Claude translator packages pass. Focused race
  checks, Claude handler, transport helper, local account-limit and API package
  tests pass. Server build, vet and diff checks pass.
- Two native 2.1.310 clients against the installed binary each received one
  combined argument delta only after finish (14:06:14 NZDT). Against the
  candidate each received two deltas, including progress before finish, and
  completed successfully (14:06:59 NZDT). These used a synthetic loopback
  upstream and a single allowed read of fixture data; zero Dragon requests.
  Receipt files: `tmp/native-tool-stream-{before,after}/receipt.json` in the
  source worktree. All disposable processes closed; no Zellij tab was created.
- Full Go suite: 97 packages pass; the preexisting
  `TestOpenAICompatExecutorToolResultContentByInputModalities` failure remains.
  It was previously reproduced on unchanged v7.3.3. This is not an all-suite
  PASS. Current log: `tmp/tool-stream-full-suite.log`.
- No long real compaction or original production timeout has been reproduced
  to completion today. Queuing, genuine upstream silence, and long output from
  parallel tool blocks waiting behind the open block can still cause inactivity.
  This patch fixes the demonstrated argument-buffering path, not every timeout.
- Dragon deployment sources are unchanged; its aggregate static gate does not
  apply to these launcher/proxy changes. Relevant gates are recorded above.

Before any installation, recheck candidate/preimage hashes and active requests,
preserve rollback and credentials, and arrange an authorized quiet window.
Restarting the proxy also restarted its dependent gateway in the previous
installation, so both services must be treated as affected. Existing clients
can use the server repair on their next request without relaunching.

## Upstream audit and local patch preservation

Latest release checked: [v8.0.41](https://github.com/router-for-me/CLIProxyAPI/releases/tag/v8.0.41),
published 2026-10-24 07:25:13 NZDT, commit
`8289ec4c71ad914d0612c2776352b76ba3d355a6`. Its source was fetched and inspected;
it has not been built, ported or qualified with our overlays.

- Alternative reasoning fields were added in commit
  [77820cb2](https://github.com/router-for-me/CLIProxyAPI/commit/77820cb2), first
  contained by v7.3.5. A future upgrade should use and test upstream behavior
  rather than blindly reapply our equivalent reasoning-alias patch.
- Bootstrap keepalives are available from v8.0.27, commit
  [519d590e](https://github.com/router-for-me/CLIProxyAPI/commit/519d590e).
  They require `streaming.send-keepalive-before-ft: true` together with a
  positive `keepalive-seconds`. Our current configuration has 15 seconds and
  omits the new boolean; copying it unchanged would lose the local early
  heartbeat behavior. This option has not been added to the live v7 config.
- v8.0.41 still accumulates OpenAI tool arguments and calls `FixJSON` only
  during finalization. An upgrade alone does not fix the reproduced stall.
- The local `/v0/local/codex/account-limits` endpoint is absent upstream.
  Port its schema-version-1 sanitized response, direct-peer/host/browser
  restrictions, no cross-origin headers, no trailing-slash redirect and local
  route handling before home-heartbeat middleware. Retain its regressions and
  verify actual statusline cache refresh, not just model discovery.
- Our explicit-proxy fail-closed dialer and `WithRedirectsDisabled` hook are
  absent from the upstream transport helper. Port and test their behavior
  against the changed v8 transport; do not overwrite them with stock binaries.
- The separately installed Claudex gateway retains the newest two Flash
  images, omission markers, token-count handling, effort mapping and loopback
  boundaries. Its binary hash remains
  `6585c433406c8013eeecfec39e7de44d62d199e23683144e1a7f5f3ae33ca241`.
  Source custody for the image patch is Dragon's `.worktrees/image-history/`;
  journal `qwen3.8-27B-fp8/journal/2026-10-21-wed-1532-keywatcher-image-history.md`.
  This is a separate component and must not be replaced by an upstream proxy
  upgrade. The runtime launcher/model-memory/180K settings are also separate.

The major-version migration changes the Go module to `/v8` and requires
Go 1.26.10. Qualify the complete port in isolation, preserve operator-owned
configuration and all unrelated providers, then perform a bounded installation
with rollback. No upstream upgrade is authorized or claimed by this audit.
